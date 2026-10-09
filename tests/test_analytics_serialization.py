"""Analytics services never run concurrently with each other.

Regression cover for the gap a 2026-07-27 OOM kill exposed: each of the six
SERVICE_NAMES batch jobs had its own asyncio.Lock, which stopped a service
overlapping *itself* but did nothing to stop two DIFFERENT services running at
once. Only the spacing of their cron times kept them apart, informally -- and
on a 967MB VPS two heavy jobs at once is enough to get the process killed.

The cron times are now spaced generously (config.yaml's schedule block), but
spacing degrades silently as jobs grow; the shared mutex is what makes
"never overlap" a guarantee instead of a schedule that happens to work.
"""

from __future__ import annotations

import asyncio

import pytest

from lab.collect.runner import _build_analytics_services
from lab.util import load_config


@pytest.fixture()
def config(tmp_path):
    cfg = load_config()
    cfg["storage"] = {
        "db_path": str(tmp_path / "lab.db"),
        "snapshots_dir": str(tmp_path / "snapshots"),
        "models_dir": str(tmp_path / "models"),
        "logs_dir": str(tmp_path / "logs"),
        "reports_dir": str(tmp_path / "reports"),
    }
    cfg.setdefault("collect", {})["pause_file"] = str(tmp_path / "PAUSE")
    return cfg


def _instrument(monkeypatch, config, durations):
    """Replace every analytics job body with a tracked async sleep, and record
    the concurrency actually observed."""
    from lab import jobs as analytics

    state = {"active": 0, "peak": 0, "order": []}

    def _make(name):
        def _job(_config, *args, **kwargs):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["order"].append(name)
            # Blocking sleep: these run via asyncio.to_thread in production, so
            # a thread-blocking body is the faithful stand-in.
            import time

            time.sleep(durations.get(name, 0.05))
            state["active"] -= 1
            return {}

        return _job

    for attr, name in [
        ("run_forecast_job", "forecast"), ("run_eval_job", "forecast"),
        ("run_report_job", "forecast"), ("run_publish_job", "publish"),
        ("run_ledger_commitment_job", "ledger"), ("run_shadow_job", "shadow"),
        ("run_map_propose_job", "map_propose"),
        ("run_pmxt_verify_job", "pmxt_verify"),
        ("run_paper_export_job", "paper_export"),
    ]:
        monkeypatch.setattr(analytics, attr, _make(name))

    # report, forecast and eval no longer call a jobs.* function at all -- they
    # spawn `lab <cmd>` in a child process (runner._run_lab_command_out_of_process,
    # after each took the host down: report 2026-07-28, forecast/eval
    # 2026-10-07 and 10-09; learn has had its own systemd unit since 08-05).
    # Patch that path too, or they run real subprocesses here and, worse, drop
    # out of the serialization check entirely -- which is exactly how report
    # silently stopped being covered by this test on 2026-07-28.
    from lab.collect import runner as runner_mod

    async def _fake_out_of_process(*args):
        _make(args[0])(config)

    monkeypatch.setattr(runner_mod, "_run_lab_command_out_of_process", _fake_out_of_process)

    monkeypatch.setattr("lab.schedule_state.record_job_run", lambda *a, **k: None)
    return state


def test_two_different_services_never_overlap(config, monkeypatch):
    """THE regression test. Before the shared mutex, firing two services at
    once let both bodies run simultaneously -- peak concurrency 2, which on
    the production VPS meant two report-sized memory footprints at once."""
    state = _instrument(monkeypatch, config, {"map_propose": 0.15, "shadow": 0.15})
    services = _build_analytics_services(config)

    async def scenario():
        await asyncio.gather(services["shadow"](), services["map_propose"]())

    asyncio.run(scenario())

    assert state["peak"] == 1, (
        f"two analytics services ran concurrently (peak={state['peak']}) -- "
        "the shared mutex is not serializing them"
    )


def test_all_services_fired_at_once_still_serialize(config, monkeypatch):
    """The worst realistic case: a 1st-of-month that is also a Sunday, with
    every cron landing together. All must still run, one at a time."""
    state = _instrument(monkeypatch, config, {})
    services = _build_analytics_services(config)

    async def scenario():
        await asyncio.gather(*(svc() for svc in services.values()))

    asyncio.run(scenario())

    assert state["peak"] == 1, f"peak concurrency {state['peak']}, expected 1"
    # Every service still got its turn -- serializing must not drop work.
    assert set(state["order"]) >= {
        "forecast", "shadow", "map_propose", "pmxt_verify", "paper_export"}


def test_serialization_does_not_deadlock_on_repeat_calls(config, monkeypatch):
    """The mutex is taken inside the per-service lock; calling the same
    service repeatedly must not wedge either lock."""
    _instrument(monkeypatch, config, {})
    services = _build_analytics_services(config)

    async def scenario():
        for _ in range(3):
            await services["shadow"]()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_pause_file_still_short_circuits_before_the_mutex(config, monkeypatch, tmp_path):
    """Guardrail 8 unchanged: PAUSE must skip the job outright, not queue it
    behind the mutex."""
    state = _instrument(monkeypatch, config, {})
    (tmp_path / "PAUSE").write_text("", encoding="utf-8")
    services = _build_analytics_services(config)

    asyncio.run(services["shadow"]())
    assert state["order"] == [], "PAUSE did not stop the service from running"
