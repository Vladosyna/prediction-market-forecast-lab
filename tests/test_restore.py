"""Restore drill: the results mirror must rebuild the research record exactly."""

from __future__ import annotations

import gzip
from datetime import timedelta

import pytest

from lab.ledger_commitment import commit_pending_days, verify_ledger
from lab.publish import sync_ledger_increment, sync_reference_tables
from lab.restore import restore_from_mirror
from lab.store import db
from lab.util import now_utc


def _market(conn, cid, venue="polymarket", event_id=None):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid, "slug": None,
        "question": f"Will {cid} happen?", "category": "politics", "description": "d",
        "end_date_iso": "2026-08-01T00:00:00+00:00", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid", "event_id": event_id,
    })


@pytest.fixture()
def source(tmp_path):
    """A small live database with two closed days of ledger."""
    conn = db.connect(tmp_path / "live.db")
    day1 = (now_utc() - timedelta(days=2)).replace(hour=2, minute=0, second=0, microsecond=0)
    day2 = day1 + timedelta(days=1)
    _market(conn, "0xa")
    _market(conn, "0xb")
    _market(conn, "0xnever_forecast")
    cur = conn.execute(
        "INSERT INTO evidence_runs (ts, condition_id, dossier_json, llm_model, tokens_in, "
        "tokens_out, cost_usd) VALUES (?, '0xa', '{\"articles\": []}', 'fake', 1, 1, 0.001)",
        (day1.isoformat(timespec="seconds"),))
    evidence_id = cur.lastrowid
    for day in (day1, day2):
        ts = day.isoformat(timespec="seconds")
        db.append_forecast(conn, {"ts": ts, "condition_id": "0xa", "model_id": "m0_market",
                                  "p_yes": 0.61, "p_market_at_ts": 0.61, "spread_at_ts": 0.02})
        db.append_forecast(conn, {"ts": ts, "condition_id": "0xb", "model_id": "m3_evidence",
                                  "p_yes": 0.3333333333333333, "p_market_at_ts": 0.35,
                                  "evidence_run_id": evidence_id})
    db.record_resolution(conn, "0xa", day2.isoformat(timespec="seconds"), 1.0, False, "gamma")
    db.record_resolution(conn, "0xnever_forecast", day2.isoformat(timespec="seconds"),
                         0.0, False, "gamma")
    conn.commit()
    yield conn, tmp_path
    conn.close()


def _mirror(conn, tmp_path):
    results = tmp_path / "results"
    sync_ledger_increment(results, conn)
    sync_reference_tables(results, conn)
    return results


def test_the_mirror_rebuilds_the_ledger_with_its_ids(source):
    conn, tmp_path = source
    results = _mirror(conn, tmp_path)

    report = restore_from_mirror(results, tmp_path / "restored.db")
    assert report["digest_mismatch"] == [] and report["row_count_mismatch"] == []
    assert report["rows"]["forecasts"] == 4
    assert report["orphan_resolutions"] == 1, "a resolution nothing forecast has no market row"

    restored = db.connect(tmp_path / "restored.db")
    cols = "id, ts, condition_id, model_id, p_yes, p_market_at_ts, spread_at_ts, evidence_run_id"
    live_rows = [tuple(r) for r in conn.execute(f"SELECT {cols} FROM forecasts ORDER BY id")]
    back_rows = [tuple(r) for r in restored.execute(f"SELECT {cols} FROM forecasts ORDER BY id")]
    assert back_rows == live_rows, "every forecast, with its id and exact floats"
    assert restored.execute("SELECT COUNT(*) FROM evidence_runs").fetchone()[0] == 1
    restored.close()


def test_the_public_commitments_verify_against_the_restored_rows(source):
    """The paper's verifiability claim has to survive the loss of the host:
    a commitment computed on the live database must verify on a database
    rebuilt from the mirror alone."""
    conn, tmp_path = source
    ledger = tmp_path / "ledger_commitments.jsonl"
    assert commit_pending_days(conn, ledger), "fixture must produce a commitment"
    results = _mirror(conn, tmp_path)

    restore_from_mirror(results, tmp_path / "restored.db")
    restored = db.connect(tmp_path / "restored.db")
    result = verify_ledger(restored, ledger)
    restored.close()
    assert result["ok"] and result["dates_verified"] == result["dates"] >= 1


def test_a_tampered_day_is_reported_and_an_existing_db_is_never_overwritten(source):
    conn, tmp_path = source
    results = _mirror(conn, tmp_path)
    day_file = sorted((results / "ledger" / "forecasts").glob("*.jsonl.gz"))[0]
    rows = gzip.decompress(day_file.read_bytes()).decode().splitlines()
    day_file.write_bytes(gzip.compress(("\n".join(rows[:-1]) + "\n").encode()))

    report = restore_from_mirror(results, tmp_path / "restored.db")
    assert [m["date"] for m in report["digest_mismatch"]] == [day_file.name[:10]]
    assert report["row_count_mismatch"][0]["file"] == len(rows) - 1

    with pytest.raises(FileExistsError):
        restore_from_mirror(results, tmp_path / "restored.db")
