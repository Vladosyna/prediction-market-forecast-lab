"""Phase 17 item 3: matched-event high-frequency capture.

Async collector functions are driven via asyncio.run() from plain `def
test_*` (pytest-asyncio is not a project dependency), mirroring the rest of
this test suite's collector tests.
"""

from __future__ import annotations

import asyncio

import pytest

from lab.api.clob import BookLevel, OrderBook
from lab.api.kalshi import KalshiMarket
from lab.collect.runner import snapshot_matched_pairs
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config, now_utc


class FakeClobClient:
    def __init__(self, books: dict[str, OrderBook]):
        self._books = books
        self.calls: list[str] = []

    async def book(self, token_id: str) -> OrderBook:
        self.calls.append(token_id)
        return self._books[token_id]


class FakeKalshiClient:
    def __init__(self, markets: dict[str, KalshiMarket]):
        self._markets = markets
        self.calls: list[str] = []

    async def market(self, ticker: str) -> KalshiMarket | None:
        self.calls.append(ticker)
        return self._markets.get(ticker)


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


def _seed_poly_market(conn, cid, token_id):
    conn.execute(
        """INSERT INTO markets (condition_id, question, category, tier, active, closed, token_id_yes)
           VALUES (?, ?, 'politics', 'liquid', 1, 0, ?)""",
        (cid, f"Q {cid}?", token_id),
    )


def _seed_kalshi_market(conn, cid, ticker):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": "kalshi", "venue_native_id": ticker,
        "slug": None, "question": "q?", "category": "economics", "description": "d",
        "end_date_iso": "2026-12-31T00:00:00+00:00", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "liquid",
    })


def test_snapshot_matched_pairs_writes_poly_leg_for_all_and_kalshi_leg_only_for_kalshi(config):
    """2 confirmed pairs (one Kalshi, one Metaculus): the Polymarket leg is
    snapshotted for BOTH, the Kalshi leg only for the Kalshi one -- Metaculus
    has no per-market order-book fetch to reuse here."""
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])

    kalshi_cid = db.venue_condition_id("kalshi", "TICKER-1")
    _seed_poly_market(conn, "0xk", "tok-k")
    _seed_poly_market(conn, "0xm", "tok-m")
    _seed_kalshi_market(conn, kalshi_cid, "TICKER-1")
    conn.commit()

    markets_map = {"confirmed": [
        {"condition_id": "0xk", "venue": "kalshi", "external_id": "TICKER-1"},
        {"condition_id": "0xm", "venue": "metaculus", "external_id": "12345"},
    ]}
    clob = FakeClobClient({
        "tok-k": OrderBook(bids=[BookLevel(price=0.49, size=100)], asks=[BookLevel(price=0.51, size=100)]),
        "tok-m": OrderBook(bids=[BookLevel(price=0.39, size=100)], asks=[BookLevel(price=0.41, size=100)]),
    })
    kalshi = FakeKalshiClient({
        "TICKER-1": KalshiMarket(ticker="TICKER-1", yes_bid_dollars=0.50, yes_ask_dollars=0.55),
    })

    counts = asyncio.run(snapshot_matched_pairs(clob, kalshi, conn, store, config, markets_map))
    assert counts == {"poly_written": 2, "kalshi_written": 1}
    assert set(clob.calls) == {"tok-k", "tok-m"}
    assert kalshi.calls == ["TICKER-1"]

    df = store.read_range([now_utc().strftime("%Y-%m-%d")])
    assert set(df["condition_id"].to_list()) == {"0xk", "0xm", kalshi_cid}
    conn.close()


def test_snapshot_matched_pairs_is_safe_noop_with_no_confirmed_pairs(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    clob = FakeClobClient({})
    kalshi = FakeKalshiClient({})

    counts = asyncio.run(snapshot_matched_pairs(clob, kalshi, conn, store, config, {"confirmed": []}))
    assert counts == {"poly_written": 0, "kalshi_written": 0}
    assert clob.calls == [] and kalshi.calls == []
    conn.close()


def test_the_pair_legs_are_fetched_concurrently(config):
    """Awaited one book at a time, ~140 legs took up to 161 s against a
    2-minute interval and a third of the firings were skipped (2026-09-30).
    The legs must overlap, within the configured bound."""
    import asyncio as aio

    from lab.store.snapshots import SnapshotStore

    in_flight = {"now": 0, "peak": 0}

    class SlowClob(FakeClobClient):
        async def book(self, token_id):
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await aio.sleep(0.01)
            in_flight["now"] -= 1
            return await super().book(token_id)

    conn = db.connect(config["storage"]["db_path"])
    books, confirmed = {}, []
    for i in range(20):
        _seed_poly_market(conn, f"0xp{i}", f"tok{i}")
        books[f"tok{i}"] = OrderBook(bids=[{"price": 0.4, "size": 10}],
                                     asks=[{"price": 0.6, "size": 10}])
        confirmed.append({"condition_id": f"0xp{i}", "venue": "metaculus", "external_id": str(i)})
    conn.commit()
    config["collect"]["snapshot_concurrency"] = 4
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    counts = aio.run(snapshot_matched_pairs(SlowClob(books), FakeKalshiClient({}), conn, store,
                                            config, {"confirmed": confirmed}))
    assert counts["poly_written"] == 20
    assert 1 < in_flight["peak"] <= 4
    conn.close()
