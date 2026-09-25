"""Mirror lab results -- curated (reports/exports/model artifacts) and raw
(data/lab.db, data/snapshots/) -- into a private git checkout and push.

This is the offsite backup CLAUDE.md calls for (Sec. 11: "the historical
order-book snapshots cannot be re-downloaded later") plus a visible feed of
model output, in one place. Runs as the last step of the nightly forecast
service (see collect/runner.py); never raises -- a failed publish must never
block or re-trigger the forecast/eval/report bundle it follows.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from lab.util import PROJECT_ROOT, now_utc, now_utc_iso

import logging

log = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def sync_env(results_dir: Path, env_path: Path) -> None:
    """Back up .env (every API key/secret this lab uses) into the private
    results repo so a dead laptop doesn't mean re-requesting every key from
    scratch. Named .env.backup, not .env -- nothing in the results repo's own
    tooling could accidentally load it as active config.

    Security tradeoff, not free: the results repo is confirmed private, but
    committing here means every key value ever set lives in that repo's git
    history permanently, including after rotation (rotating the key in the
    provider's dashboard does not erase old commits). This is a deliberate,
    explicit choice to prioritize "don't lose the keys" over "minimize where
    secrets ever touched disk" -- acceptable for a solo-operator private repo,
    worth reconsidering before ever adding a second collaborator to it.
    """
    if not env_path.exists():
        return
    dst = results_dir / ".env.backup"
    shutil.copy2(env_path, dst)


def sync_reports(results_dir: Path, reports_dir: Path) -> None:
    if not reports_dir.exists():
        return
    dst = results_dir / "reports"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(reports_dir, dst)


def sync_model_artifacts(results_dir: Path, models_dir: Path) -> None:
    if not models_dir.exists():
        return
    dst = results_dir / "models"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(models_dir, dst)


def sync_export(results_dir: Path, conn: sqlite3.Connection) -> None:
    from lab.export import export_jsonl

    out_path = results_dir / "exports" / "latest_forecasts.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = list(export_jsonl(conn))
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def sync_db(results_dir: Path, db_path: Path) -> None:
    """Consistent copy via SQLite's backup API -- safe against a concurrently
    writing WAL connection, unlike a raw file copy.

    Always starts from a fresh destination file: a stale results-repo
    checkout can leave an unsmudged Git LFS pointer stub in place of the real
    binary (e.g. `git lfs pull` was never run there) -- sqlite3.backup()
    raises "file is not a database" trying to write into that, since it
    isn't a valid SQLite header. Removing any existing destination first
    means the backup always starts from nothing, regardless of what was
    there before."""
    if not db_path.exists():
        return
    dst_dir = results_dir / "data"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / "lab.db"
    dst_path.unlink(missing_ok=True)
    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(dst_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


# Append-only research tables and the column each one is dated by. Deliberately
# NOT every table: `eval_runs` and `wealth_ledger` are derived and recomputable
# from these plus `markets` (CLAUDE.md sec. 5 says so of wealth_ledger in as many
# words), and `markets` is upsert-shaped venue metadata that the weekly db push
# carries and both venues can re-serve. What is here is what cannot be
# reconstructed from anything: a frozen probability, the outcome it was scored
# against, and the evidence a forecast was built from.
LEDGER_TABLES = {"forecasts": "ts", "resolutions": "resolved_ts", "evidence_runs": "ts"}


def sync_ledger_increment(results_dir: Path, conn: sqlite3.Connection,
                          max_days: int = 7) -> dict[str, int]:
    """Mirror the append-only ledger one gzipped JSONL per closed UTC day.

    Why this exists: `data/lab.db` is pushed weekly (publish.raw_data.
    db_interval_days), so the ONE irreplaceable artifact in this project had a
    seven-day recovery point while the snapshots describing it had a one-day
    one. Measured 2026-08-22, five days after the last db push: 90,767 forecast
    rows, 9,595 resolutions and 588 evidence runs existed only on the
    collecting host. A day of appended rows gzips to a few MB, so this closes
    the gap without touching LFS or moving a gigabyte.

    **Stateless by construction.** Which days are already mirrored is read from
    the files themselves, never from a watermark in `meta` -- a watermark can
    drift out of step with the artifact it describes (and a restore, or a db
    rollback, would silently do exactly that). The same reasoning as
    ledger_commitment's per-closed-day records.

    Only fully-past UTC days are written, so a file is never a partial day, and
    an existing file is never rewritten -- both properties the append-only
    ledger already has and this mirror should not weaken.

    Newest-first, bounded: the point is to shrink the recovery point NOW, so
    recent days go first and the historical backlog fills in behind over
    subsequent nights.
    """
    # Self-heal first: days mirrored before the manifest existed describe
    # nothing and cannot be verified until they do.
    backfill_ledger_manifest(results_dir, conn)

    today = now_utc().date().isoformat()
    written: dict[str, int] = {}
    for table, ts_col in LEDGER_TABLES.items():
        dst_dir = results_dir / "ledger" / table
        have = {p.name[: len(today)] for p in dst_dir.glob("*.jsonl.gz")} if dst_dir.exists() else set()
        days = [r[0] for r in conn.execute(
            f"SELECT DISTINCT substr({ts_col}, 1, 10) d FROM {table} "
            f"WHERE {ts_col} IS NOT NULL AND substr({ts_col}, 1, 10) < ? ORDER BY d DESC",
            (today,),
        )]
        pending = [d for d in days if d not in have][:max_days]
        if not pending:
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for day in pending:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE substr({ts_col}, 1, 10) = ? ORDER BY rowid", (day,)
            )
            # Written to a temp name and renamed, so an interrupted run cannot
            # leave a half-file that the `have` scan above would then treat as
            # a finished day and never revisit.
            tmp = dst_dir / f"{day}.jsonl.gz.tmp"
            n = 0
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(dict(row), separators=(",", ":"), sort_keys=True) + "\n")
                    n += 1
            tmp.replace(dst_dir / f"{day}.jsonl.gz")
            # A manifest line per dumped day, same shape as the public ledger
            # commitments: without a recorded row count and digest, a file that
            # went stale is indistinguishable from a current one, and an
            # unverifiable backup is a hope rather than a backup. Appended, not
            # rewritten -- the files it describes are never rewritten either.
            digest = hashlib.sha256()
            with open(dst_dir / f"{day}.jsonl.gz", "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            with open(results_dir / "ledger" / "manifest.jsonl", "a", encoding="utf-8") as mf:
                mf.write(json.dumps({"table": table, "date": day, "rows": n,
                                     "sha256": digest.hexdigest(),
                                     "written_ts": now_utc_iso()},
                                    separators=(",", ":"), sort_keys=True) + "\n")
            written[table] = written.get(table, 0) + 1
            log.info("ledger increment mirrored",
                     extra={"ctx": {"table": table, "date": day, "rows": n}})
    return written


def backfill_ledger_manifest(results_dir: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    """Give already-mirrored days the manifest line they were written without.

    The manifest arrived three days after the dump itself, so the days written
    in between describe nothing and `verify_ledger_increment` cannot check
    them -- a gap in exactly the mechanism that exists to make this backup
    checkable.

    **It reconciles rather than blesses.** A normal manifest line asserts "I
    wrote N rows out of the database"; a line written after the fact could only
    assert "this file contains N rows", which would rubber-stamp a file that
    had already gone stale. So the count is taken from the file, compared
    against the database for that day, and a line is written ONLY when the two
    agree. A disagreement is returned for the caller to surface -- that is a
    closed day whose row count moved, the invariant this dump and
    ledger_commitment.py both rest on.

    Idempotent, and folded into sync_ledger_increment so it self-heals rather
    than needing a command someone has to remember to run.
    """
    ledger_dir = results_dir / "ledger"
    if not ledger_dir.exists():
        return {"backfilled": 0, "mismatched": []}

    manifest = ledger_dir / "manifest.jsonl"
    have: set[tuple[str, str]] = set()
    if manifest.exists():
        with open(manifest, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                have.add((r["table"], r["date"]))

    added, mismatched = 0, []
    for table, ts_col in LEDGER_TABLES.items():
        for path in sorted((ledger_dir / table).glob("*.jsonl.gz")):
            day = path.name[: len("2026-08-26")]
            if (table, day) in have:
                continue
            digest = hashlib.sha256()
            n = 0
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for _ in fh:
                    n += 1
            live = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE substr({ts_col}, 1, 10) = ?", (day,)
            ).fetchone()[0]
            if live != n:
                mismatched.append({"table": table, "date": day, "file": n, "live": live})
                log.error("ledger manifest backfill: mirrored day disagrees with the database",
                          extra={"ctx": mismatched[-1]})
                continue
            with open(manifest, "a", encoding="utf-8") as mf:
                mf.write(json.dumps({"table": table, "date": day, "rows": n,
                                     "sha256": digest.hexdigest(),
                                     "written_ts": now_utc_iso(), "backfilled": True},
                                    separators=(",", ":"), sort_keys=True) + "\n")
            added += 1
    if added or mismatched:
        log.info("ledger manifest backfilled",
                 extra={"ctx": {"added": added, "mismatched": len(mismatched)}})
    return {"backfilled": added, "mismatched": mismatched}


def verify_ledger_increment(results_dir: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    """Check every mirrored day against the database and against its own digest.

    Two failure modes, deliberately distinguished. `digest_mismatch` means the
    file on disk is not the file that was written -- corruption or tampering.
    `row_count_mismatch` means the file is intact but the database now holds a
    different number of rows for that day, which would mean a CLOSED day gained
    or lost rows: the invariant both this dump and `ledger_commitment.py` rest
    on. Batched commits (2026-08-25) make a crash leave a committed prefix
    rather than nothing, so that invariant is worth checking rather than
    assuming -- a same-day retry completes the day, but only a check can say
    so.
    """
    manifest = results_dir / "ledger" / "manifest.jsonl"
    if not manifest.exists():
        return {"checked": 0, "digest_mismatch": [], "row_count_mismatch": []}

    records: dict[tuple[str, str], dict[str, Any]] = {}
    with open(manifest, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            records[(r["table"], r["date"])] = r      # last write for a day wins

    digest_bad, rows_bad = [], []
    for (table, day), r in sorted(records.items()):
        path = results_dir / "ledger" / table / f"{day}.jsonl.gz"
        if not path.exists():
            digest_bad.append({"table": table, "date": day, "reason": "missing"})
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != r["sha256"]:
            digest_bad.append({"table": table, "date": day, "reason": "digest"})
        ts_col = LEDGER_TABLES[table]
        live = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE substr({ts_col}, 1, 10) = ?", (day,)
        ).fetchone()[0]
        if live != r["rows"]:
            rows_bad.append({"table": table, "date": day,
                             "mirrored": r["rows"], "live": live})
    return {"checked": len(records), "digest_mismatch": digest_bad,
            "row_count_mismatch": rows_bad}


def sync_bootstrap(results_dir: Path, bootstrap_dir: Path) -> int:
    """Mirror the M1/M1.x training set.

    Synced unconditionally with the curated artifacts rather than behind a
    raw_data knob: it is one ~50MB file that changes only when the training
    set is rebuilt, and without it the fitted recalibration curves cannot be
    reproduced. It is derived from the HuggingFace bootstrap dataset by a
    filtering step, so it is reconstructible in principle -- but only while
    that dataset and this repo's filter agree, and at the cost of a 21-27GB
    download, which is not a backup.

    Found missing on 2026-08-22: the mirror held a 594KB predecessor committed
    by hand on 2026-07-08, while every refit since 2026-08-02 -- today's
    included, at n_train 1,967,376 -- has fitted on a 50MB file that had no
    off-host copy at all. `publish.py` had never mentioned `bootstrap`."""
    if not bootstrap_dir.exists():
        return 0
    dst_root = results_dir / "bootstrap"
    copied = 0
    for src_file in bootstrap_dir.rglob("*.parquet"):
        dst_file = dst_root / src_file.relative_to(bootstrap_dir)
        if dst_file.exists():
            src_stat, dst_stat = src_file.stat(), dst_file.stat()
            if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1
    return copied


def sync_snapshots(results_dir: Path, snapshots_dir: Path) -> int:
    """Mirror new/changed parquet partitions. Older date partitions are
    immutable once written, so this is normally a cheap incremental copy;
    only today's in-progress partition is re-copied on size/mtime change."""
    if not snapshots_dir.exists():
        return 0
    dst_root = results_dir / "data" / "snapshots"
    copied = 0
    for src_file in snapshots_dir.rglob("*.parquet"):
        rel = src_file.relative_to(snapshots_dir)
        dst_file = dst_root / rel
        if dst_file.exists():
            src_stat, dst_stat = src_file.stat(), dst_file.stat()
            if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1
    return copied


# The columns a reader needs to interpret the mirrored ledger: identity,
# venue, category, the resolution criteria text, dates, clustering and tier.
# Deliberately NOT liquidity/volume/last_synced_ts/resolution_checked_ts --
# they change on every hourly sync without changing what any row means, and
# including them would make this file differ every single week.
REFERENCE_MARKET_COLUMNS = (
    "condition_id", "venue", "venue_native_id", "slug", "question", "category",
    "description", "end_date_iso", "event_id", "neg_risk", "token_id_yes",
    "token_id_no", "tier", "active", "closed", "first_seen_ts",
)


def sync_reference_tables(results_dir: Path, conn: sqlite3.Connection) -> dict[str, int]:
    """The rows needed to READ the mirrored ledger, as plain-git gzip JSONL.

    Replaces the weekly Git LFS push of the whole data/lab.db (2026-09-25).
    That push had become impossible: the repository exceeded its LFS budget,
    GitHub refused the upload, and because the refused commit sat at the head
    of the unpushed range, every nightly ledger increment behind it was stuck
    on the host too -- nothing reached GitHub from 09-24 03:01 until the range
    was repaired by hand. LFS was the wrong medium for a monotonically growing
    blob anyway (no deltas, every version kept forever).

    The ledger increments already mirror forecasts, resolutions and
    evidence_runs daily. What they do not carry is what a forecast row MEANS:
    which venue, which category, which event cluster, what the resolution
    criteria said. So this writes exactly that, and only for markets that
    carry at least one forecast -- 267,904 market rows were 46.9MB gzipped on
    2026-09-25, growing by tens of thousands of unforecastable sports listings
    a week, which would have walked straight back into GitHub's 100MB per-file
    limit. The forecast-referenced subset is roughly a tenth of that.

    Deterministic bytes (sorted rows, sorted keys, gzip mtime=0), so a week
    with no metadata change produces an identical file and git stores nothing.
    """
    out_dir = results_dir / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    forecast_markets = "SELECT DISTINCT condition_id FROM forecasts"
    queries = {
        "markets": (f"SELECT {', '.join(REFERENCE_MARKET_COLUMNS)} FROM markets "
                    f"WHERE condition_id IN ({forecast_markets}) ORDER BY condition_id"),
        "events": ("SELECT * FROM events WHERE event_id IN (SELECT DISTINCT event_id FROM markets "
                   f"WHERE condition_id IN ({forecast_markets})) ORDER BY event_id"),
        "model_versions": "SELECT * FROM model_versions ORDER BY id",
    }
    counts: dict[str, int] = {}
    for name, query in queries.items():
        final = out_dir / f"{name}.jsonl.gz"
        tmp = out_dir / f"{name}.jsonl.gz.tmp"
        n = 0
        try:
            with open(tmp, "wb") as raw, gzip.GzipFile(
                    filename=f"{name}.jsonl", mode="wb", fileobj=raw, mtime=0) as gz:
                for row in conn.execute(query):
                    gz.write((json.dumps(dict(row), sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False) + "\n").encode("utf-8"))
                    n += 1
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
        counts[name] = n
    return counts


def publish_results(
    config: dict[str, Any],
    conn: sqlite3.Connection,
    results_dir: Path | None = None,
    push: bool = True,
    include_snapshots: bool = False,
    include_db: bool = False,
    include_env: bool = False,
    include_ledger: bool = False,
    include_reference: bool = False,
) -> dict[str, Any]:
    """Snapshots and the db are independent knobs, not one combined
    "raw data" flag: snapshots are cheap and incremental (only new/changed
    partitions copy, §Sec 11's "cannot be re-downloaded later" data), so they
    can run every night for free-tier LFS bandwidth. lab.db is a single ever-
    growing binary blob -- LFS has no delta compression for it, so every push
    transfers the FULL current size. Pushing it as often as snapshots would
    burn a GitHub LFS free-tier month's bandwidth (1GB) in days once the db
    passes a few hundred MB. run_publish_job gates `include_db` on an
    interval (publish.raw_data.db_interval_days) using a last-push timestamp
    in `meta` -- this function itself just does what it's told for either
    flag, independently. `include_env` is a third, separate knob: .env is
    tiny (no LFS bandwidth concern) so it needs no interval gating, but see
    sync_env's own docstring for the security tradeoff of backing up secrets
    into git history at all, even a private repo's."""
    pub_cfg = config.get("publish", {})
    results_dir = results_dir or (PROJECT_ROOT.parent / pub_cfg.get("results_dir", "../Polymarket-results"))
    results_dir = Path(results_dir).resolve()

    if not (results_dir / ".git").exists():
        return {"skipped": "results_dir_not_a_git_checkout", "results_dir": str(results_dir)}

    storage = config["storage"]
    sync_reports(results_dir, PROJECT_ROOT / storage["reports_dir"])
    sync_model_artifacts(results_dir, PROJECT_ROOT / storage["models_dir"])
    sync_export(results_dir, conn)
    n_bootstrap = sync_bootstrap(
        results_dir, PROJECT_ROOT / storage.get("bootstrap_dir", "data/bootstrap"))
    n_ledger: dict[str, int] = {}
    if include_ledger:
        n_ledger = sync_ledger_increment(results_dir, conn)
    n_snapshots = 0
    if include_snapshots:
        n_snapshots = sync_snapshots(results_dir, PROJECT_ROOT / storage["snapshots_dir"])
    if include_db:
        sync_db(results_dir, PROJECT_ROOT / storage["db_path"])
    n_reference: dict[str, int] = {}
    if include_reference:
        n_reference = sync_reference_tables(results_dir, conn)
    if include_env:
        sync_env(results_dir, PROJECT_ROOT / ".env")

    _run_git(["add", "-A"], results_dir)
    diff = _run_git(["diff", "--cached", "--quiet"], results_dir)
    if diff.returncode == 0:
        # "Nothing new to commit" is NOT "everything is on the remote": a
        # commit refused last night is still sitting here unpushed. Retry it,
        # and report pushed=True only when nothing on this host is missing
        # from the remote (2026-09-25 -- treating no_changes as backed up
        # cleared the backup alarm the night after a refusal).
        out: dict[str, Any] = {"committed": False, "reason": "no_changes"}
        if push:
            ahead = _run_git(["rev-list", "--count", "@{u}..HEAD"], results_dir)
            try:
                n_ahead = int((ahead.stdout or "0").strip() or 0) if ahead.returncode == 0 else 0
            except ValueError:
                n_ahead = 0
            if n_ahead:
                pushed = _run_git(["push"], results_dir)
                out.update(unpushed_commits=n_ahead, pushed=pushed.returncode == 0)
                if pushed.returncode != 0:
                    out["push_stderr"] = pushed.stderr
                    log.error("results push REFUSED again -- %d commit(s) still not off the host",
                              n_ahead, extra={"ctx": {"stderr": (pushed.stderr or "")[-500:]}})
            else:
                out["pushed"] = True
        return out

    ts = now_utc_iso()
    commit = _run_git(["commit", "-m", f"Results update {ts}"], results_dir)
    if commit.returncode != 0:
        return {"committed": False, "reason": "commit_failed", "stderr": commit.stderr}

    result = {"committed": True, "ts": ts, "snapshot_files_copied": n_snapshots,
             "bootstrap_files_copied": n_bootstrap, "ledger_days_mirrored": n_ledger,
             "reference_rows": n_reference,
             "db_included": include_db, "env_included": include_env}
    if push:
        pushed = _run_git(["push"], results_dir)
        result["pushed"] = pushed.returncode == 0
        if not result["pushed"]:
            result["push_stderr"] = pushed.stderr
            # ERROR, not a field inside an INFO "complete" line. From
            # 2026-09-24 the push was refused every night ("exceeded its LFS
            # budget") and the only record was that field, inside a line
            # that read like success. The backup had stopped leaving the host
            # and nothing on any screen said so.
            log.error("results push REFUSED -- this night's backup did not leave the host",
                      extra={"ctx": {"stderr": (pushed.stderr or "")[-500:]}})
        elif include_db:
            result["lfs_prune"] = prune_lfs(results_dir)
    return result


def prune_lfs(results_dir: Path) -> dict[str, Any]:
    """Drop local LFS objects that are already safely on the remote.

    lab.db is pushed whole every `db_interval_days` and LFS has no delta
    compression for it, so each push writes a NEW object the size of the
    entire database and every previous one stays on disk forever. By
    2026-07-31 that was 8 objects totalling 7.5GB -- including a 2.5GB and a
    1.8GB copy of the pre-dedup database whose bloat had already been fixed
    weeks earlier. At ~230MB/day the VPS would have run out of disk around
    mid-September, well before the analysis freeze.

    Pruning here rather than on a timer because this is the function that
    creates those objects: whatever retires them belongs next to whatever
    makes them, and only a successful db push can leave a new one behind.

    `--verify-remote` is not optional for this repo. Prune deletes local
    copies, and the default trusts its own bookkeeping about what the remote
    holds; for the backup of data that cannot be re-collected (brief §11),
    every object is confirmed present on GitHub before its local copy goes.
    Never raises: a failed prune costs disk, while a failed publish would
    cost a backup (guardrail 9).
    """
    proc = subprocess.run(
        ["git", "lfs", "prune", "--verify-remote"],
        cwd=results_dir, capture_output=True, text=True,
    )
    ok = proc.returncode == 0
    if not ok:
        log.warning("lfs prune failed -- disk not reclaimed, backup unaffected",
                    extra={"ctx": {"stderr": (proc.stderr or "")[:300]}})
    # git-lfs writes its progress/summary to stderr, not stdout.
    summary = [ln for ln in (proc.stderr or "").splitlines() if "prune" in ln.lower()]
    return {"ok": ok, "summary": summary[-2:]}
