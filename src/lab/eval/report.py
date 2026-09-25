"""Static HTML report via jinja2: health block, skill table, calibration plots."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment

from lab.collect.status import (
    cadence_buckets,
    gaps_from_timestamps,
    gather_status,
    tier_snapshot_timestamps,
)
from lab.eval.calibration import plot_reliability
from lab.eval.clv import clv_dates
from lab.eval.scoring import honesty_tier
from lab.store.snapshots import utc_date_str
from lab.util import PROJECT_ROOT, now_utc, now_utc_iso

log = logging.getLogger(__name__)

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Forecast Lab report</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1a1a1a; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: .3rem; }
table { border-collapse: collapse; margin: 1rem 0; width: 100%; }
th, td { border: 1px solid #ccc; padding: .4rem .6rem; text-align: right; font-size: .9rem; }
th { background: #f5f5f5; } td:first-child, th:first-child { text-align: left; }
.tier-INSUFFICIENT { color: #b00; font-weight: 600; }
.tier-PRELIMINARY { color: #b60; font-weight: 600; }
.tier-STANDARD { color: #070; font-weight: 600; }
.health { background: #f8f9fa; border: 1px solid #ddd; padding: 1rem; white-space: pre-wrap;
          font-family: monospace; font-size: .85rem; }
.note { color: #666; font-size: .85rem; }
img { max-width: 100%; }
</style></head><body>
<h1>Polymarket Forecast Lab — report</h1>
<p class="note">Generated {{ generated_at }} (UTC). All figures are research measurements;
shadow-portfolio numbers, where present, are SIMULATION only.</p>

<h2>Data health</h2>
<div class="health">{{ health }}</div>

<h2>Universe inclusion / exclusion (trailing {{ universe_log_days }} days)</h2>
<p class="note">Every market excluded from -- or never granted forecast-target status in -- the
universe, with a reason code (brief section 5/15). Defends against selection-bias claims in
review: answers "why isn't X in the ledger" in aggregate. 'ambiguous_resolution' is deliberately
not auto-populated (no safe deterministic detector exists); 'manual' comes from `lab exclude`.</p>
{% if universe_exclusion_rows %}
<table>
<tr><th>date</th><th>reason</th><th>n excluded</th></tr>
{% for r in universe_exclusion_rows %}
<tr><td>{{ r.day }}</td><td>{{ r.reason_code }}</td><td>{{ r.n }}</td></tr>
{% endfor %}
</table>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no universe_log rows in this window yet.</p>
{% endif %}
{% if universe_inclusion_rows %}
<table>
<tr><th>date</th><th>tier</th><th>n newly included</th></tr>
{% for r in universe_inclusion_rows %}
<tr><td>{{ r.day }}</td><td>{{ r.tier }}</td><td>{{ r.n }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>Skill vs market (paired Brier), by venue and category</h2>
<p class="note">skill = mean(brier_market − brier_model); positive = beating the market.
95% CI: cluster bootstrap by event (fallback market) -- descriptive only as of Phase 11.
n markets counts resolved event clusters, not venue-market rows. A skill estimate smaller
than its own MDE is noise by construction. skill_pw is the precision-weighted stratified
estimator (brief section 7); a skill claim requires the anytime-valid CS AND skill_pw's own
CI to both exclude zero and agree in direction.</p>
{% if skill_rows %}
<p class="tier-PRELIMINARY">Kalshi results computed before 2026-08-26 are withheld from this table:
that venue had no event clustering until then, so their n, bootstrap CI and anytime-valid sequence
were all computed over an inflated unit (PAP addendum 9.25). A Kalshi row absent here has not yet
been recomputed under correct clustering.</p>
<table>
<tr><th>model</th><th>venue</th><th>category</th><th>window</th><th>n rows</th><th>n markets</th><th>tier</th>
<th>brier model</th><th>brier market</th><th>skill</th><th>95% CI</th><th>MDE</th>
<th>skill_pw</th><th>skill_pw 95% CI</th><th>n strata</th>
<th>anytime CS</th><th>CS covers 0?</th>
<th>log loss</th><th>log loss mkt</th></tr>
{% for r in skill_rows %}
<tr><td>{{ r.model_id }}</td><td>{{ r.venue }}</td><td>{{ r.category }}</td>
<td>{{ r.window }}</td><td>{{ r.n }}</td><td>{{ r.n_markets }}</td>
<td class="tier-{{ r.tier.split(' ')[0] }}">{{ r.tier }}</td>
<td>{{ "%.4f"|format(r.brier_model) }}</td><td>{{ "%.4f"|format(r.brier_market) }}</td>
<td>{{ "%.4f"|format(r.skill) }}</td>
<td>[{{ "%.4f"|format(r.ci_lo) }}, {{ "%.4f"|format(r.ci_hi) }}]</td>
<td>{{ "%.4f"|format(r.mde) if r.mde == r.mde else "—" }}</td>
<td>{{ "%.4f"|format(r.skill_pw) if r.skill_pw is not none else "insufficient data" }}</td>
<td>{% if r.skill_pw_ci_lo is not none %}[{{ "%.4f"|format(r.skill_pw_ci_lo) }}, {{ "%.4f"|format(r.skill_pw_ci_hi) }}]{% else %}—{% endif %}</td>
<td>{{ r.n_strata_pw if r.n_strata_pw is not none else "—" }}</td>
<td>{% if r.cs_lo is not none %}[{{ "%.4f"|format(r.cs_lo) }}, {{ "%.4f"|format(r.cs_hi) }}]{% else %}—{% endif %}</td>
<td class="{{ 'tier-INSUFFICIENT' if r.cs_covers_zero else 'tier-STANDARD' }}">
{% if r.cs_covers_zero is not none %}{{ "yes" if r.cs_covers_zero else "no" }}{% else %}—{% endif %}</td>
<td>{{ "%.4f"|format(r.log_loss) }}</td><td>{{ "%.4f"|format(r.log_loss_market) }}</td></tr>
{% endfor %}
</table>
<p class="note">Rows labeled <b>null_control</b> are the sports control sample: statistically
significant skill there invalidates the run pending investigation. Rows with venue/category
"(legacy)" predate Phase 11 and were never re-scored per-venue.</p>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved paired forecasts yet.</p>
{% endif %}

<h2>Pooling / extremization diagnostics</h2>
<p class="note">Per-category extremization exponent applied to M4's pool and to M7's external
venue pool (brief section 6, Phase 13). a=1.0 means no extremization (today's plain log-odds
pool). rho_bar is the mean pairwise correlation of pooled sources on matched events/forecasts;
n_eff is the correlation-discounted effective source count actually used to scale how much of
the fitted a gets applied at forecast time.</p>
{% if pooling_rows %}
<table>
<tr><th>pool</th><th>category</th><th>a (fitted)</th><th>rho_bar</th><th>n members</th><th>n_eff</th></tr>
{% for r in pooling_rows %}
<tr><td>{{ r.pool }}</td><td>{{ r.category }}</td><td>{{ "%.3f"|format(r.a) }}</td>
<td>{{ "%.3f"|format(r.rho_bar) }}</td><td>{{ "%.1f"|format(r.n_members) }}</td>
<td>{{ "%.2f"|format(r.n_eff) }}</td></tr>
{% endfor %}
</table>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no extremization exponent fitted yet.</p>
{% endif %}

<h2>Distributional skill (RPS, secondary)</h2>
<p class="note">Ranked Probability Score over same-venue negRisk bucketed events (Phase 16) --
e.g. CPI ranges, temperature bands scored as one ordered distribution instead of independent
binary legs. <b>Secondary to the binary Brier table above</b>, which stays the sole primary,
pre-registered statistic (brief section 7's PAP is unaffected). A row appears here only once a
model/venue/category/window has resolved at least {{ min_bucketed_events }} bucketed events --
below that, rps stays NULL on the eval_runs row and nothing is shown for it.</p>
{% if rps_rows %}
<table>
<tr><th>model</th><th>venue</th><th>category</th><th>window</th>
<th>rps model</th><th>rps market</th><th>skill (rps)</th></tr>
{% for r in rps_rows %}
<tr><td>{{ r.model_id }}</td><td>{{ r.venue }}</td><td>{{ r.category }}</td><td>{{ r.window }}</td>
<td>{{ "%.4f"|format(r.rps_model) }}</td><td>{{ "%.4f"|format(r.rps_market) }}</td>
<td>{{ "%.4f"|format(r.skill_rps) }}</td></tr>
{% endfor %}
</table>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no model/venue/category/window has resolved
{{ min_bucketed_events }} bucketed events yet.</p>
{% endif %}

<h2>Calibration</h2>
{% if calibration_plot %}<img src="{{ calibration_plot }}" alt="reliability diagram">
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved forecasts to plot.</p>{% endif %}

<h2>CLV-style early signal</h2>
{% if not clv_trusted %}
<p class="tier-INSUFFICIENT">CLV UNTRUSTED — see status: this metric correlated with realized skill on
the sports null control (Phase 17 item 4), where any real correlation should be ~zero. Treat every
number in this section as unreliable lab-wide until a later check clears this flag.</p>
{% endif %}
<p class="note">Mean signed market drift toward the model's view at t+24h / t+72h.
Positive = the model tends to be early. Needs no resolutions.</p>
{% if clv_rows %}
<table>
<tr><th>model</th><th>horizon</th><th>n</th><th>mean signed drift</th></tr>
{% for r in clv_rows %}
<tr><td>{{ r.model_id }}</td><td>t+{{ r.horizon }}h</td><td>{{ r.n }}</td>
<td>{{ "%.4f"|format(r.drift) if r.drift == r.drift else "—" }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no forecasts with t+24h snapshots yet.</p>{% endif %}
{% if clv_dropped_for_gap %}
<p class="note">excluded: {{ clv_dropped_for_gap }} window(s) overlapped a recorded collection gap
(Phase 17 item 5) — dropped from the mean, not silently treated as ordinary missing data.</p>
{% endif %}

<h2>Virtual prediction economy — wealth ledger (SIMULATION-adjacent, no real stakes)</h2>
<p class="note">Kelly log-wealth accounting per (model, category) -- a scoring/selection layer
over every model's already-written forecasts, not a new signal (brief section 6/14). Compare
models by <b>cum_log_wealth / n_forecasts</b> (coverage-normalized), never the raw cumulative
total -- a model covering fewer markets shouldn't be rewarded or penalized for coverage alone.
The "sports" category is the null-control reference: skill there should stay near zero.</p>
{% if wealth_rankings %}
<table>
<tr><th>model</th><th>category</th><th>cum log-wealth</th><th>n forecasts</th><th>avg log-wealth/forecast</th></tr>
{% for r in wealth_rankings %}
<tr><td>{{ r.model_id }}</td><td>{{ r.category }}</td>
<td>{{ "%.4f"|format(r.cum_log_wealth) }}</td><td>{{ r.n_forecasts }}</td>
<td>{{ "%.5f"|format(r.avg_log_wealth) }}</td></tr>
{% endfor %}
</table>
{% if wealth_curves_plot %}<img src="{{ wealth_curves_plot }}" alt="wealth curves per model per category">{% endif %}
{% if wealth_drawdown_plot %}<img src="{{ wealth_drawdown_plot }}" alt="wealth drawdown per model per category">{% endif %}
{% if wealth_attribution %}
<h3>M4 attribution snapshot (today's pool, linear log-odds)</h3>
<table>
<tr><th>model</th><th>category</th><th>mean contribution</th><th>n markets</th></tr>
{% for r in wealth_attribution %}
<tr><td>{{ r.model_id }}</td><td>{{ r.category }}</td>
<td>{{ "%.4f"|format(r.mean_contribution) }}</td><td>{{ r.n_markets }}</td></tr>
{% endfor %}
</table>
{% endif %}
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no resolved forecasts scored into the wealth ledger yet.</p>
{% endif %}

<h2>Shadow portfolio (SIMULATION only, no real money)</h2>
<p class="note">Entry-filtered, M4-only realistic "would we have traded this" simulation (brief
section 8) -- distinct from the wealth ledger above, which scores every model unconditionally
for comparison power. Uses a real, sourced venue fee schedule
(<code>data/fee_schedule.yaml</code>) plus a slippage haircut; <b>net P&amp;L</b> is realized P&amp;L
after both, <b>gross P&amp;L</b> is before fees, for the net-of-cost comparison (brief section 15).</p>
{% if shadow_summary %}
<table>
<tr><th>bankroll</th><th>resolved trades</th><th>gross P&amp;L</th><th>fees paid</th>
<th>net P&amp;L</th><th>hit rate</th><th>open trades</th><th>unrealized P&amp;L</th>
<th>max drawdown</th></tr>
<tr><td>${{ "%.2f"|format(shadow_summary.bankroll_sim) }}</td>
<td>{{ shadow_summary.resolved_trades }}</td>
<td>${{ "%.2f"|format(shadow_summary.gross_pnl_sim) }}</td>
<td>${{ "%.2f"|format(shadow_summary.total_fees_sim) }}</td>
<td>${{ "%.2f"|format(shadow_summary.realized_pnl_sim) }}</td>
<td>{{ "%.1f%%"|format(shadow_summary.hit_rate * 100) if shadow_summary.hit_rate is not none else "—" }}</td>
<td>{{ shadow_summary.open_trades }}</td>
<td>${{ "%.2f"|format(shadow_summary.unrealized_pnl_sim) }}</td>
<td>${{ "%.2f"|format(shadow_summary.max_drawdown_sim) }}</td></tr>
</table>
{% else %}
<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no shadow trades yet.</p>
{% endif %}

<h2>Lessons digest (trailing quarter)</h2>
{% if lessons.n %}
<p class="note">{{ lessons.n }} post-mortems in the last {{ lessons.window_days }} days.
Miss error sources: {{ lessons.miss_error_sources }}</p>
<ul>{% for note in lessons.sample_notes %}<li class="note">{{ note }}</li>{% endfor %}</ul>
{% else %}<p class="tier-INSUFFICIENT">INSUFFICIENT DATA — no post-mortems yet.</p>{% endif %}

<h2>LLM spend</h2>
<p>Cumulative: ${{ "%.2f"|format(llm_total) }} — today: ${{ "%.2f"|format(llm_today) }}
(daily cap ${{ "%.2f"|format(llm_cap) }})</p>
</body></html>
"""


