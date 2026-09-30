"""The out-of-band pmxt scan: which markets it asks about, in what order, and
how it survives an API that hangs.

pmxt itself is never imported here (nor anywhere in src/lab); the script's
pure helpers are exercised directly."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

from lab.store import db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pmxt_router_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("pmxt_router_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_markets_m7_can_forecast_are_looked_up(tmp_path):
    scan = _load()
    conn = db.connect(tmp_path / "lab.db")
    for cid, category, tier, closed, venue, slug in (
            ("0xecon", "economics", "liquid", 0, "polymarket", "econ-slug"),
            ("0xsport", "sports", "liquid", 0, "polymarket", "sport-slug"),
            ("0xthin", "economics", "ignored", 0, "polymarket", "thin-slug"),
            ("0xdone", "politics", "tail", 1, "polymarket", "done-slug"),
            ("0xnoslug", "politics", "tail", 0, "polymarket", None),
            ("kalshi:K", "economics", "liquid", 0, "kalshi", "K")):
        db.upsert_market(conn, {
            "condition_id": cid, "venue": venue, "venue_native_id": cid, "slug": slug,
            "question": "q", "category": category, "description": "d", "end_date_iso": None,
            "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 1 - closed,
            "closed": closed, "liquidity_num": 1.0, "volume_num": 1.0, "tier": tier})
    conn.commit()
    conn.close()
    config = {"universe": {"priority_categories": ["economics", "politics"]},
              "storage": {"db_path": str(tmp_path / "lab.db")}}
    assert scan._usable_polymarket_markets(config) == {"0xecon": "econ-slug"}


def test_never_asked_first_then_oldest_and_paired_markets_skipped():
    """The order is stamped on attempts, not successes: a market pmxt never
    answers for must rotate to the back rather than pin the head."""
    scan = _load()
    usable = {"a": "s", "b": "s", "c": "s", "d": "s"}
    state = {"a": "2026-09-30T05:00:00+00:00", "b": "2026-09-29T05:00:00+00:00"}
    assert scan._lookup_order(usable, paired={"d"}, state=state) == ["c", "b", "a"]


def test_a_hanging_lookup_is_abandoned_not_waited_for():
    """pmxt held a request for 900 s on 2026-09-30 and its Router takes no
    timeout; the scan gives each lookup a bounded wait on its own thread."""
    scan = _load()
    start = time.monotonic()
    finished, result, error = scan._with_timeout(lambda: time.sleep(5), 0.2)
    assert (finished, result, error) == (False, None, None)
    assert time.monotonic() - start < 2
    assert scan._with_timeout(lambda: 42, 1) == (True, 42, None)
    finished, _, error = scan._with_timeout(lambda: 1 / 0, 1)
    assert finished and isinstance(error, ZeroDivisionError)


def test_a_cluster_becomes_a_candidate_with_the_kalshi_outcomes():
    scan = _load()
    poly = SimpleNamespace(source_exchange="polymarket", contract_address="0xabc",
                           title="Will X win?", slug="will-x-win")
    kalshi = SimpleNamespace(source_exchange="kalshi", contract_address=None, slug="KXWIN-26-X",
                             title="Who will win?", description="resolves on the count",
                             outcomes=[SimpleNamespace(label="X"), SimpleNamespace(label="Not X")])
    cand = scan._candidate(SimpleNamespace(confidence=0.95, markets=[poly, kalshi]), "now")
    assert cand["poly_condition_id"] == "0xabc" and cand["kalshi_ticker"] == "KXWIN-26-X"
    assert cand["kalshi_outcomes"] == ["X", "Not X"] and cand["confidence"] == 0.95
    assert scan._candidate(SimpleNamespace(confidence=0.9, markets=[poly]), "now") is None
