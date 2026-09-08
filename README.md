# Prediction Market Forecast Lab

A read-only research system that answers one question with statistical rigor:

> Can we produce probability estimates for event outcomes that are better
> calibrated than the prediction market's own price, measured after resolution?

The lab continuously collects public market data from **Polymarket and
Kalshi** (plus Metaculus community predictions and Manifold, as signal and
base-rate inputs only), generates timestamped probability forecasts from
eleven models, freezes them in an append-only ledger, waits for markets to
resolve, and scores every model against **each venue's own price** — Brier
score, log loss, calibration curves. A simulated "shadow portfolio"
translates any measured edge into hypothetical P&L.

**No real money is ever involved and this repository contains no execution
code** — order placement, wallets, keys, and CLOB authentication are out of
scope by design (see "Downstream use & license" below).

The measurement is pre-registered. [`docs/pre_analysis_plan.md`](docs/pre_analysis_plan.md)
fixes the hypotheses, the primary outcome and the exclusion rules, and is
append-only: every subsequent finding, defect and population change is filed
as a dated addendum rather than edited in. Each day's appended ledger rows are
hashed and the hash committed to this repository the following night
([`docs/ledger_commitments.jsonl`](docs/ledger_commitments.jsonl)), so a
reader can verify the forecast record was not rewritten after the fact.

## Why this exists

Prediction-market prices are often treated as ground-truth probabilities.
They're a good prior, but they are not automatically well-calibrated —
academic work on Polymarket/PredictIt-style markets documents systematic
long-horizon underconfidence, thin-book noise, and category-specific biases.
The only honest way to know whether *any* model — statistical, LLM-based, or
market-derived — beats the market is to freeze a probability before the
outcome is known and score it after resolution, on a large enough sample to
say so with a confidence interval instead of a hunch. That's the entire
project: no trading, no execution, just a measurement instrument run
continuously against public data.

## How it works

```
 Polymarket ──┐   (Gamma / CLOB / Data APIs)
 Kalshi     ──┼──▶ lab sync / collect ──▶ SQLite (markets, resolutions)
 Metaculus  ──┤                       └─▶ Parquet (order-book snapshots)
 Manifold   ──┘
                                              │
                                              ▼
                          lab forecast  (M0…M7 → M4 ensemble)
                          ├─ M0 market mid (null baseline)
                          ├─ M1 horizon-recalibration (logistic, per-bucket)
                          ├─ M2 category base rates
                          ├─ M3 LLM evidence pipeline (news → strict-JSON → deterministic aggregator)
                          ├─ M5 structural nowcasts (weather / macro)
                          ├─ M6 negRisk coherence scanner
                          ├─ M7 cross-venue signal (Kalshi / Metaculus, confirmed matches only)
                          └─ M4 ensemble (log-odds weighted pool, fit per category)
                                              │
                                              ▼
                     forecasts ledger (SQLite, append-only, never UPDATE/DELETE)
                                              │
                     ┌────────────────────────┼─────────────────────────┐
                     ▼                        ▼                         ▼
              lab eval (paired          lab shadow (simulated       lab learn (monthly:
              Brier / log-loss vs       portfolio, P&L, no          refits, champion/
              market, cluster           real money — labeled        challenger promotion,
              bootstrap CIs)            SIMULATION everywhere)       post-mortems)
                     │                        │                         │
                     └────────────────────────┴─────────────────────────┘
                                              ▼
                                lab report → static HTML (reports/)
                                lab export → JSONL (the integration point)
```

Every forecast row is frozen at write time against the market price captured
in the *same* snapshot (`p_market_at_ts`) — that pairing is what makes the
skill measurement honest; nothing is ever retroactively edited. See
[`CLAUDE.md`](CLAUDE.md) for the full engineering brief (schema, guardrails,
phase-by-phase acceptance criteria) this repo was built against.

## The model roster

