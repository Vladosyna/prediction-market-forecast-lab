"""M7 -- cross-venue signal on matched questions (brief section 6, Phase 9).

For a market with a confirmed external match, output the log-odds pool of the
*external* venues' probabilities only -- Polymarket's own price stays out (M0
already carries it; the ensemble learns how much to trust each source).
Deterministic at forecast time: no LLM call in this path. The LLM only
proposes candidate matches (`propose_matches`, used by `lab map propose`); a
human confirms every pair in data/markets_map.yaml before it goes live -- a
proposed-but-unconfirmed pair is never read by the forecasting path at all.

Like M6, this bypasses the per-market Forecaster.forecast() loop (it needs
async I/O against Kalshi/Metaculus) -- scan_confirmed_pairs() does the async
fetch, write_m7_forecasts() is the sync ledger writer, mirroring
m6_consistency.py's scan_universe()/write_m6_forecasts() split.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from lab.learn.refit import logit, sigmoid
from lab.models.base import ForecastResult, clamp_p
from lab.gitutil import push_with_rebase
from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

DEFAULT_MAP_PATH = PROJECT_ROOT / "data" / "markets_map.yaml"
DEFAULT_PMXT_CANDIDATES_PATH = PROJECT_ROOT / "data" / "pmxt_candidates.json"


def load_markets_map(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_MAP_PATH
    if not p.exists():
        return {"confirmed": [], "proposed": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("confirmed", [])
    data.setdefault("proposed", [])
    return data


def save_markets_map(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_MAP_PATH
    header = (
        "# Cross-venue question matching (M7, Phase 9). Propose-then-confirm:\n"
        "# `lab map propose` appends LLM candidates under `proposed`; a human\n"
        "# moves a pair into `confirmed` (via `lab map confirm`) to make it live.\n"
        "# M7 reads ONLY `confirmed`. This file is the source of truth.\n"
    )
    p.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def commit_and_push_markets_map(config: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Commit (and push) data/markets_map.yaml if it has uncommitted changes.

    Needed once more than one host can append to this file (e.g. a pmxt-scan
    host distinct from the host(s) that read it at forecast time) --
    `verify_pmxt_candidates` already durably writes the new proposals to disk
    before this is ever called, so unlike ledger_commitment.py/paper_export.py
    there is nothing to revert on failure: a failed commit just leaves the
    same uncommitted change in place for the next call (or a human) to retry,
    it can never lose or duplicate a proposal.
    """
    p = path or DEFAULT_MAP_PATH
    rel_path = str(p.relative_to(PROJECT_ROOT))
    status = _run_git(["status", "--porcelain", "--", rel_path], PROJECT_ROOT)
    if not status.stdout.strip():
        return {"committed": False, "reason": "no_changes"}

    try:
        add = _run_git(["add", rel_path], PROJECT_ROOT)
        if add.returncode != 0:
            return {"error": "git_add_failed", "stderr": add.stderr}
        commit = _run_git(["commit", "-m", "M7: pmxt-verified candidate pairs proposed"], PROJECT_ROOT)
        if commit.returncode != 0:
            return {"error": "git_commit_failed", "stderr": commit.stderr}
    except Exception as exc:
        log.exception("markets_map commit step failed")
        return {"error": "git_step_exception", "detail": str(exc)}

    result: dict[str, Any] = {"committed": True}
    if config.get("cross_venue", {}).get("markets_map_push", True):
        try:
            pushed = push_with_rebase(PROJECT_ROOT)
            result["pushed"] = pushed.returncode == 0
            if not result["pushed"]:
                result["push_stderr"] = pushed.stderr
        except Exception as exc:
            result["pushed"] = False
            result["push_error"] = str(exc)
    return result


