"""CLV-style early signal: does the market move toward the model's view?

For each forecast, take the market mid at t+24h / t+72h and measure drift
signed in the direction of the model's disagreement at freeze time:
  signed_drift = sign(p_model - p_market_at_ts) * (mid_later - p_market_at_ts)
Positive mean drift = the model tends to be early, not wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import polars as pl

from lab.store.snapshots import SnapshotStore, utc_date_str

# condition_id -> (sorted ts strings, parallel mid values). Built once per
# snapshot frame by `build_mid_index` and reused across every `_mid_at` call
# against that frame -- was a fresh `df.filter(condition_id ==)` linear scan
# per call (up to ~30k calls/report render); a report-scale frame made this
# the single largest cost after gap_windows. See tests/test_clv.py for the
# differential proof against the prior per-call-filter implementation.
# Numeric arrays, not Python lists of ISO strings and floats: at ~100 bytes per
# row the object version took the report render's peak to 1227MB (measured
# 2026-08-06 -- 444MB after the snapshot read, 1227MB after building this), and
# the render runs inside the collector's own cgroup. Epoch seconds order
# identically to the ISO-8601 UTC strings they replace, at 16 bytes per row.
MidIndex = dict[str, tuple[np.ndarray, np.ndarray]]


def build_mid_index(df: pl.DataFrame) -> MidIndex:
    if df.is_empty():
        return {}
    grouped = (
        df.select(
            pl.col("condition_id"),
            # Sliced to the naive part and parsed as UTC. Every stored ts is
            # exactly "YYYY-MM-DDTHH:MM:SS+00:00" (guardrail 6: `now_utc()` is
            # the only clock; verified uniform across the live archive), and the
            # string form this replaces was compared lexically, which likewise
            # only works because the offset never varies.
            pl.col("ts").str.slice(0, 19)
              .str.to_datetime(format="%Y-%m-%dT%H:%M:%S", time_unit="us", strict=False)
              .dt.epoch("s").alias("_epoch"),
            pl.col("mid").cast(pl.Float64),
        )
        .drop_nulls("_epoch")
        .sort("_epoch")
        .group_by("condition_id")
        .agg([pl.col("_epoch"), pl.col("mid")])
    )
    return {
        cid: (np.asarray(epochs, dtype=np.int64), np.asarray(mids, dtype=np.float64))
        for cid, epochs, mids in grouped.iter_rows()
    }


def _mid_at(index: MidIndex, condition_id: str, target_ts: datetime,
            tolerance_hours: float = 3.0) -> float | None:
    entry = index.get(condition_id)
    if entry is None:
        return None
    ts_arr, mid_arr = entry
    exact = target_ts.timestamp()          # sub-second kept, for the tie-break below
    target = int(exact)                    # the ISO `timespec="seconds"` this replaces
    lo = target - int(tolerance_hours * 3600)
    hi = target + int(tolerance_hours * 3600)
    # [i, j) = indices with lo <= ts <= hi (ts_arr is sorted, per-market
    # timestamps are unique -- SnapshotStore.append dedups on (ts, condition_id)).
    i = int(np.searchsorted(ts_arr, lo, side="left"))
    j = int(np.searchsorted(ts_arr, hi, side="right"))
    if i >= j:
        return None
    # The minimizer of |ts - target| within a sorted range is always the
    # insertion point or its immediate predecessor -- no need to scan the rest.
    k = i + int(np.searchsorted(ts_arr[i:j], target, side="left"))
    best_idx, best_dist = None, None
    for idx in (k - 1, k):
        if i <= idx < j:
            dist = abs(float(ts_arr[idx]) - exact)
            # Strict '<': on an exact tie, keep the earlier (lower idx)
            # candidate -- k-1 is tried first, so this prefers it. The prior
            # per-call implementation used polars' unstable sort here, whose
            # tie-break was already undefined; this makes it deterministic
            # rather than changing any currently-reproducible output.
            if best_dist is None or dist < best_dist:
                best_idx, best_dist = idx, dist
    if best_idx is None:
        return None
    mid = float(mid_arr[best_idx])
    # A null mid (a venue without an order book, e.g. a hidden Metaculus CP)
    # arrives as NaN here rather than None -- never impute it (guardrail 16).
    return None if np.isnan(mid) else mid


def _overlaps_gap(window_start: datetime, window_end: datetime,
                  gaps: list[tuple[datetime, datetime]]) -> bool:
    return any(window_start < g_end and g_start < window_end for g_start, g_end in gaps)


# `_mid_at` only ever reads these three columns; every CLV read projects to
# them so a multi-week drift window never materializes the order-book blobs.
CLV_SNAPSHOT_COLUMNS = ["ts", "condition_id", "mid"]


def clv_dates(forecasts: list[dict], horizons_hours: list[int]) -> set[str]:
    """The set of YYYY-MM-DD snapshot partitions `clv_drift` needs for these
    forecasts and horizons. Exposed so a caller scoring many model_ids over the
    same recent window can union them and read the store ONCE, then hand the
    result to each `clv_drift` via `snapshots=` -- avoiding one full read per
    model (the report render's dominant redundant-I/O cost)."""
    dates: set[str] = set()
    for f in forecasts:
        ts = datetime.fromisoformat(f["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        for h in horizons_hours:
            dates.add(utc_date_str(ts + timedelta(hours=h)))
            dates.add(utc_date_str(ts + timedelta(hours=h) - timedelta(days=1)))
    return dates


def clv_drift(forecasts: list[dict], store: SnapshotStore, horizons_hours: list[int],
              gap_windows: list[tuple[datetime, datetime]] | None = None,
              snapshots: pl.DataFrame | None = None,
              mid_index: "MidIndex | None" = None,
              ) -> dict[int, dict[str, float]]:
    """forecasts: rows with ts, condition_id, model_id, p_yes, p_market_at_ts.

    `gap_windows` (Phase 17 item 5, `collect/status.py::gap_windows`): a
    forecast's drift window [ts, ts+horizon] overlapping any recorded
    collection gap is excluded from the mean and counted separately
    (`dropped_for_gap`) rather than silently folded into "no data" -- a gap
    is a known-missing measurement, not the same failure mode as no snapshot
    existing near the target time at all.

    `snapshots`: an already-loaded (ts, condition_id, mid) frame covering at
    least this forecast set's `clv_dates`. When given, the internal store read
    is skipped -- callers scoring many models over one window read once and
    share it (see `clv_dates`). A superset frame is fine: `_mid_at` filters by
    condition_id and ts, so extra rows are simply ignored.

    `mid_index`: the sorted lookup structure `build_mid_index` derives from
    that frame. Sharing the FRAME across models was not enough -- the index
    was still rebuilt per call, and rebuilding it is a full sort plus group_by
    plus per-market list materialisation over the whole frame. On the
    production host that was 10.5 minutes of a 12-minute render (measured
    2026-07-28: nine of the ten render phases finished in 31s, this loop took
    the rest) and the source of its sawtooth 290MB-815MB memory profile, since
    each rebuild allocated and freed the same large structure. Callers scoring
    many models over one window should build it once and pass it here.
    """
    if not forecasts:
        return {}
    gaps = gap_windows or []
    parsed = []
    for f in forecasts:
        ts = datetime.fromisoformat(f["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((f, ts))
    if mid_index is None:
        if snapshots is not None:
            df = snapshots
        else:
            # Filtered to these forecasts' own markets: the date span is the
            # whole archive, but only a few markets in it are ever looked up.
            df = store.read_range(sorted(clv_dates(forecasts, horizons_hours)),
                                  columns=CLV_SNAPSHOT_COLUMNS,
                                  condition_ids={f["condition_id"] for f in forecasts})
        mid_index = build_mid_index(df)

    out: dict[int, dict[str, float]] = {}
    for h in horizons_hours:
        drifts: list[float] = []
        dropped_for_gap = 0
        for f, ts in parsed:
            disagreement = f["p_yes"] - f["p_market_at_ts"]
            if abs(disagreement) < 1e-9:
                continue
            window_end = ts + timedelta(hours=h)
            if gaps and _overlaps_gap(ts, window_end, gaps):
                dropped_for_gap += 1
                continue
            later_mid = _mid_at(mid_index, f["condition_id"], window_end)
            if later_mid is None:
                continue
            drifts.append(float(np.sign(disagreement)) * (later_mid - f["p_market_at_ts"]))
        out[h] = {
            "n": len(drifts),
            "mean_signed_drift": float(np.mean(drifts)) if drifts else float("nan"),
            "dropped_for_gap": dropped_for_gap,
        }
    return out


def chunked_clv_rows(forecasts_by_model: dict[str, list[dict]], model_ids: list[str],
                     store: SnapshotStore, dates: list[str], horizons_hours: list[int],
                     gap_windows: list[tuple[datetime, datetime]] | None = None,
                     markets_per_chunk: int = 500) -> tuple[list[dict], int]:
    """Per-(model, horizon) mean signed drift, scored market-chunk by market-chunk.

    Exactly the numbers a single all-markets pass gives -- a forecast's drift
    reads only its OWN market's snapshots (`_mid_at` is keyed by
    condition_id), and a mean recombines exactly from (sum, n) -- with peak
    memory set by the chunk instead of by the archive (2026-09-25). The single
    pass read every snapshot of every market any model had recently forecast
    into one frame and then built an index on top; once the Kalshi liquid tier
    reached ~1,800 markets at a 5-minute cadence that read alone took the
    report child from ~260MB to ~800MB, the index build never finished, and
    every render from 2026-09-20 was OOM-killed at that exact phase.

    Returns (rows in model_ids x horizons_hours order, total dropped_for_gap).
    """
    all_cids = sorted({f["condition_id"] for fs in forecasts_by_model.values() for f in fs})
    totals: dict[tuple[str, int], list[float]] = {}
    dropped = 0
    for start in range(0, len(all_cids), max(1, markets_per_chunk)):
        chunk = set(all_cids[start:start + markets_per_chunk])
        frame = store.read_range(dates, columns=CLV_SNAPSHOT_COLUMNS, condition_ids=chunk)
        index = build_mid_index(frame)
        del frame
        for model_id in model_ids:
            in_chunk = [f for f in forecasts_by_model.get(model_id, [])
                        if f["condition_id"] in chunk]
            if not in_chunk:
                continue
            for horizon, stats in clv_drift(in_chunk, store, horizons_hours,
                                            gap_windows=gap_windows, mid_index=index).items():
                dropped += stats.get("dropped_for_gap", 0)
                if stats["n"]:
                    acc = totals.setdefault((model_id, horizon), [0.0, 0])
                    acc[0] += stats["mean_signed_drift"] * stats["n"]
                    acc[1] += stats["n"]
        del index
    rows = []
    for model_id in model_ids:
        for horizon in horizons_hours:
            total = totals.get((model_id, horizon))
            if total and total[1]:
                rows.append({"model_id": model_id, "horizon": horizon,
                             "n": int(total[1]), "drift": total[0] / total[1]})
    return rows, dropped


def clv_validity_check(conn, config: dict[str, Any], store: SnapshotStore) -> dict[str, Any]:
    """Phase 17 item 4: is the CLV signal itself trustworthy?

    Correlates per-forecast signed drift against per-forecast realized
    paired-Brier skill contribution (brier_market - brier_model) on the
    sports null control -- a sample that should show ~zero real skill for
    any model (brief section 3/7). If CLV drift correlates with realized
    skill even here, the two are confounded by something other than genuine
    predictive information (e.g. a shared stale-price artifact), and the CLV
    metric must not be trusted lab-wide until investigated.
    """
    from lab.forecast import null_control_ids_by_venue

    nc_ids_by_venue = null_control_ids_by_venue(conn, config)
    all_nc_ids: set[str] = set()
    for ids in nc_ids_by_venue.values():
        all_nc_ids |= ids
    if not all_nc_ids:
        return {"trusted": True, "reason": "no_null_control_data", "n": 0}

    horizon = config["eval"]["clv_horizons_hours"][0]
    placeholders = ",".join("?" * len(all_nc_ids))
    rows = conn.execute(
        f"""
        SELECT f.ts, f.condition_id, f.p_yes, f.p_market_at_ts, r.payout_yes
        FROM forecasts f JOIN resolutions r ON r.condition_id = f.condition_id
        WHERE f.condition_id IN ({placeholders})
        """,
        tuple(all_nc_ids),
    ).fetchall()
    if not rows:
        return {"trusted": True, "reason": "no_resolved_null_control_forecasts", "n": 0}

    all_dates: set[str] = set()
    parsed = []
    for row in rows:
        ts = datetime.fromisoformat(row["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((dict(row), ts))
        all_dates.add(utc_date_str(ts + timedelta(hours=horizon)))
        all_dates.add(utc_date_str(ts + timedelta(hours=horizon) - timedelta(days=1)))
    # Null control only. Unfiltered this read every partition in the archive --
    # 14.9M rows, ~675MB, then a Python index built over all of it -- to score a
    # handful of sports markets. It OOM-killed the orchestrator every hour from
    # 2026-08-02 to 08-06, right after `eval complete` and before the bundle
    # could record its own success, which is what made the loop self-sustaining.
    df = store.read_range(sorted(all_dates), columns=CLV_SNAPSHOT_COLUMNS,
                          condition_ids=all_nc_ids)
    mid_index = build_mid_index(df)

    drifts: list[float] = []
    skills: list[float] = []
    for f, ts in parsed:
        disagreement = f["p_yes"] - f["p_market_at_ts"]
        if abs(disagreement) < 1e-9:
            continue
        later_mid = _mid_at(mid_index, f["condition_id"], ts + timedelta(hours=horizon))
        if later_mid is None:
            continue
        y = f["payout_yes"]
        drifts.append(float(np.sign(disagreement)) * (later_mid - f["p_market_at_ts"]))
        skills.append((f["p_market_at_ts"] - y) ** 2 - (f["p_yes"] - y) ** 2)

    min_n = config["eval"].get("clv_null_control_min_n", 30)
    if len(drifts) < min_n:
        return {"trusted": True, "reason": "insufficient_n", "n": len(drifts)}

    with np.errstate(invalid="ignore"):
        correlation = float(np.corrcoef(drifts, skills)[0, 1])
    if np.isnan(correlation):  # zero variance in one series -- nothing to detect
        return {"trusted": True, "reason": "zero_variance", "n": len(drifts)}

    max_corr = config["eval"].get("clv_null_control_max_corr", 0.15)
    return {"trusted": abs(correlation) <= max_corr, "correlation": correlation, "n": len(drifts)}


def update_clv_trust_flag(conn, config: dict[str, Any], store: SnapshotStore) -> dict[str, Any]:
    """Run the validity check and persist the result -- only when the check
    actually ran with enough data (an abstention must not silently clear a
    previously-raised untrusted flag, since it re-verified nothing)."""
    from lab.store.db import set_meta

    result = clv_validity_check(conn, config, store)
    if "correlation" in result:
        set_meta(conn, "clv_trusted", "1" if result["trusted"] else "0")
        conn.commit()
    return result
