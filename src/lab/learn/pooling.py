"""Extremized, correlation-aware pooling (Phase 13, CLAUDE.md M4/M7 extremization).

A plain log-odds pool (M4's weighted average, M7's `pool_log_odds`) is known to
be systematically underconfident when its sources carry distinct information
(Satopää et al.; Neyman-Roughgarden) -- extremizing the pooled logit corrects
for this, but only to the degree the sources are actually independent. M4's
members mostly derive from the same underlying Polymarket price; M7's external
venues price the same real-world event. Extremizing as if `n` members were
independent would overstate confidence, so the fitted exponent `a` is
discounted by the correlation-adjusted effective source count `n_eff` before
it's ever applied.
"""

from __future__ import annotations

import itertools
import logging
from datetime import timedelta
from typing import Any

import numpy as np

from lab.learn.refit import assert_walk_forward, logit, sigmoid

log = logging.getLogger(__name__)

EXTREMIZATION_GRID: tuple[float, ...] = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5)


def effective_source_count(n: int, rho_bar: float) -> float:
    """n_eff = n / (1 + (n-1)*rho_bar) -- the standard poll-aggregation
    effective-N discount for correlated sources.

    rho_bar is clamped to [0, 1]: a negative correlation would push n_eff
    above n, which extremization has no use for here (the a<=2.5 cap already
    bounds the upside) -- this keeps n_eff's role as "fraction of full
    independence" clean. n_eff itself is clamped to [1, n].
    """
    if n <= 1:
        return float(max(n, 0))
    rho_bar = float(np.clip(rho_bar, 0.0, 1.0))
    n_eff = n / (1 + (n - 1) * rho_bar)
    return float(np.clip(n_eff, 1.0, n))


def discount_extremization_exponent(a_raw: float, n: int, rho_bar: float) -> float:
    """a_eff = 1 + (a_raw - 1) * (n_eff - 1)/(n - 1) for n > 1, else 1.0.

    a_raw=1.0 -> a_eff=1.0 always, regardless of n/rho_bar (acceptance
    criterion: "a=1.0 reproduces current pooling exactly"). rho_bar->1 (e.g. a
    duplicated source) drives n_eff->1, which drives a_eff->1.0 regardless of
    a_raw (acceptance criterion: duplication suppresses extremization).
    """
    if n <= 1:
        return 1.0
    n_eff = effective_source_count(n, rho_bar)
    frac = (n_eff - 1) / (n - 1)
    return float(1.0 + (a_raw - 1.0) * frac)


def extremize_logit(pooled_logit: float, a_eff: float) -> float:
    """a_eff * pooled_logit -- applied AFTER pooling (CLAUDE.md: "applied to
    the pooled logit"), so a_eff=1.0 is a bit-exact identity transform."""
    return float(a_eff * pooled_logit)


def fit_extremization_exponent(
    train: list[dict], validation: list[dict], grid: tuple[float, ...] = EXTREMIZATION_GRID
) -> dict[str, Any]:
    """Walk-forward grid search over the raw extremization exponent `a`
    (same shape as learn/loop.py's M3_GRID search): for each candidate a,
    replay Brier of sigmoid(a * logit(raw_pooled_p)) against train, pick the
    best, report its validation Brier too.

    train/validation: [{"p_pooled": raw pooled probability (pre-extremization,
    i.e. today's plain pool), "outcome": ...}, ...].
    """
    assert_walk_forward(train, validation)

    def _brier(rows: list[dict], a: float) -> float:
        p_pool = np.array([r["p_pooled"] for r in rows], dtype=float)
        y = np.array([r["outcome"] for r in rows], dtype=float)
        p_ext = sigmoid(a * logit(p_pool))
        return float(np.mean((p_ext - y) ** 2))

    best_a, best_score = 1.0, float("inf")
    for a in grid:
        score = _brier(train, a)
        if score < best_score:
            best_a, best_score = a, score

    return {
        "a": float(best_a),
        "train_brier": best_score,
        "validation_brier": _brier(validation, best_a),
        "n_train": len(train),
        "n_validation": len(validation),
    }


# --- weight floor/ceiling + correlation clustering (Phase 14.1) ------------

