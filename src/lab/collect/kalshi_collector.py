"""Kalshi collector: market discovery/tiering, snapshot loop, resolution
watcher -- the Kalshi analog of universe.py / snapshots.py / resolutions.py
(brief section 3, Phase 10).

Assumptions (guardrail 1 -- stated rather than silently picked):

1. Discovery is deliberately NOT a bulk `/markets` scan. The unfiltered feed is
   flooded with sub-24h crypto price-target markets and multivariate combo
   markets that the brief's universe policy excludes outright (martingale
   underlyings; "ALL crypto/equity price-target markets at any horizon").
   Instead we walk `config['venues']['kalshi']['category_map']` -> per-category
   `/series` -> per-series `/markets`, explicitly skipping
   `excluded_series_categories` even if such a category were reachable another
   way, and capping total series processed per cycle via `max_series_per_sync`.
2. Tiering mirrors `assign_tier_with_category` in collect/universe.py exactly,
   just against Kalshi's own config['venues']['kalshi']['tiers'] thresholds
   (liquid requires BOTH liquidity and volume to clear; tail requires BOTH to
   clear the (lower) tail thresholds; else ignored).
3. Kalshi's `/markets` response gives no order-book depth (unlike Polymarket's
   `/book`), so `bid_depth_usd`/`ask_depth_usd` are always None for Kalshi
   snapshot rows. Nothing in the current model set reads book depth for
   non-Polymarket venues; if a future model needs it, it would come from a
   separate orderbook endpoint, not from here.
4. Kalshi has no UMA-style dispute window. A market is treated as finally
   resolved exactly when `status == 'finalized'` and `result` is 'yes' or
   'no' (empty string means "not yet settled" per the live-verified shape);
   `disputed` is always recorded False for Kalshi rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from lab.api.kalshi import KalshiClient, KalshiMarket
from lab.collect.categories import load_categories
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import now_utc, now_utc_iso

log = logging.getLogger(__name__)

_OPEN_STATUSES = ("active", "initialized")
_CLOSED_STATUSES = ("finalized", "closed")


def assign_kalshi_tier(m: KalshiMarket, config: dict[str, Any],
                       depth_usd: float | None = None) -> tuple[str, str | None]:
    """Tier a Kalshi market, on our own measured order-book depth where we have
    it and on traded volume / open interest where we do not.

    This is deliberately the SAME rule and the SAME thresholds
    (`universe.tiers.*.min_depth_usd`) `assign_tier_with_category` applies to
    Polymarket -- Phase 17 item 2's whole point is that a tier means "this much
    real depth", not "this venue's self-reported field cleared a venue-specific
    number". The two distributions turned out close enough to share one bar:
    measured 2026-08-10, p25/p50 depth was $10/$45 on Kalshi against $9/$66 on
    Polymarket.

    The fallback exists because depth is only knowable after a market has been
    snapshotted at least once, and 1,715 of 4,803 Kalshi markets had no depth at
    all on that date (a quote with no size). It is NOT Kalshi's
    `liquidity_dollars`, which reads 0.0 for every market they publish -- keying
    the liquid gate on that is what kept the entire venue out of the liquid tier,
    and out of the shadow portfolio with it, until 2026-08-09.

    Returns (tier, reason_code), mirroring `assign_tier_with_category`:
    reason_code is populated only for 'ignored', so the caller can record it in
    `universe_log` (Phase 15).
    """
    tiers = config["universe"]["tiers"]
    if depth_usd is not None:
        if depth_usd >= tiers["liquid"]["min_depth_usd"]:
            return "liquid", None
        if depth_usd >= tiers["tail"]["min_depth_usd"]:
            return "tail", None
        return "ignored", "low_liquidity"

    fallback = config["venues"]["kalshi"]["tiers"]
    vol = m.volume_fp or 0.0
    oi = m.open_interest_fp or 0.0
    liquid = fallback["liquid"]
    if vol >= liquid["min_volume"] and oi >= liquid.get("min_open_interest", 0):
        return "liquid", None
    tail = fallback["tail"]
    if vol >= tail["min_volume"] and oi >= tail.get("min_open_interest", 0):
        return "tail", None
    return "ignored", "low_liquidity"


def kalshi_market_row(m: KalshiMarket, category: str) -> dict[str, Any]:
    return {
        "condition_id": db.venue_condition_id("kalshi", m.ticker),
        "venue": "kalshi",
        "venue_native_id": m.ticker,
        "slug": None,
        "question": m.title,
        "category": category,
        "description": m.rules_primary,
        "end_date_iso": m.close_time or m.expiration_time,
        "token_id_yes": None,
        "token_id_no": None,
        "neg_risk": 0,
        "active": int(m.status in _OPEN_STATUSES),
        "closed": int(m.status in _CLOSED_STATUSES),
        "liquidity_num": m.liquidity_dollars,
        "volume_num": m.volume_fp,
        "volume_24h_num": m.volume_24h_fp,   # Phase 15 forecast covariate

    }


def _series_sync_order(conn, candidates: list[tuple[str, str]], max_series: int,
                       discovery_share: float = 0.2) -> list[str]:
    """Which series this cycle syncs: mostly the stalest ones we know carry
    markets, plus a small discovery slice of ones we have never seen.

    `max_series_per_sync` bounds each cycle for politeness, but the loop it
    bounded walked a fixed category/API order and stopped at the cap -- so the
    same head was re-synced hourly and the tail was never reached. Measured
    2026-08-10 across 5,009 Kalshi markets in ~285 series: 82% unsynced for 3+
    days, and 1,772 still flagged active past their end date, because only a
    sync clears that flag.

    The budget has to be SPLIT rather than simply staleness-ordered, and that
    is not obvious -- ordering never-synced first (the resolution watcher's
    rule, which is right there) starves the productive series here: Kalshi's
    /series listing returns 10,502 series against the ~285 that actually carry
    open markets, so a never-seen-first rotation spends every cycle on empty
    ones. The first cycle that shipped that ordering synced 40 series and saw
    zero markets. The series payload carries no volume or open-market count, so
    empties cannot be filtered out cheaply -- hence a reserved slice instead.

    Known series are ordered oldest-synced first, so ~285 of them turn over in
    about nine cycles. Unseen ones are ordered by the series' own
    `last_updated_ts` (most recent first), which needs no persisted cursor and
    puts genuinely active new series ahead of dormant ones.
    """
    if not candidates:
        return []
    # Deduplicate first, or the cap budgets slots rather than series. Measured
    # on the first cycle after the cursor shipped (2026-09-08): 40 series were
    # walked and only 33 distinct ones reached the cursor table, so ~17% of an
    # hourly budget went on fetching the same series twice -- and the rotation
    # arithmetic that justifies a ~26-hour worst case silently assumed 40.
    # Duplicates arise because the candidate list is accumulated across every
    # configured category and a series can be returned by more than one.
    _first_seen: dict[str, str | None] = {}
    for _t, _u in candidates:
        if _t not in _first_seen:
            _first_seen[_t] = _u
    candidates = list(_first_seen.items())
    rows = conn.execute(
        """
        SELECT substr(venue_native_id, 1, instr(venue_native_id || '-', '-') - 1) AS series,
               MAX(last_synced_ts) AS synced
        FROM markets WHERE venue = 'kalshi' GROUP BY series
        """
    ).fetchall()
    seen = {r["series"]: r["synced"] for r in rows if r["series"]}
    # The ROTATION key is our own attempt cursor; the PARTITION key stays
    # `seen`. They must not be the same thing, and that is the whole subtlety
    # here (2026-09-08). Ordering by MAX(last_synced_ts) meant a series that
    # returns no open markets advanced nothing, stayed oldest, and -- Python's
    # sort being stable -- was handed back at the head of every single cycle:
    # 231 dead series against ~32 known slots pinned the queue completely, and
    # the 606 series that actually carry open markets went unvisited for a
    # measured median of 383 hours against a designed 26.
    #
    # Re-keying `seen` itself on attempts would have been the tempting one-line
    # version and is wrong: membership would then include every series a
    # discovery slot ever touched, migrating ~10,700 dormant series out of
    # `unseen` and into `known` -- which is precisely the never-seen-first
    # rotation this docstring already records shipping and failing.
    cursor = {
        r["series"]: r["attempted_ts"]
        for r in conn.execute("SELECT series, attempted_ts FROM kalshi_series_sync")
    }

    known = [(t, u) for t, u in candidates if t in seen]
    unseen = [(t, u) for t, u in candidates if t not in seen]
    # Fall back to the old key while the cursor table is still filling, so the
    # first cycles after deploy stay ordered rather than arbitrary.
    known.sort(key=lambda tu: cursor.get(tu[0]) or seen.get(tu[0]) or "")
    # The discovery half was wedged the same way, on a key we do not own
    # (2026-09-08, PAP 9.29). Ordering unseen series by the venue's
    # `last_updated_ts` alone means a series that yields nothing keeps whatever
    # position that key gives it forever, so the discovery slice re-walked the
    # same head every cycle and never reached the pool it exists to sweep.
    # Two passes, relying on sort stability: venue recency first, then the
    # attempt cursor -- never-attempted series (absent from the cursor, so "")
    # stay ahead of already-walked ones and keep their recency ordering among
    # themselves, while a walked-and-empty series rotates to the back.
    unseen.sort(key=lambda tu: tu[1] or "", reverse=True)
    unseen.sort(key=lambda tu: cursor.get(tu[0]) or "")

    n_discovery = min(len(unseen), int(max_series * discovery_share))
    n_known = min(len(known), max_series - n_discovery)
    # Unused discovery budget falls back to known series, and vice versa, so a
    # cycle is never short just because one pool is small.
    picked = [t for t, _ in known[:n_known]] + [t for t, _ in unseen[:n_discovery]]
    if len(picked) < max_series:
        extra = [t for t, _ in known[n_known:]] + [t for t, _ in unseen[n_discovery:]]
        picked += extra[:max_series - len(picked)]
    return picked


async def sync_kalshi_universe(
    kalshi: KalshiClient, conn, config: dict[str, Any], store: SnapshotStore | None = None
) -> dict[str, int]:
    """Fetch open markets per configured category (deterministic, bounded fan-out)
    and upsert (idempotent). Returns a summary counts dict, mirroring
    sync_universe()'s shape/logging in collect/universe.py."""
    from lab.collect.universe import _depth_lookup, log_universe_exclusion

    kalshi_cfg = config["venues"]["kalshi"]
    depth_by_market = _depth_lookup(store, now_utc()) if store is not None else {}
    category_map: dict[str, str] = load_categories()["kalshi_series"]
    excluded = set(kalshi_cfg.get("excluded_series_categories", []))
    max_series = kalshi_cfg.get("max_series_per_sync", 40)

    counts = {"series": 0, "markets_seen": 0, "liquid": 0, "tail": 0, "ignored": 0, "skipped_category": 0}
    series_processed = 0
    # A cycle that overruns its own 60-minute interval runs late rather than
    # concurrently (_add_interval_job sets max_instances=1), which would halve
    # the rotation this cursor restores without anything saying so.
    cycle_started = now_utc()

    # Collect every category's series FIRST, then order the whole set by
    # staleness -- the cap has to be a rotation over all of them, not a cutoff
    # that keeps re-reading whichever category happens to be iterated first.
    candidates: list[tuple[str, str, str | None]] = []   # (ticker, our_category, last_updated_ts)
    for kalshi_category, our_category in category_map.items():
        if kalshi_category in excluded:
            counts["skipped_category"] += 1
            continue
        try:
            series_list = await kalshi.series_by_category(kalshi_category)
        except Exception:
            log.warning("kalshi universe: series fetch failed",
                        extra={"ctx": {"category": kalshi_category}})
            continue
        for s in series_list:
            ticker = s.get("ticker")
            if ticker:
                candidates.append((ticker, our_category, s.get("last_updated_ts")))

    by_ticker = {t: cat for t, cat, _ in candidates}
    order = _series_sync_order(conn, [(t, upd) for t, _, upd in candidates], max_series)
    counts["series_available"] = len(candidates)
    for ticker in order:
        our_category = by_ticker[ticker]
        series_processed += 1
        counts["series"] += 1
        try:
            markets = await kalshi.markets_for_series(ticker, status="open")
        except Exception:
            log.warning("kalshi universe: markets fetch failed",
                        extra={"ctx": {"series_ticker": ticker}})
            # Stamp the failure too. A series we could not reach still has to
            # rotate to the back, or a persistently failing one rebuilds the
            # same head-of-queue wedge the cursor exists to prevent.
            db.record_kalshi_series_attempt(conn, ticker, 0)
            conn.commit()
            continue
        db.record_kalshi_series_attempt(conn, ticker, len(markets))
        for m in markets:
            counts["markets_seen"] += 1
            row = kalshi_market_row(m, our_category)
            tier, reason = assign_kalshi_tier(
                m, config, depth_by_market.get(row["condition_id"]))
            counts[tier] += 1
            if reason:
                # Phase 15: Kalshi exclusions were never recorded, though this
                # venue carries ~80% of the lab's daily forecasts -- "why isn't
                # X in the ledger" has to be answerable for it too.
                log_universe_exclusion(conn, "kalshi", m.ticker, reason)
            db.upsert_market(conn, {**row, "tier": tier})
            # Group the market into its venue's own event, the same job
            # _link_negrisk_legs does for Polymarket at sync time. Without it
            # every mutually-exclusive leg counted as an independent cluster
            # and every Kalshi confidence interval was computed over an inflated
            # n (x6.8 measured). Deterministic id -- Kalshi supplies the
            # identifier, so there is nothing to invent.
            event_ticker = m.event_ticker or db.kalshi_event_ticker(m.ticker)
            if event_ticker:
                event_id = f"kalshi:{event_ticker}"
                conn.execute(
                    "INSERT OR IGNORE INTO events(event_id, title, created_ts) VALUES (?, ?, ?)",
                    (event_id, m.title, now_utc_iso()),
                )
                conn.execute("UPDATE markets SET event_id = ? WHERE condition_id = ?",
                             (event_id, row["condition_id"]))
        conn.commit()

    counts["cycle_seconds"] = int((now_utc() - cycle_started).total_seconds())
    log.info("kalshi universe sync complete", extra={"ctx": counts})
    return counts


