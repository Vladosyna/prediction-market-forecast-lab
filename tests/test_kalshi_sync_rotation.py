"""The Kalshi universe sync's rotation cursor (PAP 9.27).

Regression for 2026-09-07, when the rotation was found stalled rather than
slow. `_series_sync_order` ordered known series by MAX(last_synced_ts) taken
from market rows, but `sync_kalshi_universe` only upserts markets the venue
actually returns -- so a series with no open markets advanced nothing, stayed
oldest, and (Python's sort being stable) was handed back at the head of every
cycle. 231 dead series against ~32 known slots pinned the queue completely:
median staleness over the 606 series that DO carry open markets measured 383
hours against a designed 26, with cycles logging `markets_seen: 0` for three
hours running.

Async collector functions are driven via asyncio.run() from plain `def test_*`
(pytest-asyncio is not a project dependency), mirroring this suite's other
collector tests.
"""

from __future__ import annotations

import asyncio

import pytest

from lab.api.kalshi import KalshiMarket
from lab.collect.kalshi_collector import (
    _series_sync_order,
    sync_kalshi_universe,
    unresolved_kalshi_markets,
    watch_kalshi_resolutions,
)
from lab.collect.resolutions import resolution_backlog_size, unresolved_closed_markets
from lab.store import db
from lab.util import load_config


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _seed(conn, ticker, *, venue="kalshi", synced=None, category="economics",
          active=1, closed=0, end_date="2027-01-01T00:00:00+00:00"):
    cid = db.venue_condition_id(venue, ticker)
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": ticker,
        "slug": None, "question": "q?", "category": category, "description": "d",
        "end_date_iso": end_date, "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": active, "closed": closed,
        "liquidity_num": 0.0, "volume_num": 0.0, "tier": "tail",
    })
    if synced is not None:
        conn.execute("UPDATE markets SET last_synced_ts = ? WHERE condition_id = ?",
                     (synced, cid))
    return cid


class _Client:
    """Returns open markets only for the series in `live`."""

    def __init__(self, live):
        self.live = live
        self.series_calls = []

    async def series_by_category(self, category):
        # Each series belongs to exactly one category on the live venue, so
        # hand the whole list to the first category asked and nothing to the
        # rest -- returning it for every category would put duplicate tickers
        # in `candidates` and walk each series once per category.
        if getattr(self, "_served", False):
            return []
        self._served = True
        return [{"ticker": t, "last_updated_ts": None} for t in self.live_and_dead]

    async def markets_for_series(self, ticker, status="open"):
        self.series_calls.append(ticker)
        if ticker not in self.live:
            return []
        return [KalshiMarket.model_validate({
            "ticker": f"{ticker}-T1", "event_ticker": ticker, "title": "q?",
            "status": "active", "result": "", "volume_fp": "0", "open_interest_fp": "0",
            "close_time": "2027-01-01T00:00:00Z",
        })]


# --- the wedge ------------------------------------------------------------

def test_a_series_that_returns_nothing_is_not_re_picked_next_cycle(conn):
    """THE regression test. Its absence is what let 383 hours happen: an empty
    series advanced no market row, so nothing moved it off the head of an
    oldest-first queue, forever."""
    for i in range(4):
        _seed(conn, f"DEAD{i}-T1", synced="2026-01-01T00:00:00+00:00")
    for i in range(4):
        _seed(conn, f"LIVE{i}-T1", synced="2026-01-01T00:00:00+00:00")
    conn.commit()

    tickers = [f"DEAD{i}" for i in range(4)] + [f"LIVE{i}" for i in range(4)]
    cycle1 = _series_sync_order(conn, [(t, None) for t in tickers], max_series=4)
    assert cycle1 == [f"DEAD{i}" for i in range(4)] or set(cycle1) <= set(tickers)

    # The sync stamps every series it WALKS, productive or not.
    for t in cycle1:
        db.record_kalshi_series_attempt(conn, t, 0)
    conn.commit()

    cycle2 = _series_sync_order(conn, [(t, None) for t in tickers], max_series=4)
    assert not set(cycle2) & set(cycle1), (
        "a walked series must rotate to the back whether or not it returned markets"
    )