def confirmed_by_condition(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """condition_id -> list of confirmed {venue, external_id, ...} entries."""
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in data.get("confirmed", []):
        out.setdefault(entry["condition_id"], []).append(entry)
    return out


def confirm_match(data: dict[str, Any], condition_id: str, venue: str,
                  external_id: str | None = None) -> bool:
    """Move a proposed entry into confirmed, or (external_id given) confirm a
    hand-curated pair directly -- e.g. Metaculus, which `propose` can't reach
    (see api/metaculus.py). Returns False if there's nothing to confirm.

    Idempotent: re-confirming the same (condition_id, venue) is a no-op.
    """
    already = any(e["condition_id"] == condition_id and e["venue"] == venue
                  for e in data.get("confirmed", []))
    if already:
        return True

    proposed = data.get("proposed", [])
    match_idx = next(
        (i for i, e in enumerate(proposed)
         if e["condition_id"] == condition_id and e["venue"] == venue
         and (external_id is None or e["external_id"] == external_id)),
        None,
    )
    if match_idx is not None:
        entry = proposed.pop(match_idx)
    elif external_id is not None:
        entry = {"condition_id": condition_id, "venue": venue, "external_id": external_id}
    else:
        return False

    entry.pop("rationale", None)
    entry.pop("confidence", None)
    entry.pop("proposed_ts", None)
    entry["confirmed_ts"] = now_utc_iso()
    data.setdefault("confirmed", []).append(entry)
    return True


def reject_match(data: dict[str, Any], condition_id: str, venue: str, external_id: str) -> bool:
    """Remove a proposed entry without confirming it -- the human's other
    verdict besides confirm_match. A real, observed need: the LLM sometimes
    proposes a pair whose OWN rationale says the events don't actually match
    (e.g. two different offices/years) yet still returns it with high
    confidence -- rejecting it here is exactly why every pair is confirmed by
    a human rather than auto-promoted. Returns False if there's nothing to
    remove (already rejected/confirmed, or never proposed)."""
    proposed = data.get("proposed", [])
    match_idx = next(
        (i for i, e in enumerate(proposed)
         if e["condition_id"] == condition_id and e["venue"] == venue
         and e["external_id"] == external_id),
        None,
    )
    if match_idx is None:
        return False
    proposed.pop(match_idx)
    return True


def backfill_kalshi_metadata(conn, ticker: str,
                             config: dict[str, Any] | None = None) -> bool:
    """Fetch a confirmed Kalshi market's real question and resolution rules.

    `db.link_event` deliberately upserts a bare placeholder row for a side its
    venue's own collector hasn't synced, so the link always succeeds -- on the
    assumption the collector fills it in later. For confirmed pairs it never
    does: per-candidate legs and foreign elections sit outside the series the
    Kalshi universe sync walks, so 24 of 26 confirmed counterparts still had
    `question = NULL` three weeks on (2026-07-28).

    That is not cosmetic. Auditing a pair means comparing resolution criteria,
    and with the row empty there was nothing to compare against but the title
    a third-party matcher supplied -- which is exactly the field that proved
    misleading. Two genuinely wrong pairs (Steyer/Becerra, McConnell's
    mismatched deadline) were invisible until this metadata was filled in, and
    both were caught by Kalshi's own `rules_primary` text. Fetching it at
    confirm time is what keeps a pair checkable.

    Best-effort: returns False and leaves the placeholder alone on any failure
    (guardrail 9 -- a confirmation must not fail because Kalshi is briefly
    unreachable), and never overwrites a row the collector already populated.
    """
    import asyncio

    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.store import db as dbmod
    from lab.util import now_utc_iso

    cid = dbmod.venue_condition_id("kalshi", ticker)
    # Gate on `category`, not `question`. Until 2026-08-22 this wrote four
    # columns with a bare UPDATE -- question, description, end_date_iso,
    # last_synced_ts -- and left category NULL, active 0 and tier 'ignored'
    # on the placeholder row. `tracked_kalshi_markets` selects on
    # `active = 1`, so the Kalshi leg of such a pair was never snapshotted
    # and M7 could never use it: 113 of 192 confirmed Kalshi pairs (59%) were
    # in that state. The old guard also made it permanent -- once the partial
    # write had set `question`, every later call returned early. Gating on
    # `category` instead means a genuinely collector-synced row is still left
    # alone, while a stub gets completed.
    row = conn.execute("SELECT category FROM markets WHERE condition_id = ?", (cid,)).fetchone()
    if row is not None and row["category"]:
        return False  # already synced by the collector -- leave it alone

    async def _fetch():
        client = KalshiClient(TokenBucket(rate=4, burst=8))
        try:
            return await client.market(ticker)
        finally:
            await client.aclose()

    try:
        market = asyncio.run(_fetch())
    except Exception:
        log.warning("m7: kalshi metadata backfill failed",
                    extra={"ctx": {"ticker": ticker}})
        return False
    if market is None:
        log.warning("m7: kalshi market not found for backfill",
                    extra={"ctx": {"ticker": ticker}})
        return False

    # Write the SAME full row the collector writes, through the same builder,
    # rather than a hand-listed subset of columns. That is what keeps the two
    # write paths from drifting apart again -- the drift is what produced the
    # stubs in the first place.
    from lab.collect.kalshi_collector import assign_kalshi_tier, kalshi_market_row
    from lab.util import load_config

    cfg = config if config is not None else load_config()
    tier, _reason = assign_kalshi_tier(market, cfg)
    dbmod.upsert_market(conn, {**kalshi_market_row(market, _category_for_ticker(conn, ticker)),
                               "tier": tier})
    conn.commit()
    log.info("m7: completed a confirmed pair's kalshi leg",
             extra={"ctx": {"ticker": ticker, "tier": tier}})
    return True


def _category_for_ticker(conn, ticker: str) -> str:
    """Category for a single Kalshi ticker, without spending a request.

    The universe sync learns a market's category from the Kalshi CATEGORY it
    was discovered under (`series_by_category`), which a per-ticker fetch does
    not tell us -- KalshiMarket carries no category field. Siblings do: every
    ticker starts with its series, so a market from the same series that the
    collector has already categorized answers it exactly. Falling back to the
    taxonomy's own "unknown" rather than NULL is deliberate: NULL is what
    crashed `fit_m2_baserates` on 2026-08-22, and "unknown" is a category the
    rest of the pipeline already understands."""
    from lab.collect.categories import _FALLBACK

    series = ticker.split("-")[0]
    row = conn.execute(
        "SELECT category FROM markets WHERE venue = 'kalshi' AND category IS NOT NULL "
        "AND venue_native_id LIKE ? LIMIT 1", (series + "-%",),
    ).fetchone()
    return (row["category"] if row else None) or _FALLBACK


def repair_confirmed_kalshi_legs(conn, config: dict[str, Any], limit: int = 25,
                                path: Path | None = None) -> dict[str, Any]:
    """Complete the Kalshi leg of already-confirmed pairs that were left as stubs.

    `backfill_kalshi_metadata` used to write four columns and leave category
    NULL / active 0, and its own early-return then made that permanent. The
    result on 2026-08-22: 113 of 192 confirmed Kalshi pairs had a leg the
    collector never snapshotted (`tracked_kalshi_markets` selects on
    `active = 1`), so M7 could not use them -- it was covering 101 markets a
    day against 192 confirmed pairs. Fixing the write path stops new stubs;
    this repairs the ones already recorded.

    Bounded per call and safe to repeat: a leg the collector has genuinely
    synced is skipped by the same `category` gate, so once a pair is whole it
    costs nothing. `limit` keeps a single run polite -- one request per
    repaired leg, and the backlog drains over successive runs rather than in
    one burst."""
    data = load_markets_map(path)
    tickers = [e["external_id"] for e in (data.get("confirmed") or [])
               if e.get("venue") == "kalshi" and e.get("external_id")]
    if not tickers:
        return {"checked": 0, "repaired": 0}

    placeholders = ",".join("?" * len(tickers))
    incomplete = [r["venue_native_id"] for r in conn.execute(
        f"SELECT venue_native_id FROM markets WHERE venue = 'kalshi' "
        f"AND category IS NULL AND venue_native_id IN ({placeholders})", tuple(tickers))]

    repaired = 0
    for ticker in incomplete[:limit]:
        try:
            if backfill_kalshi_metadata(conn, ticker, config):
                repaired += 1
        except Exception:
            # Guardrail 9: one unreachable market never stops the pass.
            log.warning("m7: confirmed-leg repair failed",
                        extra={"ctx": {"ticker": ticker}})
    result = {"checked": len(tickers), "incomplete": len(incomplete), "repaired": repaired}
    if incomplete:
        log.info("m7: repaired confirmed pairs' kalshi legs", extra={"ctx": result})
    return result


def link_confirmed_event(conn, condition_id: str, venue: str, external_id: str) -> str:
    """Mint (or reuse) the event_id linking a Polymarket market to a confirmed
    external venue-market (brief section 5/Phase 10: "a confirmed match
    creates an event linking >=2 venue-markets"). Best-effort title from the
    Polymarket market's own question, if it's already synced.

    Also fills in the external side's own question/rules where the venue
    collector won't -- see backfill_kalshi_metadata for why a placeholder left
    empty makes the pair unauditable."""
    from lab.store import db as dbmod

    external_cid = dbmod.venue_condition_id(venue, external_id)
    row = conn.execute(
        "SELECT question FROM markets WHERE condition_id = ?", (condition_id,)
    ).fetchone()
    title = row["question"] if row else None
    event_id = dbmod.link_event(conn, condition_id, external_cid, title=title)
    if venue == "kalshi":
        backfill_kalshi_metadata(conn, external_id)
    return event_id


def _pair_horizon_bucket(end_date_iso: str | None, now: datetime) -> str | None:
    """Horizon bucket for the Polymarket side of a confirmed pair, used to pick
    which m1_hier_curves bucket recalibrates the Metaculus quote."""
    from lab.learn.refit import bucket_for_days

    if not end_date_iso:
        return None
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    days = max(0.0, (end - now).total_seconds() / 86400)
    return bucket_for_days(days)


def pool_log_odds(prices: list[float], a_eff: float = 1.0) -> float:
    """Deterministic log-odds average of external venue probabilities,
    optionally extremized by a correlation-discounted exponent (Phase 13,
    CLAUDE.md M4/M7 extremization). a_eff=1.0 (the default) is a bit-exact
    identity -- current pooling behavior, unchanged."""
    if not prices:
        raise ValueError("pool_log_odds requires at least one price")
    raw_logit = sum(logit(p) for p in prices) / len(prices)
    return float(sigmoid(a_eff * raw_logit))


async def scan_confirmed_pairs(conn, store, config: dict[str, Any],
                               markets_map_path: Path | None = None,
                               ) -> dict[str, ForecastResult]:
    """Async fetch: for every confirmed pair with a fresh own-price snapshot,
    pull each venue's current quote and pool them. Abstains per-market on a
    stale snapshot (guardrail 13) or when no venue returns a usable quote."""
    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.api.metaculus import MetaculusClient
    from lab.learn.pooling import discount_extremization_exponent
    from lab.learn.refit import load_active_artifact
    from lab.models.m1_hier import apply_hier_curve
    from lab.store.snapshots import utc_date_str

    data = load_markets_map(markets_map_path)
    by_cid = confirmed_by_condition(data)
    if not by_cid:
        return {}

    now = datetime.now(timezone.utc)
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(2)]
    latest = store.latest_per_market(dates)
    snap_by_cid = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}
    market_by_cid = {
        r["condition_id"]: r
        for r in conn.execute(
            "SELECT condition_id, tier, end_date_iso FROM markets WHERE condition_id IN ({})".format(
                ",".join("?" for _ in by_cid)
            ),
            list(by_cid),
        )
    } if by_cid else {}
    max_age = config["forecast"]["max_snapshot_age_minutes"]
    hier_artifact = load_active_artifact(config, "m1_hier_curves")
    ext_artifact = load_active_artifact(config, "m7_extremization")
    ext_spec = (ext_artifact or {}).get("categories", {}).get("_all")

    bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                        burst=config["collect"]["rate_limit"]["burst"])
    kalshi = KalshiClient(bucket)
    metaculus = MetaculusClient(bucket)
    results: dict[str, ForecastResult] = {}
    try:
        for cid, pairs in by_cid.items():
            snap = snap_by_cid.get(cid)
            market = market_by_cid.get(cid)
            tier = market["tier"] if market else None
            if snap is None or snap["mid"] is None or tier is None:
                continue
            snap_ts = datetime.fromisoformat(snap["ts"])
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.replace(tzinfo=timezone.utc)
            age_min = (now - snap_ts).total_seconds() / 60
            if age_min > max_age.get(tier, max_age["tail"]):
                log.warning("m7: skipping stale-snapshot market",
                           extra={"ctx": {"condition_id": cid, "age_min": age_min}})
                continue
            horizon_bucket = _pair_horizon_bucket(market["end_date_iso"], now) if market else None

            quotes: list[dict[str, Any]] = []
            for pair in pairs:
                price = None
                recalibrated = False
                if pair["venue"] == "kalshi":
                    m = await kalshi.market(pair["external_id"])
                    price = m.yes_price if m else None
                elif pair["venue"] == "metaculus":
                    q = await metaculus.question(int(pair["external_id"]))
                    price = q.community_prediction if q else None
                    # M1.x input signal (Phase 12, CLAUDE.md M1.x): recalibrate
                    # the raw community prediction through the metaculus venue
                    # offset before pooling -- Metaculus is never a forecast
                    # target itself, only an M7 input. Falls back to the raw CP
                    # unchanged when no artifact/bucket fit exists yet.
                    if (price is not None and hier_artifact is not None and horizon_bucket is not None
                            and horizon_bucket in hier_artifact.get("buckets", {})):
                        price = apply_hier_curve(hier_artifact, "metaculus", horizon_bucket, price)
                        recalibrated = True
                if price is not None and 0 < price < 1:
                    quotes.append({
                        "venue": pair["venue"], "external_id": pair["external_id"],
                        "price": price, "fetched_ts": now_utc_iso(),
                        "recalibrated": recalibrated,
                    })
            if not quotes:
                continue
            # Phase 13: correlation-discounted extremization, using the ACTUAL
            # number of venues pooled for THIS market (n=len(quotes)), not the
            # frozen count from fit time -- n_eff wants today's real pool size.
            a_raw = ext_spec["a"] if ext_spec else 1.0
            rho_bar = ext_spec.get("rho_bar", 0.0) if ext_spec else 0.0
            a_eff = discount_extremization_exponent(a_raw, n=len(quotes), rho_bar=rho_bar)
            pooled = pool_log_odds([q["price"] for q in quotes], a_eff=a_eff)
            results[cid] = ForecastResult(
                p_yes=clamp_p(pooled),
                meta={"quotes": quotes, "n_pooled": len(quotes),
                      "extremization_a_eff": a_eff, "extremization_rho_bar": rho_bar},
            )
    finally:
        await kalshi.aclose()
        await metaculus.aclose()
    log.info("m7 scan complete", extra={"ctx": {"confirmed_pairs": len(by_cid),
                                                "forecasts": len(results)}})
    return results


