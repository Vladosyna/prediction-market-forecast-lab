"""H2's net-of-cost statistic (PAP 9.41): hand-computed cases."""

from __future__ import annotations

import math

import pytest

from lab.eval.net_of_cost import row_growth

CONFIG = {"shadow": {"kelly_fraction": 0.2, "per_market_cap": 0.05}}
NO_FEES = {"schedule": []}
KALSHI_7 = {"schedule": [{"venue": "kalshi", "category": "default",
                          "effective_from": "2026-01-01", "taker_rate": 0.07}]}


def _row(p_yes, mid, spread, payout, venue="polymarket", category="economics"):
    return {"p_yes": p_yes, "p_market_at_ts": mid, "spread_at_ts": spread,
            "payout_yes": payout, "venue": venue, "category": category,
            "forecast_ts": "2026-09-01T02:00:00+00:00"}


def test_a_yes_bet_pays_at_the_touch_not_the_mid():
    # touch 0.51; full Kelly (0.7 - 0.51) / 0.49 = 0.388 -> 0.2x = 0.078 -> capped 0.05
    growth, bet = row_growth([_row(0.7, 0.5, 0.02, 1.0), _row(0.7, 0.5, 0.02, 0.0)],
                             CONFIG, NO_FEES)
    assert list(bet) == [True, True]
    assert growth[0] == pytest.approx(math.log(1 - 0.05 + 0.05 / 0.51))
    assert growth[1] == pytest.approx(math.log(1 - 0.05))


def test_the_fee_is_part_of_the_price():
    # Kalshi 7%: per-contract cost 0.51 * (1 + 0.07 * 0.49); Kelly 0.2x not capped
    cost = 0.51 * (1 + 0.07 * 0.49)
    f = 0.2 * (0.55 - cost) / (1 - cost)
    growth, bet = row_growth([_row(0.55, 0.5, 0.02, 1.0, venue="kalshi")], CONFIG, KALSHI_7)
    assert bet[0]
    assert growth[0] == pytest.approx(math.log(1 - f + f / cost))


def test_an_edge_the_costs_eat_places_no_bet():
    # model 0.51 vs touch 0.51: nothing left after the half-spread
    growth, bet = row_growth([_row(0.51, 0.5, 0.02, 1.0)], CONFIG, NO_FEES)
    assert not bet[0] and growth[0] == 0.0


def test_the_market_itself_never_scores_positive():
    rows = [_row(0.4, 0.4, 0.02, y) for y in (0.0, 1.0)] * 5
    growth, bet = row_growth(rows, CONFIG, KALSHI_7)
    assert not bet.any() and (growth == 0.0).all()


def test_a_no_bet_is_priced_on_the_no_side():
    # model 0.2 vs mid 0.4: buy NO at 0.6 + 0.01 = 0.61 with q = 0.8
    f = min(0.2 * (0.8 - 0.61) / (1 - 0.61), 0.05)
    growth, bet = row_growth([_row(0.2, 0.4, 0.02, 0.0)], CONFIG, NO_FEES)
    assert bet[0]
    assert growth[0] == pytest.approx(math.log(1 - f + f / 0.61))


def test_eval_rows_carry_the_statistic(tmp_path):
    from datetime import timedelta

    from lab.eval.run import run_eval
    from lab.store import db
    from lab.util import load_config, now_utc

    cfg = load_config()
    cfg["storage"] = {k: str(tmp_path / k) for k in
                      ("snapshots_dir", "models_dir", "logs_dir", "reports_dir")}
    cfg["storage"]["db_path"] = str(tmp_path / "lab.db")
    conn = db.connect(cfg["storage"]["db_path"])
    ts = (now_utc() - timedelta(days=30)).isoformat(timespec="seconds")
    end = (now_utc() - timedelta(days=20)).isoformat(timespec="seconds")
    db.upsert_market(conn, {
        "condition_id": "0xw", "venue": "polymarket", "venue_native_id": "0xw", "slug": None,
        "question": "q", "category": "weather", "description": "d", "end_date_iso": end,
        "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 0, "closed": 1,
        "liquidity_num": 1.0, "volume_num": 1.0, "tier": "liquid"})
    db.append_forecast(conn, {"ts": ts, "condition_id": "0xw", "model_id": "m1_debiased",
                              "p_yes": 0.7, "p_market_at_ts": 0.5, "spread_at_ts": 0.02})
    db.record_resolution(conn, "0xw", end, 1.0, False, "gamma")
    conn.commit()
    run_eval(conn, cfg)
    row = conn.execute(
        "SELECT net_growth, net_bet_share, net_growth_cs_lo, net_growth_cs_hi FROM eval_runs "
        "WHERE model_id='m1_debiased' AND window_label='all_time' AND category='weather'"
    ).fetchone()
    assert row["net_bet_share"] == 1.0 and row["net_growth"] > 0
    assert row["net_growth_cs_lo"] is not None and row["net_growth_cs_hi"] is not None
    conn.close()
