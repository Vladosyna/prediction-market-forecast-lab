# VPS Operator Runbook (Debian 13, 167.71.201.113)

**As of 2026-07-10, this VPS is the primary host, and as of 2026-07-13 the sole
one** — it runs the full pipeline (collector + forecast/eval/report/shadow/
learn + private-results publish). The Windows laptop's parallel-verification
window (meant to last "a few days") ran three days over, and the divergence
that caused is documented in this file's Changelog and in
`docs/OPERATIONS.md`'s own retirement entry — read those before assuming this
host's `lab.db` and the laptop's ever agreed on anything after 2026-07-10.
This document is a companion to [`docs/OPERATIONS.md`](OPERATIONS.md) (the
Windows-laptop runbook, now a retired-host historical record) — **not a
replacement for it**; follow the same "fix this doc if a step doesn't work"
discipline as that document.

---

## What runs where

**Host.** Debian 13 ("trixie"), IP `167.71.201.113`, repo checked out at
`/root/polymarket-forecast-lab`. Access is via SSH as `root`, using a dedicated
deploy key (not the operator's personal key, so it can be rotated/revoked
independently of any other machine's access):

```bash
ssh -i ~/.ssh/id_ed25519_deploy root@167.71.201.113
```

**Why root.** This is a small, single-purpose VPS with nothing else on it; the
original `lab-collect.service` already ran as root, and every unit added since
follows the same precedent for consistency. Revisit if this host ever runs
anything beyond this lab.

**Scope.** This host runs the **full pipeline** via `lab-run.service` (`uv run
lab run` — collector + scheduled forecast/eval, report, and shadow, much like
`docs/OPERATIONS.md`'s laptop setup) plus the **Streamlit dashboard**
(`lab-dashboard.service`, read/write UI over the same data). The original
`lab-collect.service` (collector-only) is disabled, not deleted — `lab-run.service`
subsumes everything it did.

The one job that is **not** in `lab-run.service` is the monthly `lab learn`
loop: it has its own unit and timer, for cgroup-budget reasons spelled out in
"Why `lab learn` has its own unit" below.

**Two long-running systemd services, plus several systemd timers, run on this host:**

| Unit | Purpose | Command |
|---|---|---|
| `lab-run.service` | **Primary orchestrator** (collector + forecast/eval, report, shadow, map_propose, pmxt_verify, paper_export) | `uv run lab run` |
| `lab-collect.service` | Collector-only — **disabled since 2026-07-10**, kept for rollback | `uv run lab collect` |
| `lab-dashboard.service` | Streamlit dashboard, loopback-only | `/root/.local/bin/uv run streamlit run src/lab/dashboard.py --server.port 8501 --server.address 127.0.0.1 --server.headless true` |
| `pmxt-scan.timer` → `pmxt-scan.service` | pmxt Router scan, twice daily 05:00/17:00 UTC | `uv run --with pmxt python scripts/pmxt_router_scan.py` |
| `lab-learn.timer` → `lab-learn.service` | **Monthly learning loop** (dry-run), 1st of the month 04:00 UTC | `uv run lab learn` |
| `pmxt-verify.timer` → `pmxt-verify.service` | **Disabled** — `lab-run.service` schedules the verify pass itself (17:00/21:00 UTC). Kept for a host that runs the pmxt cycle without the orchestrator | `uv run lab map pmxt-verify` |
| `results-pull.timer` → `results-pull.service` | **Disabled since 2026-07-10** — role reversed, see "Cutover" below | `git pull --ff-only origin main` |

Standard commands apply to the long-running services:

```bash
systemctl status lab-run.service
systemctl status lab-dashboard.service
systemctl restart lab-run.service
systemctl restart lab-dashboard.service
journalctl -u lab-run.service -f            # follow live logs
journalctl -u lab-dashboard.service -f
```

For the timers (oneshot services, triggered on schedule — nothing to "keep running"):

```bash
systemctl list-timers lab-learn.timer pmxt-scan.timer --no-pager   # next/last fire time
systemctl start lab-learn.service       # run the monthly learn diff right now
systemctl start pmxt-scan.service       # trigger a scan right now, out of schedule
journalctl -u lab-learn.service -n 60 --no-pager
journalctl -u pmxt-scan.service -n 40 --no-pager
```

---

## Cutover to primary (2026-07-10)

This host was collector-only until 2026-07-10, when it became the primary — the
laptop's forecast/eval/wealth history was noticeably ahead of this host's own
(restored-once-then-frozen) database, so a straight "just start `lab run` here"
would have silently orphaned days of accumulated calibration history. What
actually happened, in order:

1. A consistent snapshot of the **laptop's** live `data/lab.db` (92,323
   forecasts, 12,241 resolutions, 1,714 eval_runs at cutover time — vs. this
   host's own stale 61,452/12,229/1,144, frozen since its initial restore) was
   taken via SQLite's `backup()` API (safe against a concurrently writing WAL
   connection — same method `publish.py::sync_db` already uses) and transferred
   here, replacing this host's own `data/lab.db`. The pre-cutover VPS db is kept
   at `data/pre_migration_backups/lab.db.pre_vps_primary_cutover_<timestamp>`
   for rollback.
2. `lab-collect.service` was disabled; `lab-run.service` (above) created and
   started in its place.
3. **First-boot bug, fixed on the spot:** a 0-byte `data/snapshots/date=2026-07-10/
   snapshots.parquet` (a genuinely empty, corrupt file — confirmed 0 bytes, no
   content to lose) crashed the startup report step every time
   (`ComputeError: parquet: File out of specification`), crash-looping the
   service. Deleted (with explicit confirmation, since `data/snapshots/` is the
   brief's own "crown jewels" data) and the collector recreated a valid file on
   its next successful write for that date. If a 0-byte or truncated snapshot
   parquet ever appears again for *today's* date specifically, this is almost
   certainly the same pattern (a partition file touched into existence right as
   a process was interrupted) rather than a sign of a different bug.
4. **Second bug, fixed on the spot:** `config.yaml`'s `publish.results_dir:
   ../Polymarket-results` is a relative path that only resolves correctly on
   the laptop's own directory layout (`D:\Polymarket` + sibling
   `D:\Polymarket-results`). On this host it resolved to `/Polymarket-results`
   (filesystem root!) — nonexistent, so `publish_results` correctly no-op'd
   every time (`{"skipped": "results_dir_not_a_git_checkout"}`), logged
   misleadingly as a generic "publish job complete" with no error surfaced.
   Fixed with a **local-only, uncommitted** override to the correct absolute
   path (`/root/forecast-lab-results`) — never commit this line, it's
   host-specific, same reasoning as item 5 below. Confirmed fixed by directly
   invoking `run_publish_job` once by hand: a real new commit landed in
   `forecast-lab-results` with the expected reports/exports/models/snapshots
   diff.
5. **Third bug, fixed on the spot:** this checkout had **no git committer
   identity configured at all** (`git config user.name`/`user.email` both
   empty) — it had only ever been `git pull`'d before, never committed from.
   Every job that commits here (`ledger_commitment`, `paper_export`,
   `pmxt_verify`, and `publish_results` targeting `forecast-lab-results`) was
   silently failing its `git commit` step and logging a misleadingly generic
   "complete" regardless. Fixed with `git config user.name`/`user.email` (both
   repos: `prediction-market-forecast-lab` and `forecast-lab-results`), matching the
   laptop's own identity. **If a git-committing job ever again logs "complete"
   with no matching new commit showing up, check this first** — it's a
   config-scoped setting (not global), so a fresh checkout or a new sibling
   results-repo checkout on a third host would need this set again from
   scratch.
6. `results-pull.timer` disabled — see below, role reversed.
7. On the laptop: `config.yaml`'s `publish.enabled` set to `false` as a
   **local-only, uncommitted** override (never propagate this to git — it would
   wrongly disable the VPS's own publish too, since it's the same tracked file).
   Prevents both hosts' nightly `run_publish_job` from racing to push the same
   private repo during the parallel-verification window. Re-enable (delete that
   local override, or `git checkout -- config.yaml`) only after the laptop's
   `lab run` is fully retired — by then it won't matter, since nothing will be
   running there to push.

**Retiring the laptop (operator does this manually, when ready — not on a
schedule this doc or any process controls):** stop the laptop's `lab run`/
`lab watchdog`. Optionally set up a laptop-side pull-mirror (a Scheduled Task
running `git pull --ff-only` against the laptop's own `../Polymarket-results`
checkout, mirroring exactly what `results-pull.timer` used to do here) so the
laptop keeps an archival local copy without being an active participant.

---

## pmxt scan + verify: why this host, and how it stays safe

As of 2026-07-10, **this VPS is the sole host running the pmxt scan+verify cycle** — the
laptop's own `PolymarketForecastLabPmxtScan` Scheduled Task is disabled (not deleted; see
`docs/OPERATIONS.md`). Two things made running it on both hosts unsafe, not just
redundant:

1. **`data/markets_map.yaml` has no merge strategy.** Every write (`save_markets_map`) is
   a full read-modify-write of the whole YAML file, and nothing auto-commits it as a
   matter of course. Two hosts independently rewriting it would eventually collide on
   `git pull`/push with an ordinary line-based conflict that silently drops one side's
   proposed pairs.
2. **The `$5/day` LLM cap is enforced per host.** `llm.daily_cost_cap_usd` (config.yaml)
   is checked against each machine's own local `lab.db` — running the verify step on two
   independent checkouts doubles effective spend to `$10/day` with no code-level
   awareness of the other host.

**How this host stays the single source of truth going forward:** `run_pmxt_verify_job`
(v2.9) now commits — and, per `cross_venue.markets_map_push` (default `true`), pushes —
`data/markets_map.yaml` to the public repo whenever it actually adds new proposals. No
revert-on-failure is needed (unlike the ledger-commitment/paper-export jobs): the
proposal is already durable on disk before the git step runs, so a failed commit just
leaves the same pending change for the next scheduled run (or a human) to retry — it can
never lose or duplicate a proposal. **The laptop (or any other host) sees new proposals
only after its own `git pull`** — nothing pulls automatically in the other direction.

Reviewing and confirming proposed pairs: use this host's own dashboard
(https://167-71-201-113.sslip.io, Cross-Venue Matching (M7) mode) or SSH in and run
`uv run lab map confirm <condition_id> --venue kalshi`. Either way, confirming here means
the laptop needs its own `git pull` to see the confirmation before its next `lab
forecast` run picks it up.

**First real run, 2026-07-10:** the initial scan fired all 12 query terms back-to-back
with no delay and about half came back with an empty response body
(`Expecting value: line 1 column 1`), interleaved with queries that succeeded normally —
consistent with a rate limit on pmxt's own API, not a schema problem. Fixed by pacing
queries 1.5s apart (`scripts/pmxt_router_scan.py`); confirmed on retest — all 12 queries
returned cleanly (0 clusters matched for any of them at the time, which is a legitimate
"nothing found yet" result, not an error). If a genuine schema problem appears instead,
the script prints a line starting `pmxt schema mismatch` with the raw object dump needed
to diagnose it.

## Why `lab learn` has its own unit

`lab learn` is the heaviest batch job in the system (~900 MB peak, ~105 s) and
it runs once a month. Until 2026-08-05 the orchestrator scheduled it like any
other analytics job, spawning it as a child process — which looked like enough
isolation and was not:

**A child process does not leave its parent's cgroup.** `python -m lab learn`
launched from `lab-run.service` is accounted against *that unit's* memory
budget. So learn's peak stacked on top of the collector's steady ~500 MB inside
a 1400 MB cap, and the kernel killed it at 84 s. Out-of-process gave crash
isolation; only a separate unit gives memory isolation.

Giving it a unit is what made the footprint *measurable*, and measuring it
found two real defects rather than a number to configure around. Before those
were fixed the job needed **1747 MB of RAM plus 1666 MB of swap and still died**
on a 1973 MB box: it expanded the 1,967,376-row bootstrap training set into
Python dicts (~1.8 GB), and `estimate_rho_bar_m7` read 90 days of every venue's
snapshots without a column projection, dragging the order-book JSON blobs along
(>1.3 GB). Both are fixed in code; the job now peaks at 900 MB with no swap.
The lesson worth keeping: a cap is a diagnostic, not a fix — the first two
attempts here raised it, and both times raising it was wrong.

`/etc/systemd/system/lab-learn.service` is a `Type=oneshot` unit with its own
`MemoryMax`, triggered by `lab-learn.timer` (`OnCalendar=*-*-01 04:00:00 UTC`,
`Persistent=true` so a reboot over the 1st still runs it). Exactly the pattern
`pmxt-scan.timer` has used since the cutover.

**It runs dry-run.** `uv run lab learn` with no `--apply` writes nothing — it
produces the reviewable diff the brief requires (§6, "dry-run by default").
Applying is a deliberate operator act:

```bash
journalctl -u lab-learn.service -n 200 --no-pager    # read the monthly diff
cd /root/polymarket-forecast-lab && /root/.local/bin/uv run lab learn --apply
```

Note that M1/M1.x refits need `data/bootstrap/observations.parquet` present on
this host or they report `skipped: insufficient_data` — see `docs/OPERATIONS.md`,
"The M1 training set is a host dependency".

**Memory budget, stated honestly.** The three units' caps (1400 + 500 + 900 MB)
deliberately do **not** sum below the box's 1973 MB, which the 2026-07-30 wedge
otherwise established as the rule. The rule targets units that run continuously
and can all sit at their ceiling at once; `lab-learn` runs for under two minutes
a month, in a 04:00 slot no other scheduled job occupies, against real
concurrent usage of ~500 MB (orchestrator) + ~70 MB (dashboard). What its own
cap buys is the invariant that matters here: if learn ever balloons, systemd
kills **learn** and the collector never notices — which is exactly what happened
on 2026-08-05, repeatedly, while the collector logged 0 restarts throughout.

900 MB is the measured no-swap point, not a guess: the same run completes at
700 MB (154 MB swapped) and at 600 MB (242 MB swapped), and touches no swap at
900 MB. Prefer keeping it there — swapping a job that holds the whole training
set is how a 105-second job becomes a nine-minute one. Check the assumption
still holds after any change to the other two caps:

```bash
systemctl show lab-run lab-dashboard lab-learn -p MemoryMax -p MemoryCurrent
free -m
```

`lab status` prints the same comparison (its memory-budget block) and flags
oversubscription automatically.

**Why `report` did not also get a unit.** It has the same child-in-the-cgroup
property, but its ~834 MB peak is precisely what `lab-run`'s 1400 MB cap was
sized for, and it has been surviving there since 2026-07-28. Revisit if the
collector's steady-state RSS grows.

## The dashboard: nginx + Let's Encrypt + HTTP Basic Auth

The dashboard has **no in-app authentication by design** (CLAUDE.md §12 explicitly
lists "user auth" as out of scope for `src/lab`), so it is fronted by nginx doing
TLS termination and HTTP Basic Auth, with Streamlit bound to `127.0.0.1:8501` only
(never reachable directly from the internet regardless of firewall state).

**URL:** https://167-71-201-113.sslip.io (Basic Auth username `admin`, password
shared once at setup time — store it in a password manager, it is never committed
to any repo).

**Domain.** `167-71-201-113.sslip.io` — a free wildcard DNS service that resolves
`A-B-C-D.sslip.io` to `A.B.C.D` automatically, no signup, no records to manage.
**If this VPS's IP address ever changes, this domain breaks** (it resolves to the
*old* IP forever) and the TLS cert becomes invalid for the new IP. There is no
"update a DNS record" fix — mint a brand-new `<new-ip-with-dashes>.sslip.io`
hostname, rerun the nginx + certbot steps below against it, and update this
document's changelog at the bottom.

**Packages** (native `apt`, never Docker — CLAUDE.md §12): `nginx`, `certbot`,
`python3-certbot-nginx`, `apache2-utils` (for `htpasswd`).

**nginx site config** at `/etc/nginx/sites-available/dashboard`, symlinked into
`sites-enabled`, with the stock default vhost removed so it can't answer on port
80 instead. Reverse-proxies to `127.0.0.1:8501` with the websocket-upgrade headers
Streamlit's live-update connection requires, and long (86400s) read/send timeouts
so nginx doesn't silently drop an idle session.

**Certificate** via `certbot --nginx`, which edits the site file in place to add
the 443/TLS block and an http→https redirect. Auto-renews via a systemd timer
certbot installs itself (`systemctl list-timers | grep certbot`).

---

## Results publishing: this host is now the pusher, not a reader

`/root/forecast-lab-results` is a checkout of the **private** `forecast-lab-results`
repo. Before 2026-07-10 this was a passive, hourly-pulled read-only mirror
(`results-pull.timer`, now **disabled** — see "Cutover" above). Now that this host
runs `lab-run.service`, its own nightly `run_publish_job` (part of the same
forecast/eval/report bundle, `config.yaml`'s `publish:` section) pushes `lab.db`,
snapshots, reports, exports, and model artifacts here directly — this host is the
one now doing what `docs/OPERATIONS.md`'s laptop-side backup section describes.

Access uses this checkout's own dedicated key (`~/.ssh/id_ed25519_results`, set up
during initial VPS provisioning, scoped via `~/.ssh/config`'s `Host github.com`
block), separate from the `id_ed25519_deploy` key the operator's own machine uses
to SSH *into* this VPS — confirmed to have real push (not just read) access via a
dry-run test before the cutover.

```bash
journalctl -u lab-run.service --no-pager | grep -i "publish job complete"
cd /root/forecast-lab-results && git log --oneline -5   # confirm recent pushes landed
```

**If the laptop's own `lab run` is still running in parallel** (the few-day
verification window), its `publish.enabled` is set `false` locally (see "Cutover")
specifically so it does *not* also try to push here — two hosts pushing the same
repo on their own nightly cron would race on non-fast-forward errors. Don't
re-enable it on the laptop until that instance is being fully retired.

**If you ever want a *read-only* mirror again** (e.g. on a third host, or back on
the laptop after it's retired), `results-pull.timer`'s unit files are still present
(just disabled) — `systemctl enable --now results-pull.timer` brings the old
hourly-pull behavior back on whichever host that mechanism belongs on next.

---

## Public-repo push access (discovered missing, fixed 2026-07-10)

The cutover above gave this host a way to push to the **private** results repo
(`id_ed25519_results`) but nothing to push to the **public**
`prediction-market-forecast-lab` repo itself — its `origin` remote was plain HTTPS with
no credential helper configured. That's a real gap: once this host became
primary, `pmxt_verify`'s `commit_and_push_markets_map`, the weekly
`paper_export`, and the nightly ledger-commitment job all commit to *this*
repo, not the results one. A commit made here would succeed locally and then
fail to push, silently piling up unpushed commits until someone noticed a git
push job on the VPS with `pushed: false` in the logs — a genuine one-day gap
was caught only because a manually-confirmed M7 pair needed to reach GitHub
right away and didn't.

Fixed the same way as the results repo: a second dedicated deploy key,
`~/.ssh/id_ed25519_public_repo`, added on GitHub under this repo's own
**Settings → Deploy keys** with **write access**, separate from
`id_ed25519_results` (scoped to the results repo only) and from
`id_ed25519_deploy` (the operator's own access key into this VPS). Because both
deploy keys authenticate against the same `github.com` host, `~/.ssh/config`
disambiguates them with a second alias:

```
Host github.com
  IdentityFile ~/.ssh/id_ed25519_results
  IdentitiesOnly yes

Host github.com-public
  HostName github.com
  IdentityFile ~/.ssh/id_ed25519_public_repo
  IdentitiesOnly yes
```

and this checkout's `origin` remote points at the alias, not the bare host:

```bash
cd /root/polymarket-forecast-lab
git remote set-url origin git@github.com-public:Vladosyna/prediction-market-forecast-lab.git
```

Verify push access still works after any key rotation:

```bash
ssh -T git@github.com-public   # expect "You've successfully authenticated" (exit 1 is normal -- no shell access)
git push --dry-run
```

---

## Personal git identity: SSH auth + GPG commit signing (added 2026-07-10)

Separate from the two repo-scoped deploy keys above (`id_ed25519_results`,
`id_ed25519_public_repo` — push authorization only, no signing), this host also
has a **personal-account** SSH key and GPG signing key, matching the laptop's
equivalents (see `docs/OPERATIONS.md`'s corresponding section) so every commit
this host makes — including the fully automated ones (`pmxt_verify`,
`ledger_commitment`, `paper_export`, `run_publish_job`) — is signed and shows
as `Verified` on GitHub, not just push-authorized.

- **SSH key** — `~/.ssh/id_ed25519_personal_github` (no passphrase), added to
  GitHub under **Settings → SSH and GPG keys** as an *Authentication Key* on the
  personal account. Not wired into either repo's remote (both still use their
  own deploy-key aliases) — exists for future personal-identity SSH use from
  this host if ever needed.
- **GPG key** — ed25519, no passphrase (required here: every signing job runs
  unattended via systemd/cron with no human present to unlock a passphrase-
  protected key — the same reasoning as the deploy keys being passphrase-less),
  `Name-Real: Vladosyna`, same email as `git config user.email`. Configured
  **globally** (`git config --global commit.gpgsign true` / `tag.gpgsign true`),
  so it covers both the public repo and `/root/forecast-lab-results` checkouts
  with no extra wiring. Public key added to GitHub under **Settings → SSH and
  GPG keys**. Confirmed `"verified": true` via the GitHub API on real pushed
  commits in *both* repos.
- **Key IDs and rotation.** `gpg --list-secret-keys --keyid-format=long
  vlad.yurchina@gmail.com` shows the current signing key. To rotate: generate a
  new key the same way, `git config --global user.signingkey <new-fingerprint>`,
  add the new public key to GitHub, then revoke the old one there.
- **Why this host's key is different from the laptop's.** Independent keypairs,
  not shared private material — a compromise of this VPS's root doesn't expose
  the laptop's signing identity, and either host's GitHub-side key can be
  revoked without touching the other.

---

## Rotating the Basic Auth password

```bash
GEN_PASSWORD=$(openssl rand -base64 24)
htpasswd -b /etc/nginx/.htpasswd admin "$GEN_PASSWORD"   # no -c: update the entry, don't recreate the file
echo "New Basic Auth password: $GEN_PASSWORD"
systemctl reload nginx
```
No dashboard restart is needed — nginx re-reads `.htpasswd` per request.

---

## Cold-start / redeploy procedure

1. `ssh -i ~/.ssh/id_ed25519_deploy root@167.71.201.113`
2. `cd /root/polymarket-forecast-lab && git pull`
3. `uv sync --group dashboard`
4. `systemctl daemon-reload` (only if a unit file changed)
5. `systemctl restart lab-run.service lab-dashboard.service`
6. Confirm: `systemctl status lab-run.service lab-dashboard.service`, then
   `curl -I https://167-71-201-113.sslip.io` (expect `401` without credentials,
   `200` with `-u admin:<password>`).

---

## Pointer back to the Windows-side runbook

Windows Scheduled Tasks, the PAUSE file, the key-rotation table, and everything
specific to running this same pipeline on the laptop (now the secondary/
stand-by host) live in [`docs/OPERATIONS.md`](OPERATIONS.md). This document
covers what is specific to this VPS host.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-10 | Initial VPS dashboard exposure: nginx + Let's Encrypt + HTTP Basic Auth on `167-71-201-113.sslip.io`, `lab-dashboard.service` added alongside the pre-existing `lab-collect.service`. |
| 2026-07-10 | pmxt scan+verify cycle moved here (sole owner): `pmxt-scan.timer`/`pmxt-verify.timer` added; laptop's `PolymarketForecastLabPmxtScan` task disabled. |
| 2026-07-10 | `results-pull.timer` added: hourly `git pull --ff-only` of `/root/forecast-lab-results`, so this host holds an independent, recent local copy of the actual experiment results if the laptop's orchestrator ever stops. |
| 2026-07-10 | **Cutover to primary**: laptop's `lab.db` (fuller forecast/eval history) copied here, replacing this host's stale one; `lab-collect.service` disabled and replaced by `lab-run.service` (full orchestrator); `results-pull.timer` disabled (role reversed — this host now pushes to `forecast-lab-results` via its own nightly `run_publish_job`); laptop's own push disabled locally to avoid a git race during the parallel-verification window. Two real bugs found and fixed on the spot: `publish.results_dir`'s relative path resolved wrong on this host (local override to an absolute path); this checkout had no git committer identity configured at all, silently failing every commit-based job (`git config user.name`/`user.email` set on both repo checkouts). Both confirmed fixed via direct live invocation, not just "no error logged." |
| 2026-07-10 | **Public-repo push access fixed**: found this host had no way to push to the public `polymarket-forecast-lab` repo at all (plain HTTPS origin, no credential helper) — a gap that would have silently stranded every `pmxt_verify`/`ledger_commitment`/`paper_export` commit made here. Fixed with a new `id_ed25519_public_repo` deploy key (write access) plus a `github.com-public` SSH config alias, confirmed with a real push. |
| 2026-07-10 | **Personal git identity added**: new personal-account SSH key (`id_ed25519_personal_github`) and GPG signing key generated on this host (and, independently, on the laptop); `commit.gpgsign`/`tag.gpgsign` enabled globally. Confirmed real commits to both the public and private results repos show `"verified": true` via the GitHub API. |
| 2026-07-13 | **Laptop retired; the three-day-over parallel window's cost paid off in full.** Both hosts' `lab.db` had quietly diverged since the 07-10 cutover (each independently forecasting/resolving), which surfaced as two concrete problems on `git pull` here: (a) `docs/ledger_commitments.jsonl` had two non-reconciled entries for the same calendar date on three separate days (07-10/11/12) — resolved by keeping every entry from both sides (append-only discipline forbids picking a "winner" or silently rewriting either), so the file now honestly shows both hosts' independent commitments during the overlap; (b) the laptop's own local, never-commit `publish.enabled: false` override had leaked into a real commit (`26ccd36`, made from the laptop) — caught while merging this host's own pending commits back in, fixed in `6fbbab4` before this host pulled either. Also live-verified for the first time: this host's `lab-run.service` config also picked up `26ccd36`'s `m3_boundary_randomization_enabled: true` and the `eval/run.py`/`fee_schedule.yaml` changes cleanly through this same merge, then a `systemctl restart lab-run.service` (confirmed healthy: normal snapshot activity within seconds, `git rev-list` 0/0 against origin). Final `lab status` comparison ahead of retirement: this host 0 gaps in 24h on both tiers vs. the laptop's 145 (liquid) / 7 (tail) — the always-on host had already become the more reliable one, independent of the divergence bugs above. The laptop's final divergent state (a few hundred extra forecast rows unique to it) was backed up to the private results repo's own `laptop-final-snapshot-2026-07-13` branch, not merged into `main` — see `docs/OPERATIONS.md`'s retirement entry for why. One step remains outside any automated session's reach: the laptop's `PolymarketForecastLabWatchdog(Hourly)` scheduled tasks need `Unregister-ScheduledTask` from an **elevated** PowerShell to actually stop existing; until an operator runs that by hand, `data\PAUSE` on the laptop is the only thing stopping the hourly watchdog from resurrecting a now-pointless orchestrator there. |
| 2026-08-05 | **`lab learn` moved out of the orchestrator into its own `lab-learn.service` + `lab-learn.timer`** (monthly, 1st 04:00 UTC, dry-run, `MemoryMax=900M`). Cause: after the `database is locked` storm was fixed, learn still died through the orchestrator — this time genuinely OOM-killed, because a child process does not leave its parent's cgroup, so its peak was charged to `lab-run.service`'s budget on top of the collector's. Running it out-of-process was never memory isolation, only crash isolation. Same pattern `pmxt-scan.timer` has always used. Measuring it in its own cgroup then exposed two real defects, both fixed in code rather than configured around: the bootstrap training set was expanded into 1,967,376 Python dicts (~1.8 GB), and `estimate_rho_bar_m7` read 90 days of snapshots unprojected, order-book blobs included (>1.3 GB). Peak went 1747 MB (+1666 MB swap, still dying) → 900 MB, no swap, 105 s. Also in this change: the weekly report moved 06:00 → 14:00 UTC (it had been sitting inside the ≥ 5 h margin behind the nightly bundle that the spacing test requires, which had simply never checked `report_cron`); `pmxt-verify.timer` documented as disabled (the orchestrator schedules that pass itself at 17:00/21:00). Full writeup in `docs/OPERATIONS.md`. |
| 2026-08-16 | **Weekly jobs were firing a day late.** APScheduler's `from_crontab` reads day-of-week 0 as MONDAY, the opposite of standard cron, so `paper_export` and `report` had been running Mondays and `map_propose` Tuesdays while the config said Sunday and Monday — confirmed against the live stamps, not inferred. Weekdays are now names (`sun`, `mon`); hours unchanged, so the ordering rationale still holds. Also this week: `lab learn` given its own unit and timer after being OOM-killed inside the orchestrator's cgroup; the instance guard stopped classifying every `uv run lab <subcommand>` as a duplicate orchestrator (it would have killed the monthly learn run); Kalshi tiering moved onto measured order-book depth; and the collector survived 5 days 3 hours unattended with zero restarts and zero OOM events — the longest clean run on this host. |
| 2026-08-18 | **Results-repo LFS quota blown; db backup cadence cut to weekly, and a rejected ledger push unstuck.** Measured on the private `forecast-lab-results` mirror: **10.8GB of Git LFS storage across 131 objects** (1.56GB in HEAD alone) against GitHub free-tier allowances of 1GB storage and 1GB/month bandwidth. Cause is entirely `data/lab.db` -- now **1.08GB** and pushed whole every three days, so one push exceeds the whole monthly bandwidth on its own and every previous copy is stored forever (pushed sizes: 451 -> 534 -> 652 -> 835 -> 929 -> 1000 -> 1082MB). `publish.raw_data.db_interval_days` 3 -> 7 at the operator's direction; snapshots, exports, reports and `.env` unchanged. This slows the growth (~10 pushes/month -> ~4.3), it does not stop it: LFS keeps every version and has no delta compression, so the db needs a deduplicating store (restic-class) rather than a longer interval, and the already-stored 10.8GB will not be reclaimed by a history rewrite -- GitHub does not garbage-collect LFS objects. `config.yaml`'s own rationale comment, which claimed this interval made the db fit a free-tier LFS plan, was true when the db weighed hundreds of MB and is corrected in place. **Separately, found in the same pass:** that night's `ledger_commitment` push had been REJECTED (`! [rejected] master -> master (fetch first)`) because this checkout sat one commit behind the public repo after a docs push from the laptop, leaving the 2026-08-17 commitment committed-but-unpublished -- precisely the state `config.yaml` calls worthless ("an unpushed commitment on a public repo verifies nothing"). Rebased (2 local commits over 1 remote, clean) and pushed. Worth knowing this failure mode is silent: the job logs `pushed: false` and returns normally, and the next night's run retries, so it self-heals only if someone pulls. |
| 2026-08-18 | **Kalshi snapshot rounds split per tier and made concurrent; M4's two-pass eligibility bug fixed.** The venue-wide Kalshi round took ~60 min for 3,966 markets + 461 order books, so it overran its own 30-min interval (APScheduler dropped every second firing) and the liquid tier ran at the tail's cadence — against guardrail 13's 15-min liquid bound, which left 443 of 464 liquid Kalshi markets failing the freshness gate at forecast time every night. Rounds were latency-bound, not rate-bound (1.2 req/s against a ceiling of 8, every await sequential), so `snapshot_markets`/`snapshot_kalshi_markets` gained bounded concurrency and `job_kalshi_snapshot` became `job_kalshi_snap_liquid` (5 min) + `job_kalshi_snap_tail` (30 min). The token bucket still caps the rate — guardrail 8 is untouched. **Host-load note:** Kalshi now sustains ~8 req/s in bursts of ~2 min every 5 min instead of a flat 1.2 req/s; watch `lab-run`'s cgroup if its steady RSS moves. Polymarket has the same mechanism at `collect.snapshot_concurrency: 1` — unchanged behaviour — pending a decision on its own starved tail (~7-hour realized cadence, 831 tail markets with no snapshot at all). Also fixed: `run_forecast_job`'s ensemble pass re-derived eligibility 13-18 min after the base pass, silently emptying M4 on 08-11/14/16. PAP addenda 9.17-9.19 record all of it. |
| 2026-08-18 | **The collector was discarding most of its own scheduled firings.** APScheduler's default `misfire_grace_time` is 1s; a snapshot round runs back-to-back requests for minutes, so the loop is busy essentially always and firings were dropped rather than delayed. Measured over 10h45m: **146 liquid rounds against exactly ONE tail round** (~11 scheduled), with misfire warnings for every collector job. Effect at forecast time: Polymarket tail median snapshot age 410 min against a 90-min bound, 1,508 of 2,393 tail markets failing it, 844 with no snapshot at all. Every collector interval job now routes through `_add_interval_job` (grace = one full period, `coalesce`, `max_instances=1`). Note this is the SAME defect the 2026-07-14 audit fixed for `health_check` alone — if you add a scheduled job here, do not call `scheduler.add_job` directly. |
| 2026-08-18 | **Pushes to the public repo now recover from the collision they hit routinely.** Two hosts write to `polymarket-forecast-lab` by design (this VPS commits ledger commitments, pmxt proposals and paper exports; the laptop pushes docs), so a non-fast-forward rejection is the normal case. It happened twice on this date alone — a ledger commitment at 02:44 and a paper-export snapshot at 11:05 — and both jobs logged `pushed: false` and returned normally, leaving a cryptographic pre-registration committed but unpublished. New `lab/gitutil.py`'s `push_with_rebase` retries once behind `git pull --rebase --autostash` (autostash is required: `data/markets_map.yaml` is routinely modified-but-uncommitted here by design). Wired into `ledger_commitment.py`, `paper_export.py` and `m7_crossvenue.py`. **Deliberately NOT wired into `publish.py`** — the private results mirror is LFS-backed, a rebase there fires the smudge filter and pulls objects against an already-blown 1GB quota, and only this host pushes to it. If you ever hit a manual rejection here, the same recipe works by hand: stash, `git pull --rebase`, restore, push. |
| 2026-08-22 | **Public repo renamed** `polymarket-forecast-lab` -> `prediction-market-forecast-lab`. The old name understated the study and had become inaccurate: Kalshi now contributes more forecast markets per day than Polymarket (2,210 vs 1,515 on 08-19), and skill is scored against each venue's OWN price, never a single venue's. GitHub redirects the old URL, but both hosts' `origin` were repointed explicitly rather than left on the redirect. **Changelog rows above this one keep the old name on purpose** — they record what was true when written. The checkout path `/root/polymarket-forecast-lab` is UNCHANGED and must stay so: every systemd unit's `WorkingDirectory` depends on it, and renaming the directory would break the collector, the learn timer and the pmxt timers at once. |
| 2026-08-22 | **Backup audit, and the LFS rule changed to "only what plain git will not take".** The audit found the nightly path healthier than expected — every snapshot partition present in the mirror with byte-identical sizes, `.env.backup` sha256-identical to `.env`, the mirror fully pushed and `git lfs push --dry-run` showing zero objects awaiting upload — and two real gaps. **First, the M1/M1.x training set was never backed up at all**: `publish.py` had never mentioned `bootstrap`, so the mirror held a 594KB predecessor hand-committed on 07-08 while every refit since 08-02 (n_train 1,967,376) fitted on a 50MB file with no off-host copy. New `sync_bootstrap` mirrors it with the curated artifacts. **Second, the ledger's recovery point is 7 days** — a consequence of the 08-18 db_interval_days 3→7 change, quantified here for the first time: 90,767 forecast rows, 9,595 resolutions, 8,181 wealth_ledger rows existed only on this host at audit time. Snapshots have a 1-day RPO, so the *irreplaceable* append-only ledger is the worst-protected thing in the system; an incremental nightly ledger dump would fix it at a few MB a night and is NOT built. **LFS policy:** of 54 LFS objects in HEAD exactly one was over GitHub's 100MB hard limit (`data/lab.db`, 1,126MB); the other 53 totalled 519MB with a 20MB maximum. `*.parquet` therefore left LFS and existing objects were renormalized into ordinary git. This stops adding to the 10.8GB already stored — it does not reclaim it, GitHub does not garbage-collect LFS objects. The mirror's own `.gitattributes` is the authoritative statement of the rule and carries the measurement behind it. Cost, stated: snapshots now grow ordinary git history ~19MB/day, reaching GitHub's 5GB *soft* limit in roughly nine months — an email, not a rejection, and no bandwidth meter. |
| 2026-08-22 | **`/tmp` here is RAM, and the test suite can fill it.** Running the suite on this host wrote ~1GB into `/tmp`, which is a 987MB tmpfs — so it consumed RAM on a 1973MB box, drove `shared` to 874MB and free memory to 119MB, and made SQLite fail with `database or disk is full` mid-run. Cause was a defect committed the same hour: `sync_bootstrap` took its source from `PROJECT_ROOT` instead of config, so all 19 publish tests copied the real 50MB training set. Fixed by making it `storage.bootstrap_dir` like every other path — the suite went from 80s to 9.8s, which is the measurement that confirms it. **Standing rule for this host:** clear `/tmp/pytest-of-root` after running tests, or run them with `TMPDIR` on disk (`TMPDIR=/root/tmp uv run pytest`). A full tmpfs starves the collector, and `PrivateTmp=no` on `lab-run` means it shares the same one. |
| 2026-08-22 | **Nightly ledger increment closes the seven-day recovery point.** `data/lab.db` goes weekly, so the one irreplaceable artifact here had a 7-day RPO while the snapshots describing it had a 1-day one — measured five days after a push: 90,767 forecast rows, 9,595 resolutions and 588 evidence runs existed only on this host. `sync_ledger_increment` now writes one gzipped JSONL per **closed** UTC day per append-only table into the mirror's `ledger/` tree, in ordinary git (no LFS, `.jsonl.gz` is not in `.gitattributes`). **Stateless on purpose**: which days are mirrored is read from the files, never from a `meta` watermark that a restore or a db rollback would silently desynchronise. Newest-first and bounded per run, so the RPO drops immediately and history backfills behind. A pure reader — a test asserts it writes nothing to the database at all, since `forecasts` is guarded by an authorizer that denies UPDATE/DELETE. **Restore:** the files are plain JSONL, one row object per line with every column, so a table is rebuilt by reading them in date order and INSERTing — that is the whole procedure, and it does not need this codebase. `eval_runs` and `wealth_ledger` are deliberately absent: both are recomputable from what is here plus `markets`. |
| 2026-08-25 | **Four reliability changes, in the order they protect the paper.** H1's power now sits entirely in ~1,182 already-forecast clusters waiting to resolve before the freeze, so the job until 2026-12-31 is keeping collection intact rather than adding anything. **(1) Coverage-regression watchdog** (`status.coverage_regressions`): each (model, venue) series compared against its OWN trailing median, flagged below 50%, logged at ERROR from the nightly bundle and shown in `lab status`. Replayed over the real August incidents it flags the Kalshi blackout on **day one** rather than day six. **(2) httpx/httpcore quieted to WARNING** — ~507,000 lines/day had been holding journald's 300MB cap to ~16 hours of retention, which cost real diagnosis three times during the audit week. **(3) Batched write transactions**: the forecast pass held one lock 02:00-02:20 and eval 02:21-02:50, so collector jobs firing inside those windows died with `database is locked` (seven in one night). Now 250 rows per commit in `run_forecasts` and one transaction per model in `run_eval` — which also means an interrupted run keeps what it wrote, suiting an append-only ledger. **(4) `journal_size_limit=64MB`**: SQLite never shrinks the -wal file on its own, and one burst of heavy writing had left 1,195MB of WAL beside a 1,302MB database. |
| 2026-08-25 | **Batched commits checked against the backup invariant, and the ledger dump made verifiable.** Both `ledger_commitment.py` and the nightly increment rest on one assumption: **a closed UTC day never gains rows**. Batching changed the crash case from "lose the whole run" to "keep a committed prefix", so that assumption was re-checked rather than carried forward — measured across every committed day, **zero** gained rows after their commitment, and recent days show exactly 3 distinct forecast timestamps (base pass, M6, M7). A same-day retry completes the day via `_due`; the next day's run stamps its own date. The invariant holds. What was genuinely missing: the increment dump recorded nothing, so a stale file was indistinguishable from a current one. It now writes `ledger/manifest.jsonl` (row count + sha256 per day, appended never rewritten) and `verify_ledger_increment` checks both — separating `digest_mismatch` (the file is not the file that was written) from `row_count_mismatch` (a closed day moved, i.e. the invariant broke). |
| 2026-09-07 | **Kalshi stopped snapshotting sports markets outside the null-control sample** (PAP 9.26). The football season listed **13,290 new Kalshi sports markets since 08-20**, taking sports to **11,625 of 16,673 markets in the snapshotted tiers (69.7%)**. The tail round grew to ~16,050 markets and took **~86 minutes against a 30-minute interval**, so APScheduler skipped two firings in three (`max_instances=1`) and the realized grid sat exactly on guardrail 13's 90-minute bound. Result: the 02:00 forecast pass skipped **4,959 markets on stale prices**, and Kalshi's daily coverage fell from ~2,400 distinct markets to **10-33** on 08-30 and 09-02..09-07. None of the starved markets were sports — the eligibility filter drops those first — so the budget was being spent on the population §3 excludes, at the expense of the one under study. The round now skips unsampled sports (~4,400 markets, ~24 min). **Read `lab status` accordingly:** the tier line now prints `tracked=N (snapshotted=M, unsampled_sports=K)` — `tracked` is no longer the working set, and a large `unsampled_sports` is expected, not a fault. Reversible with `universe.null_control.snapshot_unsampled: true`. **Two things this does NOT fix, both measured today.** (1) The **liquid** round takes ~10 minutes against its 5-minute interval (1,071 markets x 2 requests at ~3.6 req/s, latency-bound below the 8 req/s ceiling); after this change ~730 markets, ~7 minutes — still over, still inside the 15-minute freshness bound, with little margin. (2) The **universe sync is diluted by the same flood**: known Kalshi series went from ~285 to **827** (198 of them sports), and at `max_series_per_sync: 40` a full rotation now takes **~21 hours instead of ~9**, with cycles returning `markets_seen: 0` for hours at a time. Stale `active`/`closed`/`end_date_iso` flags are what caused the six-day blackout of PAP 9.17, so this one is worth watching: `grep 'kalshi universe sync complete' data/logs/lab.jsonl \| tail`, and `forecast: skipped markets already past their end date` in the nightly bundle (156 today, and it is the number to watch grow). |
| 2026-09-07 | **Measured after the change above, same day — including one prediction it falsifies.** Kalshi rounds on the new code: **tail 4,551 markets** (12,233 unsampled sports skipped), 15:14:49→15:42:15 = **27m26s** and 15:44:53→15:59:15 = **14m22s**, both inside the 30-minute interval against ~86 minutes and two skipped firings in three before it. **Liquid 722-723 markets** (348-354 skipped; the count moves because the sample rotates as markets close), **4m39s** — so the liquid round now fits its 5-minute interval too, which the row above predicted it would NOT (`~7 minutes, still over`). That prediction was wrong in the safe direction and is corrected here rather than quietly. A second tail round-done line 32 seconds after the first, with `written: 0`, is the snapshot writer's own (ts_bucket, condition_id) dedup (guardrail 7) absorbing a coalesced firing — no data effect. Still unverified at the time of writing: the forecast pass runs at 02:00 UTC, so whether `skipped markets with stale snapshots` falls from 4,959 and Kalshi's daily coverage returns to ~2,400 distinct markets is visible only on the next nightly bundle. **The universe-sync dilution is unchanged and remains the thing to watch** (827 known series, ~21h rotation) — it is not addressed by this change. |
| 2026-09-08 | **The Kalshi universe sync was stalled, not slow — rotation cursor added** (PAP 9.27). 837 series known, 606 carrying open markets, and hours-since-sync over those 606 measured **p50 383, p90 770, max 1,199** against a configured ~26-hour rotation. Cause: `_series_sync_order` ordered on `MAX(last_synced_ts)` from market rows, but a series returning no OPEN markets upserts nothing, so its key never moved, it stayed oldest, and a stable sort handed back the same head every cycle — 231 dead series against ~32 slots pinned the queue (hence `markets_seen: 0` at 12:05, 13:05 and 14:05 on 09-07). Now a `kalshi_series_sync` table stamps every series the sync *walks* — success, empty, or fetch failure — and the rotation orders on that; the known/unseen partition deliberately still comes from the markets table (re-keying it would drag ~10,700 dormant series into `known`, the failure the function's own docstring records). `SCHEMA_VERSION` 13 → 14. **Two companion wedges of the same class:** `unresolved_kalshi_markets` had `LIMIT` with no `ORDER BY` and its watcher stamped nothing (July's Gamma lesson, never applied here); and `unresolved_closed_markets` was never narrowed to Polymarket after Phase 10 — measured 961 Manifold + 48 Kalshi rows in a 200-row cycle, two wasted Gamma requests each, and it was stamping `resolution_checked_ts` on them, so the global `oldest_check_age_h` could look healthy on Gamma's stamps while another venue's watcher was wedged. **`lab status` gains what nothing could previously see:** `last_synced_ts` had exactly one reader in the whole codebase — the sync's own ordering — so a 383-hour stall was invisible. There is now a per-venue watcher block (`backlog`, `never_checked`, `oldest_check`, `active_past_end`) and a Kalshi `sync rotation` line (p50/p90/max age, cursor rows, series empty 3+ cycles), plus `cycle_seconds` in the sync's log line — load-bearing, because `misfire_grace_time` is 3600 with `max_instances=1`, so a sync overrunning its hour runs late and silently halves the rotation just restored. **Expect on deploy:** the Polymarket `backlog` figure drops by ~1,009 (the number becoming true, not the backlog draining), and `oldest_check_age_h` drifts up for a sweep or two until the fixed Kalshi watcher takes over its own stamping. **Verify at +30h:** `uv run lab status` → Kalshi sync-rotation p50 < 26 and max < 30; no three consecutive cycles with `markets_seen: 0`; `cycle_seconds` well under 3600; `grep 'past their end date' data/logs/*.jsonl` below 156. **Not done deliberately:** `max_series_per_sync` stays 40 (a guardrail-8 decision needing the new `cycle_seconds` first), and the discovery half of the rotation is wedged on a venue-supplied key — fixing it would sweep ~10,700 dormant series and expand the universe, so it is measured and left alone. |
| 2026-09-08 | **The weekly paper export outgrew GitHub's 100 MB per-file limit and had blocked ALL publication from this host** (PAP 9.28). `docs/paper_exports/2026-09-06.jsonl` reached 115 MB; the pre-receive hook scans every blob in the pushed range, so one oversized file refused the whole push and left three commits stuck — including the **2026-09-06 ledger commitment**, which `config.yaml` itself says verifies nothing while unpushed. It failed silently because `gitutil._is_non_fast_forward` matches `[rejected] … (fetch first)`, and a hook rejection is a different class. From 2026-09-13 the snapshot is written as `YYYY-MM-DD.jsonl.gz` (streaming, atomic via a `.tmp` + rename, `mtime=0` so identical rows give identical bytes); measured 16.5× on the 08-30 file (96,133,567 → 5,823,204 B). `lab export --paper --out <path>` still writes plain JSONL unless the path ends in `.gz`. **Unblocking the stuck range — the file must leave the COMMIT, not the working tree:** `cd /root/polymarket-forecast-lab && git log --oneline -4` (confirm the three unpushed commits), then `git rebase --onto 5ae68dc^ 5ae68dc` to drop that one commit while keeping the markets-map and ledger commits with their own identities (a `reset --soft` would collapse all three and destroy the ledger commitment's commit), then `git pull --rebase --autostash && git push`. Verify BEFORE pushing that no oversized blob remains in the range: `git rev-list origin/master..HEAD \| git cat-file --batch-check='%(objectsize) %(rest)' --batch-all-objects \| sort -rn \| head` — or simply `git diff --stat origin/master..HEAD`. Rollback is the reflog (`git reflog`, `git reset --hard HEAD@{n}`); nothing is lost because every export is a cumulative superset, so the dropped 2026-09-06 file is fully contained in the next one. **Never force-push this repo** — the surgery is on unpushed commits only, and published history through `2026-08-30` must stay byte-for-byte as it is. |
| 2026-09-08 | **Rotation dedup, found by watching the first cycle after the cursor shipped.** That cycle logged `series: 40` but wrote only **33** rows to `kalshi_series_sync`: a series returned under more than one configured category appeared twice in the candidate list, so ~17% of an hourly budget was spent fetching the same series again — and the ~26-hour rotation arithmetic in PAP 9.27 assumes 40 distinct series a cycle, not 40 slots. `_series_sync_order` now deduplicates candidates (first occurrence wins) before partitioning, so the cap budgets series. Also confirmed healthy on that cycle: `cycle_seconds: 61` against a 3600-second interval, so the sync has ample headroom and cannot run late under `max_instances=1`. |
