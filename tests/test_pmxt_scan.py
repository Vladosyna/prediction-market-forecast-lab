"""The out-of-band pmxt scan: paginated crawl and the usable-market filter.

pmxt itself is never imported here (nor anywhere in src/lab); the crawl is
exercised against a stand-in router."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from lab.store import db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pmxt_router_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("pmxt_router_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_crawl_pages_until_a_short_page_and_passes_the_watermark(monkeypatch):
    scan = _load()
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    calls = []

    class Router:
        def fetch_matched_market_clusters(self, **kw):
            calls.append(kw)
            return list(range(scan.PAGE_SIZE)) if kw["offset"] < 2 * scan.PAGE_SIZE else [1, 2]

    crawl = scan._crawl(Router(), "2026-09-30T05:00:00+00:00")
    seen = []
    try:
        while True:
            seen.append(next(crawl))
    except StopIteration as done:
        complete = done.value
    assert complete is True and len(seen) == 2 * scan.PAGE_SIZE + 2
    assert [c["offset"] for c in calls] == [0, scan.PAGE_SIZE, 2 * scan.PAGE_SIZE]
    assert all(c["updated_since"] == "2026-09-30T05:00:00+00:00" and c["min_venues"] == 2
               for c in calls)


def test_only_markets_m7_can_forecast_are_usable(tmp_path):
    scan = _load()
    conn = db.connect(tmp_path / "lab.db")
    for cid, category, tier, closed, venue in (
            ("0xecon", "economics", "liquid", 0, "polymarket"),
            ("0xsport", "sports", "liquid", 0, "polymarket"),
            ("0xthin", "economics", "ignored", 0, "polymarket"),
            ("0xdone", "politics", "tail", 1, "polymarket"),
            ("kalshi:K", "economics", "liquid", 0, "kalshi")):
        db.upsert_market(conn, {
            "condition_id": cid, "venue": venue, "venue_native_id": cid, "slug": None,
            "question": "q", "category": category, "description": "d", "end_date_iso": None,
            "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 1 - closed,
            "closed": closed, "liquidity_num": 1.0, "volume_num": 1.0, "tier": tier})
    conn.commit()
    conn.close()
    config = {"universe": {"priority_categories": ["economics", "politics"]},
              "storage": {"db_path": str(tmp_path / "lab.db")}}
    assert scan._usable_polymarket_ids(config) == {"0xecon"}


def test_a_page_that_fails_is_retried_before_the_crawl_gives_up(monkeypatch):
    """pmxt's API now and then hangs and returns an empty body; the next
    request usually succeeds (2026-09-30: 900 s, then 1.4 s)."""
    scan = _load()
    monkeypatch.setattr(scan.time, "sleep", lambda s: None)
    attempts = []

    class Flaky:
        def fetch_matched_market_clusters(self, **kw):
            attempts.append(kw["offset"])
            if len(attempts) == 1:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return [1, 2, 3]

    crawl = scan._crawl(Flaky(), None)
    got = []
    try:
        while True:
            got.append(next(crawl))
    except StopIteration as done:
        assert done.value is True
    assert got == [1, 2, 3] and attempts == [0, 0]
