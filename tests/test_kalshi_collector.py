"""Kalshi collector: row-building/tiering, and idempotent resolution recording
(brief section 3/Phase 10). Mirrors the style of test_universe.py /
test_resolutions.py; no real network calls -- KalshiMarket built directly from
fixture dicts, matching the live-verified /markets shape.

Async collector functions are driven via asyncio.run() from plain `def test_*`
functions rather than `async def` + `@pytest.mark.asyncio`: pytest-asyncio is
not a project dependency (see pyproject.toml), so an `async def test_*` would
never actually execute its body under plain pytest (the coroutine is returned
un-awaited and the test still reports a pass) -- asyncio.run() keeps these
tests meaningful without depending on a plugin that isn't installed.
"""

from __future__ import annotations

import pytest

import asyncio

from lab.api.kalshi import KalshiMarket
from lab.collect.kalshi_collector import (
    assign_kalshi_tier,
    extract_kalshi_payout,
    kalshi_market_row,
    unresolved_kalshi_markets,
    watch_kalshi_resolutions,
)
from lab.store import db
from lab.util import load_config

CONFIG = load_config()


def _market(**kwargs) -> KalshiMarket:
    base = {
        "ticker": "KXEZGDPQOQF-26JUN05-T0.8",
        "event_ticker": "KXEZGDPQOQF-26JUN05",
        "title": "Will Euro area GDP growth rate QoQ flash for Q1 2026 be above 0.8%?",
        "rules_primary": "If Euro area GDP growth rate QoQ flash for Q1 2026 is above 0.8, then Yes.",
        "status": "active",
        "result": "",
        "close_time": "2026-05-29T19:59:46Z",
        "expiration_time": "2026-06-12T09:00:00Z",
        "liquidity_dollars": "2000.0000",
        "volume_fp": "8615.00",
        "yes_bid_dollars": "0.4500",
        "yes_ask_dollars": "0.4700",
        "last_price_dollars": "0.4600",
    }
    base.update(kwargs)
    return KalshiMarket.model_validate(base)


class FakeKalshiClient:
    """Stand-in for KalshiClient.market(ticker) in resolution-watcher tests."""

    def __init__(self, markets_by_ticker: dict[str, KalshiMarket]):
        self._markets = markets_by_ticker
        self.calls: list[str] = []

    async def market(self, ticker: str) -> KalshiMarket | None:
        self.calls.append(ticker)
        return self._markets.get(ticker)


# --- row building / tiering -------------------------------------------------


def test_kalshi_market_row_maps_fields():
    m = _market()
    row = kalshi_market_row(m, "economics")
    assert row["condition_id"] == "kalshi:KXEZGDPQOQF-26JUN05-T0.8"
    assert row["venue"] == "kalshi"
    assert row["venue_native_id"] == "KXEZGDPQOQF-26JUN05-T0.8"
    assert row["question"] == m.title
    assert row["description"] == m.rules_primary
    assert row["category"] == "economics"
    assert row["end_date_iso"] == "2026-05-29T19:59:46Z"
    assert row["token_id_yes"] is None and row["token_id_no"] is None
    assert row["neg_risk"] == 0
    assert row["active"] == 1
    assert row["closed"] == 0
    assert row["liquidity_num"] == 2000.0
    assert row["volume_num"] == 8615.0


def test_kalshi_market_row_closed_when_finalized():
    m = _market(status="finalized", result="yes")
    row = kalshi_market_row(m, "economics")
    assert row["active"] == 0
    assert row["closed"] == 1


def test_kalshi_market_row_falls_back_to_expiration_time():
    m = _market(close_time=None)
    row = kalshi_market_row(m, "economics")
    assert row["end_date_iso"] == "2026-06-12T09:00:00Z"


def test_tier_falls_back_to_volume_and_open_interest_without_depth():
    """Depth is unknowable until a market has been snapshotted once, and 1,715
    of 4,803 Kalshi markets were in that state when depth collection started."""
    liquid = CONFIG["venues"]["kalshi"]["tiers"]["liquid"]
    m_liquid = _market(volume_fp=str(liquid["min_volume"]),
                       open_interest_fp=str(liquid.get("min_open_interest", 0)))
    assert assign_kalshi_tier(m_liquid, CONFIG) == ("liquid", None)

    m_tail = _market(volume_fp="0", open_interest_fp="0")
    tier, reason = assign_kalshi_tier(m_tail, CONFIG)
    tail = CONFIG["venues"]["kalshi"]["tiers"]["tail"]
    if tail["min_volume"] > 0 or tail.get("min_open_interest", 0) > 0:
        assert (tier, reason) == ("ignored", "low_liquidity")
    else:
        assert (tier, reason) == ("tail", None)


