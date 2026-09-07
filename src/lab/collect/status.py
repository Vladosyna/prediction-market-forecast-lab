"""`lab status` -- data health: freshness, gaps, watcher lag, counts, spend."""

from __future__ import annotations

from bisect import bisect_left
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from lab.collect.kalshi_collector import drop_unsampled_sports
from lab.store import db as dbmod
from lab.store.snapshots import SnapshotStore, utc_date_str
from lab.util import now_utc


def _dates_back(now: datetime, days: int) -> list[str]:
    return [utc_date_str(now - timedelta(days=d)) for d in range(days + 1)]


def gap_windows(df: pl.DataFrame, tier_markets: list[str], cadence_minutes: int,
                window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    """Actual [bucket_start, bucket_end) intervals with zero snapshots for the
    tier (Phase 17 item 5). A bucket counts as covered when at least one
    tracked market has a row -- per-market gap accounting would flag every
    market that IPOs mid-window. Returning the intervals themselves (not just
    a count) lets callers -- e.g. eval/clv.py's gap-aware drift -- check
    whether a SPECIFIC window overlaps a recorded gap.
    """
    if not tier_markets:
        return []
    n_buckets = int((window_end - window_start).total_seconds() // (cadence_minutes * 60))
    if n_buckets <= 0:
        return []
    all_buckets = [
        (window_start + timedelta(minutes=i * cadence_minutes),
         window_start + timedelta(minutes=(i + 1) * cadence_minutes))
        for i in range(n_buckets)
    ]
    subset = df.filter(pl.col("condition_id").is_in(tier_markets))
    if subset.is_empty():
        return all_buckets
    # Sorted-list + bisect instead of a per-bucket linear scan over `seen`:
    # provably the same predicate (`exists ts: start <= ts < end` on identical
    # fixed-width ISO strings, where lexicographic order is chronological
    # order for this format) but O(n_buckets log n) instead of O(n_buckets *
    # n) -- measured 324-494x on real data, verified list-identical output to
    # the prior implementation (see tests/test_status.py). This was ~75% of a
    # report render's wall clock: the old version also recomputed both
    # bucket_start.isoformat() and bucket_end.isoformat() once per (bucket,
    # ts) pair rather than once per bucket.
    seen_sorted = sorted(subset.get_column("ts").unique().to_list())
    return gaps_from_timestamps(seen_sorted, all_buckets)


def gaps_from_timestamps(
    seen_sorted: list[str], buckets: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """The bucket-coverage half of `gap_windows`, split out so a caller that
    already has the tier's sorted timestamps need not hold a frame to get
    them (see `tier_snapshot_timestamps`)."""
    gaps: list[tuple[datetime, datetime]] = []
    for bucket_start, bucket_end in buckets:
        start_iso = bucket_start.isoformat(timespec="seconds")
        end_iso = bucket_end.isoformat(timespec="seconds")
        i = bisect_left(seen_sorted, start_iso)
        covered = i < len(seen_sorted) and seen_sorted[i] < end_iso
        if not covered:
            gaps.append((bucket_start, bucket_end))
    return gaps


def cadence_buckets(cadence_minutes: int, window_start: datetime,
                    window_end: datetime) -> list[tuple[datetime, datetime]]:
    n_buckets = int((window_end - window_start).total_seconds() // (cadence_minutes * 60))
    return [
        (window_start + timedelta(minutes=i * cadence_minutes),
         window_start + timedelta(minutes=(i + 1) * cadence_minutes))
        for i in range(max(0, n_buckets))
    ]


def tier_snapshot_timestamps(store, dates: list[str], tier_markets: list[str]) -> list[str]:
    """Sorted unique snapshot timestamps for a tier, read one partition at a time.

    `gap_windows` needs nothing from a snapshot frame except this list -- a few
    thousand strings for a month. Materialising the whole (ts, condition_id)
    span to derive it is what made the report render's 31-day read ~600MB, and
    holding that frame alongside the next large allocation put the render's
    peak at 833MB on a 967MB host (measured per-phase 2026-07-28). Reading day
    by day bounds it to one partition.
    """
    if not tier_markets:
        return []
    ids = set(tier_markets)
    seen: set[str] = set()
    for date in dates:
        df = store.read_range([date], columns=["ts", "condition_id"])
        if df.is_empty():
            continue
        seen.update(
            df.filter(pl.col("condition_id").is_in(ids)).get_column("ts").unique().to_list()
        )
        del df
    return sorted(seen)


def snapshot_gaps(df: pl.DataFrame, tier_markets: list[str], cadence_minutes: int,
                  window_start: datetime, window_end: datetime) -> int:
    """Count cadence buckets in the window with zero snapshots for the tier."""
    return len(gap_windows(df, tier_markets, cadence_minutes, window_start, window_end))


LAB_UNITS = ("lab-run.service", "lab-dashboard.service")


def memory_budget(units: tuple[str, ...] = LAB_UNITS) -> dict[str, Any] | None:
    """Per-unit cgroup memory caps against the machine's actual RAM.

    Exists because of a real outage (2026-07-30): lab-run's cap was raised to
    900M during earlier OOM work without noticing lab-dashboard already held
    600M, on a 967M box. Neither unit ever breached its own cap, so the kernel
    had no cgroup violation to act on and never chose an OOM victim -- it
    simply thrashed until the host was unreachable for ~3 hours and needed a
    manual power cycle. Snapshot history for that window is unrecoverable.

    The invariant a limit cannot enforce alone: the SUM of the caps must stay
    under physical RAM. A cap that is individually generous and collectively
    impossible protects nothing -- it converts a recoverable per-service kill
    (seconds, automatic) into an unrecoverable box wedge. That mistake took
    ten days to surface; this check surfaces it on the next `lab status`.

    Returns None where the question does not apply -- no systemd, no cgroup
    accounting, or a non-Linux host (the operator's laptop) -- rather than
    inventing a number.
    """
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        return None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            total_kb = next(
                int(line.split()[1]) for line in f if line.startswith("MemTotal:")
            )
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    total_bytes = total_kb * 1024

    caps: dict[str, int | None] = {}
    for unit in units:
        try:
            raw = subprocess.run(
                ["systemctl", "show", unit, "-p", "MemoryMax", "--value"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        # "infinity" (no cap) and an empty value (unknown unit) are both
        # "no bound to add" -- recorded as None so the caller can say which
        # units are actually constrained rather than silently counting zero.
        caps[unit] = int(raw) if raw.isdigit() else None

    capped = {u: v for u, v in caps.items() if v is not None}
    total_cap = sum(capped.values())
    return {
        "physical_bytes": total_bytes,
        "caps": caps,
        "capped_total_bytes": total_cap,
        "uncapped_units": [u for u, v in caps.items() if v is None],
        # Strictly greater: caps summing to exactly RAM already leaves nothing
        # for the kernel, sshd or journald, so treat equality as oversubscribed.
        "oversubscribed": total_cap >= total_bytes and bool(capped),
    }


# Coverage-regression watchdog (added 2026-08-25). Every defect found in the
# 2026-08 audit week was silent and cost exactly the resolved clusters H1's
# power depends on: a six-day Kalshi blackout (2,676 markets/day -> 15-23),
# m4_ensemble writing 7 rows against m0's 524 on one day and 12 against 499 on
# another, m5_nowcast at exactly zero for six days. All three would have shown
# here on the first night. None was caught by the heartbeat, which only proves
# the process is alive -- "degraded but running" is the gap this closes.
COVERAGE_LOOKBACK_DAYS = 14
COVERAGE_MIN_BASELINE = 20      # below this a median is noise, not a baseline
COVERAGE_ALERT_RATIO = 0.5      # a day under half a model's own trailing median


def coverage_regressions(conn, day: str | None = None,
                         lookback: int = COVERAGE_LOOKBACK_DAYS,
                         min_baseline: int = COVERAGE_MIN_BASELINE,
                         ratio: float = COVERAGE_ALERT_RATIO) -> list[dict[str, Any]]:
    """Per (model, venue), a day whose forecast count collapsed against that
    same series' trailing median.

    Compares each model against ITSELF, not against other models: coverage
    varies by design (M5 only covers weather/macro, M7 only matched pairs), so
    a cross-model comparison would fire constantly on the sleeping experts the
    brief deliberately tolerates.

    `day` defaults to the latest date that has any forecasts, NOT to today.
    That matters: called mid-bundle, "today" is a partially-written day and
    every model looks collapsed. The nightly caller passes its own run date
    once both passes are done; `lab status` gets the latest complete day by
    default. A watchdog that cries wolf is worse than none.
    """
    # The window is anchored on `day`, not on date('now'). Anchoring it on now
    # would make the function behave differently in production (day == today)
    # than when replayed over history, and a watchdog that cannot be checked
    # against the incidents it was built for is not one anybody will trust.
    anchor = day or "now"
    rows = conn.execute(
        """SELECT substr(f.ts, 1, 10) AS d, f.model_id AS model_id,
                  COALESCE(m.venue, 'polymarket') AS venue, COUNT(*) AS n
           FROM forecasts f LEFT JOIN markets m ON m.condition_id = f.condition_id
           WHERE substr(f.ts, 1, 10) > date(?, ?) AND substr(f.ts, 1, 10) <= date(?)
           GROUP BY 1, 2, 3""",
        (anchor, f"-{lookback + 1} days", anchor),
    ).fetchall()
    if not rows:
        return []

    series: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        series.setdefault((r["model_id"], r["venue"]), {})[r["d"]] = r["n"]
    target = day or max(r["d"] for r in rows)

    out: list[dict[str, Any]] = []
    for (model_id, venue), by_day in sorted(series.items()):
        prior = sorted(n for d, n in by_day.items() if d < target)[-lookback:]
        if len(prior) < 3:
            continue
        baseline = statistics.median(prior)
        if baseline < min_baseline:
            continue
        today = by_day.get(target, 0)
        if today < ratio * baseline:
            out.append({"model_id": model_id, "venue": venue, "day": target,
                        "n": today, "baseline": round(baseline, 1),
                        "ratio": round(today / baseline, 3) if baseline else 0.0})
    return out


def gather_status(config: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    conn = dbmod.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    out: dict[str, Any] = {"ts": now.isoformat(timespec="seconds")}

    # Ledger and universe counts.
    out["markets_by_tier"] = {
        r["tier"]: r["n"]
        for r in conn.execute("SELECT tier, COUNT(*) AS n FROM markets GROUP BY tier")
    }
    out["forecast_rows"] = conn.execute("SELECT COUNT(*) AS n FROM forecasts").fetchone()["n"]
    out["coverage_regressions"] = coverage_regressions(conn)
    out["resolutions"] = conn.execute("SELECT COUNT(*) AS n FROM resolutions").fetchone()["n"]

    # Snapshot freshness + gaps per tier.
    # NOT a seven-day frame. `tier_snapshot_timestamps` reduces one partition at
    # a time, and its own docstring already described this failure mode for the
    # report render -- this call site simply never used it. Materialising the
    # week here cost ~112MB of parquet expanded into polars strings, which fit
    # under lab-dashboard's 500MB cap while daily partitions were ~8MB and
    # stopped fitting when the 2026-08-22 concurrency change let the collector
    # reach its configured cadence and partitions grew to ~19MB. The dashboard
    # went into an OOM restart loop -- 736 restarts, killed about eight seconds
    # into each one.
    dates7 = _dates_back(now, 7)
    cadence = config["collect"]["snapshot_interval_minutes"]
    out["tiers"] = {}
    for tier in ("liquid", "tail"):
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT condition_id, category, venue FROM markets "
                "WHERE tier = ? AND active = 1 AND closed = 0",
                (tier,),
            )
        ]
        markets = [r["condition_id"] for r in rows]
        # `tracked_markets` is not the number the round works through, and
        # from 2026-09-07 the gap is large: Kalshi no longer snapshots sports
        # markets outside the null-control sample (PAP 9.26), which on that
        # date was 11,753 of the venue's 16,164 tail markets. Reporting only
        # the tracked count would repeat the failure this file already calls
        # out for the resolution backlog below -- a dashboard number that is
        # not the working set. Same predicate as the round, not a copy of it.
        _, unsampled = drop_unsampled_sports(
            conn, config, [r for r in rows if (r["venue"] or "polymarket") == "kalshi"])
        ts_sorted = tier_snapshot_timestamps(store, dates7, markets)
        last_age_min = None
        if ts_sorted:
            last_ts = datetime.fromisoformat(ts_sorted[-1]).replace(tzinfo=timezone.utc)
            last_age_min = round((now - last_ts).total_seconds() / 60, 1)
        out["tiers"][tier] = {
            "tracked_markets": len(markets),
            "snapshot_targets": len(markets) - unsampled,
            "unsampled_sports": unsampled,
            "last_snapshot_age_min": last_age_min,
            "gaps_24h": len(gaps_from_timestamps(
                ts_sorted, cadence_buckets(cadence[tier], now - timedelta(hours=24), now))),
            "gaps_7d": len(gaps_from_timestamps(
                ts_sorted, cadence_buckets(cadence[tier], now - timedelta(days=7), now))),
        }

    # Resolution-watcher lag. `backlog` is the watcher's OWN working set, not
    # just the closed=1 subset: reporting only the latter (17k of a real 42k)
    # is part of why the 2026-07-25 scan stall hid for weeks -- the number on
    # the dashboard was not the number the watcher was working through.
    # `oldest_check_age_h` is the direct stall signal: with the round-robin
    # cursor it should stay near one full sweep, and grow without bound if the
    # watcher ever wedges again.
    from lab.collect.resolutions import resolution_backlog_size

    oldest = conn.execute(
        """
        SELECT MIN(m.resolution_checked_ts) AS t FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.resolution_checked_ts IS NOT NULL
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        """,
        (now.isoformat(timespec="seconds"),),
    ).fetchone()["t"]
    unchecked = conn.execute(
        """
        SELECT COUNT(*) AS n FROM markets m
        LEFT JOIN resolutions r ON r.condition_id = m.condition_id
        WHERE r.condition_id IS NULL AND m.resolution_checked_ts IS NULL
          AND (m.closed = 1 OR (m.end_date_iso IS NOT NULL AND m.end_date_iso < ?))
        """,
        (now.isoformat(timespec="seconds"),),
    ).fetchone()["n"]
    # Infra invariant, not a data one -- but it belongs on the same screen,
    # because the failure it catches presents as a data outage (see
    # memory_budget's docstring).
    out["memory_budget"] = memory_budget()

    out["resolution_watcher"] = {
        "closed_unresolved": conn.execute(
            """
            SELECT COUNT(*) AS n FROM markets m
            LEFT JOIN resolutions r ON r.condition_id = m.condition_id
            WHERE m.closed = 1 AND r.condition_id IS NULL
            """
        ).fetchone()["n"],
        "backlog": resolution_backlog_size(conn),
        "never_checked": unchecked,
        "oldest_check_age_h": (
            round((now - datetime.fromisoformat(oldest).replace(tzinfo=timezone.utc))
                  .total_seconds() / 3600, 1)
            if oldest else None
        ),
    }

    # Per-venue collector health (Phase 10). Kalshi/Metaculus run a snapshot
    # loop; Manifold deliberately does not (guardrail 16: markets+resolutions
    # only, never a price time series) -- no last_snapshot_age for it.
    # Newest snapshot per venue, from the last two partitions only.
    venue_last_snapshot: dict[str, datetime] = {}
    for date in _dates_back(now, 2):
        vdf = store.read_range([date], columns=["ts", "venue"])
        if vdf.is_empty():
            continue
        for row in vdf.group_by("venue").agg(pl.col("ts").max()).to_dicts():
            ts = datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc)
            v = row["venue"] or "polymarket"
            if v not in venue_last_snapshot or ts > venue_last_snapshot[v]:
                venue_last_snapshot[v] = ts
        del vdf

    out["venues"] = {}
    for venue in ("kalshi", "metaculus", "manifold"):
        markets_n = conn.execute(
            "SELECT COUNT(*) AS n FROM markets WHERE venue = ?", (venue,)
        ).fetchone()["n"]
        resolutions_n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM resolutions r
            JOIN markets m ON m.condition_id = r.condition_id
            WHERE m.venue = ?
            """,
            (venue,),
        ).fetchone()["n"]
        closed_unresolved = conn.execute(
            """
            SELECT COUNT(*) AS n FROM markets m
            LEFT JOIN resolutions r ON r.condition_id = m.condition_id
            WHERE m.venue = ? AND m.closed = 1 AND r.condition_id IS NULL
            """,
            (venue,),
        ).fetchone()["n"]
        entry: dict[str, Any] = {
            "markets": markets_n, "resolutions": resolutions_n,
            "closed_unresolved": closed_unresolved,
        }
        if venue in ("kalshi", "metaculus"):
            # Two days, not seven: this is "how stale is the newest snapshot",
            # a question the last partitions answer completely. A venue silent
            # for more than two days reads as None, which is the alarm anyway.
            newest = venue_last_snapshot.get(venue)
            entry["last_snapshot_age_min"] = (
                round((now - newest).total_seconds() / 60, 1) if newest else None
            )
        out["venues"][venue] = entry

    # Today's LLM spend vs cap.
    today = utc_date_str(now)
    out["llm_spend_today_usd"] = round(dbmod.llm_spend_today(conn, today), 4)
    out["llm_daily_cap_usd"] = config["llm"]["daily_cost_cap_usd"]

    conn.close()
    return out


def format_status(status: dict[str, Any]) -> str:
    lines = [
        f"lab status @ {status['ts']}",
        f"  markets by tier: {status['markets_by_tier'] or 'none'}",
        f"  forecast rows: {status['forecast_rows']}   resolutions: {status['resolutions']}",
    ]
    regs = status.get("coverage_regressions") or []
    if regs:
        lines.append(f"  !! COVERAGE REGRESSION on {regs[0]['day']} -- {len(regs)} model/venue series:")
        for r in regs[:8]:
            lines.append(
                f"       {r['model_id']}@{r['venue']}: {r['n']} vs median {r['baseline']}"
                f" ({r['ratio']:.0%} of baseline)"
            )
    for tier, s in status["tiers"].items():
        age = s["last_snapshot_age_min"]
        age_str = f"{age}min" if age is not None else "never"
        tracked = f"tracked={s['tracked_markets']}"
        if s.get("unsampled_sports"):
            tracked += (f" (snapshotted={s['snapshot_targets']},"
                        f" unsampled_sports={s['unsampled_sports']})")
        lines.append(
            f"  [{tier}] {tracked} "
            f"last_snapshot_age={age_str} "
            f"gaps_24h={s['gaps_24h']} gaps_7d={s['gaps_7d']}"
        )
    rw = status["resolution_watcher"]
    lines.append(
        f"  resolution watcher: backlog={rw['backlog']} "
        f"(closed={rw['closed_unresolved']}, never_checked={rw['never_checked']}) "
        f"oldest_check={rw['oldest_check_age_h']}h"
    )
    for venue, v in status.get("venues", {}).items():
        if "last_snapshot_age_min" in v:
            age = v["last_snapshot_age_min"]
            age_str = f"{age}min" if age is not None else "never"
            lines.append(
                f"  [{venue}] markets={v['markets']} last_snapshot_age={age_str} "
                f"resolutions={v['resolutions']} closed_unresolved={v['closed_unresolved']}"
            )
        else:
            lines.append(
                f"  [{venue}] markets={v['markets']} resolutions={v['resolutions']} "
                f"closed_unresolved={v['closed_unresolved']} (no snapshot loop -- guardrail 16)"
            )
    mb = status.get("memory_budget")
    if mb:
        gb = 1024 ** 3
        caps = ", ".join(
            f"{u.removesuffix('.service')}={v / gb:.2f}G" if v is not None
            else f"{u.removesuffix('.service')}=uncapped"
            for u, v in mb["caps"].items()
        )
        line = (f"  memory caps: {caps} | sum={mb['capped_total_bytes'] / gb:.2f}G "
                f"of {mb['physical_bytes'] / gb:.2f}G RAM")
        if mb["oversubscribed"]:
            line += "  <-- OVERSUBSCRIBED: caps exceed RAM, a kernel OOM may never fire"
        lines.append(line)
    lines.append(f"  LLM spend today: ${status['llm_spend_today_usd']} / cap ${status['llm_daily_cap_usd']}")
    return "\n".join(lines)
