"""SQLite schema, migrations, and writers (single file data/lab.db, WAL mode).

The forecasts table is append-only: the guarded connection installs SQLite
authorizer callbacks that hard-fail any UPDATE or DELETE against it
(guardrail: immutable forecast ledger).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lab.util import PROJECT_ROOT, now_utc_iso

# "14" adds kalshi_series_sync (2026-09-08). Additive, so nothing in the §13
# forecast contract breaks -- but this value is carried in every paper-export
# manifest, so it is bumped rather than left silently drifting behind the real
# shape of the database.
SCHEMA_VERSION = "14"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- v1.9 multi-venue foundation (Phase 10, brief section 5). `markets` itself
-- gains venue/venue_native_id/event_id via ALTER in _migrate_multi_venue()
-- below -- CREATE TABLE IF NOT EXISTS can't add columns to an existing table.
CREATE TABLE IF NOT EXISTS venues (
  venue TEXT PRIMARY KEY,
  trust_tier TEXT CHECK(trust_tier IN ('money','reputation','play')),
  forecastable INTEGER DEFAULT 0,
  in_m7_pool INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  title TEXT, created_ts TEXT
);

CREATE TABLE IF NOT EXISTS markets (
  condition_id TEXT PRIMARY KEY,
  slug TEXT, question TEXT, category TEXT,
  description TEXT,
  end_date_iso TEXT,
  token_id_yes TEXT, token_id_no TEXT,
  neg_risk INTEGER DEFAULT 0,
  active INTEGER, closed INTEGER,
  liquidity_num REAL, volume_num REAL, volume_24h_num REAL,
  tier TEXT CHECK(tier IN ('liquid','tail','ignored')),
  first_seen_ts TEXT, last_synced_ts TEXT
);

CREATE TABLE IF NOT EXISTS resolutions (
  condition_id TEXT PRIMARY KEY REFERENCES markets(condition_id),
  resolved_ts TEXT,
  payout_yes REAL CHECK(payout_yes IN (0.0, 1.0)),
  disputed INTEGER DEFAULT 0,
  source TEXT
);

-- append-only. NEVER UPDATE OR DELETE ROWS.
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  p_yes REAL NOT NULL CHECK(p_yes > 0 AND p_yes < 1),
  p_market_at_ts REAL NOT NULL,
  spread_at_ts REAL,
  inputs_hash TEXT,
  evidence_run_id INTEGER,
  cost_usd REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS evidence_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT,
  dossier_json TEXT,
  llm_model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, model_id TEXT, window_label TEXT,
  n INTEGER,
  brier REAL, brier_market REAL, skill REAL,
  skill_ci_lo REAL, skill_ci_hi REAL,
  log_loss REAL, log_loss_market REAL,
  calibration_json TEXT
);

CREATE TABLE IF NOT EXISTS shadow_trades (
  id INTEGER PRIMARY KEY,
  opened_ts TEXT, condition_id TEXT, token_side TEXT CHECK(token_side IN ('YES','NO')),
  entry_price REAL, p_model REAL, p_market REAL, edge REAL,
  stake_sim REAL, kelly_frac REAL,
  exit_ts TEXT, exit_price REAL, pnl_sim REAL,
  status TEXT CHECK(status IN ('open','resolved','abandoned'))
);

CREATE TABLE IF NOT EXISTS postmortems (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT, model_id TEXT,
  kind TEXT CHECK(kind IN ('miss','win')),
  brier_model REAL, brier_market REAL,
  analysis_json TEXT,
  llm_model TEXT, cost_usd REAL
);

-- daily LLM spend ledger (guardrail 10: budget enforced before each call)
CREATE TABLE IF NOT EXISTS llm_spend (
  date TEXT NOT NULL,             -- YYYY-MM-DD UTC
  purpose TEXT NOT NULL,          -- 'm3_extraction', 'postmortem', ...
  cost_usd REAL NOT NULL,
  ts TEXT NOT NULL
);

-- append-only. Rollback = repoint is_active, never rewrite a row (brief section 5/6).
-- Coexists with data/models/*.json artifact files (Phase 2): this table owns
-- VERSIONING/active/rollback state; artifact_path points at the file rather than
-- duplicating it. data/models/ACTIVE.json is a generated pointer written by
-- registry.py whenever is_active changes -- a cache of this table, never hand-edited.
CREATE TABLE IF NOT EXISTS model_versions (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,           -- artifact key ('m1_curves', ...) or ledger id ('m3_evidence@deepseek')
  version_tag TEXT NOT NULL,        -- e.g. 'v3'; human-readable, not semver-enforced
  artifact_path TEXT NOT NULL,      -- e.g. 'data/models/m1_curves_v3.json'; content immutable once written
  params_hash TEXT NOT NULL,        -- sha256 of the artifact file, for integrity verification
  fit_window_start TEXT, fit_window_end TEXT,   -- walk-forward train window; NULL for hand-set v1 defaults
  registered_ts TEXT NOT NULL,      -- challengers earn track record only from forecasts after this
  promoted_ts TEXT,                 -- NULL while still a challenger
  retired_ts TEXT,                  -- NULL while active
  retired_reason TEXT CHECK(retired_reason IN ('replaced','rollback') OR retired_reason IS NULL),
  is_active INTEGER DEFAULT 0       -- exactly one active row per model_id; enforced in registry.py + index
);

-- derived from forecasts + resolutions; always recomputable, not a backup-critical table.
-- Phase 14 (brief section 6/14): the Kelly-fraction wealth process already used by the
-- shadow portfolio (Phase 6), generalized to every model as a scoring/selection layer --
-- NOT a second trading simulation. Unlike shadow_trades (M4-only, entry-filtered, "would
-- we have traded"), this table scores EVERY resolved forecast from EVERY model
-- unconditionally, maximizing n for comparison purposes. log_wealth_delta is the Kelly
-- log-growth for a binary bet, same side rule as shadow_trades (YES if p_model > p_market,
-- else NO). forecast_id is additive beyond the brief's literal DDL -- an idempotency key
-- (every other column keeps its documented meaning) since a model can forecast the same
-- market many times before resolution and cum_log_wealth/n_forecasts are running sums.
CREATE TABLE IF NOT EXISTS wealth_ledger (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,
  category TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  event_id TEXT,                     -- for event-level attribution, mirrors eval's clustering
  forecast_id INTEGER NOT NULL,      -- FK to forecasts.id; idempotency key
  ts TEXT NOT NULL,                  -- resolution timestamp
  kelly_fraction REAL NOT NULL,      -- same 0.2x-capped fraction as shadow_trades
  log_wealth_delta REAL NOT NULL,    -- log(1 - f + f/price) if the bet won, log(1 - f) if lost
  cum_log_wealth REAL NOT NULL,      -- running sum for this (model_id, category)
  n_forecasts INTEGER NOT NULL       -- running count; cum_log_wealth / n_forecasts is the fair,
                                      -- coverage-normalized comparison metric (sleeping-expert rule)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_condition ON forecasts(condition_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_model_ts ON forecasts(model_id, ts);
CREATE INDEX IF NOT EXISTS idx_markets_tier ON markets(tier);
CREATE INDEX IF NOT EXISTS idx_llm_spend_date ON llm_spend(date);
CREATE INDEX IF NOT EXISTS idx_model_versions_model ON model_versions(model_id);
-- DB-level backstop for the single-active invariant (registry.py also enforces it).
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_versions_active
  ON model_versions(model_id) WHERE is_active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_wealth_ledger_forecast ON wealth_ledger(forecast_id);
CREATE INDEX IF NOT EXISTS idx_wealth_ledger_model_category ON wealth_ledger(model_id, category);

-- Phase 17 (v2.4) item 1: every venue-native tag/series that didn't match any
-- entry in data/categories.yaml's taxonomy and fell back to 'unknown' -- makes
-- taxonomy drift (a renamed Gamma tag, a new Kalshi series) visible instead of
-- silently diluting the 'unknown' bucket.
CREATE TABLE IF NOT EXISTS category_drift_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  venue TEXT NOT NULL,
  raw_tag TEXT NOT NULL,
  fallback_category TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_category_drift_venue ON category_drift_log(venue, raw_tag);

-- Phase 15 (v2.3/v2.7): every market considered and excluded from the universe,
-- with a reason code -- answers "why isn't X in the ledger" and defends against
-- selection-bias claims in review (brief section 5/15). No CHECK constraint on
-- reason_code: it is deliberately an open, non-exhaustive enum (see
-- collect/universe.py for which codes are actually populated today).
CREATE TABLE IF NOT EXISTS universe_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  venue TEXT NOT NULL, venue_native_id TEXT NOT NULL,
  reason_code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_log_ts ON universe_log(ts);
CREATE INDEX IF NOT EXISTS idx_universe_log_reason ON universe_log(reason_code);
CREATE INDEX IF NOT EXISTS idx_universe_log_venue_native ON universe_log(venue, venue_native_id);
"""


