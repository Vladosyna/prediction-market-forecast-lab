"""Virtual prediction economy: wealth ledger (Phase 14, brief section 6/14).

A scoring/selection layer over M0-M7, not a new forecasting model -- it
consumes already-written forecasts and never writes a p_yes row of its own.
For every resolved forecast from every model (unconditional on the shadow
portfolio's entry filter, brief section 8, to maximize comparison power),
compute the same 0.2x-capped Kelly fraction and side rule the shadow
portfolio uses, and accumulate the resulting log-wealth delta per
(model_id, category) in `wealth_ledger`.

Sleeping experts: M5 only covers weather/macro, M7 only covers matched
cross-venue markets. Comparing raw cumulative wealth would reward or punish
coverage rather than skill -- always compare `cum_log_wealth / n_forecasts`.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from lab.shadow.portfolio import kelly_stake
from lab.util import now_utc_iso

log = logging.getLogger(__name__)


def wealth_kelly_fraction(p_model: float, p_market: float, config: dict[str, Any]
                         ) -> tuple[str, float, float]:
    """(side, price_of_side_bet, kelly_fraction).

    Reuses shadow.portfolio.kelly_stake with bankroll=1.0 -- that IS the
    fraction, no need to reimplement the Kelly math. Side rule matches the
    shadow portfolio exactly: YES if p_model > p_market, else NO (price
    1 - p_market).
    """
    scfg = config["shadow"]
    if p_model > p_market:
        side, price, p_win = "YES", p_market, p_model
    else:
        side, price, p_win = "NO", 1 - p_market, 1 - p_model
    f = kelly_stake(1.0, p_win, price, scfg["kelly_fraction"], scfg["per_market_cap"])
    return side, price, f


def log_wealth_delta(side: str, price: float, f: float, payout_yes: float) -> float:
    """log(1 - f + f/price) if the bet won, log(1 - f) if it lost.

    f<=0 (no edge -> no bet placed) is flat wealth (0.0) -- the forecast
    still counts toward n_forecasts/coverage, it just contributed no growth.
    """
    if f <= 0:
        return 0.0
    won = (payout_yes == 1.0) == (side == "YES")
    return math.log(1 - f + f / price) if won else math.log(1 - f)


def update_wealth_ledger(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Idempotent, incremental: append wealth_ledger rows for every resolved
    forecast (every model, unconditional on the shadow portfolio's entry
    filter) not yet processed, in (entry time, forecast ts) order so
    cum_log_wealth compounds correctly. Safe to call every night -- the
    NOT EXISTS guard means reprocessing the same forecast twice is a no-op.

    From 2026-09-29 a forecast joins the ledger only once its stated end date
    is SCORING_LAG_DAYS past, like every other resolved-row reader (PAP 9.35):
    MWU derives weights from these rows, and an early-resolution sample is
    enriched for YES. Rows appended before that date are left as written --
    the table is derived, and a rebuild would re-sequence every running sum.
    """
    from lab.eval.run import ENTRY_JD_SQL, SCOREABLE_SQL
    from lab.util import now_utc_iso

    rows = [dict(r) for r in conn.execute(
        f"""
        SELECT f.id AS forecast_id, f.condition_id, f.model_id, f.p_yes, f.p_market_at_ts,
               f.ts AS forecast_ts, r.payout_yes, r.resolved_ts,
               m.category AS category, m.event_id AS event_id
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE NOT EXISTS (SELECT 1 FROM wealth_ledger w WHERE w.forecast_id = f.id)
          AND {SCOREABLE_SQL}
        ORDER BY {ENTRY_JD_SQL}, f.ts
        """, (now_utc_iso(),)
    )]
    if not rows:
        return {"rows_added": 0, "models": []}

    state: dict[tuple[str, str], tuple[float, int]] = {}
    models_touched: set[str] = set()
    for r in rows:
        category = r["category"] or "unknown"
        key = (r["model_id"], category)
        if key not in state:
            existing = conn.execute(
                """SELECT cum_log_wealth, n_forecasts FROM wealth_ledger
                   WHERE model_id = ? AND category = ? ORDER BY id DESC LIMIT 1""",
                key,
            ).fetchone()
            state[key] = (existing["cum_log_wealth"], existing["n_forecasts"]) if existing else (0.0, 0)

        side, price, f = wealth_kelly_fraction(r["p_yes"], r["p_market_at_ts"], config)
        delta = log_wealth_delta(side, price, f, r["payout_yes"])
        cum, n = state[key]
        cum, n = cum + delta, n + 1
        state[key] = (cum, n)

        conn.execute(
            """INSERT INTO wealth_ledger (model_id, category, condition_id, event_id,
                                         forecast_id, ts, kelly_fraction, log_wealth_delta,
                                         cum_log_wealth, n_forecasts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["model_id"], category, r["condition_id"], r["event_id"],
             r["forecast_id"], r["resolved_ts"], f, delta, cum, n),
        )
        models_touched.add(r["model_id"])
    conn.commit()
    log.info("wealth ledger updated",
             extra={"ctx": {"rows_added": len(rows), "models": len(models_touched)}})
    return {"rows_added": len(rows), "models": sorted(models_touched)}
