"""Phase 14 -- virtual prediction economy: wealth ledger (CLAUDE.md section 6/14).

Covers the two literal acceptance criteria (sleeping-expert correctness,
coverage-normalized ranking) plus idempotency, compounding order, and the
core Kelly/log-wealth arithmetic.
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pytest

from lab.economy.wealth import log_wealth_delta, update_wealth_ledger, wealth_kelly_fraction
from lab.eval.wealth_plots import (
    bootstrap_wealth_bands,
    m4_attribution_snapshot,
    plot_wealth_curves,
    plot_wealth_drawdown,
    sleeping_expert_rankings,
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
    return cfg


def test_wealth_kelly_fraction_hand_computed(config):
    # p_model=0.6 > p_market=0.5 -> YES side, price=0.5, p_win=0.6.
    # f* = (0.6-0.5)/(1-0.5) = 0.2; 0.2x Kelly = 0.04 (under the 5% cap).
    side, price, f = wealth_kelly_fraction(0.6, 0.5, config)
    assert side == "YES" and price == pytest.approx(0.5) and f == pytest.approx(0.04)

    # p_model=0.3 < p_market=0.5 -> NO side, price=1-0.5=0.5, p_win=1-0.3=0.7.
    # f* = (0.7-0.5)/(1-0.5) = 0.4; 0.2x = 0.08 > 5% cap -> capped at 0.05.
    side, price, f = wealth_kelly_fraction(0.3, 0.5, config)
    assert side == "NO" and price == pytest.approx(0.5) and f == pytest.approx(0.05)

    # Exactly no edge -> f == 0.
    side, price, f = wealth_kelly_fraction(0.5, 0.5, config)
    assert f == 0.0


def test_log_wealth_delta_hand_computed():
    # YES side, price=0.5, f=0.04. Win: log(1 - 0.04 + 0.04/0.5) = log(1.04).
    assert log_wealth_delta("YES", 0.5, 0.04, 1.0) == pytest.approx(math.log(1.04))
    # Lose: log(1 - 0.04) = log(0.96).
    assert log_wealth_delta("YES", 0.5, 0.04, 0.0) == pytest.approx(math.log(0.96))
    # NO side wins when payout_yes == 0.0.
    assert log_wealth_delta("NO", 0.5, 0.05, 0.0) == pytest.approx(math.log(1 - 0.05 + 0.05 / 0.5))
    assert log_wealth_delta("NO", 0.5, 0.05, 1.0) == pytest.approx(math.log(1 - 0.05))
    # f<=0 -> flat wealth regardless of outcome.
    assert log_wealth_delta("YES", 0.5, 0.0, 1.0) == 0.0
    assert log_wealth_delta("YES", 0.5, 0.0, 0.0) == 0.0


# --- update_wealth_ledger ----------------------------------------------------

def _seed_resolved_forecast(conn, cid, model_id, p_yes, p_market, outcome, ts, resolved_ts,
                            category="politics"):
    # The stated end date sits eight days before the recorded resolution, so
    # PAP 9.35's censoring (end + 7 days) admits the row at exactly its
    # resolution time: entry order == resolution order, which is what these
    # ledger-mechanics tests are about.
    from datetime import datetime
    end = (datetime.fromisoformat(resolved_ts) - timedelta(days=8)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR IGNORE INTO markets (condition_id, question, category, tier, active, closed,
                                           end_date_iso)
           VALUES (?, ?, ?, 'liquid', 1, 1, ?)""",
        (cid, f"Q {cid}?", category, end),
    )
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": model_id,
        "p_yes": p_yes, "p_market_at_ts": p_market,
    })
    db.record_resolution(conn, cid, resolved_ts, outcome, False, "gamma")
    conn.commit()


def test_sleeping_expert_correctness(config):
    """Phase 14's literal acceptance criterion: a model that never forecasts
    a category shows NO wealth_ledger row for it at all (not a zero row)."""
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    _seed_resolved_forecast(conn, "0x1", "model_a", 0.6, 0.5, 1.0, ts, ts, category="politics")
    _seed_resolved_forecast(conn, "0x2", "model_a", 0.6, 0.5, 1.0, ts, ts, category="weather")
    _seed_resolved_forecast(conn, "0x3", "model_b", 0.6, 0.5, 1.0, ts, ts, category="politics")
    # model_b never forecasts weather.

    update_wealth_ledger(conn, config)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM wealth_ledger WHERE model_id='model_b' AND category='weather'"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM wealth_ledger WHERE model_id='model_a' AND category='weather'"
    ).fetchone()["n"] == 1
    conn.close()


