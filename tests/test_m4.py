"""M4 ensemble pooling and weight fitting."""

from __future__ import annotations

import pytest

from lab.learn.refit import logit, sigmoid
from lab.models.base import MarketState
from lab.models.m4_ensemble import M4Ensemble, fit_m4_weights
from lab.store import db
from lab.store.snapshots import SnapshotStore
from lab.util import load_config, now_utc


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _state(cid="0x1", category="politics") -> MarketState:
    return MarketState(
        condition_id=cid, question="Q?", category=category, description=None,
        end_date_iso=None, tier="liquid", p_market=0.5, spread=0.02,
        snapshot_ts="2026-07-02T12:00:00+00:00", days_to_resolution=30.0,
    )


def _add_forecast(conn, cid, model_id, p_yes, ts=None):
    db.append_forecast(conn, {
        "ts": ts or now_utc().isoformat(timespec="seconds"),
        "condition_id": cid, "model_id": model_id,
        "p_yes": p_yes, "p_market_at_ts": 0.5,
    })


def test_equal_weight_pool_is_logodds_mean(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    res = M4Ensemble(conn, None).forecast(_state(), {})
    expected = float(sigmoid((logit(0.5) + logit(0.7)) / 2))
    assert res.p_yes == pytest.approx(expected)
    assert res.meta["weighted"] is False


def test_abstains_with_fewer_than_two_members(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    conn.commit()
    assert M4Ensemble(conn, None).forecast(_state(), {}) is None


def test_fitted_weights_favor_better_model(conn):
    config = load_config()
    # 120 resolved politics markets: m1 always closer to truth than m0.
    for i in range(120):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed, end_date_iso) "
            "VALUES (?, 'politics', 'liquid', 1, 1, '2026-06-20T00:00:00+00:00')", (cid,))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        _add_forecast(conn, cid, "m0_market", 0.5)
        _add_forecast(conn, cid, "m1_debiased", 0.8 if outcome else 0.2)
    conn.commit()
    art = fit_m4_weights(conn, config)
    weights = art["categories"]["politics"]["weights"]
    assert weights["m1_debiased"] > weights["m0_market"]
    assert sum(weights.values()) == pytest.approx(1.0)


def test_fit_m4_weights_respects_floor_and_ceiling(conn):
    """Phase 14.1, v2.2 parity: the incumbent monthly fit gets the same
    2%/60% floor/ceiling as the MWU challenger. m1's Brier is so much better
    than m0's here that the naive softmax alone would push m1 above 98% and
    m0 below 2% -- the clamp must bring both back into [0.02, 0.60]."""
    config = load_config()
    for i in range(120):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed, end_date_iso) "
            "VALUES (?, 'politics', 'liquid', 1, 1, '2026-06-20T00:00:00+00:00')", (cid,))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        _add_forecast(conn, cid, "m0_market", 0.5)
        _add_forecast(conn, cid, "m1_debiased", 0.8 if outcome else 0.2)
    conn.commit()
    art = fit_m4_weights(conn, config)
    weights = art["categories"]["politics"]["weights"]
    assert weights["m1_debiased"] == pytest.approx(0.60, abs=1e-4)
    assert weights["m0_market"] == pytest.approx(0.40, abs=1e-4)
    assert sum(weights.values()) == pytest.approx(1.0)


def _seed_resolved_category(conn, n, category="politics"):
    for i in range(n):
        cid = f"0x{i}"
        conn.execute(
            "INSERT INTO markets (condition_id, category, tier, active, closed, end_date_iso) "
            "VALUES (?, ?, 'liquid', 1, 1, '2026-06-20T00:00:00+00:00')", (cid, category))
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        _add_forecast(conn, cid, "m0_market", 0.5)
        _add_forecast(conn, cid, "m1_debiased", 0.8 if outcome else 0.2)
    conn.commit()


def test_min_resolved_threshold_reads_config_not_the_constant(conn):
    """Regression guard: `learn.m4_min_resolved_per_category` must actually
    drive the equal-weights gate. Until 2026-07-25 the gate used a hardcoded
    module constant and this key was dead, so config and code could silently
    disagree (the paper draft's S5.8 footnote flagged exactly this).

    30 resolved markets seed total_n=60 (two POOLABLE members forecast each
    one) -- below the shipped default (100), above a lowered value (50), so
    the two configs must disagree here.
    """
    _seed_resolved_category(conn, 30)
    config = load_config()

    config["learn"]["m4_min_resolved_per_category"] = 100
    assert fit_m4_weights(conn, config)["categories"] == {}

    config["learn"]["m4_min_resolved_per_category"] = 50
    assert "politics" in fit_m4_weights(conn, config)["categories"]


