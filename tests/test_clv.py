"""CLV drift signal: hand-computed values and Phase 17 item 5's gap-aware
exclusion (a drift window overlapping a recorded collection gap is dropped
and counted, not silently treated as ordinary missing data).
"""

from __future__ import annotations

import math

from datetime import datetime, timedelta, timezone

import pytest

import polars as pl

from lab.eval.clv import (
    CLV_SNAPSHOT_COLUMNS,
    build_mid_index,
    _mid_at,
    clv_dates,
    clv_drift,
    clv_validity_check,
    update_clv_trust_flag,
)
from lab.store import db
from lab.store.db import get_meta
from lab.store.snapshots import SnapshotStore
from lab.util import load_config


def _seed_mid(store, ts: datetime, condition_id: str, mid: float) -> None:
    store.append([{
        "ts": ts.isoformat(timespec="seconds"), "condition_id": condition_id,
        "token_id_yes": "tok", "best_bid": mid - 0.01, "best_ask": mid + 0.01,
        "mid": mid, "spread": 0.02,
    }])


def test_clv_drift_hand_computed(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    _seed_mid(store, ts + timedelta(hours=24), "0x1", 0.65)
    forecasts = [{"ts": ts.isoformat(timespec="seconds"), "condition_id": "0x1",
                 "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5}]
    out = clv_drift(forecasts, store, [24])
    # disagreement=0.2 -> sign=+1; drift = +1 * (0.65 - 0.5) = 0.15.
    assert out[24]["n"] == 1
    assert out[24]["mean_signed_drift"] == pytest.approx(0.15)
    assert out[24]["dropped_for_gap"] == 0


def test_clv_drift_excludes_and_counts_windows_overlapping_a_gap(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    ts_a = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
    # Both forecasts have a real snapshot near their target time -- proves
    # forecast A is excluded because of the gap, not for lack of data.
    _seed_mid(store, ts_a + timedelta(hours=24), "0x1", 0.65)
    _seed_mid(store, ts_b + timedelta(hours=24), "0x2", 0.65)
    forecasts = [
        {"ts": ts_a.isoformat(timespec="seconds"), "condition_id": "0x1",
         "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5},
        {"ts": ts_b.isoformat(timespec="seconds"), "condition_id": "0x2",
         "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5},
    ]
    # A synthetic outage overlapping only forecast A's [ts, ts+24h] window.
    gaps = [(ts_a + timedelta(hours=10), ts_a + timedelta(hours=11))]

    out = clv_drift(forecasts, store, [24], gap_windows=gaps)
    assert out[24]["n"] == 1                 # only forecast B's drift counted
    assert out[24]["mean_signed_drift"] == pytest.approx(0.15)
    assert out[24]["dropped_for_gap"] == 1    # forecast A excluded, not silently dropped

    # Without the gap, both would have contributed (proves the exclusion is
    # specifically the gap check, not some other filter).
    out_no_gap = clv_drift(forecasts, store, [24])
    assert out_no_gap[24]["n"] == 2
    assert out_no_gap[24]["dropped_for_gap"] == 0


# --- _mid_at / build_mid_index (Phase 20 perf rewrite) --------------------
# _mid_at used to `df.filter(condition_id ==)` per call; it now looks up a
# pre-built per-market sorted-ts index via bisect. These tests pin the exact
# tolerance-window and closest-match semantics the docstring/investigation
# claims are preserved, plus a differential check against a naive reference.

def _mid_frame(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    """rows: (ts_iso, condition_id, mid)."""
    return pl.DataFrame([{"ts": r[0], "condition_id": r[1], "mid": r[2]} for r in rows])


def test_mid_at_exact_match():
    idx = build_mid_index(_mid_frame([("2026-07-01T12:00:00+00:00", "a", 0.4)]))
    target = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert _mid_at(idx, "a", target) == 0.4


def test_mid_at_picks_closer_of_two_candidates():
    idx = build_mid_index(_mid_frame([
        ("2026-07-01T10:00:00+00:00", "a", 0.3),   # 2h before target
        ("2026-07-01T12:30:00+00:00", "a", 0.7),   # 30min after target -> closer
    ]))
    target = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert _mid_at(idx, "a", target) == 0.7


def test_mid_at_exact_tie_prefers_earlier_snapshot():
    """Documented, deterministic resolution of a case the prior polars-sort
    implementation left unstable/undefined (see clv.py module note)."""
    idx = build_mid_index(_mid_frame([
        ("2026-07-01T09:00:00+00:00", "a", 0.1),   # 3h before target
        ("2026-07-01T15:00:00+00:00", "a", 0.9),   # 3h after target -- exact tie
    ]))
    target = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert _mid_at(idx, "a", target, tolerance_hours=3.0) == 0.1


def test_mid_at_outside_tolerance_returns_none():
    idx = build_mid_index(_mid_frame([("2026-07-01T00:00:00+00:00", "a", 0.5)]))
    target = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert _mid_at(idx, "a", target, tolerance_hours=3.0) is None


def test_mid_at_unknown_market_returns_none():
    idx = build_mid_index(_mid_frame([("2026-07-01T12:00:00+00:00", "a", 0.5)]))
    assert _mid_at(idx, "does-not-exist", datetime(2026, 7, 1, 12, tzinfo=timezone.utc)) is None


def test_mid_at_matches_naive_reference_on_random_data():
    """Differential check against the deliberately naive, obviously-correct
    filter+sort approach the prior implementation used (per-call
    df.filter(condition_id ==) then sort by distance), across many random
    (snapshots, target) combinations. Exact ties are excluded here (their
    resolution is intentionally different, per the dedicated tie test above)."""
    import random

    rng = random.Random(20260720)
    markets = ["a", "b", "c"]
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for m in markets:
        for i in range(40):
            ts = base + timedelta(minutes=15 * i + rng.randint(0, 14))
            rows.append((ts.isoformat(timespec="seconds"), m, round(rng.random(), 4)))
    df = _mid_frame(rows)
    idx = build_mid_index(df)

    def naive_mid_at(condition_id, target_ts, tolerance_hours=3.0):
        subset = df.filter(pl.col("condition_id") == condition_id)
        lo = (target_ts - timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
        hi = (target_ts + timedelta(hours=tolerance_hours)).isoformat(timespec="seconds")
        window = subset.filter((pl.col("ts") >= lo) & (pl.col("ts") <= hi))
        if window.is_empty():
            return None
        best_dist, best_mid = None, None
        for ts_s, mid in zip(window["ts"].to_list(), window["mid"].to_list()):
            ts_dt = datetime.fromisoformat(ts_s)
            dist = abs((ts_dt - target_ts).total_seconds())
            if best_dist is None or dist < best_dist:  # strict '<' -> earlier wins ties too
                best_dist, best_mid = dist, mid
        return best_mid

    for trial in range(50):
        m = rng.choice(markets)
        target = base + timedelta(minutes=rng.randint(-60, 60 * 11))
        got = _mid_at(idx, m, target)
        want = naive_mid_at(m, target)
        assert got == want, f"trial {trial}: market={m} target={target}"


def test_clv_drift_shared_snapshots_matches_internal_read(tmp_path):
    """Passing a pre-loaded `snapshots=` frame (the report render's read-once
    optimization) yields byte-identical results to letting clv_drift read the
    store itself -- and `clv_dates` covers exactly the partitions it needs."""
    store = SnapshotStore(tmp_path / "snapshots")
    ts = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    _seed_mid(store, ts + timedelta(hours=24), "0x1", 0.65)
    forecasts = [{"ts": ts.isoformat(timespec="seconds"), "condition_id": "0x1",
                 "model_id": "m1", "p_yes": 0.7, "p_market_at_ts": 0.5}]

    # Read once for the union of dates clv_dates reports, then share it.
    shared = store.read_range(sorted(clv_dates(forecasts, [24])), columns=CLV_SNAPSHOT_COLUMNS)
    out_shared = clv_drift(forecasts, store, [24], snapshots=shared)
    out_internal = clv_drift(forecasts, store, [24])
    assert out_shared == out_internal
    assert out_shared[24]["mean_signed_drift"] == pytest.approx(0.15)


# --- clv_validity_check / update_clv_trust_flag (Phase 17 item 4) ---------

def _config(tmp_path, **eval_overrides):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    cfg["eval"] = {**cfg["eval"], "clv_null_control_min_n": 2, **eval_overrides}
    return cfg


def _seed_null_control_case(conn, store, cid, ts, p_yes, p_market, payout_yes, later_mid, horizon):
    conn.execute(
        """INSERT OR IGNORE INTO markets (condition_id, question, category, venue, tier, active, closed)
           VALUES (?, ?, 'sports', 'polymarket', 'liquid', 1, 1)""",
        (cid, f"Q {cid}?"),
    )
    ts_iso = ts.isoformat(timespec="seconds")
    db.append_forecast(conn, {"ts": ts_iso, "condition_id": cid, "model_id": "m0_market",
                             "p_yes": p_yes, "p_market_at_ts": p_market})
    db.record_resolution(conn, cid, ts_iso, payout_yes, False, "gamma")
    store.append([{
        "ts": (ts + timedelta(hours=horizon)).isoformat(timespec="seconds"),
        "condition_id": cid, "mid": later_mid,
    }])
    conn.commit()


# Shared across both fixtures below: y=1, p_market_at_ts=0.5 for every case,
# only p_yes varies -> skill = 0.25 - (p_yes-1)**2, hand-computed:
#   p_yes=0.8 -> skill= 0.21 (disagreement +0.3, sign +1)
#   p_yes=0.6 -> skill= 0.09 (disagreement +0.1, sign +1)
#   p_yes=0.1 -> skill=-0.56 (disagreement -0.4, sign -1)
#   p_yes=0.4 -> skill=-0.11 (disagreement -0.1, sign -1)
_CASES = [("0x1", 0.8, 0.21, 1), ("0x2", 0.6, 0.09, 1), ("0x3", 0.1, -0.56, -1), ("0x4", 0.4, -0.11, -1)]


def test_clv_validity_check_stays_trusted_when_drift_has_no_variance(tmp_path):
    """Uncorrelated (here: literally constant) drift across varying realized
    skill -- there is nothing for a correlation to detect, so the check must
    not raise a false alarm."""
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, _skill, sign in _CASES:
        later_mid = 0.5 + 0.1 * sign  # constant drift = +0.1 regardless of sign/skill
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = clv_validity_check(conn, config, store)
    assert result["n"] == 4
    assert result["trusted"] is True
    assert result["reason"] == "zero_variance"
    conn.close()


def test_clv_validity_check_flags_untrusted_when_drift_tracks_skill(tmp_path):
    """Drift set to an exact positive multiple of realized skill (perfect
    correlation) on the null control -- must be flagged untrusted."""
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, skill, sign in _CASES:
        drift = 0.3 * skill
        later_mid = 0.5 + sign * drift  # drift = sign*(later_mid-0.5) = drift by construction
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = clv_validity_check(conn, config, store)
    assert result["n"] == 4
    assert result["correlation"] == pytest.approx(1.0)
    assert result["trusted"] is False
    conn.close()


def test_update_clv_trust_flag_persists_and_abstention_does_not_clear_it(tmp_path):
    config = _config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    horizon = config["eval"]["clv_horizons_hours"][0]

    for cid, p_yes, skill, sign in _CASES:
        drift = 0.3 * skill
        later_mid = 0.5 + sign * drift
        _seed_null_control_case(conn, store, cid, ts, p_yes, 0.5, 1.0, later_mid, horizon)

    result = update_clv_trust_flag(conn, config, store)
    assert result["trusted"] is False
    assert get_meta(conn, "clv_trusted") == "0"

    # A later run with insufficient data (an abstention) must not silently
    # clear the flag -- it re-verified nothing.
    empty_config = _config(tmp_path, clv_null_control_min_n=1000)
    abstained = update_clv_trust_flag(conn, empty_config, store)
    assert "correlation" not in abstained
    assert get_meta(conn, "clv_trusted") == "0"
    conn.close()


# --- index reuse across models (2026-07-28 render blow-up) -----------------

def test_clv_drift_uses_a_supplied_index_without_rebuilding(monkeypatch):
    """Sharing the snapshot FRAME across models was not enough: every
    clv_drift call still rebuilt the index from it -- a full sort + group_by +
    per-market list materialisation. On the production host that measured as
    10.5 minutes of a 12-minute render and drove its 290MB-815MB memory
    sawtooth, because each rebuild allocated and freed the same structure."""
    import lab.eval.clv as clv_mod

    df = _mid_frame([
        ("2026-07-01T12:00:00+00:00", "a", 0.4),
        ("2026-07-02T12:00:00+00:00", "a", 0.6),
    ])
    index = clv_mod.build_mid_index(df)

    def _boom(_df):
        raise AssertionError("index rebuilt despite one being supplied")

    monkeypatch.setattr(clv_mod, "build_mid_index", _boom)

    forecasts = [{"ts": "2026-07-01T12:00:00+00:00", "condition_id": "a",
                  "model_id": "m0_market", "p_yes": 0.7, "p_market_at_ts": 0.4}]
    out = clv_mod.clv_drift(forecasts, None, [24], mid_index=index)
    assert out[24]["n"] == 1


def test_clv_drift_still_builds_its_own_index_when_none_given():
    """The standalone path (a single caller, no shared frame) must keep
    working -- the parameter is an optimisation, not a new requirement."""
    df = _mid_frame([
        ("2026-07-01T12:00:00+00:00", "a", 0.4),
        ("2026-07-02T12:00:00+00:00", "a", 0.6),
    ])
    forecasts = [{"ts": "2026-07-01T12:00:00+00:00", "condition_id": "a",
                  "model_id": "m0_market", "p_yes": 0.7, "p_market_at_ts": 0.4}]
    out = clv_drift(forecasts, None, [24], snapshots=df)
    assert out[24]["n"] == 1


def test_supplied_index_and_frame_give_identical_results():
    """The optimisation must not change a single reported number."""
    df = _mid_frame([
        ("2026-07-01T12:00:00+00:00", "a", 0.40),
        ("2026-07-02T12:00:00+00:00", "a", 0.62),
        ("2026-07-01T12:00:00+00:00", "b", 0.30),
        ("2026-07-02T12:00:00+00:00", "b", 0.25),
    ])
    forecasts = [
        {"ts": "2026-07-01T12:00:00+00:00", "condition_id": "a",
         "model_id": "m0_market", "p_yes": 0.7, "p_market_at_ts": 0.40},
        {"ts": "2026-07-01T12:00:00+00:00", "condition_id": "b",
         "model_id": "m0_market", "p_yes": 0.1, "p_market_at_ts": 0.30},
    ]
    via_frame = clv_drift(forecasts, None, [24, 72], snapshots=df)
    via_index = clv_drift(forecasts, None, [24, 72], mid_index=build_mid_index(df))

    assert via_frame.keys() == via_index.keys()
    for h in via_frame:
        a, b = via_frame[h], via_index[h]
        assert a.keys() == b.keys()
        for k in a:
            # An empty horizon reports mean drift as NaN, which never compares
            # equal to itself -- match on NaN-ness there rather than value.
            if isinstance(a[k], float) and math.isnan(a[k]):
                assert math.isnan(b[k]), f"{h}/{k}: NaN vs {b[k]}"
            else:
                assert a[k] == b[k], f"{h}/{k}: {a[k]} vs {b[k]}"


# --- the index must stay numeric (2026-08-06 report OOM) --------------------

def test_mid_index_holds_numeric_arrays_not_python_objects():
    """Measured on the live host: 444MB after the snapshot read, 1227MB after
    building this index -- ~100 bytes per row for a Python str + float where 16
    will do. The report render runs inside the collector's cgroup, so that peak
    is the collector's too."""
    import numpy as np
    import polars as pl

    from lab.eval.clv import build_mid_index

    df = pl.DataFrame([
        {"ts": "2026-03-01T00:00:00+00:00", "condition_id": "0xA", "mid": 0.4},
        {"ts": "2026-03-01T00:05:00+00:00", "condition_id": "0xA", "mid": 0.5},
        {"ts": "2026-03-01T00:00:00+00:00", "condition_id": "0xB", "mid": 0.6},
    ])
    index = build_mid_index(df)

    assert set(index) == {"0xA", "0xB"}
    for epochs, mids in index.values():
        assert isinstance(epochs, np.ndarray) and epochs.dtype == np.int64
        assert isinstance(mids, np.ndarray) and mids.dtype == np.float64
    # sorted, and the epochs really are the instants
    epochs, mids = index["0xA"]
    assert list(epochs) == sorted(epochs)
    assert epochs[1] - epochs[0] == 300
    assert list(mids) == [0.4, 0.5]


def test_mid_index_never_imputes_a_null_mid():
    """A venue without an order book (a hidden Metaculus CP) stores a null mid.
    It arrives as NaN in a float64 array, and guardrail 16 forbids imputing it."""
    import polars as pl

    from lab.eval.clv import _mid_at, build_mid_index

    df = pl.DataFrame([{"ts": "2026-03-01T00:00:00+00:00", "condition_id": "0xN", "mid": None}])
    index = build_mid_index(df)
    target = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert _mid_at(index, "0xN", target, tolerance_hours=3.0) is None


def test_chunked_clv_equals_a_single_pass_exactly(tmp_path):
    """2026-09-25: the report's single all-markets CLV read OOM-killed every
    render from 09-20. Chunking by market must change memory, not numbers --
    a forecast's drift reads only its own market's snapshots, and a mean
    recombines exactly from (sum, n)."""
    from lab.eval.clv import chunked_clv_rows

    store = SnapshotStore(tmp_path / "snapshots")
    t0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    forecasts_by_model = {"m1": [], "m2": []}
    for i in range(7):
        cid = f"0x{i}"
        _seed_mid(store, t0 + timedelta(hours=24), cid, 0.40 + 0.05 * i)
        _seed_mid(store, t0 + timedelta(hours=72), cid, 0.35 + 0.04 * i)
        for mid, p in (("m1", 0.62 - 0.03 * i), ("m2", 0.38 + 0.02 * i)):
            forecasts_by_model[mid].append({
                "ts": t0.isoformat(timespec="seconds"), "condition_id": cid,
                "model_id": mid, "p_yes": p, "p_market_at_ts": 0.5})

    dates = sorted(clv_dates([f for fs in forecasts_by_model.values() for f in fs], [24, 72]))
    one_pass, d1 = chunked_clv_rows(forecasts_by_model, ["m1", "m2"], store, dates, [24, 72],
                                    markets_per_chunk=10_000)
    tiny, d2 = chunked_clv_rows(forecasts_by_model, ["m1", "m2"], store, dates, [24, 72],
                                markets_per_chunk=2)
    assert [(r["model_id"], r["horizon"], r["n"]) for r in one_pass] == \
           [(r["model_id"], r["horizon"], r["n"]) for r in tiny]
    for a, b in zip(one_pass, tiny):
        assert a["drift"] == pytest.approx(b["drift"], abs=1e-12)
    assert d1 == d2

    # and both agree with the pre-chunking per-model computation
    for mid in ("m1", "m2"):
        direct = clv_drift(forecasts_by_model[mid], store, [24, 72])
        for r in (x for x in one_pass if x["model_id"] == mid):
            assert r["n"] == direct[r["horizon"]]["n"]
            assert r["drift"] == pytest.approx(direct[r["horizon"]]["mean_signed_drift"], abs=1e-12)
