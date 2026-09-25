"""`lab learn` -- the monthly batch learning loop (guardrails 14/15, brief section 6).

Everything here is batch-and-versioned and, above all, *safe*:

* **Dry-run by default.** `run_learn(..., apply=False)` computes every proposed
  change and reports a diff, writing nothing. Persistence + promotion happen
  only under `apply=True` (the CLI `--apply` flag).
* **Walk-forward only.** Refit paths split train/validation and raise if a
  validation window is missing (`assert_walk_forward`).
* **Bounded step.** No live parameter moves more than `max_step_pct` per cycle.
* **CI-gated promotion.** A challenger is promoted only when it beats the
  champion with a cluster-bootstrap CI excluding zero (reusing eval/scoring.py),
  never on a point estimate.
* **Automatic rollback.** Before refitting, a circuit breaker checks whether a
  freshly promoted champion has degraded over its trailing window and reverts it.
* **Kill switch.** Refuses to run while `data/PAUSE` exists.

No code path adjusts any model in response to an individual outcome, and no LLM
call ever touches resolved history (guardrail 15): M3 refits are arithmetic over
evidence objects already frozen in `evidence_runs`.
"""

from __future__ import annotations

import itertools
import json
import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np

from lab.eval.anytime import confidence_sequence
from lab.eval.scoring import cluster_bootstrap_ci
from lab.learn import registry
from lab.learn.refit import (
    assert_walk_forward,
    bound_step,
    bucket_for_days,
    fit_m1_curves,
    fit_m1_hier_curves,
    fit_m2_baserates,
    fit_m5_macro_sds,
    load_active_artifact,
    logit,
    save_artifact,
    sigmoid,
)
from lab.models.m1_hier import apply_hier_curve
from lab.news.aggregate import aggregate
from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

DEFAULT_PROMOTION_MIN_N = 200


# --- config helpers -------------------------------------------------------

