"""`lab export`: latest forecast per (market, model) + metadata as JSONL.

This is the downstream integration point (brief section 13). Schema per line:
{condition_id, slug, question, category, end_date_iso, tier, model_id, ts,
 p_yes, p_market_at_ts, spread_at_ts}

`lab export --paper` (Phase 15) is a second, independent export: the full
resolved-forecast replication dataset for the eventual paper, plus a manifest
(code version hash, schema version, row count) so a reviewer can verify what
they're re-analyzing. See EXPORT_PAPER_FIELDS/export_paper_rows below.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator

from lab.store import db as dbmod
from lab.util import now_utc_iso

EXPORT_FIELDS = [
    "condition_id", "slug", "question", "category", "end_date_iso", "tier",
    "model_id", "ts", "p_yes", "p_market_at_ts", "spread_at_ts",
]

# Phase 15 replication export. Deliberately excludes cost_usd, evidence_run_id,
# and inputs_hash (internal/operational, not needed to re-analyze results) and
# never joins evidence_runs (holds scraped article text) -- this schema has no
# PII anywhere, so "anonymized" means exactly this: operational fields out,
# nothing else to redact.
EXPORT_PAPER_FIELDS = [
    "condition_id", "venue", "category", "tier", "model_id", "forecast_ts",
    "p_yes", "p_market_at_ts", "spread_at_ts", "resolved_ts", "payout_yes",
    "event_id", "m3_randomized", "m3_random_seed",
    # Phase 15 microstructure covariates. The phase exists to make heterogeneity
    # analysis possible without ex-post reconstruction, so the replication
    # dataset has to carry them -- omitting them left a reviewer unable to run
    # the very splits the covariates were collected for. NULL on every row
    # written before 2026-08-10 (they are forward-only, never backfilled) and
    # `trades_24h` NULL throughout: no venue reports a 24h trade count on the
    # objects the collector already fetches. See docs/paper_export_schema.md.
    "depth_covariate", "volume_24h", "trades_24h", "hour_utc",
]


def export_rows(conn) -> Iterator[dict]:
    rows = conn.execute(
        """
        SELECT m.condition_id, m.slug, m.question, m.category, m.end_date_iso, m.tier,
               f.model_id, f.ts, f.p_yes, f.p_market_at_ts, f.spread_at_ts
        FROM forecasts f
        JOIN markets m ON m.condition_id = f.condition_id
        JOIN (SELECT condition_id, model_id, MAX(ts) AS ts FROM forecasts
              GROUP BY condition_id, model_id) latest
          ON latest.condition_id = f.condition_id AND latest.model_id = f.model_id
             AND latest.ts = f.ts
        ORDER BY m.condition_id, f.model_id
        """
    )
    for r in rows:
        yield {k: r[k] for k in EXPORT_FIELDS}


def export_jsonl(conn) -> Iterator[str]:
    for row in export_rows(conn):
        yield json.dumps(row, ensure_ascii=False)


def export_paper_rows(conn) -> Iterator[dict]:
    """Every resolved forecast from every model, projected to
    EXPORT_PAPER_FIELDS -- the paper-grade replication dataset (Phase 15).

    Reuses `resolved_forecast_rows` (the same paired forecast+resolution+
    market query `lab eval` scores on) per model_id rather than reinventing
    the join, so this export is provably consistent with what was actually
    scored -- including its forward-only challenger filter (a version never
    leaks rows from before its own registered_ts) and its `disputed = 0`
    exclusion.

    Deterministically ordered from 2026-09-08, so `rows_sha256` in the manifest
    means something: models in name order, and rows within a model sorted by
    their own canonical serialisation, which is total (no tie can fall back on
    an arbitrary order).

    **The sort lives here and must never move into `resolved_forecast_rows`.**
    That query is shared with `run_eval`, whose
    `_per_cluster_diffs_in_resolution_order` orders clusters with
    `np.argsort(resolved_ts)` -- quicksort, i.e. *unstable* -- so ties in
    `resolved_ts` break on input order. Giving the shared query an ORDER BY
    would therefore change the sequence the anytime-valid confidence sequence
    consumes, moving a pre-registered statistic with no change in data.
    Per-model sorting also bounds memory: `resolved_forecast_rows` already
    materialises one model's rows, and sorting the whole export at once would
    hold every resolved forecast in Python dicts -- the shape that OOM-killed
    four different jobs in v2.11.
    """
    from lab.eval.run import resolved_forecast_rows

    model_ids = [r["model_id"] for r in conn.execute(
        "SELECT DISTINCT model_id FROM forecasts ORDER BY model_id"
    )]
    for model_id in model_ids:
        projected = []
        for row in resolved_forecast_rows(conn, model_id, None):
            row["model_id"] = model_id
            projected.append({k: row[k] for k in EXPORT_PAPER_FIELDS})
        projected.sort(key=canonical_row)
        yield from projected


def canonical_row(row: dict) -> str:
    """One row's canonical form: sorted-key compact JSON.

    Identical convention to `ledger_commitment._hash_rows`, deliberately -- the
    two verifiable artifacts this repo publishes should not disagree about what
    "the canonical bytes of a row" means. Separate from the JSONL serialisation
    on purpose: that one is human-readable (`ensure_ascii=False`, default
    spacing), and a digest must not depend on formatting choices.
    """
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def export_paper_jsonl(conn) -> Iterator[str]:
    for row in export_paper_rows(conn):
        yield json.dumps(row, ensure_ascii=False)


def write_paper_export_stream(conn, text_out) -> tuple[int, str]:
    """Write the paper export to an open text stream; return (rows, sha256).

    One pass, nothing materialised: the digest is folded in as rows go by, so
    a 232k-row export costs no more memory than a single row. The hash is over
    the newline-joined canonical rows, i.e. exactly what
    `hashlib.sha256("\\n".join(canonical_row(r) for r in rows))` would give --
    matching `ledger_commitment._hash_rows`, including its empty case
    (`sha256(b"")` for a dataset with no rows, no special-casing needed).

    Two real callers -- the scheduled weekly snapshot and `lab export --paper`
    -- which is why this is shared rather than written twice; both must produce
    the same digest for the same data or the verifier is worthless.
    """
    digest = hashlib.sha256()
    n = 0
    for row in export_paper_rows(conn):
        if n:
            digest.update(b"\n")
        digest.update(canonical_row(row).encode("utf-8"))
        text_out.write(json.dumps(row, ensure_ascii=False))
        text_out.write("\n")
        n += 1
    return n, digest.hexdigest()


def paper_export_manifest(conn, row_count: int,
                          rows_sha256: str | None = None) -> dict[str, Any]:
    """Code version hash + schema version + row count + content digest -- lets
    a reviewer verify what they're re-analyzing (brief section 15's "code
    version hash + schema documentation"). Reuses process_guard.code_version(),
    the same deterministic, content-based hash `lab ps` already reports -- not
    a new git-based hash.

    `rows_sha256` (2026-09-08) is sha256 over the newline-joined canonical rows
    -- `canonical_row`'s convention, shared with `ledger_commitment._hash_rows`.
    Until it existed a published export was checkable only by counting its
    lines, and Phase 15's own acceptance criterion ("the --paper export
    round-trips through a validation script") had no script; `scripts/
    verify_paper_export.py` is that script.

    What it does and does not prove, stated because the distinction matters to
    a reviewer: it proves the file is the file that was written, byte for byte
    modulo formatting. It does **not** prove the underlying rows are immutable.
    `event_id`, `tier` and `category` are re-derived from the live `markets`
    table on every dump, so two exports a week apart can legitimately differ on
    rows whose forecasts never moved. Immutability of the forecast ledger
    itself is what `docs/ledger_commitments.jsonl` attests, not this.

    None (the default) omits the field entirely rather than writing a null, so
    the manifests published before this date and the ones after it are each
    self-consistent.
    """
    from lab.process_guard import code_version

    manifest: dict[str, Any] = {
        "code_version": code_version(),
        "schema_version": dbmod.SCHEMA_VERSION,
        "generated_at": now_utc_iso(),
        "row_count": row_count,
        "fields": EXPORT_PAPER_FIELDS,
    }
    if rows_sha256 is not None:
        manifest["rows_sha256"] = rows_sha256
    return manifest
