"""M6 -- cross-market coherence scanner. Deterministic, no LLM.

Within a negRisk event the mutually exclusive YES prices must sum to ~1.
The scanner records every event's deviation and emits standalone forecasts
only for incoherent legs, pulling them toward the coherent joint solution
(price / sum). Pairwise logical links come from a curated links.yaml
(starts empty).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 0.03   # |sum - 1| beyond this flags the event
LEG_MIN_SHIFT = 0.01         # emit a forecast only when the leg moves this much


def scan_negrisk_event(legs: list[dict[str, float | str]]) -> dict[str, Any]:
    """legs: [{condition_id, p_yes}, ...] for one negRisk event.

    Returns the deviation record and per-leg coherent prices when incoherent.
    """
    total = sum(float(l["p_yes"]) for l in legs)
    deviation = total - 1.0
    result: dict[str, Any] = {
        "n_legs": len(legs),
        "sum_p_yes": total,
        "deviation": deviation,
        "incoherent": abs(deviation) > DEVIATION_THRESHOLD and len(legs) >= 2,
        "corrections": [],
    }
    if result["incoherent"] and total > 0:
        for l in legs:
            p = float(l["p_yes"])
            coherent = p / total
            if abs(coherent - p) >= LEG_MIN_SHIFT:
                result["corrections"].append({
                    "condition_id": l["condition_id"],
                    "p_market": p,
                    "p_coherent": coherent,
                    "spread": l.get("spread"),
                })
    return result


def fresh_prices(conn, latest, config: dict[str, Any], now) -> dict[str, dict[str, Any]]:
    """{condition_id: {p_yes, spread}} for the latest snapshots guardrail 13
    accepts -- within the market's tier bound, the tail bound for a market in
    no snapshotted tier. Until 2026-10-11 M6 took any mid from the last two
    days, so a leg could be priced, and a forecast paired, a day stale."""
    from datetime import datetime, timezone

    max_age = config["forecast"]["max_snapshot_age_minutes"]
    tiers = {r[0]: r[1] for r in conn.execute(
        "SELECT condition_id, tier FROM markets WHERE tier IN ('liquid', 'tail')")}
    out: dict[str, dict[str, Any]] = {}
    for r in latest.to_dicts():
        if r["mid"] is None:
            continue
        ts = datetime.fromisoformat(r["ts"])
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        bound = max_age.get(tiers.get(r["condition_id"]), max_age["tail"])
        if (now - ts).total_seconds() / 60 <= bound:
            out[r["condition_id"]] = {"p_yes": r["mid"], "spread": r.get("spread")}
    return out


def load_links(path: Path | None = None) -> list[dict[str, Any]]:
    """Curated pairwise logical constraints; empty file at start."""
    p = path or PROJECT_ROOT / "links.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("links", [])


def check_link(link: dict[str, Any], prices: dict[str, float]) -> dict[str, Any] | None:
    """Conditional-probability bound: P(narrow) <= P(broad) for implies-links.

    link: {kind: 'implies', narrow: condition_id, broad: condition_id}
    (if the narrow event happens, the broad one must too).
    """
    if link.get("kind") != "implies":
        return None
    p_narrow = prices.get(link["narrow"])
    p_broad = prices.get(link["broad"])
    if p_narrow is None or p_broad is None:
        return None
    violation = p_narrow - p_broad
    if violation <= DEVIATION_THRESHOLD:
        return None
    midpoint = (p_narrow + p_broad) / 2
    return {
        "link": link,
        "violation": violation,
        "corrections": [
            {"condition_id": link["narrow"], "p_market": p_narrow, "p_coherent": midpoint},
            {"condition_id": link["broad"], "p_market": p_broad, "p_coherent": midpoint},
        ],
    }


async def scan_universe(conn, store, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Group active negRisk markets into events via Gamma and scan each.

    Event grouping is fetched live (the markets table intentionally follows
    the brief's schema, which has no event column).
    """
    from lab.api.gamma import GammaClient
    from lab.api.http import TokenBucket
    from lab.store.snapshots import utc_date_str
    from datetime import timedelta
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    if latest.is_empty():
        return []
    fresh = fresh_prices(conn, latest, config, now)

    bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                        burst=config["collect"]["rate_limit"]["burst"])
    gamma = GammaClient(bucket)
    findings: list[dict[str, Any]] = []
    incomplete = 0
    try:
        raw = await gamma.get_json("/events", params={
            "limit": 100, "offset": 0, "active": "true", "closed": "false",
            "order": "volume", "ascending": "false",
        })
        events = raw if isinstance(raw, list) else raw.get("data", [])
        for ev in events:
            if not ev.get("negRisk"):
                continue
            # Every open leg, or no scan (2026-10-11). The sum runs over the
            # legs that can still win; a leg missing from it -- unpriced, or
            # priced too long ago for guardrail 13 -- pulls the sum below 1 by
            # its own probability, which read as incoherence and pushed every
            # other leg up.
            open_legs = [m.get("conditionId") for m in ev.get("markets", [])
                         if m.get("active", True) and not m.get("closed", False)]
            if any(cid not in fresh for cid in open_legs):
                incomplete += 1
                continue
            legs = [{"condition_id": cid, **fresh[cid]} for cid in open_legs]
            if len(legs) >= 2:
                scan = scan_negrisk_event(legs)
                scan["event_slug"] = ev.get("slug")
                findings.append(scan)
    finally:
        await gamma.aclose()

    prices = {cid: v["p_yes"] for cid, v in fresh.items()}
    for link in load_links():
        v = check_link(link, prices)
        if v:
            for corr in v["corrections"]:
                corr["spread"] = fresh[corr["condition_id"]]["spread"]
            findings.append({"kind": "link_violation", **v})

    incoherent = [f for f in findings if f.get("incoherent") or f.get("violation")]
    log.info("m6 scan complete",
             extra={"ctx": {"events": len(findings), "incoherent": len(incoherent),
                            "skipped_incomplete_or_stale": incomplete}})
    return findings