def latest_eval_rows(conn) -> list[dict]:
    """Most recent eval_runs row per (model, window, venue, category).

    `IS` (not `=`) for venue/category: SQLite's `=` is NULL-unsafe, and a
    legacy pre-Phase-11 row has NULL venue/category -- `IS` groups those
    together correctly instead of dropping them from every join match.
    """
    rows = conn.execute(
        """
        WITH eligible AS (
            SELECT * FROM eval_runs
            WHERE venue IS NOT 'kalshi' OR ts >= :kalshi_cut
        )
        SELECT e.* FROM eligible e
        JOIN (SELECT model_id, window_label, venue, category, MAX(ts) AS ts
              FROM eligible GROUP BY model_id, window_label, venue, category) latest
        ON latest.model_id = e.model_id AND latest.window_label = e.window_label
           AND latest.venue IS e.venue AND latest.category IS e.category
           AND latest.ts = e.ts
        ORDER BY e.model_id, e.venue, e.category, e.window_label
        """,
        {"kalshi_cut": KALSHI_CLUSTERING_FIX_TS},
    ).fetchall()
    return [dict(r) for r in rows]


# Kalshi eval_runs rows written before this date were computed over an inflated
# n: that venue had no event clustering, so each mutually-exclusive leg of one
# question counted as an independent observation (x6.8 measured, x8.8 in H1's
# own >=30-day bucket -- see PAP addendum 9.25). They are superseded rather than
# deleted, since eval_runs is a record of what was computed when -- but the
# report must not present them as current.
#
# Selecting MAX(ts) per combination would retire most of them on the next
# nightly run anyway. The case that needs this filter is the one that would not:
# a (model, venue, category, window) combination that stops being computed
# leaves its last pre-correction row as the newest forever.
KALSHI_CLUSTERING_FIX_TS = "2026-08-26"


