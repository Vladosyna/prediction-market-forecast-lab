"""Phase 14.1 -- shadow MWU ensemble weighting (CLAUDE.md section 6/14.1,
guardrail 17). Covers the five literal acceptance criteria plus the
clamp/clustering primitives underneath them.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from lab.economy.mwu import (
    _mwu_rollback_check,
    fit_mwu_weights,
    mwu_learning_rate,
    mwu_raw_weights,
    update_mwu_challenger,
)
from lab.economy.wealth import update_wealth_ledger
from lab.learn import registry
from lab.learn.pooling import (
    clamp_and_renormalize_weights,
    clamp_weights_with_cluster_ceiling,
    cluster_correlated_models,
    estimate_pairwise_rho_m4,
    estimate_rho_bar_m4,
)
from lab.learn.refit import load_active_artifact, save_artifact
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
    return cfg


def test_clamp_and_renormalize_respects_floor_ceiling_under_adversarial_sequence():
    """Phase 14.1's literal acceptance criterion: under an adversarial
    win/loss sequence (modeled here as a sequence of raw weight vectors
    swinging to extremes, with the 'winning' identity flipping each round),
    every clamped output respects floor<=w_i<=ceiling and sums to 1."""
    floor, ceiling = 0.02, 0.60
    rng = np.random.default_rng(0)
    models = ["A", "B", "C", "D"]
    for round_i in range(50):
        # One model dominates almost completely each round; who dominates
        # flips every round (adversarial: never lets one model "settle").
        winner = models[round_i % len(models)]
        raw = {m: (0.001 if m != winner else 1.0) for m in models}
        # Add noise so it isn't a perfectly clean tie among the losers.
        raw = {m: v * float(rng.uniform(0.5, 1.5)) for m, v in raw.items()}
        clamped = clamp_and_renormalize_weights(raw, floor, ceiling)
        for m in models:
            assert floor - 1e-9 <= clamped[m] <= ceiling + 1e-9, (round_i, m, clamped)
        assert sum(clamped.values()) == pytest.approx(1.0)


def test_clamp_and_renormalize_is_identity_when_already_feasible():
    w = {"A": 0.4, "B": 0.35, "C": 0.25}
    out = clamp_and_renormalize_weights(w, floor=0.02, ceiling=0.60)
    for k in w:
        assert out[k] == pytest.approx(w[k], abs=1e-6)


def test_clamp_and_renormalize_empty_input():
    assert clamp_and_renormalize_weights({}, 0.02, 0.60) == {}


# --- cluster-aware ceiling (Phase 14.1's second literal criterion) ----------

def test_cluster_correlated_models_groups_only_high_correlation_pairs():
    pairwise = {("A", "A2"): 0.95, ("A", "B"): 0.1, ("A2", "B"): 0.05}
    clusters = cluster_correlated_models(pairwise, ["A", "A2", "B"], threshold=0.8)
    clusters_as_sets = sorted(clusters, key=len)
    assert clusters_as_sets[0] == {"B"}
    assert clusters_as_sets[1] == {"A", "A2"}


def test_cluster_ceiling_caps_duplicated_pair_jointly():
    """Phase 14.1's literal acceptance criterion: duplicating a high-wealth
    model (rho_bar -> 1 for that pair) does not let the pair jointly exceed
    the ceiling, even though a plain per-model clamp would let them (each
    individually already sits under the ceiling)."""
    floor, ceiling = 0.02, 0.60
    raw = {"A": 0.45, "A2": 0.45, "B": 0.10}  # A2 is a wealth-duplicate of A

    plain = clamp_and_renormalize_weights(raw, floor, ceiling)
    assert plain["A"] + plain["A2"] == pytest.approx(0.90)  # plain clamp does NOT fix this

    clusters = cluster_correlated_models(
        {("A", "A2"): 1.0, ("A", "B"): 0.0, ("A2", "B"): 0.0}, ["A", "A2", "B"], threshold=0.8
    )
    clustered = clamp_weights_with_cluster_ceiling(raw, clusters, floor, ceiling)
    assert clustered["A"] + clustered["A2"] <= ceiling + 1e-6
    assert clustered["B"] >= floor
    assert sum(clustered.values()) == pytest.approx(1.0)
    # Confirms the cluster-aware mechanism is what closes the gap the plain clamp leaves open.
    assert clustered["A"] + clustered["A2"] < plain["A"] + plain["A2"]


def test_cluster_ceiling_whole_pool_correlated_falls_back_to_equal_weights():
    raw = {"A": 0.7, "B": 0.3}
    clusters = [{"A", "B"}]  # the only cluster spans the entire pool
    out = clamp_weights_with_cluster_ceiling(raw, clusters, 0.02, 0.60)
    assert out["A"] == pytest.approx(0.5)
    assert out["B"] == pytest.approx(0.5)


def test_cluster_ceiling_no_clusters_behaves_like_plain_clamp():
    raw = {"A": 0.4, "B": 0.35, "C": 0.25}
    out = clamp_weights_with_cluster_ceiling(raw, [], 0.02, 0.60)
    plain = clamp_and_renormalize_weights(raw, 0.02, 0.60)
    for k in raw:
        assert out[k] == pytest.approx(plain[k], abs=1e-6)


# --- estimate_pairwise_rho_m4 refactor: regression guard --------------------

def _seed_forecast(conn, cid, ts, model_id, p_yes, category="politics"):
    conn.execute(
        """INSERT OR IGNORE INTO markets (condition_id, question, category, tier, active, closed)
           VALUES (?, ?, ?, 'liquid', 1, 0)""",
        (cid, f"Q {cid}?", category),
    )
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": model_id, "p_yes": p_yes, "p_market_at_ts": 0.5,
    })


def test_pairwise_and_mean_rho_agree_after_refactor(config):
    """Regression guard on the estimate_rho_bar_m4 refactor: its own return
    value must still equal the mean of estimate_pairwise_rho_m4's per-pair
    values for the same category -- the refactor changed how the result is
    exposed, not what it computes."""
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    rng = np.random.default_rng(3)
    model_ids = ("m0_market", "m1_debiased", "m2_baserate")
    for i in range(50):
        cid = f"0x{i}"
        base = float(np.clip(rng.uniform(0.1, 0.9), 0.05, 0.95))
        # m0/m1 track each other closely; m2 is independent noise.
        _seed_forecast(conn, cid, ts, "m0_market", base)
        _seed_forecast(conn, cid, ts, "m1_debiased", float(np.clip(base + rng.normal(0, 0.01), 0.02, 0.98)))
        _seed_forecast(conn, cid, ts, "m2_baserate", float(np.clip(rng.uniform(0.1, 0.9), 0.05, 0.95)))
    conn.commit()

    mean_rho = estimate_rho_bar_m4(conn, config, model_ids=model_ids, min_pairs_per_category=10)
    pairwise = estimate_pairwise_rho_m4(conn, config, model_ids=model_ids, category="politics",
                                        min_pairs=10)
    assert mean_rho["politics"] == pytest.approx(float(np.mean(list(pairwise.values()))))
    # And the m0/m1 pair should show up as clearly more correlated than the others.
    m0_m1 = pairwise.get(("m0_market", "m1_debiased"))
    assert m0_m1 is not None and m0_m1 > 0.9
    conn.close()


# --- mwu_learning_rate -------------------------------------------------------

def test_mwu_learning_rate_shrinks_with_t():
    eta_early = mwu_learning_rate(n_models=4, t=10)
    eta_late = mwu_learning_rate(n_models=4, t=1000)
    assert eta_early > eta_late > 0
    # matches the closed form at a couple of hand-picked points
    assert mwu_learning_rate(4, 8) == pytest.approx(np.sqrt(8 * np.log(4) / 8))
    assert mwu_learning_rate(2, 1) == pytest.approx(np.sqrt(8 * np.log(2) / 1))


def test_mwu_raw_weights_favor_higher_wealth():
    w = mwu_raw_weights({"A": 0.05, "B": 0.01}, eta=2.0)
    assert w["A"] > w["B"]
    assert sum(w.values()) == pytest.approx(1.0)


# --- update_mwu_challenger: invisible-until-promoted / promotion / probation

def _seed_m4_pool(conn, n=60, category="politics"):
    # Forecast, ended and resolved ten days ago: past PAP 9.35's censoring.
    ts = (now_utc() - timedelta(days=10)).isoformat(timespec="seconds")
    for i in range(n):
        cid = f"pool-{category}-{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed, end_date_iso) "
            "VALUES (?, ?, 'liquid', 1, 1, ?)", (cid, category, ts),
        )
        outcome = float(i % 2)
        db.record_resolution(conn, cid, ts, outcome, False, "gamma")
        # m0 is mediocre (always 0.5); m1 is sharp (tracks the outcome tightly).
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m0_market",
                                  "p_yes": 0.5, "p_market_at_ts": 0.5})
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m1_debiased",
                                  "p_yes": 0.9 if outcome else 0.1, "p_market_at_ts": 0.5})
        db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": "m4_ensemble",
                                  "p_yes": 0.7 if outcome else 0.3, "p_market_at_ts": 0.5})
    conn.commit()


def test_mwu_min_resolved_threshold_reads_config_not_the_constant(config):
    """The challenger's coverage gate must read the same config key the
    incumbent fit reads (`learn.m4_min_resolved_per_category`), so the two
    cannot silently diverge. Until 2026-07-25 both used a hardcoded constant
    and the key was dead.

    n=30 seeds total_n=60 across the two POOLABLE members -- below the
    shipped default (100), above a lowered config value (50).
    """
    conn = db.connect(config["storage"]["db_path"])
    _seed_m4_pool(conn, n=30)
    update_wealth_ledger(conn, config)

    config["learn"]["m4_min_resolved_per_category"] = 100
    assert fit_mwu_weights(conn, config)["categories"] == {}

    config["learn"]["m4_min_resolved_per_category"] = 50
    assert "politics" in fit_mwu_weights(conn, config)["categories"]
    conn.close()


def test_mwu_challenger_invisible_until_promoted(config):
    """Phase 14.1's literal acceptance criterion: the challenger is
    invisible to production forecasts until promoted -- here it's blocked
    by probation (age_days=0 on the very first mwu version), which is
    itself part of "not yet earned promotion.\""""
    conn = db.connect(config["storage"]["db_path"])
    _seed_m4_pool(conn, n=150)
    update_wealth_ledger(conn, config)

    before = load_active_artifact(config, "m4_weights")
    result = update_mwu_challenger(conn, config, apply=True)
    assert result["challenger"]["promoted"] is False
    assert result["challenger"]["reason"] == "probation"
    after = load_active_artifact(config, "m4_weights")
    assert after == before  # unchanged -- invisible to M4Ensemble/production
    conn.close()


