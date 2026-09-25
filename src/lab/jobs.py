"""Self-contained analytics jobs shared by the CLI and the orchestrator.

Each function opens its own database connection, does one unit of work, and
closes it -- so they are safe to run on a worker thread while the collector
holds its own connection on the event loop (SQLite WAL + busy_timeout).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lab.store import db
from lab.store.snapshots import SnapshotStore

log = logging.getLogger(__name__)


def run_forecast_job(config: dict[str, Any]) -> dict[str, Any]:
    """Full forecast pass: base models + M6 coherence + M7 cross-venue + M4 ensemble."""
    from lab.forecast import build_default_models, eligible_market_states, run_forecasts
    from lab.learn.refit import load_active_artifact
    from lab.models.m4_ensemble import M4Ensemble
    from lab.models.m6_consistency import scan_universe, write_m6_forecasts
    from lab.models.m7_crossvenue import scan_confirmed_pairs, write_m7_forecasts
    from lab.util import now_utc

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        # One eligibility view and one freeze timestamp for the whole bundle.
        # The ensemble pass runs 13-18 minutes after the base pass (M3's LLM
        # calls and the M6/M7 scans sit between them); deriving its own
        # eligibility there re-applied guardrail 13's 15-minute freshness
        # window to prices the base pass had already frozen, which emptied M4
        # entirely on any day the collector landed no snapshot round in the
        # gap. See run_forecasts' docstring for the measured damage.
        states = eligible_market_states(conn, store, config)
        ts = now_utc().isoformat(timespec="seconds")
        counts = run_forecasts(conn, store, build_default_models(conn, config, store), config,
                               states=states, ts=ts)
        findings = asyncio.run(scan_universe(conn, store, config))
        counts["m6_written"] = write_m6_forecasts(conn, store, findings, config)
        m7_results = asyncio.run(scan_confirmed_pairs(conn, store, config))
        counts["m7_written"] = write_m7_forecasts(conn, store, m7_results, config)
        m4 = M4Ensemble(conn, load_active_artifact(config, "m4_weights"),
                        load_active_artifact(config, "m4_extremization"),
                        pool_date=ts[:10])
        counts["m4_written"] = run_forecasts(conn, store, [m4], config,
                                             states=states, ts=ts)["written"]

        # Coverage watchdog, after both passes so the day is complete. Logged
        # at ERROR on purpose: every defect the 2026-08 audit week turned up
        # was silent and cost resolved clusters the primary hypothesis depends
        # on, and the heartbeat cannot see "alive but writing nothing".
        from lab.collect.status import coverage_regressions

        regressions = coverage_regressions(conn, day=ts[:10])
        counts["coverage_regressions"] = len(regressions)
        for r in regressions:
            log.error("coverage regression: model wrote far below its own trailing median",
                      extra={"ctx": r})
        # Raised until a nightly run finds none -- see heartbeat.set_alarm.
        from lab.heartbeat import set_alarm

        set_alarm(conn, "coverage", (
            f"{ts[:10]}: {len(regressions)} model/venue series below 50% of their own "
            "median -- " + ", ".join(
                f"{r['model_id']}@{r['venue']} {r['n']}/{r['baseline']}" for r in regressions[:6])
        ) if regressions else None)
        conn.commit()
    finally:
        conn.close()
    log.info("forecast job complete", extra={"ctx": counts})
    return counts


def run_eval_job(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Score resolved paired forecasts; also updates the wealth ledger
    (Phase 14), the shadow MWU ensemble-weight challenger (Phase 14.1), and
    the CLV validity flag (Phase 17 item 4) -- all "wired into the existing
    nightly lab eval step -- no new CLI command." Guardrail 17: MWU is the
    one process permitted to update m4_weights between monthly `lab learn`
    cycles."""
    from lab.economy.mwu import update_mwu_challenger
    from lab.economy.wealth import update_wealth_ledger
    from lab.eval.clv import update_clv_trust_flag
    from lab.eval.run import run_eval
    from lab.store.snapshots import SnapshotStore

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        summaries = run_eval(conn, config)
        wealth_summary = update_wealth_ledger(conn, config)
        mwu_summary = update_mwu_challenger(conn, config)
        clv_trust = update_clv_trust_flag(conn, config, store)
    finally:
        conn.close()
    log.info("eval job complete", extra={"ctx": {"models": len(summaries), "wealth": wealth_summary,
                                                  "mwu": mwu_summary, "clv_trust": clv_trust}})
    return summaries