def _learn_cfg(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("learn", {}) or {}


def _max_step_pct(config: dict[str, Any]) -> float:
    return float(_learn_cfg(config).get("max_step_pct", 0.20))


def _rollback_window(config: dict[str, Any]) -> int:
    return int(_learn_cfg(config).get("rollback_window", 50))


def _promotion_min_n(config: dict[str, Any]) -> int:
    return int(_learn_cfg(config).get("promotion_min_n", DEFAULT_PROMOTION_MIN_N))


def _iterations(config: dict[str, Any]) -> int:
    return int(config.get("eval", {}).get("bootstrap_iterations", 2000))


def _cs_alpha(config: dict[str, Any]) -> float:
    return float(config.get("eval", {}).get("confidence_sequence", {}).get("alpha", 0.05))


def _load_artifact_file(artifact_path: str) -> dict[str, Any] | None:
    p = Path(artifact_path)
    p = p if p.is_absolute() else PROJECT_ROOT / p
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- CI-gated promotion (brief section 6) ---------------------------------

def passes_ci_gate(
    champ_brier: np.ndarray,
    chall_brier: np.ndarray,
    condition_ids: np.ndarray,
    *,
    iterations: int,
    min_n: int,
    alpha: float = 0.05,
) -> tuple[bool, dict[str, Any]]:
    """True iff the challenger beats the champion with the anytime-valid
    confidence sequence excluding zero (brief section 7, Phase 11: "promotions,
    rollbacks... must cite the confidence sequence; the fixed-n CI stays for
    monthly descriptive snapshots"). Despite the parameter name, `condition_ids`
    is an opaque cluster key as of Phase 11 -- event_id where a cross-venue
    match is confirmed, else condition_id.

    diffs = champ_brier - chall_brier ; positive = challenger better (lower Brier).
    The classical cluster-bootstrap CI is still computed and returned in `stats`
    for the descriptive snapshot, but no longer decides promotion/rollback.
    """
    cids = np.asarray(condition_ids)
    diffs = np.asarray(champ_brier, dtype=float) - np.asarray(chall_brier, dtype=float)
    n_markets = int(len(np.unique(cids))) if len(cids) else 0
    stats: dict[str, Any] = {"n_markets": n_markets,
                             "skill": float(np.mean(diffs)) if len(diffs) else 0.0}
    if len(diffs) == 0 or n_markets < min_n:
        stats["reason"] = "insufficient_n"
        return False, stats
    lo, hi = cluster_bootstrap_ci(diffs, cids, iterations=iterations)
    stats["ci_lo"], stats["ci_hi"] = lo, hi  # descriptive only (brief section 7)
    per_cluster_diffs = np.array([float(np.mean(diffs[cids == c])) for c in np.unique(cids)])
    cs = confidence_sequence(per_cluster_diffs, alpha=alpha)
    stats["cs_lo"], stats["cs_hi"] = cs.lo, cs.hi
    return (cs.lo > 0), stats


# --- per-model prediction replays (arithmetic only, no LLM) ----------------

def _m1_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    fit = artifact.get("buckets", {}).get(bucket_for_days(row["days_to_resolution"]))
    if not fit or "alpha" not in fit:
        return float(row["p_market"])
    return float(sigmoid(fit["alpha"] + fit["beta"] * logit(row["p_market"])))


def _m1_hier_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    bucket = bucket_for_days(row["days_to_resolution"])
    if bucket is None or bucket not in artifact.get("buckets", {}):
        return float(row["p_market"])
    return apply_hier_curve(artifact, row.get("venue") or "polymarket", bucket, row["p_market"])


def _m3_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    p = artifact.get("params") or {}
    if not p:
        return float(row["p_market"])
    return float(aggregate(row["p_market"], row["items"], row["ts"],
                           k=p["k"], tau_days=p["tau_days"], max_shift=p["max_shift"])["p_yes"])


def _extremization_predict(artifact: dict[str, Any], row: dict[str, Any], category_key: str) -> float:
    """Shared replay logic for m4_extremization/m7_extremization: apply the
    fitted, correlation-discounted exponent to the pool's raw logit. `row`
    already carries `p_pooled` -- today's un-extremized pool -- so no member
    reconstruction is needed."""
    from lab.learn.pooling import discount_extremization_exponent

    spec = artifact.get("categories", {}).get(category_key)
    if not spec:
        return float(row["p_pooled"])
    a_eff = discount_extremization_exponent(
        spec["a"], n=max(1, int(round(spec.get("n_members_at_fit", 1)))),
        rho_bar=spec.get("rho_bar", 0.0),
    )
    return float(sigmoid(a_eff * logit(row["p_pooled"])))


def _m4_extremization_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    return _extremization_predict(artifact, row, row.get("category") or "unknown")


def _m7_extremization_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    return _extremization_predict(artifact, row, "_all")


def _m4_weights_predict(artifact: dict[str, Any], row: dict[str, Any]) -> float:
    """Replays M4Ensemble.forecast()'s pooling logic (minus extremization --
    a separate, independently-versioned step) using `row["members"]`
    (m4_pool_rows) and the given weights artifact. Used to CI-gate the MWU
    challenger against the incumbent m4_weights (Phase 14.1)."""
    members = sorted(row["members"])
    cat_weights = artifact.get("categories", {}).get(row["category"], {}).get("weights")
    if cat_weights:
        w = np.array([cat_weights.get(m, 0.0) for m in members])
        if w.sum() <= 0:
            w = np.ones(len(members))
    else:
        w = np.ones(len(members))
    w = w / w.sum()
    return float(sigmoid(np.dot(w, [logit(row["members"][m]) for m in members])))


# --- resolved-row loaders (carry condition_id for clustering) --------------

def m1_resolved_rows(conn, limit: int | None = None) -> list[dict]:
    """(condition_id, p_market, outcome, days_to_resolution) from resolved rows.

    Ordered newest-resolved first so a trailing window is a simple head slice.
    """
    rows = [dict(r) for r in conn.execute(
        """
        SELECT f.condition_id, f.p_market_at_ts AS p_market, r.payout_yes AS outcome,
               r.resolved_ts, m.event_id AS event_id, m.venue AS venue,
               -- STATED horizon at forecast time (PAP 9.31), not realized:
               -- the curve is applied on the stated one, and fitting on the
               -- realized one both skews train against serve and conditions
               -- on the outcome ("by date" markets resolve early when YES).
               COALESCE(f.days_to_resolution_at_ts,
                        julianday(m.end_date_iso) - julianday(f.ts)) AS days_to_resolution
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = 'm0_market'
        ORDER BY r.resolved_ts DESC
        """
    )]
    rows = [r for r in rows if r["days_to_resolution"] and r["days_to_resolution"] > 0]
    return rows[:limit] if limit else rows


def m3_resolved_rows(conn, limit: int | None = None) -> list[dict]:
    """Resolved M3 forecasts + stored dossiers, newest-resolved first.

    GLOB 'm3_evidence*' matches both the Anthropic champion id ('m3_evidence')
    and provider/prompt challenger ids ('m3_evidence@deepseek', 'm3_evidence@v2')
    -- the earlier exact-match query silently returned zero rows once the active
    provider was DeepSeek, so the aggregator never refit.
    """
    out: list[dict] = []
    for r in conn.execute(
        """
        SELECT f.condition_id, f.ts, f.p_market_at_ts AS p_market, r.payout_yes AS outcome,
               r.resolved_ts, e.dossier_json, m.event_id AS event_id
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN evidence_runs e ON e.id = f.evidence_run_id
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id GLOB 'm3_evidence*'
        ORDER BY r.resolved_ts DESC
        """
    ):
        try:
            dossier = json.loads(r["dossier_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        out.append({
            "condition_id": r["condition_id"], "ts": r["ts"], "p_market": r["p_market"],
            "outcome": r["outcome"], "resolved_ts": r["resolved_ts"], "event_id": r["event_id"],
            "items": dossier.get("evidence_items", []),
        })
    return out[:limit] if limit else out


def m4_resolved_rows(conn, limit: int | None = None) -> list[dict]:
    """Resolved m4_ensemble forecasts (Phase 13). `p_yes` here IS today's raw,
    un-extremized pool -- extremization doesn't exist in what's stored yet --
    so it doubles directly as the `p_pooled` fit_extremization_exponent/
    _m4_extremization_predict expect. Newest-resolved first."""
    rows = [dict(r) for r in conn.execute(
        """
        SELECT f.condition_id, f.p_yes AS p_pooled, r.payout_yes AS outcome,
               r.resolved_ts, m.category AS category, m.event_id AS event_id
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = 'm4_ensemble'
        ORDER BY r.resolved_ts DESC
        """
    )]
    return rows[:limit] if limit else rows


def m7_resolved_rows(conn, limit: int | None = None) -> list[dict]:
    """Resolved m7_crossvenue forecasts (Phase 13), same shape as m4_resolved_rows."""
    rows = [dict(r) for r in conn.execute(
        """
        SELECT f.condition_id, f.p_yes AS p_pooled, r.payout_yes AS outcome,
               r.resolved_ts, m.category AS category, m.event_id AS event_id
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = 'm7_crossvenue'
        ORDER BY r.resolved_ts DESC
        """
    )]
    return rows[:limit] if limit else rows


def m4_pool_rows(conn) -> list[dict]:
    """Resolved markets with an m4_ensemble forecast, joined back to each
    POOLABLE member's own contemporaneous p_yes (their latest forecast on or
    before m4_ensemble's own last forecast date) -- enables replaying the
    pool under alternative weight artifacts (MWU challenger vs incumbent,
    Phase 14.1). One row per resolved market (the pool's LAST pre-resolution
    state, not one row per historical daily snapshot -- this replays "would
    this weight vector have produced a better final decision," which only
    needs each market's last pool composition, not the full multi-day
    history Phase 11's skill scoring uses for a different purpose)."""
    from lab.models.m4_ensemble import POOLABLE

    m4_latest = conn.execute(
        """
        SELECT f.condition_id, MAX(f.ts) AS m4_ts, m.category AS category,
               m.event_id AS event_id, r.payout_yes AS outcome, r.resolved_ts
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id AND r.disputed = 0
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = 'm4_ensemble'
        GROUP BY f.condition_id
        """
    ).fetchall()

    placeholders = ",".join("?" for _ in POOLABLE)
    out: list[dict] = []
    for row in m4_latest:
        member_rows = conn.execute(
            f"""
            SELECT f.model_id, f.p_yes FROM forecasts f
            JOIN (SELECT model_id, MAX(ts) AS ts FROM forecasts
                  WHERE condition_id = ? AND model_id IN ({placeholders}) AND ts <= ?
                  GROUP BY model_id) latest
            ON latest.model_id = f.model_id AND latest.ts = f.ts
            WHERE f.condition_id = ?
            """,
            (row["condition_id"], *POOLABLE, row["m4_ts"], row["condition_id"]),
        ).fetchall()
        members = {r["model_id"]: r["p_yes"] for r in member_rows}
        if len(members) < 2:
            continue
        out.append({
            "condition_id": row["condition_id"], "category": row["category"] or "unknown",
            "event_id": row["event_id"], "outcome": row["outcome"],
            "resolved_ts": row["resolved_ts"], "members": members,
        })
    return out


# --- bounded fits ----------------------------------------------------------

def _bound_m1_curves(old: dict | None, new: dict, pct: float) -> dict:
    """Clamp each bucket's alpha/beta move; leave diagnostics (n, nll, iso) alone."""
    if not old:
        return new
    old_b = old.get("buckets", {})
    for name, fit in new.get("buckets", {}).items():
        ob = old_b.get(name)
        if ob and "alpha" in ob and "beta" in ob and "alpha" in fit and "beta" in fit:
            bounded = bound_step({"alpha": ob["alpha"], "beta": ob["beta"]},
                                 {"alpha": fit["alpha"], "beta": fit["beta"]}, pct)
            fit["alpha"], fit["beta"] = bounded["alpha"], bounded["beta"]
    return new


def _bound_m3_params(old: dict | None, new: dict, pct: float) -> dict:
    if old and old.get("params") and new.get("params"):
        new["params"] = bound_step(old["params"], new["params"], pct)
    return new


def _bound_m4_weights(old: dict | None, new: dict, pct: float) -> dict:
    if not old:
        return new
    old_c = old.get("categories", {})
    for cat, spec in new.get("categories", {}).items():
        oc = old_c.get(cat)
        if oc and "weights" in oc and "weights" in spec:
            spec["weights"] = bound_step(oc["weights"], spec["weights"], pct)
    return new


def _bound_extremization(old: dict | None, new: dict, pct: float) -> dict:
    """Clamp each category's `a` move (Phase 13). Shared by m4_extremization
    and m7_extremization -- both use the same {"categories": {key: {"a": ...}}}
    shape (m7's is a single "_all" pseudo-category)."""
    if not old:
        return new
    old_c = old.get("categories", {})
    for cat, spec in new.get("categories", {}).items():
        oc = old_c.get(cat)
        if oc and "a" in oc and "a" in spec:
            spec["a"] = bound_step({"a": oc["a"]}, {"a": spec["a"]}, pct)["a"]
    return new


def _bound_m5_sds(old: dict | None, new: dict, pct: float) -> dict:
    """Clamp each series' sd move; leave n/val_nll diagnostics alone."""
    if not old:
        return new
    old_s = old.get("series", {})
    for name, fit in new.get("series", {}).items():
        os_ = old_s.get(name)
        if os_ and "sd" in os_ and "sd" in fit:
            fit["sd"] = bound_step(os_["sd"], fit["sd"], pct)
    return new


def fit_m1_walk_forward(train: list[dict], validation: list[dict]) -> dict[str, Any]:
    """Refit path with a structural walk-forward guard (brief section 6)."""
    assert_walk_forward(train, validation)
    return fit_m1_curves(train)


def _bound_m1_hier_curves(old: dict | None, new: dict, pct: float) -> dict:
    """Clamp each bucket's global alpha/beta AND each venue's offset move;
    leave diagnostics (n, nll) alone. Mirrors _bound_m1_curves, extended to
    also walk each venue's offset dict (Phase 12, M1.x)."""
    if not old:
        return new
    old_buckets = old.get("buckets", {})
    for name, fit in new.get("buckets", {}).items():
        ob = old_buckets.get(name)
        if not ob:
            continue
        og, ng = ob.get("global"), fit.get("global")
        if og and ng and "alpha" in og and "beta" in og:
            bounded = bound_step({"alpha": og["alpha"], "beta": og["beta"]},
                                 {"alpha": ng["alpha"], "beta": ng["beta"]}, pct)
            ng["alpha"], ng["beta"] = bounded["alpha"], bounded["beta"]
        old_venues = ob.get("venues", {})
        for venue, vfit in fit.get("venues", {}).items():
            ovfit = old_venues.get(venue)
            if ovfit and "alpha_offset" in ovfit and "beta_offset" in ovfit:
                bounded = bound_step(
                    {"alpha_offset": ovfit["alpha_offset"], "beta_offset": ovfit["beta_offset"]},
                    {"alpha_offset": vfit["alpha_offset"], "beta_offset": vfit["beta_offset"]}, pct)
                vfit["alpha_offset"], vfit["beta_offset"] = bounded["alpha_offset"], bounded["beta_offset"]
    return new


def fit_m1_hier_walk_forward(train: list[dict], validation: list[dict]) -> dict[str, Any]:
    """Refit path with a structural walk-forward guard (brief section 6),
    mirrors fit_m1_walk_forward for the hierarchical multi-venue curve."""
    assert_walk_forward(train, validation)
    return fit_m1_hier_curves(train)


# --- challenger registration + promotion decision -------------------------

def _process_challenger(
    conn,
    config: dict[str, Any],
    model_key: str,
    artifact: dict[str, Any],
    holdout: list[dict],
    predict_fn: Callable[[dict, dict], float] | None,
    *,
    apply: bool,
    auto: bool = False,
    fit_window: tuple[str | None, str | None] = (None, None),
) -> dict[str, Any]:
    """Decide promotion for `artifact`; persist + register only under `apply`.

    `auto=True` promotes descriptive/reward-signal artifacts (M2 base rates, M4
    weights) without a CI gate. Otherwise the challenger must beat the champion
    on `holdout` with a CI excluding zero and n >= promotion_min_n.
    """
    champ = load_active_artifact(config, model_key)
    decision: dict[str, Any] = {"model": model_key, "promoted": False}

    if champ is None:
        decision.update(promoted=True, reason="first")
    elif auto:
        decision.update(promoted=True, reason="auto")
    elif not holdout or predict_fn is None:
        decision["reason"] = "no_holdout"
    else:
        cids = np.array([r.get("event_id") or r["condition_id"] for r in holdout])
        champ_b = np.array([(predict_fn(champ, r) - r["outcome"]) ** 2 for r in holdout])
        chall_b = np.array([(predict_fn(artifact, r) - r["outcome"]) ** 2 for r in holdout])
        ok, stats = passes_ci_gate(champ_b, chall_b, cids,
                                   iterations=_iterations(config),
                                   min_n=_promotion_min_n(config),
                                   alpha=_cs_alpha(config))
        decision.update(promoted=ok, **stats)

    if apply:
        path = save_artifact(config, model_key, artifact, promote=False)
        vid = registry.register_version(conn, config, model_key, path, fit_window=fit_window)
        decision["version_id"] = vid
        decision["artifact"] = path.name
        if decision["promoted"]:
            registry.set_active(conn, config, model_key, vid)
    return decision


# --- M1 / M2 / M4 scheduled refits ----------------------------------------

def refit_statistical_models(conn, config: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    """M1 curves (CI-gated), M2 base rates + M4 weights + M5 macro sd (auto, reward signal)."""
    import os

    from lab.learn.bootstrap import load_observations
    from lab.models.m4_ensemble import fit_m4_weights

    results: dict[str, Any] = {}
    pct = _max_step_pct(config)

    # Kept as a DataFrame, deliberately: `.to_dicts()` on this file (1,967,376
    # rows) cost ~1.8GB RSS and was OOM-killed on the production host, while
    # both fitters only ever read fixed columns as arrays (see refit._obs_columns).
    try:
        obs = load_observations(config)
    except FileNotFoundError:
        obs = []
    live = m1_resolved_rows(conn)  # walk-forward holdout: the lab's own forward record
    n_obs = len(obs)

    if n_obs and live:
        m1 = fit_m1_walk_forward(train=obs, validation=live)
        m1 = _bound_m1_curves(load_active_artifact(config, "m1_curves"), m1, pct)
        results["m1_curves"] = _process_challenger(
            conn, config, "m1_curves", m1, live, _m1_predict, apply=apply)
        results["m1_curves"].update(n_train=n_obs, n_holdout=len(live))
    else:
        results["m1_curves"] = {"skipped": "insufficient_data",
                                "n_train": n_obs, "n_holdout": len(live)}

    # M1.x hierarchical recalibration (Phase 12): same train/validation split as
    # m1_curves above -- the bootstrap `obs` carries no venue tag and defaults to
    # 'polymarket' (fit_m1_hier_curves' own convention), while `live` (m0_market's
    # forward record) already spans every forecastable venue via its markets join.
    # Registers under its OWN artifact key ('m1_hier_curves'), so the very first
    # fit auto-promotes (no incumbent to touch) and every later refit is CI-gated
    # against its own prior version -- m1_curves' active pointer is never touched.
    if n_obs and live:
        m1h = fit_m1_hier_walk_forward(train=obs, validation=live)
        m1h = _bound_m1_hier_curves(load_active_artifact(config, "m1_hier_curves"), m1h, pct)
        results["m1_hier_curves"] = _process_challenger(
            conn, config, "m1_hier_curves", m1h, live, _m1_hier_predict, apply=apply)
        results["m1_hier_curves"].update(n_train=n_obs, n_holdout=len(live))
    else:
        results["m1_hier_curves"] = {"skipped": "insufficient_data",
                                     "n_train": n_obs, "n_holdout": len(live)}

    # First occurrence per condition_id wins, same as the row loop this replaces
    # -- done in polars so the 2M-row training set never becomes Python objects.
    per_market: dict[str, dict] = {}
    if n_obs:
        first = obs.unique(subset=["condition_id"], keep="first", maintain_order=True)
        for cid, cat, outcome in zip(first.get_column("condition_id"),
                                     first.get_column("category"),
                                     first.get_column("outcome")):
            if cid:
                per_market[cid] = {"category": cat if cat is not None else "unknown",
                                   "outcome": outcome}
    for r in conn.execute(
        """SELECT m.condition_id, m.category, r.payout_yes AS outcome
           FROM resolutions r JOIN markets m ON m.condition_id = r.condition_id
           WHERE r.disputed = 0"""
    ).fetchall():
        per_market[r["condition_id"]] = {"category": r["category"], "outcome": r["outcome"]}
    if per_market:
        m2 = fit_m2_baserates(list(per_market.values()))
        results["m2_baserates"] = _process_challenger(
            conn, config, "m2_baserates", m2, [], None, apply=apply, auto=True)

    m4 = fit_m4_weights(conn, config)
    if m4["categories"]:
        m4 = _bound_m4_weights(load_active_artifact(config, "m4_weights"), m4, pct)
        results["m4_weights"] = _process_challenger(
            conn, config, "m4_weights", m4, [], None, apply=apply, auto=True)
        results["m4_weights"]["categories"] = list(m4["categories"])

    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        results["m5_macro_sd"] = {"skipped": "no_fred_api_key"}
    else:
        m5cfg = _learn_cfg(config).get("m5_macro", {})
        m5 = fit_m5_macro_sds(fred_key, min_quarters=m5cfg.get("min_quarters", 12),
                              validation_quarters=m5cfg.get("validation_quarters", 8))
        if m5["series"]:
            m5 = _bound_m5_sds(load_active_artifact(config, "m5_macro_sd"), m5, pct)
            results["m5_macro_sd"] = _process_challenger(
                conn, config, "m5_macro_sd", m5, [], None, apply=apply, auto=True)
            results["m5_macro_sd"]["series"] = list(m5["series"])
        else:
            results["m5_macro_sd"] = {"skipped": "insufficient_quarters"}
    return results


# --- extremization exponent fit (Phase 13) ---------------------------------

def fit_m4_extremization(conn, config: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    """Per-category extremization exponent for M4's pool (Phase 13, CLAUDE.md
    M4/M7 extremization). Walk-forward per category: the oldest 80% of each
    category's resolved m4_ensemble forecasts fits the grid search (brief
    section 6), the newest 20% is held out; all categories' holdouts are
    combined into one flat CI-gate validation set (same discipline as
    fit_m3_aggregator, adapted for a per-category artifact)."""
    from lab.learn.pooling import estimate_rho_bar_m4, fit_extremization_exponent
    from lab.models.m4_ensemble import POOLABLE

    min_n = int(_learn_cfg(config).get("m4_extremization_min_n", 60))
    rows = list(reversed(m4_resolved_rows(conn)))  # oldest-first for walk-forward
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"] or "unknown", []).append(r)

    rho_by_cat = estimate_rho_bar_m4(conn, config, model_ids=POOLABLE)
    artifact: dict[str, Any] = {"kind": "m4_extremization", "fitted_at": now_utc_iso(), "categories": {}}
    combined_validation: list[dict] = []
    for cat, cat_rows in by_cat.items():
        if len(cat_rows) < min_n:
            continue
        split = int(len(cat_rows) * 0.8)
        train, validation = cat_rows[:split], cat_rows[split:]
        fit = fit_extremization_exponent(train, validation)  # asserts walk-forward internally
        artifact["categories"][cat] = {
            "a": fit["a"], "rho_bar": rho_by_cat.get(cat, 0.0),
            "n_members_at_fit": len(POOLABLE),
            "n_train": fit["n_train"], "n_validation": fit["n_validation"],
        }
        combined_validation.extend(validation)

    if not artifact["categories"]:
        return {"skipped": "insufficient_data", "n_resolved": len(rows)}

    pct = _max_step_pct(config)
    artifact = _bound_extremization(load_active_artifact(config, "m4_extremization"), artifact, pct)
    decision = _process_challenger(
        conn, config, "m4_extremization", artifact, combined_validation,
        _m4_extremization_predict, apply=apply)
    decision.update(categories=list(artifact["categories"]), n_resolved=len(rows))
    return decision


def fit_m7_extremization(conn, config: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    """Single overall extremization exponent for M7's external pool (Phase
    13) -- confirmed cross-venue pairs are few (Phase 9's own acceptance bar
    was only >=5), so a per-category split would starve every cell; one
    pooled fit instead, keyed "_all"."""
    from lab.learn.pooling import estimate_rho_bar_m7, fit_extremization_exponent
    from lab.models.m7_crossvenue import confirmed_by_condition, load_markets_map
    from lab.store.snapshots import SnapshotStore

    min_n = int(_learn_cfg(config).get("m7_extremization_min_n", 60))
    rows = list(reversed(m7_resolved_rows(conn)))  # oldest-first
    if len(rows) < min_n:
        return {"skipped": "insufficient_data", "n_resolved": len(rows)}

    split = int(len(rows) * 0.8)
    train, validation = rows[:split], rows[split:]
    fit = fit_extremization_exponent(train, validation)  # asserts walk-forward internally

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    rho = estimate_rho_bar_m7(conn, store, config)
    by_cid = confirmed_by_condition(load_markets_map())
    n_members = (sum(len(v) for v in by_cid.values()) / len(by_cid)) if by_cid else 2.0

    artifact: dict[str, Any] = {
        "kind": "m7_extremization", "fitted_at": now_utc_iso(),
        "categories": {"_all": {
            "a": fit["a"], "rho_bar": rho if rho is not None else 0.0,
            "n_members_at_fit": n_members,
            "n_train": fit["n_train"], "n_validation": fit["n_validation"],
        }},
    }
    pct = _max_step_pct(config)
    artifact = _bound_extremization(load_active_artifact(config, "m7_extremization"), artifact, pct)
    decision = _process_challenger(
        conn, config, "m7_extremization", artifact, validation, _m7_extremization_predict, apply=apply)
    decision.update(n_resolved=len(rows))
    return decision


# --- M3 aggregator walk-forward fit ----------------------------------------

M3_GRID = {
    "k": [0.08, 0.15, 0.25],
    "tau_days": [3.0, 5.0, 10.0],
    "max_shift": [0.5, 0.8, 1.2],
}


def resolved_m3_runs(conn) -> list[dict]:
    """Resolved M3 runs oldest-first (walk-forward order)."""
    return list(reversed(m3_resolved_rows(conn)))


def replay_brier(runs: list[dict], k: float, tau: float, cap: float) -> float:
    scores = []
    for run in runs:
        p = aggregate(run["p_market"], run["items"], run["ts"],
                      k=k, tau_days=tau, max_shift=cap)["p_yes"]
        scores.append((p - run["outcome"]) ** 2)
    return float(np.mean(scores)) if scores else float("inf")


def fit_m3_aggregator(conn, config: dict[str, Any], *, apply: bool = False) -> dict[str, Any] | None:
    """Walk-forward grid search over (k, tau, cap); gated on min resolved runs.

    Fits only on the training split; the newest split is the out-of-sample
    holdout used for CI-gated promotion (no leakage from validation into the fit).
    """
    runs = resolved_m3_runs(conn)
    min_n = config["learn"]["m3_min_resolved"]
    if len(runs) < min_n:
        log.info("m3 aggregator fit skipped",
                 extra={"ctx": {"resolved": len(runs), "required": min_n}})
        return None

    split = int(len(runs) * 0.8)
    train, validation = runs[:split], runs[split:]
    assert_walk_forward(train, validation)

    n_folds = 5
    fold_size = max(1, len(train) // n_folds)
    best_params, best_score = None, float("inf")
    for k, tau, cap in itertools.product(M3_GRID["k"], M3_GRID["tau_days"], M3_GRID["max_shift"]):
        fold_scores = [
            replay_brier(train[i * fold_size:(i + 1) * fold_size], k, tau, cap)
            for i in range(1, n_folds)  # skip earliest fold as burn-in
        ]
        score = float(np.mean(fold_scores))
        if score < best_score:
            best_score, best_params = score, {"k": k, "tau_days": tau, "max_shift": cap}

    artifact = {"kind": "m3_params", "fitted_at": now_utc_iso(),
                "params": best_params, "walk_forward_brier": best_score, "n_runs": len(runs)}
    artifact = _bound_m3_params(load_active_artifact(config, "m3_params"), artifact, _max_step_pct(config))

    decision = _process_challenger(
        conn, config, "m3_params", artifact, validation, _m3_predict, apply=apply,
        fit_window=(train[0]["ts"], train[-1]["ts"]))
    decision.update(params=artifact["params"], n_runs=len(runs))
    return decision


# --- rollback circuit breaker (brief section 6) ---------------------------

def run_rollback_checks(conn, config: dict[str, Any], *, apply: bool) -> list[dict[str, Any]]:
    """Revert a freshly promoted champion that has degraded over its trailing window.

    For each model with a promoted active version and a prior promotable version,
    replay both on the last `rollback_window` resolved forecasts. If the prior
    version beats the current one with a CI excluding zero, roll back.
    """
    window = _rollback_window(config)
    checks = [("m1_curves", _m1_predict, m1_resolved_rows),
              ("m3_params", _m3_predict, m3_resolved_rows),
              ("m4_extremization", _m4_extremization_predict, m4_resolved_rows),
              ("m7_extremization", _m7_extremization_predict, m7_resolved_rows)]
    out: list[dict[str, Any]] = []
    for model_key, predict_fn, rows_fn in checks:
        active = registry.active_version(conn, model_key)
        if not active or not active.get("promoted_ts"):
            continue
        prev = registry.previous_promotable(conn, model_key, exclude_id=active["id"])
        if not prev:
            continue
        holdout = rows_fn(conn, limit=window)
        if len(holdout) < 2:
            continue
        active_art = _load_artifact_file(active["artifact_path"])
        prev_art = _load_artifact_file(prev["artifact_path"])
        if active_art is None or prev_art is None:
            continue
        cids = np.array([r.get("event_id") or r["condition_id"] for r in holdout])
        active_b = np.array([(predict_fn(active_art, r) - r["outcome"]) ** 2 for r in holdout])
        prev_b = np.array([(predict_fn(prev_art, r) - r["outcome"]) ** 2 for r in holdout])
        # champ=active, challenger=prev: positive skill => prev beats current => degraded.
        degraded, stats = passes_ci_gate(active_b, prev_b, cids,
                                         iterations=_iterations(config), min_n=2,
                                         alpha=_cs_alpha(config))
        entry: dict[str, Any] = {"model": model_key, "degraded": degraded, "window": len(holdout),
                                 **stats}
        if degraded and apply:
            restored = registry.rollback(conn, config, model_key, reason="rollback")
            entry["rolled_back_to"] = restored["version_tag"] if restored else None
        out.append(entry)
    return out


# --- top-level ------------------------------------------------------------

def run_learn(conn, config: dict[str, Any], llm=None, *, apply: bool = False) -> dict[str, Any]:
    """Monthly loop. Dry-run by default; writes to model_versions only under apply.

    Refuses to run while data/PAUSE exists (the same kill switch the collector
    respects). Order: rollback circuit breaker -> refits -> M3 aggregator ->
    extremization exponents (Phase 13) -> post-mortems (post-mortems run only
    on apply, since they cost LLM budget).
    """
    from lab.collect.runner import is_paused

    if is_paused(config):
        log.warning("learn: PAUSE file present -- refusing to run")
        return {"ts": now_utc_iso(), "skipped": "paused"}

    summary: dict[str, Any] = {"ts": now_utc_iso(), "apply": apply}
    summary["rollbacks"] = run_rollback_checks(conn, config, apply=apply)
    summary["refits"] = refit_statistical_models(conn, config, apply=apply)
    summary["m3_aggregator"] = fit_m3_aggregator(conn, config, apply=apply)
    summary["m4_extremization"] = fit_m4_extremization(conn, config, apply=apply)
    summary["m7_extremization"] = fit_m7_extremization(conn, config, apply=apply)
    if apply:
        from lab.learn.postmortem import run_postmortems
        summary["postmortems"] = run_postmortems(conn, config, llm)
    else:
        summary["postmortems"] = "skipped (dry-run)"
    log.info("learn complete", extra={"ctx": {"apply": apply,
                                              "rollbacks": summary["rollbacks"]}})
    return summary
