"""`lab` CLI skeleton (Phase 0).

Commands are wired to their implementations phase by phase; until then each
prints a clear not-implemented notice and exits non-zero so cron jobs fail
loudly rather than silently succeeding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import typer

from lab.util import load_config, setup_logging, use_stable_event_loop

use_stable_event_loop()

_log = logging.getLogger("lab.crash")


def _log_uncaught_exception(exc_type, exc_value, exc_tb) -> None:
    """Last-resort handler: any exception that would otherwise only print to a
    console window (invisible once that window closes, e.g. start.bat) or
    vanish entirely gets one guaranteed line -- with full traceback -- in
    data/logs/lab.jsonl before the process exits. KeyboardInterrupt still
    prints normally (Ctrl+C is an expected, deliberate stop, not a crash)."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _log.critical("uncaught exception -- process is about to exit",
                  exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _log_uncaught_exception

app = typer.Typer(
    name="lab",
    help="Polymarket Forecast Lab -- read-only forecasting research instrument.",
    no_args_is_help=True,
)


def _not_implemented(command: str, phase: str) -> None:
    typer.secho(
        f"`lab {command}` is not implemented yet (arrives in {phase}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.callback()
def main() -> None:
    """Initialize config and logging for every command."""
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging(load_config())


@app.command()
def sync() -> None:
    """Discover markets from Gamma and update the universe with tiering."""
    from lab.api.gamma import GammaClient
    from lab.api.http import TokenBucket
    from lab.collect.universe import sync_universe
    from lab.store import db
    from lab.store.snapshots import SnapshotStore

    config = load_config()

    async def _run() -> dict:
        bucket = TokenBucket(
            rate=config["collect"]["rate_limit"]["requests_per_second"],
            burst=config["collect"]["rate_limit"]["burst"],
        )
        gamma = GammaClient(bucket)
        conn = db.connect(config["storage"]["db_path"])
        store = SnapshotStore(config["storage"]["snapshots_dir"])
        try:
            return await sync_universe(gamma, conn, store, config)
        finally:
            await gamma.aclose()
            conn.close()

    counts = asyncio.run(_run())
    typer.echo(f"universe sync: {counts}")


@app.command()
def exclude(
    venue: str = typer.Argument(..., help="e.g. polymarket, kalshi."),
    venue_native_id: str = typer.Argument(..., help="condition_id or venue-native market id."),
) -> None:
    """Manually record a market as excluded from the universe (Phase 15's
    universe_log, reason_code='manual') -- a thin, log-only annotation, not a
    propose/confirm flow: it does not itself stop the market from being
    forecast if it's otherwise eligible."""
    from lab.collect.universe import log_universe_exclusion
    from lab.store import db

    conn = db.connect(load_config()["storage"]["db_path"])
    try:
        log_universe_exclusion(conn, venue, venue_native_id, "manual")
        conn.commit()
    finally:
        conn.close()
    typer.echo(f"exclude: logged ({venue}, {venue_native_id}) as manual")


@app.command()
def collect() -> None:
    """Run the long-lived collection process (snapshots + resolution watcher)."""
    from lab.collect.runner import run_collect

    asyncio.run(run_collect(load_config()))


@app.command()
def run() -> None:
    """One-button orchestrator: collector + scheduled forecast/eval/report/shadow/learn."""
    from lab.collect.runner import run_orchestrator

    typer.echo("Forecast Lab orchestrator starting (Ctrl+C to stop)...")
    asyncio.run(run_orchestrator(load_config()))


@app.command()
def watchdog() -> None:
    """Supervise `lab run`: auto-restarts it after any exit, after a cooldown
    delay (config.yaml watchdog.restart_delay_seconds, default 10 min)."""
    from lab.collect.runner import run_watchdog

    typer.echo("Forecast Lab watchdog starting (auto-restarts `lab run`, Ctrl+C to stop everything)...")
    run_watchdog(load_config())


@app.command()
def forecast() -> None:
    """Generate forecasts for the eligible universe and freeze them in the ledger."""
    from lab.jobs import run_forecast_job

    counts = run_forecast_job(load_config())
    typer.echo(f"forecast run: {counts}")


@app.command()
def eval(
    include_disputed: bool = typer.Option(
        False, "--include-disputed",
        help="PAP Addendum 9.2(b) robustness re-run: score with disputed markets "
             "included instead of excluded, written under a '_disputed_inclusive' "
             "window_label alongside (not replacing) the primary eval_runs rows. "
             "Not part of the nightly job; run manually when the comparison is needed.",
    ),
) -> None:
    """Score resolved forecasts: paired Brier/log-loss, skill with bootstrap CIs."""
    if include_disputed:
        from lab.eval.run import run_eval
        from lab.store import db

        conn = db.connect(load_config()["storage"]["db_path"])
        try:
            summaries = run_eval(conn, load_config(), include_disputed=True)
        finally:
            conn.close()
    else:
        from lab.jobs import run_eval_job

        summaries = run_eval_job(load_config())
    if not summaries:
        typer.echo("eval: no resolved paired forecasts yet (INSUFFICIENT DATA)")
    for s in summaries:
        r = s["result"]
        typer.echo(
            f"  {s['model_id']} [{s['window']}] n={r.n} skill={r.skill:+.4f} "
            f"CI=[{r.skill_ci_lo:+.4f},{r.skill_ci_hi:+.4f}] mde={r.mde:.4f}"
        )


@app.command()
def report() -> None:
    """Render the static HTML report from evaluation results."""
    from lab.jobs import run_report_job

    path = run_report_job(load_config())
    typer.echo(f"report: {path}")


@app.command()
def shadow() -> None:
    """Run the simulated shadow portfolio (SIMULATION only, no real money)."""
    from lab.jobs import run_shadow_job

    result = run_shadow_job(load_config())
    typer.echo(f"shadow (SIMULATION): opened={result['opened']} settled={result['settled']}")
    typer.echo(f"  {result['summary']}")


@app.command()
def export(
    out: str = typer.Option(None, help="Output file; stdout when omitted."),
    paper: bool = typer.Option(
        False, "--paper", help="Full resolved-forecast replication dataset + manifest (Phase 15)."),
) -> None:
    """Emit latest forecast per (market, model) as JSONL -- the downstream integration point.

    `--paper` emits a different, second dataset instead: every resolved
    forecast from every model, plus a `<out>.meta.json` manifest (code
    version hash, schema version, row count) so a reviewer can verify what
    they're re-analyzing. Requires --out (a manifest has nowhere sensible to
    go on stdout)."""
    from pathlib import Path

    from lab.store import db

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        if paper:
            if not out:
                typer.secho("export --paper requires --out (manifest needs a file path)",
                           fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            import gzip
            import io

            from lab.export import paper_export_manifest, write_paper_export_stream

            # The scheduled weekly snapshot is gzipped (it outgrew GitHub's
            # 100MB per-file limit); this manual flow is NOT, unless the
            # operator asks for it by naming a .gz path. --out is operator-
            # chosen, and silently gzipping a file called foo.jsonl is a
            # surprise, not a service.
            if str(out).endswith(".gz"):
                with open(out, "wb") as raw:
                    with gzip.GzipFile(filename=Path(out).name[:-3], mode="wb",
                                       fileobj=raw, compresslevel=9, mtime=0) as gz:
                        with io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as text:
                            n, rows_sha256 = write_paper_export_stream(conn, text)
            else:
                with open(out, "w", encoding="utf-8", newline="\n") as fh:
                    n, rows_sha256 = write_paper_export_stream(conn, fh)
            manifest = paper_export_manifest(conn, n, rows_sha256)
            Path(f"{out}.meta.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8")
            typer.echo(f"export --paper: {n} rows -> {out} "
                      f"(manifest: {out}.meta.json, code_version={manifest['code_version']})")
            return
        from lab.export import export_jsonl

        lines = list(export_jsonl(conn))
    finally:
        conn.close()
    if out:
        Path(out).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        typer.echo(f"export: {len(lines)} rows -> {out}")
    else:
        for line in lines:
            typer.echo(line)


@app.command()
def status() -> None:
    """Data health: snapshot freshness, gaps, watcher lag, ledger counts, LLM spend."""
    from lab.collect.status import format_status, gather_status

    typer.echo(format_status(gather_status(load_config())))


@app.command("verify-ledger")
def verify_ledger_cmd() -> None:
    """Recompute every ledger commitment's hash from the DB and report.

    The independent-verifiability procedure the paper describes, made
    runnable. Exits non-zero only when some date has NO valid commitment --
    the condition that would actually mean the ledger was edited. Records
    superseded during the 2026-07-10 dual-host cutover are listed separately
    (see docs/ledger_commitment_incidents.md).
    """
    from lab.ledger_commitment import verify_ledger
    from lab.store import db
    from lab.util import PROJECT_ROOT

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        path = PROJECT_ROOT / config.get("ledger", {}).get(
            "commitments_path", "docs/ledger_commitments.jsonl")
        result = verify_ledger(conn, path)
    finally:
        conn.close()

    typer.echo(f"ledger: {path}")
    typer.echo(f"  records={result['records']}  dates={result['dates']}  "
               f"verified={result['dates_verified']}")
    for s in result["superseded"]:
        typer.echo(f"  superseded (dual-host cutover): {s['date']} "
                   f"ids {s['first_id']}..{s['last_id']} n={s['row_count']}")
    if result["dates_unverified"]:
        typer.echo(f"  UNVERIFIED DATES: {', '.join(result['dates_unverified'])}")
        raise typer.Exit(code=1)
    typer.echo("  OK -- every date has a verifying commitment")


@app.command()
def bootstrap(
    source: str = typer.Option(
        "hf",
        help="'hf' = quant.parquet + markets.parquet (brief-pinned; ~21-27 GB download). "
             "'clob' = markets.parquet + CLOB prices-history (light fallback).",
    ),
    sample_size: int = typer.Option(2000, help="[clob] Top-volume resolved markets to fetch."),
    min_volume: float = typer.Option(10000.0, help="[clob] Minimum lifetime volume (USD)."),
    max_per_market: int = typer.Option(0, help="[hf] Cap observations per market (0 = no cap)."),
    skip_fetch: bool = typer.Option(False, help="Reuse existing observations.parquet; refit only."),
) -> None:
    """Phase 2: historical bootstrap -- download resolved markets, fit M1/M2 artifacts."""
    from lab.learn import bootstrap as bs
    from lab.learn.plots import plot_m1_curves
    from lab.learn.refit import fit_m1_curves, fit_m2_baserates, save_artifact

    config = load_config()
    if not skip_fetch:
        asyncio.run(bs.run_bootstrap(
            config, source=source, sample_size=sample_size, min_volume=min_volume,
            max_per_market=(max_per_market or None)))
    obs = bs.load_observations(config)
    typer.echo(f"observations: {len(obs)} rows / {obs['condition_id'].n_unique()} markets")

    # The frame, not dicts: this file is 1,967,376 rows and expanding it cost
    # ~1.8GB, which is more than the production host has. `lab learn` stopped
    # doing this on 2026-08-05; this path was missed then.
    m1 = fit_m1_curves(obs)
    save_artifact(config, "m1_curves", m1)
    for name, fit in m1["buckets"].items():
        typer.echo(f"  m1 {name}: alpha={fit['alpha']:.3f} beta={fit['beta']:.3f} n={fit['n']}")

    per_market = obs.group_by("condition_id").first().select("category", "outcome")
    m2 = fit_m2_baserates(per_market)
    save_artifact(config, "m2_baserates", m2)
    typer.echo(f"  m2 base rates: {len(m2['categories'])} categories")

    for path in plot_m1_curves(m1, config):
        typer.echo(f"  plot: {path}")


@app.command()
def learn(
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Persist refits and promote/rollback versions. Default is a dry-run diff "
             "that writes nothing (brief section 6).",
    ),
) -> None:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems.

    Dry-run by default: computes every proposed change and prints a diff without
    touching model_versions or ACTIVE.json. Pass --apply to commit.
    """
    from lab.jobs import run_learn_job

    summary = run_learn_job(load_config(), apply=apply)
    mode = "APPLIED" if apply else "DRY-RUN (nothing written; pass --apply to commit)"
    typer.echo(f"learn [{mode}]: {summary}")


@app.command()
def rollback(
    model_id: str = typer.Argument(..., help="Model key, e.g. m1_curves, m3_params, m4_weights."),
    to: str = typer.Option(None, "--to", help="Target version_tag (default: previous promotable)."),
) -> None:
    """Revert a model's active version to a prior one (manual override)."""
    from lab.jobs import run_rollback_job

    result = run_rollback_job(load_config(), model_id, to_version_tag=to)
    if result["restored"] is None:
        typer.secho(f"rollback: nothing to restore for {model_id}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rollback: {model_id} -> {result['restored']} (active)")


@app.command()
def guard(
    retire_outdated: bool = typer.Option(
        False,
        "--retire-outdated",
        help="Also stop a lone outdated instance (use before restarting after a code update).",
    ),
) -> None:
    """Stop redundant or unmanaged lab instances (orchestrator, collector, dashboard, watchdog)."""
    from lab import process_guard

    result = process_guard.cleanup(load_config(), retire_sole_outdated=retire_outdated)
    stopped = result.get("stopped") or []
    if stopped:
        typer.echo(f"guard: stopped pids {stopped}")
    else:
        typer.echo("guard: nothing to stop")


@app.command()
def ps() -> None:
    """List our own running instances; flag outdated code versions and duplicates."""
    import time

    from lab import process_guard

    config = load_config()
    snap = process_guard.report(config)
    typer.echo(f"current code version: {snap['current_version']}")
    typer.echo("managed instances:")
    if not snap["managed"]:
        typer.echo("  (none registered)")
    for e in snap["managed"]:
        age_min = (time.time() - (e.get("start_ts") or time.time())) / 60
        flags = []
        if e.get("pid") in snap["flagged_pids"]:
            flags.append("REDUNDANT/OUTDATED")
        if e.get("code_version") != snap["current_version"]:
            flags.append("stale-version")
        tag = f"  [{' '.join(flags)}]" if flags else ""
        typer.echo(
            f"  {e.get('role'):12} pid={e.get('pid'):<7} ver={e.get('code_version')} "
            f"age={age_min:.0f}min{tag}"
        )
    if snap["unmanaged"]:
        typer.echo("unmanaged lab-looking processes (not registered; consider stopping):")
        for e in snap["unmanaged"]:
            age_min = (time.time() - (e.get("start_ts") or time.time())) / 60
            flags = []
            if e.get("pid") in snap["flagged_pids"]:
                flags.append("REDUNDANT/OUTDATED")
            tag = f"  [{' '.join(flags)}]" if flags else ""
            typer.echo(f"  {e.get('role'):12} pid={e.get('pid'):<7} age={age_min:.0f}min{tag}")


map_app = typer.Typer(help="Cross-venue question matching (M7, Phase 9): propose-then-confirm.")
app.add_typer(map_app, name="map")


@map_app.command("propose")
def map_propose() -> None:
    """LLM proposes candidate Kalshi matches for top liquid priority-category markets.

    Metaculus isn't reachable without an account (see api/metaculus.py) --
    use `lab map confirm --venue metaculus` for a hand-curated pair instead.
    """
    import asyncio as _asyncio

    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.models.m7_crossvenue import kalshi_propose_candidates, propose_matches
    from lab.news.extract import create_llm_client
    from lab.store import db

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    llm = create_llm_client(conn, config)
    if llm is None:
        typer.secho("map propose: no LLM configured (see llm.api_key_env in config.yaml)",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    async def _run():
        bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                             burst=config["collect"]["rate_limit"]["burst"])
        kalshi = KalshiClient(bucket)
        try:
            candidates = await kalshi_propose_candidates(kalshi, config)
            return propose_matches(conn, config, candidates, llm)
        finally:
            await kalshi.aclose()

    try:
        proposals = _asyncio.run(_run())
    finally:
        conn.close()
    typer.echo(f"map propose: {len(proposals)} new candidate(s) added to data/markets_map.yaml")
    for p in proposals:
        typer.echo(f"  {p['condition_id']} <-> kalshi:{p['external_id']} "
                   f"(confidence={p['confidence']:.2f}) {p['rationale']}")


@map_app.command("confirm")
def map_confirm(
    condition_id: str = typer.Argument(..., help="Polymarket condition_id."),
    venue: str = typer.Option(..., help="kalshi | metaculus"),
    external_id: str = typer.Option(
        None, help="Required if not already in `proposed` (e.g. a hand-found Metaculus pair)."),
) -> None:
    """Move a proposed pair into `confirmed` -- or confirm a hand-curated one directly.

    Also mints (or reuses) the event_id linking the two venue-markets in the DB
    (brief section 5/Phase 10) -- the markets_map.yaml confirmation is the one
    and only place an event gets created, matching M7's single source of truth.
    """
    from lab.models.m7_crossvenue import confirm_match, link_confirmed_event, load_markets_map, save_markets_map
    from lab.store import db

    data = load_markets_map()
    ok = confirm_match(data, condition_id, venue, external_id=external_id)
    if not ok:
        typer.secho(
            f"map confirm: no proposed ({condition_id}, {venue}) entry -- pass --external-id "
            "to confirm a hand-curated pair directly",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    save_markets_map(data)
    entry = next(e for e in data["confirmed"] if e["condition_id"] == condition_id and e["venue"] == venue)

    conn = db.connect(load_config()["storage"]["db_path"])
    try:
        event_id = link_confirmed_event(conn, condition_id, venue, entry["external_id"])
    finally:
        conn.close()
    typer.echo(f"map confirm: {condition_id} <-> {venue} is now live for M7 (event_id={event_id})")


@map_app.command("list")
def map_list() -> None:
    """Show confirmed and proposed (awaiting human review) pairs."""
    from lab.models.m7_crossvenue import load_markets_map

    data = load_markets_map()
    typer.echo(f"confirmed ({len(data['confirmed'])}):")
    for e in data["confirmed"]:
        typer.echo(f"  {e['condition_id']} <-> {e['venue']}:{e['external_id']}")
    typer.echo(f"proposed, awaiting confirmation ({len(data['proposed'])}):")
    for e in data["proposed"]:
        typer.echo(f"  {e['condition_id']} <-> {e['venue']}:{e['external_id']} "
                   f"(confidence={e.get('confidence', 0):.2f})")


@map_app.command("pmxt-verify")
def map_pmxt_verify() -> None:
    """LLM-verify pmxt Router candidates (data/pmxt_candidates.json) into `proposed`.

    A standalone, manually/cron-invokable wrapper around the same job the
    orchestrator (`lab run`) fires twice daily on `cross_venue.pmxt_verify_cron`
    -- for a host that runs the pmxt scan+verify cycle on its own schedule
    (e.g. a native OS timer) without running the full orchestrator. Commits
    (and, per config, pushes) markets_map.yaml when it actually gains new
    proposals, so any other host reads them on its next `git pull`.
    """
    from lab.jobs import run_pmxt_verify_job

    result = run_pmxt_verify_job(load_config())
    if "skipped" in result:
        typer.echo(f"map pmxt-verify: skipped ({result['skipped']})")
        return
    typer.echo(f"map pmxt-verify: {result['new_proposals']} new proposal(s)")
    if "git" in result:
        typer.echo(f"  git: {result['git']}")


if __name__ == "__main__":
    app()