def run_report_job(config: dict[str, Any]) -> str:
    """Render the static HTML report."""
    from lab.eval.report import render_report

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        path = render_report(conn, store, config)
    finally:
        conn.close()
    log.info("report job complete", extra={"ctx": {"path": str(path)}})
    return str(path)


def run_shadow_job(config: dict[str, Any]) -> dict[str, Any]:
    """Simulated shadow portfolio: settle resolved, open new entries."""
    from lab.shadow.portfolio import portfolio_summary, run_shadow_entries, settle_resolved

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    try:
        settled = settle_resolved(conn)
        opened = run_shadow_entries(conn, store, config)
        summary = portfolio_summary(conn, store, config)
    finally:
        conn.close()
    result = {"opened": opened, "settled": settled, "summary": summary}
    log.info("shadow job complete", extra={"ctx": {"opened": opened, "settled": settled}})
    return result


def run_learn_job(config: dict[str, Any], apply: bool = False) -> Any:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems.

    Dry-run by default (writes nothing); pass apply=True to persist and promote.
    The orchestrator's scheduled run leaves apply=False, so every learning cycle
    is a reviewable diff rather than a silent mutation (brief section 6).
    """
    from lab.learn.loop import run_learn
    from lab.news.extract import create_llm_client

    conn = db.connect(config["storage"]["db_path"])
    llm = create_llm_client(conn, config)
    try:
        summary = run_learn(conn, config, llm, apply=apply)
    finally:
        conn.close()
    log.info("learn job complete", extra={"ctx": {"apply": apply, "summary": str(summary)[:200]}})
    return summary


def run_map_propose_job(config: dict[str, Any]) -> dict[str, Any]:
    """M7: LLM proposes candidate Kalshi matches into markets_map.yaml's `proposed`
    list -- never touches `confirmed`. Safe to run unattended: a pair only
    starts feeding M7 once a human runs `lab map confirm` on it (brief section 6,
    Phase 9 acceptance: "a proposed-but-unconfirmed pair is NOT forecast").
    Skips cleanly (no-op) if no LLM is configured, same as a missing API key
    would for M3."""
    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.models.m7_crossvenue import kalshi_propose_candidates, propose_matches
    from lab.news.extract import create_llm_client

    conn = db.connect(config["storage"]["db_path"])
    try:
        llm = create_llm_client(conn, config)
        if llm is None:
            log.info("map propose job: no LLM configured, skipping")
            return {"skipped": "no_llm"}
        async def _fetch_candidates() -> dict[str, list[Any]]:
            bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                                 burst=config["collect"]["rate_limit"]["burst"])
            kalshi = KalshiClient(bucket)
            try:
                # Category-scoped, not a bare open_markets(limit=200) -- that
                # pulled whatever Kalshi considers globally "open" (verified
                # live to be dominated by garbled multi-leg sports-combo
                # products), crowding out real Economics/Politics/Weather
                # candidates entirely. cli.py's `lab map propose` and the
                # dashboard button were already fixed to use this; this
                # scheduled path was the one call site still on the old,
                # buggy fetch.
                return await kalshi_propose_candidates(kalshi, config)
            finally:
                await kalshi.aclose()

        candidates = asyncio.run(_fetch_candidates())
        proposals = propose_matches(conn, config, candidates, llm)
    finally:
        conn.close()
    result = {"new_proposals": len(proposals)}
    log.info("map propose job complete", extra={"ctx": result})
    return result


def run_pmxt_verify_job(config: dict[str, Any]) -> dict[str, Any]:
    """Twice-daily companion to the weekly LLM-based `run_map_propose_job`:
    verifies candidate pairs an out-of-band pmxt Router scan
    (scripts/pmxt_router_scan.py, its own separate scheduled task -- never
    called from this process, see that script's docstring) wrote to
    data/pmxt_candidates.json, and appends the ones our own LLM check agrees
    with into markets_map.yaml's `proposed` list. Same propose-then-confirm
    contract as run_map_propose_job -- a human still runs `lab map confirm`
    before a pair is ever live. Skips cleanly if no LLM is configured or no
    candidates file exists yet.

    When this host actually added new proposals, also commits+pushes
    markets_map.yaml -- needed once the scan/verify cycle can run on a host
    other than the one that reads the file at forecast time (never blocks or
    re-triggers this job on failure, see commit_and_push_markets_map)."""
    from lab.models.m7_crossvenue import (
        commit_and_push_markets_map,
        repair_confirmed_kalshi_legs,
        verify_pmxt_candidates,
    )
    from lab.news.extract import create_llm_client

    conn = db.connect(config["storage"]["db_path"])
    try:
        # Before anything LLM-dependent: complete the Kalshi leg of any
        # already-confirmed pair still recorded as a stub. Deliberately ahead
        # of the no-LLM early return -- this repair costs no tokens, and a
        # host without an LLM key still needs its confirmed pairs usable.
        repair = repair_confirmed_kalshi_legs(conn, config)
        llm = create_llm_client(conn, config)
        if llm is None:
            log.info("pmxt verify job: no LLM configured, skipping")
            return {"skipped": "no_llm", "repair": repair}
        proposals = verify_pmxt_candidates(conn, config, llm)
    finally:
        conn.close()
    result = {"new_proposals": len(proposals), "repair": repair}
    if proposals:
        result["git"] = commit_and_push_markets_map(config)
    log.info("pmxt verify job complete", extra={"ctx": result})
    return result


def _db_push_due(conn, interval_days: int, key: str = "last_raw_db_push_ts") -> bool:
    """True if data/lab.db hasn't been pushed to the results repo in at least
    `interval_days` -- lab.db is a single ever-growing binary blob with no
    LFS delta compression, so pushing it as often as the (cheap, incremental)
    snapshots would burn a GitHub LFS free-tier month's bandwidth in days."""
    from datetime import datetime

    from lab.util import now_utc

    last = db.get_meta(conn, key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (now_utc() - last_dt).total_seconds() >= interval_days * 86400


def run_publish_job(config: dict[str, Any]) -> dict[str, Any]:
    """Mirror reports/exports/model artifacts to the private results repo and
    push -- always. Snapshots (new/changed parquet partitions) push every
    night too when publish.raw_data.snapshots_enabled is set: they're small
    and incremental (brief section 11: "the historical order-book snapshots
    cannot be re-downloaded later" is exactly the crown-jewel data this is
    for). data/lab.db additionally pushes only every
    publish.raw_data.db_interval_days when publish.raw_data.db_enabled is
    set, tracked via a `last_raw_db_push_ts` meta key. .env (every API
    key/secret) pushes every night when publish.raw_data.env_enabled is set
    -- no interval gating needed, it's tiny -- so the keys survive even if
    this laptop is lost; see publish.sync_env's docstring for the git-history
    tradeoff of backing up secrets at all. All three raw-data knobs default
    OFF, so an operator who wants only the curated nightly mirror (the
    pre-Phase-16 behavior) gets exactly that with no config changes. Never
    raises: a publish failure (e.g. no network) must not block or re-trigger
    the forecast/eval/report bundle it follows."""
    from lab.publish import publish_results
    from lab.util import now_utc_iso

    if not config.get("publish", {}).get("enabled", False):
        return {"skipped": "disabled"}
    raw_cfg = config.get("publish", {}).get("raw_data", {})
    include_snapshots = bool(raw_cfg.get("snapshots_enabled", False))
    include_env = bool(raw_cfg.get("env_enabled", False))
    include_ledger = bool(raw_cfg.get("ledger_increment_enabled", False))
    conn = db.connect(config["storage"]["db_path"])
    try:
        include_db = bool(raw_cfg.get("db_enabled", False)) and _db_push_due(
            conn, int(raw_cfg.get("db_interval_days", 3))
        )
        include_reference = bool(raw_cfg.get("reference_enabled", False)) and _db_push_due(
            conn, int(raw_cfg.get("reference_interval_days", 7)), key="last_reference_push_ts"
        )
        result = publish_results(
            config, conn, include_snapshots=include_snapshots, include_db=include_db,
            include_env=include_env, include_ledger=include_ledger,
            include_reference=include_reference,
        )
        # Every "it was backed up" marker below requires the push to have
        # SUCCEEDED, not merely the commit (2026-09-25). Stamping on commit
        # meant a refused push still counted as this week's db backup, and the
        # backup heartbeat was sent unconditionally -- so from 09-24 the
        # external monitor was told the backup was healthy every night while
        # GitHub refused every push and nothing left the host.
        backed_up = bool(result.get("pushed"))
        if include_db and result.get("pushed"):
            db.set_meta(conn, "last_raw_db_push_ts", now_utc_iso())
        if include_reference and result.get("pushed"):
            db.set_meta(conn, "last_reference_push_ts", now_utc_iso())
        from lab.heartbeat import active_alarms, send_heartbeat, set_alarm

        if backed_up:
            set_alarm(conn, "backup", None)
            asyncio.run(send_heartbeat("backup"))
        elif (result.get("committed") or result.get("unpushed_commits"))                 and not active_alarms(conn).get("backup"):
            # First refusal only: "since" must keep meaning the FIRST one.
            set_alarm(conn, "backup", f"results push refused since {now_utc_iso()}: "
                      + (result.get("push_stderr") or "")[-300:].strip())
        conn.commit()
    except Exception:
        log.exception("publish job failed")
        try:
            from lab.heartbeat import active_alarms, set_alarm

            if not active_alarms(conn).get("backup"):
                set_alarm(conn, "backup", f"publish job crashed since {now_utc_iso()}")
                conn.commit()
        except Exception:
            pass
        return {"error": "publish_failed"}
    finally:
        conn.close()
    log.info("publish job complete", extra={"ctx": result})
    return result


def run_ledger_commitment_job(config: dict[str, Any]) -> dict[str, Any]:
    """Phase 15: nightly cryptographic pre-registration commitment.

    Commits (and pushes) a sha256 over each closed UTC day's appended
    forecasts rows to THIS repo's docs/ledger_commitments.jsonl -- not the
    private results mirror publish.py targets. Never raises: a failed git
    step here must not block or re-trigger the forecast/eval/report bundle
    it follows, same contract as run_publish_job.
    """
    from lab.ledger_commitment import commit_and_push

    if not config.get("ledger", {}).get("enabled", True):
        return {"skipped": "disabled"}
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = commit_and_push(config, conn)
    except Exception:
        log.exception("ledger commitment job failed")
        return {"error": "ledger_commitment_failed"}
    finally:
        conn.close()
    log.info("ledger commitment job complete", extra={"ctx": result})
    return result


def run_paper_export_job(config: dict[str, Any]) -> dict[str, Any]:
    """Phase 15 addendum (v2.8): weekly automated `lab export --paper`
    snapshot, committed and pushed to the public repo. Never raises: a failed
    git step here must not block or re-trigger the forecast/eval/report
    bundle, same contract as run_publish_job / run_ledger_commitment_job."""
    from lab.paper_export import commit_and_push_paper_export

    if not config.get("paper_export", {}).get("enabled", True):
        return {"skipped": "disabled"}
    conn = db.connect(config["storage"]["db_path"])
    try:
        result = commit_and_push_paper_export(config, conn)
    except Exception:
        log.exception("paper export job failed")
        return {"error": "paper_export_failed"}
    finally:
        conn.close()
    log.info("paper export job complete", extra={"ctx": result})
    return result


def run_rollback_job(config: dict[str, Any], model_id: str,
                     to_version_tag: str | None = None) -> dict[str, Any]:
    """Manually revert a model's active version to a prior one (outside the cycle)."""
    from lab.learn import registry

    conn = db.connect(config["storage"]["db_path"])
    try:
        restored = registry.rollback(conn, config, model_id, reason="rollback",
                                     to_version_tag=to_version_tag)
    finally:
        conn.close()
    result = {"model_id": model_id,
              "restored": None if restored is None else restored["version_tag"]}
    log.info("rollback job complete", extra={"ctx": result})
    return result