def tracked_kalshi_markets(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT condition_id, venue_native_id, tier, category FROM markets "
        "WHERE venue = 'kalshi' AND active = 1 AND closed = 0"
    ).fetchall()
    return [dict(r) for r in rows]


def drop_unsampled_sports(conn, config: dict[str, Any],
                          markets: list[dict]) -> tuple[list[dict], int]:
    """Of `markets`, the ones a venue-wide snapshot round should collect:
    everything except sports markets outside the seeded null-control sample.

    Those markets cannot become forecast targets. `eligible_market_states`
    drops them before it checks anything else, and `drop_null_control_outsiders`
    guards the two models that write outside it -- §3 keeps "a small random
    sample of sports markets in the ledger", and a control whose membership is
    not controlled is not a control. So a request spent on them buys no
    research observation, only a parquet row nothing reads.

    That was harmless while it was cheap, and it stopped being cheap when the
    football season opened. Measured 2026-09-03: of 16,164 Kalshi markets in
    the snapshotted tiers, 11,783 (73%) were sports, 4,333 of them listed in
    the previous ten days (KXCYCLINGSTAGE 1,000, KXNCAAFCFPPOLL 360,
    KXMLBTEAMTOTAL 315, KXNFLRACE 240), median volume 0. The tail round no
    longer fit inside guardrail 13's 90-minute bound -- 4,918 markets carried
    no snapshot at all and the median age of the rest was 80.6 minutes -- so on
    2026-09-02 the forecast pass skipped 4,211 markets on stale prices (21 in
    late August) and Kalshi's forecast count fell from 19,895/day to 7,915.
    The starved markets were economics and politics: the sports filter runs
    first, so none of those 4,211 were sports. The budget was being spent on
    the population the policy excludes, at the expense of the one it studies.

    **Kalshi only, deliberately.** Polymarket's rounds are left alone: they are
    not budget-bound after the 2026-08-22 concurrency change (tail median age
    34.4 min against the 90-minute bound), so this would buy nothing there --
    and it would cost something. Tiering falls back to venue-reported figures
    once a market has no recent snapshot to measure depth from, and
    Polymarket's fallback tail bar is real (`min_liquidity` 1000 /
    `min_volume` 5000), so an unsampled sports market could fall out of
    `tier IN ('liquid','tail')`, leave the pool `null_control_ids` samples
    from, and never be drawable again -- the policy would slowly select its own
    control. Kalshi's fallback tail bar is `min_volume: 0,
    min_open_interest: 0`, which every market clears, so the pool this returns
    a subset of is exactly the pool it was before. Turnover stays what it was:
    driven by markets resolving, not by what we chose to stop measuring.

    Not written to `universe_log`: these are not universe exclusions (the
    markets stay tracked, synced and resolution-watched) and it would add
    ~11,700 rows a day to a table read only as daily counts. The set is exactly
    reproducible from `null_control.random_seed` and the market table, which is
    what an auditor actually needs.

    Returns (kept, dropped_count).
    """
    nc = config["universe"]["null_control"]
    if nc.get("snapshot_unsampled", False):
        return markets, 0
    nc_category = nc["category"]
    if not any(m.get("category") == nc_category for m in markets):
        return markets, 0   # nothing to decide; the liquid round is mostly economics

    from lab.forecast import null_control_ids

    sampled = null_control_ids(conn, config)
    kept = [m for m in markets
            if m.get("category") != nc_category or m["condition_id"] in sampled]
    return kept, len(markets) - len(kept)


