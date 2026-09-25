"""Missed-run catch-up: overdue decision logic and last-run persistence."""

from __future__ import annotations

import asyncio

import pytest

from lab import schedule_state
from lab.collect.runner import (
    _register_analytics_jobs,
    _register_health_check,
    _snapshot_stale_threshold_minutes,
    register_collect_jobs,
)
from lab.schedule_state import is_overdue, is_snapshot_stale
from lab.store import db
from lab.util import load_config


def test_is_overdue_never_run():
    assert is_overdue(None, 24) is True


def test_is_overdue_fresh():
    assert is_overdue(60.0, 24) is False


def test_is_overdue_stale():
    assert is_overdue(25 * 3600, 24) is True


def test_is_overdue_boundary():
    assert is_overdue(24 * 3600, 24) is False


def test_is_snapshot_stale_never():
    assert is_snapshot_stale(None, 10) is True


def test_is_snapshot_stale_fresh():
    assert is_snapshot_stale(5.0, 10) is False


def test_is_snapshot_stale_stalled():
    assert is_snapshot_stale(11.0, 10) is True


def test_is_snapshot_stale_boundary():
    assert is_snapshot_stale(10.0, 10) is False


def test_snapshot_stale_threshold_minutes():
    config = {
        "collect": {"snapshot_interval_minutes": {"liquid": 5}},
        "forecast": {"max_snapshot_age_minutes": {"liquid": 15}},
    }
    assert _snapshot_stale_threshold_minutes(config) == 15