def write_m7_forecasts(conn, store, results: dict[str, ForecastResult],
                       config: dict[str, Any]) -> int:
    """Append ledger rows for every market M7 produced a pooled quote for."""
    from datetime import timedelta

    from lab.store import db as dbmod
    from lab.store.snapshots import utc_date_str
    from lab.util import now_utc

    now = now_utc()
    latest = store.latest_per_market([utc_date_str(now - timedelta(days=d)) for d in range(2)])
    snap = {r["condition_id"]: r for r in latest.to_dicts()} if not latest.is_empty() else {}
    ts = now.isoformat(timespec="seconds")

    # Same universe policy as M6 (§3). It bites harder here: M7's markets come
    # from human-confirmed pairs, so before 2026-08-22 an editorially selected
    # set was landing on the null control -- the selection guardrail 12 exists
    # to forbid. 31 sports markets in the week to that date, 29 of them never
    # produced by the filtered path.
    from lab.forecast import drop_null_control_outsiders

    allowed = drop_null_control_outsiders(conn, config, list(results))
    written = skipped_null_control = 0
    for cid, result in results.items():
        if cid not in allowed:
            skipped_null_control += 1
            continue
        row = snap.get(cid)
        if row is None:
            continue
        dbmod.append_forecast(conn, {
            "ts": ts,
            "condition_id": cid,
            "model_id": "m7_crossvenue",
            "p_yes": result.p_yes,
            "p_market_at_ts": row["mid"],
            "spread_at_ts": row["spread"],
        })
        log.info("m7 forecast", extra={"ctx": {"condition_id": cid, "p_yes": result.p_yes,
                                                **result.meta}})
        written += 1
    if skipped_null_control:
        log.info("m7: skipped sports markets outside the null-control sample",
                 extra={"ctx": {"count": skipped_null_control}})
    conn.commit()
    return written