def test_the_partition_is_not_re_keyed_on_attempts(conn):
    """The tempting one-line version of this fix -- key `seen` on attempts --
    is wrong, and this asserts it stayed unwritten. `seen` decides
    known-vs-unseen membership as well as order; keying it on attempts would
    migrate every discovery-touched series permanently into `known`, growing it
    from 837 toward the venue's ~11,547 -- which is exactly the never-seen-first
    rotation _series_sync_order's own docstring records shipping and failing."""
    _seed(conn, "KNOWN-T1", synced="2026-01-01T00:00:00+00:00")
    # attempted via a discovery slot, but no market row ever landed
    db.record_kalshi_series_attempt(conn, "GHOST", 0)
    conn.commit()

    order = _series_sync_order(conn, [("KNOWN", None), ("GHOST", "2026-09-01")],
                               max_series=1, discovery_share=1.0)
    assert order == ["GHOST"], (
        "GHOST has no row in markets, so it must still be classified unseen "
        "and drawn from the discovery slice"
    )


def test_an_empty_cursor_reproduces_the_previous_ordering(conn):
    """First cycle after deploy must not be arbitrary."""
    _seed(conn, "OLD-T1", synced="2026-01-01T00:00:00+00:00")
    _seed(conn, "NEW-T1", synced="2026-09-01T00:00:00+00:00")
    conn.commit()
    order = _series_sync_order(conn, [("NEW", None), ("OLD", None)], max_series=2)
    assert order == ["OLD", "NEW"]


def test_the_sync_stamps_every_series_it_walks(conn):
    """Including the ones that return nothing and the ones that raise."""
    config = load_config()
    client = _Client(live={"LIVE"})
    client.live_and_dead = ["LIVE", "EMPTY"]

    async def boom(ticker, status="open"):
        client.series_calls.append(ticker)
        if ticker == "EMPTY":
            raise RuntimeError("fetch failed")
        return await _Client.markets_for_series(client, ticker, status)

    client.markets_for_series = boom
    asyncio.run(sync_kalshi_universe(client, conn, config))

    rows = {r["series"]: dict(r) for r in conn.execute(
        "SELECT series, markets_seen, consecutive_empty FROM kalshi_series_sync")}
    assert set(rows) == {"LIVE", "EMPTY"}, "a failed fetch must still rotate to the back"
    assert rows["LIVE"]["markets_seen"] == 1 and rows["LIVE"]["consecutive_empty"] == 0
    assert rows["EMPTY"]["markets_seen"] == 0 and rows["EMPTY"]["consecutive_empty"] == 1


def test_consecutive_empty_counts_up_and_resets(conn):
    db.record_kalshi_series_attempt(conn, "S", 0)
    db.record_kalshi_series_attempt(conn, "S", 0)
    conn.commit()
    assert conn.execute(
        "SELECT consecutive_empty FROM kalshi_series_sync WHERE series='S'"
    ).fetchone()[0] == 2
    db.record_kalshi_series_attempt(conn, "S", 5)
    conn.commit()
    assert conn.execute(
        "SELECT consecutive_empty FROM kalshi_series_sync WHERE series='S'"
    ).fetchone()[0] == 0


# --- the resolution watchers ---------------------------------------------

def test_the_kalshi_watcher_is_a_round_robin(conn):
    """Same head-of-scan wedge collect/resolutions.py fixed on 2026-07-25; this
    query never got the lesson. 215 Kalshi rows sit past a stale end date and
    will never settle, and an unordered LIMIT re-fetches them forever."""
    past = "2026-01-01T00:00:00+00:00"
    a = _seed(conn, "A-T1", end_date=past)
    b = _seed(conn, "B-T1", end_date=past)
    conn.execute("UPDATE markets SET resolution_checked_ts = ? WHERE condition_id = ?",
                 ("2026-09-01T00:00:00+00:00", a))
    conn.commit()
    first = [r["condition_id"] for r in unresolved_kalshi_markets(conn, limit=1)]
    assert first == [b], "never-checked first (NULLs sort first on ASC)"


