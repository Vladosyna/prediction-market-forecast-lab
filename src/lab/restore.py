"""Rebuild a lab database from the private results mirror (restore drill).

The mirror stopped carrying a whole `data/lab.db` on 2026-09-25 (the LFS budget
ran out, see publish.sync_reference_tables). What it carries instead is the
append-only ledger, one gzipped JSONL per closed UTC day with a digest in
`ledger/manifest.jsonl`, plus `reference/` -- the rows needed to read it. This
module turns those files back into a database, so the backup can be tested
rather than trusted: an untested backup is a hope, not a backup (brief Phase 18).

What comes back: every mirrored forecast, resolution and evidence run with its
original id (the ledger commitments name id ranges, and forecasts point at
evidence runs by id), the markets and events that forecasts refer to, and the
model registry. What does not: derived tables (eval_runs, wealth_ledger --
recomputable), operational state (meta, cursors), the collector's view of
markets nothing has forecast, and anything newer than the last mirrored day.
Resolutions of markets outside the reference set are counted and skipped: they
cannot be interpreted without their market, and no forecast depends on them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from lab.store import db

log = logging.getLogger(__name__)

# Insert order matters: forecasts point at evidence runs, resolutions at markets.
LEDGER_ORDER = ("evidence_runs", "forecasts", "resolutions")
REFERENCE_ORDER = ("markets", "events", "model_versions")
BATCH = 5000


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(results_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    path = results_dir / "ledger" / "manifest.jsonl"
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                records[(r["table"], r["date"])] = r      # last write for a day wins
    return records


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _insert(conn, table: str, rows: list[dict[str, Any]], allowed: set[str]) -> int:
    """Insert dicts whose keys may predate or postdate this schema: only the
    columns both sides know are written, which is how a dump from before a
    migration still loads."""
    if not rows:
        return 0
    keys = sorted({k for r in rows for k in r} & allowed)
    placeholders = ",".join("?" * len(keys))
    conn.executemany(
        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders})",
        [tuple(r.get(k) for k in keys) for r in rows],
    )
    return len(rows)


def restore_from_mirror(results_dir: Path, db_path: Path) -> dict[str, Any]:
    """Build a NEW database at `db_path` from the mirror at `results_dir`.

    Refuses to touch an existing file. Returns a report: per-table row counts,
    files whose digest or row count disagrees with the manifest, files the
    manifest does not describe, and resolutions skipped for want of a market.
    """
    results_dir, db_path = Path(results_dir), Path(db_path)
    if db_path.exists():
        raise FileExistsError(f"{db_path} exists -- a restore never overwrites a database")
    manifest = _manifest(results_dir)
    report: dict[str, Any] = {"rows": {}, "digest_mismatch": [], "row_count_mismatch": [],
                              "unlisted_files": [], "orphan_resolutions": 0}

    conn = db.connect(db_path)
    try:
        for name in REFERENCE_ORDER:
            path = results_dir / "reference" / f"{name}.jsonl.gz"
            if not path.exists():
                report["rows"][name] = 0
                continue
            allowed = set(_columns(conn, name))
            n, batch = 0, []
            for row in _rows(path):
                batch.append(row)
                if len(batch) >= BATCH:
                    n += _insert(conn, name, batch, allowed)
                    batch = []
            n += _insert(conn, name, batch, allowed)
            report["rows"][name] = n
        conn.commit()

        # Markets first forecast after the last weekly dump (2026-09-30): each
        # closed day's file describes the markets that day's forecasts
        # introduced. The weekly dump is newer for everything it contains, so
        # it wins and these only fill in what it lacks.
        daily_dir = results_dir / "reference" / "markets_daily"
        daily_manifest = {}
        if (daily_dir / "manifest.jsonl").exists():
            with open(daily_dir / "manifest.jsonl", encoding="utf-8") as fh:
                for line in fh:
                    r = json.loads(line)
                    daily_manifest[r["date"]] = r
        allowed = set(_columns(conn, "markets"))
        added = 0
        for path in sorted(daily_dir.glob("*.jsonl.gz")) if daily_dir.exists() else []:
            day = path.name[: len("2026-01-01")]
            record = daily_manifest.get(day)
            if record is not None and _sha256(path) != record["sha256"]:
                report["digest_mismatch"].append({"table": "markets_daily", "date": day})
            rows = [r for r in _rows(path) if not conn.execute(
                "SELECT 1 FROM markets WHERE condition_id = ?", (r["condition_id"],)).fetchone()]
            added += _insert(conn, "markets", rows, allowed)
        conn.commit()
        report["rows"]["markets_daily"] = added

        known_markets = {r[0] for r in conn.execute("SELECT condition_id FROM markets")}
        for table in LEDGER_ORDER:
            allowed = set(_columns(conn, table))
            total = 0
            for path in sorted((results_dir / "ledger" / table).glob("*.jsonl.gz")):
                day = path.name[: len("2026-01-01")]
                record = manifest.get((table, day))
                if record is None:
                    report["unlisted_files"].append({"table": table, "date": day})
                elif _sha256(path) != record["sha256"]:
                    report["digest_mismatch"].append({"table": table, "date": day})
                rows = list(_rows(path))
                if record is not None and len(rows) != record["rows"]:
                    report["row_count_mismatch"].append(
                        {"table": table, "date": day, "file": len(rows), "manifest": record["rows"]})
                if table == "resolutions":
                    kept = [r for r in rows if r["condition_id"] in known_markets]
                    report["orphan_resolutions"] += len(rows) - len(kept)
                    rows = kept
                for i in range(0, len(rows), BATCH):
                    total += _insert(conn, table, rows[i:i + BATCH], allowed)
                conn.commit()
            report["rows"][table] = total
    finally:
        conn.close()
    log.info("restore from mirror complete", extra={"ctx": {
        "rows": report["rows"], "digest_mismatch": len(report["digest_mismatch"]),
        "row_count_mismatch": len(report["row_count_mismatch"]),
        "orphan_resolutions": report["orphan_resolutions"]}})
    return report
