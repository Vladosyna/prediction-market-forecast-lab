"""The Kalshi snapshot round collects only markets that can become forecast
targets (PAP 9.26).

Regression for 2026-09-02/03, when the venue fell out of the forecastable
population for two days. Nothing had broken: the NFL/NCAA season listed 4,333
new Kalshi sports markets in ten days, sports reached 73% of the venue's
snapshotted tiers, and the tail round stopped fitting inside guardrail 13's
90-minute freshness bound -- so 4,211 economics/politics markets were skipped
at forecast time on stale prices and Kalshi's forecast count fell from
19,895/day to 7,915. Every one of those sports markets was already excluded
from forecasting by §3's null-control policy, so the entire budget overrun
bought nothing.

Async collector functions are driven via asyncio.run() from plain `def test_*`
(pytest-asyncio is not a project dependency), mirroring the rest of this
suite's collector tests.
"""

from __future__ import annotations

import asyncio

import pytest

from lab.api.kalshi import KalshiMarket
from lab.collect.kalshi_collector import (
    assign_kalshi_tier,
    drop_unsampled_sports,
    snapshot_kalshi,
    tracked_kalshi_markets_by_ids,
)
from lab.forecast import null_control_ids
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config


class FakeKalshiClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def market(self, ticker: str) -> KalshiMarket:
        self.calls.append(ticker)
        return KalshiMarket.model_validate({
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "title": "q?",
            "status": "active",
            "result": "",
            "yes_bid_dollars": "0.4500",
            "yes_ask_dollars": "0.4700",
            "last_price_dollars": "0.4600",
        })


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    return cfg


@pytest.fixture()
def conn(config):
    c = db.connect(config["storage"]["db_path"])
    yield c
    c.close()


def _seed(conn, ticker, category, tier="tail", venue="kalshi"):
    cid = db.venue_condition_id(venue, ticker)
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": ticker,
        "slug": None, "question": "q?", "category": category, "description": "d",
        "end_date_iso": "2026-12-31T00:00:00+00:00",
        "token_id_yes": None, "token_id_no": None, "neg_risk": 0,
        "active": 1, "closed": 0, "liquidity_num": 0.0, "volume_num": 0.0,
        "tier": tier,
    })
    return cid


def _seed_universe(conn, n_sports=200, n_other=5):
    """The production shape: sports vastly outnumbering everything else."""
    for i in range(n_sports):
        _seed(conn, f"KXNFLRACE-26SEP0{i % 9}-T{i}", "sports")
    for i in range(n_other):
        _seed(conn, f"KXCPI-26SEP-T{i}", "economics")
    conn.commit()


def test_the_round_snapshots_the_sample_and_no_other_sports(config, conn):
    """THE regression test: the round must cost what the research population
    costs, not what the venue happens to list."""
    _seed_universe(conn)
    sampled = null_control_ids(conn, config)
    assert sampled, "fixture must produce a non-empty null-control sample"

    kalshi = FakeKalshiClient()
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    asyncio.run(snapshot_kalshi(kalshi, conn, store, config, tier="tail"))

    fetched = {db.venue_condition_id("kalshi", t) for t in kalshi.calls}
    sports = {r["condition_id"] for r in conn.execute(
        "SELECT condition_id FROM markets WHERE category = 'sports'")}
    econ = {r["condition_id"] for r in conn.execute(
        "SELECT condition_id FROM markets WHERE category = 'economics'")}

    assert fetched & sports == sampled, "exactly the sampled sports markets, no more, no fewer"
    assert econ <= fetched, "the research population must not lose coverage"
    assert len(fetched) == len(econ) + len(sampled)


def test_the_config_flag_restores_the_previous_behaviour(config, conn):
    """A population change needs an off switch that is byte-identical to what
    ran before it, or it cannot honestly be called reversible."""
    _seed_universe(conn)
    config["universe"]["null_control"]["snapshot_unsampled"] = True

    kalshi = FakeKalshiClient()
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    asyncio.run(snapshot_kalshi(kalshi, conn, store, config, tier="tail"))

    assert len(kalshi.calls) == 205


