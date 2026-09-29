"""Phase 16 (v2.4): event-distribution assembly for RPS scoring.

Many Kalshi/Polymarket "markets" are one numeric question split into
mutually exclusive buckets (CPI ranges, temperature bands). Scoring each
bucket as an isolated binary discards the cross-bucket structure; this
module assembles the per-model implied distribution over an event's buckets
so `eval/scoring.py::rps` can score the whole shape at once.

What counts as a bucketed event (rewritten 2026-09-29, PAP 9.37). The first
version grouped legs by `markets.event_id` and admitted an event when exactly
one of its FORECAST legs resolved YES and every leg's question held a number.
Each of those three turned out to be wrong in production:

- `event_id` is a clustering key, not a mutual-exclusivity guarantee: Kalshi
  events (linked for clustering in August) and, from 9.33, every multi-market
  Gamma event share it -- cumulative "above X" and "by date" ladders included.
  Only a Polymarket negRisk group is mutually exclusive by construction, so
  only those are admitted.
- "Exactly one forecast leg won" selects on the outcome: a leg priced outside
  the forecast bounds is never forecast, so an event won by such a leg had no
  winner among its forecast legs and dropped out. The distribution must be
  COMPLETE instead -- every leg the event had at forecast time, forecast in
  the same pass -- which is decided before the outcome is known.
- A number in every question is not an order: categorical events whose
  questions share a year ("... in 2026?") parsed to one value per leg and
  were scored in arbitrary order. The parsed values must be distinct.

Events are assembled from exactly the rows a statistic is computed on, so the
RPS on an eval_runs row has that row's venue, category, window, exclusions and
censoring rather than its own. One observation per event: its latest complete
pass. Cross-venue bucket matching remains out of scope.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def parse_bucket_order(question: str | None) -> float | None:
    """First numeric value in a question's text (handles $, %, commas), or
    None if nothing parses -- e.g. "Will CPI be between 3.0% and 3.5%?" -> 3.0.
    """
    if not question:
        return None
    match = _NUMBER_RE.search(question)
    if not match:
        return None
    raw = match.group().lstrip("$").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def implied_cdf(legs_p_yes: list[float]) -> np.ndarray:
    """Renormalize a leg-probability vector (already bucket-ordered) into a
    proper PMF summing to 1 -- the model's implied distribution over
    mutually exclusive buckets. Falls back to a uniform PMF if the legs sum
    to zero or less (degenerate input, never a real forecast in practice).
    """
    p = np.asarray(legs_p_yes, dtype=float)
    total = p.sum()
    if total <= 0:
        return np.full(len(p), 1.0 / len(p))
    return p / total


def coherence_deviation(legs_p_yes: list[float]) -> float:
    """abs(sum(p) - 1) -- the M6 coherence-deviation covariate the phase
    text calls for logging (not scoring) alongside the implied CDF. Same
    quantity `m6_consistency.scan_negrisk_event` already computes; exposed
    here directly so callers don't need to reconstruct legs into that
    function's own {condition_id, p_yes} dict shape just for this number.
    """
    return abs(float(np.asarray(legs_p_yes, dtype=float).sum()) - 1.0)


def negrisk_legs(conn) -> dict[str, dict[str, tuple[str, str]]]:
    """Every Polymarket negRisk leg the market table knows, as
    {event_id: {condition_id: (question, first_seen_ts)}}. Loaded once per
    evaluation run (a few tens of thousands of short rows) rather than per
    statistic."""
    legs: dict[str, dict[str, tuple[str, str]]] = {}
    for r in conn.execute(
        """SELECT event_id, condition_id, question, first_seen_ts FROM markets
           WHERE COALESCE(venue, 'polymarket') = 'polymarket' AND neg_risk = 1
             AND event_id IS NOT NULL"""
    ):
        legs.setdefault(r["event_id"], {})[r["condition_id"]] = (
            r["question"], r["first_seen_ts"] or "")
    return legs


def bucketed_events(rows: list[dict[str, Any]],
                    legs: dict[str, dict[str, tuple[str, str]]],
                    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """RPS observations from `rows` (one model's scored rows for one statistic,
    as `eval/run.py::resolved_forecast_rows` returns them) and the negRisk
    leg map. Returns (events, skipped-counts by reason) -- the counts go into
    one log line per statistic, never one per event.

    A pass (the rows sharing one forecast_ts) is complete when it holds every
    leg the event had by then (`first_seen_ts <= forecast_ts`); an event is
    scored on its latest complete pass, bucket-ordered by the distinct number
    in each leg's question, and must have exactly one winning leg.
    """
    passes: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        if (r.get("venue") or "polymarket") != "polymarket" or not r.get("event_id"):
            continue
        if r["event_id"] not in legs:
            continue
        passes.setdefault((r["event_id"], r["forecast_ts"]), {})[r["condition_id"]] = r

    latest: dict[str, tuple[str, dict[str, dict]]] = {}
    skipped: dict[str, int] = {}
    for (event_id, ts), got in passes.items():
        required = {cid for cid, (_, seen) in legs[event_id].items() if seen <= ts}
        if not required or not required <= got.keys():
            continue
        if event_id not in latest or ts > latest[event_id][0]:
            latest[event_id] = (ts, {cid: got[cid] for cid in required})
    incomplete = len({e for e, _ in passes}) - len(latest)
    if incomplete:
        skipped["no_complete_pass"] = incomplete

    events: list[dict[str, Any]] = []
    for event_id in sorted(latest):
        _, by_cid = latest[event_id]
        cids = sorted(by_cid)
        orders = [parse_bucket_order(legs[event_id][cid][0]) for cid in cids]
        if any(o is None for o in orders) or len(set(orders)) != len(orders):
            skipped["unordered"] = skipped.get("unordered", 0) + 1
            continue
        winners = [cid for cid in cids if by_cid[cid]["payout_yes"] == 1.0]
        if len(winners) != 1:
            skipped["not_one_winner"] = skipped.get("not_one_winner", 0) + 1
            continue
        ordered = [cids[i] for i in np.argsort(orders, kind="stable")]
        events.append({
            "event_id": event_id,
            "category": by_cid[ordered[0]].get("category"),
            "p_model": [by_cid[c]["p_yes"] for c in ordered],
            "p_market": [by_cid[c]["p_market_at_ts"] for c in ordered],
            "y_bucket_idx": ordered.index(winners[0]),
            "condition_ids": ordered,
            "resolved_ts": max(by_cid[c]["resolved_ts"] for c in ordered),
        })
    return events, skipped
