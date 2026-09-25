"""Long-running collection process: APScheduler driving sync/snapshots/resolutions.

Every job checks the PAUSE kill file first, so polling halts within one cycle
of the file appearing (guardrail 8).

`run_collect` runs collection only. `run_orchestrator` (the one-button entry
point) additionally schedules the analytics jobs -- forecast/eval, report,
shadow -- on the same event loop.

`lab learn` deliberately is NOT among them: it owns a separate systemd unit
and timer (see docs/VPS_OPERATIONS.md). A child process does not leave its
parent's cgroup, so running the monthly loop from here made it share the
collector's memory budget -- on 2026-08-05 it was OOM-killed at 84s doing
exactly that, while a standalone run of the same job finished in 101s. Batch
work with its own footprint gets its own budget, the way the pmxt scan
already does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from lab.api.clob import ClobClient
from lab.api.gamma import GammaClient
from lab.api.http import TokenBucket
from lab.api.kalshi import KalshiClient
from lab.api.manifold import ManifoldClient
from lab.api.metaculus import MetaculusClient
from lab.collect.kalshi_collector import (
    snapshot_kalshi,
    snapshot_kalshi_markets,
    sync_kalshi_universe,
    tracked_kalshi_markets_by_ids,
    watch_kalshi_resolutions,
)
from lab.collect.manifold_collector import sync_manifold_markets
from lab.collect.metaculus_collector import snapshot_metaculus, watch_metaculus_resolutions
from lab.collect.resolutions import watch_resolutions
from lab.collect.snapshots import snapshot_markets, snapshot_tier, tracked_markets_by_ids
from lab.collect.universe import sync_universe
from lab.heartbeat import send_heartbeat
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import PROJECT_ROOT, now_utc, now_utc_iso

log = logging.getLogger(__name__)


def pause_file_path(config: dict[str, Any]) -> Path:
    p = Path(config["collect"]["pause_file"])
    return p if p.is_absolute() else PROJECT_ROOT / p


def _runtime_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["db_path"])
    d = (d if d.is_absolute() else PROJECT_ROOT / d).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def heartbeat_path(config: dict[str, Any]) -> Path:
    """File the orchestrator touches every loop; the watchdog reads its mtime."""
    return _runtime_dir(config) / "orchestrator.heartbeat"


def pid_path(config: dict[str, Any]) -> Path:
    return _runtime_dir(config) / "orchestrator.pid"


def _write_heartbeat(config: dict[str, Any]) -> None:
    from lab.util import now_utc_iso

    try:
        heartbeat_path(config).write_text(now_utc_iso(), encoding="utf-8")
    except OSError:
        log.warning("could not write heartbeat file")


def is_paused(config: dict[str, Any]) -> bool:
    if pause_file_path(config).exists():
        log.warning("PAUSE file present -- skipping cycle")
        return True
    return False


async def snapshot_matched_pairs(clob: ClobClient, kalshi: KalshiClient, conn, store: SnapshotStore,
                                config: dict[str, Any], markets_map_data: dict[str, Any] | None = None,
                                ) -> dict[str, int]:
    """Phase 17 item 3: confirmed cross-venue pairs only, at a much tighter
    cadence than the tier-wide loops -- the lead-lag hypothesis (PAP H3) is
    underpowered on a 5-min grid, and finer history can't be captured
    retroactively. Metaculus pairs get Polymarket-side HF only -- its
    community-prediction poll isn't an order-book snapshot and has no
    per-market fetch to reuse here. `markets_map_data` lets callers (tests)
    inject a fixture map directly instead of reading data/markets_map.yaml.
    """
    from lab.models.m7_crossvenue import load_markets_map

    data = markets_map_data if markets_map_data is not None else load_markets_map()
    confirmed = data.get("confirmed", [])
    counts = {"poly_written": 0, "kalshi_written": 0}
    if not confirmed:
        return counts

    poly_ids = sorted({e["condition_id"] for e in confirmed})
    kalshi_ids = sorted({
        db.venue_condition_id("kalshi", e["external_id"])
        for e in confirmed if e.get("venue") == "kalshi"
    })

    bucket_minutes = config["cross_venue"]["hf_snapshot_interval_minutes"]
    ts_bucket = floor_ts_bucket(now_utc(), bucket_minutes)
    depth_levels = config["collect"].get("book_depth_levels", 10)

    poly_markets = tracked_markets_by_ids(conn, poly_ids)
    if poly_markets:
        counts["poly_written"] = await snapshot_markets(clob, store, poly_markets, ts_bucket, depth_levels)

    if kalshi_ids:
        kalshi_markets = tracked_kalshi_markets_by_ids(conn, kalshi_ids)
        if kalshi_markets:
            counts["kalshi_written"] = await snapshot_kalshi_markets(kalshi, store, kalshi_markets, ts_bucket)

    log.info("matched-pair HF snapshot done", extra={"ctx": {
        "pairs": len(confirmed), "poly_markets": len(poly_markets) if poly_ids else 0,
        **counts,
    }})
    return counts


@dataclass
class CollectContext:
    """Owns the shared clients/DB connection and the collection job callables."""

    gamma: GammaClient
    clob: ClobClient
    conn: Any
    store: SnapshotStore
    jobs: dict[str, Callable[[], Awaitable[None]]]
    # Phase 10: one client per external venue, each on its own TokenBucket
    # (per-host rate limiters, not shared with Polymarket's bucket object).
    kalshi: KalshiClient | None = None
    metaculus: MetaculusClient | None = None
    manifold: ManifoldClient | None = None

    async def aclose(self) -> None:
        await self.gamma.aclose()
        await self.clob.aclose()
        for client in (self.kalshi, self.metaculus, self.manifold):
            if client is not None:
                await client.aclose()
        self.conn.close()


def _add_interval_job(scheduler, fn, minutes: float, **kw) -> None:
    """Register a collector interval job that cannot silently lose a firing.

    APScheduler's default `misfire_grace_time` is ONE SECOND: a firing whose
    scheduled moment passes while the event loop is busy is discarded, not
    delayed. The collector's loop is busy essentially all the time (a snapshot
    round runs back-to-back requests for minutes), so this was throwing away
    nearly every low-frequency firing. Measured over 10h45m on 2026-08-18:
    `job_snap_tail` completed ONE round against ~11 scheduled, with a misfire
    warning for each of the rest -- which is why the Polymarket tail's realized
    cadence was ~7 hours against a configured 60 minutes, why 1,508 of 2,393
    tail markets sat past guardrail 13's freshness bound at forecast time, and
    why 844 of them had no snapshot at all. Every other collector job was
    losing firings the same way.

    This is the same defect v2.11 fixed for the analytics cron jobs; the
    collector's own interval jobs were left on the default.

    Grace is one full period: a firing may run late, but never later than its
    own successor, and `coalesce` collapses a backlog into a single run rather
    than replaying it.
    """
    kw.setdefault("max_instances", 1)
    kw.setdefault("coalesce", True)
    kw.setdefault("misfire_grace_time", max(60, int(minutes * 60)))
    scheduler.add_job(fn, "interval", minutes=minutes, **kw)


def register_collect_jobs(scheduler: AsyncIOScheduler, config: dict[str, Any]) -> CollectContext:
    """Build shared resources and register the collection interval jobs."""
    bucket = TokenBucket(
        rate=config["collect"]["rate_limit"]["requests_per_second"],
        burst=config["collect"]["rate_limit"]["burst"],
    )
    gamma = GammaClient(bucket)
    clob = ClobClient(bucket)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])

    async def job_sync() -> None:
        if not is_paused(config):
            await sync_universe(gamma, conn, store, config)

    async def job_snap_liquid() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "liquid", config)

    async def job_snap_tail() -> None:
        if not is_paused(config):
            await snapshot_tier(clob, conn, store, "tail", config)

    async def job_resolutions() -> None:
        if not is_paused(config):
            await watch_resolutions(
                gamma, conn, limit=config["collect"].get("resolution_backlog_limit", 200)
            )

    cadence = config["collect"]["snapshot_interval_minutes"]
    _add_interval_job(scheduler, job_sync, config["universe"]["sync_interval_minutes"])
    _add_interval_job(scheduler, job_snap_liquid, cadence["liquid"])
    _add_interval_job(scheduler, job_snap_tail, cadence["tail"])
    _add_interval_job(scheduler, job_resolutions, config["collect"]["resolution_poll_minutes"])

    # Phase 18: unconditional, unlike the 4 jobs above -- deliberately does NOT
    # check is_paused(). The heartbeat's whole point is proving the process/
    # event-loop itself is alive; if it went silent only because the operator
    # deliberately dropped data/PAUSE for maintenance, the operator should NOT
    # get a false "collector is dead" alert. So it pings every tick, PAUSE or not.
    async def job_heartbeat_ping() -> None:
        await send_heartbeat("collector")

    _add_interval_job(scheduler, job_heartbeat_ping,
                      config.get("ops", {}).get("heartbeat_interval_minutes", 5),
                      id="heartbeat_ping")

    # --- Phase 10: external-venue collectors, each on its own TokenBucket ---
    venues_cfg = config.get("venues", {})

    kalshi_bucket = TokenBucket(rate=venues_cfg["kalshi"]["rate_limit"]["requests_per_second"],
                               burst=venues_cfg["kalshi"]["rate_limit"]["burst"])
    kalshi = KalshiClient(kalshi_bucket)

    metaculus_bucket = TokenBucket(rate=venues_cfg["metaculus"]["rate_limit"]["requests_per_second"],
                                   burst=venues_cfg["metaculus"]["rate_limit"]["burst"])
    metaculus = MetaculusClient(metaculus_bucket)

    manifold_bucket = TokenBucket(rate=venues_cfg["manifold"]["rate_limit"]["requests_per_second"],
                                  burst=venues_cfg["manifold"]["rate_limit"]["burst"])
    manifold = ManifoldClient(manifold_bucket)

    async def job_kalshi_sync() -> None:
        if not is_paused(config):
            await sync_kalshi_universe(kalshi, conn, config, store)

    # Split per tier on 2026-08-18. One venue-wide job meant the liquid tier
    # inherited the tail's cadence -- a ~60-minute round against guardrail 13's
    # 15-minute liquid freshness bound, so 443 of 464 liquid Kalshi markets
    # were dropped at forecast time every night.
    async def job_kalshi_snap_liquid() -> None:
        if not is_paused(config):
            await snapshot_kalshi(kalshi, conn, store, config, tier="liquid")

    async def job_kalshi_snap_tail() -> None:
        if not is_paused(config):
            await snapshot_kalshi(kalshi, conn, store, config, tier="tail")

    async def job_kalshi_resolutions() -> None:
        if not is_paused(config):
            await watch_kalshi_resolutions(kalshi, conn)

    async def job_metaculus_snapshot() -> None:
        if not is_paused(config):
            await snapshot_metaculus(metaculus, conn, store, config)

    async def job_metaculus_resolutions() -> None:
        if not is_paused(config):
            await watch_metaculus_resolutions(metaculus, conn)

    async def job_manifold_sync() -> None:
        if not is_paused(config):
            await sync_manifold_markets(manifold, conn, config)

    async def job_snap_matched() -> None:
        if not is_paused(config):
            await snapshot_matched_pairs(clob, kalshi, conn, store, config)

    _add_interval_job(scheduler, job_kalshi_sync, venues_cfg["kalshi"]["sync_interval_minutes"])
    kalshi_cadence = venues_cfg["kalshi"]["snapshot_interval_minutes"]
    _add_interval_job(scheduler, job_kalshi_snap_liquid, kalshi_cadence["liquid"])
    _add_interval_job(scheduler, job_kalshi_snap_tail, kalshi_cadence["tail"])
    _add_interval_job(scheduler, job_kalshi_resolutions,
                      venues_cfg["kalshi"]["resolution_poll_minutes"])
    _add_interval_job(scheduler, job_metaculus_snapshot,
                      venues_cfg["metaculus"]["snapshot_interval_minutes"])
    _add_interval_job(scheduler, job_metaculus_resolutions,
                      venues_cfg["metaculus"]["resolution_poll_minutes"])
    _add_interval_job(scheduler, job_manifold_sync,
                      venues_cfg["manifold"]["sync_interval_minutes"])
    _add_interval_job(scheduler, job_snap_matched,
                      config["cross_venue"]["hf_snapshot_interval_minutes"])

    return CollectContext(
        gamma=gamma, clob=clob, conn=conn, store=store,
        kalshi=kalshi, metaculus=metaculus, manifold=manifold,
        jobs={
            "sync": job_sync,
            "snap_liquid": job_snap_liquid,
            "snap_tail": job_snap_tail,
            "resolutions": job_resolutions,
            "kalshi_sync": job_kalshi_sync,
            "kalshi_snap_liquid": job_kalshi_snap_liquid,
            "kalshi_snap_tail": job_kalshi_snap_tail,
            "kalshi_resolutions": job_kalshi_resolutions,
            "metaculus_snapshot": job_metaculus_snapshot,
            "metaculus_resolutions": job_metaculus_resolutions,
            "manifold_sync": job_manifold_sync,
            "snap_matched": job_snap_matched,
        },
    )


async def _startup_collection_cycle(ctx: CollectContext) -> None:
    log.info("collector startup cycle")
    await ctx.jobs["sync"]()
    await ctx.jobs["snap_liquid"]()
    await ctx.jobs["snap_tail"]()
    await ctx.jobs["resolutions"]()
    # Phase 10: external venues. Each is independently fail-soft (guardrail 9)
    # inside its own collector module -- one venue's outage never blocks
    # another's startup pass.
    for name in ("kalshi_sync", "kalshi_snap_liquid", "kalshi_snap_tail", "kalshi_resolutions",
                 "metaculus_snapshot", "metaculus_resolutions", "manifold_sync",
                 "snap_matched"):
        try:
            await ctx.jobs[name]()
        except Exception:
            log.exception("startup cycle: venue job failed", extra={"ctx": {"job": name}})


async def run_collect(config: dict[str, Any]) -> None:
    from lab import process_guard

    try:
        process_guard.enforce(config, "collector")
    except Exception:
        log.exception("instance guard failed at collector startup")

    stop = asyncio.Event()
    _install_signal_handlers(stop.set)

    scheduler = AsyncIOScheduler(timezone="UTC")
    ctx = register_collect_jobs(scheduler, config)
    scheduler.start()

    log.info("collector started")

    async def _phases() -> None:
        await _startup_collection_cycle(ctx)
        await _idle_until_stopped(stop)

    try:
        await _run_until_stopped(_phases(), stop)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await ctx.aclose()
        log.info("collector stopped")


SERVICE_NAMES = ("forecast", "report", "shadow", "map_propose", "pmxt_verify",
                 "paper_export")


@dataclass
class AnalyticsContext:
    """The per-service runner coroutines plus their control-time thresholds."""

    services: dict[str, Callable[[], Awaitable[None]]]
    max_age_hours: dict[str, float]


# Services the catch-up (startup + hourly health check) never runs, however
# overdue they are (2026-09-25). The catch-up has no backoff: a service that
# FAILS past its control age is retried on every tick, forever, because a
# failure records no success. For `report` that became the worst possible loop.
# The weekly render OOM'd on 2026-09-20 (the database had doubled to 2.1GB),
# crossed its 192h control age on 09-24 at 17:05, and from then on every hourly
# health check started it again; each attempt was OOM-killed, and systemd's
# default OOMPolicy=stop took the whole orchestrator down with it -- twelve
# times in thirteen hours, killing snapshot rounds mid-flight every time.
# The report renders data the nightly bundle has already committed, so a stale
# page costs nothing a retry storm is worth; the weekly cron still runs it, and
# `lab report` stays available on demand.
NO_CATCH_UP = frozenset({"report"})


def _control_max_ages(config: dict[str, Any]) -> dict[str, float]:
    control = config.get("schedule", {}).get("control", {})
    return {
        "forecast": control.get("forecast_max_age_hours", 24),
        # Weekly, and deliberately NOT hourly-retried: the render is a
        # rendering of data the forecast bundle already committed, so a
        # failed one costs a stale page. Retrying it hourly costs a 3-minute
        # 834MB child every hour for nothing.
        "report": control.get("report_max_age_hours", 192),
        "shadow": control.get("shadow_max_age_hours", 168),
        "map_propose": control.get("map_propose_max_age_hours", 168),
        "pmxt_verify": control.get("pmxt_verify_max_age_hours", 18),
        "paper_export": control.get("paper_export_max_age_hours", 192),
    }


def _mark_preferred_oom_victim(pid: int) -> None:
    """Make a batch child the kernel's first choice when the cgroup runs out.

    The child shares lab-run.service's cgroup, so when the pair together
    crosses MemoryMax the kernel picks ONE process to kill by oom_score --
    usually the bigger child, but nothing guaranteed it, and losing the
    collector instead is the one outcome this whole out-of-process design
    exists to prevent. 1000 is the maximum: always this child first. Written
    from the parent rather than via preexec_fn, which CPython documents as
    unsafe once threads exist (asyncio.to_thread guarantees they do here); the
    race is harmless because the child spends seconds importing before it can
    allocate anything that matters. Best-effort: no /proc (Windows, tests)
    simply means no adjustment.
    """
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w", encoding="ascii") as fh:
            fh.write("1000")
    except OSError:
        pass


async def _run_lab_command_out_of_process(*args: str) -> None:
    """Run `lab <args>` in a child process and raise if it fails.

    For the batch jobs whose peak memory does not fit alongside the collector
    on this host. Two things change in a child: the peak belongs to a process
    that exits, so memory returns to the OS instead of staying resident in the
    collector for the rest of the day, and an OOM kill lands on the child while
    collection keeps running.

    Both current callers earned their place by taking the box down.
    `report` (2026-07-28): peaks ~834MB over ~178s against a collector already
    holding ~565MB, on 967MB of RAM. `learn` (2026-08-02): the monthly loop
    ran for the first time since 2026-07-02, against a database that had grown
    four-fold in the meantime, and pushed 1.8GB into swap -- staying under its
    700MB RSS cap the whole time, since MemoryMax does not bound swap -- which
    thrashed the host for 21 hours with collection dead before a global OOM
    ended it.

    Raising on a non-zero exit is deliberate: `_run` then declines to record a
    success, so the job retries on its own control age rather than pretending
    it did something. A negative code is a signal, and -9 is the case worth
    recognising on sight.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "lab", *args,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    _mark_preferred_oom_victim(proc.pid)
    out, _ = await proc.communicate()
    tail = (out or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
    if proc.returncode != 0:
        raise RuntimeError(
            f"lab {' '.join(args)} exited {proc.returncode} (a negative code is a "
            f"signal, e.g. -9 = OOM-killed): {' | '.join(tail)}"
        )
    log.info("lab command completed out-of-process",
             extra={"ctx": {"args": list(args), "tail": tail}})


def _build_analytics_services(config: dict[str, Any]) -> dict[str, Callable[[], Awaitable[None]]]:
    """One guarded coroutine per service; cron, startup, and health-check share these.

    A per-service asyncio.Lock ensures a service never runs concurrently with
    itself; on success each records its run time so overdue checks stay honest.

    `analytics_run_lock` (2026-07-28) additionally serializes DIFFERENT
    services against each other -- the per-service locks alone never did
    that, so two heavy jobs could run at once purely because their cron times
    happened to be close. This is the structural fix for a real gap that a
    2026-07-27 OOM kill made visible: the nightly forecast bundle's old 02:00
    slot and map_propose's old 05:00-Monday slot left only a 3h margin, and
    the kill happened close enough to both in time to be a plausible
    collision -- though the process had gone quiet for over 2h beforehand, so
    the exact trigger was never nailed down from logs alone. Whether or not
    that specific overlap was the cause, nothing was preventing it, and
    nothing should be. schedule.forecast_cron's config comment spaces the six
    cron times generously as the primary fix, but a
    duration spike (a bigger dataset, a slow LLM call, upstream latency) can
    erode any fixed gap over time -- the lock is what keeps "never overlap" an
    actual guarantee rather than a schedule that happens to work today. It is
    NOT held across the per-cycle collector jobs (snapshot/sync/resolution
    polling) -- those stay on their own independent cadence; only the six
    named SERVICE_NAMES batch jobs share it, since those are the ones with a
    real per-run memory footprint and no latency requirement that would make
    waiting for the lock costly.
    """
    from lab import jobs as analytics
    from lab.schedule_state import record_job_run

    locks = {name: asyncio.Lock() for name in SERVICE_NAMES}
    analytics_run_lock = asyncio.Lock()

    async def _run(name: str, body: Callable[[], Awaitable[None]],
                   tail: Callable[[], Awaitable[None]] | None = None) -> None:
        """`tail` runs inside the shared mutex like `body`, but unconditionally
        and without gating the last-run bookkeeping -- the contract the
        forecast bundle's publish/ledger-commitment steps need (their
        success must not decide whether forecast/eval/report counts as done,
        or a stalled git push would re-trigger and re-bill the whole bundle
        hourly). They were previously called outside _run entirely, which also
        put them outside the mutex: a multi-hundred-MB git push racing another
        service is exactly the concurrency this lock exists to prevent."""
        if is_paused(config):
            return
        if locks[name].locked():
            log.info("analytics service already running -- skipping",
                     extra={"ctx": {"service": name}})
            return
        async with locks[name]:
            if analytics_run_lock.locked():
                log.info("waiting for another analytics service to finish",
                         extra={"ctx": {"service": name}})
            async with analytics_run_lock:
                try:
                    await body()
                except Exception:
                    log.exception("analytics service failed", extra={"ctx": {"service": name}})
                    body_ok = False
                else:
                    body_ok = True
                # Record BEFORE the tail, not after (2026-07-28). A tail step
                # that dies hard -- OOM-killed, not raising -- must not leave an
                # already-finished body looking overdue, because the hourly
                # catch-up then re-runs the whole bundle, dies in the same
                # place, and loops. That is exactly what happened: the report
                # render stopped fitting in memory once the resolution backlog
                # drained (13k -> 57k resolved), and forecast+eval were rebuilt
                # every ~3h for 28 hours without ever recording a success, each
                # attempt taking the collector down with it.
                if body_ok:
                    await asyncio.to_thread(record_job_run, config, name)
                if tail is not None:
                    await tail()

    async def run_forecast_service() -> None:
        async def body() -> None:
            # Only the steps that WRITE research data. Everything downstream of
            # them renders or publishes what they produced, and re-running the
            # ledger writer to recover a failed render is both pointless and
            # (for the LLM-backed forecast step) billable.
            await asyncio.to_thread(analytics.run_forecast_job, config)
            await asyncio.to_thread(analytics.run_eval_job, config)

        async def tail() -> None:
            # Steps whose failure must not re-trigger the bundle: a stalled git
            # push should not re-run (and re-bill) the whole bundle hourly, and
            # the ledger commitment targets this repo rather than the results
            # mirror. `tail` keeps them inside the shared analytics mutex
            # without letting them gate the last-run bookkeeping.
            await asyncio.to_thread(analytics.run_publish_job, config)
            await asyncio.to_thread(analytics.run_ledger_commitment_job, config)

        await _run("forecast", body, tail=tail)

    async def run_report_service() -> None:
        await _run("report", lambda: _run_lab_command_out_of_process("report"))

    async def run_shadow_service() -> None:
        await _run("shadow", lambda: asyncio.to_thread(analytics.run_shadow_job, config))

    async def run_map_propose_service() -> None:
        await _run("map_propose", lambda: asyncio.to_thread(analytics.run_map_propose_job, config))

    async def run_pmxt_verify_service() -> None:
        await _run("pmxt_verify", lambda: asyncio.to_thread(analytics.run_pmxt_verify_job, config))

    async def run_paper_export_service() -> None:
        await _run("paper_export", lambda: asyncio.to_thread(analytics.run_paper_export_job, config))

    return {
        "forecast": run_forecast_service,
        "report": run_report_service,
        "shadow": run_shadow_service,
        "map_propose": run_map_propose_service,
        "pmxt_verify": run_pmxt_verify_service,
        "paper_export": run_paper_export_service,
    }


async def _run_overdue_services(
    config: dict[str, Any],
    actx: AnalyticsContext,
    skip: set[str] | None = None,
) -> list[str]:
    """Run each service immediately if its last success is past its control time.

    Returns the names of services that were started.
    """
    from lab.schedule_state import is_overdue, last_run_age_seconds

    skip = skip or set()
    started: list[str] = []
    for name, service in actx.services.items():
        if name in skip or name in NO_CATCH_UP:
            continue
        age = await asyncio.to_thread(last_run_age_seconds, config, name)
        if is_overdue(age, actx.max_age_hours[name]):
            log.info("catch-up: running overdue service",
                     extra={"ctx": {"service": name, "age_seconds": age,
                                    "max_age_hours": actx.max_age_hours[name]}})
            await service()
            started.append(name)
    return started


def _register_analytics_jobs(scheduler: AsyncIOScheduler, config: dict[str, Any]) -> AnalyticsContext:
    """Schedule forecast/eval, report and shadow on cron triggers (UTC).

    `learn` is absent by design -- it runs from its own systemd timer so its
    memory footprint does not share the collector's cgroup budget (see the
    module docstring)."""
    sched = config.get("schedule", {})
    forecast_cron = sched.get("forecast_cron", "0 2 * * *")   # nightly 02:00
    report_cron = sched.get("report_cron", "0 14 * * 0")      # weekly Sun 14:00
    shadow_cron = sched.get("shadow_cron", "0 3 * * 0")       # weekly Sun 03:00
    map_propose_cron = config.get("cross_venue", {}).get(
        "propose_cron", "0 5 * * 1")                          # weekly Mon 05:00
    pmxt_verify_cron = config.get("cross_venue", {}).get(
        "pmxt_verify_cron", "0 6,18 * * *")                    # twice daily 06:00/18:00
    paper_export_cron = config.get("paper_export", {}).get("cron", "0 5 * * 0")

    services = _build_analytics_services(config)
    actx = AnalyticsContext(services=services, max_age_hours=_control_max_ages(config))

    # Every cron job below gets an explicit misfire grace. APScheduler's default
    # is 1 SECOND: a firing that lands while the event loop is busy is silently
    # discarded -- no log line, no error, no retry until the control window
    # expires days later. That exact failure was found and fixed for the health
    # check on 2026-07-14 and never extended to the jobs that write research
    # data. On 2026-08-09 the weekly shadow job missed its 07:00 slot that way
    # and had not run since 08-03; the loop is now routinely busy because the
    # 1-minute matched-pair capture overruns its own interval. An hour of grace
    # is far beyond any plausible busy window, and `coalesce=True` means a late
    # firing still produces exactly one run.
    GRACE = 3600

    scheduler.add_job(services["forecast"], CronTrigger.from_crontab(forecast_cron, timezone="UTC"),
                      id="nightly", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    # Weekly, not nightly, and out-of-process (see
    # _run_lab_command_out_of_process): the HTML report renders data the
    # nightly bundle already committed, so its cadence is an operator
    # convenience, not a research requirement -- while each render costs a
    # ~834MB, ~3-minute child on a 967MB host. `lab report` stays available
    # on demand for a fresher page at any time.
    scheduler.add_job(services["report"], CronTrigger.from_crontab(report_cron, timezone="UTC"),
                      id="report_weekly", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    scheduler.add_job(services["shadow"], CronTrigger.from_crontab(shadow_cron, timezone="UTC"),
                      id="weekly", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    # No learn job here: it runs from its own systemd timer (lab-learn.timer),
    # so its memory footprint gets its own cgroup budget instead of competing
    # with the collector's. See the module docstring.
    # M7: proposes candidate matches only -- never auto-confirms. A human still
    # has to run `lab map confirm` before a pair goes live (brief section 6/9).
    scheduler.add_job(services["map_propose"], CronTrigger.from_crontab(map_propose_cron, timezone="UTC"),
                      id="map_propose_weekly", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    # M7: verifies pmxt's out-of-band Router suggestions (data/pmxt_candidates.json,
    # written by scripts/pmxt_router_scan.py's own separate scheduled task --
    # never called from this process). Same propose-only, never-auto-confirm
    # contract as map_propose above.
    scheduler.add_job(services["pmxt_verify"], CronTrigger.from_crontab(pmxt_verify_cron, timezone="UTC"),
                      id="pmxt_verify_twice_daily", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    scheduler.add_job(services["paper_export"], CronTrigger.from_crontab(paper_export_cron, timezone="UTC"),
                      id="paper_export_weekly", max_instances=1, coalesce=True, misfire_grace_time=GRACE)
    log.info("analytics scheduled",
             extra={"ctx": {"nightly": forecast_cron, "report": report_cron, "weekly": shadow_cron,
                            "map_propose": map_propose_cron,
                            "pmxt_verify": pmxt_verify_cron, "paper_export": paper_export_cron,
                            "control": actx.max_age_hours}})
    return actx


def _snapshot_stale_threshold_minutes(config: dict[str, Any]) -> float:
    """Safe margin before the liquid tier counts as stalled: the larger of 2x
    the snapshot cadence and the forecast freshness guard."""
    cadence = config["collect"]["snapshot_interval_minutes"]["liquid"]
    forecast_max = config.get("forecast", {}).get(
        "max_snapshot_age_minutes", {}).get("liquid", cadence * 2)
    return max(2 * cadence, forecast_max)


async def _check_collector_liveness(config: dict[str, Any], ctx: CollectContext) -> bool:
    """Verify the liquid tier is fresh; force a collection cycle if it has stalled.

    Returns True when a recovery cycle was triggered.
    """
    from lab.collect.status import gather_status
    from lab.schedule_state import is_snapshot_stale

    status = await asyncio.to_thread(gather_status, config)
    age_min = status.get("tiers", {}).get("liquid", {}).get("last_snapshot_age_min")
    threshold = _snapshot_stale_threshold_minutes(config)
    if is_snapshot_stale(age_min, threshold):
        log.warning("health-check: collector stalled -- forcing collection cycle",
                    extra={"ctx": {"liquid_snapshot_age_min": age_min,
                                   "threshold_min": threshold}})
        await ctx.jobs["sync"]()
        await ctx.jobs["snap_liquid"]()
        return True
    log.info("health-check: collector healthy",
             extra={"ctx": {"liquid_snapshot_age_min": age_min,
                            "threshold_min": threshold}})
    return False


def _enforce_instance_guard(config: dict[str, Any], role: str) -> dict[str, Any]:
    """Stand down outdated/redundant instances; never raise into the caller."""
    from lab import process_guard

    try:
        return process_guard.enforce(config, role)
    except Exception:
        log.exception("instance guard failed")
        return {}


def _register_health_check(
    scheduler: AsyncIOScheduler,
    config: dict[str, Any],
    ctx: CollectContext,
    actx: AnalyticsContext,
) -> None:
    """Hourly liveness check: restart overdue analytics services and a stalled
    collector immediately upon detection."""
    minutes = config.get("schedule", {}).get("health_check_interval_minutes", 60)

    async def health_check() -> None:
        if is_paused(config):
            return
        guard = await asyncio.to_thread(_enforce_instance_guard, config, "orchestrator")
        restarted = await _run_overdue_services(config, actx)
        collector_recovered = await _check_collector_liveness(config, ctx)
        log.info("health-check complete",
                 extra={"ctx": {"restarted_services": restarted,
                                "collector_recovered": collector_recovered,
                                "instances_stopped": guard.get("stopped", [])}})

    # misfire_grace_time: APScheduler's own default (1s) is far tighter than
    # this scheduler's observed jitter under load (5-7s late is routine when
    # ~7 hourly jobs pile up on the same tick, per live VPS audit 2026-07-14)
    # -- without an explicit override every firing was silently discarded as
    # a misfire, so the one job meant to catch and recover an overdue nightly
    # bundle never ran at all. An hourly check firing a few minutes late is
    # inconsequential; being silently skipped forever is not.
    scheduler.add_job(health_check, "interval", minutes=minutes,
                      id="health_check", max_instances=1, coalesce=True,
                      misfire_grace_time=300)
    log.info("health-check scheduled", extra={"ctx": {"interval_minutes": minutes}})


def _checkpoint(config: dict[str, Any], marker: str) -> None:
    """Unbuffered startup progress marker (survives a hard crash for diagnosis)."""
    try:
        p = pid_path(config).with_name("orchestrator.startup")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now_utc_iso()} {marker}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _loop_exception_handler(config: dict[str, Any], loop: asyncio.AbstractEventLoop,
                             context: dict[str, Any]) -> None:
    """Guaranteed last stop for exceptions asyncio would otherwise only print to
    stderr (e.g. raised inside a callback or a fire-and-forget task with no one
    awaiting it) -- these previously left orchestrator crashes with zero trace
    in data/logs/. Falls back to asyncio's own default handler after logging."""
    exc = context.get("exception")
    log.critical("unhandled asyncio exception -- orchestrator may be about to die",
                 extra={"ctx": {"message": context.get("message")}}, exc_info=exc)
    loop.default_exception_handler(context)


async def _run_until_stopped(coro: Awaitable[None], stop: asyncio.Event) -> None:
    """Run `coro`, abandoning it as soon as `stop` is set.

    Needed because the startup phases are *long*: the tail-tier pass alone
    walks ~13k markets at roughly a request every 0.2s, so simply checking
    `stop` between phases (the 2026-07-25 first cut of this fix) still made a
    stop wait tens of minutes -- far past any service manager's stop timeout,
    which is the SIGKILL this whole change exists to avoid.

    Abandoning means cancelling, and cancellation is safe here precisely
    because asyncio can only deliver it at an `await`: a synchronous write
    (polars' parquet write plus its atomic rename, a sqlite transaction) has
    no await inside it and therefore always runs to completion. What gets
    interrupted is the network wait around such a write, never the write.
    """
    task = asyncio.ensure_future(coro)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        if not task.done():
            log.info("stop requested mid-startup -- abandoning in-flight collection")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task  # let the cancellation land before clients are closed
    if task.done() and not task.cancelled():
        task.result()  # surface a genuine failure rather than swallowing it


async def _idle_until_stopped(stop: asyncio.Event, on_tick: Callable[[], None] | None = None,
                              interval: float = 60.0) -> None:
    """Tick every `interval` seconds until `stop` is set, then return.

    Deliberately NOT `while True: await asyncio.sleep(interval)`: that loop has
    no way to observe a stop request, so the caller's `finally:` cleanup only
    ever ran via cancellation or a hard kill (see _install_signal_handlers)."""
    while not stop.is_set():
        if on_tick is not None:
            on_tick()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except (asyncio.TimeoutError, TimeoutError):
            continue


def _install_signal_handlers(request_stop: Callable[[], None]) -> None:
    """Ask the run loop to shut down cleanly, and log *why* it is stopping.

    Two things this has to get right, the first of them learned the hard way
    (2026-07-25, after `systemctl restart` was observed logging
    "State 'stop-sigterm' timed out. Killing."):

    1. **A handler that only logs silently disables the signal.** Installing
       any Python handler REPLACES the signal's default disposition, and
       SIGTERM's default is "terminate the process". The previous version of
       this function logged and returned, which turned SIGTERM into a no-op:
       the process ran on until systemd's TimeoutStopSec (90s by default)
       expired and SIGKILL arrived, so the caller's `finally:` block never ran
       and whatever was in flight was killed mid-write rather than finished.
       A handler must therefore actively initiate shutdown, not merely narrate
       it -- which is what `request_stop` is for.
    2. **A second signal must still be able to kill a hung shutdown.** After
       the first signal we restore the default disposition, so an operator
       pressing Ctrl+C twice (or systemd escalating) is never trapped by a
       cleanup path that itself wedged.

    The original docstring's point still stands and is why the log line stays:
    a crash with no "received stop signal" line, but a heartbeat that went
    stale, means something killed the process outright rather than asking it
    to stop -- a hard kill can never be intercepted by any handler.
    """
    loop = asyncio.get_running_loop()
    seen: set[int] = set()

    def _restore_default(signum: int) -> None:
        try:
            loop.remove_signal_handler(signum)  # asyncio restores SIG_DFL itself
            return
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    def _on_signal(signum: int) -> None:
        if signum in seen:
            return
        seen.add(signum)
        log.warning("received stop signal -- shutting down",
                    extra={"ctx": {"signal": signum}})
        _restore_default(signum)
        request_stop()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):  # SIGBREAK is Windows-only (Ctrl+Break)
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # Windows has no add_signal_handler. Fall back to the C-level hook,
            # hopping back onto the loop thread -- request_stop touches asyncio
            # state, which is not safe to poke from a signal handler directly.
            def _thread_hop(signum, _frame, _loop=loop):
                _loop.call_soon_threadsafe(_on_signal, signum)
            try:
                signal.signal(sig, _thread_hop)
            except (ValueError, OSError):
                pass  # e.g. not the main thread, or unsupported on this platform


async def run_orchestrator(config: dict[str, Any]) -> None:
    """One-button entry point: collector + scheduled analytics in one process."""
    _checkpoint(config, "A: entered run_orchestrator")
    stop = asyncio.Event()
    _install_signal_handlers(stop.set)
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: _loop_exception_handler(config, loop, context))
    guard = _enforce_instance_guard(config, "orchestrator")
    _checkpoint(config, f"B: enforce done, stopped={guard.get('stopped')}")
    if guard.get("stopped"):
        log.info("orchestrator took over from prior instances",
                 extra={"ctx": {"stopped": guard["stopped"]}})

    scheduler = AsyncIOScheduler(timezone="UTC")
    ctx = register_collect_jobs(scheduler, config)
    actx = _register_analytics_jobs(scheduler, config)
    _register_health_check(scheduler, config, ctx, actx)
    scheduler.start()
    _checkpoint(config, "C: scheduler started")

    pid_path(config).write_text(str(os.getpid()), encoding="utf-8")
    _write_heartbeat(config)
    log.info("orchestrator started", extra={"ctx": {"pid": os.getpid()}})
    _checkpoint(config, "D: pid+heartbeat written, entering startup cycle")

    # Startup runs under _run_until_stopped, and the whole thing sits inside
    # the try, so a stop arriving mid-startup both takes effect promptly and
    # still reaches the cleanup below. Before 2026-07-25 a SIGTERM here was
    # ignored outright (see _install_signal_handlers).
    async def _phases() -> None:
        await _startup_collection_cycle(ctx)
        _checkpoint(config, "E: startup collection cycle done")

        skip: set[str] = set()
        if config.get("schedule", {}).get("run_on_start", True):
            log.info("orchestrator startup analytics pass")
            await actx.services["forecast"]()
            skip.add("forecast")
        await _run_overdue_services(config, actx, skip=skip)

        await _idle_until_stopped(stop, lambda: _write_heartbeat(config))

    try:
        await _run_until_stopped(_phases(), stop)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await ctx.aclose()
        try:
            pid_path(config).unlink(missing_ok=True)
        except OSError:
            pass
        log.info("orchestrator stopped")


def run_watchdog(config: dict[str, Any]) -> None:
    """Supervise `lab run` as a child process: on any exit, wait
    `watchdog.restart_delay_seconds` (default 10 min) and relaunch it.

    Runs until the watchdog itself is interrupted (Ctrl+C / SIGTERM), at which
    point its child is terminated too rather than left orphaned. The delay is
    deliberate (see config.yaml) -- this is a supervisor, not a tight retry loop.

    Stands down any other live `lab watchdog` instance on startup (same guard
    the orchestrator uses on itself): two supervisors each restarting their own
    orchestrator child fight over which child survives via process_guard,
    producing a permanent crash-restart loop with no forward progress.
    """
    guard = _enforce_instance_guard(config, "watchdog")
    if guard.get("stopped"):
        log.info("watchdog took over from prior watchdog instances",
                 extra={"ctx": {"stopped": guard["stopped"]}})
    delay = config.get("watchdog", {}).get("restart_delay_seconds", 600)
    cmd = [sys.executable, "-m", "lab", "run"]
    attempt = 0
    while True:
        attempt += 1
        log.info("watchdog: starting orchestrator", extra={"ctx": {"attempt": attempt}})
        proc = subprocess.Popen(cmd)
        try:
            code = proc.wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("watchdog: stopping -- terminating orchestrator child")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        log.warning("watchdog: orchestrator exited -- will restart after delay",
                   extra={"ctx": {"exit_code": code, "attempt": attempt, "delay_seconds": delay}})
        try:
            time.sleep(delay)
        except (KeyboardInterrupt, SystemExit):
            log.info("watchdog: stopping during restart delay")
            raise
