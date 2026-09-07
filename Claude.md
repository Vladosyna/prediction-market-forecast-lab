# Polymarket Forecast Lab — Implementation Brief for Claude Code

**v2.6** — the final gap: operations hardening (Phase 18) — dead-man heartbeat (the brief's own named worst failure is a silently dead collector, and the operator travels for weeks at a time), operator runbook, tested backup restore. §12 amended: a passive outbound heartbeat is not a "notification bot." No model work remains; everything further should come from data, not code.

**v2.7** — a verification audit cross-checked the paper concept memo against the live codebase and closed what it found. Implemented the three Phase 15 sub-tasks that were spec'd but missing: `universe_log` (§5/§15, with its own reason-code write path in `universe.py`), M3 boundary randomization (§6/guardrail 12's pre-specified-seed carve-out), and `lab export --paper` (§15's replication dataset + manifest). Added a real, sourced venue fee schedule (`data/fee_schedule.yaml`) for the shadow portfolio's net-of-cost line (§8/§15) — Polymarket's and Kalshi's actual published fee formulas, not invented numbers — which required adding a shadow-portfolio section to the report for the first time. Corrected two places where this document had drifted from the actual code rather than the reverse: §7's anytime-valid CS cites Howard-Ramdas-McAuliffe-Sekhon (2021), not Waudby-Smith-Ramdas, matching what `eval/anytime.py` has implemented and disclosed all along; §6's M1.x paragraph now describes the real mechanism (one shared, self-contested `m1_hier_curves` artifact, not separate per-venue contests against M1). `docs/pre_analysis_plan.md` gained its first two addenda (9.1: the analysis-freeze date; 9.2: the same CS citation correction plus a dispute-exclusion robustness commitment), added the only way its own append-only discipline permits. No model work remains.

**v2.8** — closed a real operational gap the paper-export machinery had carried since Phase 15: `lab export --paper` was on-demand only, never scheduled or committed anywhere. Added a weekly automated snapshot (`docs/paper_exports/YYYY-MM-DD.jsonl` + manifest, reusing `export_paper_jsonl`/`paper_export_manifest` verbatim) committed and pushed to this same public repo — the identical independent-verifiability rationale `docs/ledger_commitments.jsonl` already uses — wired into the orchestrator's own scheduler (`paper_export.cron`, config.yaml) beside the existing `map_propose`/`pmxt_verify` jobs. The manual `lab export --paper --out <path>` CLI flow is unchanged and stays available for one-off exports.

**v2.9** — the pmxt scan+verify cycle (§6 M7) moved from laptop-only to VPS-owned, so `data/markets_map.yaml` can now gain proposals from a host other than the one that reads it at forecast time. `run_pmxt_verify_job` now commits (and, per `cross_venue.markets_map_push`, pushes) `markets_map.yaml` to this public repo whenever it actually adds new proposals — previously this write was disk-only, left to a human to notice and commit. A new `commit_and_push_markets_map` (`m7_crossvenue.py`) needs no revert-on-failure unlike its ledger/paper-export cousins: the proposal is already durably on disk before the git step runs, so a failed commit just leaves the same pending change for the next call to retry, never losing or duplicating a proposal. A new `lab map pmxt-verify` CLI command exposes the job standalone, for a host (like the VPS) that runs the pmxt cycle on its own native OS timer without running the full `lab run` orchestrator — the pmxt *scan* itself (`scripts/pmxt_router_scan.py`) stays exactly as out-of-band as before (never imported into `src/lab`, guardrail unchanged), only the LLM-verify step gained a standalone entry point. Running scan+verify on two hosts at once was deliberately avoided (would silently double the $5/day LLM cap, enforced per-host against each machine's own `lab.db`, and race two independent full-file rewrites of the same git-tracked YAML) — the laptop's own `PolymarketForecastLabPmxtScan` task is paused now that the VPS is the sole owner.

**v2.10** — an external verification pass cross-checking an in-progress paper draft against the live system (§13's forecast-contract users read this repo the same skeptical way) found four gaps, all closed here, none of them model work. §6's BOCPD paragraph and guardrail 14 corrected: v2.4's wording described the changepoint-triggered `lab learn` trigger as if built; it was only ever specified, never implemented. Building it for real would be new learning-loop code, which v2.6 already declared closed, so the fix is documentation — the paragraph now says plainly that the monthly cron is the only live trigger. M3's boundary randomization (Phase 15, guardrail 12's carve-out) is now actually enabled (`config.yaml`'s `m3_boundary_randomization_enabled: true`) — implemented and tested since Phase 15 but left off by default pending the deliberate operator choice its own comment always called for; M3 coverage moves from a hard top-K cutoff to the randomized K±10 band from this point forward. `data/fee_schedule.yaml`'s Polymarket `effective_from` sentinel (`2000-01-01`, an honest "not a researched fact" placeholder since v2.7) is replaced by real dates: Qin & Yang (2026, arXiv:2606.04217)'s own staggered-fee-reform analysis gives category-level activation months (crypto Jan 2026, sports Feb 2026, other categories Mar 2026) that this repo's own fee-schedule research missed. PAP Addendum 9.2(b)'s disputed-markets robustness check had nothing to compare against — `disputed = 0` was unconditional everywhere, so "re-run excluding disputed markets" would have trivially reproduced the primary result; `resolved_forecast_rows`/`run_eval` (`eval/run.py`) gained an `include_disputed` parameter, exposed as `lab eval --include-disputed`, that scores the identical model/venue/category/window matrix with disputed markets included and writes it under a `_disputed_inclusive`-suffixed `window_label` alongside (never overwriting) the primary rows. No model work remains.

**v2.11** — a week of unattended operation plus a systematic audit against this document found nine defects, none of them model work, and closed the last of Phase 15's unbuilt sub-tasks. The largest was that **H1's primary statistic was never computed**: the PAP states it over horizon buckets, while `run_eval` iterated model × venue × category × window with no horizon dimension, so no `eval_runs` row ever corresponded to the primary hypothesis — and its realized n therefore went unseen for months (PAP 9.13/9.14). Buckets are now scored through the same machinery, inheriting the anytime-valid CS, event clustering and honesty tiers. Four defects were the same shape — a large read narrowed by neither columns nor rows — and each one OOM-killed something: the 1,967,376-row bootstrap set expanded into Python dicts, `latest_per_market` reading order-book blobs at 11 call sites, `clv_validity_check` reading the whole snapshot archive to score a handful of sports markets, and a Python mid-index built over it. Three were silent-skip bugs: APScheduler's 1-second default `misfire_grace_time` discarded cron firings whole (the weekly shadow job had not run in six days); `classify_cmdline` matched "run" anywhere, so every `uv run lab <subcommand>` looked like a duplicate orchestrator and was SIGTERMed — which would have killed the monthly `lab-learn` unit mid-run; and `from_crontab`'s day-of-week 0 means Monday, not Sunday, so every weekly job fired a day later than its own comment claimed. Two were research-data defects now disclosed rather than repaired in place: the price-move trigger had no minimum spacing, so a catch-up storm wrote 54,634 forecasts beyond one per market/model/day (PAP 9.5), and Kalshi's universe sync starved its own tail so 39,583 forecasts were written on markets already past their end date (PAP 9.11) — both left in the append-only ledger with pre-specified robustness checks committed alongside. Phase 15's microstructure covariates and Kalshi open interest are collected from 2026-08-10/11, forward-only, and now reach the replication export; `m3b_direct`, implemented since Phase 4 and never wired into the model list, produces forecasts from 2026-08-11. Kalshi tiering moved off a `liquidity_dollars` field the venue reports as 0.0 for every market — which had kept the entire venue out of the liquid tier, and out of the shadow portfolio with it — onto the same measured-depth threshold Polymarket already used. No model work remains.

**v2.12** — a coverage audit prompted by a simple question ("are forecast rows growing normally?") found that they were not, and closed four defects plus one operational gap. None is model work. **The largest was a six-day Kalshi blackout**: distinct Kalshi markets receiving forecasts fell from 2,676/day to 15–23/day for 2026-08-11 through 08-16, taking `m5_nowcast` — 93% of whose lifetime rows are Kalshi, and whose class §6 calls the highest-conviction edge in the design — to exactly zero for six days. The cause was established by eliminating tiering, snapshot availability, snapshot freshness and price validity in turn, each measured: v2.11's own end-date guard (PAP 9.11's remedy) fired correctly on `end_date_iso` values that Kalshi's still-starving sync had left stale in the past, and the survivors prove it — all 20 markets forecast on 08-14 carry end dates in 2026-09..2029-11. Disclosed as PAP 9.17, an exclusion window rather than a down-weighting, because the handful that survived are a long-dated subset selected by the defect itself. **Second, the M4 ensemble silently wrote almost nothing on three days** (7 rows against m0's 524 on 08-14, 12 against 499 on 08-16, 173 against 733 on 08-11, and exactly 1.00 of m0 every other day): `run_forecast_job` makes two passes, and the second re-derived its own eligibility 13–18 minutes later, reopening guardrail 13's 15-minute liquid freshness window against prices the first pass had already frozen. The fix hands both passes one frozen state set and one timestamp, which also closes a quieter defect that affected *every* day: M4's `p_market_at_ts` was read up to 18 minutes after the prices its own members were pooled from, so its paired baseline was not the one its inputs saw (PAP 9.18 declares the resulting series discontinuity at 2026-08-18). **Third, Kalshi's liquid tier was collected on a cadence its own freshness bound could not meet** — one venue-wide round took ~60 minutes, so 443 of 464 liquid markets failed the 15-minute gate every night, and those 464 are exactly the ones PAP 9.9/9.10 had promoted onto the measured-depth bar. Snapshot rounds were latency-bound, not rate-bound (1.2 req/s against a ceiling of 8, every await sequential); they now issue bounded-concurrent requests and Kalshi is split into a 5-minute liquid round and a 30-minute tail round (PAP 9.19). The token bucket still caps the rate, so guardrail 8 is untouched — concurrency changes only how much of the already-permitted rate a round can reach. The identical pattern on Polymarket is measured and recorded but deliberately NOT changed: its liquid round occupies its whole 5-minute interval and starves the tail to a ~7-hour realized cadence, with 1,508 of 2,393 tail markets past the 90-minute bound and 831 carrying no snapshot at all — `collect.snapshot_concurrency` is in place at 1 (byte-identical behaviour) pending a deliberate decision and its own addendum. **Fourth, operationally**: the private results mirror had accumulated 10.8GB of Git LFS storage against a 1GB free-tier allowance, entirely from `data/lab.db` (now 1.08GB, pushed whole every three days with no delta compression and every version retained forever), so the db backup moved to weekly — which halves the rate and does not fix the shape; LFS is the wrong medium for a monotonically growing blob. And a `ledger_commitment` push had been REJECTED (`fetch first`) because this host sat one commit behind after a docs push from the laptop, leaving a cryptographic pre-registration committed but unpublished — the state `config.yaml` itself calls worthless. That failure is silent by construction: the job logs `pushed: false` and returns normally — and it happened a second time the same day, to the weekly paper export at 11:05, which is what turned it from an incident into a design fault: two hosts write to the public repo by design, so a non-fast-forward rejection is the normal case and the jobs have to handle it rather than report it. A new `lab/gitutil.py` retries once behind `git pull --rebase --autostash`, wired into the ledger, paper-export and markets-map pushes but pointedly not into the LFS-backed results mirror, where a rebase would pull objects against the quota this same entry describes. **Fifth, and the one that explains the Polymarket half of all this**: the collector was discarding most of its own scheduled firings. APScheduler's default `misfire_grace_time` is one second, and a snapshot round issues back-to-back requests for minutes, so any firing whose moment arrived while the loop was busy was thrown away rather than delayed — measured over 10h45m, **146 liquid rounds completed against exactly one tail round**, with a misfire warning for every other collector job as well. That is the whole explanation for the Polymarket tail's ~7-hour realized cadence against a configured 60 minutes, its 410-minute median snapshot age against guardrail 13's 90-minute bound, and the 844 tail markets with no snapshot at all — including markets with $32M of recorded volume that had been re-synced the same morning. The 2026-07-14 audit had found this exact defect for the hourly `health_check` job and fixed it *there*, in a comment that describes the mechanism precisely; it was never generalized, so the lesson recorded here is that the test now asserts the property for every registered interval job rather than for one. PAP 9.20 declares the resulting grid discontinuity and the ~1,500 tail markets/day re-entering the forecastable population. `collect.snapshot_concurrency` was held at 1 through every one of these fixes — the misfire fix alone restores the configured cadence, and raising concurrency is a choice rather than a defect fix, so it was not folded into one. It was then raised to 8 later the same day as its own decision (PAP 9.21), on a margin argument rather than a rescue: the tail round still took ~12 minutes sequentially, which put the median tail snapshot age at 71.4 min against a 90-minute bound — inside it, with almost nothing to spare. Measured after the change, the Polymarket tail median is 34.4 min with 18 markets past the bound, against 410 min and 1,508 that morning. No model work remains.

**v2.13** — the coverage watchdog v2.12 built caught a collapse on its first real day, and what it caught was not a fault in the collector but a fault in what the collector was told to collect. Kalshi listed **13,290 new sports markets between 2026-08-20 and 2026-09-07** as the football season opened — `KXCYCLINGSTAGE`, `KXNCAAFCFPPOLL`, `KXMLBTEAMTOTAL`, `KXNFLRACE`, median volume 0 — taking sports to **11,625 of the 16,673 Kalshi markets in the snapshotted tiers (69.7%)**. The tail round grew with them and stopped fitting its own cadence: ~16,050 markets, **~86 minutes per round against a 30-minute interval**, two firings in three skipped by `max_instances=1`, the realized grid sitting exactly on guardrail 13's 90-minute bound. The forecast pass then skipped **4,959 markets on stale prices** and Kalshi's daily coverage fell from ~2,400 distinct markets to **10–33** on 2026-08-30 and 09-02 through 09-07. The point is what those 4,959 were: `eligible_market_states` applies the sports filter before the freshness check, so **none of them were sports** — they were the economics and politics markets the study is made of. §3 keeps sports as the null control, "a small random sample... forecast by the cheap models only" (30, seeded), and v2.12's own addendum 9.23 had already closed the last write paths that reached outside it, so ~70% of a venue's request budget was being spent on markets that cannot receive a forecast, and the study's own population paid for it. The venue-wide Kalshi rounds now collect only markets that can become forecast targets (~4,400, ~24 minutes), reversible in one config line. Three boundaries were drawn deliberately rather than by omission: the per-pair high-frequency job (Phase 17 item 3) is **not** filtered — its value is the price series H3 reads, not a forecast; the resolution watcher is **not** filtered, so sports outcomes keep feeding M1/M1.x and M2; and **Polymarket is unchanged**, because its rounds are not budget-bound after 9.21 and — the load-bearing reason — its fallback tier bar is real, so an unsnapshotted sports market there could fall out of `tier IN ('liquid','tail')`, leave the pool the control samples from, and never be drawable again. Kalshi's fallback tail bar is `min_volume: 0, min_open_interest: 0`, which every market clears, so on that venue the sampling frame after the change is the sampling frame before it — a config fact a test now asserts, because the change is only safe while it stays true. PAP 9.26 declares the lost days (2026-08-30 and 09-02..09-07) an exclusion window on 9.17's reasoning — the 10–33 daily survivors are whichever markets a starved round reached in time, a subset selected by the defect — and declares what the change costs: snapshot history for ~11,600 sports markets stops, unrecoverable. Measured the same day, on the running host: the tail round is **4,551 markets in 27m26s and then 14m22s**, inside its 30-minute interval against ~86 minutes and two skipped firings in three before it; the liquid round is **722 markets in 4m39s**, which also puts it inside its own 5-minute interval — so the second of the two things this entry set out to leave unfixed fixed itself, and the prediction that it would not (`~7 minutes, still over`) was wrong. What remains genuinely unfixed is the **universe sync, diluted by the same flood** — known Kalshi series went from ~285 to 827, so a full rotation at `max_series_per_sync: 40` now takes ~21 hours instead of ~9, which is the same stale-`end_date_iso` mechanism that produced 9.17's six-day blackout and therefore the next thing to watch. The honest close is the monitoring one: the watchdog fired on day one and could not tell anyone, because §12 forbids this lab from notifying and its own operator was travelling — five of the seven lost days are that gap, not a detection failure. No model work remains.

**Read this entire document before writing any code.** It is the single source of truth for this project. Work through the phases in order. Each phase has acceptance criteria — do not move to the next phase until they pass.

---

## 1. What we are building (and what we are NOT building)

**Goal.** A read-only research system that answers one question with statistical rigor:

> *Can we produce probability estimates for Polymarket event outcomes that are better calibrated than the market price itself, measured after resolution?*

The system continuously collects public market data, generates timestamped probability forecasts from several models, freezes them in an append-only ledger, waits for markets to resolve, and scores every model against the market baseline (Brier score, log loss, calibration curves). A simulated "shadow portfolio" translates any measured edge into hypothetical P&L. No real money is ever involved.

**Scope invariants of this repository (see §13 for the open-source model — these define what this repo is, not what its users may do):**

1. **This repository contains no execution code — by scope, not ideology.** It is a measurement instrument, published under MIT. Order placement, wallets, private keys, token allowances, L1/L2 CLOB authentication, EIP-712 signing all belong in downstream projects that consume this lab's forecast contract (§13). Inside this repo, "buy" and "sell" appear only in simulation code clearly labeled as such.
2. **Public endpoints only.** Use only unauthenticated, public REST/WebSocket endpoints. No account creation on Polymarket, no API keys for Polymarket (LLM API keys are fine).
3. **No geoblock circumvention.** No VPNs, no proxies, no routing tricks. If an endpoint is unreachable from the operator's network, log it and fall back to historical/offline data sources. Never work around access restrictions.
4. **Polite API citizenship.** Global rate limiter, exponential backoff on 429/5xx, conservative polling cadence, response caching where sensible.
5. **Immutable forecast ledger.** Once a forecast row is written, it is never updated or deleted. Scoring happens in separate tables. This is what makes the calibration measurement honest.

**Why this is legally clean (context, not legal advice):** reading public market data is research; forecasting is research; simulated positions involve no stake, so no gambling activity occurs; no Polymarket account or ToS acceptance is required for public data. This system is equivalent to what academic groups do with the same data.

---

## 2. Tech stack (decided — do not substitute)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Env/deps | `uv` | `pyproject.toml`, locked |
| HTTP | `httpx` (async) + `tenacity` | thin hand-written clients; do NOT use `py-clob-client` or any unified trading SDK (e.g. pmxt) as a runtime dependency — read-only usage of a trading SDK still pulls signing/custody code into the dependency tree, and the scope guard (§9.5) greps our own `src/`, not installed packages, so it wouldn't catch the drift. Tools of this kind (pmxt's matching engine included) are fine run out-of-band, by a human, to produce a file this repo then commits — never imported into `src/lab`. |
| Storage: state | SQLite via `sqlite3` stdlib | single file `data/lab.db`, WAL mode |
| Storage: time series | Parquet via `polars` | partitioned by date under `data/snapshots/` |
| Schemas | `pydantic` v2 | all API responses validated |
| Scheduling | `APScheduler` inside a long-running `collect` process | run under tmux/systemd |
| LLM | Anthropic API (`anthropic` SDK), model configurable, default a current Sonnet-class model | strict JSON outputs, daily cost cap |
| Math/eval | `numpy`, `scipy` | bootstrap CIs |
| Plots/report | `matplotlib` + `jinja2` → static HTML | no web server in core |
| CLI | `typer` | `lab <command>` |
| Tests | `pytest` | scoring math is test-critical |
| Dashboard (Phase 6, optional) | Streamlit | reads the same SQLite/Parquet |