# venue -> (trust_tier, forecastable, in_m7_pool). Brief section 5/16: Polymarket
# and Kalshi are real-money and forecastable; Metaculus (reputation-scored) feeds
# M7's external pool but is never itself a forecast target; Manifold (play-money)
# feeds event mapping and M2 base rates only -- excluded from M7 and forecasting.
VENUE_SEEDS: tuple[tuple[str, str, int, int], ...] = (
    ("polymarket", "money", 1, 0),
    ("kalshi", "money", 1, 1),
    ("metaculus", "reputation", 0, 1),
    ("manifold", "play", 0, 0),
)


def venue_condition_id(venue: str, native_id: str) -> str:
    """Synthesized market key for non-Polymarket rows (brief section 5):
    condition_id stays the universal key so every existing FK, the forecasts
    ledger, and the snapshot layout keep working unchanged. Polymarket's own
    condition_id (its native hash) is used as-is, never prefixed."""
    return native_id if venue == "polymarket" else f"{venue}:{native_id}"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone() is not None


def migrate_multi_venue(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v1.9 migration (Phase 10): ALTER `markets` with venue columns
    (CREATE TABLE IF NOT EXISTS above can't add columns to a table that already
    exists) and seed the `venues` table. Safe to call on every connect() --
    each step checks before acting, so a second run is a no-op. Never rewrites
    or drops anything; a fresh DB gets the columns from the first connect()
    with no separate migration event.
    """
    applied = {"venue_column": False, "venue_native_id_column": False, "event_id_column": False}
    if not _column_exists(conn, "markets", "venue"):
        conn.execute("ALTER TABLE markets ADD COLUMN venue TEXT DEFAULT 'polymarket'")
        applied["venue_column"] = True
    if not _column_exists(conn, "markets", "venue_native_id"):
        conn.execute("ALTER TABLE markets ADD COLUMN venue_native_id TEXT")
        applied["venue_native_id_column"] = True
        # Backfill: for pre-existing (Polymarket) rows, the native id IS the
        # condition_id -- new venues populate this at insert time instead.
        conn.execute(
            "UPDATE markets SET venue_native_id = condition_id WHERE venue_native_id IS NULL"
        )
    if not _column_exists(conn, "markets", "event_id"):
        conn.execute("ALTER TABLE markets ADD COLUMN event_id TEXT")
        applied["event_id_column"] = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_venue ON markets(venue)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_id)")
    for venue, trust_tier, forecastable, in_m7_pool in VENUE_SEEDS:
        conn.execute(
            "INSERT OR IGNORE INTO venues(venue, trust_tier, forecastable, in_m7_pool) "
            "VALUES (?, ?, ?, ?)",
            (venue, trust_tier, forecastable, in_m7_pool),
        )
    conn.commit()
    return applied


def migrate_eval_measurement_upgrade(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.1 migration (Phase 11): ALTER `eval_runs` with the venue/
    category dimension, the anytime-valid confidence sequence columns, and the
    precision-weighted stratified estimator columns (CREATE TABLE IF NOT EXISTS
    in SCHEMA can't add columns to a table that already has rows). Safe to call
    on every connect() -- each column checks before adding, so a second run is
    a no-op. Old rows keep venue/category NULL (legacy pooled snapshots from
    before this migration), never rewritten.
    """
    applied = {
        "venue_column": False, "category_column": False,
        "skill_pw_column": False, "skill_pw_ci_lo_column": False,
        "skill_pw_ci_hi_column": False, "n_strata_pw_column": False,
        "cs_lo_column": False, "cs_hi_column": False,
        "cs_covers_zero_column": False, "n_event_clusters_column": False,
    }
    columns = {
        "venue_column": ("venue", "TEXT"),
        "category_column": ("category", "TEXT"),
        "skill_pw_column": ("skill_pw", "REAL"),
        "skill_pw_ci_lo_column": ("skill_pw_ci_lo", "REAL"),
        "skill_pw_ci_hi_column": ("skill_pw_ci_hi", "REAL"),
        "n_strata_pw_column": ("n_strata_pw", "INTEGER"),
        "cs_lo_column": ("cs_lo", "REAL"),
        "cs_hi_column": ("cs_hi", "REAL"),
        "cs_covers_zero_column": ("cs_covers_zero", "INTEGER"),
        "n_event_clusters_column": ("n_event_clusters", "INTEGER"),
    }
    for key, (column, sql_type) in columns.items():
        if not _column_exists(conn, "eval_runs", column):
            conn.execute(f"ALTER TABLE eval_runs ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_venue_category ON eval_runs(venue, category)"
    )
    conn.commit()
    return applied


def migrate_distributional_scoring(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.4 migration (Phase 16): ALTER `eval_runs` with nullable
    RPS columns. RPS is a secondary outcome on the SAME row `evaluate_model`
    already writes (venue/category/window) -- not a new table -- populated
    only for (model, venue, category, window) combinations with enough
    resolved bucketed events; NULL everywhere else, same convention as every
    other optional metric column already in this table.
    """
    applied = {"rps_column": False, "rps_market_column": False}
    for key, (column, sql_type) in {
        "rps_column": ("rps", "REAL"),
        "rps_market_column": ("rps_market", "REAL"),
    }.items():
        if not _column_exists(conn, "eval_runs", column):
            conn.execute(f"ALTER TABLE eval_runs ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.commit()
    return applied



def migrate_microstructure_covariates(conn: sqlite3.Connection) -> dict[str, bool]:
    """Phase 15's remaining `forecasts` covariates, added 2026-08-10.

    Specified in CLAUDE.md section 5 and in Phase 15's acceptance criteria
    ("covariate columns populate on live forecasts") since the phase was
    written, and never implemented -- `migrate_m3_boundary_randomization`'s own
    docstring records them as "a separate sub-task", which was then never picked
    up. Only `spread_at_ts`, which predates Phase 15, existed.

    Nullable and forward-only by design: the brief is explicit that these are
    "populated going forward, never backfilled by reconstruction", so every row
    written before this migration keeps NULL and the paper reports the date the
    covariates start. `trades_24h` is added for schema completeness but stays
    NULL for now -- neither venue returns a 24h trade count on the objects the
    collector already fetches, and the collector is running at ~9.6 req/s, so
    a per-market Data API call is not free to add.
    """
    applied = {}
    for column, sql_type in (("depth_covariate", "REAL"), ("volume_24h", "REAL"),
                             ("trades_24h", "INTEGER"), ("hour_utc", "INTEGER")):
        applied[column] = False
        if not _column_exists(conn, "forecasts", column):
            conn.execute(f"ALTER TABLE forecasts ADD COLUMN {column} {sql_type}")
            applied[column] = True
    if not _column_exists(conn, "markets", "volume_24h_num"):
        # Carries the venue's own 24h volume from the universe sync to forecast
        # time. Both venues already return it on objects we fetch anyway
        # (Gamma `volume24hr`, Kalshi `volume_24h_fp`), so this costs no requests.
        conn.execute("ALTER TABLE markets ADD COLUMN volume_24h_num REAL")
        applied["markets_volume_24h_num"] = True
    conn.commit()
    return applied


def migrate_m3_boundary_randomization(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.7 migration (Phase 15): ALTER `forecasts` with the M3
    boundary-randomization columns only -- the other Phase 15 `forecasts`
    covariates (depth_covariate, volume_24h, trades_24h, hour_utc) are a
    separate sub-task, not part of this migration. `m3_randomized` flags
    whether THIS forecast row was a coin-flip member of the K-10..K+10
    liquidity band (guardrail 12's pre-specified, seeded randomization
    carve-out); `m3_random_seed` is the exact seed used, so the whole
    roster is reproducible later from historical liquidity snapshots."""
    applied = {"m3_randomized_column": False, "m3_random_seed_column": False}
    for key, (column, sql_type) in {
        "m3_randomized_column": ("m3_randomized", "INTEGER DEFAULT 0"),
        "m3_random_seed_column": ("m3_random_seed", "TEXT"),
    }.items():
        if not _column_exists(conn, "forecasts", column):
            conn.execute(f"ALTER TABLE forecasts ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.commit()
    return applied


def migrate_shadow_fees(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent v2.7 migration (Phase 15): ALTER `shadow_trades` with the
    net-of-cost fee columns. `fee_paid_sim` is the simulated taker fee paid
    at entry (src/lab/shadow/fees.py); `effective_spread_sim` is
    entry_price - raw_price, i.e. the slippage haircut actually applied,
    now persisted explicitly rather than only computed inline."""
    applied = {"fee_paid_sim_column": False, "effective_spread_sim_column": False}
    for key, (column, sql_type) in {
        "fee_paid_sim_column": ("fee_paid_sim", "REAL"),
        "effective_spread_sim_column": ("effective_spread_sim", "REAL"),
    }.items():
        if not _column_exists(conn, "shadow_trades", column):
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {column} {sql_type}")
            applied[key] = True
    conn.commit()
    return applied



def migrate_close_resolved_markets(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent 2026-08-10 migration: a market with a recorded resolution is
    marked closed.

    Nothing had been clearing the flag. A venue drops a settled market from its
    listing, so the universe sync never sees it again and cannot update it, and
    `record_resolution` did not touch `markets` at all. The rows therefore stayed
    `active=1, closed=0` forever and kept being snapshotted -- 1,772 on Kalshi
    alone, every one of them with a resolution row already written.

    Only corrects rows that already carry a resolution, so it cannot invent a
    closure. `markets` is upserted rather than append-only (unlike `forecasts`),
    so updating it is ordinary maintenance, not a rewrite of the record.
    """
    before = conn.execute(
        """SELECT COUNT(*) FROM markets m JOIN resolutions r
           ON r.condition_id = m.condition_id
           WHERE m.closed = 0 OR m.active = 1"""
    ).fetchone()[0]
    conn.execute(
        """UPDATE markets SET closed = 1, active = 0
           WHERE condition_id IN (SELECT condition_id FROM resolutions)
             AND (closed = 0 OR active = 1)"""
    )
    conn.commit()
    return {"closed": before}


def migrate_resolution_checked_ts(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent 2026-07-25 migration: give the resolution watcher a cursor.

    Its candidate query took `LIMIT n` with no ORDER BY, so SQLite handed back
    the same n rows in scan order every 30-minute cycle. The head of that scan
    had filled with markets Gamma reports as never `closed` whose end dates are
    long past -- permanently unresolvable, permanently first -- so the watcher
    re-fetched the same few hundred hopeless markets forever and never reached
    the rest. The working set had grown to ~42k markets draining at a few dozen
    a day while ~600 closed daily.

    `resolution_checked_ts` turns that scan into a round-robin: order by it
    ascending and each cycle takes the least-recently-checked markets. SQLite
    sorts NULLs first on ASC, which gives exactly the priority we want for
    free -- a market that has never been checked (a fresh closure) jumps ahead
    of the old sludge, while the backlog still sweeps steadily in the
    background instead of blocking it.
    """
    applied = {"resolution_checked_ts_column": False, "index": False}
    if not _column_exists(conn, "markets", "resolution_checked_ts"):
        conn.execute("ALTER TABLE markets ADD COLUMN resolution_checked_ts TEXT")
        applied["resolution_checked_ts_column"] = True
    if not _index_exists(conn, "idx_markets_resolution_checked"):
        conn.execute(
            "CREATE INDEX idx_markets_resolution_checked "
            "ON markets(resolution_checked_ts)"
        )
        applied["index"] = True
    conn.commit()
    return applied


def migrate_universe_log_dedup(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent 2026-07-20 migration: collapse `universe_log` to one row per
    (venue, venue_native_id, reason_code, day) and enforce it with a UNIQUE
    index, so `log_universe_exclusion`'s insert can become INSERT OR IGNORE.

    The table's only consumer (`eval/report.py`'s `universe_exclusion_counts`)
    has only ever read `GROUP BY date(ts), reason_code, COUNT(*)` -- a daily
    count, never an individual row -- but the write path logged one row per
    currently-excluded market on EVERY hourly sync, unconditionally. On the
    live VPS this produced ~21.7x same-day duplication (measured 2026-07-20)
    and grew universe_log to ~90% of the database (2.5GB), independently
    costing the nightly report render ~38s just to GROUP BY it. Collapsing to
    one row per excluded-market-reason-day preserves everything the report
    (or any future consumer doing "was X excluded, when, and why") can read
    -- day-level was always the table's actual working resolution, just
    recorded with ~20x redundant noise on top. Confirmed against the PAP and
    the paper draft's own referee-objection table (`universe_log` "records
    every exclusion with a reason code" -- an existence claim about markets,
    not a claim about raw sync-occurrence counts): this does not touch the
    forecast/resolution ledger, the exclusion RULE in collect/universe.py
    (unchanged), or any primary-hypothesis scoring path.

    This is the one migration in this file that removes existing rows rather
    than only adding columns. On a bloated production table the one-time
    cleanup rebuilds the table (bulk-copy the ~5% of rows that survive into a
    fresh, index-free table, then build indexes once at the end) rather than
    deleting the ~95% that don't in place -- a first attempt using plain
    `DELETE ... WHERE id NOT IN (...)` didn't finish in 15 minutes against
    the VPS's 8M-row table, because every one of those millions of deletes
    had to maintain the table's 3 existing indexes individually; the rebuild
    touches the surviving rows once and defers all index-building to the end,
    the same technique VACUUM itself uses. Original `id` values are preserved
    (explicit column list, not auto-assigned) -- nothing else in this codebase
    references a universe_log id, but there's no reason to renumber history
    for a mechanical rebuild. Run it with the service stopped and VACUUM
    separately afterward -- neither this rebuild nor a plain DELETE shrinks
    the file on disk by itself (see docs/VPS_OPERATIONS.md). A fresh or
    already-deduped database just finds the index already present and
    returns instantly.
    """
    applied = {"deduped_existing_rows": False, "unique_index": False}
    if not _index_exists(conn, "idx_universe_log_dedup"):
        before = conn.execute("SELECT COUNT(*) FROM universe_log").fetchone()[0]
        # Explicit BEGIN: unlike INSERT/UPDATE/DELETE, Python's sqlite3 module
        # does NOT auto-open a transaction before DDL (CREATE/DROP/ALTER), so
        # without this each DDL statement below would commit independently as
        # its own atomic unit -- discovered the hard way when an interrupted
        # run left a committed-but-half-populated universe_log_dedup_tmp
        # table behind even though the migration "looked" like one block.
        # DROP TABLE IF EXISTS defends against exactly that leftover.
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS universe_log_dedup_tmp")
        conn.execute(
            "CREATE TABLE universe_log_dedup_tmp ("
            "  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
            "  venue TEXT NOT NULL, venue_native_id TEXT NOT NULL, reason_code TEXT NOT NULL)"
        )
        conn.execute(
            """
            INSERT INTO universe_log_dedup_tmp (id, ts, venue, venue_native_id, reason_code)
            SELECT id, ts, venue, venue_native_id, reason_code FROM universe_log
            WHERE id IN (
                SELECT MIN(id) FROM universe_log
                GROUP BY venue, venue_native_id, reason_code, date(ts)
            )
            """
        )
        conn.execute("DROP TABLE universe_log")
        conn.execute("ALTER TABLE universe_log_dedup_tmp RENAME TO universe_log")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_universe_log_ts ON universe_log(ts)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_universe_log_reason ON universe_log(reason_code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_universe_log_venue_native "
            "ON universe_log(venue, venue_native_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_universe_log_dedup "
            "ON universe_log(venue, venue_native_id, reason_code, date(ts))"
        )
        after = conn.execute("SELECT COUNT(*) FROM universe_log").fetchone()[0]
        applied["deduped_existing_rows"] = after < before
        applied["unique_index"] = True
    conn.commit()
    return applied


class ForecastLedgerViolation(RuntimeError):
    """Raised on any attempt to UPDATE or DELETE a forecast row."""


def _authorizer(action: int, arg1: str | None, arg2, db_name, trigger) -> int:
    if arg1 == "forecasts" and action in (sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """True when this database is already at SCHEMA_VERSION.

    A pure read, deliberately: it decides whether connect() may skip the
    schema+migration block entirely. See connect() for why that matters.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return False  # no meta table -> brand-new database, needs the full path
    return row is not None and row[0] == SCHEMA_VERSION


def kalshi_event_ticker(venue_native_id: str | None) -> str | None:
    """The Kalshi event a market belongs to, from its ticker.

    Kalshi tickers are SERIES-EVENT-OUTCOME, and the first two segments are
    exactly the venue's own `event_ticker`. Verified against the live API on 40
    randomly sampled tickers drawn from markets this lab has actually forecast:
    40 matches, 0 mismatches. That check is what licenses using it to backfill
    ALREADY-RESOLVED markets, which the universe sync will never re-visit and
    whose grouping therefore cannot be corrected later.
    """
    if not venue_native_id or "-" not in venue_native_id:
        return None
    parts = venue_native_id.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else None


def migrate_kalshi_event_clusters(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent: give every Kalshi market the event_id its own venue assigns it.

    Kalshi markets are overwhelmingly mutually-exclusive legs of ONE numeric
    question -- 24 threshold buckets of one PPI release, 18 candidates in one
    election -- and none of them carried an event_id: 2 of 28,634 resolved
    scored rows, against 72.8% on Polymarket, where `_link_negrisk_legs` has
    grouped legs at sync time all along. Kalshi had no equivalent, so every leg
    counted as its own cluster.

    That inflates the unit every confidence interval in this study is computed
    over. Measured on the confirmatory window: 1,649 scored Kalshi markets are
    243 events (x6.8), and H1's >=30-day bucket is 22 events, not 193 (x8.8).
    The pre-analysis plan forbids exactly this in its own words -- "clustering
    by venue-market would overstate n. Naive CIs would lie."

    Deliberately deterministic (`kalshi:<event_ticker>`) rather than the uuid
    `link_event` mints for Polymarket: Gamma hands us a list of legs with no
    natural identifier, so that path has to invent one and propagate it
    pairwise. Kalshi hands us the identifier, so inventing a second one would
    only make the mapping unreproducible.
    """
    cur = conn.execute(
        "SELECT condition_id, venue_native_id, question FROM markets "
        "WHERE venue = 'kalshi' AND event_id IS NULL AND venue_native_id LIKE '%-%'"
    )
    updates, events = [], {}
    for cid, native, question in cur:
        ticker = kalshi_event_ticker(native)
        if not ticker:
            continue
        event_id = f"kalshi:{ticker}"
        events.setdefault(event_id, question)
        updates.append((event_id, cid))
    if not updates:
        return {"markets_linked": 0, "events": 0}
    conn.executemany(
        "INSERT OR IGNORE INTO events(event_id, title, created_ts) VALUES (?, ?, ?)",
        [(e, t, now_utc_iso()) for e, t in events.items()],
    )
    conn.executemany("UPDATE markets SET event_id = ? WHERE condition_id = ?", updates)
    conn.commit()
    return {"markets_linked": len(updates), "events": len(events)}


def migrate_kalshi_series_cursor(conn: sqlite3.Connection) -> dict[str, bool]:
    """Idempotent 2026-09-08 migration: give the Kalshi universe sync its own
    rotation cursor, because it had been inferring one from data it does not
    always write.

    `_series_sync_order` ordered known series by MAX(last_synced_ts) over that
    series' market rows -- but `sync_kalshi_universe` fetches
    `markets_for_series(status="open")` and only upserts what comes back, so a
    series that returns zero open markets advances nothing. Its key stayed
    frozen at whatever it was, it stayed the oldest, and Python's stable sort
    handed back the same head every cycle: with 231 dead series against ~32
    known slots, the known half of the rotation was not slow, it was fixed.
    Measured 2026-09-07: median staleness over the 606 series that DO carry
    open markets was 383 hours against a designed 26, with cycles logging
    `markets_seen: 0` three hours running.

    A separate table rather than a column on `markets`: writing
    `last_synced_ts` onto rows the venue did not return would make a column of
    the §13 forecast contract assert something false. This one records what we
    ATTEMPTED, which is exactly what a rotation cursor is.

    `consecutive_empty` is recorded, not acted on -- a series that returns
    nothing for weeks is worth seeing in `lab status`, but skipping it would
    reintroduce the same class of bug from the other side.
    """
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kalshi_series_sync'"
    ).fetchone() is not None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kalshi_series_sync (
          series TEXT PRIMARY KEY,
          attempted_ts TEXT NOT NULL,
          markets_seen INTEGER NOT NULL DEFAULT 0,
          consecutive_empty INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return {"kalshi_series_sync": not existed}


def record_kalshi_series_attempt(conn: sqlite3.Connection, series: str,
                                 markets_seen: int, ts: str | None = None) -> None:
    """Stamp one series as attempted. Called for every series the sync walks --
    success, empty response, or fetch failure alike.

    Unconditional by design, and that is the whole fix: stamping only on a
    productive walk is what let dead series pin the head of the queue forever.
    `collect/resolutions.py` learned the identical lesson on 2026-07-25 and its
    `watch_resolutions` stamps before the fetch for the same reason.
    """
    conn.execute(
        """
        INSERT INTO kalshi_series_sync (series, attempted_ts, markets_seen, consecutive_empty)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(series) DO UPDATE SET
          attempted_ts = excluded.attempted_ts,
          markets_seen = excluded.markets_seen,
          consecutive_empty = CASE WHEN excluded.markets_seen > 0
                                   THEN 0 ELSE kalshi_series_sync.consecutive_empty + 1 END
        """,
        (series, ts or now_utc_iso(), markets_seen, 0 if markets_seen > 0 else 1),
    )


def _apply_schema_and_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate_multi_venue(conn)
    migrate_eval_measurement_upgrade(conn)
    migrate_distributional_scoring(conn)
    migrate_m3_boundary_randomization(conn)
    migrate_shadow_fees(conn)
    migrate_universe_log_dedup(conn)
    migrate_resolution_checked_ts(conn)
    migrate_close_resolved_markets(conn)
    migrate_microstructure_covariates(conn)
    migrate_kalshi_event_clusters(conn)
    migrate_kalshi_series_cursor(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_utc_iso(),)
    )
    # Forward migration: new tables above are created idempotently; bump the
    # recorded schema_version on pre-existing databases (INSERT OR IGNORE above
    # never updates it). No destructive change -- data is untouched.
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is not None and row[0] != SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (SCHEMA_VERSION,)
        )
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the lab database with schema and guards applied.

    The schema+migration block runs only when the database is not already at
    SCHEMA_VERSION. It used to run on EVERY connection, and that was the cause
    of this project's recurring "database is locked" failures (2026-08-01
    through 08-05, most visibly as an hourly crash loop): `executescript`, the
    seven migrations and two INSERT OR IGNOREs all take the write lock, so
    merely opening a connection to READ one value -- which
    `schedule_state.last_run_age_seconds` does on every catch-up tick --
    contended with whatever was writing at the time. Under light load it was
    invisible; under a long-running job it made every concurrent open fail.

    Skipping it is safe precisely because the migrations are already
    idempotent and version-gated: the version check reads what they would
    otherwise re-assert. A new or out-of-date database still takes the full
    path.
    """
    path = Path(db_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Let the collector and orchestrator analytics connections wait on each
    # other instead of failing with "database is locked" under WAL.
    #
    # 10s -> 30s on 2026-08-23, after seven such failures in one night, all
    # inside the 02:04-03:04 window: job_resolutions three times, plus
    # job_manifold_sync, job_sync and job_kalshi_sync. Nothing was lost -- the
    # next firing half an hour later succeeded and resolutions kept landing at
    # 106-146/hour -- but a whole collector cycle is skipped each time.
    #
    # **This narrows the window; it does not close it, and the reason is worth
    # knowing before anyone reads a future recurrence as this fix failing.**
    # The nightly writers hold ONE transaction for their whole run:
    # `append_forecast` does not commit, `run_forecasts` commits once after
    # every loop (forecast.py), and `run_eval`/the wealth ledger do the same.
    # So the forecast pass held a write transaction from 02:00 to 02:20 and
    # eval from 02:21 to 02:50 -- against which no busy_timeout worth setting
    # can help. Three of the seven failures landed 4-6 minutes into that
    # 20-minute transaction. What 30s buys is the SHORT collisions, which is
    # most of them outside the bundle. Committing those jobs in batches is the
    # actual fix and is a deliberate change to the ledger's write path, not
    # something to fold into a timeout bump.
    conn.execute("PRAGMA busy_timeout=30000")
    # WAL hygiene. SQLite never shrinks the -wal file on its own: it reuses the
    # space in place, so one burst of heavy writing leaves a file that size
    # forever. Measured 2026-08-25: 1,195MB of WAL beside a 1,302MB database.
    # That is not a load problem -- iowait was zero -- but crash recovery has to
    # replay it and every consistent-copy backup has to account for it. The
    # limit caps the file after each checkpoint; 64MB is comfortably above a
    # nightly bundle's working set, so it does not force extra checkpoints
    # during normal writing.
    conn.execute("PRAGMA journal_size_limit=67108864")
    if not _schema_is_current(conn):
        _apply_schema_and_migrations(conn)
    conn.set_authorizer(_authorizer)
    return conn


def upsert_market(conn: sqlite3.Connection, row: dict) -> None:
    """Idempotent market upsert; preserves first_seen_ts across re-syncs.

    Venue-aware (v1.9, Phase 10) but fully backward-compatible: callers that
    don't pass venue/venue_native_id (every pre-Phase-10 call site) default to
    'polymarket' with venue_native_id = condition_id. `event_id` is deliberately
    excluded from the ON CONFLICT UPDATE -- a cross-venue link minted by
    `lab map confirm` must survive the next routine universe re-sync.
    """
    row = {"venue": "polymarket", "venue_native_id": row.get("condition_id"), "event_id": None,
           "volume_24h_num": None, **row}
    conn.execute(
        """
        INSERT INTO markets (condition_id, slug, question, category, description,
                             end_date_iso, token_id_yes, token_id_no, neg_risk,
                             active, closed, liquidity_num, volume_num, volume_24h_num, tier,
                             venue, venue_native_id, event_id,
                             first_seen_ts, last_synced_ts)
        VALUES (:condition_id, :slug, :question, :category, :description,
                :end_date_iso, :token_id_yes, :token_id_no, :neg_risk,
                :active, :closed, :liquidity_num, :volume_num, :volume_24h_num, :tier,
                :venue, :venue_native_id, :event_id,
                :now, :now)
        ON CONFLICT(condition_id) DO UPDATE SET
            slug=excluded.slug, question=excluded.question, category=excluded.category,
            description=excluded.description, end_date_iso=excluded.end_date_iso,
            token_id_yes=excluded.token_id_yes, token_id_no=excluded.token_id_no,
            neg_risk=excluded.neg_risk, active=excluded.active, closed=excluded.closed,
            liquidity_num=excluded.liquidity_num, volume_num=excluded.volume_num,
            volume_24h_num=excluded.volume_24h_num,
            tier=excluded.tier, last_synced_ts=excluded.last_synced_ts
        """,
        {**row, "now": now_utc_iso()},
    )


def link_event(conn: sqlite3.Connection, condition_id_a: str, condition_id_b: str,
              title: str | None = None) -> str:
    """Mint (or reuse) an event linking two venue-markets on human confirmation
    (brief section 5/Phase 10: "event_id minted on first human-confirmed
    cross-venue match"). Idempotent: re-linking the same pair is a no-op.
    Upserts a minimal placeholder row for either side not yet synced by its
    venue's own collector, so the link always succeeds.
    """
    import uuid

    for cid in (condition_id_a, condition_id_b):
        conn.execute(
            "INSERT OR IGNORE INTO markets (condition_id, venue, venue_native_id, tier, "
            "active, closed, first_seen_ts, last_synced_ts) VALUES (?, ?, ?, 'ignored', 0, 0, ?, ?)",
            (cid, cid.split(":", 1)[0] if ":" in cid else "polymarket",
             cid.split(":", 1)[1] if ":" in cid else cid, now_utc_iso(), now_utc_iso()),
        )
    rows = conn.execute(
        "SELECT condition_id, event_id FROM markets WHERE condition_id IN (?, ?)",
        (condition_id_a, condition_id_b),
    ).fetchall()
    existing = next((r["event_id"] for r in rows if r["event_id"]), None)
    event_id = existing or f"evt_{uuid.uuid4().hex[:16]}"
    if existing is None:
        conn.execute(
            "INSERT OR IGNORE INTO events(event_id, title, created_ts) VALUES (?, ?, ?)",
            (event_id, title, now_utc_iso()),
        )
    conn.execute(
        "UPDATE markets SET event_id = ? WHERE condition_id IN (?, ?) AND (event_id IS NULL OR event_id = ?)",
        (event_id, condition_id_a, condition_id_b, event_id),
    )
    conn.commit()
    return event_id


def record_resolution(
    conn: sqlite3.Connection,
    condition_id: str,
    resolved_ts: str,
    payout_yes: float,
    disputed: bool,
    source: str,
) -> None:
    """At-least-once, idempotent: replays of the same final payout are no-ops."""
    conn.execute(
        """
        INSERT INTO resolutions (condition_id, resolved_ts, payout_yes, disputed, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(condition_id) DO UPDATE SET
            resolved_ts=excluded.resolved_ts, payout_yes=excluded.payout_yes,
            disputed=excluded.disputed, source=excluded.source
        """,
        (condition_id, resolved_ts, payout_yes, int(disputed), source),
    )
    # A resolved market is closed by definition, and nothing else reliably says
    # so: a venue's market listing simply stops returning it, so the universe
    # sync -- which can only update what it receives -- never clears the flag.
    # 1,772 Kalshi markets sat `active=1, closed=0` with resolutions already
    # recorded (2026-08-10), which kept them in the snapshot loop forever.
    # Safe against both resolution watchers: each selects markets with NO
    # resolution row, so by the time this runs the market has already left
    # their candidate set.
    conn.execute(
        "UPDATE markets SET closed = 1, active = 0 WHERE condition_id = ?",
        (condition_id,),
    )


def append_forecast(conn: sqlite3.Connection, row: dict) -> int:
    """The ONLY write path into the forecasts ledger. Insert-only by design."""
    cur = conn.execute(
        """
        INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts,
                               spread_at_ts, inputs_hash, evidence_run_id, cost_usd,
                               m3_randomized, m3_random_seed,
                               depth_covariate, volume_24h, trades_24h, hour_utc)
        VALUES (:ts, :condition_id, :model_id, :p_yes, :p_market_at_ts,
                :spread_at_ts, :inputs_hash, :evidence_run_id, :cost_usd,
                :m3_randomized, :m3_random_seed,
                :depth_covariate, :volume_24h, :trades_24h, :hour_utc)
        """,
        {
            "spread_at_ts": None,
            "inputs_hash": None,
            "evidence_run_id": None,
            "cost_usd": 0.0,
            "m3_randomized": 0,
            "m3_random_seed": None,
            # Phase 15 covariates: nullable, so a caller that does not supply
            # them (a test fixture, an older path) still writes a valid row.
            "depth_covariate": None,
            "volume_24h": None,
            "trades_24h": None,
            "hour_utc": None,
            **row,
        },
    )
    return cur.lastrowid


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a key/value pair into the meta table (allowed by the authorizer)."""
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def llm_spend_today(conn: sqlite3.Connection, date_utc: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_spend WHERE date = ?", (date_utc,)
    ).fetchone()
    return float(row["total"])


def record_llm_spend(
    conn: sqlite3.Connection, date_utc: str, purpose: str, cost_usd: float
) -> None:
    conn.execute(
        "INSERT INTO llm_spend (date, purpose, cost_usd, ts) VALUES (?, ?, ?, ?)",
        (date_utc, purpose, cost_usd, now_utc_iso()),
    )
