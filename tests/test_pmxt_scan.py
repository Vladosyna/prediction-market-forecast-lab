"""The out-of-band pmxt scan: which markets it asks about, in what order, and
how it survives an API that hangs.

pmxt itself is never imported here (nor anywhere in src/lab); the script's
pure helpers are exercised directly."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_saving_appends_to_what_the_candidates_file_holds_now(tmp_path):
    """The verify job consumes the file at 17:00 and 21:00, often mid-scan: a
    save appends to what is there now instead of rewriting a start-of-run copy."""
    scan = _load()
    cands, stamps = tmp_path / "pmxt_candidates.json", tmp_path / "pmxt_lookup_state.json"
    a = {"poly_condition_id": "0xa", "kalshi_ticker": "A"}
    b = {"poly_condition_id": "0xb", "kalshi_ticker": "B"}
    assert scan._save([a], {"0xa": "t1"}, cands, stamps) == 1
    cands.write_text("[]", encoding="utf-8")                      # verified and consumed
    assert scan._save([b], {"0xa": "t1", "0xb": "t2"}, cands, stamps) == 1
    assert scan._save([b], {"0xa": "t1", "0xb": "t2"}, cands, stamps) == 0
    assert json.loads(cands.read_text(encoding="utf-8")) == [b]
    assert json.loads(stamps.read_text(encoding="utf-8")) == {"0xa": "t1", "0xb": "t2"}


def test_every_pmxt_request_carries_a_timeout():
    """pmxt's REST layer hands urllib3 timeout=None -- wait forever -- unless
    the call brings its own, and the Router never does: requests were held
    900 s on 2026-09-30, and half of a random sample hung past 20 s."""
    scan = _load()
    seen = []

    class ApiClient:
        def call_api(self, method, url, header_params=None, body=None, post_params=None,
                     _request_timeout=None):
            seen.append(_request_timeout)

    router = scan._bounded(SimpleNamespace(_api_client=ApiClient()), 15)
    router._api_client.call_api("GET", "https://example.invalid/v0/matched-market-clusters")
    assert seen == [15]


def test_a_pmxt_whose_requests_cannot_be_bounded_is_refused():
    scan = _load()

    class ApiClient:
        def call_api(self, method, url, header_params=None):
            pass

    with pytest.raises(RuntimeError):
        scan._bounded(SimpleNamespace(_api_client=ApiClient()), 15)


def test_lookups_run_a_few_at_a_time_and_every_outcome_is_reported():
    scan = _load()
    active, peak, guard = [0], [0], threading.Lock()

    def lookup(job):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.02)
            if job % 3 == 0:
                raise TimeoutError("read timed out")
            return job * 10
        finally:
            with guard:
                active[0] -= 1

    got = {job: (result, error) for job, result, error in
           scan._run_lookups(range(1, 10), lookup, workers=2, start_interval_s=0.0)}
    assert len(got) == 9 and got[1] == (10, None) and got[8] == (80, None)
    assert got[3][0] is None and isinstance(got[3][1], TimeoutError)
    assert peak[0] <= 2


def test_new_lookups_start_at_most_once_per_interval():
    scan = _load()
    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    list(scan._run_lookups(range(4), lambda job: job, workers=1, start_interval_s=1.0,
                           clock=lambda: now[0], sleep=sleep))
    assert slept == [1.0, 1.0, 1.0]


def test_a_run_stops_when_nearly_every_recent_lookup_fails(monkeypatch):
    scan = _load()
    monkeypatch.setattr(scan, "FAIL_WINDOW", 4)

    def lookup(job):
        raise ConnectionRefusedError("pmxt down")

    seen = list(scan._run_lookups(range(40), lookup, workers=2, start_interval_s=0.0))
    assert 4 <= len(seen) < 40 and all(error is not None for _, _, error in seen)


def test_a_second_scan_does_not_run_beside_the_first(tmp_path):
    pytest.importorskip("fcntl")
    scan = _load()
    first = scan._lock_or_none(tmp_path / "scan.lock")
    assert first is not None and scan._lock_or_none(tmp_path / "scan.lock") is None
    first.close()
    again = scan._lock_or_none(tmp_path / "scan.lock")
    assert again is not None
    again.close()


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