def clamp_and_renormalize_weights(w: dict[str, float], floor: float, ceiling: float,
                                  max_iter: int = 200) -> dict[str, float]:
    """Iteratively clamp each weight to [floor, ceiling] and renormalize to
    sum 1, repeating until stable. Small pools (<=8 models) converge in a
    handful of iterations. Assumes floor*len(w) <= 1 <= ceiling*len(w) --
    feasible for the brief's 2%/60% defaults at realistic pool sizes.

    rtol=0 in the convergence check is deliberate: the default rtol=1e-5
    would call this "converged" while a value clipped to the ceiling is
    still renormalized back to ~1e-5 relative *above* the ceiling on every
    remaining iteration -- a small residual that would otherwise never
    shrink away. A final explicit clip guarantees the hard bound regardless.
    """
    keys = list(w)
    n = len(keys)
    if n == 0:
        return {}
    vals = np.array([w[k] for k in keys], dtype=float)
    total = vals.sum()
    vals = vals / total if total > 0 else np.full(n, 1.0 / n)
    for _ in range(max_iter):
        clipped = np.clip(vals, floor, ceiling)
        s = clipped.sum()
        renormalized = clipped / s if s > 0 else np.full(n, 1.0 / n)
        if np.allclose(renormalized, vals, atol=1e-14, rtol=0):
            vals = renormalized
            break
        vals = renormalized
    # Final hard clip: renormalizing after the last clip can nudge a value
    # a hair back outside [floor, ceiling] even at convergence -- guarantee
    # the bound exactly, at the cost of summing to 1 only approximately
    # (acceptable: this is a soft safety cap, not a probability simplex).
    vals = np.clip(vals, floor, ceiling)
    return dict(zip(keys, vals.tolist()))


def cluster_correlated_models(pairwise_rho: dict[tuple[str, str], float], models: list[str],
                              threshold: float = 0.8) -> list[set[str]]:
    """Union-find grouping of models whose pairwise correlation >= threshold.

    A pool-wide scalar rho_bar can't guarantee a correlated PAIR's joint
    weight stays under the ceiling once other, uncorrelated models are also
    in the pool (their zero correlation dilutes the mean) -- clustering on
    the actual pairwise values is what makes that guarantee possible.
    """
    parent = {m: m for m in models}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (a, b), rho in pairwise_rho.items():
        if a in parent and b in parent and rho >= threshold:
            union(a, b)

    groups: dict[str, set[str]] = {}
    for m in models:
        groups.setdefault(find(m), set()).add(m)
    return list(groups.values())


def clamp_weights_with_cluster_ceiling(w: dict[str, float], clusters: list[set[str]],
                                       floor: float, ceiling: float,
                                       max_iter: int = 200) -> dict[str, float]:
    """floor applies per model; ceiling applies to each cluster's COMBINED
    weight (mass freed by scaling an over-ceiling cluster down is
    redistributed to models OUTSIDE it, proportional to their current
    share) -- so a correlated pair can't jointly exceed the ceiling just
    because each half individually stays under it.

    Degenerate case: a cluster spanning the WHOLE pool has nothing outside
    it to redistribute to, so the ceiling is unenforceable there -- falls
    back to equal weights across the pool, the most conservative response
    to total correlation.
    """
    models = list(w)
    n = len(models)
    if n == 0:
        return {}
    idx = {m: i for i, m in enumerate(models)}
    vals = np.array([w[m] for m in models], dtype=float)
    total = vals.sum()
    vals = vals / total if total > 0 else np.full(n, 1.0 / n)

    for cluster in clusters:
        if len(cluster) == n:
            vals = np.full(n, 1.0 / n)
            return dict(zip(models, vals.tolist()))

    for _ in range(max_iter):
        prev = vals.copy()
        for cluster in clusters:
            cluster_idx = [idx[m] for m in cluster]
            cluster_sum = vals[cluster_idx].sum()
            if cluster_sum > ceiling + 1e-12:
                outside_idx = [i for i in range(n) if i not in cluster_idx]
                outside_sum = vals[outside_idx].sum()
                freed = cluster_sum - ceiling
                vals[cluster_idx] *= ceiling / cluster_sum
                if outside_sum > 0:
                    vals[outside_idx] *= (outside_sum + freed) / outside_sum
        vals = np.clip(vals, floor, None)
        s = vals.sum()
        vals = vals / s if s > 0 else np.full(n, 1.0 / n)
        if np.allclose(vals, prev, atol=1e-14, rtol=0):
            break
    vals = np.clip(vals, floor, None)
    return dict(zip(models, vals.tolist()))