def test_mwu_respects_probation_on_insufficient_n():
    """Phase 14.1's literal acceptance criterion (n-side of probation): even
    with the 90-day clock already satisfied, too few resolved forecasts per
    category still blocks promotion."""
    cfg = load_config()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg["storage"] = {
            "db_path": f"{d}/lab.db", "snapshots_dir": f"{d}/snapshots",
            "models_dir": f"{d}/models", "logs_dir": f"{d}/logs", "reports_dir": f"{d}/reports",
        }
        conn = db.connect(cfg["storage"]["db_path"])
        _seed_m4_pool(conn, n=60)  # total_n=120: >=100 (fit threshold) but <200 (probation)
        update_wealth_ledger(conn, cfg)

        old_ts = (now_utc() - timedelta(days=91)).isoformat(timespec="seconds")
        artifact = fit_mwu_weights(conn, cfg)
        path = save_artifact(cfg, "m4_weights", artifact, promote=False)
        registry.register_version(conn, cfg, "m4_weights", path, version_tag="mwu-v1",
                                  registered_ts=old_ts)

        result = update_mwu_challenger(conn, cfg, apply=True)
        assert result["challenger"]["age_days"] >= 90  # the 90-day clock IS satisfied
        assert result["challenger"]["min_n_over_categories"] < 200  # but n is not
        assert result["challenger"]["promoted"] is False
        assert result["challenger"]["reason"] == "probation"
        conn.close()