def test_coverage_normalized_ranking_favors_sharp_low_coverage_model(config):
    """Phase 14's literal acceptance criterion: the coverage-normalized
    metric ranks a high-coverage mediocre model correctly against a
    low-coverage sharp one, even though raw cumulative wealth ranks them the
    other way (which is exactly the naive comparison the brief warns
    against)."""
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(5):  # sharp, low coverage: big edge (0.9 vs 0.5), always wins
        _seed_resolved_forecast(conn, f"sharp-{i}", "sharp_model", 0.9, 0.5, 1.0, ts, ts)
    for i in range(50):  # mediocre, high coverage: small edge (0.55 vs 0.5), always wins
        _seed_resolved_forecast(conn, f"mediocre-{i}", "mediocre_model", 0.55, 0.5, 1.0, ts, ts)

    update_wealth_ledger(conn, config)

    sharp = conn.execute(
        "SELECT cum_log_wealth, n_forecasts FROM wealth_ledger WHERE model_id='sharp_model' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    mediocre = conn.execute(
        "SELECT cum_log_wealth, n_forecasts FROM wealth_ledger WHERE model_id='mediocre_model' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert mediocre["cum_log_wealth"] > sharp["cum_log_wealth"]  # naive raw comparison is wrong
    sharp_avg = sharp["cum_log_wealth"] / sharp["n_forecasts"]
    mediocre_avg = mediocre["cum_log_wealth"] / mediocre["n_forecasts"]
    assert sharp_avg > mediocre_avg  # coverage-normalized comparison is correct
    conn.close()


def test_update_wealth_ledger_idempotent(config):
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    _seed_resolved_forecast(conn, "0x1", "m0_market", 0.6, 0.5, 1.0, ts, ts)

    first = update_wealth_ledger(conn, config)
    assert first["rows_added"] == 1
    second = update_wealth_ledger(conn, config)
    assert second["rows_added"] == 0

    assert conn.execute("SELECT COUNT(*) AS n FROM wealth_ledger").fetchone()["n"] == 1
    conn.close()


def test_wealth_compounds_in_resolution_order(config):
    conn = db.connect(config["storage"]["db_path"])
    now = now_utc()
    ts1 = (now - timedelta(days=3)).isoformat(timespec="seconds")
    ts2 = (now - timedelta(days=2)).isoformat(timespec="seconds")
    ts3 = (now - timedelta(days=1)).isoformat(timespec="seconds")
    _seed_resolved_forecast(conn, "0xA", "m0_market", 0.6, 0.5, 1.0, ts1, ts1)
    _seed_resolved_forecast(conn, "0xB", "m0_market", 0.6, 0.5, 1.0, ts2, ts2)
    _seed_resolved_forecast(conn, "0xC", "m0_market", 0.6, 0.5, 0.0, ts3, ts3)  # this one loses

    update_wealth_ledger(conn, config)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM wealth_ledger WHERE model_id='m0_market' ORDER BY id"
    )]
    assert len(rows) == 3
    side, price, f = wealth_kelly_fraction(0.6, 0.5, config)
    win_delta = log_wealth_delta(side, price, f, 1.0)
    lose_delta = log_wealth_delta(side, price, f, 0.0)
    expected_cum = [win_delta, 2 * win_delta, 2 * win_delta + lose_delta]
    for row, exp in zip(rows, expected_cum):
        assert row["cum_log_wealth"] == pytest.approx(exp)
    assert [row["n_forecasts"] for row in rows] == [1, 2, 3]
    conn.close()


# --- wealth_plots.py ---------------------------------------------------------

def test_bootstrap_wealth_bands_shape_and_bounds():
    deltas = [0.01, 0.02, -0.01, 0.03, 0.015]
    lo, hi = bootstrap_wealth_bands(deltas, iterations=200)
    assert len(lo) == len(hi) == len(deltas)
    assert np.all(lo <= hi)
    # Permutation never changes the total -- the final step's band collapses
    # exactly onto the true sum regardless of order.
    assert lo[-1] == pytest.approx(sum(deltas))
    assert hi[-1] == pytest.approx(sum(deltas))
    # Intermediate steps have genuine spread (not collapsed to a point).
    assert hi[1] > lo[1]


def test_bootstrap_wealth_bands_empty_input():
    lo, hi = bootstrap_wealth_bands([])
    assert len(lo) == 0 and len(hi) == 0


def test_sleeping_expert_rankings_and_plots_render(config):
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(5):
        _seed_resolved_forecast(conn, f"sharp-{i}", "sharp_model", 0.9, 0.5, 1.0, ts, ts)
    for i in range(10):
        _seed_resolved_forecast(conn, f"mediocre-{i}", "mediocre_model", 0.55, 0.5, 1.0, ts, ts)
    update_wealth_ledger(conn, config)

    rankings = sleeping_expert_rankings(conn)
    politics = [r for r in rankings if r["category"] == "politics"]
    assert politics[0]["model_id"] == "sharp_model"  # ranked first: higher avg_log_wealth

    curves_path = plot_wealth_curves(conn, config)
    assert curves_path is not None and curves_path.exists()
    drawdown_path = plot_wealth_drawdown(conn, config)
    assert drawdown_path is not None and drawdown_path.exists()
    conn.close()


def test_plots_return_none_without_data(config):
    conn = db.connect(config["storage"]["db_path"])
    assert plot_wealth_curves(conn, config) is None
    assert plot_wealth_drawdown(conn, config) is None
    assert sleeping_expert_rankings(conn) == []
    conn.close()


def test_m4_attribution_snapshot_no_crash_without_data(config):
    conn = db.connect(config["storage"]["db_path"])
    assert m4_attribution_snapshot(conn, config) == []
    conn.close()