def test_tier_ignores_kalshis_dead_liquidity_field():
    """Kalshi reports `liquidity_dollars` as 0.0 for every market it publishes
    -- verified 2026-08-09 across all 4,822 collected and against a live API
    sample. Keying the liquid gate on it meant no Kalshi market could ever be
    liquid, which silently excluded the entire venue from the shadow portfolio
    (it scans the liquid tier only)."""
    liquid = CONFIG["venues"]["kalshi"]["tiers"]["liquid"]
    m = _market(liquidity_dollars="0",
                volume_fp=str(liquid["min_volume"]),
                open_interest_fp=str(liquid.get("min_open_interest", 0)))
    assert assign_kalshi_tier(m, CONFIG)[0] == "liquid", (
        "a zero liquidity_dollars must not block the liquid tier"
    )


def test_measured_depth_overrides_the_proxies_on_both_sides():
    """Phase 17 item 2: a tier means "this much real depth", so measured depth
    decides regardless of what volume/open interest say -- in either direction.
    """
    depth_tiers = CONFIG["universe"]["tiers"]
    rich_proxies = _market(volume_fp="10000000", open_interest_fp="1000000")
    poor_proxies = _market(volume_fp="0", open_interest_fp="0")

    # Deep book, worthless proxies -> liquid.
    assert assign_kalshi_tier(
        poor_proxies, CONFIG, depth_usd=depth_tiers["liquid"]["min_depth_usd"]
    ) == ("liquid", None)
    # Empty book, spectacular proxies -> not liquid. Volume is lifetime and
    # says nothing about whether anyone is quoting now.
    assert assign_kalshi_tier(rich_proxies, CONFIG, depth_usd=0.0) == (
        "ignored", "low_liquidity")


def test_kalshi_and_polymarket_share_one_depth_bar():
    """One rule, one threshold, both venues -- not two similar rules that can
    drift apart."""
    import inspect

    from lab.collect import kalshi_collector, universe

    for src in (inspect.getsource(kalshi_collector.assign_kalshi_tier),
                inspect.getsource(universe.assign_tier_with_category)):
        assert 'tiers["liquid"]["min_depth_usd"]' in src
        assert 'tiers["tail"]["min_depth_usd"]' in src
    # and the Kalshi rule reads them out of the SHARED universe block
    ksrc = inspect.getsource(kalshi_collector.assign_kalshi_tier)
    assert 'config["universe"]["tiers"]' in ksrc


def test_top_of_book_usd_distinguishes_missing_from_zero():
    """A missing quote is NULL, never 0.0: downstream, 0.0 asserts 'measured,
    and there is none', which the shadow portfolio's depth filter would read as
    a real observation."""
    from lab.collect.kalshi_collector import _top_of_book_usd

    assert _top_of_book_usd(0.91, 2.64) == pytest.approx(2.4024)
    assert _top_of_book_usd(None, 100.0) is None
    assert _top_of_book_usd(0.5, None) is None
    assert _top_of_book_usd(0.0, 100.0) is None
    assert _top_of_book_usd(0.5, 0.0) is None


# --- resolution watcher: finality + idempotency -----------------------------


def test_extract_kalshi_payout():
    assert extract_kalshi_payout(_market(status="finalized", result="yes")) == 1.0
    assert extract_kalshi_payout(_market(status="finalized", result="no")) == 0.0
    assert extract_kalshi_payout(_market(status="active", result="")) is None
    assert extract_kalshi_payout(_market(status="finalized", result="")) is None