def _pairwise_logit_correlations_by_category(
    conn, model_ids: tuple[str, ...], min_pairs: int
) -> dict[str, dict[tuple[str, str], float]]:
    """Per category, per-model-pair Pearson correlation of logit(p_yes) on
    matched same-day (condition_id, ts) forecasts. Shared by
    estimate_rho_bar_m4 (pool-averaged, Phase 13) and estimate_pairwise_rho_m4
    (per-pair, Phase 14.1's cluster-aware ceiling) -- one query/pairing pass,
    two different reductions over the same per-pair values.
    """
    # Streamed in (condition_id, ts) order, each matched point folded into
    # running co-moments per (category, pair) as it goes (2026-10-14). This
    # used to fetchall() every forecast of every pooled model -- ~2M rows as
    # Python objects -- and hold them in a dict of dicts: the monthly learn
    # run peaked at 935 MB in this one step and was OOM-killed at its 900 MB
    # cap, and the nightly MWU calls it once per category. Same Pearson
    # coefficient, computed with Welford's update instead of np.corrcoef.
    cursor = conn.execute(
        """
        SELECT f.condition_id, f.ts, f.model_id, f.p_yes, m.category AS category
        FROM forecasts f JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id IN ({})
        ORDER BY f.condition_id, f.ts
        """.format(",".join("?" for _ in model_ids)),
        model_ids,
    )
    # (category, (a, b)) -> [n, mean_x, mean_y, m2_x, m2_y, c_xy]
    moments: dict[tuple[str, tuple[str, str]], list[float]] = {}

    def fold(cat: str, logits_by_model: dict[str, float]) -> None:
        for a, b in itertools.combinations(sorted(logits_by_model), 2):
            x, y = logits_by_model[a], logits_by_model[b]
            m = moments.setdefault((cat, (a, b)), [0, 0.0, 0.0, 0.0, 0.0, 0.0])
            m[0] += 1
            dx = x - m[1]
            m[1] += dx / m[0]
            dy = y - m[2]
            m[2] += dy / m[0]
            m[3] += dx * (x - m[1])
            m[4] += dy * (y - m[2])
            m[5] += dx * (y - m[2])

    key = cat = None
    point: dict[str, float] = {}
    for r in cursor:
        k = (r["condition_id"], r["ts"])
        if k != key:
            if point:
                fold(cat, point)
            key, cat, point = k, r["category"] or "unknown", {}
        point[r["model_id"]] = float(logit(r["p_yes"]))
    if point:
        fold(cat, point)

    out: dict[str, dict[tuple[str, str], float]] = {}
    for (cat, pair), (n, _, _, m2x, m2y, cxy) in moments.items():
        if n < min_pairs or m2x <= 0 or m2y <= 0:
            continue
        out.setdefault(cat, {})[pair] = float(cxy / np.sqrt(m2x * m2y))
    return out


def estimate_rho_bar_m4(conn, config: dict[str, Any], model_ids: tuple[str, ...],
                        min_pairs_per_category: int = 30) -> dict[str, float]:
    """Per category: mean pairwise Pearson correlation of logit(p_yes) across
    `model_ids`, matched on same-day (condition_id, ts) forecasts. Categories
    with too few overlapping-pair observations are omitted entirely -- the
    caller treats a missing category as rho_bar=0.0 (no discount), the
    conservative default until enough data exists to estimate it."""
    by_cat_pairs = _pairwise_logit_correlations_by_category(conn, model_ids, min_pairs_per_category)
    return {cat: float(np.mean(list(pairs.values()))) for cat, pairs in by_cat_pairs.items()}