def test_the_null_control_pool_is_not_narrowed_by_the_policy(config, conn):
    """The load-bearing config fact, asserted so it cannot be changed silently.

    A market that stops being snapshotted has no measured depth, so tiering
    falls back to venue-reported figures. Kalshi's fallback tail bar is
    volume 0 / open interest 0, which every market clears -- so an unsampled
    sports market stays in `tier IN ('liquid','tail')` and stays drawable by
    `null_control_ids`. Raise that bar and this change would start selecting
    its own control: markets dropped from snapshotting would fall out of the
    pool and could never be sampled again. That is also why the policy is
    wired to Kalshi and not to Polymarket, whose fallback tail bar is real.
    """
    tail = config["venues"]["kalshi"]["tiers"]["tail"]
    assert tail["min_volume"] == 0 and tail.get("min_open_interest", 0) == 0

    m = KalshiMarket.model_validate({
        "ticker": "KXNFLRACE-26SEP01-T1", "title": "q?", "status": "active",
        "result": "", "volume_fp": "0", "open_interest_fp": "0",
    })
    assert assign_kalshi_tier(m, config, depth_usd=None) == ("tail", None)

    _seed_universe(conn)
    before = null_control_ids(conn, config)
    kalshi = FakeKalshiClient()
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    asyncio.run(snapshot_kalshi(kalshi, conn, store, config, tier="tail"))
    assert null_control_ids(conn, config) == before


def test_the_matched_pair_job_is_never_filtered(config, conn):
    """Phase 17 item 3's per-pair capture takes an explicit condition_id list
    and serves the lead-lag hypothesis (PAP H3), which reads the price series
    itself -- not any forecast written against it. A confirmed sports pair
    must keep its high-frequency legs."""
    cid = _seed(conn, "KXNFLGAME-26SEP01-T1", "sports")
    conn.commit()
    assert [r["condition_id"] for r in tracked_kalshi_markets_by_ids(conn, [cid])] == [cid]


def test_polymarket_rows_are_left_alone(config, conn):
    """Scoped to Kalshi deliberately -- see drop_unsampled_sports."""
    poly = _seed(conn, "0xpoly1", "sports", venue="polymarket")
    conn.commit()
    rows = [{"condition_id": poly, "category": "sports", "venue": "polymarket"}]
    kept, dropped = drop_unsampled_sports(conn, config, rows)
    assert dropped == 0 and kept == rows, (
        "the helper filters what it is given; the Kalshi round is the only caller "
        "that gives it markets, and status.py passes it Kalshi rows only"
    )


def test_status_reports_the_working_set_not_just_the_tracked_count(config, conn):
    """`tracked` would otherwise read 16,164 for a round that fetches 4,411 --
    the same "the number on the dashboard is not the number the job works
    through" failure this file's resolution-backlog comment already records."""
    from lab.collect.status import gather_status

    _seed_universe(conn)
    _seed(conn, "0xpolysport", "sports", venue="polymarket")
    conn.commit()
    conn.close()

    c2 = db.connect(config["storage"]["db_path"])
    kalshi_sampled = {cid for cid in null_control_ids(c2, config)
                      if cid.startswith("kalshi:")}
    c2.close()

    tail = gather_status(config)["tiers"]["tail"]
    assert tail["tracked_markets"] == 206
    assert tail["unsampled_sports"] == 200 - len(kalshi_sampled), (
        "Polymarket's sports market must not be counted -- the policy is Kalshi-only"
    )
    assert tail["snapshot_targets"] == 206 - tail["unsampled_sports"]


def test_a_round_with_no_sports_costs_nothing_extra(config, conn):
    """No sampling query when there is nothing to sample against -- the
    liquid round is mostly economics and should not pay for the policy."""
    for i in range(3):
        _seed(conn, f"KXCPI-26SEP-T{i}", "economics", tier="liquid")
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        "SELECT condition_id, category, venue FROM markets")]
    kept, dropped = drop_unsampled_sports(conn, config, rows)
    assert dropped == 0 and kept is rows
