"""Phase 7.1 learning-loop safety (brief section 6).

Covers the four acceptance fixtures:
  (a) a refit call missing a validation window raises;
  (b) a large parameter jump is clamped to max_step_pct;
  (c) a challenger with a better point estimate but a CI including zero is NOT promoted;
  (d) a promoted challenger that degrades triggers automatic rollback (retired_reason='rollback').
Plus registry invariants (single active, ACTIVE.json generation, rollback restore)
and the forward-only registered_ts scoring guard.
"""

from __future__ import annotations

import json
from datetime import timedelta

import numpy as np
import pytest

from lab.eval.run import resolved_forecast_rows
from lab.learn import registry
from lab.learn.loop import fit_m1_walk_forward, passes_ci_gate, run_rollback_checks
from lab.learn.refit import (
    WalkForwardError,
    assert_walk_forward,
    bound_step,
    save_artifact,
)
from lab.store import db
from lab.util import load_config, now_utc


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
    cfg.setdefault("learn", {})
    cfg["learn"].update(max_step_pct=0.20, rollback_window=50, promotion_min_n=200)
    cfg.setdefault("eval", {})["bootstrap_iterations"] = 500
    return cfg


# --- (a) walk-forward is structural ---------------------------------------

def test_assert_walk_forward_requires_both_windows():
    with pytest.raises(WalkForwardError):
        assert_walk_forward([], [{"x": 1}])
    with pytest.raises(WalkForwardError):
        assert_walk_forward([{"x": 1}], [])
    # A valid split does not raise.
    assert_walk_forward([{"x": 1}], [{"y": 2}])


def test_refit_without_validation_raises():
    train = [{"p_market": 0.6, "outcome": 1.0, "days_to_resolution": 3.0}] * 200
    with pytest.raises(WalkForwardError):
        fit_m1_walk_forward(train, [])          # missing validation window
    with pytest.raises(WalkForwardError):
        fit_m1_walk_forward([], train)          # missing training window


# --- (b) bounded step ------------------------------------------------------

def test_bound_step_clamps_large_jump():
    old = {"k": 0.15, "tau_days": 5.0, "max_shift": 0.8}
    new = {"k": 0.5, "tau_days": 50.0, "max_shift": 0.1}   # all far outside +/-20%
    bounded = bound_step(old, new, 0.20)
    assert bounded["k"] == pytest.approx(0.18)              # 0.15 * 1.2
    assert bounded["tau_days"] == pytest.approx(6.0)        # 5.0 * 1.2
    assert bounded["max_shift"] == pytest.approx(0.64)      # 0.8 * 0.8 (floor)


def test_bound_step_nested_and_passthrough():
    old = {"buckets": {"lt7d": {"alpha": 0.0, "beta": 1.0}}}
    new = {"buckets": {"lt7d": {"alpha": 0.5, "beta": 5.0}}}
    bounded = bound_step(old, new, 0.20)
    assert bounded["buckets"]["lt7d"]["beta"] == pytest.approx(1.2)  # clamped
    assert bounded["buckets"]["lt7d"]["alpha"] == pytest.approx(0.5)  # old 0 -> relative undefined
    # A brand-new key has no incumbent to step from: passes through untouched.
    assert bound_step({"a": 1.0}, {"a": 1.0, "b": 9.0}, 0.2)["b"] == 9.0


# --- (c) CI-gated promotion ------------------------------------------------