def test_unresolved_kalshi_markets_filters_by_venue(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    db.upsert_market(conn, {
        "condition_id": "0xpoly", "venue": "polymarket", "venue_native_id": "0xpoly",
        "slug": "s", "question": "q2", "category": "politics", "description": "d2",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": "111", "token_id_no": "222",
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    unresolved = unresolved_kalshi_markets(conn)
    assert {r["condition_id"] for r in unresolved} == {"kalshi:T1"}
    conn.close()


def test_watch_kalshi_resolutions_records_and_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    client = FakeKalshiClient({"T1": _market(ticker="T1", status="finalized", result="yes")})

    recorded_first = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded_first == 1
    row = conn.execute(
        "SELECT payout_yes, disputed, source FROM resolutions WHERE condition_id='kalshi:T1'"
    ).fetchone()
    assert row["payout_yes"] == 1.0
    assert row["disputed"] == 0
    assert row["source"] == "kalshi"

    # Second poll: no longer in the unresolved set (it now has a resolutions
    # row), so watch_kalshi_resolutions should not double-count or raise.
    recorded_second = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded_second == 0
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM resolutions WHERE condition_id='kalshi:T1'"
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_watch_kalshi_resolutions_skips_unsettled(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T2", "venue": "kalshi", "venue_native_id": "T2",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    conn.commit()

    client = FakeKalshiClient({"T2": _market(ticker="T2", status="active", result="")})
    recorded = asyncio.run(watch_kalshi_resolutions(client, conn))
    assert recorded == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM resolutions").fetchone()["n"] == 0
    conn.close()


def test_orderbook_inverts_the_no_ladder_into_yes_asks():
    """Kalshi publishes TWO BID ladders, not a bid and an ask: a NO bid at q is
    an offer to sell YES at 1 - q. Getting that inversion wrong would silently
    mirror every Kalshi ask price."""
    import asyncio

    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient

    client = KalshiClient(TokenBucket(rate=100, burst=100))

    async def fake_get_json(path, params=None):
        return {"orderbook_fp": {
            "yes_dollars": [["0.40", "100"], ["0.45", "200"]],   # YES bids
            "no_dollars": [["0.50", "300"], ["0.52", "400"]],    # NO bids
        }}

    client.get_json = fake_get_json
    book = asyncio.run(client.orderbook("TEST"))

    assert book.best_bid == pytest.approx(0.45)          # highest YES bid
    assert book.best_ask == pytest.approx(0.48)          # 1 - highest NO bid (0.52)
    assert book.depth_usd("bid") == pytest.approx(0.45 * 200)
    assert book.depth_usd("ask") == pytest.approx(0.48 * 400)
    # best-first ordering, same contract as the Polymarket book
    assert book.top_levels("bid", 2)[0][0] == pytest.approx(0.45)
    assert book.top_levels("ask", 2)[0][0] == pytest.approx(0.48)


def test_orderbook_returns_an_empty_book_not_none_when_kalshi_has_no_levels():
    """Common for markets that have stopped trading but are still flagged open
    (four of six top-volume markets sampled on 2026-08-09). Must not crash and
    must not be mistaken for zero depth."""
    import asyncio

    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient

    client = KalshiClient(TokenBucket(rate=100, burst=100))

    async def fake_get_json(path, params=None):
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}

    client.get_json = fake_get_json
    book = asyncio.run(client.orderbook("TEST"))
    assert book is not None
    assert book.bids == [] and book.asks == []
    assert book.best_bid is None and book.best_ask is None


def test_series_sync_order_rotates_instead_of_starving_the_tail(tmp_path):
    """`max_series_per_sync` bounded each cycle but the loop walked a fixed
    order and stopped at the cap, so the same head was re-synced hourly and the
    tail was never reached: 82% of 5,009 Kalshi markets unsynced for 3+ days,
    1,772 still flagged active past their end date (2026-08-10)."""
    from lab.collect.kalshi_collector import _series_sync_order
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    try:
        def add(ticker, synced):
            conn.execute(
                "INSERT INTO markets (condition_id, venue, venue_native_id, question,"
                " tier, active, closed, last_synced_ts) VALUES (?,'kalshi',?,?,'tail',1,0,?)",
                (f"kalshi:{ticker}-1", f"{ticker}-26AUG", "q", synced))
        add("FRESH", "2026-08-10T12:00:00+00:00")
        add("STALE", "2026-08-01T12:00:00+00:00")
        add("MIDDLE", "2026-08-05T12:00:00+00:00")
        conn.commit()

        cands = [("FRESH", "economics", None), ("MIDDLE", "economics", None),
                 ("STALE", "economics", None)]
        order = _series_sync_order(conn, [(t, u) for t, _, u in cands], max_series=3,
                                   discovery_share=0.0)
    finally:
        conn.close()

    assert order == ["STALE", "MIDDLE", "FRESH"], "oldest-synced series go first"


def test_discovery_slice_cannot_starve_the_series_that_carry_markets(tmp_path):
    """The non-obvious half. Ordering never-synced first -- the resolution
    watcher's rule -- is wrong here: Kalshi's /series listing returns 10,502
    series against the ~285 that carry open markets, so a never-seen-first
    rotation spends every cycle on empties. The cycle that shipped that
    ordering synced 40 series and saw ZERO markets (2026-08-10).
    """
    from lab.collect.kalshi_collector import _series_sync_order
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    try:
        for i in range(4):
            conn.execute(
                "INSERT INTO markets (condition_id, venue, venue_native_id, question,"
                " tier, active, closed, last_synced_ts) VALUES (?,'kalshi',?,?,'tail',1,0,?)",
                (f"kalshi:KNOWN{i}-1", f"KNOWN{i}-26AUG", "q", "2026-08-01T00:00:00+00:00"))
        conn.commit()

        known = [(f"KNOWN{i}", None) for i in range(4)]
        # Valid, strictly increasing timestamps -- EMPTY199 is the most recent.
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        empties = [(f"EMPTY{i}", (base + timedelta(minutes=i)).isoformat(timespec="seconds"))
                   for i in range(200)]
        order = _series_sync_order(conn, known + empties, max_series=10,
                                   discovery_share=0.2)
    finally:
        conn.close()

    picked_known = [t for t in order if t.startswith("KNOWN")]
    picked_empty = [t for t in order if t.startswith("EMPTY")]
    assert len(order) == 10
    assert len(picked_known) == 4, "every known series must be in a 10-slot cycle"
    assert len(picked_empty) == 6, "the rest goes to discovery, not the other way round"
    # discovery prefers the most recently updated series
    assert picked_empty[0] == "EMPTY199"


def test_unused_discovery_budget_falls_back_to_known_series(tmp_path):
    """A cycle must never be short just because one pool is small."""
    from lab.collect.kalshi_collector import _series_sync_order
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    try:
        for i in range(8):
            conn.execute(
                "INSERT INTO markets (condition_id, venue, venue_native_id, question,"
                " tier, active, closed, last_synced_ts) VALUES (?,'kalshi',?,?,'tail',1,0,?)",
                (f"kalshi:K{i}-1", f"K{i}-26AUG", "q", "2026-08-0%dT00:00:00+00:00" % (i + 1)))
        conn.commit()
        order = _series_sync_order(conn, [(f"K{i}", None) for i in range(8)],
                                   max_series=5, discovery_share=0.2)
    finally:
        conn.close()
    assert len(order) == 5, "no unseen series exist, so all five slots go to known ones"
    assert order[0] == "K0", "still oldest-first"


def test_the_series_cap_is_applied_after_ordering_not_before():
    """The cap has to slice a staleness-ordered list. Slicing the API's own
    order is what made it a permanent cutoff rather than a rotation."""
    import inspect

    from lab.collect import kalshi_collector

    src = inspect.getsource(kalshi_collector.sync_kalshi_universe)
    assert "_series_sync_order(conn, [(t, upd) for t, _, upd in candidates], max_series)" in src, (
        "series must be ordered and budgeted before the cap is applied"
    )
    # and the old early-break out of the category loop must be gone
    assert "if series_processed >= max_series:\n            break" not in src


def test_kalshi_snapshots_carry_open_interest():
    """Phase 15's crowd-size covariates name "Kalshi open interest -- stored
    with snapshots". It was never stored, though the collector already fetches
    the field (it tiers on it), so this costs no request. Forward-only like the
    other Phase 15 covariates: earlier partitions keep NULL."""
    import asyncio
    import tempfile
    from pathlib import Path

    from lab.api.kalshi import KalshiMarket
    from lab.collect.kalshi_collector import snapshot_kalshi_markets
    from lab.store.snapshots import SnapshotStore

    class _Client:
        async def market(self, ticker):
            return KalshiMarket.model_validate({
                "ticker": ticker, "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42",
                "yes_bid_size_fp": "100", "yes_ask_size_fp": "200",
                "open_interest_fp": "1234", "volume_fp": "5000",
            })

    store = SnapshotStore(str(Path(tempfile.mkdtemp()) / "snapshots"))
    written = asyncio.run(snapshot_kalshi_markets(
        _Client(), store, [{"condition_id": "kalshi:T1", "venue_native_id": "T1"}],
        "2026-08-11T00:00:00+00:00"))

    assert written == 1
    df = store.read_range(["2026-08-11"])
    assert df["open_interest"][0] == pytest.approx(1234.0)
    # and a venue that reports none leaves it NULL rather than 0
    from lab.store.snapshots import SNAPSHOT_SCHEMA
    assert "open_interest" in SNAPSHOT_SCHEMA


def test_snapshot_kalshi_rounds_are_per_tier(tmp_path):
    """One venue-wide job made the liquid tier inherit the tail's ~60-minute
    cadence, against guardrail 13's 15-minute liquid freshness bound: 443 of
    464 liquid Kalshi markets were dropped at forecast time every night. The
    liquid round must cover the liquid tier only, on its own bucket, with
    depth; the tail round must not pay for order books."""
    import asyncio

    from lab.api.kalshi import KalshiMarket
    from lab.collect.kalshi_collector import snapshot_kalshi
    from lab.store import db
    from lab.store.snapshots import SnapshotStore

    conn = db.connect(tmp_path / "lab.db")
    for i, tier in enumerate(["liquid", "tail", "tail", "ignored"]):
        db.upsert_market(conn, {
            "condition_id": f"kalshi:T{i}", "venue": "kalshi", "venue_native_id": f"T{i}",
            "slug": f"t{i}", "question": "Q?", "category": "economics", "description": "d",
            "end_date_iso": "2027-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
            "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
            "tier": tier,
        })
    conn.commit()

    books_asked: list[str] = []

    class _Client:
        async def market(self, ticker):
            return KalshiMarket.model_validate({
                "ticker": ticker, "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42",
                "yes_bid_size_fp": "100", "yes_ask_size_fp": "200", "volume_fp": "1",
            })

        async def orderbook(self, ticker):
            books_asked.append(ticker)
            return None

    cfg = {"venues": {"kalshi": {"snapshot_interval_minutes": {"liquid": 5, "tail": 30},
                                 "snapshot_concurrency": 4}},
           "collect": {"book_depth_levels": 10},
           # The round consults the null-control snapshot policy (PAP 9.26)
           # before it fetches anything; these rows are all economics, so the
           # policy has nothing to decide here -- but it is read, not guessed.
           "universe": {"null_control": {"category": "sports", "sample_size": 30,
                                         "random_seed": 42, "snapshot_unsampled": False}}}
    store = SnapshotStore(tmp_path / "snapshots")

    assert asyncio.run(snapshot_kalshi(_Client(), conn, store, cfg, tier="liquid")) == 1
    assert books_asked == ["T0"]          # depth for the liquid tier only

    books_asked.clear()
    # tail round: everything not liquid, and no order-book requests at all
    assert asyncio.run(snapshot_kalshi(_Client(), conn, store, cfg, tier="tail")) == 3
    assert books_asked == []
    conn.close()


def test_snapshot_round_runs_concurrently_within_its_bound(tmp_path):
    """The rounds were latency-bound, not rate-bound: a full Kalshi round took
    ~60 minutes at 1.2 req/s against an 8 req/s ceiling because every await was
    sequential. Concurrency must actually overlap, and must respect its bound
    -- politeness stays with the TokenBucket, which caps the rate either way."""
    import asyncio

    from lab.api.kalshi import KalshiMarket
    from lab.collect.kalshi_collector import snapshot_kalshi_markets
    from lab.store.snapshots import SnapshotStore

    class _Client:
        def __init__(self):
            self.in_flight = 0
            self.peak = 0

        async def market(self, ticker):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.01)
            self.in_flight -= 1
            return KalshiMarket.model_validate({
                "ticker": ticker, "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42",
                "volume_fp": "1",
            })

    markets = [{"condition_id": f"kalshi:T{i}", "venue_native_id": f"T{i}"} for i in range(20)]

    seq = _Client()
    store = SnapshotStore(tmp_path / "seq")
    assert asyncio.run(snapshot_kalshi_markets(seq, store, markets,
                                               "2026-08-18T00:00:00+00:00", concurrency=1)) == 20
    assert seq.peak == 1                      # default is exactly the old behaviour

    par = _Client()
    store2 = SnapshotStore(tmp_path / "par")
    assert asyncio.run(snapshot_kalshi_markets(par, store2, markets,
                                               "2026-08-18T00:00:00+00:00", concurrency=4)) == 20
    assert 1 < par.peak <= 4                  # overlapped, and never past the bound


def test_kalshi_event_ticker_derives_the_venues_own_grouping():
    """Kalshi tickers are SERIES-EVENT-OUTCOME and the first two segments are
    the venue's own event_ticker -- verified against the live API on 40 sampled
    tickers, 40 matches. That check is what licenses backfilling already
    resolved markets, which the sync will never revisit."""
    from lab.store.db import kalshi_event_ticker

    assert kalshi_event_ticker("KXUSPPIYOY-26AUG13-T4.0") == "KXUSPPIYOY-26AUG13"
    assert kalshi_event_ticker("KXPRESPERSON-28-DTRU") == "KXPRESPERSON-28"
    assert kalshi_event_ticker("KXGOVCA-26") == "KXGOVCA-26"       # already an event
    assert kalshi_event_ticker("NOHYPHEN") is None
    assert kalshi_event_ticker(None) is None


def test_kalshi_legs_of_one_event_collapse_to_one_cluster(tmp_path):
    """Regression, 2026-08-26: Kalshi markets are mutually-exclusive legs of ONE
    numeric question -- 24 threshold buckets of one PPI release, 18 candidates
    in one election -- and NONE carried an event_id (2 of 28,634 resolved scored
    rows, against 72.8% on Polymarket). Every leg therefore counted as its own
    cluster, inflating the unit every confidence interval is computed over by
    6.8x, and 8.8x in H1's own >=30-day bucket. The plan forbids exactly this:
    "clustering by venue-market would overstate n. Naive CIs would lie." """
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    legs = ["KXUSPPIYOY-26AUG13-T4.0", "KXUSPPIYOY-26AUG13-T4.1", "KXUSPPIYOY-26AUG13-T4.2"]
    other = "KXPRESPERSON-28-DTRU"
    for t in legs + [other]:
        db.upsert_market(conn, {
            "condition_id": f"kalshi:{t}", "venue": "kalshi", "venue_native_id": t,
            "slug": None, "question": "Q?", "category": "economics", "description": "d",
            "end_date_iso": "2027-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
            "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
            "tier": "tail",
        })
    conn.commit()

    res = db.migrate_kalshi_event_clusters(conn)
    assert res["markets_linked"] == 4 and res["events"] == 2

    ids = {r[0] for r in conn.execute(
        "SELECT event_id FROM markets WHERE venue_native_id LIKE 'KXUSPPIYOY%'")}
    assert ids == {"kalshi:KXUSPPIYOY-26AUG13"}          # three legs, one cluster
    assert conn.execute("SELECT event_id FROM markets WHERE venue_native_id = ?",
                        (other,)).fetchone()[0] == "kalshi:KXPRESPERSON-28"

    # idempotent: a second pass links nothing new
    assert db.migrate_kalshi_event_clusters(conn)["markets_linked"] == 0
    conn.close()


def test_kalshi_backfill_never_touches_polymarket_or_existing_links(tmp_path):
    """The migration must not reach beyond its venue, and must not overwrite a
    grouping some other mechanism already established."""
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "0xpoly", "venue": "polymarket", "venue_native_id": "0xpoly",
        "slug": "s", "question": "q", "category": "politics", "description": "d",
        "end_date_iso": "2027-01-01T00:00:00Z", "token_id_yes": "1", "token_id_no": "2",
        "neg_risk": 1, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "tail",
    })
    db.upsert_market(conn, {
        "condition_id": "kalshi:KXA-26-X", "venue": "kalshi", "venue_native_id": "KXA-26-X",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2027-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "tail",
    })
    conn.execute("UPDATE markets SET event_id = 'evt_preexisting' WHERE condition_id = ?",
                 ("kalshi:KXA-26-X",))
    conn.commit()

    db.migrate_kalshi_event_clusters(conn)
    assert conn.execute("SELECT event_id FROM markets WHERE condition_id='0xpoly'").fetchone()[0] is None
    assert conn.execute("SELECT event_id FROM markets WHERE condition_id=?",
                        ("kalshi:KXA-26-X",)).fetchone()[0] == "evt_preexisting"
    conn.close()