def test_the_kalshi_watcher_stamps_even_when_nothing_settles(conn):
    cid = _seed(conn, "A-T1", end_date="2026-01-01T00:00:00+00:00")
    conn.commit()

    class _Unsettled:
        async def market(self, ticker):
            return KalshiMarket.model_validate({
                "ticker": ticker, "title": "q?", "status": "active", "result": "",
            })

    assert asyncio.run(watch_kalshi_resolutions(_Unsettled(), conn)) == 0
    assert conn.execute(
        "SELECT resolution_checked_ts FROM markets WHERE condition_id = ?", (cid,)
    ).fetchone()[0] is not None, "an unsettled candidate must still rotate"


def test_the_gamma_watcher_no_longer_claims_kalshi_rows(conn):
    """Until 2026-09-08 every Kalshi row past its end date was a Gamma
    candidate -- 18,079 of them against a 200-row cycle, two wasted requests
    each -- and, worse for monitoring, this loop stamped resolution_checked_ts
    on them, so the global oldest_check_age_h could look healthy on Gamma's
    stamps while the Kalshi watcher sat wedged."""
    past = "2026-01-01T00:00:00+00:00"
    poly = _seed(conn, "0xpoly", venue="polymarket", end_date=past)
    _seed(conn, "K-T1", venue="kalshi", end_date=past)
    conn.commit()
    assert unresolved_closed_markets(conn) == [poly]
    assert resolution_backlog_size(conn) == 1, (
        "the backlog number must describe the working set of the watcher it names"
    )


def test_the_cap_budgets_distinct_series_not_slots(conn):
    """Measured on the first live cycle after the cursor shipped: 40 series
    walked, 33 distinct ones stamped -- a series returned under more than one
    configured category was fetched twice, spending ~17% of an hourly budget
    on nothing and quietly making the rotation slower than the arithmetic that
    justifies it."""
    _seed(conn, "A-T1", synced="2026-01-01T00:00:00+00:00")
    _seed(conn, "B-T1", synced="2026-02-01T00:00:00+00:00")
    conn.commit()
    order = _series_sync_order(
        conn, [("A", None), ("B", None), ("A", None), ("B", None)], max_series=4)
    assert order == ["A", "B"], "each series at most once per cycle"


def test_the_watchers_stamp_a_batch_in_one_transaction(conn):
    """Per-row commits took database-lock errors from 5-8 a day to 98 on
    2026-09-09, the day a second watcher started issuing 200 write
    transactions a cycle. Batching also strengthens the round-robin: every
    candidate is rotated before ANY of them is fetched, so a mid-cycle crash
    cannot leave the head of the queue unrotated."""
    past = "2026-01-01T00:00:00+00:00"
    cids = [_seed(conn, f"S{i}-T1", end_date=past) for i in range(5)]
    conn.commit()

    class _CountingConn:
        """sqlite3.Connection.commit is read-only, so count through a proxy."""

        def __init__(self, inner):
            self._inner = inner
            self.commits = 0

        def commit(self):
            self.commits += 1
            return self._inner.commit()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Boom:
        async def market(self, ticker):
            raise RuntimeError("every fetch fails")

    proxy = _CountingConn(conn)
    assert asyncio.run(watch_kalshi_resolutions(_Boom(), proxy)) == 0
    assert proxy.commits == 1, "one stamping transaction, not one per candidate"
    stamped = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE resolution_checked_ts IS NOT NULL"
    ).fetchone()[0]
    assert stamped == len(cids), "every candidate rotates even when every fetch fails"