def test_ci_gate_rejects_better_point_estimate_with_ci_over_zero():
    n = 250
    cids = np.arange(n)
    champ = np.full(n, 0.25)
    # Deterministic: large symmetric spread (+/-0.5) plus a tiny positive bias.
    # Mean diff is +0.01 (challenger better on the point estimate) but the spread
    # is huge, so the cluster bootstrap CI comfortably straddles zero.
    diffs = np.tile([0.5, -0.5], n // 2) + 0.01
    chall = champ - diffs
    promoted, stats = passes_ci_gate(champ, chall, cids, iterations=500, min_n=200)
    assert stats["skill"] > 0            # better point estimate...
    assert stats["ci_lo"] < 0            # ...but the CI includes zero
    assert promoted is False


def test_ci_gate_accepts_consistent_improvement():
    """Phase 11: the gate is now the anytime-valid confidence sequence, not
    the bootstrap CI (which stays purely descriptive, see passes_ci_gate's
    docstring). The CS's normal-mixture boundary is calibrated for a target
    horizon and, unlike the fixed-n bootstrap, doesn't collapse arbitrarily
    fast just because a synthetic signal happens to be very low-noise -- n=250
    (enough for the old bootstrap-only gate) no longer suffices here; n=600
    does, with a comfortable margin, while the effect and noise level are
    otherwise identical to before."""
    rng = np.random.default_rng(2)
    n = 600
    cids = np.arange(n)
    champ = np.full(n, 0.25)
    chall = champ - rng.normal(0.05, 0.01, n)   # consistent, tight improvement
    promoted, stats = passes_ci_gate(champ, chall, cids, iterations=500, min_n=200)
    assert stats["ci_lo"] > 0     # descriptive bootstrap CI also excludes zero
    assert stats["cs_lo"] > 0     # the actual gate: the CS excludes zero
    assert promoted is True


def test_ci_gate_demonstrably_follows_the_cs_not_the_bootstrap_ci():
    """Phase 11 acceptance criterion: "promotion/rollback code paths
    demonstrably consult the CS." At n=250 with this low-noise signal, the
    fixed-n bootstrap CI already excludes zero, but the anytime-valid CS
    (more conservative at this sample size) does not -- a real disagreement
    between the two estimators. The gate must follow the CS and refuse
    promotion, proving it is no longer a bootstrap-CI decision wearing the
    same function name."""
    rng = np.random.default_rng(2)
    n = 250
    cids = np.arange(n)
    champ = np.full(n, 0.25)
    chall = champ - rng.normal(0.05, 0.01, n)
    promoted, stats = passes_ci_gate(champ, chall, cids, iterations=500, min_n=200)
    assert stats["ci_lo"] > 0      # bootstrap CI says "promote"...
    assert stats["cs_lo"] <= 0     # ...but the CS disagrees...
    assert promoted is False       # ...and the CS wins.


def test_ci_gate_blocks_below_min_n():
    cids = np.arange(50)
    champ = np.full(50, 0.25)
    chall = np.full(50, 0.10)
    promoted, stats = passes_ci_gate(champ, chall, cids, iterations=500, min_n=200)
    assert promoted is False
    assert stats["reason"] == "insufficient_n"


# --- registry invariants ---------------------------------------------------

def _register(conn, config, key, artifact):
    path = save_artifact(config, key, artifact, promote=False)
    return registry.register_version(conn, config, key, path)


def test_registry_single_active_and_active_json(config):
    conn = db.connect(config["storage"]["db_path"])
    v1 = _register(conn, config, "m1_curves", {"kind": "m1_curves", "buckets": {}})
    v2 = _register(conn, config, "m1_curves", {"kind": "m1_curves", "buckets": {"lt7d": {}}})

    registry.set_active(conn, config, "m1_curves", v1)
    assert registry.active_version(conn, "m1_curves")["id"] == v1

    registry.set_active(conn, config, "m1_curves", v2)
    active = registry.active_version(conn, "m1_curves")
    assert active["id"] == v2
    # Exactly one active row for the model_id.
    n_active = conn.execute(
        "SELECT COUNT(*) AS c FROM model_versions WHERE model_id='m1_curves' AND is_active=1"
    ).fetchone()["c"]
    assert n_active == 1
    # The incumbent was retired as 'replaced'.
    assert registry.get_version(conn, v1)["retired_reason"] == "replaced"

    # ACTIVE.json is a generated pointer written by the registry.
    active_json = json.loads((registry.models_dir(config) / "ACTIVE.json").read_text())
    assert active_json["m1_curves"].endswith(".json")
    conn.close()


def test_registry_rollback_restores_prior(config):
    conn = db.connect(config["storage"]["db_path"])
    v1 = _register(conn, config, "m3_params", {"kind": "m3_params", "params": {"k": 0.15}})
    v2 = _register(conn, config, "m3_params", {"kind": "m3_params", "params": {"k": 0.25}})
    registry.set_active(conn, config, "m3_params", v1)
    registry.set_active(conn, config, "m3_params", v2)

    restored = registry.rollback(conn, config, "m3_params", reason="rollback")
    assert restored["id"] == v1
    assert registry.active_version(conn, "m3_params")["id"] == v1
    assert registry.get_version(conn, v2)["retired_reason"] == "rollback"
    conn.close()


# --- (d) automatic rollback on degradation --------------------------------

def _seed_m0(conn, n=60):
    now = now_utc()
    ts = (now - timedelta(days=3)).isoformat(timespec="seconds")
    rts = now.isoformat(timespec="seconds")
    for i in range(n):
        cid = f"0x{i}"
        # A real market has an end date; this one resolves on it, so the
        # stated horizon (what M1 refits on from PAP 9.31) equals the 3 days
        # the realized one gave before.
        conn.execute(
            "INSERT INTO markets (condition_id, question, category, tier, active, closed, "
            "end_date_iso) VALUES (?, ?, 'politics', 'liquid', 1, 1, ?)", (cid, f"Q{i}?", rts))
        p = 0.58 + (i % 5) * 0.01   # 0.58..0.62 -- extremizing hurts when outcomes are NO
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m0_market",
                                  "p_yes": p, "p_market_at_ts": p})
        db.record_resolution(conn, cid, rts, 0.0, False, "gamma")
    conn.commit()


