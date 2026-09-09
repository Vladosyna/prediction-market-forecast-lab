"""Market discovery & tiering (one sync per hour).

Tiering assumption (brief leaves the combination open): a market is `liquid`
when BOTH liquidity and volume clear the liquid thresholds, `tail` when both
clear the tail thresholds, else `ignored`. Markets without a live order book
are `ignored` regardless. Excluded categories (crypto/equities) stay in the
DB as `ignored` so calibration stats can still use them, except sub-24h
crypto "pulse" markets which are skipped entirely.

Phase 15's `universe_log` records every exclusion with a reason code:
'non_binary', 'crypto_price_target', 'no_orderbook', 'low_liquidity' (all via
`log_universe_exclusion`, called from `sync_universe`), plus 'tail_price'
(a forecast-target exclusion, orthogonal to tiering -- see `_mid_lookup`) and
'manual' (the `lab exclude` CLI command). 'ambiguous_resolution' is
deliberately NOT auto-populated: no safe deterministic detector exists for
resolution-wording ambiguity, and inventing one would itself be the kind of
editorial judgment guardrail 12 forbids.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from lab.api.gamma import GammaClient, GammaMarket
from lab.collect.categories import (
    category_from_polymarket_tags,
    load_categories,
    log_unrecognized_tag,
)
from lab.store import db
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

CRYPTO_HINTS = ("crypto", "bitcoin", "btc", "ethereum", "eth ", "solana", "dogecoin", "xrp")

# Equity price-target markets are the same martingale-underlying problem as
# crypto ones (brief section 3: "ALL crypto/equity price-target markets at
# any horizon... the market price *is* the forecast") -- a company's current
# valuation/market-cap IS the market's own forecast of it, so there's no edge
# to measure. Keyword-based like CRYPTO_HINTS rather than category-based,
# since these questions show up with category="unknown" on Gamma.
EQUITY_PRICE_TARGET_HINTS = ("market cap", "valuation", "fdv", "fully diluted")


def _category(m: GammaMarket) -> str:
    return (m.category or "unknown").strip().lower() or "unknown"


def _market_text(m: GammaMarket) -> str:
    return " ".join(filter(None, [_category(m), m.slug or "", m.question or ""])).lower()


def _looks_crypto(m: GammaMarket) -> bool:
    return any(h in _market_text(m) for h in CRYPTO_HINTS)


def _looks_equity_price_target(m: GammaMarket) -> bool:
    return any(h in _market_text(m) for h in EQUITY_PRICE_TARGET_HINTS)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_sub_24h_crypto(m: GammaMarket, now: datetime) -> bool:
    if not _looks_crypto(m):
        return False
    end = _parse_iso(m.end_date_iso)
    start = _parse_iso(m.start_date_iso)
    if end and start and end - start <= timedelta(hours=24):
        return True
    return bool(end and end - now <= timedelta(hours=24))


def assign_tier(m: GammaMarket, config: dict[str, Any]) -> str:
    return assign_tier_with_category(m, _category(m), config)[0]


def assign_tier_with_category(m: GammaMarket, category: str, config: dict[str, Any],
                              depth_usd: float | None = None) -> tuple[str, str | None]:
    """Phase 17 item 2: once we have our own collected order-book depth for a
    market, tier on THAT (depth_usd = bid_depth_usd + ask_depth_usd from the
    latest snapshot) instead of Gamma's self-reported liquidity_num/volume_num
    -- volume in particular is wash-trading-contaminated (concentrated in
    sports) and was never a sound tiering signal, just the only one available
    pre-collection. A brand-new market has no snapshot yet (depth_usd is
    None); it falls back to the liquidity/volume path below until one exists.

    Returns (tier, reason_code): reason_code is populated only when tier ==
    'ignored', for universe_log (Phase 15) -- callers log it so a market that
    never gets a forecast row (tier='ignored' is excluded from
    eligible_market_states just like a market never upserted at all) still
    answers "why isn't X in the ledger".
    """
    if m.enable_order_book is False or m.accepting_orders is False:
        return "ignored", "no_orderbook"
    if (category in set(config["universe"]["excluded_categories"]) or _looks_crypto(m)
            or _looks_equity_price_target(m)):
        return "ignored", "crypto_price_target"
    tiers = config["universe"]["tiers"]
    if depth_usd is not None:
        if depth_usd >= tiers["liquid"]["min_depth_usd"]:
            return "liquid", None
        if depth_usd >= tiers["tail"]["min_depth_usd"]:
            return "tail", None
        return "ignored", "low_liquidity"
    liq = m.liquidity_num or 0.0
    vol = m.volume_num or 0.0
    if liq >= tiers["liquid"]["min_liquidity"] and vol >= tiers["liquid"]["min_volume"]:
        return "liquid", None
    if liq >= tiers["tail"]["min_liquidity"] and vol >= tiers["tail"]["min_volume"]:
        return "tail", None
    return "ignored", "low_liquidity"


def market_row(m: GammaMarket, tier: str, category: str | None = None) -> dict[str, Any]:
    return {
        "condition_id": m.condition_id,
        "slug": m.slug,
        "question": m.question,
        "category": category or _category(m),
        "description": m.description,
        "end_date_iso": m.end_date_iso,
        "token_id_yes": m.token_id_yes,
        "token_id_no": m.token_id_no,
        "neg_risk": int(m.neg_risk),
        "active": int(bool(m.active)),
        "closed": int(bool(m.closed)),
        "liquidity_num": m.liquidity_num,
        # Covariate only, not used for tiering (Phase 17 item 2): Gamma's
        # volume_num is wash-trading-contaminated, concentrated in sports.
        "volume_num": m.volume_num,
        "volume_24h_num": m.volume_24h,   # Phase 15 forecast covariate

        "tier": tier,
    }


def _link_negrisk_legs(conn, condition_ids: list[str], title: str | None) -> None:
    """Phase 16: chain-link a negRisk event's legs into one shared event_id
    via the same mechanism the cross-venue matcher uses (`db.link_event`),
    just applied at sync time to same-venue legs instead of at human-confirm
    time. `link_event` already reuses an existing event_id from either side
    of a pair, so calling it pairwise (leg[0]-leg[1], leg[0]-leg[2], ...)
    correctly propagates ONE shared id across all N legs -- no new N-ary
    linking primitive needed. This is what lets Phase 16's RPS scoring find
    "which legs belong to one bucketed numeric question" on RESOLVED
    forecasts later -- Gamma's own event grouping is live-only (gone once a
    market closes), so it must be persisted here, not reconstructed after
    the fact. Idempotent: re-sync of an already-linked event is a no-op.
    """
    first = condition_ids[0]
    for other in condition_ids[1:]:
        db.link_event(conn, first, other, title=title)


def _depth_lookup(store: SnapshotStore, now: datetime, days_back: int = 3) -> dict[str, float]:
    """condition_id -> most recent (bid_depth_usd + ask_depth_usd), Phase 17
    item 2. A market with no snapshot in the window is simply absent from the
    returned dict -- callers treat that as "no depth data yet" (falls back to
    liquidity/volume tiering), not as zero depth."""
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(days_back + 1)]
    df = store.latest_per_market(dates)
    if df.is_empty():
        return {}
    # A row can exist with BOTH depth columns null -- a venue whose quote had no
    # size at that moment. `fill_null(0)` alone would turn that into a measured
    # $0 and tier the market 'ignored', which is the opposite of this function's
    # documented contract. Invisible while Polymarket was the only venue with
    # depth (every row has it); on Kalshi 1,715 of 4,803 markets had none at all
    # when depth collection started (2026-08-10), so it decides their tier.
    df = df.filter(
        pl.col("bid_depth_usd").is_not_null() | pl.col("ask_depth_usd").is_not_null()
    )
    if df.is_empty():
        return {}
    depth = df["bid_depth_usd"].fill_null(0) + df["ask_depth_usd"].fill_null(0)
    return dict(zip(df["condition_id"].to_list(), depth.to_list()))


def _mid_lookup(store: SnapshotStore, now: datetime, days_back: int = 3) -> dict[str, float]:
    """condition_id -> most recent mid price, for the tail_price universe_log
    check (Phase 15) -- mirrors _depth_lookup's exact "absent means no data
    yet, not zero" contract."""
    dates = [utc_date_str(now - timedelta(days=d)) for d in range(days_back + 1)]
    df = store.latest_per_market(dates)
    if df.is_empty():
        return {}
    return dict(zip(df["condition_id"].to_list(), df["mid"].to_list()))