def write_m6_forecasts(conn, store, findings: list[dict[str, Any]],
                       config: dict[str, Any]) -> int:
    """Append ledger rows for incoherent legs, pulled toward coherence.

    Each row is paired against the price its own coherent value was computed
    from (the scan's fresh price), not a re-read of the archive minutes later
    -- the same rule M4 has followed since 2026-08-18."""
    from lab.store import db as dbmod
    from lab.util import now_utc

    now = now_utc()
    ts = now.isoformat(timespec="seconds")
    lo, hi = config["forecast"]["p_clamp"]

    # §3's null-control restriction is a UNIVERSE policy, so it has to hold on
    # every write path, not just the one inside eligible_market_states. Until
    # 2026-08-22 it held only there, and this loop wrote on 62 sports markets
    # against a configured sample of 30.
    from lab.forecast import _due, drop_null_control_outsiders

    allowed = drop_null_control_outsiders(
        conn, config,
        [c["condition_id"] for f in findings for c in f.get("corrections", [])])

    written = skipped_null_control = not_due = 0
    for finding in findings:
        for corr in finding.get("corrections", []):
            if corr["condition_id"] not in allowed:
                skipped_null_control += 1
                continue
            # Once per market per calendar day, as every other model (PAP 9.38,
            # 9.44): without this a second pass the same day -- any catch-up --
            # wrote M6 again (30, 43 and 37 markets on 10-07, 10-09, 10-11).
            if not _due(conn, corr["condition_id"], "m6_consistency", config, None, now=now):
                not_due += 1
                continue
            dbmod.append_forecast(conn, {
                "ts": ts,
                "condition_id": corr["condition_id"],
                "model_id": "m6_consistency",
                "p_yes": min(hi, max(lo, corr["p_coherent"])),
                "p_market_at_ts": corr["p_market"],
                "spread_at_ts": corr.get("spread"),
            })
            written += 1
    if skipped_null_control:
        log.info("m6: skipped sports markets outside the null-control sample",
                 extra={"ctx": {"count": skipped_null_control}})
    if not_due:
        log.info("m6: skipped markets already forecast today", extra={"ctx": {"count": not_due}})
    conn.commit()
    return written