async def kalshi_propose_candidates(kalshi, config: dict[str, Any]) -> dict[str, list[Any]]:
    """Category-scoped Kalshi candidate pools for `lab map propose`, keyed by
    OUR internal category (economics/weather/politics/...), not returned as
    one flat list.

    A bare `open_markets(limit=200)` (no filter) pulls whatever Kalshi
    considers "open" globally -- verified live to be dominated by garbled
    multi-leg sports/esports combo products (KXMVE... tickers), crowding out
    the handful of real Economics/Politics/Weather markets Kalshi actually
    lists. Reuses the SAME series_by_category -> markets_for_series flow
    collect/kalshi_collector.py already uses for universe sync, scoped to
    only the Kalshi category names whose categories.yaml mapping lands in
    our own priority_categories (brief section 3 P1-P4).

    Returning a flat list (as this function originally did) meant
    propose_matches showed EVERY Polymarket market the ENTIRE candidate pool
    regardless of category -- a weather market got shown politics and
    entertainment candidates too, diluting the prompt with obviously-
    irrelevant options and wasting most of each call's tokens. Keying by our
    own category lets propose_matches show each market only its own
    category's candidates (see there).

    Uses its own `propose_series_per_category` budget rather than
    venues.kalshi.max_series_per_sync -- that config is tuned for the hourly
    universe-sync's politeness budget; propose runs weekly (propose_cron)
    and can afford to look at more series per category.
    """
    from lab.collect.categories import load_categories

    taxonomy = load_categories()
    priority = set(config["universe"]["priority_categories"])
    kalshi_to_ours = {k: v for k, v in taxonomy.get("kalshi_series", {}).items() if v in priority}
    per_cat_series = int(config["cross_venue"].get("propose_series_per_category", 40))

    candidates: dict[str, list[Any]] = {cat: [] for cat in priority}
    for kalshi_category, our_category in kalshi_to_ours.items():
        try:
            series_list = await kalshi.series_by_category(kalshi_category)
        except Exception:
            log.warning("m7 propose: series fetch failed",
                       extra={"ctx": {"category": kalshi_category}})
            continue
        series_seen = 0
        for s in series_list:
            if series_seen >= per_cat_series:
                break
            ticker = s.get("ticker")
            if not ticker:
                continue
            series_seen += 1
            try:
                markets = await kalshi.markets_for_series(ticker, status="open")
            except Exception:
                log.warning("m7 propose: markets fetch failed",
                           extra={"ctx": {"series_ticker": ticker}})
                continue
            candidates[our_category].extend(markets)
    return candidates


