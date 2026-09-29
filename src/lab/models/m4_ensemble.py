"""M4 -- log-odds weighted pool of the other models' forecasts.

Weights are fit per category on resolved forecasts (inverse-Brier softmax
over a rolling window) and stored as a versioned artifact; equal weights
until a category has at least `learn.m4_min_resolved_per_category` resolved
samples. M4 pools each market's latest same-day forecasts from the ledger,
so it runs after the other models.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from lab.learn.pooling import (
    clamp_and_renormalize_weights,
    discount_extremization_exponent,
    extremize_logit,
)
from lab.learn.refit import logit, sigmoid
from lab.models.base import ForecastResult, MarketState, clamp_p

log = logging.getLogger(__name__)

POOLABLE = ("m0_market", "m1_debiased", "m2_baserate", "m3_evidence",
            "m5_nowcast", "m6_consistency", "m7_crossvenue")
# Fallback only. The live value is `learn.m4_min_resolved_per_category` in
# config.yaml, read by fit_m4_weights below and by economy/mwu.py's challenger
# fit; this constant applies solely when that key is absent from the config.
# (Until 2026-07-25 the constant WAS the live value and the config key was
# dead -- the two could silently disagree, which is exactly the drift the
# paper draft's own S5.8 footnote flagged.)
MIN_RESOLVED_PER_CATEGORY = 100


def fit_m4_weights(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Per-category softmax(-brier) weights over resolved forecasts, floor/
    ceiling-clamped (Phase 14.1, v2.2 parity: "the same 2%/60% per-model
    floor and ceiling given to the MWU challenger... the incumbent shouldn't
    be less protected... than the challenger trying to unseat it")."""
    from lab.util import now_utc_iso

    learn_cfg = config.get("learn", {})
    floor = float(learn_cfg.get("m4_weight_floor", 0.02))
    ceiling = float(learn_cfg.get("m4_weight_ceiling", 0.60))
    min_resolved = int(learn_cfg.get("m4_min_resolved_per_category",
                                     MIN_RESOLVED_PER_CATEGORY))

    artifact: dict[str, Any] = {"kind": "m4_weights", "fitted_at": now_utc_iso(),
                                "categories": {}}
    # Censored at the stated end date like every resolved-row reader (PAP
    # 9.35): weights fit on an early-resolution sample favour whichever member
    # predicts YES higher on long-dated markets.
    from lab.eval.run import SCOREABLE_SQL

    rows = conn.execute(
        """
        SELECT m.category, f.model_id, AVG((f.p_yes - r.payout_yes)*(f.p_yes - r.payout_yes)) AS brier,
               COUNT(*) AS n
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id IN ({}) AND {}
        GROUP BY m.category, f.model_id
        """.format(",".join("?" for _ in POOLABLE), SCOREABLE_SQL),
        (*POOLABLE, now_utc_iso()),
    ).fetchall()
    by_cat: dict[str, dict[str, dict]] = {}
    for r in rows:
        # Same NULL-category guard as fit_m2_baserates. Latent rather than
        # live here -- an uncategorized group has never reached
        # `min_resolved`, so it was always dropped a line later -- but a None
        # key would otherwise reach json.dumps and land in the artifact as
        # the string "null", a category no market can ever match.
        if r["category"] is None:
            continue
        by_cat.setdefault(r["category"], {})[r["model_id"]] = {"brier": r["brier"], "n": r["n"]}
    for cat, models in by_cat.items():
        total_n = sum(v["n"] for v in models.values())
        if total_n < min_resolved:
            continue
        briers = np.array([models[m]["brier"] for m in sorted(models)])
        # Softmax over negative Brier: better models earn weight through
        # realized Brier and nothing else (temperature 0.05 ~ Brier scale).
        w = np.exp(-briers / 0.05)
        w = w / w.sum()
        weights = clamp_and_renormalize_weights(dict(zip(sorted(models), w.tolist())), floor, ceiling)
        artifact["categories"][cat] = {
            "weights": weights,
            "n_resolved": total_n,
        }
    return artifact


class M4Ensemble:
    model_id = "m4_ensemble"

    def __init__(self, conn, weights_artifact: dict[str, Any] | None,
                extremization_artifact: dict[str, Any] | None = None,
                pool_date: str | None = None) -> None:
        self.conn = conn
        # The UTC day (YYYY-MM-DD) whose forecasts this instance pools. None
        # keeps the original behaviour -- the wall-clock day at pool time --
        # which is correct for any caller running inside one day. The forecast
        # job passes its own run timestamp so that a bundle starting before
        # midnight and reaching the ensemble after it still pools the rows it
        # actually wrote, instead of pooling nothing and abstaining on every
        # market.
        self.pool_date = pool_date
        self.artifact = weights_artifact or {"categories": {}}
        # Phase 13: per-category extremization exponent, correlation-discounted
        # at pool time using the ACTUAL member count present (not the frozen
        # count from fit time -- discount_extremization_exponent's n_eff math
        # wants today's real pool size). No artifact yet -> a=1.0, identity.
        self.extremization_artifact = extremization_artifact or {"categories": {}}

    def _todays_pool(self, condition_id: str) -> dict[str, float]:
        rows = self.conn.execute(
            """
            SELECT f.model_id, f.p_yes FROM forecasts f
            JOIN (SELECT model_id, MAX(ts) AS ts FROM forecasts
                  WHERE condition_id = ? AND date(ts) = coalesce(?, date('now'))
                  GROUP BY model_id) latest
            ON latest.model_id = f.model_id AND latest.ts = f.ts
            WHERE f.condition_id = ? AND f.model_id IN ({})
            """.format(",".join("?" for _ in POOLABLE)),
            (condition_id, self.pool_date, condition_id, *POOLABLE),
        ).fetchall()
        return {r["model_id"]: r["p_yes"] for r in rows}

    def forecast(self, market: MarketState, context: dict[str, Any]) -> ForecastResult | None:
        pool = self._todays_pool(market.condition_id)
        if len(pool) < 2:  # pooling one model is just that model
            return None
        cat_weights = self.artifact.get("categories", {}).get(market.category, {}).get("weights")
        members = sorted(pool)
        if cat_weights:
            w = np.array([cat_weights.get(m, 0.0) for m in members])
            if w.sum() <= 0:
                w = np.ones(len(members))
        else:
            w = np.ones(len(members))  # equal weights until n >= 100
        w = w / w.sum()
        raw_logit = float(np.dot(w, [logit(pool[m]) for m in members]))

        ext_spec = self.extremization_artifact.get("categories", {}).get(market.category)
        a_raw = ext_spec["a"] if ext_spec else 1.0
        rho_bar = ext_spec.get("rho_bar", 0.0) if ext_spec else 0.0
        a_eff = discount_extremization_exponent(a_raw, n=len(members), rho_bar=rho_bar)
        pooled = float(sigmoid(extremize_logit(raw_logit, a_eff)))

        return ForecastResult(
            p_yes=clamp_p(pooled),
            meta={"members": members, "weights": w.tolist(),
                  "weighted": bool(cat_weights),
                  "extremization_a_eff": a_eff, "extremization_rho_bar": rho_bar,
                  "artifact_version": self.artifact.get("version")},
        )
