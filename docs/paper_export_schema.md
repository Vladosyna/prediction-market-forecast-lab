# Paper replication export schema

`lab export --paper --out <path>` writes two files:

- `<path>` — one JSON object per line (JSONL), one row per resolved forecast
  from every model. Reuses the same paired forecast+resolution+market query
  `lab eval` scores on (`eval/run.py::resolved_forecast_rows`), including its
  forward-only challenger filter (a versioned model never leaks rows from
  before its own `registered_ts`) and its `resolutions.disputed = 0`
  exclusion — this export is provably consistent with what was actually
  scored, not a separate reconstruction.
- `<path>.meta.json` — a manifest: the exact code version and schema version
  that produced the export, when it was generated, and how many rows it
  contains, so a reviewer can verify what they are re-analyzing. Note what it
  does **not** contain: no field hashes the payload's bytes, so the manifest is
  a provenance record, not a tamper seal. Re-compressing or re-serialising a
  file leaves no signature in it.

No PII exists anywhere in this schema (there are no user/account records at
all), so "anonymized" here means exactly one thing: internal/operational
fields (`cost_usd`, `evidence_run_id`, `inputs_hash`, and anything from
`evidence_runs`, which holds scraped article text) are excluded — nothing
else needs redacting.

## Row fields

| Field | Type | Meaning |
|---|---|---|
| `condition_id` | string | Market key (synthesized `{venue}:{native_id}` for non-Polymarket rows). |
| `venue` | string | `polymarket`, `kalshi`, `metaculus`, `manifold`. |
| `category` | string | Internal taxonomy category (`data/categories.yaml`). |
| `tier` | string | `liquid`, `tail`, or `ignored` at forecast time. |
| `model_id` | string | Forecaster identity, e.g. `m0_market`, `m1_hier@kalshi`, `m3_evidence@deepseek`. |
| `forecast_ts` | string (ISO 8601 UTC) | When this forecast was frozen in the ledger. |
| `p_yes` | float (0,1) | The model's forecast probability. |
| `p_market_at_ts` | float (0,1) | The market's own price at the same freeze moment. |
| `spread_at_ts` | float or null | Bid/ask spread at freeze time, if available. |
| `resolved_ts` | string (ISO 8601 UTC) | When the market resolved. |
| `payout_yes` | float (0.0 or 1.0) | The resolved outcome. |
| `event_id` | string or null | Cross-venue/negRisk event cluster id, for event-level clustering (null if this market was never linked to one). |
| `m3_randomized` | int (0 or 1) | Phase 15 boundary-randomization tag: 1 iff this M3 forecast was a coin-flip member of the K-10..K+10 liquidity band. Always 0 for non-M3 models. |
| `m3_random_seed` | string or null | The seed used, when `m3_randomized = 1`; null otherwise. |
| `depth_covariate` | float or null | Top-of-book depth in USD (bid + ask) from the snapshot that supplied `p_market_at_ts`. **Null on every row frozen before 2026-08-10** — Phase 15's covariates are populated going forward and never reconstructed, so a null here means "not measured", never "measured as zero". |
| `volume_24h` | float or null | The venue's own 24-hour volume for the market, captured at universe sync (Gamma `volume24hr`, Kalshi `volume_24h_fp`). Same forward-only rule and same null semantics as `depth_covariate`. |
| `trades_24h` | int or null | **Null throughout.** Neither venue reports a 24-hour trade count on the objects the collector already fetches, and a per-market Data API call was not added at the collector's sustained request rate. Present in the schema so the column's absence is explicit rather than silent; reported as not collected, not as missing data. |
| `hour_utc` | int (0–23) or null | Hour of day, UTC, at freeze time. Derivable from `forecast_ts`, stored because CLAUDE.md §5's schema names it. Null before 2026-08-10. |

## Manifest fields (`<path>.meta.json`)

| Field | Meaning |
|---|---|
| `code_version` | `process_guard.code_version()` — a deterministic sha1 (first 12 hex chars) over every `.py` file under `src/lab` plus `config.yaml`. Identical across two checkouts with identical bytes; changes whenever the code that produced the export changes. |
| `schema_version` | The database schema version (`meta.schema_version`) at export time. |
| `generated_at` | ISO 8601 UTC timestamp of the export run. |
| `row_count` | Number of rows in the JSONL file. |
| `fields` | The exact field list above, for a quick sanity check against this document. |

## Automated weekly snapshot

Since v2.8, a dated snapshot (+ matching `.meta.json`) is produced
automatically every week under `docs/paper_exports/` and committed to this
public repo, using the exact schema and manifest fields documented above. The
deployed schedule is `paper_export.cron` in `config.yaml`, currently
`"0 11 * * sun"` (Sundays, 11:00 UTC). See `src/lab/paper_export.py`.

**The container changed on 2026-09-08, and the split date matters when you
read these files:**

| Dates | File |
|---|---|
| `2026-07-10` … `2026-08-30` | `YYYY-MM-DD.jsonl` + `YYYY-MM-DD.jsonl.meta.json` |
| `2026-09-13` onward | `YYYY-MM-DD.jsonl.gz` + `YYYY-MM-DD.jsonl.gz.meta.json` |

A consumer globbing `*.jsonl` therefore gets a **silent partial read** of the
series — glob both, or `zcat`/`gzip.open` the newer half. Decompress with
`gzip.open(path, "rt", encoding="utf-8")` (Python) or `zcat` (shell); each
line is the same JSON object documented above, and the schema is unchanged.

The switch was forced, not stylistic: the snapshot is a full cumulative
re-dump, so it grew from 0.67 MB (2026-07-10) to 115 MB (2026-09-06) and
crossed GitHub's 100 MB per-file hard limit, at which point the pre-receive
hook refused the whole push. Earlier files stay plain and stay published —
this repo's history is a pre-registration record and is not rewritten. The
gzip stream is written with `mtime=0` and a fixed internal filename, so two
exports of identical rows are byte-identical.

The CLI's manual `lab export --paper --out <path>` flow is unaffected and
still writes plain JSONL — unless you name a path ending in `.gz`, in which
case it gzips with the same settings.