Secrets: `.env` with `ANTHROPIC_API_KEY` (or the configured LLM provider's key), `FRED_API_KEY`, `METACULUS_API_KEY`, and optional `NEWSAPI_KEY`. **A test must fail if any code imports web3/eth-account or references the CLOB `POST /order` path** (see §9).

---

## 3. Data sources — quick reference

All public, no auth. As of mid-2026 (post CLOB V2 migration, April 2026):

- **Gamma API** — `https://gamma-api.polymarket.com`
  Market/event metadata, resolution criteria, token IDs. Endpoints: `/markets`, `/events`. Pagination via `limit` + `offset` (limits reduced in 2026 — assume ≤100 per page and paginate). No free-text search; filter by `slug`, tag, `active`, `closed`.
- **CLOB API (public market data only)** — `https://clob.polymarket.com`
  `/book?token_id=` (order book), `/price?token_id=&side=`, `/midpoint?token_id=`, `/prices-history?market=<token_id>&interval=&fidelity=` (price time series).
- **Data API** — `https://data-api.polymarket.com`
  Public trades, holder/position aggregates.
- **WebSocket (market channel, public)** — `wss://ws-subscriptions-clob.polymarket.com/ws/`
  Real-time book updates. Use for the liquid tier if REST polling proves too coarse; start with REST.
- **Historical bootstrap** — HuggingFace dataset `SII-WANGZJ/Polymarket_data` (1.1B records, 268,706 markets; also mirrored on GitHub). Covers Polymarket's inception through end of 2025 — it is a static snapshot, not a live feed. 2026-onward data comes from our own Phase 1 collector, not this dataset. Pull only two of its five files:
  - `quant.parquet` (~21–27GB) — cleaned trades unified to the YES token; this is the "prices at multiple horizons" source for M1/M2.
  - `markets.parquet` (~68–165MB) — market metadata and resolution outcomes; joins to `quant.parquet` on market id.
  Skip `orderfilled.parquet`, `trades.parquet`, `users.parquet` (85GB combined, raw/user-level — not needed for fitting). Fetch via `huggingface_hub`:
  ```python
  from huggingface_hub import hf_hub_download
  hf_hub_download(repo_id="SII-WANGZJ/Polymarket_data", filename="quant.parquet", repo_type="dataset", local_dir="data/bootstrap/")
  hf_hub_download(repo_id="SII-WANGZJ/Polymarket_data", filename="markets.parquet", repo_type="dataset", local_dir="data/bootstrap/")
  ```
  Then filter with `polars.scan_parquet(...)` (lazy) down to resolved binary markets — never load either file fully into memory.
- **External model inputs (for M5):**
  - *Weather:* Open-Meteo **Ensemble API** — `https://api.open-meteo.com/v1/ensemble?latitude=&longitude=&hourly=<vars>&models=<model>` — returns per-member forecasts (up to 51 members), which is what P(threshold exceeded) actually needs. The plain `/v1/forecast` endpoint returns one deterministic value and is not sufficient here. No key, CC BY 4.0 (attribute in README), free tier caps at 10,000 calls/day — irrelevant at our scale. `api.weather.gov` (NWS) as a secondary cross-check for US station observations only.
  - *Macro:* **FRED API is primary**, not optional — it cleanly mirrors both Atlanta Fed nowcasts as documented series: `GDPNOW` (real GDP growth) and `PCENOW` (real PCE growth). Free key from `fredaccount.stlouisfed.org`, stored as `FRED_API_KEY`. Request pattern: `https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW&api_key=...&file_type=json`. Cleveland Fed's separate CPI/PCE inflation nowcast has no confirmed stable machine endpoint as of this writing — its page is `clevelandfed.org/indicators-and-data/inflation-nowcasting`; inspect its actual download mechanism at Phase 5 implementation time rather than hardcoding a guessed URL, and skip that specific sub-adapter if none is found. GDPNow alone already covers the growth side of the macro category.
  - *Cross-venue sources (M1.x recalibration, M7 signal — all read-only):* **Kalshi** — `https://external-api.kalshi.com/trade-api/v2` (`/series`, `/events`, `/markets`, `/markets/{ticker}/orderbook`); market data requires no auth; ~10 req/s — route through the same global rate limiter. **Metaculus** — official public API (`metaculus.com/api`); community prediction on public questions; authenticate with `METACULUS_API_KEY` from `.env` for higher limits; when a community prediction is hidden (tournament hiding windows), record NULL — never impute. **Manifold** — public API, play money: collected for event mapping and M2 base rates only; excluded from M7, from M1.x fits, and from all skill claims (guardrail 16). Betfair requires an account — out of scope.
  - *Historical archives (GJP, PredictIt, the HF bootstrap):* M2 base rates only, provenance-tagged. Survivorship and selection effects make them unusable for M1.x fits and for skill claims.
  - *Pooling rule (amended in v1.9):* naive pooling of venues into one recalibration fit remains forbidden — venue biases differ and can be opposite. The sanctioned mechanism is hierarchical **partial** pooling (§6 M1.x): cross-venue information enters only as shrinkage toward a shared global curve, never as raw pooled observations pretending to be one venue.

Domain facts to encode:
- Every binary market has two outcome tokens (YES/NO); prices are in [0,1] and the pair sums to ≈ 1. Track the YES token; store its `token_id` and the `condition_id` of the market.
- Markets resolve via UMA optimistic oracle; there is a dispute window (~2h) and occasional contested resolutions. The resolution watcher must record final payout, not first report. Flag disputed markets.
- Metadata can exist for markets with no live book. Filter universe on `active`, `closed`, `enableOrderBook`/`accepting_orders`, and liquidity/volume fields.
- Rate limits are generous for reads (thousands per 10s) but poll far below them: default snapshot cadence 5 min for the liquid tier, 60 min for the tail. One universe sync per hour.

**Universe policy (from the edge research — encode in `config.yaml`):**

- **Priority tiers for forecasting:** (P1) economic data releases & central-bank decisions — model-drivable; (P2) weather markets where present — model-drivable; (P3) long-horizon (≥30 days to resolution) politics/geopolitics — documented long-horizon underconfidence makes recalibration the edge; (P4) entertainment/awards — base-rate drivable.
- **Excluded as structurally unforecastable:** ALL crypto/equity price-target markets at any horizon (martingale underlyings — the market price *is* the forecast); "will X say/tweet Y" novelty markets and anything with ambiguous resolution wording; markets already priced ≥ 0.95 or ≤ 0.05 (residual edge there is dominated by oracle/dispute tail risk — keep them in calibration stats, never as forecast targets).
- **Null control:** keep a small random sample of sports markets in the ledger, forecast by the cheap models only. Sports markets are near-efficient; if the lab "finds skill" there, the harness is broken, not the market. The weekly report prints null-control skill next to everything else.

---

## 4. Repository layout

```
forecast-lab/
├── pyproject.toml
├── .env.example
├── config.yaml                  # universe filters, cadences, thresholds, cost caps
├── README.md                    # generated in Phase 0: quickstart + commands
├── src/lab/
│   ├── api/                     # thin public-data clients
│   │   ├── gamma.py
│   │   ├── clob.py
│   │   └── dataapi.py
│   ├── store/
│   │   ├── db.py                # sqlite schema + migrations + writers
│   │   └── snapshots.py         # parquet append/read
│   ├── collect/
│   │   ├── universe.py          # market discovery & tiering
│   │   ├── snapshots.py         # book/price snapshot loop
│   │   └── resolutions.py       # resolution watcher
│   ├── models/
│   │   ├── base.py              # Forecaster protocol: forecast(market, context) -> p_yes
│   │   ├── m0_market.py
│   │   ├── m1_debiased.py
│   │   ├── m2_baserate.py
│   │   ├── m3_evidence.py
│   │   ├── m1_hier.py           # v1.9 hierarchical multi-venue recalibration (M1.x)
│   │   ├── m5_nowcast.py        # + thin per-category data adapters (weather, macro)
│   │   ├── m6_consistency.py
│   │   ├── m7_crossvenue.py     # + api/kalshi.py, api/metaculus.py, api/manifold.py thin read-only clients (Phases 9–10)
│   │   └── m4_ensemble.py
│   ├── news/
│   │   ├── providers.py         # RSS + Google News RSS per market; NewsAPI optional
│   │   ├── extract.py           # LLM evidence extraction (strict JSON)
│   │   └── aggregate.py         # deterministic log-odds aggregation
│   ├── eval/
│   │   ├── scoring.py           # brier, log loss, paired skill
│   │   ├── calibration.py       # reliability diagrams
│   │   ├── clv.py               # price-drift metric
│   │   └── report.py            # jinja2 HTML report
│   ├── learn/
│   │   ├── refit.py             # scheduled parameter refits (M1 curves, M2 rates, M3 aggregator, M5 error dists, M4 weights)
│   │   ├── registry.py          # model_versions CRUD, promotion gate, rollback circuit breaker
│   │   └── postmortem.py        # structured post-mortems on top-decile misses/wins
│   ├── economy/
│   │   ├── wealth.py            # wealth_ledger updates, sleeping-expert normalization (v2.0)
│   │   └── mwu.py               # shadow multiplicative-weights challenger for M4 weights (v2.0)
│   ├── shadow/
│   │   └── portfolio.py         # simulated positions
│   └── cli.py
├── tests/
├── reports/                     # generated HTML (gitignored)
└── data/                        # sqlite + parquet (gitignored)
```

---

## 5. Database schema (SQLite)

```sql
CREATE TABLE meta (
  key TEXT PRIMARY KEY,             -- 'schema_version', 'created_at', ...
  value TEXT
);

-- v1.9 multi-venue migration (Phase 10). Applied via ALTER/CREATE migrations on the live DB, never by recreating tables.
-- Phase 15 (v2.3): every market considered and excluded from the universe, with a reason —
-- answers "why isn't X in the ledger" and defends against selection-bias claims in review.
CREATE TABLE universe_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  venue TEXT NOT NULL, venue_native_id TEXT NOT NULL,
  reason_code TEXT NOT NULL   -- 'crypto_price_target','ambiguous_resolution','tail_price','low_liquidity','manual', ...
);

CREATE TABLE venues (
  venue TEXT PRIMARY KEY,            -- 'polymarket','kalshi','metaculus','manifold'
  trust_tier TEXT CHECK(trust_tier IN ('money','reputation','play')),
  forecastable INTEGER DEFAULT 0,    -- venues whose markets we forecast and score: polymarket=1, kalshi=1
  in_m7_pool INTEGER DEFAULT 0       -- polymarket=0 (M0 already carries it), kalshi=1, metaculus=1, manifold=0
);

CREATE TABLE events (
  event_id TEXT PRIMARY KEY,         -- minted on first human-confirmed cross-venue match (markets_map.yaml flow)
  title TEXT, created_ts TEXT
);

-- markets gains three columns via ALTER: venue TEXT DEFAULT 'polymarket',
-- venue_native_id TEXT, event_id TEXT NULL. condition_id remains the universal market key:
-- non-Polymarket rows synthesize it as '{venue}:{venue_native_id}', so every existing FK,
-- the forecasts ledger, and the snapshot layout keep working unchanged.
-- Snapshot parquet gains a 'venue' column; for venues without an order book
-- (Metaculus community prediction, Manifold), store the venue probability in 'mid'
-- and leave book fields NULL.

-- discovered markets (binary only, v1)
CREATE TABLE markets (
  condition_id TEXT PRIMARY KEY,
  slug TEXT, question TEXT, category TEXT,
  description TEXT,               -- verbatim resolution criteria; critical for M3
  end_date_iso TEXT,
  token_id_yes TEXT, token_id_no TEXT,
  neg_risk INTEGER DEFAULT 0,
  active INTEGER, closed INTEGER,
  liquidity_num REAL, volume_num REAL,
  tier TEXT CHECK(tier IN ('liquid','tail','ignored')),
  first_seen_ts TEXT, last_synced_ts TEXT
);

CREATE TABLE resolutions (
  condition_id TEXT PRIMARY KEY REFERENCES markets(condition_id),
  resolved_ts TEXT,
  payout_yes REAL CHECK(payout_yes IN (0.0, 1.0)),
  disputed INTEGER DEFAULT 0,
  source TEXT                      -- 'gamma' etc.
);

-- append-only. NEVER UPDATE OR DELETE ROWS.
CREATE TABLE forecasts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                -- UTC ISO, freeze moment
  condition_id TEXT NOT NULL,
  model_id TEXT NOT NULL,          -- 'm0_market', 'm1_debiased', ...
  p_yes REAL NOT NULL CHECK(p_yes > 0 AND p_yes < 1),
  p_market_at_ts REAL NOT NULL,    -- YES mid captured at the same moment
  spread_at_ts REAL,
  inputs_hash TEXT,                -- sha256 over: model code version, curve/artifact versions, config hash, input snapshot ids
  evidence_run_id INTEGER,         -- FK for m3
  cost_usd REAL DEFAULT 0,
  -- Phase 15 (v2.3), all nullable — populated going forward, never backfilled by reconstruction:
  -- spread_at_ts above already covers the spread covariate; only depth/volume/timing are new here.
  depth_covariate REAL, volume_24h REAL, trades_24h INTEGER, hour_utc INTEGER,
  m3_randomized INTEGER DEFAULT 0, m3_random_seed TEXT   -- boundary-randomization experiment, guardrail 12
);

CREATE TABLE evidence_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT,
  dossier_json TEXT,               -- articles fetched, extraction output, aggregation trace
  llm_model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL
);

CREATE TABLE eval_runs (
  id INTEGER PRIMARY KEY,
  ts TEXT, model_id TEXT, window_label TEXT,
  n INTEGER,
  brier REAL, brier_market REAL, skill REAL,          -- skill = brier_market - brier (positive = we beat market)
  skill_ci_lo REAL, skill_ci_hi REAL,                  -- cluster bootstrap by event_id (fallback condition_id) — see §7
  skill_pw REAL,                                       -- precision-weighted stratified skill (§7, v2.1); NULL if <3 strata qualify
  skill_pw_ci_lo REAL, skill_pw_ci_hi REAL,            -- bootstrap CI on skill_pw
  n_strata_pw INTEGER,                                 -- qualifying price-bucket strata (n_s >= 30) used
  log_loss REAL, log_loss_market REAL,
  calibration_json TEXT                                -- bins for reliability diagram
);

CREATE TABLE shadow_trades (
  id INTEGER PRIMARY KEY,
  opened_ts TEXT, condition_id TEXT, token_side TEXT CHECK(token_side IN ('YES','NO')),
  entry_price REAL, p_model REAL, p_market REAL, edge REAL,
  stake_sim REAL, kelly_frac REAL,
  exit_ts TEXT, exit_price REAL, pnl_sim REAL,
  status TEXT CHECK(status IN ('open','resolved','abandoned'))
);

CREATE TABLE postmortems (
  id INTEGER PRIMARY KEY,
  ts TEXT, condition_id TEXT, model_id TEXT,
  kind TEXT CHECK(kind IN ('miss','win')),
  brier_model REAL, brier_market REAL,
  analysis_json TEXT,              -- structured: error_source, evidence_quality, resolution_reading, notes
  llm_model TEXT, cost_usd REAL
);

-- append-only. Rollback = repoint is_active, never rewrite a row.
-- Coexists with the data/models/*.json artifact files from Phase 2: this table is authoritative
-- for VERSIONING (active/promoted/retired state, audit trail); the JSON files remain artifact
-- storage. artifact_path points at the existing file rather than duplicating its contents.
-- data/models/ACTIVE.json is a generated pointer, rewritten by registry.py whenever is_active
-- changes — it is a cache of this table, never edited independently.
CREATE TABLE model_versions (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,           -- 'm1_debiased', 'm3_evidence', 'm4_ensemble', ...
  version_tag TEXT NOT NULL,        -- e.g. 'v3'; human-readable, not semver-enforced
  artifact_path TEXT NOT NULL,      -- e.g. 'data/models/m1_debiased_v3.json'; file content immutable once written
  params_hash TEXT NOT NULL,        -- sha256 of the artifact file, for integrity verification
  fit_window_start TEXT, fit_window_end TEXT,   -- walk-forward train window; NULL for hand-set v1 defaults
  registered_ts TEXT NOT NULL,      -- challengers earn track record only from forecasts after this
  promoted_ts TEXT,                 -- NULL while still a challenger
  retired_ts TEXT,                  -- NULL while active
  retired_reason TEXT CHECK(retired_reason IN ('replaced','rollback') OR retired_reason IS NULL),
  is_active INTEGER DEFAULT 0       -- exactly one active row per model_id; enforced in registry.py
);

-- derived from forecasts + resolutions; always recomputable, not a backup-critical table.
-- One row per (model_id, category, resolved forecast): the Kelly-fraction wealth process already
-- used by the shadow portfolio (§8), generalized to every model as a scoring/selection layer —
-- NOT a second trading simulation. Unlike §8 (M4 only, entry-filtered, "would we have traded"),
-- this table scores EVERY resolved forecast from EVERY model unconditionally, maximizing n for
-- comparison purposes (§7/§11's whole point). log_wealth_delta is the Kelly log-growth for a
-- binary bet, same side rule as §8 (YES if p_model > p_market, else NO) — mathematically the
-- same quantity as the anytime-valid confidence sequence in §7 (a wealth process IS a test martingale).
CREATE TABLE wealth_ledger (
  id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,
  category TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  event_id TEXT,                     -- for event-level attribution, mirrors §7's clustering
  ts TEXT NOT NULL,                  -- resolution timestamp
  kelly_fraction REAL NOT NULL,      -- same 0.2x-capped fraction as shadow_trades (§8)
  log_wealth_delta REAL NOT NULL,    -- log(1 + f*(1/p_market - 1)) if YES resolves, log(1 - f) if NO
  cum_log_wealth REAL NOT NULL,      -- running sum for this (model_id, category)
  n_forecasts INTEGER NOT NULL       -- running count; cum_log_wealth / n_forecasts is the fair,
                                      -- coverage-normalized comparison metric (sleeping-expert rule, §6)
);
```

Snapshots go to Parquet, not SQLite: columns `ts, condition_id, token_id_yes, best_bid, best_ask, mid, spread, bid_depth_usd, ask_depth_usd, last_trade_price`, partitioned `data/snapshots/date=YYYY-MM-DD/*.parquet`.

---

## 6. Forecast models

All models implement `Forecaster.forecast(market, context) -> ForecastResult(p_yes, meta)`. Probabilities are clamped to [0.01, 0.99] before writing.

**M0 `m0_market` — the null model.** `p_yes = market mid`. This is the baseline every other model must beat. It is written to the ledger like any other model so paired comparison is trivial.

**M1 `m1_debiased` — horizon-aware market recalibration.** The strongest documented, implementable edge: prediction-market prices are systematically **underconfident at long horizons** (calibration slope > 1 far from resolution, converging to ≈1 near resolution), and recent Polymarket data shows a **reversed favorite-longshot bias** at the tails — so never hardcode a bias direction; fit it. Implementation: logistic recalibration `logit(p̂) = α_h + β_h · logit(p_market)` fitted per time-to-resolution bucket (<7d, 7–30d, 30–90d, >90d) and, once n allows, per category, on historical resolved markets from the bootstrap. β_h > 1 at long horizons = extremizing. Guards: isotonic sanity check, monthly refit, every curve versioned, horizon bucket stored with each forecast.

**M1.x `m1_hier@{venue}` — hierarchical multi-venue recalibration (v1.9).** One recalibration family, one curve per venue: `logit(p̂) = (α_g + α_v) + (β_g + β_v) · logit(p_venue)` per horizon bucket, where the global parameters (α_g, β_g) are fit on all venues and the venue offsets (α_v, β_v) are shrunk toward zero by a ridge penalty scaled ∝ 1/n_v — empirical Bayes: small venues borrow the global shape, large venues earn their own. Penalized logistic via scipy; no MCMC — a NumPyro upgrade is a v2 candidate only if the ridge tier proves insufficient. Fits on polymarket, kalshi, and metaculus outcomes; never on manifold or archives (guardrail 16). Roles (implementation note, v2.7): `m1_hier@polymarket`, `m1_hier@kalshi`, and `m1_hier@metaculus` are per-venue *labels* on Forecaster instances that all read one shared, jointly-fit artifact registered under a single key, `m1_hier_curves` — the champion/challenger contest happens once, at the artifact level, self-contested against its own prior version (first fit auto-promotes with no incumbent to touch, per guardrail 18); M1's own incumbent artifact (`m1_curves`) is never a party to this contest — a deliberate simplification over an earlier per-venue-vs-M1 design that was never implemented. `m1_hier@metaculus`'s role recalibrating the community prediction as an **input signal** for M7 is unaffected — Metaculus is still not a forecast target (`forecastable=0`). Caveat, not a defect: variance-component estimation in hierarchical models is textbook-unstable with only 2–3 groups; expect this to behave more like a heuristic regularizer than a well-identified decomposition until more venues exist — don't over-read early between-venue variance estimates.

**M2 `m2_baserate` — category base rates.** For question templates that recur ("Will X happen by DATE"), compute historical base rates by category from resolved markets and blend with market prior in log-odds space with a small fixed weight. This is a sanity-check model, expected to be weak alone.

**M3 `m3_evidence` — LLM evidence pipeline.** The interesting one. Strict separation of roles:

1. **Dossier build** (deterministic): question, verbatim resolution criteria from `markets.description`, end date, current price, 7-day price path.
2. **Retrieval** (pluggable `NewsProvider`): Google News RSS query built from market keywords + configurable RSS feed list; optional NewsAPI if key present. Dedup by URL, keep publish timestamps.
3. **Extraction** (LLM, strict JSON): for each article, extract evidence items:
   `{claim, direction: "for_yes"|"for_no"|"neutral", strength: 1-3, source_reliability: 1-3, relevance: 0.0-1.0, published_ts}`.
   The prompt must include the resolution criteria and instruct the model to judge relevance *to the resolution criteria*, not to the topic. Reject/retry on invalid JSON.
4. **Aggregation** (deterministic, unit-tested — **the LLM never writes the final number**):
   - Start from market prior: `L = logit(p_market)`.
   - Each item contributes `Δ = k · strength · reliability · relevance · sign(direction)` with recency decay `exp(-age_days/τ)`; defaults `k=0.15`, `τ=5`.
   - Cap total shift per run at ±0.8 log-odds. `p_yes = sigmoid(L + clipped ΣΔ)`.
5. **Cost control**: per-day USD cap in config; when exceeded, M3 skips markets (log it). Every run stored in `evidence_runs` with full trace.

Optional experiment `m3b_direct`: the LLM states a probability directly (with a calibration-focused prompt). Logged like any model. The lab will then *measure* whether deterministic aggregation beats direct LLM estimates — a genuinely useful result either way.

**M5 `m5_nowcast` — structural models for model-drivable categories.** The highest-conviction edge class in the literature: where an external quantitative model publishes a distribution, map it onto the market's resolution criteria and output a probability directly.
- *Weather markets:* pull ensemble forecasts (open-meteo / NWS) for the referenced station and date; compute P(threshold exceeded) from ensemble spread.
- *Econ releases / Fed:* Cleveland Fed nowcast, GDPNow → P(print lands in the market's bucket) via an error distribution fitted on historical release surprises.
One thin adapter per category. Do not generalize prematurely — two adapters is the v1 scope.

**M6 `m6_consistency` — cross-market coherence scanner.** Deterministic, no LLM. Within a negRisk event, mutually exclusive YES prices must sum to ≈ 1 — record the deviation. For logically linked market pairs (curated `links.yaml`, starts empty), check conditional-probability bounds. Output is a signal attached to forecasts (direction + magnitude of incoherence); M6 emits standalone forecasts only for incoherent legs, pulling them toward the coherent joint solution.

**M7 `m7_crossvenue` — cross-venue signal on matched questions.** For markets that also trade on Kalshi or carry a Metaculus community prediction, output the log-odds pool of the *external* venues' probabilities — Polymarket's own price stays out (M0 already carries it; the ensemble learns how much to trust each source). Question matching is the failure point and is handled conservatively: a curated `data/markets_map.yaml` where candidates are *proposed* (by the LLM, or — faster and likely more accurate for this specific job — a human running a third-party matching tool like pmxt's Router out-of-band, per §2's dependency rule) but a human confirms every pair before it goes live — one mismatched pair silently poisons the signal. `markets_map.yaml`, not whatever proposed it, is the committed source of truth read at forecast time — this is what keeps the pipeline reproducible (Phase 15) regardless of which proposer tool comes and goes. External prices are snapshotted at forecast ts under the same freshness rule (§9.13). Deterministic at forecast time; no LLM call in the loop.

**M4 `m4_ensemble`.** Log-odds weighted pool of M0–M3, M5–M7; weights fit per category on a rolling window of resolved forecasts (equal weights until ≥100 resolved samples in that category), with the same 2%/60% per-model floor and ceiling given to the MWU challenger below (added here in v2.2 for parity — the incumbent shouldn't be less protected against streak-driven weight concentration than the challenger trying to unseat it). v1.9 adds a per-category **extremization exponent** `a ∈ [1.0, 2.5]` (config caps) applied to the pooled logit, fitted in `lab learn` under the bounded-step and challenger rules. Extremization compensates for the pool's shrink toward 0.5 — but only to the degree the sources are independent: scale it with the effective source count `n_eff = n / (1 + (n − 1) · ρ̄)`, where ρ̄ is the mean pairwise correlation of source log-odds estimated on matched events. Correlated venues must not be extremized as if independent; the same n_eff discount applies to M7's external pool. Where M5 exists it should dominate — the fit will discover that; don't hand-tune. (Model IDs are stable; the numbering is historical, ensemble stays last in the pipeline.)

**Forecast cadence:** once per market per day per model (config), plus an extra forecast when |24h price move| > 0.10 on a tracked market. M3 runs only on the liquid tier, selected by a **deterministic rule** (top-K by liquidity within priority categories) — never by perceived difficulty, or the skill measurement inherits selection bias. M5 runs on every market its adapters cover. M6 runs on every negRisk event in the universe.

**Wealth ledger & virtual prediction economy (v2.0).** A scoring and selection layer over M0–M7, not a new forecasting model — it consumes their already-written forecasts and never writes a `p_yes` row of its own.

*Why this is principled, not gamified:* Kelly (1956) and Cover's universal-portfolio results establish an exact duality between log-optimal betting and log-score: a model's wealth, grown by staking a Kelly fraction of its edge against the market price, compounds at the model's actual log-score advantage over the market. This sits in the same theoretical family as §7's anytime-valid confidence sequence — both are betting-theoretic constructions in the Shafer & Vovk "testing by betting" sense — **but they are not the same computation** (v2.2 correction of an earlier overclaim): the CS uses a mixture betting strategy tuned for statistical tightness; the wealth ledger uses a fixed, capped fractional-Kelly strategy tuned for interpretable, realistic-looking P&L. Related in spirit, genuinely different numbers — implement as two separate processes, and don't attempt to derive one from the other or unify them into a shared code path.

*Mechanics.* Wired into the existing nightly `lab eval` step — no new CLI command. For every resolved forecast from every model, unconditional on §8's entry filter (this table exists to maximize comparison power, §7/§11's whole point, not to simulate realistic execution), compute the same 0.2×-capped Kelly fraction and side rule §8 uses, and accumulate the resulting log-wealth delta per `(model_id, category)` in `wealth_ledger`.

*Sleeping experts.* M5 only covers weather/macro, M7 only covers matched cross-venue markets. Comparing raw cumulative wealth would reward or punish coverage rather than skill. Always compare `cum_log_wealth / n_forecasts` (average per-forecast log-growth) across models with different coverage — never the raw cumulative total.

*Wealth-based ensemble weighting (the MWU challenger, Phase 14.1).* A new `m4_ensemble@mwu` challenger derives per-category weights from relative wealth via a multiplicative-weights update (Hedge/MWU: `w_i ∝ exp(η_t · cum_log_wealth_i)`), with learning rate `η_t = √(8 ln N / t)` shrinking as resolved-forecast count `t` grows — the standard regret-bound-optimal schedule, not a hand-tuned rate. A weight floor (2%) and ceiling (60%) per model prevent a short streak from collapsing weight onto one model (the documented failure mode in Numerai's staking history). The same correlation discount from the M4 paragraph above (n_eff via ρ̄) applies before weights are normalized — correlated high-wealth models must not jointly dominate. This challenger computes nightly, in shadow, alongside the existing eval step, but is governed by the exact same promotion machinery as any other challenger below: it earns production weight only after clearing the anytime-valid CI-gated promotion, with the standard rollback circuit breaker watching it afterward. This nightly cadence is the one narrowly-scoped, explicitly justified exception to guardrail 14 (§9.17) — every other model's internal parameters remain strictly monthly-batch.

**Learning & versioning (how the system improves from its own record — and the hard line around the LLM).** Learning happens in batches over resolved outcomes, never per decision — a single win or loss is noise, and a system that adjusts to individual outcomes learns the noise. All mechanisms run inside the monthly `lab learn` job, dry-run by default. v2.4 specified one more trigger, not a faster clock, as future work: a Bayesian online changepoint detector (BOCPD, Adams–MacKay) on each category's rolling paired-Brier stream that would fire an off-schedule `lab learn` run when change probability crosses a config threshold — same walk-forward, bounded-step, challenger, and dry-run rules; the trigger would change WHEN a batch runs, never HOW MUCH it may move (guardrail 14, as amended). Corrected in v2.10: this was never implemented — v2.4's wording read as if it had been, which a v2.9 paper-draft audit caught. `lab learn`'s only trigger today is the monthly cron; the monthly cadence is what guardrail 14 governs in practice. Build it if a real regime-shift need materializes; until then this paragraph describes a documented, deliberately deferred design, not live code — and building it would itself be new model/learning-loop code, which v2.6 already declared closed. A quarterly full-Bayes audit (NumPyro) of the M1.x shrinkage strengths is an optional validation task inside `lab learn`, not a nightly dependency and not a migration off the ridge tier.

*The line that must never move:* the LLM's weights are never fine-tuned, and the LLM is never re-invoked against a historical dossier after the fact. "Training" in this codebase means exactly two safe things:

1. **Fitting closed-form parameters on frozen data** — M1 curves, M2 base rates, M5 surprise distributions, M4 weights, and the M3 **aggregator** knobs (k, τ, cap). All of these are pure arithmetic over numbers already sitting in the database (prices, outcomes, or — for M3 — the structured evidence objects `{direction, strength, reliability, relevance, published_ts}` extracted at forecast time and frozen in `evidence_runs`). No LLM call happens during any of these refits, so there is no channel for the model's post-hoc knowledge to leak in. This is safe on the same footing as M1/M2/M5.
2. **Forward-only challenger registration** — a new M3 extraction prompt, or a new `m3b_direct` variant, registers as a new `model_id` (`m3_evidence@v2`) with a `registered_ts` in `model_versions`. It earns forecasts, and skill, only from markets it forecasts *after* that timestamp. It is never scored against, or backtested on, history that predates its own existence — doing so would require the LLM to re-read old news today, when today's model may already know how those old questions resolved.

**Rule of thumb for any future change to this system:** if it requires a *new LLM inference call* on a market that has already resolved, it is forbidden, full stop. If it only requires re-doing arithmetic on numbers already in the database, it is a normal refit.

**Safeguards (so one bad month can't corrupt a good model):**
- **Walk-forward only.** Every refit fits on data up to cutoff T and validates on data after T. A refit function that accepts a single history window with no train/validation split is a bug — `tests/test_learning_safety.py` asserts the split exists on every refit path.
- **Bounded step per cycle.** No refit may move a live parameter (recalibration slope, ensemble weight, aggregator k/τ/cap) by more than `max_step_pct` (config, default 20% relative) in one monthly cycle. A refit wanting to move further logs the full proposed change and applies only the capped step; the next cycle continues the move if the evidence still supports it. One noisy month becomes a slow lean, not a lurch.
- **Promotion requires a confidence interval, not a point estimate.** A challenger is promoted only when its measured skill beats the champion's with the interval excluding zero, reusing `eval/scoring.py` rather than a bespoke metric. From Phase 11 onward that interval is the **anytime-valid confidence sequence** (§7), which stays valid under repeated looks; the fixed-n bootstrap CI remains a descriptive statistic.
- **Automatic rollback — the actual safety valve.** After promotion, the new champion keeps being scored forward like anything else. If its trailing skill over the next `rollback_window` resolved forecasts (config, default 50) falls below the retired champion's historical skill under the same CI test, `lab learn` reverts the active pointer automatically and records `retired_reason='rollback'`. Learning that turns out to hurt undoes itself instead of relying solely on the entry gate having been right.
- **Append-only registry.** Every parameter set or prompt gets a new row in `model_versions`, never edited — but the row points at its artifact via `artifact_path` rather than duplicating it; the artifact itself (curve coefficients, base rates, prompt text) lives in `data/models/*.json` exactly as it has since Phase 2. Rollback means repointing `is_active` at a previous row, not recomputing and hoping.
- **One kill switch.** `lab learn` refuses to run while `data/PAUSE` exists — the same file the collector already respects (guardrail 8, §9). No second switch to remember.
- **Dry-run by default.** `lab learn` always produces a diff report first — what would change, by how much, on what n, for which models — and only writes to `model_versions` with an explicit `--apply` flag. Every learning cycle is a reviewable event, not a silent mutation.

**Post-mortems:** monthly, for the top decile of misses and of wins among resolved forecasts, the LLM produces a structured analysis (error source: evidence / weighting / resolution-criteria reading / category / horizon) stored in `postmortems`; the report carries a quarterly lessons digest. Lessons feed *versioned* changes a human decides to make — never an automatic parameter nudge.

---

## 7. Evaluation protocol (the heart of the system)

- **Freeze semantics.** A forecast is scored exactly as written at `ts`, against `p_market_at_ts` captured in the same row. No retroactive edits — enforced by an append-only writer and a test.
- **Scoring at resolution.** For resolved markets: Brier `(p − y)²` and log loss (with clamped p). Compute for the model and for `p_market_at_ts` on the *same rows* (paired).
- **Skill.** `skill = mean(brier_market − brier_model)` over paired rows, computed per venue against that venue's own price. Positive = beating the market. Report with a **cluster bootstrap CI resampling by `event_id`** (falling back to `condition_id` where no event mapping exists): forecasts on the same market are correlated, and the same underlying event listed on several venues is still ONE observation of the world — clustering by venue-market would overstate n. Naive CIs would lie.
- **Calibration.** Reliability diagrams (10 bins) per model, plus per-category breakdown once n allows.
- **CLV-style early signal** (doesn't need resolution): for each forecast, measure whether the market price at t+24h / t+72h moved toward the model's view. Report mean signed drift in the direction of the model's disagreement. This detects information timing months before enough markets resolve.
- **Honesty thresholds & statistical power.** The report displays n everywhere; from Phase 11 onward n counts resolved **event clusters**, not venue-market rows. Tiers per venue × category: n < 200 → "INSUFFICIENT DATA"; 200 ≤ n < 500 → "PRELIMINARY"; n ≥ 500 → standard claim. Additionally the report computes the **minimum detectable effect** at current n from the empirical sd of per-cluster paired Brier differences (MDE ≈ 2.8 · σ_d / √n, for 80% power at α = 0.05) and prints it next to every skill number — a skill estimate smaller than its own MDE is noise by construction. No cherry-picked windows: all-time and trailing-90-days only.
- **Anytime-valid monitoring (v1.9, citation corrected v2.7).** Alongside the bootstrap CI, compute a time-uniform confidence sequence — the normal-mixture uniform boundary of Howard, Ramdas, McAuliffe & Sekhon (2021, *Annals of Statistics* 49(2):1055-1080, arXiv:1810.08240), applied with a plug-in running sample variance (an asymptotic variant, this project's own deliberate choice — not how the cited paper itself is framed) — for the mean paired Brier difference. This is deliberately distinct from Waudby-Smith & Ramdas (2020)'s own nonasymptotic betting-based CS, which this project does not implement (see `src/lab/eval/anytime.py`'s docstring for the full reasoning). Nightly reports may be read daily without alpha decay **only** through this interval: promotions, rollbacks, and public skill claims must cite the confidence sequence; the fixed-n CI stays for monthly descriptive snapshots. (§6's wealth ledger is a related but distinct betting-theoretic construction, not a duplicate or an alternate form of this same computation — see there for why the two stay implemented separately.)
- **Precision-weighted stratified estimator (v2.1 — replaces the v1.9 control-variate check, which was mathematically vacuous).** The original design centered its covariate at its own in-sample mean, which forces the correction term to zero regardless of β: the "corrected" estimate wasn't merely same-signed, it was numerically identical to the raw mean, so the "agree in sign" check could never fail. The fix: stratify resolved forecasts into price buckets on `p_market_at_ts` (5–7 fixed bins, e.g. [0,0.05), [0.05,0.2), ..., [0.95,1]) — chosen because Brier-difference variance is driven directly by price level (Bernoulli variance ≈ `p(1−p)`), so price buckets capture real, independently-known heterogeneity rather than a circularly-defined one. Within each stratum with `n_s ≥ 30`, compute `d̄_s` and its variance; pool via inverse-variance weights: `skill_pw = Σ w_s·d̄_s / Σ w_s`, `w_s = 1/Var(d̄_s)`. Under homogeneous variance across strata this collapses exactly to the raw pooled mean (a required unit-test invariant); it only diverges — meaningfully — when variance is genuinely heterogeneous, which is exactly the condition price buckets are chosen to capture. A claim requires the primary anytime-valid CS AND `skill_pw`'s own bootstrap CI to both exclude zero and agree in direction. Fewer than 3 qualifying strata → report "insufficient data for stratified check," don't compute it on too few cells.
- **Null control.** The sports control sample (§3 universe policy) is scored identically and shown in the same table. Statistically significant "skill" there invalidates the run pending investigation.
- **LLM models are live-only.** Never backtest M3/M3b on markets resolved before the LLM's training cutoff — training-data leakage makes such numbers meaningless. Statistical models (M1/M2/M5/M6) may be backtested; LLM skill accrues only forward. The one exception: the M3 **aggregator's** own parameters (k/τ/cap) may be fit on frozen historical evidence objects, because that fit is arithmetic, not a new LLM call (§6).

---

## 8. Shadow portfolio (simulation only)

Purpose: translate calibration edge into an interpretable number for the deployed ensemble specifically — realistic, entry-filtered, M4-only. (v2.0: the broader `wealth_ledger`, §6, runs the same Kelly math unconditionally across every model for comparison power; this section stays the one "would we actually have traded this" simulation.) Everything labeled `SIMULATION` in code, DB, and reports.

- Bankroll: simulated $10,000.
- Entry rule (evaluated daily on liquid tier, using M4): `edge = |p_model − p_market| ≥ 0.05` AND spread ≤ 0.03 AND top-of-book depth ≥ $500 on the entry side AND `0.05 < p_market < 0.95` (tail entries are oracle-risk bets, not forecasting bets).
- Side: buy YES if `p_model > p_market`, else buy NO (price `1 − mid`).
- Fill model: simulated fill at best ask for the chosen side plus a slippage haircut proportional to (stake / visible depth), capped; parameters in config.
- Sizing: fractional Kelly at 0.2× with per-market cap 5% and per-category cap 20% of bankroll.
- Exit: hold to resolution (v1). Mark-to-market daily for the open book.
- Report: realized sim P&L (resolved only) separately from unrealized; max drawdown; hit rate; comparison vs "bet nothing" and vs "always take market side".

---

## 9. Engineering guardrails

Derived from the Karpathy guidelines — they are binding for this project:

1. **Think before coding.** If this brief is ambiguous somewhere, state the assumption in a code comment and in the phase summary — don't silently pick.
2. **Simplicity first.** Minimum code that satisfies the phase's acceptance criteria. No speculative abstraction, no plugin systems beyond `NewsProvider`, no config options nobody asked for. If a module exceeds ~300 lines, justify or split.
3. **Surgical changes.** Later phases must not rewrite earlier phases unless an acceptance criterion breaks.
4. **Goal-driven execution.** Every phase ends with its acceptance checks actually executed, not assumed.

Domain-specific guardrails:

5. **The scope guard.** `tests/test_scope.py` greps `src/` and fails if it finds: imports of `web3`, `eth_account`, `py_clob_client`; the strings `private_key`, `POST /order`, `create_order`, `eip712` (case-insensitive). This is not a usage restriction — the MIT license grants forks every freedom, and removing this test is one commit. Its job is to keep the canonical repository exactly what it claims to be (a measurement instrument) and to prevent execution code from entering the maintainer's publication via contributions or automation, while the maintainer operates from a jurisdiction where Polymarket does not accept traders. Runs in every phase.
6. **UTC everywhere.** All timestamps ISO-8601 UTC. A helper `now_utc()` is the only clock call allowed.
7. **Idempotent collectors.** Restart-safe: universe sync upserts; snapshot writer dedups on (ts_bucket, condition_id); resolution watcher is at-least-once with idempotent writes.
8. **Rate limiting.** One global async token-bucket per venue host (default: 5 req/s, burst 10 for Polymarket; Kalshi/Metaculus/Manifold per their own documented limits, §3). This guardrail was Polymarket-only wording before multi-venue collection existed (Phase 10) — generalized here rather than left stale. Backoff with jitter on 429/5xx. Respect a kill file (`data/PAUSE`) that halts all polling, across every venue, when present.
9. **Fail soft, log loud.** Network failures degrade to skip-and-retry; the process never crashes on a single bad market. Structured logging (`logging` + JSON lines to `data/logs/`).
10. **LLM budget.** Hard daily USD cap enforced in code before each call; spend persisted; the report shows cumulative cost.
11. **No look-ahead.** Every M3 evidence item must satisfy `published_ts ≤ forecast ts`; the dossier stores both timestamps. LLM models are never scored on pre-cutoff history (§7).
12. **No selection bias.** Cheap models (M0/M1/M2/M6, and M5 where its adapters apply) forecast the ENTIRE eligible universe daily. Any subsetting (M3 cost cap) follows the deterministic rule in §6 — never editorial judgment about which markets look "forecastable". M1.x and M7 are naturally scoped to the venues or matched-questions they structurally cover — a structural boundary, not an editorial one — same category as M5's carve-out, not a new exception to track. Pre-specified, seeded randomization (Phase 15's M3 boundary experiment) counts as a deterministic rule for this guardrail's purposes: the prohibition targets editorial judgment, and a logged coin flip contains none.
13. **Price freshness.** A forecast row requires `p_market_at_ts` from a snapshot no older than 15 min (liquid tier) / 90 min (tail). If the latest snapshot is stale, skip the forecast and log it — a forecast paired against a stale price corrupts the skill comparison silently, which is the worst failure class in this system.
14. **Self-modification is scheduled and versioned.** Parameters and prompts change only via `lab learn` — on its monthly schedule (§6; a BOCPD-triggered off-schedule run remains a specified but unbuilt future trigger under identical rules, not a live path — see §6's v2.10 correction) — only when min-n thresholds are met, only via walk-forward fitting, and only as challenger versions measured against the incumbent (§6). No code path may adjust any model in response to an individual outcome.
15. **Learning never re-invokes the LLM on resolved history.** Refits touching M3 are arithmetic over evidence already frozen in `evidence_runs`; new prompts/extraction logic earn skill only from forecasts made after their own `registered_ts`. Full safeguard list (bounded step, CI-gated promotion, automatic rollback, dry-run default) lives in §6 — this rule is the one that may never be relaxed for convenience.
16. **Venue trust & provenance.** Every external observation carries its venue. Manifold (play money) is excluded from the M7 pool, from M1.x fits, and from all skill claims — it feeds event mapping and M2 base rates only. Metaculus community predictions hidden by tournament windows are recorded as NULL, never imputed. Historical archives (GJP, PredictIt, the HF dataset) feed M2 base rates only, provenance-tagged — never M1.x fits for venues we don't collect live, never skill claims.
17. **Ensemble-weight MWU is a narrow, explicit exception to guardrail 14.** The wealth-based `m4_ensemble@mwu` challenger (§6, Phase 14.1) may update its own per-category weights on every new resolution, in shadow only, because: (a) it touches only meta-level ensemble weights, never any model's internal parameters; (b) its update is a provably regret-bounded algorithm (MWU/Hedge) with a floor and ceiling, not an ad hoc reaction to one outcome; (c) it never affects production forecasts until it clears the same CI-gated promotion as any other challenger. Guardrail 14 remains unqualified for every model-internal parameter — M1.x curves, M3 aggregator knobs, M5 error distributions stay strictly monthly-batch, no exception.
18. **First activation without an incumbent is not a guardrail-14 violation.** `m1_hier@kalshi` (§6 M1.x) and any future model's first activation for a venue or context where nothing existed before still register in `model_versions` with a `registered_ts` and fit window, for audit purposes — but clear no promotion contest, because there is nothing yet to beat. Guardrail 14's "measured against the incumbent" binds from the SECOND version of that `model_id` onward; the first is a documented bootstrap, not a loophole for skipping the gate later.

---

## 10. Implementation phases

### Phase 0 — Scaffold
Tasks: repo init, `uv` project, `pyproject.toml`, `LICENSE` (MIT), `config.yaml` with documented defaults, `.env.example`, `typer` CLI skeleton (`lab sync`, `lab collect`, `lab forecast`, `lab eval`, `lab report`, `lab shadow`, `lab export`, `lab status`, `lab learn`), logging setup, `test_scope.py`, README quickstart + "Downstream use & license" section (§13).
**Accept when:** `uv run lab --help` works; `uv run pytest` green (includes the tripwire test).

### Phase 1 — Collection
Tasks: Gamma client + universe sync with tiering (config thresholds on liquidity/volume; binary markets only; skip sub-24h crypto "pulse" markets by default); CLOB client + snapshot loop (5 min liquid / 60 min tail) writing Parquet; resolution watcher polling closed markets → `resolutions` (respect dispute flag: only record when Gamma marks final payout); `lab status` implementation: last-snapshot age per tier, snapshot gaps over trailing 24h/7d, resolution-watcher lag, ledger row counts, today's LLM spend.
**Accept when:** after a 1-hour live run: ≥50 liquid markets tracked; Parquet contains multiple snapshot rounds; process restart produces no duplicate rows; universe re-sync updates `last_synced_ts` without churn; PAUSE file halts polling within one cycle; `lab status` reports correct freshness and flags a synthetic gap in fixture data.

### Phase 2 — Historical bootstrap & debiasing
Tasks: downloader for `quant.parquet` + `markets.parquet` from `SII-WANGZJ/Polymarket_data` (§3 has the exact fetch code); lazy-filter to resolved binary markets with prices at multiple horizons + outcomes; fit M1 logistic recalibration per horizon bucket (expect slope > 1 at long horizons; verify the tail-bias direction empirically — recent Polymarket data shows a *reversed* favorite-longshot bias) and M2 category base rates; persist versioned curve artifacts under `data/models/`; plot calibration-slope-by-horizon and price-vs-outcome curves into `reports/`.
**Accept when:** curve artifacts exist and load; unit test asserts monotonicity of each recalibration; the report artifact shows fitted curves per horizon bucket with sample sizes per bin.

### Phase 3 — Ledger, baseline models, evaluation
Tasks: append-only forecast writer; M0/M1/M2 wired to daily cadence; `eval/scoring.py` with paired Brier/log-loss + cluster bootstrap; calibration plots; nightly `lab eval && lab report` producing static HTML; `lab export` — latest forecast per (market, model) plus market metadata as JSONL to stdout or file (the downstream integration point, §13).
**Accept when:** scoring functions pass unit tests on hand-computed fixtures (including the paired-skill sign convention and bootstrap clustering); report renders on synthetic fixture data AND on whatever real data has accumulated; forecast writer raises on any UPDATE attempt; `lab export` emits schema-valid JSONL that a test stub parses back losslessly.

### Phase 4 — Evidence pipeline (M3)
Tasks: `NewsProvider` (Google News RSS + config feed list; NewsAPI optional), dossier builder, LLM extraction with strict-JSON retry, deterministic aggregator (unit-tested: caps, decay, direction signs), cost accounting + daily cap, `evidence_runs` persistence, optional `m3b_direct`.
**Accept when:** for 10 liquid markets, an end-to-end run produces evidence rows and M3 forecasts in the ledger; aggregator tests pass; simulated cost-cap breach cleanly skips remaining markets with a log line; a stored dossier is human-readable enough to audit one forecast start-to-finish.

### Phase 5 — Structural models & consistency (M5, M6)
Tasks: weather adapter using the Open-Meteo **Ensemble API** (not plain forecast — §3) → threshold probabilities from ensemble spread, for whatever weather markets exist in the universe; macro adapter using **FRED** (`GDPNOW`, `PCENOW` series) → bucket probabilities via a historical-surprise error distribution; if a stable Cleveland Fed inflation-nowcast endpoint is found at implementation time, add it as a second macro sub-adapter, otherwise skip it and note why; negRisk coherence scanner with deviation logging; curated `links.yaml` (empty at start) for pairwise logical constraints.
**Accept when:** for at least one live weather market and one live macro market (fixtures if none live), M5 writes forecasts with a stored input trace; M6 flags a synthetic incoherent negRisk fixture and stays silent on a coherent one; both models appear in the nightly eval.

### Phase 6 — Ensemble, shadow portfolio, weekly report
Tasks: M4 with rolling-window weight fit (equal weights until n≥100); shadow portfolio engine per §8; weekly report section: skill table with CIs per model, calibration grid, CLV drift, sim P&L (clearly labeled SIMULATION), LLM spend.
**Accept when:** shadow entries occur only when all filters pass (test with synthetic book data); portfolio math unit-tested (Kelly fraction, caps, slippage haircut); weekly report renders with all sections and honest "INSUFFICIENT DATA" placeholders where n is low.

### Phase 7 — Learning loop
Tasks: `lab learn` command; consolidated scheduled refits (M1/M2/M5 artifacts, M4 weights); M3 aggregator walk-forward fitter gated on ≥150 resolved M3 forecasts; challenger registration and promotion mechanics per §6; post-mortem generator (top-decile misses and wins per month, structured JSON, inside the LLM budget cap) + quarterly lessons digest wired into the report.
**Accept when:** refits produce versioned artifacts without touching live ones until promotion; on fixture data, a synthetic challenger with better out-of-sample scores gets promoted and a worse one does not; post-mortems generate on fixtures and render in the report; `test_scope.py` still green.

### Phase 7.1 — Learning-loop hardening (retrofit)
Phase 7 already exists in this repository. This phase audits it against the safeguards in §6 and retrofits what's missing — it does not rebuild Phase 7 from scratch.
Tasks: add the `model_versions` table (schema migration) as a versioning layer that **coexists** with the existing `data/models/*.json` artifact files from Phase 2 — the table owns active/promoted/retired state and points at artifacts via `artifact_path`, it does not duplicate their contents; `data/models/ACTIVE.json` becomes a generated pointer written by `registry.py`, never edited by hand; no changes to how M0–M6 read their active artifact at inference time. Add `learn/registry.py` (versioned writes, single active pointer per `model_id`, rollback); make walk-forward train/validation split a structural requirement in `refit.py` — a refit call with no validation window should raise, not silently fit on full history; add `max_step_pct` bounding to every refit before it writes; confirm promotion goes through the CI-exclusion test in `eval/scoring.py` rather than a point-estimate comparison, and fix it if it doesn't; implement the rollback circuit breaker as a check `lab learn` runs before any new refit each month; make `lab learn` dry-run by default with output-only diffs, gated behind an explicit `--apply` flag; add `lab rollback <model_id>` for manual override outside the monthly cycle; audit the `m3b_direct` / M3 prompt-challenger code path specifically and confirm every challenger carries a `registered_ts` and is never scored on forecasts predating it.
Also write `tests/test_learning_safety.py` covering: (a) a refit call missing a validation window raises; (b) a synthetic large-jump scenario gets clamped to `max_step_pct`; (c) a challenger with a better point estimate but a CI that includes zero is NOT promoted; (d) a promoted challenger whose simulated subsequent performance degrades triggers automatic rollback and `retired_reason='rollback'` is recorded.
**Accept when:** `test_learning_safety.py` passes, all four fixtures included; a real `lab learn` run against accumulated data produces a dry-run diff and writes nothing until `--apply`; `lab rollback` demonstrably restores a prior `model_versions` row as active; `test_scope.py` still green.

### Phase 8 — Optional dashboard
Streamlit app reading the same SQLite/Parquet: live universe, latest forecasts vs market, calibration, shadow book, and — once Phase 14 exists — the wealth-economy visuals (equity curves, drawdown, null-control band, attribution) as interactive views of what `lab report` already renders statically. Only after Phase 6 is stable; the wealth-economy views additionally require Phase 14.

### Phase 9 — Optional: cross-venue signal (M7)
Only after Phase 6 is stable; priority categories only. Tasks: thin read-only clients for Kalshi public market data and the Metaculus API (§3 — no accounts, no keys, same global rate limiter); `data/markets_map.yaml` with a propose-then-confirm matching flow (LLM proposes candidate pairs, a human confirms, the file is the source of truth); M7 per §6 wired into the ledger and the M4 weight fit; external-price snapshots stored alongside our own.
**Accept when:** on ≥5 confirmed matched pairs (fixtures acceptable), M7 writes forecasts with stored external-price traces; a proposed-but-unconfirmed pair is NOT forecast; M7 appears in the nightly eval and in M4's weight fit; `test_scope.py` still green.

### Phase 10 — Multi-venue collection foundation
Supersedes the client sub-tasks of Phase 9 (if Phase 9 is unbuilt, M7 now depends on this phase instead). **Calendar priority like Phase 1: external snapshot history is just as unrecoverable — get this collecting, then build Phases 11–13 while it accumulates.**
Tasks: schema migration per §5 (venues seed rows, `events` table, ALTER on markets, `venue` column in Parquet; synthesized `{venue}:{native_id}` keys — zero FK churn); Kalshi collector (universe sync, snapshots, resolution watcher on settled markets); Metaculus collector (community-prediction snapshots into `mid`, NULL when hidden, resolution watcher; `METACULUS_API_KEY` from `.env`); Manifold collector (markets + resolutions only, guardrail 16); per-host rate limiters behind the global budget; `lab status` gains per-venue freshness/gap lines; the markets_map.yaml propose-then-confirm flow (reused from Phase 9 or implemented here) now also mints `event_id`s on confirmation.
**Accept when:** a 1-hour live run shows snapshots from ≥2 external venues; restart produces no duplicates; a confirmed match creates an event linking ≥2 venue-markets; `lab status` reports per-venue health; a hidden Metaculus CP is stored as NULL on a fixture; `test_scope.py` still green.

### Phase 11 — Measurement upgrade (correctness before new models)
Tasks: event-level cluster bootstrap in `eval/scoring.py` (resample by `event_id`, fallback `condition_id`); per-venue paired skill tables (each forecast scored against its own venue's price; forecastable venues read from the `venues` table); anytime-valid confidence sequence per §7 wired into the report and into the promotion/rollback paths; precision-weighted stratified skill estimator per §7 as the secondary column set (`skill_pw` + CI + `n_strata_pw` in `eval_runs`) — if this phase or an earlier draft already implemented the v1.9 control-variate design, replace it; nothing else in Phase 11 changes; the report becomes a venue × category matrix with per-venue honesty tiers; extend the sports null control to every forecastable venue.
**Accept when:** a synthetic fixture with cross-venue-correlated outcomes yields a WIDER event-clustered CI than market-clustered (test asserts it); on simulated null data the confidence sequence covers zero across all look times in ≥95% of runs; promotion/rollback code paths demonstrably consult the CS; a fixture with homogeneous within-stratum variance makes `skill_pw` equal the raw pooled skill (the non-degeneracy invariant from §7 — this test would have caught the original bug); a fixture with heterogeneous variance makes `skill_pw` diverge from the raw skill; fewer than 3 qualifying strata correctly yields NULL/"insufficient data" rather than a computed value; the report renders the matrix with both estimators.

### Phase 12 — Hierarchical recalibration (M1.x)
Tasks: `m1_hier` per §6 (penalized logistic, global + ridge-shrunk venue offsets per horizon bucket, scipy); fitted inside `lab learn` under walk-forward and bounded-step rules; register the shared `m1_hier_curves` artifact (self-contested, per §6's implementation note) and wire `m1_hier@polymarket`/`m1_hier@kalshi`/`m1_hier@metaculus` as the three venue-labeled Forecaster instances reading it; curves versioned through the §7.1 registry (`artifact_path` files like everything else).
**Accept when:** on synthetic multi-venue fixtures, a small-n venue's curve shrinks toward the global and a large-n venue's diverges when its data demands it (test asserts both); a refit without a validation window raises; the challenger is registered without touching the incumbent; `test_scope.py` still green.

### Phase 13 — Aggregation upgrade (extremized, correlation-aware pooling)
Tasks: per-category extremization exponent in M4 and in M7's external pool per §6 (`a ∈ [1.0, 2.5]`, fitted in `lab learn` under bounded-step + challenger rules); ρ̄ estimation from historical cross-venue logit correlations on matched events; n_eff discount wired into the exponent.
**Accept when:** unit tests confirm `a = 1.0` reproduces current pooling exactly; duplicating a source (ρ̄ → 1) drives n_eff → 1 and suppresses extra extremization; fitted exponents appear in the report with their n; challenger mechanics respected.

### Phase 14 — Virtual prediction economy: wealth ledger
Tasks: `wealth_ledger` table (§5); `economy/wealth.py` computing the Kelly log-wealth delta on every resolved forecast from every model (unconditional on §8's entry filter), accumulated per (model_id, category), wired into the existing nightly `lab eval` step — no new CLI command; sleeping-expert normalization (`cum_log_wealth / n_forecasts`) surfaced in the report; report additions: per-model per-category cumulative log-wealth curves (log scale), drawdown, a null-control reference band (the sports null-control model's wealth path as the zero-skill benchmark), bootstrap wealth bands (resample forecast order); linear log-odds P&L attribution for M4 (`contribution_i = w_i · logit(p_i)` — exact, no Shapley sampling needed given the pool is already linear in log-odds).
**Accept when:** on fixture data, a model that never forecasts a category shows no wealth change for it (sleeping-expert correctness); the coverage-normalized metric ranks a high-coverage mediocre model correctly against a low-coverage sharp one; the null-control band renders alongside real model curves; `test_scope.py` still green (no execution surface added).

### Phase 14.1 — Shadow MWU ensemble weighting
Depends on Phase 14, Phase 7.1 (registry/promotion machinery), and Phase 11 (anytime-valid CS used for promotion). Experimental and probationary by design — read §6's wealth-ledger paragraph and guardrail 17 (§9) before starting.
Tasks: `economy/mwu.py` implementing the Hedge/MWU update with the `η_t = √(8 ln N / t)` schedule, 2%/60% floor/ceiling, and the existing n_eff correlation discount (§6 M4 paragraph) applied before normalization; register `m4_ensemble@mwu` as a challenger via the existing registry (§7.1) with `registered_ts`; compute its shadow weights nightly inside the existing `lab eval` step — no new CLI command, and this is the one process in the codebase permitted to update between `lab learn` cycles, solely because it touches ensemble weights and never model internals (guardrail 17); wire its promotion through the standard anytime-valid CI gate and rollback circuit breaker, with a minimum 90-day / n≥200-per-category probation (§7's honesty tier) before it is even eligible for promotion consideration.
**Accept when:** on a synthetic fixture, the MWU weights provably respect the floor/ceiling under an adversarial win/loss sequence; duplicating a high-wealth model (ρ̄→1) does not let the pair jointly exceed the ceiling; the challenger is invisible to production forecasts until promoted; a promoted MWU weighting that subsequently underperforms triggers the same automatic rollback as any other challenger; `test_learning_safety.py` gains a case for this challenger; `test_scope.py` still green.

### Phase 15 — Publication instrumentation (collect now, write later)
Purpose: make the lab's data paper-grade from today — live pre-registered evidence cannot be added retroactively. Target literature: prediction-market efficiency and forecast aggregation (International Journal of Forecasting class). None of this changes any model's behavior except the optional randomization item.
Tasks:
- **Ledger commitment:** a nightly job computes sha256 over the day's appended `forecasts` rows and commits (hash, row count, date) to a `ledger_commitments` file in the public GitHub repo — cryptographically verifiable pre-registration. Optionally also anchor the hash via OpenTimestamps.
- **Pre-analysis plan:** `docs/pre_analysis_plan.md`, dated and committed BEFORE the confirmatory window opens: primary hypotheses (long-horizon underconfidence on Polymarket; recalibration skill net of costs in P1/P2; cross-venue lead-lag), primary outcome (paired Brier skill with event-clustered anytime-valid CS), exclusion rules.
- **Microstructure covariates in the ledger:** at forecast ts, persist spread, top-of-book depth, 24h volume, 24h trade count, and hour-of-day UTC into forecast rows (new nullable columns) so heterogeneity analysis needs no ex-post reconstruction.
- **Net-of-cost accounting:** a versioned fee-schedule file per venue (fees change — record when); shadow portfolio logs effective spread paid per simulated fill; the report gains a net-of-cost skill line.
- **Universe exclusion log:** `universe_log` table — every excluded market with a reason code and date; daily inclusion/exclusion counts in the report.
- **Crowd-size covariates:** Metaculus forecaster counts, Polymarket holder counts (Data API), Kalshi open interest — stored with snapshots.
- **M3 boundary randomization (optional, high value):** markets ranked K−10..K+10 by the deterministic liquidity rule are randomly assigned to M3 coverage with a logged seed — a built-in experiment identifying the LLM pipeline's marginal contribution causally (guardrail 12, as amended, applies).
- **Replication export:** `lab export --paper` — anonymized resolved-forecast dataset + code version hash + schema documentation.
**Accept when:** a nightly commitment appears in the repo and re-verifies against the DB; the PAP is committed with a date; covariate columns populate on live forecasts; the exclusion log fills on a real universe sync; the randomization assignment is exactly reproducible from its logged seed; the `--paper` export round-trips through a validation script; `test_scope.py` still green.

### Phase 16 — Distributional scoring (RPS/CRPS) for bucketed events
Rationale: many Kalshi and Polymarket macro/weather "markets" are one numeric question split into mutually exclusive buckets (CPI ranges, temperature bands). The lab currently scores each bucket as an isolated binary, discarding cross-bucket structure. Scoring the implied distribution extracts far more information per resolved event — a continuous score instead of one bit — which is the cheapest remaining power gain: same events, more signal, no waiting. M5 already produces distributions natively; M0/M1 imply them from the bucket price vector.
Tasks: an event-distribution layer grouping bucket markets that belong to one numeric question (reuse the negRisk/`events` structures and the markets_map flow for cross-venue cases); per-model implied CDF over buckets (renormalized; M6's coherence deviation logged as a covariate); RPS computation in `eval/scoring.py` with the same pairing-vs-market, event clustering, and anytime-valid machinery as Brier; `eval_runs` gains nullable `rps`, `rps_market` columns via migration; the report gains a distributional-skill section for categories with ≥20 resolved bucketed events. Binary Brier remains the primary pre-registered outcome (the PAP is unchanged) — RPS is a secondary outcome, declared as such.
**Accept when:** hand-computed RPS fixtures pass, including the identity that a two-bucket event's RPS reduces to the Brier score; a fixture where a model nails the shape of the distribution but misses the realized bucket scores better on RPS than a lucky-spike model (the whole point of the upgrade); pairing/clustering reuse is proven by tests rather than assumed; `test_scope.py` still green.

### Phase 17 — Observation-quality pack
Five small, independent upgrades to raw-data quality; each lands as its own commit.
Tasks:
- **Stable internal category taxonomy:** a versioned `categories.yaml` enum + mapping from venue-native tags (Gamma event tags drift; Kalshi series don't align with Polymarket tags); remaps are logged, and all per-category fits/weights key on the internal enum only — silent population mixing across a tag rename is the failure this prevents.
- **Depth-based tiering:** the liquidity tier and liquidity covariates switch from volume-weighted to order-book-depth-weighted (top-10 levels are already collected); volume stays available as a covariate but is documented as contaminated (a substantial share of historical Polymarket volume is wash trading, concentrated in sports).
- **Matched-event high-frequency capture:** for confirmed cross-venue event pairs only, snapshot cadence drops to 1 min (config; measured 2026-08-09 to take just over a minute per round at 147 confirmed pairs, so the live value is 2 -- the binding constraint is guardrail 8's rate limiter, not the scheduler) on both legs — the lead-lag hypothesis (PAP H3) is underpowered on a 5-minute grid, and finer history cannot be captured retroactively.
- **CLV validity diagnostic:** a standing report check — correlation of price-drift (the CLV proxy) with realized resolution skill on the sports null control; if the null control shows CLV "skill," the CLV metric is flagged untrusted lab-wide until investigated.
- **Gap-aware derived metrics:** CLV/drift windows overlapping a recorded collection gap are excluded and counted; the report shows how many windows were dropped and why.
**Accept when:** a taxonomy remap on a fixture leaves per-category fit keys unchanged; tier assignment provably uses depth on a fixture where volume and depth disagree; HF cadence activates only on confirmed pairs and stays inside rate limits; the CLV diagnostic renders with the null control; gap exclusion is counted on a fixture with a synthetic outage; `test_scope.py` still green.

### Phase 18 — Operations hardening (final)
No model work. Three items closing the gap between "built" and "safely unattended" — the operator travels for weeks at a time, and the system must survive without a keyboard.
Tasks:
- **Dead-man heartbeat:** the collector loop and the nightly backup job emit an outbound HTTPS heartbeat to a free monitoring endpoint (healthchecks.io-class; URL in `.env` as `HEARTBEAT_URL`, absent = feature silently off). If heartbeats stop, the external service alerts the operator by email — our code never notifies anyone itself (§12 carve-out). This directly addresses §11's named worst failure: the collector dying silently.
- **Operator runbook:** `docs/OPERATIONS.md` — what runs where (host, service/unit names), cold-start restart procedure, the PAUSE file, backup location and restore procedure, key inventory with rotation steps (LLM provider, FRED, Metaculus, heartbeat URL), and what each `lab status` red flag means and what to do about it.
- **Backup-restore drill:** one-time now, then quarterly per the runbook: restore `data/` from the private backup repo onto a clean checkout, run `lab status` and the full test suite against the restored state, record the date in the runbook. An untested backup is a hope, not a backup.
**Accept when:** killing the collector demonstrably stops heartbeats and the monitoring service registers the lapse (manual check, documented in the runbook); a cold-start restart succeeds by following `OPERATIONS.md` literally, with any missing step fixed in the doc rather than improvised; the restore drill has been executed once with its date recorded; `test_scope.py` still green.

---

## 11. Operations

- Run collection under tmux or a systemd user unit: `uv run lab collect`. Target host: an always-on Linux box.
- **Data health is a first-class concern.** The nightly report opens with the `lab status` health block (freshness, gaps, watcher lag, spend). The single worst operational failure is the collector dying silently — snapshot history is unrecoverable.
- **Timeline expectations.** Weather markets and the sports null-control resolve in days → first calibration stats in ~2–4 weeks. Macro releases: weeks. Long-horizon politics: months. The n ≥ 500 "standard claim" tier realistically arrives at month 3–6 depending on category mix — do not read tea leaves earlier. Multi-venue (v1.9): Kalshi's daily weather/econ resolutions multiply resolved-event counts in P1/P2 severalfold — expect the standard-claim tier in those categories roughly twice as early; Metaculus adds an independent signal, not short-term n.
- Nightly cron: `lab forecast && lab eval && lab report`. Weekly: `lab shadow report`. Monthly: `lab learn`.
- Disk: snapshots at default cadence are small (est. < 200 MB/month at 200 markets); prune policy configurable but default keep-everything.
- Backup: `data/lab.db` + `data/snapshots/` are the crown jewels — the historical order-book snapshots cannot be re-downloaded later. rsync them somewhere daily from day one.
- Success reviews: after ~200 resolved paired forecasts, read the skill table. Positive skill with CI excluding zero on some category → that category is a candidate edge worth deeper study. No skill anywhere → the lab has paid for itself by preventing a doomed trading build.

## 12. Explicitly out of scope (do not build)

Order execution or anything touching wallets/keys (belongs in downstream projects — §13); VPN/proxy logic; Betfair (requires an account); Kalshi *trading* endpoints — its read-only market data is in scope via Phases 9–10; ALL crypto/equity price-target markets, not just sub-24h pulses; a database server; user auth; Docker (plain `uv` is fine); notification bots — with one carve-out: the passive dead-man heartbeat of Phase 18 is not one (our code only emits an outbound ping; the external monitoring service does the notifying). If a task seems to require any of these, stop and flag it instead.

---

## 13. Open-source model & downstream use

This project is published for anyone to analyze, improve, learn from, and build on — including commercially.

- **License: MIT** (`LICENSE` created in Phase 0). No usage restrictions of any kind: commercial use, forks, closed-source derivatives, and execution layers built on top are all permitted. The standard MIT warranty disclaimer applies; downstream users are responsible for compliance in their own jurisdictions.
- **The forecast contract is the public API.** The SQLite schema (§5) and Parquet layout are a stable interface: any breaking change bumps a schema version stored in a `meta` table and is noted in the changelog. Downstream code may read the DB and Parquet directly.
- **`lab export` is the integration point.** Latest forecast per (market, model) with market metadata, as JSONL. Any external consumer — analytics, dashboards, execution layers — plugs in here without touching lab internals.
- **Extension pattern.** An execution layer is a separate package or repo that consumes `lab export` (or the DB) and implements its own order logic, risk, and compliance. This repository defines the boundary and keeps its side of it; everything past the boundary belongs to downstream authors — their design, their responsibility.
- **Contributions.** PRs adding execution code to the core are declined and redirected to the extension pattern. Everything else — models, adapters, data sources, evaluation methods — is welcome.
