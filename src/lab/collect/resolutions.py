"""Resolution watcher: polls closed markets and records FINAL payouts.

Finality assumption (brief section 3: record final payout, not first report):
Gamma reports a market as finally resolved when it is `closed` and the YES
outcome price collapses to exactly 0 or 1. Markets whose UMA status mentions
a dispute are recorded with the disputed flag once they do reach a final
payout, and skipped while the dispute is open. Writes are idempotent
(at-least-once safe).
"""

from __future__ import annotations

import logging

from lab.api.gamma import GammaClient, GammaMarket
from lab.store import db
from lab.util import now_utc_iso

log = logging.getLogger(__name__)


def unresolved_closed_markets(conn, limit: int = 200) -> list[str]:
    """Least-recently-checked candidates first.

    The ORDER BY is load-bearing, not tidiness (2026-07-25). Without it SQLite
    returned the same `limit` rows in scan order on every cycle, and the head
    of that scan had filled with markets Gamma never reports as `closed` whose
    end dates are long past -- unresolvable, and permanently first in line. The
    watcher re-fetched those same few hundred every 30 minutes and never
    reached anything behind them: a ~42k working set draining at a few dozen a
    day while ~600 markets closed daily. Half of all forecast weather markets
    (the fastest-resolving category, and one carrying a primary hypothesis)
    were sitting unscoreable behind it.

    NULLs sort first on ASC in SQLite, which is exactly the priority wanted: a
    market never checked yet -- a fresh closure -- goes to the front, while the
    old backlog sweeps steadily behind it rather than blocking it.

    Polymarket only, from 2026-09-08. This query predates multi-venue
    collection (Phase 10) and was never narrowed, so rows from venues Gamma
    has never heard of were candidates here: measured 2026-09-08, 961 Manifold
    and 48 Kalshi against 9,935 Polymarket, i.e. 9.2% of a 200-row cycle spent
    on ids that cannot resolve. Each costs TWO wasted requests, because
    `market_by_condition` tries `{condition_ids}` then `{condition_ids,
    closed:"true"}` before returning None. Both venues have their own watchers.

    The subtler harm was to monitoring: this loop stamps
    `resolution_checked_ts` unconditionally, so it kept foreign-venue rows
    looking freshly checked, and `lab status`'s `oldest_check_age_h` -- a MIN
    over that column -- could report a healthy age produced entirely by Gamma
    while another venue's watcher sat wedged.
    """
    rows = conn.execute(
        """
        SELECT m.condition_id FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL
          AND COALESCE(m.venue, 'polymarket') = 'polymarket'
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        ORDER BY m.resolution_checked_ts ASC
        LIMIT ?
        """,
        (now_utc_iso(), limit),
    ).fetchall()
    return [r["condition_id"] for r in rows]


def resolution_backlog_size(conn) -> int:
    """The watcher's ACTUAL working set -- the same predicate
    unresolved_closed_markets selects on.

    `lab status` used to report only the `closed = 1` half of this (17k of a
    real 42k), which is part of why the stall above went unnoticed for weeks:
    the number on the dashboard was not the number the watcher was working
    through.

    `venue` is filtered here for the same reason and on the same day the
    candidate query gained it: this function's entire contract is "the same
    predicate `unresolved_closed_markets` selects on", and a backlog number
    that counts 18k markets this watcher will never fetch is the very failure
    the paragraph above describes. The reported figure therefore drops sharply
    on 2026-09-08 -- that is the number becoming true, not the backlog
    draining. Kalshi's own backlog is reported per venue in `lab status`.
    """
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL
          AND COALESCE(m.venue, 'polymarket') = 'polymarket'
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        """,
        (now_utc_iso(),),
    ).fetchone()["n"]


def extract_final_payout(m: GammaMarket) -> tuple[float, bool] | None:
    """Return (payout_yes, disputed) if the market has a final payout, else None."""
    if not m.closed or len(m.outcome_prices) < 2:
        return None
    try:
        p_yes = float(m.outcome_prices[0])
    except ValueError:
        return None
    if p_yes not in (0.0, 1.0):
        return None
    statuses = " ".join(m.uma_resolution_statuses).lower()
    if "disputed" in statuses and "resolved" not in statuses:
        return None  # dispute still open -- wait for the final report
    return p_yes, "disputed" in statuses


async def watch_resolutions(gamma: GammaClient, conn, limit: int = 200) -> int:
    """One poll round. Returns number of resolutions recorded."""
    recorded = 0
    for condition_id in unresolved_closed_markets(conn, limit=limit):
        # Stamp first, and unconditionally. A market whose fetch fails, or one
        # Gamma simply never finalizes, still has to rotate to the back of the
        # queue -- stamping only on success would rebuild the same head-of-scan
        # wedge the ordering exists to prevent, just with a different
        # population sitting in the head.
        conn.execute(
            "UPDATE markets SET resolution_checked_ts = ? WHERE condition_id = ?",
            (now_utc_iso(), condition_id),
        )
        conn.commit()
        try:
            m = await gamma.market_by_condition(condition_id)
        except Exception:
            log.warning("resolutions: gamma fetch failed",
                        extra={"ctx": {"condition_id": condition_id}})
            continue
        if m is None:
            continue
        # Keep closed flag in sync so the market leaves the snapshot loop.
        if m.closed:
            conn.execute(
                "UPDATE markets SET closed = 1, active = ?, last_synced_ts = ? WHERE condition_id = ?",
                (int(bool(m.active)), now_utc_iso(), condition_id),
            )
        final = extract_final_payout(m)
        if final is None:
            # Still commit the closed-flag update above -- don't hold a write
            # transaction open across the rest of a (possibly long) backlog scan.
            conn.commit()
            continue
        payout_yes, disputed = final
        db.record_resolution(
            conn, condition_id,
            resolved_ts=now_utc_iso(),
            payout_yes=payout_yes,
            disputed=disputed,
            source="gamma",
        )
        recorded += 1
        # Commit per-candidate: at limit=3000 a single end-of-loop commit would
        # hold one write transaction for minutes, locking out every other
        # connection (collector jobs, `lab status`, ad-hoc queries) for the
        # whole scan instead of a single statement's worth of time.
        conn.commit()
    if recorded:
        log.info("resolutions recorded", extra={"ctx": {"count": recorded}})
    return recorded