def test_mwu_promotion_after_probation_clears(config):
    """Phase 14.1's literal acceptance criterion: once probation clears
    (90 days + n>=200 per category) and there's no incumbent to beat, the
    challenger is promoted."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_m4_pool(conn, n=210)  # total_n=420, comfortably over the 200 floor
    update_wealth_ledger(conn, config)

    old_ts = (now_utc() - timedelta(days=91)).isoformat(timespec="seconds")
    artifact = fit_mwu_weights(conn, config)
    path = save_artifact(config, "m4_weights", artifact, promote=False)
    registry.register_version(conn, config, "m4_weights", path, version_tag="mwu-v1",
                              registered_ts=old_ts)

    result = update_mwu_challenger(conn, config, apply=True)
    assert result["challenger"]["probation_cleared"] is True
    assert result["challenger"]["promoted"] is True
    assert result["challenger"]["reason"] == "first"  # no incumbent existed to challenge
    assert load_active_artifact(config, "m4_weights") is not None
    conn.close()


def test_mwu_promotion_and_rollback(config):
    """Phase 14.1's literal acceptance criterion: a promoted MWU weighting
    that subsequently underperforms triggers the same automatic rollback as
    any other challenger."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_m4_pool(conn, n=60)

    good = {"kind": "m4_weights",
           "categories": {"politics": {"weights": {"m0_market": 0.02, "m1_debiased": 0.98},
                                       "n_resolved": 60}}}
    bad = {"kind": "m4_weights",
          "categories": {"politics": {"weights": {"m0_market": 0.98, "m1_debiased": 0.02},
                                      "n_resolved": 60}}}
    path1 = save_artifact(config, "m4_weights", good, promote=False)
    v1 = registry.register_version(conn, config, "m4_weights", path1)
    registry.set_active(conn, config, "m4_weights", v1)
    path2 = save_artifact(config, "m4_weights", bad, promote=False)
    v2 = registry.register_version(conn, config, "m4_weights", path2)
    registry.set_active(conn, config, "m4_weights", v2)

    result = _mwu_rollback_check(conn, config, apply=True)
    assert result is not None
    assert result["degraded"] is True
    assert registry.active_version(conn, "m4_weights")["id"] == v1
    conn.close()