PROPOSE_SYSTEM = """You match prediction-market questions across venues for a research pipeline.
Given ONE Polymarket question and a list of candidate Kalshi markets, identify
which candidates (if any) ask about the SAME real-world event with the SAME
resolution criteria -- not just a similar topic. Respond ONLY with a JSON
object: {"matches": [{"external_id": str, "confidence": float 0.0-1.0, "rationale": str}]}.
Return {"matches": []} if nothing qualifies. Be conservative: a wrong match is
worse than a missed one."""


def _propose_prompt(question: str, candidates: list[dict[str, str]]) -> str:
    lines = [f"POLYMARKET QUESTION: {question}", "", "CANDIDATE KALSHI MARKETS:"]
    for c in candidates:
        lines.append(f"- external_id={c['external_id']}: {c['title']}")
    return "\n".join(lines)


def propose_matches(conn, config: dict[str, Any], kalshi_candidates, llm,
                    markets_map_path: Path | None = None,
                    ) -> list[dict[str, Any]]:
    """LLM proposes candidate Kalshi matches for our top-K priority-category,
    liquid-tier markets. Metaculus is not reachable without an account (see
    api/metaculus.py) so `propose` only covers Kalshi; a human can still
    `lab map confirm` a hand-found Metaculus pair directly.

    Deterministic aggregation is not applicable here (there's no numeric
    signal to aggregate) -- the LLM's judgment on MATCH IDENTITY is the
    product itself; a human confirms every one before it's live, same as any
    other high-stakes LLM output in this codebase.
    """
    import json

    data = load_markets_map(markets_map_path)
    already = {(e["condition_id"], e["venue"]) for e in data.get("confirmed", []) + data.get("proposed", [])}

    cats = config["universe"]["priority_categories"]
    top_k = int(config["cross_venue"]["propose_top_k"])
    # Per-category share, not one global ORDER BY volume_num DESC LIMIT top_k
    # across all priority categories combined -- verified live that a single
    # voluminous negRisk event (hundreds of "will [name] win 2028" legs) can
    # swamp every slot in a global top-K, leaving weather/economics/
    # geopolitics/entertainment zero representation regardless of how the
    # legs are ranked. An even per-category share guarantees every priority
    # category gets a fair shot at being proposed.
    per_cat = max(1, top_k // len(cats))
    # Distinct EVENTS, not raw rows: verified live that even within one
    # category, one negRisk event's legs (e.g. 5 mutually exclusive "Fed
    # hikes/cuts/holds after the July meeting" buckets) can fill the whole
    # per-category share by themselves, so per_cat slots go to 5 variants of
    # one question instead of covering distinct real-world topics. Oversample
    # by volume, then dedupe by event_id (fallback condition_id) in order.
    oversample = per_cat * 5
    rows: list[Any] = []
    for cat in cats:
        raw = conn.execute(
            """
            SELECT condition_id, question, event_id, category FROM markets
            WHERE tier = 'liquid' AND category = ? AND active = 1 AND closed = 0
            ORDER BY volume_num DESC LIMIT ?
            """,
            (cat, oversample),
        ).fetchall()
        seen_events: set[str] = set()
        for m in raw:
            key = m["event_id"] or m["condition_id"]
            if key in seen_events:
                continue
            seen_events.add(key)
            rows.append(m)
            if len(seen_events) >= per_cat:
                break

    proposals: list[dict[str, Any]] = []
    for m in rows:
        if (m["condition_id"], "kalshi") in already or not m["question"]:
            continue
        # Only THIS market's own category's Kalshi candidates -- showing a
        # weather market politics/entertainment candidates too was diluting
        # the LLM's judgment and burning tokens on obviously-irrelevant
        # options (kalshi_candidates is keyed by our internal category, see
        # kalshi_propose_candidates).
        cat_candidates = kalshi_candidates.get(m["category"], [])
        candidates = [{"external_id": k.ticker, "title": k.title or ""} for k in cat_candidates]
        if not candidates:
            continue
        text, _usage = llm.complete(
            PROPOSE_SYSTEM, _propose_prompt(m["question"], candidates), purpose="m7_propose",
        )
        try:
            parsed = json.loads(text.strip().strip("`").removeprefix("json"))
        except (json.JSONDecodeError, AttributeError):
            log.warning("m7: invalid propose JSON", extra={"ctx": {"condition_id": m["condition_id"]}})
            continue
        by_ext = {c["external_id"]: c["title"] for c in candidates}
        for match in parsed.get("matches", []):
            ext_id = match.get("external_id")
            if ext_id not in by_ext:
                continue
            proposals.append({
                "condition_id": m["condition_id"], "question": m["question"],
                "venue": "kalshi", "external_id": ext_id,
                "external_question": by_ext[ext_id],
                "rationale": match.get("rationale", ""),
                "confidence": float(match.get("confidence", 0.0)),
                "proposed_ts": now_utc_iso(),
            })
    data.setdefault("proposed", []).extend(proposals)
    save_markets_map(data, markets_map_path)
    return proposals


PMXT_VERIFY_SYSTEM = """You verify a SUGGESTED cross-venue prediction-market match for a
research pipeline. A third-party matching tool has proposed that ONE Polymarket question
and ONE Kalshi market describe the same real-world event with the same resolution
criteria. Judge independently -- the third-party tool's own confidence score is context,
not ground truth; it is sometimes wrong. Respond ONLY with a JSON object:
{"match": bool, "confidence": float 0.0-1.0, "rationale": str}
Be conservative: a wrong match is worse than a missed one."""


def _pmxt_verify_prompt(poly_question: str, poly_description: str | None, kalshi_title: str,
                        relation_type: str, pmxt_confidence: float,
                        kalshi_ticker: str | None = None,
                        kalshi_outcomes: list[str] | None = None,
                        kalshi_description: str | None = None) -> str:
    """Kalshi's identity is carried by the ticker and outcome labels, not the title.

    Passing the title alone made this check reject everything (2026-07-28: six
    of six, and five of five the day before). A Kalshi per-candidate market
    like KXPRESPERSON-28-NHAL reports its market-level `title` as the generic
    *event* question -- "Who will win the next presidential election?" -- while
    the candidate it actually resolves on lives in the ticker suffix and in the
    outcome labels ("Nikki Haley" / "Not Nikki Haley"). Shown only that title
    against "Will Nikki Haley win the 2028 US Presidential Election?", the
    verifier rightly said these are different questions: on the evidence it was
    given, they were. The judgement was never the problem; the evidence was.
    """
    lines = [
        f"POLYMARKET QUESTION: {poly_question}",
        f"POLYMARKET RESOLUTION CRITERIA: {poly_description or '(none on file)'}",
        "",
        f"SUGGESTED KALSHI MATCH: {kalshi_title}",
    ]
    if kalshi_ticker:
        lines.append(f"KALSHI TICKER: {kalshi_ticker}")
    if kalshi_outcomes:
        lines.append(f"KALSHI OUTCOMES: {' | '.join(kalshi_outcomes)}")
    if kalshi_description:
        lines.append(f"KALSHI RESOLUTION CRITERIA: {kalshi_description}")
    lines += [
        "",
        "NOTE: a Kalshi market's title is often the broad EVENT question while the"
        " specific outcome it resolves on is identified by the ticker suffix and the"
        " outcome labels above. Judge the pair on the specific outcome, not the title alone.",
        "NOTE: symmetrically, a Polymarket market in a negRisk set carries the whole"
        " SET's resolution text (e.g. 'resolves to the amount of basis points changed')"
        " while the market itself is one leg of it, named by its own question (e.g. 'No"
        " change ...'). Judge the leg named in the question, not the set the criteria"
        " describe.",
        "",
        f"THIRD-PARTY TOOL'S OWN VERDICT: relation_type={relation_type}, confidence={pmxt_confidence}",
    ]
    return "\n".join(lines)


def load_pmxt_candidates(path: Path | None = None) -> list[dict[str, Any]]:
    """Raw candidate pairs from scripts/pmxt_router_scan.py's own scheduled
    task -- an out-of-band process, never called from inside this codebase
    (see that script's docstring for why). A missing or unreadable file just
    means nothing to verify yet, not an error."""
    p = path or DEFAULT_PMXT_CANDIDATES_PATH
    if not p.exists():
        return []
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("m7: pmxt candidates file unreadable", extra={"ctx": {"path": str(p)}})
        return []
    return loaded if isinstance(loaded, list) else []


def verify_pmxt_candidates(conn, config: dict[str, Any], llm,
                          candidates_path: Path | None = None,
                          markets_map_path: Path | None = None,
                          ) -> list[dict[str, Any]]:
    """Second, independent check on pmxt's out-of-band Router suggestions.
    pmxt is a THIRD-PARTY tool proposing candidates the same way the LLM does
    in propose_matches -- it earns no special trust just because a different
    tool produced it, and a human still confirms every pair in
    markets_map.yaml before M7 ever reads it (unchanged propose-then-confirm
    contract).

    Consumes every candidate it gets through, whatever the outcome --
    re-verifying the same stale candidates forever would just burn LLM budget
    on pairs already accepted, rejected, or since resolved/delisted -- and
    keeps the rest: candidates the scan appended while this ran (a scan is
    now one lookup per market and takes minutes to hours, 2026-09-30), and
    the ones left when the daily LLM cap runs out, which used to take the
    proposals already made with it.
    """
    from lab.news.extract import BudgetExceeded

    path = candidates_path or DEFAULT_PMXT_CANDIDATES_PATH
    candidates = load_pmxt_candidates(path)
    if not candidates:
        return []

    data = load_markets_map(markets_map_path)
    already = {(e["condition_id"], e["venue"]) for e in data.get("confirmed", []) + data.get("proposed", [])}

    proposals: list[dict[str, Any]] = []
    got_through = len(candidates)
    for i, c in enumerate(candidates):
        condition_id = c.get("poly_condition_id")
        kalshi_ticker = c.get("kalshi_ticker")
        if not condition_id or not kalshi_ticker or (condition_id, "kalshi") in already:
            continue
        row = conn.execute(
            "SELECT question, description FROM markets WHERE condition_id = ?", (condition_id,)
        ).fetchone()
        question = (row["question"] if row else None) or c.get("poly_question")
        if not question:
            continue
        description = row["description"] if row else None

        try:
            text, _usage = llm.complete(
                PMXT_VERIFY_SYSTEM,
                _pmxt_verify_prompt(question, description, c.get("kalshi_title", ""),
                                    c.get("relation_type", "unknown"), float(c.get("confidence", 0.0)),
                                    kalshi_ticker=kalshi_ticker,
                                    kalshi_outcomes=c.get("kalshi_outcomes"),
                                    kalshi_description=c.get("kalshi_description")),
                purpose="m7_pmxt_verify",
            )
        except BudgetExceeded as exc:
            log.warning("m7: pmxt verify stopped at the daily LLM cap; the rest wait for the next run",
                        extra={"ctx": {"verified": i, "left": len(candidates) - i, "error": str(exc)}})
            got_through = i
            break
        try:
            parsed = json.loads(text.strip().strip("`").removeprefix("json"))
        except (json.JSONDecodeError, AttributeError):
            log.warning("m7: invalid pmxt-verify JSON", extra={"ctx": {"condition_id": condition_id}})
            continue
        if not parsed.get("match"):
            continue
        proposals.append({
            "condition_id": condition_id, "question": question,
            "venue": "kalshi", "external_id": kalshi_ticker,
            "external_question": c.get("kalshi_title", ""),
            "rationale": parsed.get("rationale", ""),
            "confidence": float(parsed.get("confidence", 0.0)),
            "proposed_ts": now_utc_iso(),
            "source": "pmxt",
            "source_meta": {"relation_type": c.get("relation_type"),
                           "pmxt_confidence": c.get("confidence")},
        })
        already.add((condition_id, "kalshi"))

    if proposals:
        data.setdefault("proposed", []).extend(proposals)
        save_markets_map(data, markets_map_path)

    done = {_candidate_key(c) for c in candidates[:got_through]}
    remaining = [c for c in load_pmxt_candidates(path) if _candidate_key(c) not in done]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(remaining, indent=2), encoding="utf-8")
    tmp.replace(path)  # the scan writes the same way, so neither reads the other's half-file
    return proposals


def _candidate_key(c: dict[str, Any]) -> tuple[Any, Any]:
    return c.get("poly_condition_id"), c.get("kalshi_ticker")