def tracked_kalshi_markets_by_ids(conn, condition_ids: list[str]) -> list[dict]:
    """Phase 17 item 3: an explicit, small set of Kalshi markets (confirmed
    cross-venue pairs) rather than every open Kalshi market."""
    if not condition_ids:
        return []
    placeholders = ",".join("?" * len(condition_ids))
    rows = conn.execute(
        f"SELECT condition_id, venue_native_id FROM markets "
        f"WHERE venue = 'kalshi' AND condition_id IN ({placeholders})",
        tuple(condition_ids),
    ).fetchall()
    return [dict(r) for r in rows]


def _top_of_book_usd(price: float | None, size: float | None) -> float | None:
    """price * size, or None when either side of the quote is missing/zero.

    Kalshi sizes are contract counts settling at $1 apiece, so this is USD
    notional at the best level -- the same quantity `OrderBook.depth_usd`
    returns for Polymarket, so the two venues' depth columns mean one thing.
    """
    if price is None or size is None:
        return None
    if price <= 0 or size <= 0:
        return None
    return float(price) * float(size)


async def snapshot_kalshi_markets(kalshi: KalshiClient, store: SnapshotStore,
                                 markets: list[dict], ts_bucket: str,
                                 depth_levels: int = 0, concurrency: int = 1) -> int:
    """Snapshot an explicit set of Kalshi markets. Shared by snapshot_kalshi
    (every open Kalshi market) and Phase 17 item 3's per-confirmed-pair
    high-frequency job (a small, explicit condition_id list).

    `depth_levels > 0` additionally fetches each market's order book and fills
    the depth columns. That costs one extra request per market, so callers pass
    it only for the liquid tier. Until 2026-08-09 these columns were written
    NULL unconditionally, which made every Kalshi market fail the shadow
    portfolio's own depth filter (brief section 8: top-of-book depth >= $500)
    even before its tier excluded it.

    `concurrency` bounds how many fetches are in flight. Measured 2026-08-17,
    a full Kalshi round took ~60 minutes for 3,966 markets plus 461 order
    books -- 1.2 req/s against a configured ceiling of 8, because every await
    was strictly sequential. That is also why the round overran its own
    30-minute interval and APScheduler silently dropped every second firing.
    The TokenBucket still caps the rate per request, so guardrail 8 holds
    whatever this is set to; 1 reproduces the old sequential behaviour.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(row: dict) -> dict | None:
        ticker = row["venue_native_id"]
        async with sem:
            try:
                m = await kalshi.market(ticker)
            except Exception:
                log.warning("kalshi snapshot: market fetch failed",
                            extra={"ctx": {"condition_id": row["condition_id"]}})
                return None
        if m is None or m.yes_price is None:
            return None
        spread = None
        if m.yes_bid_dollars is not None and m.yes_ask_dollars is not None:
            spread = m.yes_ask_dollars - m.yes_bid_dollars

        # Top-of-book depth comes free on the market object we already fetched:
        # `yes_bid_size_fp`/`yes_ask_size_fp` are contract counts and each
        # contract settles at $1, so price * size is the same USD notional
        # `depth_usd` sums for Polymarket. No extra request, and it works for
        # every tier. A missing or zero size stays NULL, never 0.0 -- a zero
        # would read as "measured, and there is none" to every downstream
        # filter, which is a different claim.
        bid_depth = _top_of_book_usd(m.yes_bid_dollars, m.yes_bid_size_fp)
        ask_depth = _top_of_book_usd(m.yes_ask_dollars, m.yes_ask_size_fp)
        bids_json = asks_json = None
        if depth_levels > 0:
            # The full ladder, for the liquid tier only: unlike the scalars
            # above it costs a request each, and unlike them it cannot be
            # reconstructed later (same reasoning as the Polymarket store).
            async with sem:
                book = await kalshi.orderbook(ticker)
            if book is not None and book.bids:
                bids_json = json.dumps(book.top_levels("bid", depth_levels))
            if book is not None and book.asks:
                asks_json = json.dumps(book.top_levels("ask", depth_levels))

        return {
            "ts": ts_bucket,
            "condition_id": row["condition_id"],
            "token_id_yes": None,
            "best_bid": m.yes_bid_dollars,
            "best_ask": m.yes_ask_dollars,
            "mid": m.yes_price,
            "spread": spread,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
            "last_trade_price": m.last_price_dollars,
            "open_interest": m.open_interest_fp,   # Phase 15 crowd-size covariate
            "bids_json": bids_json,
            "asks_json": asks_json,
            "venue": "kalshi",
        }

    rows = [r for r in await asyncio.gather(*(_one(m) for m in markets)) if r is not None]
    return store.append(rows)


async def snapshot_kalshi(kalshi: KalshiClient, conn, store: SnapshotStore,
                         config: dict[str, Any], tier: str = "liquid") -> int:
    """One snapshot round for a single Kalshi tier. Returns rows written.

    Split per tier on 2026-08-18. Until then one job snapshotted the whole
    venue on one interval, which made the liquid tier's cadence the tail's:
    ~60 minutes in practice. Guardrail 13 allows a liquid-tier price 15
    minutes of age, so 443 of Kalshi's 464 liquid markets failed the
    freshness gate at forecast time EVERY night and were dropped -- and those
    464 are precisely the markets addenda 9.9/9.10 promoted onto the measured
    depth bar. A tier whose freshness bound is 15 minutes has to be collected
    on a cadence that can meet it.
    """
    markets = [m for m in tracked_kalshi_markets(conn)
               if (m.get("tier") == "liquid") == (tier == "liquid")]
    # Sports markets outside the null-control sample are not forecastable and
    # were consuming 73% of this venue's request budget -- see
    # drop_unsampled_sports. The per-pair high-frequency job (Phase 17 item 3)
    # takes an explicit condition_id list and is deliberately NOT filtered:
    # its snapshots serve the lead-lag hypothesis (PAP H3), which reads the
    # price series itself rather than any forecast written against it.
    markets, dropped = drop_unsampled_sports(conn, config, markets)
    if not markets:
        log.info("kalshi snapshot round: no markets", extra={"ctx": {"tier": tier}})
        return 0
    bucket_minutes = config["venues"]["kalshi"]["snapshot_interval_minutes"][tier]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)

    # Order books only for the liquid tier: one extra request each, and the
    # tail is thousands of markets. The liquid tier is what the shadow
    # portfolio scans and what depth-based tiering would refine, so that is
    # where the measurement is worth its request budget (guardrail 8).
    depth_levels = config["collect"].get("book_depth_levels", 10) if tier == "liquid" else 0
    written = await snapshot_kalshi_markets(
        kalshi, store, markets, ts_bucket, depth_levels=depth_levels,
        concurrency=config["venues"]["kalshi"].get("snapshot_concurrency", 1))
    log.info("kalshi snapshot round done",
             extra={"ctx": {"tier": tier, "markets": len(markets),
                            "unsampled_sports_skipped": dropped,
                            "with_depth": len(markets) if depth_levels else 0,
                            "written": written}})
    return written


def unresolved_kalshi_markets(conn, limit: int = 200) -> list[dict]:
    """Least-recently-checked candidates first.

    Carries the identical ORDER BY that `collect/resolutions.py`'s
    `unresolved_closed_markets` gained on 2026-07-25, and for the identical
    reason -- read that docstring, it describes this exact failure. `LIMIT`
    with no ordering hands back the same rows in scan order every cycle, and
    the head fills with markets that will never settle -- Kalshi rows whose
    `end_date_iso` went stale in the past while the sync was pinned -- so the
    watcher re-fetches those forever and never reaches anything behind them.
    That population is small right now (48 rows on 2026-09-08, and the venue's
    whole unresolved candidate set is 48) precisely because this watcher HAS
    been draining; the ordering is here so a wedge cannot form again once the
    restored sync rotation starts feeding it. NULLs sort first on ASC, which
    puts a fresh closure ahead of the old backlog for free.
    """
    rows = conn.execute(
        """
        SELECT m.condition_id, m.venue_native_id FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.venue = 'kalshi'
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        ORDER BY m.resolution_checked_ts ASC
        LIMIT ?
        """,
        (now_utc_iso(), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def extract_kalshi_payout(m: KalshiMarket) -> float | None:
    """Return payout_yes if the market has a final Kalshi settlement, else None."""
    if m.status != "finalized":
        return None
    if m.result == "yes":
        return 1.0
    if m.result == "no":
        return 0.0
    return None


async def watch_kalshi_resolutions(kalshi: KalshiClient, conn, limit: int = 200) -> int:
    """One poll round over unresolved Kalshi markets already in our DB. Returns
    number of resolutions recorded. Mirrors collect/resolutions.py's pattern."""
    recorded = 0
    for row in unresolved_kalshi_markets(conn, limit=limit):
        condition_id, ticker = row["condition_id"], row["venue_native_id"]
        # Stamp first, and unconditionally -- the ordering above is only a
        # round-robin if every candidate rotates, including the ones that fail
        # to fetch and the ones Kalshi never finalizes. Verbatim the rule
        # resolutions.py:96-100 states for the Gamma watcher.
        conn.execute(
            "UPDATE markets SET resolution_checked_ts = ? WHERE condition_id = ?",
            (now_utc_iso(), condition_id),
        )
        conn.commit()
        try:
            m = await kalshi.market(ticker)
        except Exception:
            log.warning("kalshi resolutions: fetch failed",
                        extra={"ctx": {"condition_id": condition_id}})
            continue
        if m is None:
            conn.commit()
            continue
        payout_yes = extract_kalshi_payout(m)
        if payout_yes is None:
            conn.commit()
            continue
        db.record_resolution(
            conn, condition_id,
            resolved_ts=now_utc_iso(),
            payout_yes=payout_yes,
            disputed=False,
            source="kalshi",
        )
        recorded += 1
        # Commit per-candidate: avoids holding one long write transaction open
        # across a large backlog scan (same rationale as resolutions.py).
        conn.commit()
    if recorded:
        log.info("kalshi resolutions recorded", extra={"ctx": {"count": recorded}})
    return recorded
