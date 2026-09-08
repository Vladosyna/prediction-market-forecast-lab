"""Phase 15 addendum (v2.8): automated weekly `lab export --paper` snapshot,
committed and pushed to the public repo -- closes the gap where the manual
`lab export --paper` CLI flow (Phase 15) was never scheduled or auto-committed
anywhere. Writes docs/paper_exports/YYYY-MM-DD.jsonl + a matching
<same-name>.jsonl.meta.json manifest -- the identical "<out>.meta.json" naming
convention `lab export --paper --out <path>` already uses -- reusing
export.py's export_paper_jsonl / paper_export_manifest verbatim. Committed to
the SAME public repo docs/ledger_commitments.jsonl already lives in, for the
same independent-verifiability rationale (see ledger_commitment.py's own
docstring).

Design notes:
- A local _run_git mirrors ledger_commitment.py's and publish.py's own copies
  of the same three-line subprocess wrapper rather than importing either --
  this codebase's own precedent (publish.py already duplicates it rather than
  sharing with ledger_commitment.py) is to keep this trivial, well-understood
  helper local to each caller instead of introducing a shared abstraction.
- Revert-on-failure differs from ledger_commitment.py on purpose:
  ledger_commitment reverts by trimming N appended JSONL lines from one
  ever-growing file; this module instead deletes the two whole files it just
  created, since each week's export is its own dated file pair, not an
  append.
- Idempotent per calendar date: if today's dated file pair already exists,
  the job is a no-op -- the health check's overdue-service catch-up must not
  produce a second, conflicting export for a date already snapshotted.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from lab.gitutil import push_with_rebase
from lab.util import PROJECT_ROOT, now_utc

log = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def paper_export_paths(config: dict[str, Any], dt=None) -> tuple[Path, Path]:
    """(gz_path, meta_path) for "today" (or dt) -- meta_path mirrors the
    CLI's own "<out>.meta.json" convention exactly (out + ".meta.json").

    Gzipped from 2026-09-08. The weekly snapshot is a full cumulative re-dump,
    so it grew roughly linearly -- 0.67MB on 2026-07-10, 115MB on 09-06 --
    and crossed GitHub's 100MB per-file HARD limit, at which point the
    pre-receive hook refused the entire push range and took an already-written
    ledger commitment down with it. Measured on the 2026-08-30 export,
    gzip -9 gives 16.5x (96.1MB -> 5.8MB), which carries this well past the
    2026-12-31 freeze even at the highest weekly growth rate yet observed.

    Everything through 2026-08-30 stays plain `.jsonl` and published: this
    repo's history is the pre-registration record and is not rewritten.
    """
    dt = dt or now_utc()
    out_dir = PROJECT_ROOT / config.get("paper_export", {}).get("dir", "docs/paper_exports")
    stamp = dt.date().isoformat()
    gz_path = out_dir / f"{stamp}.jsonl.gz"
    meta_path = Path(f"{gz_path}.meta.json")
    return gz_path, meta_path


def write_paper_export(conn, config: dict[str, Any]) -> dict[str, Any]:
    """Write today's dated .jsonl.gz + manifest; no-op if they already exist."""
    from lab.export import export_paper_jsonl, paper_export_manifest

    gz_path, meta_path = paper_export_paths(config)
    # The legacy sibling matters exactly once, on the deploy's own calendar
    # date: without it a catch-up run would write a SECOND export pair for a
    # date already snapshotted as plain .jsonl, which is what the idempotence
    # gate exists to prevent.
    if gz_path.exists() or gz_path.with_suffix("").exists():
        return {"written": False, "reason": "already_exists", "path": str(gz_path)}

    gz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = gz_path.with_name(gz_path.name + ".tmp")
    row_count = 0
    try:
        # Streamed, not materialised: the row set is already ~700k lines and
        # only grows. mtime=0 and an explicit filename make two exports of the
        # same rows byte-identical -- gzip otherwise stamps time.time() and a
        # basename into the header, and §15 exists so a reviewer can re-derive
        # an artifact and get the same bytes.
        with open(tmp_path, "wb") as raw:
            with gzip.GzipFile(filename=gz_path.name[:-3], mode="wb",
                               fileobj=raw, compresslevel=9, mtime=0) as gz:
                with io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as text:
                    for line in export_paper_jsonl(conn):
                        text.write(line)
                        text.write("\n")
                        row_count += 1

        # Built BEFORE the rename, deliberately. code_version() hashes every
        # src/lab/*.py plus config.yaml; if it raised after the payload was in
        # place we would leave a published-shaped .gz with no manifest beside
        # it, and the exists() gate above would then treat that date as done
        # forever -- breaking the two-file contract docs/paper_export_schema.md
        # states to reviewers.
        manifest = paper_export_manifest(conn, row_count)
        tmp_path.replace(gz_path)
        meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return {"written": True, "path": str(gz_path), "row_count": row_count,
            "code_version": manifest["code_version"]}


def _revert_new_files(*paths: Path) -> None:
    """Undo file creation if the git add/commit step fails -- mirrors
    ledger_commitment._revert_ledger_append's contract (a retry must see this
    date as not-yet-exported, never permanently orphaned)."""
    for p in paths:
        p.unlink(missing_ok=True)


def commit_and_push_paper_export(config: dict[str, Any], conn) -> dict[str, Any]:
    """Write (if new) and commit+push today's paper-export snapshot."""
    written = write_paper_export(conn, config)
    if not written.get("written"):
        return {"committed": False, **written}

    gz_path, meta_path = paper_export_paths(config)
    rel_paths = [str(p.relative_to(PROJECT_ROOT)) for p in (gz_path, meta_path)]
    # `.stem` on "2026-09-13.jsonl.gz" is "2026-09-13.jsonl", which would put a
    # container detail into a permanent public commit subject. Take the date.
    stamp = gz_path.name.split(".")[0]

    try:
        add = _run_git(["add", *rel_paths], PROJECT_ROOT)
        if add.returncode != 0:
            _revert_new_files(gz_path, meta_path)
            return {"error": "git_add_failed", "stderr": add.stderr}

        commit = _run_git(["commit", "-m", f"Paper export snapshot: {stamp}"], PROJECT_ROOT)
        if commit.returncode != 0:
            _revert_new_files(gz_path, meta_path)
            return {"error": "git_commit_failed", "stderr": commit.stderr}
    except Exception as exc:
        _revert_new_files(gz_path, meta_path)
        log.exception("paper export git step failed")
        return {"error": "git_step_exception", "detail": str(exc)}

    result: dict[str, Any] = {"committed": True, "path": str(gz_path),
                              "row_count": written["row_count"]}
    if config.get("paper_export", {}).get("push", True):
        try:
            pushed = push_with_rebase(PROJECT_ROOT)
            result["pushed"] = pushed.returncode == 0
            if not result["pushed"]:
                result["push_stderr"] = pushed.stderr
        except Exception as exc:
            result["pushed"] = False
            result["push_error"] = str(exc)
    return result