def estimate_pairwise_rho_m4(conn, config: dict[str, Any], model_ids: tuple[str, ...],
                             category: str, min_pairs: int = 30) -> dict[tuple[str, str], float]:
    """Per-pair Pearson correlation of logit(p_yes) for ONE category -- the
    same matched-forecast-rows logic estimate_rho_bar_m4 uses internally,
    exposed per-pair instead of pool-averaged (Phase 14.1: a single scalar
    rho_bar can't identify which specific pair is redundant once other,
    uncorrelated models dilute the pool-wide mean)."""
    by_cat_pairs = _pairwise_logit_correlations_by_category(conn, model_ids, min_pairs)
    return by_cat_pairs.get(category, {})


def estimate_rho_bar_m7(conn, store, config: dict[str, Any],
                        markets_map_path=None, min_days: int = 10,
                        lookback_days: int = 90) -> float | None:
    """Single overall value (not per-category -- confirmed-pair count is
    small, Phase 9's own acceptance bar was only >=5 pairs): for each
    confirmed pair, pull each side's own snapshot mid history over the
    trailing `lookback_days` (a monthly-batch job, not latency sensitive, but
    still bounded rather than scanning the entire snapshot archive), resample
    to one observation per UTC date (last value that day), align by date,
    correlate logit(mid). Averaged across pairs with >= min_days overlapping
    days. Returns None if no pair has enough overlap yet (caller treats this
    as rho_bar=0.0, the conservative no-discount default)."""
    from lab.models.m7_crossvenue import confirmed_by_condition, load_markets_map
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    data = load_markets_map(markets_map_path)
    by_cid = confirmed_by_condition(data)
    if not by_cid:
        return None

    now = now_utc()
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(lookback_days)]
    # Projection matters here more than anywhere else in the codebase: this is
    # 90 days of every venue's snapshots, and an unprojected read pulls the
    # bids_json/asks_json order-book blobs -- "the bulk of a row's bytes", per
    # read_range's own docstring -- for all of it. Unprojected it cost >1.3GB
    # and was OOM-killed on the production host (2026-08-05); this function
    # only ever reads these three columns.
    from lab.store import db as dbmod

    # Only the confirmed pairs' own markets. Projection alone still left this
    # reading every row of a 90-day window (~14.9M) to correlate a few dozen
    # markets -- same defect the CLV validity check died of.
    pair_cids = {cid for cid in by_cid}
    for pairs in by_cid.values():
        pair_cids |= {dbmod.venue_condition_id(p["venue"], p["external_id"]) for p in pairs}
    # One day at a time, each reduced to its last mid per market before the
    # next is read (2026-10-14): the pairs' legs are snapshotted every two
    # minutes, and with 404 confirmed pairs the 90-day read of every row
    # peaked this step at ~1.07 GB and OOM-killed the monthly learn run.
    # Only the last value per market and day is used below, so the result is
    # the same.
    import polars as pl

    days = []
    for date in dates:
        day = store.read_range([date], columns=["ts", "condition_id", "mid"],
                               condition_ids=pair_cids)
        if not day.is_empty():
            days.append(day.sort("ts").group_by("condition_id").last()
                        .with_columns(pl.lit(date).alias("_date")))
    if not days:
        return None
    df = pl.concat(days, how="diagonal")

    correlations = []
    for cid, pairs in by_cid.items():
        external_cids = [dbmod.venue_condition_id(p["venue"], p["external_id"]) for p in pairs]
        all_cids = [cid, *external_cids]
        daily = df.filter(df["condition_id"].is_in(all_cids))
        if daily.is_empty():
            continue
        by_cid_date: dict[str, dict[str, float]] = {}
        for row in daily.to_dicts():
            if row["mid"] is None:
                continue
            by_cid_date.setdefault(row["condition_id"], {})[row["_date"]] = float(row["mid"])

        for a, b in itertools.combinations(all_cids, 2):
            a_series, b_series = by_cid_date.get(a, {}), by_cid_date.get(b, {})
            shared_dates = sorted(set(a_series) & set(b_series))
            if len(shared_dates) < min_days:
                continue
            xs = logit(np.array([a_series[d] for d in shared_dates]))
            ys = logit(np.array([b_series[d] for d in shared_dates]))
            if np.std(xs) == 0 or np.std(ys) == 0:
                continue
            correlations.append(float(np.corrcoef(xs, ys)[0, 1]))

    return float(np.mean(correlations)) if correlations else None
