"""Parquet snapshot store, partitioned data/snapshots/date=YYYY-MM-DD/*.parquet.

Dedup key is (ts_bucket, condition_id): ts is floored to the tier's cadence
bucket before writing, and appends drop rows whose key already exists in the
partition -- restart-safe by construction (guardrail 7).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = {
    "ts": pl.String,            # ISO-8601 UTC, floored to cadence bucket
    "condition_id": pl.String,
    "token_id_yes": pl.String,
    "best_bid": pl.Float64,
    "best_ask": pl.Float64,
    "mid": pl.Float64,
    "spread": pl.Float64,
    "bid_depth_usd": pl.Float64,
    "ask_depth_usd": pl.Float64,
    "last_trade_price": pl.Float64,
    # Full book depth (added 2026-07-02, additive/non-breaking): JSON arrays
    # of best-first [price, size] pairs, up to collect.book_depth_levels.
    # Null in partitions written before the change.
    "bids_json": pl.String,
    "asks_json": pl.String,
    # Phase 15 crowd-size covariate, added 2026-08-11 (additive/non-breaking).
    # Kalshi reports open interest on the market object the collector already
    # fetches, so this costs no request. NULL for venues that do not report one:
    # Polymarket's equivalent (holder counts) lives behind a separate Data API
    # call, which is not free at this collector's load and is not taken here.
    "open_interest": pl.Float64,
    # v1.9 multi-venue (Phase 10, additive/non-breaking): 'polymarket' (default,
    # so every pre-existing row and call site is unaffected), 'kalshi',
    # 'metaculus'. Venues without an order book (Metaculus community
    # prediction) store their probability in `mid` and leave book fields NULL.
    "venue": pl.String,
}

# What `latest_per_market` reads: everything except the order-book JSON blobs.
# Nothing in src/lab reads those back today -- they are collected for later
# depth analysis -- and they are the bulk of a row's bytes. Reading them was
# costing ~578MB per call against ~24MB for the same window projected, and
# `latest_per_market` is called 11 times across the forecast/universe/M3/M6/M7/
# shadow paths, several of them inside a single forecast bundle. A caller that
# genuinely needs a book blob can pass `columns` explicitly.
LATEST_PER_MARKET_COLUMNS = [c for c in SNAPSHOT_SCHEMA if c not in ("bids_json", "asks_json")]

# The single whole-day file every partition used before 2026-10-17 (see append).
LEGACY_PARTITION_FILE = "snapshots.parquet"


def floor_ts_bucket(ts: datetime, bucket_minutes: int) -> str:
    """Floor a UTC datetime to the cadence bucket, ISO-8601."""
    minute = (ts.minute // bucket_minutes) * bucket_minutes if bucket_minutes < 60 else 0
    floored = ts.replace(minute=minute, second=0, microsecond=0)
    if bucket_minutes >= 60:
        hour = (ts.hour // (bucket_minutes // 60)) * (bucket_minutes // 60)
        floored = floored.replace(hour=hour)
    return floored.isoformat(timespec="seconds")


class SnapshotStore:
    def __init__(self, snapshots_dir: str | Path) -> None:
        base = Path(snapshots_dir)
        self.base = base if base.is_absolute() else PROJECT_ROOT / base
        self.base.mkdir(parents=True, exist_ok=True)

    def _day_dir(self, date_str: str) -> Path:
        return self.base / f"date={date_str}"

    def _partition(self, date_str: str, hour: str) -> Path:
        return self._day_dir(date_str) / f"snapshots-{hour}.parquet"

    def partition_files(self, date_str: str) -> list[Path]:
        """Every parquet file of one date partition, in chronological order.

        From 2026-10-17 a day is written as one file per UTC hour of the
        bucketed `ts` (`snapshots-HH.parquet`); every day before that, and the
        morning of the switch-over, is a single `snapshots.parquet`. The
        whole-day file sorts first because on the switch-over day it holds the
        earliest rows. `*.tmp` files (an append in flight) never match.
        """
        day = self._day_dir(date_str)
        if not day.is_dir():
            return []
        return sorted(day.glob("*.parquet"),
                      key=lambda p: (p.name != LEGACY_PARTITION_FILE, p.name))

    def _existing_keys(self, files: list[Path], part: pl.DataFrame) -> pl.DataFrame:
        """The (ts, condition_id) keys of `part` already present in `files`.

        A semi-join inside a streaming scan of the two key columns: only the
        matching keys are ever materialised, never the day's keys (let alone
        its order-book blobs)."""
        wanted = part.select("ts", "condition_id").lazy()
        hits = []
        for p in files:
            try:
                hits.append(pl.scan_parquet(p).select("ts", "condition_id")
                            .join(wanted, on=["ts", "condition_id"], how="semi")
                            .collect(engine="streaming"))
            except Exception:
                # Same policy as read_range: one unreadable file is skipped
                # loudly, not allowed to stop collection for the rest of the day.
                log.error("snapshot partition unreadable, skipping",
                          extra={"ctx": {"path": str(p)}})
        if not hits:
            return part.select("ts", "condition_id").clear()
        return pl.concat(hits)

    def append(self, rows: list[dict]) -> int:
        """Append rows, deduplicating on (ts, condition_id). Returns rows written.

        One file per (date, hour of `ts`), 2026-10-17. Until then each append
        read the WHOLE day's partition -- order-book blobs included -- and
        rewrote it, so every snapshot round (the matched-pair job alone fires
        every two minutes) cost a transient allocation that grew with the day:
        measured on the orchestrator at +370MB with the day's file at 31MB,
        and ~+600MB by its end at ~51MB, inside the same cgroup as the nightly
        forecast and eval children, which the 10-16 and 10-17 OOM kills hit.
        An append now rewrites only its own hour's file (~1/24 of the day) and
        checks duplicates against the rest of the day by key alone. Readers
        are unaffected: §5's layout was always `date=YYYY-MM-DD/*.parquet`,
        every reader in this package goes through `partition_files`, and none
        depends on physical row order -- `(ts, condition_id)` is unique and
        every consumer sorts by `ts`.
        """
        if not rows:
            return 0
        rows = [
            {col: r.get(col, "polymarket" if col == "venue" else None) for col in SNAPSHOT_SCHEMA}
            for r in rows
        ]
        df = pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)
        written = 0
        # A batch can straddle midnight (and an hour); group rows into their files.
        df = df.with_columns(pl.col("ts").str.slice(0, 10).alias("_date"),
                             pl.col("ts").str.slice(11, 2).alias("_hour"))
        for (date_str, hour), part in df.partition_by(["_date", "_hour"], as_dict=True).items():
            part = part.drop("_date", "_hour")
            path = self._partition(date_str, hour)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing_files = self.partition_files(date_str)
            if existing_files:
                keys = self._existing_keys(existing_files, part)
                part = part.join(keys, on=["ts", "condition_id"], how="anti")
                if part.is_empty():
                    continue
            if path.exists():
                # diagonal: a file written before a column was added gets nulls.
                merged = pl.concat([pl.read_parquet(path), part], how="diagonal")
            else:
                merged = part
            # Write atomically: a full write to a temp file, then an atomic
            # rename over the real path (os.replace is atomic within a
            # filesystem on both POSIX and Windows). A reader -- or the next
            # append's read-back above -- therefore only ever sees a complete
            # partition, never the half-written/0-byte file an interrupted
            # in-place write.parquet() would leave behind (the corruption that
            # otherwise crashes read_range, and with it the whole report job).
            tmp = path.with_name(path.name + ".tmp")
            merged.write_parquet(tmp)
            tmp.replace(path)
            written += len(part)
        return written

    def read_range(self, dates: list[str], columns: list[str] | None = None,
                   condition_ids: Iterable[str] | None = None) -> pl.DataFrame:
        """Read snapshot partitions for the given YYYY-MM-DD dates (missing ones skipped).

        `columns`: optional projection. When given, only those columns are read
        from disk via a lazy `scan_parquet`, so the heavy `bids_json`/`asks_json`
        order-book blobs (the bulk of a row's bytes) are never materialized when
        a caller only needs a few scalar columns -- the status/CLV/gap-window
        readers, which span weeks of history, only ever touch ts/condition_id and
        one of {mid, venue}. Defaults to None: the full schema, byte-for-byte the
        same result every existing caller already gets. A projected column absent
        from an older partition (columns are additive over time, e.g. `venue`,
        `bids_json`) comes back null via the diagonal concat below -- exactly as a
        full read already handles it.

        `condition_ids`: optional row filter, pushed into the same scan. Several
        callers span the whole archive but care about a handful of markets -- the
        CLV validity check reads every partition to score the sports null
        control, the cross-venue correlation reads 90 days to correlate a few
        confirmed pairs. Projection alone does not save them: 30 partitions at
        three columns is still 14.9M rows and ~675MB, which is what OOM-killed
        the orchestrator on 2026-08-06. Filtering at read time is the difference
        between that and a few tens of thousands of rows.
        """
        present = [p for d in dates for p in self.partition_files(d)]
        wanted = list(dict.fromkeys(condition_ids)) if condition_ids is not None else None
        frames = []
        for p in present:
            try:
                if columns is None and wanted is None:
                    frames.append(pl.read_parquet(p))
                else:
                    lf = pl.scan_parquet(p)
                    available = set(lf.collect_schema().names())
                    if columns is not None:
                        lf = lf.select([c for c in columns if c in available])
                    if wanted is not None:
                        lf = lf.filter(pl.col("condition_id").is_in(wanted))
                    frames.append(lf.collect())
            except Exception:
                # A truncated or 0-byte partition -- e.g. a write interrupted
                # before the atomic rename in append() landed, as happens on an
                # abrupt shutdown -- must not abort the whole read and, via the
                # report/eval path, take down (and crash-loop) the orchestrator.
                # Skip that one day's file loudly rather than everything at once
                # (guardrail 9: fail soft, log loud). append() now writes
                # atomically so new files can't land in this state.
                log.error("snapshot partition unreadable, skipping",
                          extra={"ctx": {"path": str(p)}})
        if not frames:
            schema = SNAPSHOT_SCHEMA if columns is None else {c: SNAPSHOT_SCHEMA[c] for c in columns}
            return pl.DataFrame(schema=schema)
        return pl.concat(frames, how="diagonal")

    def latest_per_market(self, dates: list[str], columns: list[str] | None = None,
                          condition_ids: Iterable[str] | None = None) -> pl.DataFrame:
        """Most recent snapshot row per condition_id across `dates`.

        Reduced one day at a time rather than over a single concatenated frame:
        the result is the same row either way (`ts` is ISO-8601, so lexical and
        chronological order agree, and the last-of-the-per-day-lasts is the
        overall last), but the peak is one day's partition instead of the whole
        window. Defaults to `LATEST_PER_MARKET_COLUMNS` -- see there for why the
        order-book blobs are excluded. `condition_ids` filters inside the scan,
        for callers that need a handful of markets rather than all of them.
        """
        cols = columns if columns is not None else LATEST_PER_MARKET_COLUMNS
        wanted = list(condition_ids) if condition_ids is not None else None
        reduced = []
        for date in dates:
            day = self.read_range([date], columns=cols, condition_ids=wanted)
            if not day.is_empty():
                reduced.append(day.sort("ts").group_by("condition_id").last())
        if not reduced:
            return self.read_range([], columns=cols)   # correctly-typed empty frame
        if len(reduced) == 1:
            return reduced[0]
        return pl.concat(reduced, how="diagonal").sort("ts").group_by("condition_id").last()


def utc_date_str(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
