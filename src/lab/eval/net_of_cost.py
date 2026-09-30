"""H2's net-of-cost statistic, computed on every scored forecast (PAP 9.41).

H2 asks whether recalibration and structural models beat the market in P1/P2
"net of costs", and the plan made the shadow portfolio (§8) the proxy for the
costs. By 2026-09-30 that portfolio had closed six trades since July: its entry
filter (edge >= 0.05 on M4, spread <= 0.03, depth >= $500) almost never fires,
so the question would reach the freeze unanswerable. This statistic asks it of
every forecast the Brier skill is already computed on.

For each row: bet the model's side at the price a taker would actually pay --
the touch (mid plus half the spread recorded with the forecast) marked up by
the venue's own taker fee in force that day (data/fee_schedule.yaml; per
contract, rate * p * (1 - p)) -- sized as §8 sizes a trade (0.2x Kelly on that
cost-inclusive price, capped at 5% of bankroll), held to resolution. The row's
value is the log of the bankroll multiple. When the edge does not survive the
costs the bet is not placed and the row contributes exactly zero -- the market
itself therefore scores zero, never positive. The statistic is the mean over
event clusters, with the same anytime-valid confidence sequence as the primary
Brier statistic, in the same entry order.

What it leaves out, stated rather than hidden: depth-dependent slippage beyond
the touch (§8's haircut needs a bankroll in dollars; at <=5% Kelly fractions a
touch fill is the realistic case), per-category exposure caps, and compounding
across forecasts -- it scores each forecast's bet on its own.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from lab.shadow.fees import load_fee_schedule, taker_rate_for


def row_growth(rows: list[dict[str, Any]], config: dict[str, Any],
               schedule: dict[str, Any] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(log bankroll multiple, bet placed) per row, cost-inclusive."""
    if not rows:
        return np.zeros(0), np.zeros(0, dtype=bool)
    schedule = schedule if schedule is not None else load_fee_schedule()
    scfg = config["shadow"]
    kelly_mult, cap = float(scfg["kelly_fraction"]), float(scfg["per_market_cap"])

    rates: dict[tuple[str, str | None, str], float] = {}
    rate = np.empty(len(rows))
    for i, r in enumerate(rows):
        key = (r.get("venue") or "polymarket", r.get("category"), r["forecast_ts"][:10])
        if key not in rates:
            rates[key] = taker_rate_for(schedule, key[0], key[1], key[2])
        rate[i] = rates[key]

    p_model = np.array([r["p_yes"] for r in rows], dtype=float)
    mid = np.array([r["p_market_at_ts"] for r in rows], dtype=float)
    spread = np.array([r.get("spread_at_ts") or 0.0 for r in rows], dtype=float)
    yes_won = np.array([r["payout_yes"] for r in rows], dtype=float) == 1.0

    yes = p_model > mid
    q = np.where(yes, p_model, 1.0 - p_model)             # model's probability of its side
    touch = np.where(yes, mid, 1.0 - mid) + np.clip(spread, 0.0, None) / 2.0
    cost = touch * (1.0 + rate * (1.0 - touch))            # per contract, fee included
    valid = (cost > 0.0) & (cost < 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        f_star = np.where(valid, (q - cost) / (1.0 - cost), 0.0)
    f = np.where(f_star > 0.0, np.minimum(f_star * kelly_mult, cap), 0.0)
    bet = f > 0.0
    won = yes_won == yes
    with np.errstate(divide="ignore", invalid="ignore"):
        growth = np.where(
            bet, np.where(won, np.log(1.0 - f + f / np.where(valid, cost, 1.0)), np.log(1.0 - f)),
            0.0)
    return growth, bet
