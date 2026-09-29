"""Phase 13 -- extremized, correlation-aware pooling (CLAUDE.md M4/M7
extremization, brief section 6/10)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from lab.learn import registry
from lab.learn.loop import fit_m4_extremization, fit_m7_extremization
from lab.learn.pooling import (
    discount_extremization_exponent,
    effective_source_count,
    estimate_rho_bar_m4,
    estimate_rho_bar_m7,
    fit_extremization_exponent,
)
from lab.learn.refit import WalkForwardError, logit, sigmoid
from lab.models.m7_crossvenue import save_markets_map
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
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


# --- acceptance criterion 1: a=1.0 reproduces current pooling exactly ------

def test_a_equals_one_reproduces_current_pooling_exactly():
    pooled_logit = 0.73
    for n in (1, 2, 5, 20):
        for rho_bar in (0.0, 0.3, 0.9, 1.0):
            a_eff = discount_extremization_exponent(1.0, n, rho_bar)
            assert a_eff == pytest.approx(1.0)
            assert a_eff * pooled_logit == pytest.approx(pooled_logit)


# --- acceptance criterion 2: duplicating a source suppresses extremization -

def test_duplicating_source_drives_n_eff_to_one_and_suppresses_extremization():
    n_eff = effective_source_count(n=2, rho_bar=1.0)
    assert n_eff == pytest.approx(1.0)
    a_eff = discount_extremization_exponent(a_raw=2.5, n=2, rho_bar=1.0)
    assert a_eff == pytest.approx(1.0)


def test_effective_source_count_bounds():
    assert effective_source_count(n=5, rho_bar=0.0) == pytest.approx(5.0)
    n_eff = effective_source_count(n=5, rho_bar=0.5)
    assert 1.0 <= n_eff <= 5.0
    # A negative correlation is clamped to 0 rather than inflating n_eff past n.
    assert effective_source_count(n=5, rho_bar=-0.8) == pytest.approx(5.0)
    assert effective_source_count(n=1, rho_bar=0.9) == pytest.approx(1.0)
    assert effective_source_count(n=0, rho_bar=0.9) == pytest.approx(0.0)


def test_discount_scales_between_identity_and_full_extremization():
    # Partial correlation -> a_eff strictly between 1.0 and a_raw.
    a_eff = discount_extremization_exponent(a_raw=2.0, n=5, rho_bar=0.5)
    assert 1.0 < a_eff < 2.0
    # Fully independent sources -> full extremization is applied, unshrunk.
    a_full = discount_extremization_exponent(a_raw=2.0, n=5, rho_bar=0.0)
    assert a_full == pytest.approx(2.0)


# --- fit_extremization_exponent: walk-forward structural guard ------------

def test_fit_extremization_exponent_requires_validation_window():
    train = [{"p_pooled": 0.6, "outcome": 1.0}] * 50
    with pytest.raises(WalkForwardError):
        fit_extremization_exponent(train, [])
    with pytest.raises(WalkForwardError):
        fit_extremization_exponent([], train)


def test_fit_extremization_exponent_picks_extremizing_a_when_underconfident():
    """Synthetic underconfident pool (raw pooled p sits closer to 0.5 than the
    true outcome probability) -- the grid search should prefer a > 1.0."""
    rng = np.random.default_rng(3)
    n = 2000
    true_logit = rng.uniform(-3, 3, size=n)
    y = rng.binomial(1, sigmoid(true_logit))
    # Pool is shrunk toward 0 relative to the true signal (underconfident).
    pooled_logit = 0.5 * true_logit
    p_pooled = sigmoid(pooled_logit)
    rows = [{"p_pooled": float(p), "outcome": float(o)} for p, o in zip(p_pooled, y)]
    train, validation = rows[: n // 2], rows[n // 2:]

    result = fit_extremization_exponent(train, validation)
    assert result["a"] > 1.0
    assert result["n_train"] == len(train)
    assert result["n_validation"] == len(validation)


# --- estimate_rho_bar_m4 ----------------------------------------------------

def _seed_forecast(conn, cid, ts, model_id, p_yes, category="politics"):
    conn.execute(
        """INSERT OR IGNORE INTO markets (condition_id, question, category, tier, active, closed)
           VALUES (?, ?, ?, 'liquid', 1, 0)""",
        (cid, f"Q {cid}?", category),
    )
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": model_id, "p_yes": p_yes, "p_market_at_ts": 0.5,
    })


def test_estimate_rho_bar_m4_identical_models_correlate_near_one(config):
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    rng = np.random.default_rng(5)
    for i in range(40):
        p = float(np.clip(rng.uniform(0.1, 0.9), 0.02, 0.98))
        cid = f"0x{i}"
        # Two model_ids report the SAME probability every time -> perfectly correlated.
        _seed_forecast(conn, cid, ts, "m0_market", p)
        _seed_forecast(conn, cid, ts, "m1_debiased", p)
    conn.commit()

    rho = estimate_rho_bar_m4(conn, config, model_ids=("m0_market", "m1_debiased"),
                              min_pairs_per_category=10)
    assert rho["politics"] == pytest.approx(1.0, abs=1e-6)
    conn.close()


def test_estimate_rho_bar_m4_omits_categories_below_min_pairs(config):
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(3):  # below any reasonable min_pairs_per_category
        cid = f"0x{i}"
        _seed_forecast(conn, cid, ts, "m0_market", 0.5 + 0.01 * i, category="weather")
        _seed_forecast(conn, cid, ts, "m1_debiased", 0.5 + 0.01 * i, category="weather")
    conn.commit()

    rho = estimate_rho_bar_m4(conn, config, model_ids=("m0_market", "m1_debiased"),
                              min_pairs_per_category=30)
    assert "weather" not in rho
    conn.close()


# --- estimate_rho_bar_m7 ----------------------------------------------------

def test_estimate_rho_bar_m7_correlates_matched_pair_snapshot_history(config, tmp_path):
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    conn = db.connect(config["storage"]["db_path"])
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"}],
         "proposed": []},
        map_path,
    )

    now = now_utc()
    rng = np.random.default_rng(9)
    for day_offset in range(15):
        ts = now - timedelta(days=day_offset)
        p = float(np.clip(0.5 + 0.02 * day_offset, 0.05, 0.95))
        for cid in ("0x1", "kalshi:T1"):
            store.append([{
                "ts": floor_ts_bucket(ts, 5), "condition_id": cid, "token_id_yes": f"tok-{cid}",
                "best_bid": p - 0.01, "best_ask": p + 0.01, "mid": p, "spread": 0.02,
                "bid_depth_usd": 100.0, "ask_depth_usd": 100.0, "last_trade_price": None,
                "venue": "kalshi" if cid.startswith("kalshi") else "polymarket",
            }])

    rho = estimate_rho_bar_m7(conn, store, config, markets_map_path=map_path, min_days=5)
    # Both sides moved in lockstep (same p every day) -> near-perfect correlation.
    assert rho == pytest.approx(1.0, abs=1e-6)
    conn.close()


def test_estimate_rho_bar_m7_returns_none_without_confirmed_pairs(config, tmp_path):
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    conn = db.connect(config["storage"]["db_path"])
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    assert estimate_rho_bar_m7(conn, store, config, markets_map_path=map_path) is None
    conn.close()


# --- learn/loop.py wiring: fit_m4_extremization / fit_m7_extremization -----

def _seed_resolved(conn, model_id, n, category="politics"):
    # Ended and resolved ten days ago: past PAP 9.35's stated-end censoring.
    ts = (now_utc() - timedelta(days=10)).isoformat(timespec="seconds")
    rng = np.random.default_rng(11)
    true_logit = rng.uniform(-2, 2, size=n)
    y = rng.binomial(1, sigmoid(true_logit))
    p_pooled = sigmoid(0.6 * true_logit)  # underconfident pool -> a > 1.0 should fit
    for i in range(n):
        cid = f"{model_id}-{category}-{i}"
        conn.execute(
            """INSERT OR IGNORE INTO markets (condition_id, question, category, tier, active, closed,
                                               end_date_iso)
               VALUES (?, ?, ?, 'liquid', 1, 1, ?)""",
            (cid, f"Q {cid}?", category, ts),
        )
        db.append_forecast(conn, {
            "ts": ts, "condition_id": cid, "model_id": model_id,
            "p_yes": float(p_pooled[i]), "p_market_at_ts": 0.5,
        })
        db.record_resolution(conn, cid, ts, float(y[i]), False, "gamma")
    conn.commit()


def test_fit_m4_extremization_insufficient_data_skips(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved(conn, "m4_ensemble", n=10)  # well below default min_n=60
    decision = fit_m4_extremization(conn, config, apply=True)
    assert decision["skipped"] == "insufficient_data"
    conn.close()


def test_fit_m4_extremization_registers_first_fit_as_challenger(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved(conn, "m4_ensemble", n=80, category="politics")
    decision = fit_m4_extremization(conn, config, apply=True)
    assert decision["promoted"] is True  # no incumbent for this key yet -> "first"
    assert "politics" in decision["categories"]
    active = registry.active_version(conn, "m4_extremization")
    assert active is not None
    conn.close()


def test_fit_m7_extremization_insufficient_data_skips(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved(conn, "m7_crossvenue", n=10)
    decision = fit_m7_extremization(conn, config, apply=True)
    assert decision["skipped"] == "insufficient_data"
    conn.close()


def test_fit_m7_extremization_registers_first_fit_as_challenger(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_resolved(conn, "m7_crossvenue", n=80)
    decision = fit_m7_extremization(conn, config, apply=True)
    assert decision["promoted"] is True
    active = registry.active_version(conn, "m7_extremization")
    assert active is not None
    conn.close()


def test_estimate_rho_bar_m7_projects_columns_instead_of_reading_order_books(config, tmp_path):
    """This is 90 days x every venue's snapshots. Unprojected it also drags in
    the bids_json/asks_json blobs -- >1.3GB on the live host, OOM-killed
    (2026-08-05). The projection is the whole difference, so it is asserted,
    not assumed.
    """
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    conn = db.connect(config["storage"]["db_path"])
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"}],
         "proposed": []},
        map_path,
    )

    now = now_utc()
    for day_offset in range(12):
        ts = now - timedelta(days=day_offset)
        for cid in ("0x1", "kalshi:T1"):
            store.append([{
                "ts": floor_ts_bucket(ts, 5), "condition_id": cid, "token_id_yes": f"tok-{cid}",
                "best_bid": 0.49, "best_ask": 0.51, "mid": 0.5, "spread": 0.02,
                "bid_depth_usd": 100.0, "ask_depth_usd": 100.0, "last_trade_price": None,
                "venue": "kalshi" if cid.startswith("kalshi") else "polymarket",
            }])

    seen: list = []
    real_read_range = store.read_range

    def spy(dates, columns=None, condition_ids=None):
        seen.append((columns, condition_ids))
        return real_read_range(dates, columns=columns, condition_ids=condition_ids)

    store.read_range = spy
    estimate_rho_bar_m7(conn, store, config, markets_map_path=map_path, min_days=5)
    conn.close()

    assert seen, "estimate_rho_bar_m7 never read snapshots -- fixture is wrong"
    for columns, condition_ids in seen:
        assert columns is not None, "unprojected read: the order-book blobs come with it"
        assert "bids_json" not in columns and "asks_json" not in columns
        # Projection alone still read every row of a 90-day window; the pairs
        # are what this function correlates, so they are what it should read.
        assert condition_ids is not None, "unfiltered read across the whole window"
        assert set(condition_ids) == {"0x1", "kalshi:T1"}


def test_price_moves_24h_projects_columns_too():
    """Same defect class, and this one runs nightly inside the collector's own
    cgroup."""
    import inspect

    from lab import forecast as fc

    src = inspect.getsource(fc.price_moves_24h)
    assert "read_range(dates, columns=" in src, "unprojected snapshot read is back"