def universe_exclusion_counts(conn, days: int = 30) -> list[dict]:
    """Phase 15: daily excluded-market counts by reason_code, from
    universe_log -- answers "why isn't X in the ledger" in aggregate."""
    cutoff = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT date(ts) AS day, reason_code, COUNT(*) AS n
        FROM universe_log
        WHERE ts >= ?
        GROUP BY day, reason_code
        ORDER BY day DESC, reason_code
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def universe_inclusion_counts(conn, days: int = 30) -> list[dict]:
    """Daily count of markets newly added to the forecastable universe
    (tier in liquid/tail), for symmetry with universe_exclusion_counts."""
    cutoff = (now_utc() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT date(first_seen_ts) AS day, tier, COUNT(*) AS n
        FROM markets
        WHERE first_seen_ts >= ? AND tier IN ('liquid', 'tail')
        GROUP BY day, tier
        ORDER BY day DESC, tier
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _phase(name: str) -> None:
    """Log a render phase with current RSS.

    A render takes ~12 minutes on the production host and used to emit nothing
    at all for the whole span. When it started being OOM-killed (2026-07-28,
    after the resolution backlog drained and peak RSS reached ~815MB against
    967MB of RAM) that silence was the main obstacle to diagnosing it -- three
    separate hypotheses had to be measured and discarded before the real cost
    centres were found. A phase line costs nothing and turns the next failure
    into an attribution instead of a guess."""
    try:
        import psutil

        rss_mb = psutil.Process().memory_info().rss / 1e6
    except Exception:
        rss_mb = -1.0
    log.info("report phase", extra={"ctx": {"phase": name, "rss_mb": round(rss_mb)}})


def render_report(conn, store, config: dict[str, Any]) -> Path:
    reports = Path(config["storage"]["reports_dir"])
    reports = reports if reports.is_absolute() else PROJECT_ROOT / reports
    reports.mkdir(parents=True, exist_ok=True)

    _phase("start")
    from lab.collect.status import format_status
    health = format_status(gather_status(config))
    _phase("status")

    universe_log_days = 30
    universe_exclusion_rows = universe_exclusion_counts(conn, universe_log_days)
    universe_inclusion_rows = universe_inclusion_counts(conn, universe_log_days)

    skill_rows = []
    bins_by_model: dict[str, list[dict]] = {}
    for r in latest_eval_rows(conn):
        if r["n_event_clusters"] is not None:
            # Phase 11 row: the event-cluster count is already computed and
            # persisted by eval/run.py's evaluate_model -- no need to redo it.
            n_markets = r["n_event_clusters"]
        else:
            # Legacy pre-Phase-11 row (venue/category NULL): fall back to the
            # old unscoped condition_id count.
            n_markets = len({
                row["condition_id"] for row in conn.execute(
                    """SELECT DISTINCT f.condition_id FROM forecasts f
                       JOIN resolutions res ON res.condition_id = f.condition_id
                       WHERE f.model_id = ?""", (r["model_id"],))
            })
        skill_rows.append({
            "model_id": r["model_id"], "window": r["window_label"],
            "venue": r["venue"] or "(legacy)", "category": r["category"] or "(legacy)",
            "n": r["n"], "n_markets": n_markets,
            "tier": honesty_tier(n_markets, config["eval"]["n_insufficient"],
                                 config["eval"]["n_preliminary"]),
            "brier_model": r["brier"], "brier_market": r["brier_market"],
            "skill": r["skill"], "ci_lo": r["skill_ci_lo"], "ci_hi": r["skill_ci_hi"],
            "mde": float("nan"),
            "skill_pw": r["skill_pw"], "skill_pw_ci_lo": r["skill_pw_ci_lo"],
            "skill_pw_ci_hi": r["skill_pw_ci_hi"], "n_strata_pw": r["n_strata_pw"],
            "cs_lo": r["cs_lo"], "cs_hi": r["cs_hi"], "cs_covers_zero": r["cs_covers_zero"],
            "log_loss": r["log_loss"], "log_loss_market": r["log_loss_market"],
        })
        if r["window_label"] == "all_time" and r["calibration_json"]:
            bins_by_model[r["model_id"]] = json.loads(r["calibration_json"])

    # Recompute MDE inline from stored paired rows (cheap at current scale),
    # scoped to each row's own venue/category and keyed by event-cluster.
    from lab.eval.run import resolved_forecast_rows
    for row, r in zip(skill_rows, latest_eval_rows(conn)):
        if row["window"] != "all_time":
            continue
        venue_filter = r["venue"]
        category_filter = None if r["category"] in (None, "ALL") else r["category"]
        pairs = resolved_forecast_rows(
            conn, row["model_id"], None, venue=venue_filter, category=category_filter
        )
        if len(pairs) < 2:
            continue
        diffs_by_cluster: dict[str, list[float]] = {}
        for p in pairs:
            d = (p["p_market_at_ts"] - p["payout_yes"]) ** 2 - (p["p_yes"] - p["payout_yes"]) ** 2
            cluster_id = p["event_id"] or p["condition_id"]
            diffs_by_cluster.setdefault(cluster_id, []).append(d)
        per_cluster = np.array([np.mean(v) for v in diffs_by_cluster.values()])
        if len(per_cluster) > 1:
            row["mde"] = float(2.8 * np.std(per_cluster, ddof=1) / np.sqrt(len(per_cluster)))

    from lab.learn.pooling import effective_source_count
    from lab.learn.refit import load_active_artifact as _load_active_artifact

    pooling_rows = []
    for pool_label, artifact_key in (("M4 ensemble", "m4_extremization"),
                                     ("M7 cross-venue", "m7_extremization")):
        artifact = _load_active_artifact(config, artifact_key)
        if not artifact:
            continue
        for cat, spec in artifact.get("categories", {}).items():
            n_members = spec.get("n_members_at_fit", 1)
            rho_bar = spec.get("rho_bar", 0.0)
            pooling_rows.append({
                "pool": pool_label, "category": cat, "a": spec["a"], "rho_bar": rho_bar,
                "n_members": n_members, "n_eff": effective_source_count(n_members, rho_bar),
            })

    # Phase 16: RPS is only ever persisted on a row once evaluate_model saw
    # >= min_bucketed_events bucketed events for it -- the gate is already
    # baked into which rows have a non-NULL rps, nothing further to check.
    rps_rows = [{
        "model_id": r["model_id"], "venue": r["venue"] or "(legacy)",
        "category": r["category"] or "(legacy)", "window": r["window_label"],
        "rps_model": r["rps"], "rps_market": r["rps_market"],
        "skill_rps": r["rps_market"] - r["rps"],
    } for r in latest_eval_rows(conn) if r["rps"] is not None]

    _phase("eval_tables")
    calibration_plot = None
    if bins_by_model:
        plot_path = plot_reliability(bins_by_model, reports / "reliability.png")
        calibration_plot = plot_path.name

    # Phase 17 item 4: sticky lab-wide distrust flag -- set by the nightly
    # eval job's clv_validity_check(); persists across report renders until a
    # later check (with enough data to actually re-verify) clears it.
    _phase("calibration_plot")
    from lab.store.db import get_meta
    clv_trusted = get_meta(conn, "clv_trusted") != "0"

    # Phase 17 item 5: gap-aware CLV -- a drift window overlapping a recorded
    # collection gap is excluded rather than silently folded into "no data".
    now = now_utc()
    clv_lookback_days = 30
    gap_dates = [utc_date_str(now - timedelta(days=d)) for d in range(clv_lookback_days + 1)]
    cadence = config["collect"]["snapshot_interval_minutes"]
    clv_gap_windows: list[tuple] = []
    for tier in ("liquid", "tail"):
        tier_ids = [r["condition_id"] for r in conn.execute(
            "SELECT condition_id FROM markets WHERE tier = ?", (tier,))]
        # Per-day read: gap detection needs only this tier's sorted unique
        # timestamps, so the 31-day span never exists in memory at once. It
        # used to, at ~600MB, held right through the next large allocation.
        seen = tier_snapshot_timestamps(store, gap_dates, tier_ids)
        clv_gap_windows.extend(gaps_from_timestamps(
            seen,
            cadence_buckets(cadence[tier], now - timedelta(days=clv_lookback_days), now),
        ))

    _phase("gap_windows")
    clv_rows = []
    clv_dropped_for_gap = 0
    clv_horizons = config["eval"]["clv_horizons_hours"]
    model_ids = [r["model_id"] for r in conn.execute("SELECT DISTINCT model_id FROM forecasts")]
    # Every model's CLV windows fall in the same recent span, so read the
    # snapshot store ONCE for the union of all their needed dates (projected to
    # the 3 columns _mid_at touches) and share it across the per-model loop --
    # was ~1 full read per model_id, the report render's dominant redundant I/O.
    forecasts_by_model = {
        model_id: [dict(r) for r in conn.execute(
            "SELECT ts, condition_id, model_id, p_yes, p_market_at_ts FROM forecasts "
            "WHERE model_id = ? ORDER BY ts DESC LIMIT 2000", (model_id,))]
        for model_id in model_ids
    }
    all_clv_dates: set[str] = set()
    all_clv_cids: set[str] = set()
    for forecasts in forecasts_by_model.values():
        all_clv_dates |= clv_dates(forecasts, clv_horizons)
        all_clv_cids |= {f["condition_id"] for f in forecasts}
    # In chunks of markets, not one read of all of them (2026-09-25). Every
    # render from 2026-09-20 was OOM-killed at exactly this point: RSS went
    # ~260MB -> ~700-800MB on the snapshot read, and the index build on top
    # never finished ("clv_index_built" was never logged). What grew was the
    # snapshot DENSITY for the markets being looked up -- the Kalshi liquid tier
    # reached ~1,800 markets at a 5-minute cadence -- while the read still took
    # every snapshot of every market any model had recently forecast, in one
    # frame. A forecast's drift reads only ITS OWN market's snapshots
    # (`_mid_at` is keyed by condition_id), so scoring market-chunk by
    # market-chunk gives exactly the same drifts, and a plain mean recombines
    # exactly from (sum, n) -- while peak memory follows the chunk, not the
    # archive. `read_range` pushes the condition_id filter into the parquet
    # scan, so each chunk reads only its own rows.
    from lab.eval.clv import chunked_clv_rows

    clv_rows, clv_dropped_for_gap = chunked_clv_rows(
        forecasts_by_model, model_ids, store, sorted(all_clv_dates), clv_horizons,
        gap_windows=clv_gap_windows)
    _phase("clv_snapshots_read")
    _phase("clv_index_built")

    _phase("clv")
    from lab.eval.wealth_plots import (
        m4_attribution_snapshot,
        plot_wealth_curves,
        plot_wealth_drawdown,
        sleeping_expert_rankings,
    )

    wealth_rankings = sleeping_expert_rankings(conn)
    wealth_curves_plot = None
    wealth_drawdown_plot = None
    wealth_attribution: list[dict] = []
    if wealth_rankings:
        curves_path = plot_wealth_curves(conn, config)
        wealth_curves_plot = curves_path.name if curves_path else None
        drawdown_path = plot_wealth_drawdown(conn, config)
        wealth_drawdown_plot = drawdown_path.name if drawdown_path else None
        wealth_attribution = m4_attribution_snapshot(conn, config)

    # Phase 15: shadow-portfolio section -- the net-of-cost skill line
    # (brief section 8/15). Gated on any trade ever existing; portfolio_summary
    # itself is safe to call on an empty shadow_trades table (COALESCE(...,0)).
    _phase("wealth_plots")
    from lab.shadow.portfolio import portfolio_summary as _shadow_portfolio_summary

    shadow_summary = _shadow_portfolio_summary(conn, store, config)
    if shadow_summary["resolved_trades"] == 0 and shadow_summary["open_trades"] == 0:
        shadow_summary = None

    llm_total = conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS t FROM llm_spend").fetchone()["t"]
    from lab.store.db import llm_spend_today
    llm_today = llm_spend_today(conn, utc_date_str(now_utc()))

    from lab.learn.postmortem import lessons_digest

    _phase("shadow")
    html = Environment().from_string(TEMPLATE).render(
        generated_at=now_utc_iso(),
        lessons=lessons_digest(conn),
        health=health,
        universe_log_days=universe_log_days,
        universe_exclusion_rows=universe_exclusion_rows,
        universe_inclusion_rows=universe_inclusion_rows,
        skill_rows=skill_rows,
        rps_rows=rps_rows,
        min_bucketed_events=config["eval"].get("min_bucketed_events", 20),
        pooling_rows=pooling_rows,
        calibration_plot=calibration_plot,
        clv_rows=clv_rows,
        clv_dropped_for_gap=clv_dropped_for_gap,
        clv_trusted=clv_trusted,
        wealth_rankings=wealth_rankings,
        wealth_curves_plot=wealth_curves_plot,
        wealth_drawdown_plot=wealth_drawdown_plot,
        wealth_attribution=wealth_attribution,
        shadow_summary=shadow_summary,
        llm_total=llm_total,
        llm_today=llm_today,
        llm_cap=config["llm"]["daily_cost_cap_usd"],
    )
    out = reports / "report.html"
    out.write_text(html, encoding="utf-8")
    log.info("report rendered", extra={"ctx": {"path": str(out)}})
    return out