def test_degraded_champion_auto_rolls_back(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_m0(conn)

    # v1 = good (identity), v2 = bad (heavy extremizer) -- both promoted in turn.
    good = {"kind": "m1_curves", "buckets": {"lt7d": {"alpha": 0.0, "beta": 1.0}}}
    bad = {"kind": "m1_curves", "buckets": {"lt7d": {"alpha": 0.0, "beta": 5.0}}}
    v1 = _register(conn, config, "m1_curves", good)
    registry.set_active(conn, config, "m1_curves", v1)
    v2 = _register(conn, config, "m1_curves", bad)
    registry.set_active(conn, config, "m1_curves", v2)
    assert registry.active_version(conn, "m1_curves")["id"] == v2

    results = run_rollback_checks(conn, config, apply=True)
    entry = next(r for r in results if r["model"] == "m1_curves")
    assert entry["degraded"] is True

    # The bad champion was reverted to the good prior version, audit-recorded.
    assert registry.active_version(conn, "m1_curves")["id"] == v1
    assert registry.get_version(conn, v2)["retired_reason"] == "rollback"
    conn.close()


def test_mwu_challenger_degraded_auto_rolls_back(config):
    """Phase 14.1's own acceptance line: 'test_learning_safety.py gains a
    case for this challenger.' The shadow MWU weighting (economy/mwu.py)
    registers under the SAME 'm4_weights' key the incumbent monthly fit
    uses, so its rollback reuses the identical registry mechanics -- proven
    here with a dedicated nightly (not monthly) check, since guardrail 17
    lets this one challenger update between lab learn cycles."""
    from lab.economy.mwu import _mwu_rollback_check

    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(60):
        cid = f"mwu-{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed) "
            "VALUES (?, 'politics', 'liquid', 1, 1)", (cid,))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, ts, outcome, False, "gamma")
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m0_market",
                                  "p_yes": 0.5, "p_market_at_ts": 0.5})
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m1_debiased",
                                  "p_yes": 0.9 if outcome else 0.1, "p_market_at_ts": 0.5})
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m4_ensemble",
                                  "p_yes": 0.7 if outcome else 0.3, "p_market_at_ts": 0.5})
    conn.commit()

    good = {"kind": "m4_weights",
           "categories": {"politics": {"weights": {"m0_market": 0.02, "m1_debiased": 0.98},
                                       "n_resolved": 60}}}
    bad = {"kind": "m4_weights",
          "categories": {"politics": {"weights": {"m0_market": 0.98, "m1_debiased": 0.02},
                                      "n_resolved": 60}}}
    v1 = _register(conn, config, "m4_weights", good)
    registry.set_active(conn, config, "m4_weights", v1)
    v2 = _register(conn, config, "m4_weights", bad)
    registry.set_active(conn, config, "m4_weights", v2)
    assert registry.active_version(conn, "m4_weights")["id"] == v2

    entry = _mwu_rollback_check(conn, config, apply=True)
    assert entry is not None
    assert entry["degraded"] is True
    assert registry.active_version(conn, "m4_weights")["id"] == v1
    assert registry.get_version(conn, v2)["retired_reason"] == "rollback"
    conn.close()