def test_meta_round_trip(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    try:
        assert db.get_meta(conn, "missing") is None
        db.set_meta(conn, "last_run_forecast", "2026-07-01T00:00:00+00:00")
        assert db.get_meta(conn, "last_run_forecast") == "2026-07-01T00:00:00+00:00"
        db.set_meta(conn, "last_run_forecast", "2026-07-02T00:00:00+00:00")
        assert db.get_meta(conn, "last_run_forecast") == "2026-07-02T00:00:00+00:00"
    finally:
        conn.close()


def test_record_and_read_age(tmp_path):
    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    assert schedule_state.last_run_age_seconds(config, "forecast") is None

    schedule_state.record_job_run(config, "forecast")
    age = schedule_state.last_run_age_seconds(config, "forecast")
    assert age is not None
    assert age == pytest.approx(0, abs=5)
    assert is_overdue(age, 24) is False


def test_orchestrator_job_registration(tmp_path):
    """Non-destructive wiring check: all expected scheduler jobs are registered."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    config = load_config()
    config = {
        **config,
        "storage": {
            **config["storage"],
            "db_path": str(tmp_path / "lab.db"),
            "snapshots_dir": str(tmp_path / "snapshots"),
        },
    }

    async def _check() -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        ctx = register_collect_jobs(scheduler, config)
        actx = _register_analytics_jobs(scheduler, config)
        _register_health_check(scheduler, config, ctx, actx)

        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids >= {"nightly", "weekly", "health_check", "heartbeat_ping",
                           "map_propose_weekly", "pmxt_verify_twice_daily"}
        assert len(job_ids) >= 9  # 4 collector + heartbeat_ping + 3 analytics + health_check

        health = scheduler.get_job("health_check")
        assert health is not None
        assert health.trigger.interval.total_seconds() == 60 * 60
        # Live VPS audit (2026-07-14): APScheduler's 1s default misfire_grace_time
        # was tighter than this scheduler's routine multi-second jitter under load,
        # so every hourly firing was silently discarded and the job never once ran.
        assert health.misfire_grace_time == 300

        hb = scheduler.get_job("heartbeat_ping")
        assert hb is not None
        assert hb.trigger.interval.total_seconds() == 5 * 60

        await ctx.aclose()

    asyncio.run(_check())



# --- a failed tail must not re-trigger the body (2026-07-28 crash loop) ----

def test_success_is_recorded_before_the_tail_runs(tmp_path, monkeypatch):
    """The bundle's retry engine must track the steps that WRITE data, not the
    ones that render it.

    Live failure this encodes: once the resolution backlog drained (13k -> 57k
    resolved), the report render stopped fitting in memory and the process was
    OOM-killed mid-render. Because the last-run stamp was written after the
    tail, an already-finished forecast+eval never counted as done, so the
    hourly catch-up rebuilt the whole bundle every ~3 hours for 28 hours,
    taking the collector down with it each time.

    An OOM kill cannot be simulated in-process -- it is not an exception -- so
    the test asserts the ordering that makes it survivable: the stamp exists by
    the time the tail is entered.
    """
    import asyncio

    from lab.schedule_state import last_run_age_seconds, record_job_run

    config = {"storage": {"db_path": str(tmp_path / "lab.db")}}
    stamped_when_tail_ran = {}

    # Exercise the ordering the real _run contract guarantees.
    async def run_like_service():
        body_ok = False
        try:
            pass  # body: forecast + eval, succeeds
        except Exception:
            body_ok = False
        else:
            body_ok = True
        if body_ok:
            await asyncio.to_thread(record_job_run, config, "forecast")
        # tail: report render -- observe whether the stamp is already in place
        stamped_when_tail_ran["age"] = await asyncio.to_thread(
            last_run_age_seconds, config, "forecast")
        raise RuntimeError("report OOM stand-in")

    with pytest.raises(RuntimeError):
        asyncio.run(run_like_service())

    assert stamped_when_tail_ran["age"] is not None, (
        "no last-run stamp when the tail started -- a dying render would leave "
        "the bundle permanently overdue and re-trigger it hourly"
    )


def test_report_is_its_own_service_not_part_of_the_nightly_bundle():
    """Guards the split: the nightly bundle writes research data, and nothing
    in it renders. A failed render must cost a stale page, never a rebuild of
    forecast+eval (an LLM-billed one) and never the collector."""
    import inspect

    from lab.collect import runner

    assert "report" in runner.SERVICE_NAMES
    src = inspect.getsource(runner._build_analytics_services)
    bundle = src.split("async def run_forecast_service", 1)[1].split("async def run_report_service", 1)[0]
    assert "run_forecast_job" in bundle and "run_eval_job" in bundle
    assert "run_report_job" not in bundle, "report is back inside the nightly bundle"


def test_report_control_age_is_not_hourly():
    """The catch-up must not re-run a failed render every hour: it is a ~834MB,
    ~3-minute child, and retrying it buys nothing the data does not already
    have."""
    from lab.collect.runner import _control_max_ages

    assert _control_max_ages({})["report"] >= 168


def test_heavy_batch_jobs_run_out_of_process():
    """Their peaks must belong to a process that exits. In-process the peak
    became the collector's own RSS, and both of these took the host down that
    way: report on 2026-07-28, learn on 2026-08-02."""
    import inspect

    from lab.collect import runner

    src = inspect.getsource(runner._run_lab_command_out_of_process)
    assert "create_subprocess_exec" in src
    assert '"-m", "lab", *args' in src
    # A non-zero exit must propagate, or _run would record a success for work
    # that never happened.
    assert "raise RuntimeError" in src

    services = inspect.getsource(runner._build_analytics_services)
    assert '_run_lab_command_out_of_process("report")' in services, (
        "report is back in the orchestrator process"
    )


def test_learn_is_not_scheduled_by_the_orchestrator_at_all():
    """A child process does not leave its parent's cgroup, so running the
    monthly loop from here made it share the collector's memory budget -- it
    was OOM-killed at 84s on 2026-08-05 doing exactly that, while the same job
    standalone finishes in 101s. It owns a systemd timer now."""
    import inspect

    from lab.collect import runner
    from lab.collect.runner import _control_max_ages

    assert "learn" not in runner.SERVICE_NAMES
    assert "learn" not in _control_max_ages({})
    src = inspect.getsource(runner._register_analytics_jobs)
    assert 'services["learn"]' not in src


def test_every_analytics_cron_job_has_an_explicit_misfire_grace():
    """APScheduler's default is 1 SECOND, and a cron firing that lands while the
    event loop is busy is discarded with no log line at all -- the job simply
    never runs until its control window expires days later.

    This was found and fixed for `health_check` on 2026-07-14 and not extended
    to the jobs that write research data. On 2026-08-09 the weekly shadow job
    missed its 07:00 slot exactly this way and had gone unrun since 08-03, while
    the 1-minute matched-pair capture kept the loop busy.
    """
    import asyncio

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from lab.collect.runner import _register_analytics_jobs
    from lab.util import load_config

    async def _check() -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        _register_analytics_jobs(scheduler, load_config())
        cron_jobs = [j for j in scheduler.get_jobs() if j.id != "health_check"]
        assert cron_jobs, "no analytics jobs registered -- fixture is wrong"
        for job in cron_jobs:
            assert job.misfire_grace_time is not None and job.misfire_grace_time >= 300, (
                f"{job.id} runs on APScheduler's 1s default grace and can be "
                "silently dropped"
            )
            # A late firing must still produce exactly one run, not a backlog.
            assert job.coalesce is True, f"{job.id} would run repeatedly to catch up"

    asyncio.run(_check())


def test_every_collector_interval_job_survives_a_busy_loop(tmp_path):
    """Regression, measured 2026-08-18: APScheduler's default misfire_grace_time
    is ONE SECOND, so a firing whose moment passes while the event loop is busy
    is discarded rather than delayed. A snapshot round runs back-to-back
    requests for minutes, so the loop is busy essentially always, and over
    10h45m `job_snap_tail` completed exactly ONE round against ~11 scheduled --
    which is why the Polymarket tail's realized cadence was ~7 hours against a
    configured 60 minutes, with 844 tail markets carrying no snapshot at all.
    Every collector job was losing firings this way. The 2026-07-14 audit had
    found and fixed exactly this for `health_check` alone; it was never
    generalized, so assert the property for all of them rather than one."""
    import asyncio as _asyncio

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    config = load_config()
    config = {
        **config,
        "storage": {
            **config["storage"],
            "db_path": str(tmp_path / "lab.db"),
            "snapshots_dir": str(tmp_path / "snapshots"),
        },
    }

    async def _check() -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        ctx = register_collect_jobs(scheduler, config)

        jobs = scheduler.get_jobs()
        assert jobs, "no collector jobs registered"
        for job in jobs:
            interval = job.trigger.interval.total_seconds()
            # getattr, not attribute access: APScheduler does not even set the
            # attribute when the caller leaves it default, so a plain access
            # would fail with AttributeError instead of saying what is wrong.
            grace = getattr(job, "misfire_grace_time", None)
            assert grace is not None, f"{job.name} kept APScheduler's 1s default"
            assert grace >= 60, f"{job.name} grace too tight"
            # never longer than its own period: a late firing must not outlive
            # its successor and double up.
            assert grace <= max(60, interval), f"{job.name} grace exceeds its period"
            assert job.coalesce is True, f"{job.name} would replay a backlog"
            assert job.max_instances == 1, f"{job.name} allows overlapping rounds"

        await ctx.aclose()

    _asyncio.run(_check())


def _seed_coverage(conn, model, venue, days, n):
    """`days` consecutive dates ending 2026-08-20, `n` forecasts each."""
    import datetime

    from lab.store import db
    base = datetime.date(2026, 8, 20)
    for i in range(days):
        d = (base - datetime.timedelta(days=i)).isoformat()
        for j in range(n):
            db.append_forecast(conn, {
                "ts": f"{d}T02:00:00+00:00", "condition_id": f"{venue}:{model}:{i}:{j}",
                "model_id": model, "p_yes": 0.5, "p_market_at_ts": 0.5,
            })


def test_coverage_watchdog_catches_a_silent_collapse(tmp_path):
    """Regression guard for the 2026-08 audit week: a six-day Kalshi blackout,
    m4_ensemble writing 7 rows against m0's 524, m5_nowcast at exactly zero for
    six days -- all silent, all costing the resolved clusters H1's power rests
    on. Each would surface here on the first night."""
    from lab.collect.status import coverage_regressions
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    _seed_coverage(conn, "m0_market", "polymarket", days=10, n=40)   # steady
    _seed_coverage(conn, "m5_nowcast", "polymarket", days=10, n=40)  # steady...
    conn.commit()

    # ...then one day where m5 collapses and m0 does not
    for j in range(40):
        db.append_forecast(conn, {"ts": "2026-08-21T02:00:00+00:00",
                                  "condition_id": f"ok{j}", "model_id": "m0_market",
                                  "p_yes": 0.5, "p_market_at_ts": 0.5})
    for j in range(3):
        db.append_forecast(conn, {"ts": "2026-08-21T02:00:00+00:00",
                                  "condition_id": f"bad{j}", "model_id": "m5_nowcast",
                                  "p_yes": 0.5, "p_market_at_ts": 0.5})
    conn.commit()

    regs = coverage_regressions(conn, day="2026-08-21")
    assert [r["model_id"] for r in regs] == ["m5_nowcast"]
    assert regs[0]["n"] == 3 and regs[0]["baseline"] == 40.0
    conn.close()


def test_coverage_watchdog_ignores_small_and_sleeping_models(tmp_path):
    """Coverage varies by design -- M5 covers only weather/macro, M7 only
    matched pairs -- so each series is compared against ITSELF, and a series
    too small for a median to mean anything is not compared at all."""
    from lab.collect.status import coverage_regressions
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    _seed_coverage(conn, "m7_crossvenue", "polymarket", days=10, n=4)  # below min_baseline
    conn.commit()
    db.append_forecast(conn, {"ts": "2026-08-21T02:00:00+00:00", "condition_id": "z",
                              "model_id": "m7_crossvenue", "p_yes": 0.5, "p_market_at_ts": 0.5})
    conn.commit()

    assert coverage_regressions(conn, day="2026-08-21") == []
    conn.close()


def test_coverage_watchdog_defaults_to_the_latest_complete_day(tmp_path):
    """Called mid-bundle, "today" is a partially written day and every model
    looks collapsed. The default is the latest day WITH data, and the nightly
    caller passes its own run date once both passes are done."""
    from lab.collect.status import coverage_regressions
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    _seed_coverage(conn, "m0_market", "polymarket", days=10, n=40)
    conn.commit()

    # no rows for 2026-08-21 at all: the default target is 08-20, which is fine
    assert coverage_regressions(conn) == []
    conn.close()


def test_a_failing_report_is_never_retried_by_the_catch_up():
    """2026-09-20..25: the weekly render OOM'd, crossed its 192h control age,
    and the catch-up -- which has no backoff, since a failure records no
    success -- started it again on every hourly health check. Each attempt was
    OOM-killed and took the orchestrator down with it, twelve times in
    thirteen hours. The weekly cron still runs it; the catch-up never does."""
    import asyncio

    from lab.collect import runner

    ran: list[str] = []

    def svc(name):
        async def _s():
            ran.append(name)
        return _s

    actx = runner.AnalyticsContext(
        services={"report": svc("report"), "shadow": svc("shadow")},
        max_age_hours={"report": 192, "shadow": 168},
    )

    import lab.schedule_state as ss
    orig = ss.last_run_age_seconds
    ss.last_run_age_seconds = lambda config, name: 10 ** 9     # everything hugely overdue
    try:
        started = asyncio.run(runner._run_overdue_services({}, actx))
    finally:
        ss.last_run_age_seconds = orig

    assert "report" not in ran and "report" not in started
    assert ran == ["shadow"], "every other overdue service must still be caught up"


def test_a_batch_child_is_marked_as_the_preferred_oom_victim(tmp_path, monkeypatch):
    """The child shares lab-run's cgroup; when the pair crosses MemoryMax the
    kernel kills ONE process by oom_score. It must never be the collector."""
    from lab.collect import runner

    written: dict[str, str] = {}

    real_open = open

    def fake_open(path, mode="r", *a, **kw):
        if str(path).startswith("/proc/") and str(path).endswith("/oom_score_adj"):
            class _F:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def write(self_inner, s):
                    written[str(path)] = s
            return _F()
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    runner._mark_preferred_oom_victim(4242)
    assert written == {"/proc/4242/oom_score_adj": "1000"}


def test_marking_the_oom_victim_is_best_effort():
    """No /proc (Windows, containers) must never break a batch job."""
    from lab.collect import runner

    runner._mark_preferred_oom_victim(999999999)   # nonexistent pid: must not raise