def log_universe_exclusion(conn, venue: str, venue_native_id: str, reason_code: str) -> None:
    """Record a market excluded from -- or never granted forecast-target
    status in -- the universe, with why (Phase 15's universe_log, brief
    section 5/15). One row per (venue, venue_native_id, reason_code, day) --
    a market re-excluded for the same reason on the same UTC day across
    multiple hourly syncs is a no-op (INSERT OR IGNORE against the unique
    index db.migrate_universe_log_dedup adds), not a fresh row. Revised
    2026-07-20: the original "one row per occurrence" design produced ~20x
    same-day duplication with no consumer that ever read it (the report's
    universe_exclusion_counts only ever GROUP BY date(ts)) -- see
    db.migrate_universe_log_dedup's docstring for the full reasoning. Caller
    commits."""
    conn.execute(
        "INSERT OR IGNORE INTO universe_log (ts, venue, venue_native_id, reason_code) "
        "VALUES (?, ?, ?, ?)",
        (now_utc_iso(), venue, venue_native_id, reason_code),
    )


async def sync_universe(gamma: GammaClient, conn, store: SnapshotStore,
                        config: dict[str, Any]) -> dict[str, int]:
    """Fetch active events (markets + tags) from Gamma, tier and upsert (idempotent).

    Events are the source of truth because market-level `category` is no
    longer populated -- categories come from event tags.
    """
    now = now_utc()
    taxonomy = load_categories()
    depth_by_market = _depth_lookup(store, now)
    mid_by_market = _mid_lookup(store, now)
    price_lo, price_hi = config["universe"]["forecast_price_bounds"]
    events = await gamma.iter_events(active="true", closed="false")
    counts = {"events": len(events), "seen": 0, "binary": 0,
              "liquid": 0, "tail": 0, "ignored": 0, "skipped": 0}
    seen_ids: set[str] = set()
    for ev in events:
        category = category_from_polymarket_tags(ev.tag_slugs, taxonomy)
        if category == "unknown" and ev.tag_slugs:
            log_unrecognized_tag(conn, "polymarket", ",".join(sorted(ev.tag_slugs)))
        event_leg_ids: list[str] = []
        for m in ev.markets:
            if m.condition_id in seen_ids:
                continue
            seen_ids.add(m.condition_id)
            counts["seen"] += 1
            if not m.is_binary:
                log_universe_exclusion(conn, "polymarket", m.condition_id, "non_binary")
                continue
            counts["binary"] += 1
            if config["universe"]["skip_sub_24h_crypto"] and is_sub_24h_crypto(m, now):
                counts["skipped"] += 1
                log_universe_exclusion(conn, "polymarket", m.condition_id, "crypto_price_target")
                continue
            effective = m if not ev.neg_risk else m.model_copy(update={"neg_risk": True})
            depth_usd = depth_by_market.get(effective.condition_id)
            tier, reason = assign_tier_with_category(effective, category, config, depth_usd=depth_usd)
            counts[tier] += 1
            db.upsert_market(conn, market_row(effective, tier, category))
            if reason:
                log_universe_exclusion(conn, "polymarket", effective.condition_id, reason)
            # tail_price is orthogonal to tiering -- a liquid/tail market can
            # still be excluded as a forecast TARGET (not from the universe
            # outright) if its price sits outside forecast_price_bounds; only
            # log when we actually have a snapshot to check (Phase 15).
            if tier in ("liquid", "tail"):
                mid = mid_by_market.get(effective.condition_id)
                if mid is not None and not (price_lo < mid < price_hi):
                    log_universe_exclusion(conn, "polymarket", effective.condition_id, "tail_price")
            if ev.neg_risk:
                event_leg_ids.append(effective.condition_id)
        if ev.neg_risk and len(event_leg_ids) >= 2:
            _link_negrisk_legs(conn, event_leg_ids, title=ev.slug)
        # Bounded transactions, not one for the whole universe (2026-09-09).
        # A single commit at the end held one write lock across every upsert in
        # the sync -- thousands of them -- and SQLite allows exactly one
        # writer, so every other job that wanted to write during that window
        # queued behind it and gave up at `busy_timeout`. That is the same
        # reasoning that put a 250-row batch into `run_forecasts` on
        # 2026-08-25; this call site never got it. Committing per event keeps
        # each transaction to one event's legs and leaves the sync just as
        # restartable -- upserts are idempotent, so a partial sync is a
        # prefix, not a corruption.
        conn.commit()
    conn.commit()
    log.info("universe sync complete", extra={"ctx": counts})
    return counts