| Model | Type | Edge thesis |
|---|---|---|
| `m0_market` | null baseline | market mid — the number every other model must beat |
| `m1_debiased` | statistical | logistic recalibration of the market price, fit per time-to-resolution bucket; the direction of the bias is *fit*, never hardcoded |
| `m1_hier@{venue}` | statistical (hierarchical) | one global recalibration shape plus a ridge-shrunk per-venue offset (`α_g+α_v`, `β_g+β_v`), fit jointly across Polymarket/Kalshi/Metaculus — a small-n venue borrows the global curve, a large-n venue diverges where its own data demands it. Forecasts in parallel with `m1_debiased` as an observable challenger (not yet pooled into M4 — same precedent as `m3b_direct` below); its Metaculus offset also recalibrates the raw community prediction before M7 pools it |
| `m2_baserate` | statistical | historical base rate by recurring question template, blended in log-odds space |
| `m3_evidence` | LLM (structured) | news retrieval → strict-JSON evidence extraction → **deterministic** log-odds aggregator (the LLM never writes the final number) |
| `m3b_direct` | LLM (direct) | the same model states a probability itself, on the same markets and from the same dossier `m3_evidence` just built — the two arms differ only in whether the final number comes from the deterministic aggregator or from the LLM. Running since 2026-08-11; a comparison arm, deliberately never pooled into M4 |
| `m5_nowcast` | structural | maps an external quantitative model (open-meteo/NWS ensembles, Cleveland Fed / GDPNow) straight onto the market's resolution criteria |
| `m6_consistency` | deterministic | negRisk / linked-market coherence scanner — flags legs that don't sum to ~1 |
| `m7_crossvenue` | cross-venue | log-odds pool of external venues' prices (Kalshi public market data; Metaculus community prediction where a token grants access, recalibrated through `m1_hier`'s Metaculus offset first) on a curated, human-confirmed `markets_map.yaml` — Polymarket's own price stays out. The pooled logit is extremized by a fitted, correlation-discounted exponent (Phase 13) |
| `m4_ensemble` | ensemble | log-odds weighted pool of the above, weights fit per category on resolved history; the pooled logit is extremized by a per-category exponent, discounted by how correlated the pooled sources actually are (Phase 13) so a handful of near-duplicate signals can't buy false confidence |

A `sports` null-control sample runs the cheap models only: if the lab "finds
skill" on a near-efficient market like sports, the harness is broken, not the
market — the weekly report prints null-control skill next to everything else.

## Statistical principles

A short field guide to the estimators this repo actually computes — see
[`eval/`](src/lab/eval/) and [`learn/`](src/lab/learn/) for the code.

- **Paired scoring against the market.** Every skill number is
  `mean(brier_market − brier_model)` on the *same* forecast rows, never an
  unpaired comparison — this removes the dominant variance component
  (question difficulty) for free.
  [`eval/scoring.py`](src/lab/eval/scoring.py).
- **Event-level cluster bootstrap.** The same real-world event can be priced
  on several venues; resampling by venue-market row would treat correlated
  observations as independent. The bootstrap resamples whole `event_id`
  clusters instead (falling back to `condition_id` where no cross-venue link
  exists). [`eval/cluster.py`](src/lab/eval/cluster.py).
- **Anytime-valid confidence sequence.** Nightly reports are read
  continuously, which invalidates a fixed-n p-value under repeated looks. The
  report computes a time-uniform normal-mixture confidence sequence (Howard,
  Ramdas, McAuliffe & Sekhon, *Annals of Statistics* 49(2), 2021, Prop. 3/5) —
  this interval, not the classical bootstrap CI, is what actually gates model
  promotion and rollback. [`eval/anytime.py`](src/lab/eval/anytime.py).
