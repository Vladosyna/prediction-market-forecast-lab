# Operator Runbook

This is the operator runbook required by CLAUDE.md Phase 18 ("Operations hardening
(final)"). It is written for a tired 3am operator: follow the numbered steps literally.
If a step below does not work exactly as written when you actually run it, **fix this
doc** — don't route around it and don't rely on tribal knowledge instead.

**Retired 2026-07-13. This laptop no longer runs the lab — the VPS
(`docs/VPS_OPERATIONS.md`) is sole primary.** Everything below is kept as the
historical/cold-start record, not a description of this host's current state.

What actually happened at retirement, since the parallel-run window (started
2026-07-10, meant to last "a few days") ran three days longer than intended
and caused two real, concrete problems before anyone noticed: the two hosts'
`lab.db` files had quietly diverged since the VPS-primary cutover (each
generating its own new forecasts independently), which surfaced as (a) two
non-reconciled `docs/ledger_commitments.jsonl` entries for the same calendar
date on three separate days (2026-07-10/11/12 — resolved by keeping both
sides' entries, never silently overwriting either, per the append-only
discipline this file exists to enforce) and (b) this file's own local,
never-commit `publish.enabled: false` override briefly leaking into a real
commit on the shared repo (`26ccd36`, caught and reverted in `6fbbab4` before
the VPS pulled it). Both are now fixed; see `Claude.md` v2.10 and
`docs/VPS_OPERATIONS.md`'s changelog for the full account.

Retirement steps actually taken: (1) final `lab status` comparison against
the VPS, confirming VPS's 24h gap count was 0/0 (liquid/tail) against this
laptop's 145/7 — the always-on server had already become the more reliable
instance, independent of the divergence issue; (2) one last manual raw-data
backup (`uv run python scripts\publish_results.py --raw-data`) — pushed to a
dedicated `laptop-final-snapshot-2026-07-13` branch on the private results
repo rather than `main`, since `main` had already diverged too far from the
VPS's own ongoing pushes to merge a binary DB into sensibly; (3) all four
running processes killed (two duplicate `lab run` orchestrators were found
running simultaneously — one under `.venv`, one under a stray global Python
install; likewise two duplicate Streamlit dashboards); (4) `data\PAUSE`
created as a safety net, since removing the two scheduled tasks below
requires an elevated PowerShell session neither this session nor the
original retirement session had — **an operator still needs to run
`scripts\uninstall-watchdog.ps1` from an admin PowerShell** to stop the
hourly watchdog from resurrecting the orchestrator; until that happens,
delete `data\PAUSE` at your own risk.

---

## What runs where

**Host.** This lab runs on a Windows 11 Pro laptop, hostname `VLADOSYNAPC`, under
`D:\Polymarket`. This is a **deliberate deviation** from CLAUDE.md §11, which assumes
"an always-on Linux box" — there is no Linux box for this deployment. Everything below
(Scheduled Tasks instead of systemd/cron, PowerShell instead of bash) exists because of
that choice, not by oversight.

**Known past incident — sleep froze the collector.** Earlier in this project's life,
Windows Modern Standby (sleep) silently froze the entire asyncio event loop for about 8
hours overnight. This was confirmed by correlating `Get-WinEvent` System log event IDs
506/507 (sleep/wake) exactly against the timestamp of the last collector log line before
the gap. **Fix applied:** AC-power sleep was set to Never (Balanced power scheme,
"Sleep after" = 0) via `powercfg`. Because this is a laptop and Windows updates can
silently reset power plans, **periodically spot-check this hasn't regressed**:

```powershell
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
```

Confirm the AC line reads:

```
Current AC Power Setting Index: 0x00000000
```

If it reads anything else, the laptop can silently sleep again and freeze the collector
exactly like the original incident. Reset it:

```powershell
powercfg /change standby-timeout-ac 0
```

**Services (Windows Scheduled Tasks, not systemd units).** Confirmed live via
`schtasks /Query`, both currently status "Ready":

| Task name | Trigger |
|---|---|
| `PolymarketForecastLabWatchdog` | At user logon |
| `PolymarketForecastLabWatchdogHourly` | Every 1 hour |

Both tasks run `scripts\watchdog-task.bat`, which invokes `scripts\watchdog.ps1`. Every
cycle, the watchdog:

1. Runs `lab guard` (cleans up stale locks / dead processes).
2. Checks whether the orchestrator (`.venv\Scripts\python.exe -m lab run`) is alive, by
   process list and by `data\orchestrator.pid`. Starts it detached if not.
3. Does the same check-and-start for the Streamlit dashboard (port 8501).
4. Logs every action to `data\logs\watchdog.log`.
5. Treats `data\orchestrator.heartbeat` — a local file the orchestrator's main loop
   touches every 60 seconds — as "possibly hung" (logged only, **not** auto-killed) if
   it is older than 45 minutes.

**Do not confuse the two heartbeats.** `data\orchestrator.heartbeat` above is a local
liveness file the watchdog reads on this machine only. The Phase 18 dead-man heartbeat
(`HEARTBEAT_URL`, see below) is a separate, outbound HTTPS ping to an external
monitoring service, so someone gets alerted even if this laptop itself is fully down and
no watchdog is running at all. They serve different failure modes — don't treat one as
a substitute for the other.

Both scheduled tasks, the venv, and all scripts live under `D:\Polymarket`.

**Third scheduled task — pmxt Router scan (M7, out-of-band, separate from everything
above) — DISABLED on this machine since 2026-07-10, ownership moved to the VPS.**
`PolymarketForecastLabPmxtScan` (`Disable-ScheduledTask`, not deleted — re-enable with
`Enable-ScheduledTask -TaskName PolymarketForecastLabPmxtScan` if the VPS is ever
decommissioned) used to run `uv run --with pmxt python scripts\pmxt_router_scan.py`
twice daily (05:00/17:00 local). This is deliberately its own task, not part of the
orchestrator or the watchdog: pmxt is a third-party unified prediction-market
**trading** SDK (Claude.md tech-stack row / §12) — its hosted API key (`PMXT_API_KEY`
in `.env`) can also authorize live trading, so per Claude.md's own rule it must never
be imported into `src/lab` or run by any automated process this repo owns. `uv run
--with pmxt` installs it into an ephemeral environment for just that one invocation —
`pyproject.toml` is never touched, so pmxt never becomes this project's own dependency.

**Why disabled, not left running alongside the VPS's copy (v2.9):** the pmxt scan +
LLM-verify cycle writes to `data\markets_map.yaml`, a single git-tracked file with no
merge strategy — two hosts independently rewriting it would silently drop whichever
side lost the next merge, and the $5/day LLM cap (`llm.daily_cost_cap_usd`) is enforced
per-host against each machine's own `lab.db`, so running it twice would silently double
effective spend to $10/day. See `docs/VPS_OPERATIONS.md`'s pmxt section for the VPS
side, which is now the sole owner of this cycle — `run_pmxt_verify_job` there commits
and pushes `markets_map.yaml` whenever it finds new proposals, so `git pull` on this
laptop is what surfaces them here.

The scan writes `data\pmxt_candidates.json` (raw pairs pmxt's Router thinks might be the
same event across Polymarket and Kalshi). A **second, independent** step —
`verify_pmxt_candidates` (also wired into `lab run`'s own scheduler at
`cross_venue.pmxt_verify_cron`, twice daily, default 06:00/18:00 UTC, though it's a
harmless no-op here now with the scan disabled and no fresh candidates file ever
appearing) — reads that file, runs our own LLM check on each pair (pmxt's own
confidence score is treated as context, not ground truth), and only THEN appends
anything it agrees with into `data\markets_map.yaml`'s `proposed` list. A human still
runs `lab map confirm` (CLI or the dashboard's Cross-Venue Matching mode) before any
pair is ever live — nothing pmxt surfaces is ever auto-confirmed.

Install: `powershell -ExecutionPolicy Bypass -File scripts\install-pmxt-scan-task.ps1`.
Remove: `scripts\uninstall-pmxt-scan-task.ps1`. **First-run caveat (resolved, kept for
history):** the very first live run fired every query term back-to-back with no delay
and about half came back with an empty response body (`Expecting value: line 1 column
1`), interleaved with queries that succeeded normally — consistent with a rate limit on
pmxt's own API. Fixed by pacing queries 1.5s apart (`scripts\pmxt_router_scan.py`); if a
genuine schema problem ever appears instead, it prints a line starting `pmxt schema
mismatch` with the raw object dump needed to fix it.

---

## Cold-start restart procedure

From a clean machine or checkout, with the Python venv already set up (`uv sync` has
been run):

1. Open an elevated or normal PowerShell in the repo root (`D:\Polymarket` or your
   clone path).
2. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
   ```
   This registers both scheduled tasks above **and** immediately runs the watchdog once,
   which starts the orchestrator (and dashboard) if they are not already running.
3. Confirm it worked:
   ```powershell
   schtasks /Query /TN PolymarketForecastLabWatchdog
   schtasks /Query /TN PolymarketForecastLabWatchdogHourly
   uv run lab status
   ```

**Manual start, without the scheduler:**

- Foreground orchestrator (blocks the terminal, good for debugging):
  ```powershell
  uv run lab run
  ```
- Supervised orchestrator (auto-restarts on crash after a delay — default 10 minutes,
  `watchdog.restart_delay_seconds` in `config.yaml`):
  ```powershell
  uv run lab watchdog
  ```

**To remove the scheduled tasks:**
```powershell
scripts\uninstall-watchdog.ps1
```

If any of the three numbered steps above does not behave exactly as described the next
time someone runs them fresh, that is a bug in this document — fix the doc, do not
improvise a workaround and leave the doc wrong for the next person.

---

## The PAUSE file

Path: `data\PAUSE` (configured at `collect.pause_file` in `config.yaml`).

- **To pause safely for maintenance:** create an empty file at that path.
- **To resume:** delete the file.
- Every collector job, across every venue (Polymarket, Kalshi, Metaculus, Manifold),
  checks for this file first and skips its cycle if present (guardrail 8). It therefore
  takes at most one collection cycle — a few minutes — to fully halt all polling.
- **The Phase 18 outbound heartbeat still pings while PAUSE is set.** This is
  deliberate: the heartbeat only proves the process is alive, not that it is actively
  collecting. If it stopped pinging during PAUSE, every deliberate maintenance window
  would fire a false "collector is dead" alert.

---

## Backup location and restore procedure

**Location.** The private mirror lives at a sibling checkout: `..\Polymarket-results`
(i.e. next to, not inside, this repo). This is **not** the public GitHub repo — raw
market data and the sqlite db must stay private, so they never go to the MIT-licensed
public repo.

**How it stays in sync (automatic, nightly):**

- Every nightly run of the analytics bundle also runs the publish job, which **always**
  mirrors curated output (`reports/`, `exports/`, model artifacts) to the private repo.
- It **additionally** pushes `data\snapshots\` every night, when
  `publish.raw_data.snapshots_enabled` is `true` in `config.yaml` (currently `true`).
- It **additionally** pushes `data\lab.db` every `publish.raw_data.db_interval_days`
  days (currently `3`), when `publish.raw_data.db_enabled` is `true` (currently
  `true`) — gated by a `last_raw_db_push_ts` timestamp stored in the db's own `meta`
  table. This spacing exists because `lab.db` is a single, ever-growing binary with no
  Git LFS delta compression: pushing it as often as the small/incremental snapshot
  partitions would burn a GitHub LFS free-tier month's 1GB bandwidth quota in days.

**Currently paused on this host (2026-07-10 onward, local-only override — see the
banner at the top of this doc):** `publish.enabled` is `false` in this laptop's own
`config.yaml`, uncommitted, so none of the above runs from here while the VPS is
the primary pusher to the same private repo. Nothing to fix — this is intentional
for the parallel-verification window, not a broken backup.

**Manual, on-demand push** (does not wait for the nightly schedule):
```powershell
uv run python scripts\publish_results.py [--raw-data | --snapshots-only | --db-only] [--no-push]
```

**Known quirk — a stale legacy path exists in the results repo.** The private results
repo currently also contains a **legacy root-level `lab.db` / `lab.db-shm` pair**, left
over from before the Phase 15 raw-data-path reorg. That pair is **not** the current
backup. The live, current path the sync code actually writes to is:

```
..\Polymarket-results\data\lab.db
```

Always restore from that path, never from the root-level pair. This doc does not delete
the stale pair automatically — that is a manual cleanup call for the operator to make
next time they are in that repo (verify it's truly unused, then delete it by hand).

### Restore procedure

1. Clone the public code repo into a clean directory:
   ```powershell
   git clone https://github.com/Vladosyna/prediction-market-forecast-lab.git
   ```
   This confirms the published repo alone (MIT-licensed, per CLAUDE.md §13) is
   sufficient to rebuild the lab's code.
2. Inside the clone, install dependencies:
   ```powershell
   uv sync
   ```
3. Copy your separately-held `.env` file into the clone's root. `.env` is gitignored
   and excluded from **both** git remotes (public code repo and private results repo)
   by design — it is never part of any automatic backup. Keep your own secure copy
   (password manager or equivalent) from day one; this runbook cannot restore what was
   never backed up in the first place.
4. Copy `data\lab.db` and `data\snapshots\` from `..\Polymarket-results\data\` (the
   current path — see the quirk note above, not the legacy root-level pair) into the
   clone's `data\` directory. Copying `models\` from the results repo into the clone's
   `data\models\` is optional but recommended for full forecasting capability
   immediately after restore.
5. Run:
   ```powershell
   uv run lab status
   ```
   Confirm it reports the restored row counts/freshness sensibly. Freshness will show
   as stale until the collector starts running again in the new location — that is
   expected, not a failure.
6. Run:
   ```powershell
   uv run pytest -q
   ```
   Confirm the full suite passes. This proves the published repo alone (no
   machine-specific state beyond `.env` plus the `data\` directory) is sufficient to
   stand the lab back up.
7. Resume unattended operation:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
   ```

See the drill log at the very end of this document for the record of when this
procedure was last actually executed, by whom, and with what result.

---

## Key inventory and rotation

All keys live in `.env`, which is gitignored in **this** repo and never touches the
public remote. As of `publish.raw_data.env_enabled: true` (default on), `.env` is
additionally backed up as `.env.backup` into the private results repo
(`forecast-lab-results`, confirmed a genuinely private GitHub repo) every night via
`run_publish_job` — so losing this laptop doesn't mean re-requesting every key from
scratch. This is a deliberate tradeoff, not a free lunch: every key value ever set now
lives permanently in that private repo's git history, including after rotation
(rotating a key at the provider only replaces the value in the live `.env`; old commits
still hold the old value). Acceptable for a solo-operator private repo; reconsider before
ever adding a second collaborator to `forecast-lab-results`, or before making it public.
Set `env_enabled: false` in `config.yaml` to opt back out.

| Key | Used for | Rotate at | Notes |
|---|---|---|---|
| `DEEPSEEK_API_KEY` (or `ANTHROPIC_API_KEY` if `config.yaml`'s `llm.provider` is set to `"anthropic"`) | M3 evidence pipeline, M7 cross-venue propose LLM calls | `platform.deepseek.com` / `console.anthropic.com` dashboard | See restart nuance below |
| `FRED_API_KEY` | M5 macro nowcast inputs (GDPNow/PCENow series) | Free key at `fredaccount.stlouisfed.org` | |
| `METACULUS_API_KEY` | M7 cross-venue signal input, M1.x recalibration | Requires a real Metaculus account (anonymous access removed as of 2026-07-03) | |
| `NEWSAPI_KEY` | Optional M3 retrieval augmentation | `newsapi.org` | Google News RSS works without it — this key is optional |
| `HEARTBEAT_URL` | Phase 18 dead-man heartbeat | Any healthchecks.io-class free monitoring service, no card required | Absent = feature silently off: no error, no forecast-quality impact, just no external monitoring |
| `PMXT_API_KEY` | M7 out-of-band pmxt Router scan (`scripts\pmxt_router_scan.py`, its own separate scheduled task — see above) | `pmxt.dev` dashboard | **Trading-capable key** — the same hosted key also authorizes live order placement/escrow custody on pmxt's other endpoints, even though this repo only ever calls its read-only Router search. Never used by `src/lab` or any process this repo schedules directly — only by the standalone scan script, on its own task. Rotate immediately if this key is ever suspected leaked, same urgency as a trading credential, not a read-only data key. |

**`HEARTBEAT_URL` setup notes:** prefer a "Cron" check type over a "Simple" check type
if the monitoring service offers one. Both the collector loop (every
`ops.heartbeat_interval_minutes`, default 5 minutes) and the once-nightly backup job
ping the same URL at very different cadences, which can confuse a strict fixed-period
"Simple" check's expected-interval estimate.

**Restart nuance — applies to every key above.** `.env` is only loaded once, at process
start (`load_dotenv()`). An already-running orchestrator process will **not** pick up a
rotated key until it is restarted. The watchdog's own next scheduled restart is **not**
triggered automatically by a key rotation — after rotating a key, manually restart the
orchestrator (e.g. via Task Scheduler "Run", or `uv run lab run`) right away.

---

## Personal git identity: SSH auth + GPG commit signing (added 2026-07-10)

Separate from the repo-scoped deploy keys used by automation (this host has none —
it authenticates over HTTPS via `gh` CLI's credential helper, `credential.https://
github.com.helper`), this laptop also has a **personal-account** SSH key and GPG
signing key, generated the same day as the VPS's equivalents (see
`docs/VPS_OPERATIONS.md`'s matching section) so commits from either host are
independently attributable and verifiable, not just push-authorized.

- **SSH key** — `~/.ssh/id_ed25519_personal_github` (no passphrase), added to
  GitHub under **Settings → SSH and GPG keys** as an *Authentication Key* on the
  personal account. Not currently wired into any remote (this host still pushes
  over HTTPS via `gh`) — it exists so this host can authenticate as the personal
  account over SSH if ever needed (e.g. cloning another private repo), without
  depending on the `gh` CLI's OAuth session.
- **GPG key** — ed25519, no passphrase, `Name-Real: Vladosyna`, same email as
  `git config user.email`. `git config --global commit.gpgsign true` and
  `tag.gpgsign true` are set, so **every commit from this host is now signed by
  default** — public key added to GitHub under **Settings → SSH and GPG keys**.
  Confirmed showing `"verified": true` via the GitHub API on a real pushed commit.
  No passphrase was a deliberate simplification to generate it non-interactively
  in one sitting — regenerate it locally with a passphrase (`gpg --full-generate-
  key`, then re-point `user.signingkey` and re-add the new public key to GitHub)
  if tighter protection of this specific key ever matters more than convenience;
  nothing else in this repo depends on the current key's fingerprint.
- **Key IDs and rotation.** Find the current signing key: `gpg --list-secret-keys
  --keyid-format=long vlad.yurchina@gmail.com`. To rotate: generate a new key the
  same way, `git config --global user.signingkey <new-fingerprint>`, add the new
  public key to GitHub, then optionally revoke the old one there.
- **Why per-host keys, not one shared key.** Each host has its own independent
  SSH/GPG keypair rather than copying one keypair's private material between
  machines — a compromise of one host's key doesn't implicate the other, and
  revoking one host's access on GitHub doesn't touch the other's.

---

## `lab status` red-flag glossary

| Flag | What it means | What to do |
|---|---|---|
| `last_snapshot_age` far above the tier's configured cadence (liquid ~5 min, tail ~60 min) | The collector process is stalled, crashed, or the machine itself was asleep/off (see the past sleep incident above) | Check `data\logs\watchdog.log` first — is the watchdog even seeing the process as alive? Then check Windows Event Viewer's System log for sleep/wake events: `Get-WinEvent -FilterHashtable @{LogName='System';Id=42,506,507}`, and correlate against the last line in `data\logs\lab.jsonl` before the gap. |
| `gaps_24h` / `gaps_7d` elevated | Intermittent connectivity or upstream rate-limiting, not necessarily a dead process | Watch for repeated 429/5xx entries in `data\logs\lab.jsonl` around the gap window. Check `last_snapshot_age` too, to tell "intermittent" apart from "dead." |
| `resolution watcher: oldest_check=Nh` climbing past roughly one full sweep (~21h at the current 1000/cycle batch and 30-min cadence) | **The direct stall signal.** No candidate should go longer than one sweep without being looked at; if the oldest keeps ageing, the watcher is not getting through its queue | Check the backlog size and `never_checked` on the same line. If `never_checked` is large and static, the scan is not advancing — that is the 2026-07-25 failure mode (see below), and the ordering in `resolutions.unresolved_closed_markets` is the first thing to check. Otherwise consider raising `collect.resolution_backlog_limit`. |
| `resolution watcher: backlog=N` growing over time | Either a genuine UMA dispute-window backlog (expected, transient) or the watcher falling behind | Check whether N is roughly stable (healthy — disputes resolve on their own schedule) or monotonically climbing (watcher problem). Note `backlog` is the watcher's real working set; the `closed=` figure beside it is only the already-closed subset and is always smaller. |
| Per-venue `closed_unresolved` lines (Kalshi/Manifold) | Same read as above, scoped to that venue | Same action as above, per venue. |
| `metaculus last_snapshot_age=never` | Expected and benign by design, until at least one Metaculus pair exists in `data\markets_map.yaml`'s confirmed list | No action — there is no broad Metaculus universe sync, only confirmed-pair snapshots. |
| "LLM spend today: $X / cap $Y" near the cap | By design (guardrail 10): M3 and the weekly M7 propose job will start skipping remaining markets for the rest of the UTC day once the cap is hit | Not an error — just fewer forecasts/proposals until the cap resets at UTC midnight. No action needed. |

### Incident: every connection took the write lock (2026-08-01 → 08-05)

Presented as an hourly 100%-CPU spike, an hourly restart, and heartbeat
alerts. Cost: 84 unit restarts over three days, and 50 junk `model_versions`
rows written into an append-only audit trail.

**Root cause, and it was not the job that looked guilty.** `db.connect()` ran
`executescript(SCHEMA)`, all seven migrations and two `INSERT OR IGNORE`s on
**every call**. All of that takes the SQLite write lock. So opening a
connection merely to *read* — which `schedule_state.last_run_age_seconds`
does on every hourly catch-up tick — contended with whatever was writing.
Under light load this was invisible for months. With one long-running job
holding the writer, every concurrent open failed outright. The production
traceback ends at `migrate_multi_venue`'s `INSERT OR IGNORE INTO venues`,
reached from `last_run_age_seconds`.

`lab learn` was blamed for three days and was innocent. It was simply the
first job long enough to keep a writer busy while the catch-up kept opening
read connections. Every hour: catch-up saw learn overdue (it never stamped a
success, because it kept dying) → ran it → learn wrote → every other job died
on "database is locked" → the unit restarted → repeat. Each partial run got
far enough to register a `model_versions` row before dying, which is where the
50 junk rows came from.

**Fix:** the schema/migration block now runs only when the database is not
already at `SCHEMA_VERSION`, so an ordinary open is a single read. Safe
precisely because the migrations were already idempotent and version-gated —
the version check reads what they would otherwise re-assert. A new or stale
database still takes the full path. Regression test holds the write lock on
one connection and requires another to open successfully; it fails without the
fix, at the same line as the production traceback.

**The 50 junk rows are left in place.** `model_versions` is append-only for
the same reason `docs/ledger_commitments.jsonl` is: deleting rows to tidy an
audit trail is the act the trail exists to make detectable. They are inert —
all 50 have `is_active = 0` and none was ever promoted (registered
2026-08-03T02:13 .. 2026-08-05T17:03; the 7 active versions all predate the
window or come from the deliberate 2026-08-02 apply). Anyone counting
`m4_weights` versions should know they include hourly artifacts of a crash
loop, not 178 considered model revisions.

**Two operator lessons, both earned the hard way here.** A safety measure
removed to run an experiment has to be watched to its conclusion — the hold on
learn's catch-up was lifted on 2026-08-02 to test something and left lifted
for three unattended days. And a job completing from the CLI does not
establish that it completes under the orchestrator; that was the exact gap the
test was meant to close, and a single manual success was treated as if it had
closed it.

---

### Second act: with the lock gone, learn was OOM-killed — a child does not leave its parent's cgroup (2026-08-05)

The lock fix above was necessary and worked (zero `database is locked` errors
after it landed), but it was not the whole story. Re-running learn through the
orchestrator under observation — the thing that had never been done — it died
again at 84 s:

```
lab learn exited -9 (a negative code is a signal, e.g. -9 = OOM-killed)
```

The same job standalone from the CLI finishes in about 101 s. That gap is the
entire finding, and it is not about learn:

**Running a batch job out-of-process does not give it its own memory budget.**
`_run_lab_command_out_of_process` spawns `python -m lab learn` as a child, and a
child inherits its parent's cgroup. So learn's ~830 MB peak landed *inside*
`lab-run.service`'s 1400 MB cap, on top of the collector's own ~570 MB. The
cgroup was over its limit and the kernel killed the newest, largest member.
Out-of-process bought process isolation (a crash cannot corrupt the
orchestrator's heap) — it never bought memory isolation, and the earlier
report-render fix was read as if it had.

**Fix: `lab learn` left the orchestrator entirely for its own systemd unit and
timer** (`lab-learn.service` / `lab-learn.timer`, documented in
`docs/VPS_OPERATIONS.md`). This is not a new pattern in this project — the pmxt
scan has run exactly this way since the cutover. A separate unit means a
separate cgroup, so learn's peak is charged to learn; if it ever exceeds its
own cap, systemd kills learn and nothing else, and the collector does not
notice. `tests/test_schedule.py::test_learn_is_not_scheduled_by_the_orchestrator_at_all`
holds the removal in place.

**Third act: with its own cgroup, the footprint became measurable — and the
number was absurd.** In an *uncapped* accounting cgroup the job peaked at
**1747 MB of RAM plus 1666 MB of swap and was still killed**, on a 1973 MB box.
No cap can accommodate that, so the next three attempts (900 MB, 700 MB,
1200 MB) were all wrong by construction. Step-by-step RSS probing through
`run_learn`'s stages found two independent defects, both fixed in code:

1. **The bootstrap training set was expanded into Python dicts.**
   `loop.py`'s `load_observations(config).to_dicts()` turned
   `observations.parquet` — 1,967,376 rows × 5 columns, ~80 MB as Arrow — into
   ~1.8 GB of dict shells and boxed floats. Both M1 fitters only ever read
   fixed columns as numpy arrays, so they now take either shape and the loop
   hands them the frame. Verified byte-identical against the previous
   implementation on all four horizon buckets before deploying.
2. **`estimate_rho_bar_m7` read 90 days of snapshots unprojected.** Its input
   is 268 resolved rows, but it pulled every venue's snapshot history for the
   whole window *including* the `bids_json`/`asks_json` order-book blobs —
   >1.3 GB. `read_range` has had a `columns=` projection since the report
   readers needed one; this call site simply never used it. `price_moves_24h`
   had the same defect on a 3-day window and, worse, runs nightly inside the
   collector's own cgroup, so it was fixed too.

After both: **900 MB peak, no swap, 105 s**, completing cleanly under its own
cap. The same run also succeeds at 700 MB and 600 MB by swapping, so 900 MB is
chosen as the measured no-swap point rather than the minimum that survives.

Note what the fixes did *not* change: `m1_curves` and `m1_hier_curves` came
back **not promoted**, their confidence sequences spanning zero (`cs_lo`
≈ −0.018) on n_train = 1,967,376 and n_holdout = 14,468. The CI gate worked
exactly as specified throughout; it was never what was broken.

**The operator lesson, second half.** Three times in this incident the
reflex was to adjust the memory cap, and three times the cap was not the
problem — a cap is a diagnostic that tells you what a job demands, and a job
demanding 3.4 GB to fit four logistic curves is telling you about the job. The
useful move was to instrument the stages and read the numbers.

Two follow-ons landed with it. The weekly report moved 06:00 → 14:00 UTC: it
had been sitting 4 h behind the nightly bundle, inside the ≥ 5 h margin the
spacing test requires of the heaviest job — the test had simply never included
`report_cron` in its job list, and reviewing the list for learn's removal is
what surfaced it. And `report` itself remains an in-cgroup child of `lab-run`
by deliberate choice: its ~834 MB peak is what the 1400 MB cap was sized for,
and it is not worth a unit of its own while that holds.

---

### The hourly crash loop: the nightly bundle read the whole snapshot archive (2026-08-02 .. 08-06)

**Symptom the operator saw:** ~100% CPU every hour. **What it actually was:**
`lab-run.service` OOM-killed on a 62-minute cycle, 19 restarts, each one taking
the collector down with it — roughly 24 collector kills a day for four days,
against unrecoverable snapshot history.

**The loop.** `last_run_forecast` was stuck at 2026-08-02T16:18. Every hourly
health check saw the nightly bundle 90+ hours overdue, ran it, the bundle
crossed the unit's 1400 MB cap, the cgroup killed the whole unit, systemd
restarted it, and the stamp stayed stale. 21 minutes of CPU per cycle on a
1-vCPU box is what surfaced it.

**Three separate reads, all the same defect, found one at a time.** Each fix was
real and none of them was sufficient, which is the part worth remembering:

1. `eligible_market_states` → `latest_per_market`: two days of every venue's
   snapshots at the full schema, **+578 MB** per call, and it is called 11 times
   across forecast/universe/M3/M6/M7/shadow — several inside one bundle. Fixed
   by defaulting the method to every column except the order-book blobs (nothing
   in `src/lab` reads them back) and reducing one day at a time.
2. `clv_validity_check` → `read_range`: **every partition in the archive** —
   measured live, 30 partitions, 14,900,405 rows, 675 MB even projected to three
   columns — to score a handful of sports markets. Projection could not fix it;
   the waste was rows. `read_range` gained a `condition_ids` filter, applied at
   all four call sites of that shape rather than only the one that crashed.
3. `build_mid_index`: the index built over that read was a dict of Python lists
   of ISO strings and floats, ~100 bytes a row. It took the report render from
   444 MB to **1227 MB**. Now int64 epochs and float64 mids, 16 bytes a row.

**What made the loop self-sustaining, and why it hid from the first two fixes.**
The fatal read was in `update_clv_trust_flag`, which runs *after* `run_eval`
returns. So the bundle logged `eval complete`, then died — before
`record_job_run`. Finished work that never counted as done, re-run hourly. The
diagnostic tell was one missing log line: `lab.eval.run: eval complete` appeared
every cycle, `lab.jobs: eval job complete` never did.

**Verified fixed, 2026-08-06:** bundle ran end to end (forecast 16:03, eval job
complete 16:15), stamp written, 0 restarts, unit peak 833 MB against the
1400 MB cap. The bundle's own concurrency guard also proved out: the health
check fired a second forecast catch-up at 16:12 while the first was still
running, and the per-service lock skipped it — no double run, no double LLM
spend.

**The report was next in line and would have died tomorrow.** `last_run_report`
was 7 days old against a 192-hour control, so the catch-up would have fired it
on 08-07. Measured before the index fix: collector 288 MB anon + render
1227 MB = 1515 MB against a 1400 MB cap. After: 288 + 684 = 972 MB. Caught by
measuring instead of waiting.

**Two operator notes on metrics used here.** A cgroup's `MemoryPeak` counts
reclaimable page cache, so a read-heavy job reports a peak equal to whatever cap
it was given — it reads "at the limit" whether or not it is near trouble. Use
the process's anonymous RSS (`memory.stat`'s `anon`, or the report's own
per-phase probes) to size anything. And `MemoryCurrent` right after a big job
overstates the steady state for the same reason: it read 708 MB where the
collector's actual anon was 288 MB.

**The lesson.** Two days, three fixes, one defect class: a large read that is
narrowed by neither columns nor rows. After the first fix the class was not
swept — only the site that had crashed was. Whenever one of these turns up,
enumerate every caller of the same reader and classify each, rather than fixing
the one in the traceback.

**The operator lesson.** "It runs out-of-process" and "it has its own resource
budget" are different claims, and this system had been treating the first as
evidence for the second since 2026-07-28. Only a process tree that systemd
starts itself gets its own cgroup accounting.

---

### The M1 training set is a host dependency, not a repo artifact

`data/bootstrap/observations.parquet` (51 MB, 1,967,376 rows) is the ONLY
input the M1 / M1.x recalibration refit trains on. Everything the lab collects
itself goes to the walk-forward holdout instead, so without this file the
refit reports `skipped: insufficient_data` with `n_train=0` and the curves
simply stop being updated.

That is exactly what happened between the 2026-07-10 laptop-to-VPS cutover and
2026-08-02: the directory existed but was empty on the VPS, so M1's curves sat
frozen at their 2026-07-03 values for a month. Nothing was broken and nothing
lied -- `lab learn` reported the skip every run -- but the dry-run output was
not being read, which is the failure mode a dry-run has when nobody reviews it.

Practical notes:

- The file is **static**. It is derived from a historical archive that ends in
  2025 and carries no date column; 2026-onward data never enters it. Copy once
  per host, never regenerate.
- It is NOT the 27 GB `quant.parquet`. That one is only the raw source used to
  build this file (`lab bootstrap`), and is not needed at refit time.
- It is gitignored and not covered by the results-mirror backup, so it lives
  only on hosts you put it on. Losing it is recoverable (rebuild from the
  archive) but slow; copying the 51 MB from another host is faster.
- A host without it still runs everything else correctly. The symptom is
  narrow: M1/M1.x silently stop refitting.

---

### Incident: cgroup limits oversubscribed, box wedged (2026-07-30)

The VPS became unreachable for ~3 hours and needed a manual power cycle from
the DigitalOcean console. Snapshot history for that window is gone.

**The symptom worth recognising again:** ICMP answered normally (175 ms, 0%
loss) while *no userspace service could be served* — HTTPS timed out, and SSH
could not complete a TCP handshake even at a 4-minute timeout. Kernel alive,
userspace starved. If ping works and nothing else does, do not keep retrying
SSH; power-cycle.

**No OOM kill, no restart.** The orchestrator ran continuously throughout.
The only trace in its own log is a growing lag between the systemd timestamp
and the application's own: 21 seconds at 07:14, **29 minutes** by 07:45. The
system journal shows `systemd-journald` and `systemd-resolved` repeatedly
"Under memory pressure, flushing caches" from 08:42 until the reboot.

**Cause: the per-service memory caps summed to more than the machine had.**

| service | MemoryMax |
|---|---|
| lab-run | 900 M |
| lab-dashboard | 600 M |
| **sum** | **1500 M** |
| physical RAM | **967 M** |

Each service could grow to its own cap without ever breaching it, while
together they exhausted the box. With no cgroup violation to act on, the
kernel never selected an OOM victim — it simply thrashed. The 900 M figure
was set during the 2026-07-20 OOM work without accounting for the dashboard's
pre-existing 600 M.

The dashboard was not involved: it logged nothing during the window and holds
~25 M; the 223 nginx hits were mostly 404 scanners.

**Fix (drop-ins under `/etc/systemd/system/<unit>.service.d/memory.conf`):**
lab-run 600 M/700 M, lab-dashboard 150 M/200 M — the caps now sum to 900 M,
below physical RAM.

**The rule this encodes:** the sum of `MemoryMax` across services must stay
below physical RAM. A cap that is individually generous and collectively
impossible protects nothing — it converts a recoverable per-service kill
(seconds, automatic) into an unrecoverable box wedge (manual power cycle,
hours of lost collection).

**Consequence to watch:** lab-run's measured working set is 440–570 M, with
excursions near 790 M during a tail sweep. At `MemoryMax=700 M` such an
excursion will now be killed and restarted. That is the intended trade, but
if restarts become frequent it is the signal that this box is undersized
rather than that the cap is wrong.

---

### Incident: the resolution watcher stall (2026-07-02 → 2026-07-25)

Worth recording because the failure was silent, lasted three weeks, and the
dashboard actively hid it.

`unresolved_closed_markets` took `LIMIT n` with **no `ORDER BY`**, so SQLite
returned the same `n` rows in scan order every cycle. The head of that scan
had filled with markets Gamma never reports as `closed` whose end dates were
long past — permanently unresolvable, permanently first — so the watcher
re-fetched the same few hundred hopeless markets every 30 minutes and never
reached anything behind them. The working set reached ~42k markets draining at
40–60/day while ~600 closed daily; 11,260 forecasts sat on markets that could
never be scored, and half of all forecast *weather* markets (629 scored vs 630
stuck) were affected — the category carrying a primary hypothesis.

Two things kept it invisible, both now fixed:

- `lab status` reported only the `closed = 1` subset (17k of a real 42k), so
  the number on the dashboard was never the number the watcher was working
  through. It now reports the watcher's own working set.
- There was no staleness signal at all. `oldest_check_age_h` is now the
  first-class one: it cannot stay flat while the scan is stuck.

The glossary row above did say "monotonically climbing → watcher problem," and
the number *was* climbing at every check. The lesson is not that the guidance
was missing but that a slowly-growing number with a plausible benign reading
("UMA disputes are backed up") gets explained away indefinitely. A signal that
is *unambiguous* when broken — like `oldest_check_age_h` — is worth more than a
signal that needs interpretation.

No data was lost: the forecast ledger was never affected, and all 25 of the
oldest stuck weather markets probed after the fix were still served by Gamma
with final payouts available. The backlog is recoverable in full, and a single
800-market sweep immediately after the fix recorded 356 resolutions.

---

## Backup-restore drill log

One-time now, then quarterly per this runbook: restore `data\` from the private backup
repo onto a clean checkout, run `lab status` and the full test suite against the
restored state, and record the result below.

| Date | Performed by | Result | Notes |
|---|---|---|---|
| 2026-07-08 | Claude Code | **Pass** | Fresh `git clone` of the public repo into a clean directory, `uv sync`, `.env` copied in by hand (never backed up automatically, by design), `data/lab.db` + `data/snapshots/` + `models/` restored from `../Polymarket-results/data/` (the current path — see the stale root-level `lab.db` note above). `uv run lab status` reported correct row counts (61452 forecast rows, 11980 resolutions) with expected stale snapshot ages (collector wasn't running in the clone — not a failure). `uv run pytest -q` → 356 passed (the clone predates this same day's Phase 18 push, so it doesn't yet include `heartbeat.py`'s own tests; a same-day re-run after the Phase 18 push would show 361). Confirms the published repo + a restored `data/` directory alone are sufficient to stand the lab back up. |
| 2026-09-30 | Claude Code | **Pass — one gap found and fixed** | First drill of the backup as it now exists (no whole `lab.db` in the mirror since 2026-09-25). `lab restore --from /root/forecast-lab-results --to /root/restore-drill/lab.db` on the VPS, reading the mirror checkout at the commit GitHub also holds (`daddffe`): 1,444,254 forecasts, 6,956 evidence runs, 7,974 resolutions of forecast markets, 18,709 markets, 4,694 events and 252 model versions in about a minute; every manifest digest and row count matched; **all 86 committed dates in `docs/ledger_commitments.jsonl` verified against the restored rows**. Forecast and evidence counts equal the live DB through the last mirrored day (09-29). The H1-family statistics as of 2026-09-26 — `m0_market`, `m1_debiased`, `m4_ensemble` on both venues, confirmatory window, censored as PAP 9.35 — came out **identical** to the live DB in n, clusters, mean and confidence sequence. **Gap:** the reference dump of markets was weekly (last 09-26), so 2,616 markets first forecast 09-27..29 had no market row in the backup, their forecasts could not be interpreted, and 222 of their resolutions could not be restored. Fixed the same day: `reference/markets_daily/<day>.jsonl.gz` — markets first forecast that day, written with the nightly ledger increment, immutable, with a manifest; `lab restore` fills from them what the weekly dump lacks. Snapshots and model artifacts are mirrored as plain files and were not re-tested. Next drill: quarterly, or after any change to what the mirror carries. |
