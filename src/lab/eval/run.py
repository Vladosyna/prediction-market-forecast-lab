"""`lab eval`: score resolved forecasts per model and window, persist eval_runs.

Phase 11 (brief section 7/11): grouped per (model_id, window_label, venue,
category) -- each forecast is scored against its OWN venue's price, forecastable
venues read from the `venues` table. The cluster bootstrap resamples by
event_id (falling back to condition_id); the anytime-valid confidence sequence
and the precision-weighted stratified estimator are computed alongside the
classical bootstrap CI and persisted as secondary columns.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import numpy as np

from lab.eval.anytime import confidence_sequence
from lab.eval.calibration import calibration_bins
from lab.eval.distributional import bucketed_resolved_events
from lab.eval.scoring import brier, paired_rps_skill, paired_skill
from lab.eval.stratified import precision_weighted_skill
from lab.store import db as dbmod
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

WINDOWS = {"all_time": None, "trailing_90d": 90}

# Pre-registered constants -- deliberately NOT in config.yaml, which is
# operator-tunable. Both are fixed by docs/pre_analysis_plan.md, and until
# 2026-09-25 the code applied neither: an audit found every Kalshi statistic,
# including the anytime-valid CS the PAP says may be read daily, computed over
# the forecasts the PAP had excluded, and no eval_runs row corresponding to the
# confirmatory sample §6 defines. A pre-registered rule protects the analysis
# only once it is code written before the results are read.
#
# PAP §6: confirmatory = forecasts made on or after the commitment date.
CONFIRMATORY_START = "2026-07-06"
# Forecast-date windows excluded "for any Kalshi-population statistic",
# inclusive, each with the addendum that declared it.
KALSHI_EXCLUSION_WINDOWS = (
    ("2026-08-11", "2026-08-16"),   # PAP 9.17 -- stale end dates blanked the venue
    ("2026-08-30", "2026-08-30"),   # PAP 9.26 -- sports flood starved the tail round
    ("2026-09-02", "2026-09-07"),   # PAP 9.26
    ("2026-09-18", "2026-09-25"),   # PAP 9.30 -- rate budget, lock storm, OOM loop
)
# (label, trailing days, since timestamp). "confirmatory" is the pre-registered
# sample, not a chosen window, so CLAUDE.md §7's "no cherry-picked windows"
# rule is what requires it rather than what forbids it.
EVAL_WINDOWS = {
    "all_time": (None, None),
    "trailing_90d": (90, None),
    "confirmatory": (None, CONFIRMATORY_START),
}

# Sentinel category value for the "all categories pooled, this venue" row --
# distinct from a legacy pre-Phase-11 eval_runs row's NULL category.
ALL_CATEGORIES = "ALL"


def challenger_registered_ts(conn, model_id: str) -> str | None:
    """Earliest registration timestamp for a versioned/challenger model_id.

    Forward-only rule (brief section 6/guardrail 15): a registered challenger
    earns skill only from forecasts made after its own registration -- it is
    never scored on history predating its existence. Legacy model_ids with no
    model_versions row are unaffected (returns None).
    """
    row = conn.execute(
        "SELECT MIN(registered_ts) AS ts FROM model_versions WHERE model_id = ?", (model_id,)
    ).fetchone()
    return row["ts"] if row is not None else None


def resolved_forecast_rows(
    conn, model_id: str, window_days: int | None,
    venue: str | None = None, category: str | None = None,
    null_control_ids: set[str] | None = None, invert_null_control: bool = False,
    include_disputed: bool = False, since_ts: str | None = None,
    apply_exclusions: bool = False,
) -> list[dict]:
    """Paired rows: forecast + resolution outcome + venue/category/event_id
    for one model, optionally scoped to one venue and/or one category.

    include_disputed=False (default, unchanged behavior) excludes disputed
    resolutions unconditionally -- every existing caller keeps today's
    result exactly. include_disputed=True drops that filter, giving PAP
    Addendum 9.2(b)'s promised robustness check something to actually
    compare against (previously nothing did: disputed markets were excluded
    everywhere with no inclusive path to re-run instead)."""
    query = """
        SELECT f.condition_id, f.p_yes, f.p_market_at_ts, f.spread_at_ts,
               r.payout_yes, r.resolved_ts,
               f.ts AS forecast_ts, f.m3_randomized AS m3_randomized,
               f.m3_random_seed AS m3_random_seed,
               f.depth_covariate AS depth_covariate, f.volume_24h AS volume_24h,
               f.trades_24h AS trades_24h, f.hour_utc AS hour_utc,
               m.venue AS venue, m.category AS category, m.event_id AS event_id,
               m.tier AS tier, m.end_date_iso AS end_date_iso, m.neg_risk AS neg_risk,
               f.days_to_resolution_at_ts AS days_to_resolution_at_ts
        FROM forecasts f
        JOIN resolutions r ON r.condition_id = f.condition_id
        JOIN markets m ON m.condition_id = f.condition_id
        WHERE f.model_id = ?
    """
    if not include_disputed:
        query += " AND r.disputed = 0"
    params: list[Any] = [model_id]
    if venue is not None:
        query += " AND m.venue = ?"
        params.append(venue)
    if category is not None:
        query += " AND m.category = ?"
        params.append(category)
    if window_days is not None:
        query += " AND f.ts >= ?"
        params.append((now_utc() - timedelta(days=window_days)).isoformat(timespec="seconds"))
    if since_ts is not None:
        query += " AND f.ts >= ?"
        params.append(since_ts)
    if apply_exclusions and KALSHI_EXCLUSION_WINDOWS:
        # Off by default so the paper export stays the complete replication
        # dataset (every row carries forecast_ts, so a replicator applies the
        # same windows at analysis time -- docs/paper_export_schema.md says
        # which). run_eval turns it on: the PAP excludes these windows from
        # "any Kalshi-population statistic", not from the record.
        spans = " OR ".join("date(f.ts) BETWEEN ? AND ?" for _ in KALSHI_EXCLUSION_WINDOWS)
        query += f" AND NOT (COALESCE(m.venue, 'polymarket') = 'kalshi' AND ({spans}))"
        for lo, hi in KALSHI_EXCLUSION_WINDOWS:
            params += [lo, hi]
    registered_ts = challenger_registered_ts(conn, model_id)
    if registered_ts is not None:
        query += " AND f.ts >= ?"
        params.append(registered_ts)
    rows = [dict(r) for r in conn.execute(query, params)]
    if null_control_ids is not None:
        if invert_null_control:
            rows = [r for r in rows if r["condition_id"] in null_control_ids]
        else:
            rows = [r for r in rows if r["condition_id"] not in null_control_ids]
    return rows


def _event_cluster_ids(rows: list[dict]) -> np.ndarray:
    return np.array([r["event_id"] or r["condition_id"] for r in rows])


def _per_cluster_diffs_in_resolution_order(
    diffs: np.ndarray, cluster_ids: np.ndarray, resolved_ts: list[str]
) -> np.ndarray:
    """One mean diff per event-cluster, ordered by each cluster's earliest
    resolution -- what the anytime-valid CS treats as its sequential sample
    (brief section 7: "n counts resolved event clusters, not venue-market
    rows")."""
    # Deterministic order (2026-09-25, PAP 9.32): resolution time, then cluster
    # id. The previous `np.argsort(resolved_ts)` used quicksort -- unstable --
    # over rows the query returns in no defined order, so clusters resolving
    # in the same second came out in SQLite's physical row order, and the
    # sequence the anytime-valid CS consumes (the pre-registered confirmatory
    # statistic) could change after a VACUUM or a new index with no change in
    # data. Resolution-time order is unchanged; only ties are now decided by
    # the data instead of by storage.
    order = np.lexsort((np.asarray(cluster_ids, dtype=str), np.asarray(resolved_ts, dtype=str)))
    first_seen: dict[str, int] = {}
    ordered_clusters: list[str] = []
    for idx in order:
        cid = cluster_ids[idx]
        if cid not in first_seen:
            first_seen[cid] = len(ordered_clusters)
            ordered_clusters.append(cid)
    buckets: list[list[float]] = [[] for _ in ordered_clusters]
    for cid, d in zip(cluster_ids, diffs):
        buckets[first_seen[cid]].append(d)
    return np.array([float(np.mean(b)) for b in buckets])


def evaluate_model(
    conn, model_id: str, window_label: str, rows: list[dict], config: dict[str, Any],
    venue: str | None = None, category: str | None = None, window_days: int | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    p_model = np.array([r["p_yes"] for r in rows])
    p_market = np.array([r["p_market_at_ts"] for r in rows])
    y = np.array([r["payout_yes"] for r in rows])
    cluster_ids = _event_cluster_ids(rows)

    result = paired_skill(
        p_model=p_model, p_market=p_market, y=y, condition_ids=cluster_ids,
        iterations=config["eval"]["bootstrap_iterations"],
    )
    bins = calibration_bins(p_model, y, n_bins=config["eval"]["calibration_bins"])

    diffs = brier(p_market, y) - brier(p_model, y)
    resolved_ts = [r["resolved_ts"] for r in rows]
    per_cluster_diffs = _per_cluster_diffs_in_resolution_order(diffs, cluster_ids, resolved_ts)
    cs = confidence_sequence(
        per_cluster_diffs, alpha=config["eval"]["confidence_sequence"]["alpha"]
    )

    stratified = precision_weighted_skill(
        diffs, p_market, cluster_ids, iterations=config["eval"]["bootstrap_iterations"]
    )

    # Phase 16 (v2.4): RPS is a SECONDARY outcome on this same eval_runs row --
    # binary Brier above stays the sole primary, pre-registered statistic.
    # Bucketed events naturally only ever exist for the venue that carries
    # negRisk groupings (Polymarket); a cross-venue confirmed pair's synthetic
    # external-venue condition_id never accrues its own forecasts (M7 pools
    # external prices as an INPUT to the Polymarket-side forecast, it never
    # writes one for the external leg), so it can never satisfy the >=2-legs
    # check below -- no explicit venue filter needed to keep the two event_id
    # use-cases from colliding.
    # ALL_CATEGORIES/null_control rows pool across every category (no single
    # category to filter bucketed events to); null_control specifically
    # doesn't further restrict to sports-only bucketed events here -- a
    # stated simplification, since RPS is already a secondary metric and
    # threading null-control condition_ids through this pipeline too would
    # add real machinery for a case that will be rare (few sports events are
    # themselves bucketed numeric questions).
    bucketed_category = None if category == ALL_CATEGORIES else category
    rps_result = None
    n_bucketed = config["eval"].get("min_bucketed_events", 20)
    events = bucketed_resolved_events(conn, model_id, category=bucketed_category,
                                      window_days=window_days)
    if len(events) >= n_bucketed:
        rps_result = paired_rps_skill(events, iterations=config["eval"]["bootstrap_iterations"])

    conn.execute(
        """
        INSERT INTO eval_runs (ts, model_id, window_label, n, brier, brier_market,
                               skill, skill_ci_lo, skill_ci_hi, log_loss,
                               log_loss_market, calibration_json,
                               venue, category, skill_pw, skill_pw_ci_lo, skill_pw_ci_hi,
                               n_strata_pw, cs_lo, cs_hi, cs_covers_zero, n_event_clusters,
                               rps, rps_market)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_utc_iso(), model_id, window_label, result.n, result.brier_model,
         result.brier_market, result.skill, result.skill_ci_lo, result.skill_ci_hi,
         result.log_loss_model, result.log_loss_market, json.dumps(bins),
         venue, category, stratified.skill_pw, stratified.ci_lo, stratified.ci_hi,
         stratified.n_strata, cs.lo, cs.hi, int(cs.covers_zero), result.n_markets,
         rps_result.rps_model if rps_result else None,
         rps_result.rps_market if rps_result else None),
    )
    # Commit this row now (2026-09-25). run_eval used to commit once per
    # MODEL, which meant the write transaction opened by this INSERT stayed
    # open through every bootstrap, confidence sequence and stratified fit for
    # the rest of that model's venues x categories x windows x horizon buckets
    # -- minutes at a time, dozens of times a night -- while collector jobs
    # queued behind it and died at busy_timeout. eval_runs rows are
    # independent, so a partial run is still a coherent prefix.
    conn.commit()
    return {
        "model_id": model_id, "window": window_label, "venue": venue, "category": category,
        "result": result, "bins": bins, "cs": cs, "stratified": stratified,
        "rps_result": rps_result, "n_bucketed_events": len(events),
    }



def _days_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return (datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() / 86400
    except ValueError:
        return None


def _bucket_for_days(days: float | None) -> str | None:
    from lab.learn.refit import HORIZON_BUCKETS

    if days is None or days <= 0:
        return None
    for name, (lo, hi) in HORIZON_BUCKETS.items():
        if lo <= days < hi:
            return name
    return None


def _stated_horizon_bucket(row: dict) -> str | None:
    """The PRIMARY horizon for H1 from 2026-09-25 (PAP 9.31): time to the
    market's STATED end date at the moment the forecast was made -- the only
    horizon anyone could know then, and the one M1 itself used to pick a curve.

    Rows frozen from 2026-09-25 carry it (`days_to_resolution_at_ts`); older
    rows fall back to the market's current end date, a disclosed proxy that
    differs only where a venue moved the date after the forecast.
    """
    days = row.get("days_to_resolution_at_ts")
    if days is None:
        days = _days_between(row.get("forecast_ts"), row.get("end_date_iso"))
    return _bucket_for_days(days)


def _realized_horizon_bucket(row: dict) -> str | None:
    """The definition used until 2026-09-25, now the named sensitivity check
    ("_hr_" rows, confirmatory window only). Resolution time minus forecast
    time conditions on the outcome -- "by date" markets resolve early exactly
    when the event happens, so long realized horizons are NO-enriched -- and
    it inherits the resolution watcher's recording lag. Measured 2026-09-25 on
    Polymarket: price minus outcome in the >=30-day bucket was +0.026 under
    this definition and -0.009 under the stated one. See PAP 9.31.
    """
    return _bucket_for_days(_days_between(row.get("forecast_ts"), row.get("resolved_ts")))


def run_eval(conn, config: dict[str, Any], include_disputed: bool = False,
             row_filter: Callable[[list[dict]], list[dict]] | None = None,
             suffix: str = "", models: frozenset[str] | set[str] | None = None,
             ) -> list[dict[str, Any]]:
    """include_disputed=False (default) is the unchanged nightly path.
    include_disputed=True is PAP Addendum 9.2(b)'s robustness re-run: same
    models/venues/categories/windows, disputed markets included instead of
    unconditionally dropped, written to eval_runs under a distinctly
    suffixed window_label so it never collides with or overwrites a primary
    row -- a named comparison alongside the primary result, not a
    replacement for it (brief section 9.2(b), section 4.9(i))."""
    from lab.forecast import null_control_ids_by_venue

    # `row_filter`/`suffix`/`models` are the pre-registered robustness checks
    # (ROBUSTNESS_CHECKS below): the identical matrix on a filtered row set,
    # under its own parallel window_label, never overwriting a primary row.
    label_suffix = ("_disputed_inclusive" if include_disputed else "") + suffix
    keep = row_filter or (lambda rows: rows)

    nc_ids_by_venue = null_control_ids_by_venue(conn, config)
    model_ids = [r["model_id"] for r in conn.execute(
        "SELECT DISTINCT model_id FROM forecasts ORDER BY model_id"
    ) if models is None or r["model_id"] in models]
    venue_categories: dict[str, list[str]] = {}
    for r in conn.execute(
        """
        SELECT DISTINCT venue, category FROM markets
        WHERE venue IN (SELECT venue FROM venues WHERE forecastable = 1)
        ORDER BY venue, category
        """
    ):
        venue_categories.setdefault(r["venue"], []).append(r["category"])

    out: list[dict[str, Any]] = []
    for model_id in model_ids:
        # One transaction per model rather than one for the whole run: eval
        # held a write lock from 02:21 to 02:50 and collector jobs firing in
        # that window failed with "database is locked". Committing here bounds
        # it to a single model's work; eval_runs rows are independent, so a
        # partial run leaves a coherent prefix.
        conn.commit()
        for venue, categories in venue_categories.items():
            nc_ids = nc_ids_by_venue.get(venue)
            for category in categories:
                for label, (days, since) in EVAL_WINDOWS.items():
                    rows = resolved_forecast_rows(
                        conn, model_id, days, venue=venue, category=category,
                        null_control_ids=nc_ids, include_disputed=include_disputed,
                        since_ts=since, apply_exclusions=True,
                    )
                    rows = keep(rows)
                    summary = evaluate_model(
                        conn, model_id, label + label_suffix, rows, config,
                        venue=venue, category=category, window_days=days,
                    )
                    if summary:
                        out.append(summary)
            # "ALL categories" aggregate row per venue -- per-category n stays
            # sparse for months (brief section 11 timelines), this keeps a
            # non-sparse view available from day one.
            for label, (days, since) in EVAL_WINDOWS.items():
                rows = resolved_forecast_rows(
                    conn, model_id, days, venue=venue, null_control_ids=nc_ids,
                    include_disputed=include_disputed, since_ts=since, apply_exclusions=True,
                )
                rows = keep(rows)
                summary = evaluate_model(
                    conn, model_id, label + label_suffix, rows, config,
                    venue=venue, category=ALL_CATEGORIES, window_days=days,
                )
                if summary:
                    out.append(summary)

                # Horizon buckets are scored from the rows just fetched, not
                # from a second identical query: this loop is per model x venue
                # x window and the eval already runs for tens of minutes inside
                # the collector's own cgroup.
                # "_hs_" = stated horizon, the primary strata (PAP 9.31);
                # "_hr_" = realized horizon, the pre-2026-09-25 definition,
                # kept as a named sensitivity check on the confirmatory
                # sample only. The old "_h_" label is no longer written, so
                # no eval_runs label changes meaning mid-series.
                definitions = [("hs", _stated_horizon_bucket)]
                if label == "confirmatory":
                    definitions.append(("hr", _realized_horizon_bucket))
                for tag, bucket_of in definitions:
                    by_bucket: dict[str, list[dict]] = {}
                    for row in rows:
                        bucket = bucket_of(row)
                        if bucket:
                            by_bucket.setdefault(bucket, []).append(row)
                    for bucket, bucket_rows in by_bucket.items():
                        h_summary = evaluate_model(
                            conn, model_id, f"{label}_{tag}_{bucket}" + label_suffix,
                            bucket_rows, config, venue=venue,
                            category=ALL_CATEGORIES, window_days=days,
                        )
                        if h_summary:
                            out.append(h_summary)
            # (H1 is stated over HORIZON BUCKETS -- "paired Brier skill in the
            # >=30-day horizon buckets", PAP section 2. Until 2026-08-10 nothing
            # computed that: run_eval's dimensions were model x venue x category
            # x window, with no horizon at all, so the primary hypothesis had no
            # primary statistic and its realized n went unseen for months -- it
            # turned out to be 13-33 event clusters against this plan's own
            # 200-cluster INSUFFICIENT floor. Scored in the loop above so it
            # inherits the anytime-valid CS, the event clustering and the
            # honesty tiers rather than being computed ad hoc at the freeze.)
            # Null control scored separately, same math, shown side by side --
            # one venue-scoped sample per forecastable venue. window_days=None
            # (all-time) since nc_rows above isn't window-scoped either.
            nc_rows = resolved_forecast_rows(
                conn, model_id, None, venue=venue, null_control_ids=nc_ids,
                invert_null_control=True, include_disputed=include_disputed,
                apply_exclusions=True,
            )
            nc_rows = keep(nc_rows)
            nc_summary = evaluate_model(
                conn, model_id, "null_control" + label_suffix, nc_rows, config,
                venue=venue, category=ALL_CATEGORIES, window_days=None,
            )
            if nc_summary:
                out.append(nc_summary)
    conn.commit()
    log.info("eval complete", extra={"ctx": {"summaries": len(out), "include_disputed": include_disputed}})
    return out


# --- Pre-registered robustness checks (2026-09-25) ---------------------------
#
# Each was committed in docs/pre_analysis_plan.md BEFORE the confirmatory
# analysis, as "the identical model x venue x category x window matrix" on a
# different row set, "alongside -- never replacing -- the primary result".
# Until 2026-09-25 only 9.2(b) existed as code; the rest were prose. A check
# promised before the results are read protects the analysis only if it is
# implemented before they are read, so they are code now -- run on demand
# (`lab eval --robustness`), not nightly: each is a full pass of the matrix,
# and seven of them every night would be the resource failure this project
# just spent a day undoing. Run them for the confirmatory analysis.

M1_FAMILY = frozenset({"m1_debiased", "m1_hier@polymarket", "m1_hier@kalshi", "m1_hier@metaculus"})


def _first_per_market_day(rows: list[dict]) -> list[dict]:
    """PAP 9.5: the first forecast per (market, model, UTC day) -- the
    pre-registered cadence. run_eval passes one model's rows at a time, so
    (market, day) is the key. Deterministic order, so the result -- and the
    CS computed on it -- does not depend on the query's row order."""
    seen: set[tuple[str, str]] = set()
    out = []
    for r in sorted(rows, key=lambda r: (r["forecast_ts"], r["condition_id"])):
        key = (r["condition_id"], r["forecast_ts"][:10])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _before_end_date(rows: list[dict]) -> list[dict]:
    """PAP 9.11: exclude forecasts written on or after their market's end date."""
    return [r for r in rows
            if not ((d := _days_between(r["forecast_ts"], r.get("end_date_iso"))) is not None
                    and d <= 0)]


def _since(day: str) -> Callable[[list[dict]], list[dict]]:
    return lambda rows: [r for r in rows if r["forecast_ts"][:10] >= day]


ROBUSTNESS_CHECKS: dict[str, dict[str, Any]] = {
    # 9.2(b): disputed resolutions included instead of excluded.
    "disputed_inclusive": {"pap": "9.2(b)", "include_disputed": True},
    # 9.5: deduplicated ledger, first forecast per (market, model, day).
    "dedup_daily": {"pap": "9.5", "suffix": "_dedup_daily", "filter": _first_per_market_day},
    # 9.11: no forecast written on or after its market's end date.
    "pre_end_date": {"pap": "9.11", "suffix": "_pre_end_date", "filter": _before_end_date},
    # 9.18, 9.19, 9.20 (and 9.21, which reuses them): one boundary, 2026-08-18.
    "since_20260818": {"pap": "9.18-9.21", "suffix": "_since_20260818",
                       "filter": _since("2026-08-18")},
    # 9.22: M7 only, from its pair repair on 2026-08-22.
    "m7_since_20260822": {"pap": "9.22", "suffix": "_since_20260822",
                          "models": frozenset({"m7_crossvenue"}), "filter": _since("2026-08-22")},
    # 9.3(a): M1/M1.x split by negRisk. 9.3(b) -- collateral-yield programs --
    # has no market-level data in this lab (which Polymarket markets were
    # holding-rewards eligible was never collected; Kalshi's APY is
    # venue-wide), so the per-venue matrix is the closest available split.
    "negrisk": {"pap": "9.3(a)", "suffix": "_negrisk", "models": M1_FAMILY,
                "filter": lambda rows: [r for r in rows if r.get("neg_risk")]},
    "non_negrisk": {"pap": "9.3(a)", "suffix": "_non_negrisk", "models": M1_FAMILY,
                    "filter": lambda rows: [r for r in rows if not r.get("neg_risk")]},
}


def run_robustness_checks(conn, config: dict[str, Any],
                          names: list[str] | None = None) -> dict[str, int]:
    """Every pre-registered robustness check (or the named subset), each as a
    full pass of the evaluation matrix under its own window_label suffix.
    Returns {check: summaries written}."""
    unknown = set(names or []) - set(ROBUSTNESS_CHECKS)
    if unknown:
        raise ValueError(f"unknown robustness check(s): {sorted(unknown)}")
    out: dict[str, int] = {}
    for name, spec in ROBUSTNESS_CHECKS.items():
        if names and name not in names:
            continue
        summaries = run_eval(conn, config,
                             include_disputed=spec.get("include_disputed", False),
                             row_filter=spec.get("filter"), suffix=spec.get("suffix", ""),
                             models=spec.get("models"))
        out[name] = len(summaries)
        log.info("robustness check complete",
                 extra={"ctx": {"check": name, "pap": spec["pap"], "summaries": len(summaries)}})
    return out