- **Precision-weighted stratified estimator.** Brier-difference variance is
  driven directly by price level (`p(1−p)`), so pooling naively can mask real
  heterogeneity. Resolved forecasts are stratified into price buckets, each
  stratum's mean paired difference is inverse-variance weighted, and the
  pooled estimate provably collapses to the raw mean under homogeneous
  variance — it only diverges when the heterogeneity is real.
  [`eval/stratified.py`](src/lab/eval/stratified.py).
- **Hierarchical partial pooling (`m1_hier`, Phase 12).** One recalibration
  shape is shared across venues; each venue earns its own offset only in
  proportion to how much data it has (a ridge penalty scaled ∝ 1/n_venue) —
  a new venue borrows the global curve instead of overfitting its first
  month of noise. [`learn/refit.py`](src/lab/learn/refit.py)
  (`fit_m1_hier_curves`).
- **Correlation-discounted extremization (Phase 13).** A log-odds pool of
  correlated sources is provably overconfident if extremized as though they
  were independent. The fitted exponent is scaled by the correlation-adjusted
  effective source count (`n_eff = n / (1 + (n−1)·ρ̄)`) before it's ever
  applied — a duplicated or near-duplicate source buys no extra confidence.
  [`learn/pooling.py`](src/lab/learn/pooling.py).
- **Virtual prediction economy (Phase 14).** Kelly log-optimal betting and
  log-score are formally dual (Kelly 1956; Cover 1991): staking a fixed,
  capped Kelly fraction of a model's edge against the market price on every
  resolved forecast compounds wealth at the model's own log-score advantage —
  a second, betting-theoretic readout of the same skill number the lab
  already computes rigorously, not a new signal. Every model's coverage
  differs (M5 only covers weather/macro, M7 only matched cross-venue markets),
  so models are always compared by `cum_log_wealth / n_forecasts`
  (sleeping-expert normalization), never the raw cumulative total — a
  low-coverage sharp model shouldn't lose to a high-coverage mediocre one
  just because it forecast less. [`economy/wealth.py`](src/lab/economy/wealth.py),
  [`eval/wealth_plots.py`](src/lab/eval/wealth_plots.py).

- **Shadow MWU ensemble weighting (Phase 14.1).** A Hedge/multiplicative-
  weights challenger derives M4's category weights from relative wealth
  (`w_i ∝ exp(η_t · avg_log_wealth_i)`, `η_t = √(8 ln N / t)` — the standard
  regret-bound-optimal schedule), then clamps them with the same floor/
  ceiling the incumbent monthly fit now also carries. A single pool-wide
  correlation scalar can't stop a duplicated high-wealth model's *pair* from
  jointly dominating (it dilutes toward the mean once uncorrelated models
  are also in the pool), so the ceiling is enforced per correlation-*cluster*
  (union-find on pairwise correlation) instead of per model. Registers as a
  challenger under the exact same `model_versions` key the monthly fit uses,
  so promotion is a pointer flip — no changes needed anywhere else — gated
  by a 90-day/n≥200-per-category probation before it's even eligible for the
  standard CI-gated promotion. [`economy/mwu.py`](src/lab/economy/mwu.py).
