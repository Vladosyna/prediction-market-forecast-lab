#!/usr/bin/env python3
"""Verify a published paper-export file against its manifest.

Phase 15's acceptance criteria include "the `--paper` export round-trips
through a validation script". There was no such script until 2026-09-08, and
until the same date no manifest field hashed the content either -- so a
published export was checkable only by counting its lines.

    python scripts/verify_paper_export.py docs/paper_exports/2026-09-13.jsonl.gz

Reads plain `.jsonl` and gzipped `.jsonl.gz` alike, finds `<path>.meta.json`
beside it, and checks:

  1. every line parses as JSON;
  2. every row has exactly the manifest's own `fields`, no more and no fewer;
  3. the row count matches `row_count`;
  4. sha256 over the newline-joined canonical rows matches `rows_sha256`.

Check 4 is skipped, loudly, for the exports published before the digest
existed (2026-07-10 .. 2026-08-30) -- those manifests carry no `rows_sha256`,
and a verifier that silently passed on a missing digest would be worse than
no verifier at all.

**What a pass means.** The file is the file that was written. It does NOT mean
the rows are immutable: `event_id`, `tier` and `category` are re-derived from
the live markets table on every dump, so two exports a week apart can differ
on rows whose forecasts never moved. Immutability of the ledger itself is what
docs/ledger_commitments.jsonl attests.

Deliberately dependency-free (stdlib only) and importing nothing from
`src/lab`: a reviewer who has this repo's data files but not its environment
must still be able to run it, and a verifier that shares code with the writer
verifies less than it appears to.

Exit code 0 = verified, 1 = mismatch, 2 = usage/IO error.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def verify(path: Path) -> int:
    meta_path = Path(f"{path}.meta.json")
    if not path.exists():
        print(f"FAIL: {path} does not exist", file=sys.stderr)
        return 2
    if not meta_path.exists():
        print(f"FAIL: manifest {meta_path} does not exist -- the export is a "
              f"two-file artifact and half of it is missing", file=sys.stderr)
        return 2

    manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    fields = manifest.get("fields")
    if not fields:
        print("FAIL: manifest carries no field list", file=sys.stderr)
        return 1

    digest = hashlib.sha256()
    n = 0
    bad_fields = 0
    try:
        with _open_text(path) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"FAIL: line {lineno} is not valid JSON: {exc}", file=sys.stderr)
                    return 1
                if list(row.keys()) != list(fields) and bad_fields < 3:
                    if sorted(row.keys()) != sorted(fields):
                        print(f"FAIL: line {lineno} field set differs from the manifest: "
                              f"extra={sorted(set(row) - set(fields))} "
                              f"missing={sorted(set(fields) - set(row))}", file=sys.stderr)
                        bad_fields += 1
                if n:
                    digest.update(b"\n")
                digest.update(
                    json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                n += 1
    except OSError as exc:
        print(f"FAIL: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    if bad_fields:
        return 1

    ok = True
    if n != manifest.get("row_count"):
        print(f"FAIL: row_count {manifest.get('row_count')} but file holds {n}", file=sys.stderr)
        ok = False

    expected = manifest.get("rows_sha256")
    if expected is None:
        print(f"SKIP: this manifest predates rows_sha256 (published before "
              f"2026-09-08); content cannot be verified, only counted.")
    elif expected != digest.hexdigest():
        print(f"FAIL: rows_sha256 {expected} but computed {digest.hexdigest()}", file=sys.stderr)
        ok = False

    if ok:
        print(f"OK: {path.name} -- {n} rows, {len(fields)} fields, "
              f"code_version={manifest.get('code_version')}, "
              f"schema_version={manifest.get('schema_version')}"
              + (f", rows_sha256 verified" if expected else ""))
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(f"usage: {Path(argv[0]).name} <export.jsonl|export.jsonl.gz>", file=sys.stderr)
        return 2
    return verify(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