# --- forward-only registered_ts scoring (guardrail 15) --------------------

def test_registered_challenger_not_scored_before_registration(config):
    conn = db.connect(config["storage"]["db_path"])
    now = now_utc()
    old_ts = (now - timedelta(days=40)).isoformat(timespec="seconds")
    new_ts = (now - timedelta(days=5)).isoformat(timespec="seconds")
    reg_ts = (now - timedelta(days=20)).isoformat(timespec="seconds")

    for i, ts in enumerate([old_ts, new_ts]):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, question, category, tier, active, closed) "
            "VALUES (?, ?, 'politics', 'liquid', 1, 1)", (cid, f"Q{i}?"))
        db.append_forecast(conn, {"ts": ts, "condition_id": cid,
                                  "model_id": "m3_evidence@deepseek",
                                  "p_yes": 0.6, "p_market_at_ts": 0.5})
        db.record_resolution(conn, cid, now.isoformat(timespec="seconds"), 1.0, False, "gamma")
    conn.commit()

    # Without a registration, both resolved forecasts are scorable.
    assert len(resolved_forecast_rows(conn, "m3_evidence@deepseek", None)) == 2

    # After registering the challenger, the pre-registration forecast is excluded.
    path = save_artifact(config, "m3_evidence@deepseek",
                         {"kind": "m3_prompt", "provider": "deepseek"}, promote=False)
    registry.register_version(conn, config, "m3_evidence@deepseek", path, registered_ts=reg_ts)
    rows = resolved_forecast_rows(conn, "m3_evidence@deepseek", None)
    assert len(rows) == 1
    conn.close()


def test_m2_baserates_survives_an_uncategorized_market():
    """Regression, 2026-08-22: a resolved market with category NULL became a
    None dict key, and `sorted(by_cat.items())` then raised
    `'<' not supported between instances of NoneType and str`, taking the
    whole monthly `lab learn` down. Four resolved Kalshi markets first seen on
    08-14 and 08-17 were enough -- which is why the 2026-08-05 run passed and
    every run after would have failed, silently, inside a timer unit nothing
    watches. Such a market cannot contribute to a PER-category base rate, so
    it is dropped; the count is kept because it is the only signal that the
    taxonomy has an unmapped series."""
    from lab.learn.refit import fit_m2_baserates

    rows = ([{"category": "politics", "outcome": 1.0}] * 30
            + [{"category": "politics", "outcome": 0.0}] * 30
            + [{"category": None, "outcome": 1.0}] * 4)

    art = fit_m2_baserates(rows, min_n=50)

    assert art["skipped_uncategorized"] == 4
    assert set(art["categories"]) == {"politics"}
    assert art["categories"]["politics"]["n"] == 60
    assert art["categories"]["politics"]["base_rate"] == pytest.approx(0.5)


def test_m4_weight_fit_never_emits_a_null_category(tmp_path):
    """The same NULL-category shape one gate away: an uncategorized group has
    never reached min_resolved, but a None key reaching json.dumps would land
    in the artifact as the string "null" -- a category no market can match."""
    from lab.models.m4_ensemble import fit_m4_weights
    from lab.store import db
    from lab.util import load_config, now_utc_iso

    conn = db.connect(tmp_path / "lab.db")
    for i in range(120):
        cid = f"0x{i:04x}"
        db.upsert_market(conn, {
            "condition_id": cid, "venue": "polymarket", "venue_native_id": cid,
            "slug": None, "question": "q", "category": None if i % 2 else "politics",
            "description": "d", "end_date_iso": "2026-01-01T00:00:00Z",
            "token_id_yes": None, "token_id_no": None, "neg_risk": 0,
            "active": 0, "closed": 1, "liquidity_num": 1.0, "volume_num": 1.0, "tier": "tail",
        })
        db.append_forecast(conn, {"ts": now_utc_iso(), "condition_id": cid,
                                  "model_id": "m0_market", "p_yes": 0.6, "p_market_at_ts": 0.6})
        db.record_resolution(conn, cid, now_utc_iso(), 1.0, 0, "test")
    conn.commit()

    art = fit_m4_weights(conn, load_config())
    assert None not in art["categories"]
    assert "null" not in art["categories"]
    conn.close()