- **Net-of-cost accounting (Phase 15).** The shadow portfolio's simulated fills are
  charged a real, sourced per-venue taker fee (`data/fee_schedule.yaml` — Polymarket's
  and Kalshi's actual published fee formulas, not invented numbers), and the report
  carries a net-of-cost skill line alongside the gross one — a headline "beats the
  market" number that ignores trading cost isn't one worth reporting.
  [`shadow/fees.py`](src/lab/shadow/fees.py).
- **Distributional scoring for bucketed events (Phase 16).** Many Kalshi/Polymarket
  macro and weather markets are really one numeric question split into mutually
  exclusive buckets (CPI ranges, temperature bands); scoring each bucket as an
  isolated binary throws away the cross-bucket shape. `eval/scoring.py` also computes
  the ranked probability score (RPS) per event — the same paired-vs-market,
  event-clustered, anytime-valid machinery as Brier — reported as a declared
  secondary outcome once a category has ≥20 resolved bucketed events (binary Brier
  stays the primary pre-registered outcome).
  [`eval/distributional.py`](src/lab/eval/distributional.py).

All of the above are fit or computed monthly inside `lab learn`, dry-run by
default, walk-forward validated, bounded-step, and CI-gated on promotion —
never in response to a single outcome (guardrails 14/15 in
[`CLAUDE.md`](CLAUDE.md)). Two narrow, explicitly-justified exceptions run
nightly instead, inside `lab eval`: the wealth ledger (pure arithmetic over
already-resolved forecasts, not a model parameter — it never writes a
forecast of its own) and the shadow MWU challenger above (guardrail 17 —
it touches only meta-level ensemble weights, never any model's internals,
and never affects production forecasts until it clears the same promotion
gate as any other challenger).

## Project status

All core phases, the multi-venue collection foundation, the measurement
upgrade, the hierarchical/pooling refinements, and the publication,
distributional-scoring, observation-quality, and operations-hardening rounds
(Phases 15–18) are implemented and tested (incl. the `test_scope.py` tripwire
that fails the build if execution-code strings ever land in `src/`):

- [x] Phase 0 — scaffold, config, CLI skeleton
- [x] Phase 1 — collection (Gamma/CLOB clients, tiering, snapshot loop, resolution watcher)
- [x] Phase 2 — historical bootstrap & M1/M2 fitting
- [x] Phase 3 — append-only ledger, M0–M2, scoring, static report, `lab export`
- [x] Phase 4 — M3 evidence pipeline (news → LLM extraction → deterministic aggregation)
- [x] Phase 5 — M5 structural nowcasts, M6 coherence scanner
- [x] Phase 6 — M4 ensemble, shadow portfolio (simulation), weekly report
- [x] Phase 7 / 7.1 — learning loop (`lab learn`: scheduled refits, `model_versions` registry, walk-forward guard, CI-gated promotion, automatic rollback, post-mortems)
- [x] Phase 8 — Streamlit dashboard (mostly read-only: live universe, forecasts vs market, calibration, shadow book, wealth economy; the one exception, Cross-Venue Matching (M7), lets a human confirm/reject proposed matches, writing to `data/markets_map.yaml` — see "Dashboard" below)
- [x] Phase 9 — cross-venue signal (M7): Kalshi read-only client (verified live, public, no auth), Metaculus client (requires an operator-supplied API token — Metaculus removed anonymous access; see `src/lab/api/metaculus.py` for the verified request shape), curated propose-then-confirm matching (`lab map propose` / `lab map confirm` / `data/markets_map.yaml`), wired into the ledger and the M4 weight fit
- [x] Phase 10 — multi-venue collection foundation: Kalshi/Metaculus/Manifold collectors, `venues`/`events` schema, synthesized `{venue}:{native_id}` keys, per-venue `lab status` lines
- [x] Phase 11 — measurement upgrade: event-level cluster bootstrap, anytime-valid confidence sequence (the actual promotion/rollback gate), precision-weighted stratified estimator, venue × category report matrix
- [x] Phase 12 — hierarchical recalibration (`m1_hier`): ridge-shrunk per-venue offsets on a shared global horizon curve, fit across Polymarket/Kalshi/Metaculus
- [x] Phase 13 — extremized, correlation-aware pooling: per-category extremization exponent on M4's and M7's pools, discounted by the correlation-adjusted effective source count
- [x] Phase 14 — virtual prediction economy: `wealth_ledger`, Kelly log-wealth accounting per (model, category) wired into the nightly `lab eval` step, sleeping-expert-normalized comparison (`cum_log_wealth / n_forecasts`), equity-curve/drawdown/attribution report section and a dedicated dashboard mode
- [x] Phase 14.1 — shadow MWU ensemble weighting: a wealth-derived, regret-bounded (Hedge/MWU) challenger to M4's category weights, cluster-aware floor/ceiling clamped, 90-day/n≥200-per-category probation, CI-gated and rollback-guarded through the same registry as any other challenger
- [x] Phase 15 — publication instrumentation: `universe_log` (every excluded market + reason code, `lab exclude` for manual entries), M3 boundary-randomization experiment (guardrail 12's pre-specified-seed carve-out), microstructure covariates persisted at forecast time (spread since Phase 15; depth, 24h volume and hour-of-day from 2026-08-10, when the rest of the sub-task was finally built — forward-only, never backfilled; 24h trade count is not collected, since neither venue reports one on the objects the collector already fetches), a real sourced venue fee schedule (`data/fee_schedule.yaml`) feeding a net-of-cost shadow-portfolio report section, a dated `docs/pre_analysis_plan.md`, and `lab export --paper` (anonymized replication dataset + manifest, now also snapshotted weekly and committed automatically — see below)
- [x] Phase 16 — distributional scoring: ranked probability score (RPS) for bucketed (negRisk / ordered-bucket) events in `eval/scoring.py` / `eval/distributional.py`, using the same pairing, event-clustering, and anytime-valid machinery as Brier — a declared secondary outcome, not a replacement for the primary pre-registered Brier skill
- [x] Phase 17 — observation-quality pack: versioned `data/categories.yaml` taxonomy (remaps logged, per-category fits keyed on the internal enum only), depth-based liquidity tiering (order-book depth, not wash-trading-contaminated volume) — applied to Kalshi as well from 2026-08-10, on the same threshold, once Kalshi order-book depth started being collected; matched-event high-frequency capture on confirmed cross-venue pairs (nominally 1-minute, measured at 2 — a round of both legs does not fit in a minute under the rate limiter, so the config states what is actually collected), a CLV validity diagnostic against the sports null control, and gap-aware derived metrics
- [x] Phase 18 — operations hardening: dead-man heartbeat (`HEARTBEAT_URL`) from the collector and the nightly backup job, the [`docs/OPERATIONS.md`](docs/OPERATIONS.md) operator runbook, and a completed, logged backup-restore drill

Every phase in the engineering brief ([`CLAUDE.md`](CLAUDE.md)) is now implemented and tested.

### Where it stands (2026-08-22)

The collector has run continuously against live Polymarket and Kalshi data
since early July 2026:

| | |
|---|---|
| forecast rows in the ledger | 729,512 |
| resolutions recorded | 100,764 |
| forecasts written per day | ~20,000 across ~4,100 markets |
| models writing daily | 11 |
| confirmatory window closes | 2026-12-31 (analysis freeze) |

**No skill claim has been made, and none is due yet.** Calibration statistics
need resolved markets to accumulate before anything clears the honesty
thresholds in the brief (n ≥ 200 = "preliminary", n ≥ 500 = "standard claim",
counted in resolved *event clusters*, not rows) — weather and the sports
null-control resolve in days, long-horizon politics in months. Promotion and
public claims are gated on the anytime-valid confidence sequence, which is
deliberately far more conservative than the fixed-n bootstrap interval printed
beside it.

The pre-analysis plan carries **23 dated addenda**. Most of them record
defects found in operation — coverage gaps, a scheduler dropping its own
firings, a model silently writing nothing on three days — together with the
exclusion window or robustness check each one implies. That record is the
point: a live instrument accumulates defects, and a study that only publishes
the clean half of its own history is not measuring what it claims to.

## Quickstart

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> forecast-lab && cd forecast-lab
uv sync
cp .env.example .env        # add DEEPSEEK_API_KEY (or ANTHROPIC_API_KEY) for M3
uv run lab --help
uv run pytest               # includes the scope-guard tripwire test
```

Configuration lives in `config.yaml` (universe filters, cadences, thresholds,
cost caps) — every default is documented inline.

## Commands

| Command | Purpose |
|---|---|
| `lab sync` | Discover markets from Gamma, tier the universe (liquid / tail / ignored) |
| `lab exclude <venue> <venue_native_id>` | Manually log a market as excluded from the universe (`universe_log`, reason_code='manual') |
| `lab bootstrap` | One-time historical bootstrap: download resolved markets, fit M1/M2 artifacts |
| `lab collect` | Long-running collector: order-book snapshots + resolution watcher |
| `lab forecast` | Generate forecasts for the eligible universe, freeze in the ledger |
| `lab eval` | Score resolved forecasts: paired Brier / log loss, skill with bootstrap CIs |
| `lab report` | Render the static HTML report |
| `lab shadow` | Simulated shadow portfolio (SIMULATION only) |
| `lab export` | Latest forecast per (market, model) as JSONL — the integration point; `--paper` emits the full resolved-forecast replication dataset + manifest instead (plain JSONL, or gzipped if `--out` ends in `.gz`; the weekly automated snapshot under `docs/paper_exports/` is gzipped from 2026-09-13 — see `docs/paper_export_schema.md` for the split date) |
| `lab status` | Data health: snapshot freshness, gaps, watcher lag, spend |
| `lab learn` | Monthly learning loop: batch refits, champion/challenger, post-mortems |
| `lab rollback <model_id>` | Manually revert a model's active version to a prior one, outside the monthly learn cycle |
| `lab run` | **One-button orchestrator**: collector + scheduled forecast/eval/report/shadow/learn in a single process |
| `lab watchdog` | Supervises `lab run`: auto-restarts it 10 minutes after any exit/crash |
| `lab guard` | Stop redundant or unmanaged lab instances (orchestrator, collector, dashboard, watchdog) |
| `lab ps` | List this machine's running lab instances; flag stale code versions and duplicates |
| `lab map propose` | M7: LLM proposes candidate Kalshi/Metaculus matches into `markets_map.yaml` (`proposed`, not live) |
| `lab map confirm <condition_id>` | M7: human confirms a proposed (or hand-curated) match — only confirmed pairs are ever forecast |
| `lab map list` | M7: show confirmed and pending-proposed matches |

### One button (recommended)

`lab run` keeps the collector alive and fires the analytics jobs itself on the
schedule in `config.yaml` (`schedule:` section, all UTC): forecast+eval+report
nightly, shadow weekly, learn monthly. It also runs one forecast/eval/report
pass on startup (`schedule.run_on_start`). No cron or systemd needed.

`lab watchdog` wraps `lab run` as a supervised child process: if it ever exits
for any reason (crash, hard kill, etc.), the watchdog waits 10 minutes
(`config.yaml` → `watchdog.restart_delay_seconds`) and restarts it — a
deliberate cooldown rather than a tight retry loop, so a genuinely broken
config doesn't hammer Polymarket's API in a crash loop. Any unhandled
exception in `lab run` itself is now also logged with a full traceback to
`data/logs/lab.jsonl` before the process exits (`sys.excepthook` + an asyncio
loop exception handler), so a crash always leaves a diagnosable trace.

On **Windows**, just double-click **`start.bat`** — it launches the watchdog
(which in turn supervises the orchestrator) plus the dashboard
(http://localhost:8501) in separate windows. Press `Ctrl+C` in the watchdog
window to stop everything. To halt polling without killing the process,
create the kill file `data/PAUSE` (delete it to resume).

```bash
uv run lab watchdog       # cross-platform equivalent of start.bat (no dashboard)
uv run lab run            # or run the orchestrator directly, without auto-restart
```

### Manual operation (advanced)

If you prefer external scheduling (cron / systemd / Windows Scheduled Tasks)
instead of `lab run`:

```bash
uv run lab collect                      # under tmux / systemd / a Scheduled Task
# nightly:
uv run lab forecast && uv run lab eval && uv run lab report
# weekly:  uv run lab shadow
# monthly: uv run lab learn
```

Back up `data/lab.db` and `data/snapshots/` daily from day one — historical
order-book snapshots cannot be re-downloaded later.

**This project does not assume one canonical deployment topology.** The reference
operator runbook, [`docs/OPERATIONS.md`](docs/OPERATIONS.md), documents the actual
production setup in full: primarily a Windows 11 laptop running the complete
pipeline (collector + forecast/eval/report/shadow/learn) under Windows Scheduled
Tasks rather than tmux/systemd/cron — CLAUDE.md's original assumption of "an
always-on Linux box" was a starting default, not a hard requirement, and the runbook
documents this as a deliberate, working deviation. A second host, a small Debian
VPS, separately runs `lab-collect.service` (a systemd unit) as an independent,
parallel collector for cross-checking data continuity — see
[`docs/VPS_OPERATIONS.md`](docs/VPS_OPERATIONS.md) for that host's setup; it does not
run the forecast/eval/report/shadow/learn pipeline, which stays the laptop's job for
now. Pick whichever scheduler fits your own platform — nothing in the code assumes
a specific one.

## Dashboard (optional)

A Streamlit dashboard over the same SQLite/Parquet, organized into six modes via a
sidebar selector: **Overview** (health + universe), **Forecasts vs Market**,
**Calibration & Skill**, **Wealth Economy** (Phase 14: equity curves, drawdown,
sleeping-expert rankings, M4 attribution — reusing the same plot functions the
static report renders), **Shadow Portfolio** (SIMULATION), and **Cross-Venue
Matching (M7)**.

Five of the six modes are read-only. **Cross-Venue Matching (M7) is the one
exception — it writes.** Confirming or rejecting a proposed match there calls the
exact same `confirm_match`/`reject_match` functions `lab map confirm` uses, editing
`data/markets_map.yaml` directly — this is the human-in-the-loop gate M7's design
requires (a human confirms every cross-venue pair before it's ever live), just
surfaced as a button instead of only a CLI argument. That mode's "Run `lab map
propose` now" button also makes real Kalshi + LLM API calls, drawing against the
same daily LLM cost cap as everything else (guardrail 10) — small, but not free and
not read-only.

```bash
uv sync --group dashboard
uv run streamlit run src/lab/dashboard.py
```

## Scope invariants

1. **No execution code.** Measurement instrument only; "buy"/"sell" appear
   only in clearly-labeled simulation code. Enforced by `tests/test_scope.py`.
2. **Public endpoints only.** No Polymarket account, no Polymarket API keys.
3. **No geoblock circumvention.** Unreachable endpoints are logged and the
   lab falls back to historical/offline sources.
4. **Polite API citizenship.** Global rate limiter, exponential backoff,
   conservative polling, and a `data/PAUSE` kill file.
5. **Immutable forecast ledger.** Forecast rows are never updated or deleted;
   scoring lives in separate tables. This is what makes the calibration
   measurement honest.

## Downstream use & license

Published under the **MIT license** — no usage restrictions of any kind:
commercial use, forks, closed-source derivatives, and execution layers built
on top are all permitted. The standard MIT warranty disclaimer applies;
downstream users are responsible for compliance in their own jurisdictions.

- **The forecast contract is the public API.** The SQLite schema and Parquet
  layout are a stable interface; any breaking change bumps the schema version
  in the `meta` table and is noted in the changelog.
- **`lab export` is the integration point.** Latest forecast per
  (market, model) with market metadata, as JSONL. External consumers —
  analytics, dashboards, execution layers — plug in here without touching
  lab internals.
- **Extension pattern.** An execution layer is a separate package or repo
  that consumes `lab export` (or reads the DB directly) and implements its
  own order logic, risk, and compliance. This repository defines the boundary
  and keeps its side of it.
- **Contributions.** PRs adding execution code to the core are declined and
  redirected to the extension pattern. Everything else — models, adapters,
  data sources, evaluation methods — is welcome.