def test_min_resolved_falls_back_to_constant_when_key_absent(conn):
    """Removing the key entirely must not crash or silently admit every
    category -- it falls back to the module default."""
    from lab.models.m4_ensemble import MIN_RESOLVED_PER_CATEGORY

    # total_n = 2 * markets, kept just under the default so the gate must bite.
    _seed_resolved_category(conn, MIN_RESOLVED_PER_CATEGORY // 2 - 20)
    config = load_config()
    config["learn"].pop("m4_min_resolved_per_category", None)
    assert fit_m4_weights(conn, config)["categories"] == {}


# --- Phase 13: extremization applied at forecast time -----------------------

def test_no_extremization_artifact_is_byte_identical_to_today(conn):
    """Regression guard: omitting extremization_artifact (the default) must
    reproduce today's plain log-odds pool exactly -- no behavior change for
    existing deployments until a fit actually runs."""
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    without = M4Ensemble(conn, None).forecast(_state(), {})
    with_none = M4Ensemble(conn, None, None).forecast(_state(), {})
    assert without.p_yes == pytest.approx(with_none.p_yes)
    assert with_none.meta["extremization_a_eff"] == pytest.approx(1.0)


def test_extremization_shifts_pool_away_from_plain_average(conn):
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    plain = M4Ensemble(conn, None).forecast(_state(), {})
    ext_artifact = {"categories": {"politics": {"a": 2.0, "rho_bar": 0.0}}}
    extremized = M4Ensemble(conn, None, ext_artifact).forecast(_state(), {})

    assert extremized.meta["extremization_a_eff"] == pytest.approx(2.0)  # rho_bar=0 -> full a
    # Both members agree YES-leaning (0.5, 0.7) -> extremizing pushes further
    # from 0.5 in the same direction, not toward it.
    assert extremized.p_yes > plain.p_yes > 0.5


def test_extremization_fully_correlated_pair_collapses_to_identity(conn):
    """rho_bar=1.0 with n=2 members -> n_eff=1 -> a_eff=1.0 regardless of the
    fitted a (Phase 13's "duplicating a source suppresses extremization")."""
    _add_forecast(conn, "0x1", "m0_market", 0.5)
    _add_forecast(conn, "0x1", "m1_debiased", 0.7)
    conn.commit()
    plain = M4Ensemble(conn, None).forecast(_state(), {})
    ext_artifact = {"categories": {"politics": {"a": 2.5, "rho_bar": 1.0}}}
    extremized = M4Ensemble(conn, None, ext_artifact).forecast(_state(), {})
    assert extremized.meta["extremization_a_eff"] == pytest.approx(1.0)
    assert extremized.p_yes == pytest.approx(plain.p_yes)
def test_second_pass_reuses_the_frozen_state_set(conn, tmp_path, monkeypatch):
    """Regression, 2026-08-14 and 2026-08-16: the ensemble ran as a second
    `run_forecasts` pass that derived its OWN eligibility, 13-18 minutes after
    the base pass. Guardrail 13's 15-minute freshness window had reopened by
    then against every market the collector had not re-snapshotted in the gap,
    so M4 wrote 7 rows against m0's 524 (and 12 against 499), while the ratio
    is exactly 1.00 on every other day. A caller that supplies states must be
    given exactly those states, and the shared `ts` must be the row's freeze
    moment -- M4 is pooling prices frozen then, not now."""
    import lab.forecast as fc
    from lab.models.m0_market import M0Market

    def _must_not_run(*_a, **_k):
        raise AssertionError("eligibility re-derived despite caller-supplied states")

    monkeypatch.setattr(fc, "eligible_market_states", _must_not_run)
    frozen_ts = "2026-08-14T02:00:02+00:00"
    counts = fc.run_forecasts(conn, SnapshotStore(tmp_path / "snapshots"), [M0Market()],
                              load_config(), states=[_state("0xfrozen")], ts=frozen_ts)

    assert counts["eligible_markets"] == 1
    assert counts["written"] == 1
    row = conn.execute("SELECT ts, model_id FROM forecasts").fetchone()
    assert row["ts"] == frozen_ts
    assert row["model_id"] == "m0_market"


def test_pool_date_anchors_the_window_across_midnight(conn):
    """`date('now')` made the pool window follow wall-clock rather than the run:
    a bundle whose base pass lands before midnight and whose ensemble pass lands
    after it would pool nothing and abstain everywhere. pool_date pins the
    window to the run that wrote the rows."""
    _add_forecast(conn, "0x1", "m0_market", 0.5, ts="2026-08-14T23:58:00+00:00")
    _add_forecast(conn, "0x1", "m1_debiased", 0.7, ts="2026-08-14T23:58:00+00:00")
    conn.commit()

    assert M4Ensemble(conn, None, pool_date="2026-08-14").forecast(_state(), {}) is not None
    assert M4Ensemble(conn, None, pool_date="2026-08-15").forecast(_state(), {}) is None
