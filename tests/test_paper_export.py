"""Phase 15 addendum (v2.8): automated weekly `lab export --paper` snapshot,
committed and pushed to the public repo.

Covers: dated path naming, byte-identical reuse of export_paper_jsonl, same-day
idempotency, real git commit creation, revert-on-commit-failure + retry, and
the never-raise job boundary (mirrors tests/test_ledger_commitment.py's
git-orchestration pattern and tests/test_export_paper.py's fixture seeding).
"""

from __future__ import annotations

import gzip
import json
import subprocess
from datetime import datetime, timezone

import pytest

from lab import paper_export as pe
from lab.export import export_paper_jsonl
from lab.store import db
from lab.util import load_config

FIXED_NOW = datetime(2026, 7, 10, 5, 0, 0, tzinfo=timezone.utc)


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
    cfg["paper_export"] = {"enabled": True, "dir": "docs/paper_exports", "push": False}
    return cfg


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(pe, "now_utc", lambda: FIXED_NOW)


@pytest.fixture(autouse=True)
def sandbox_project_root(monkeypatch, tmp_path):
    """Never let a test write into the real repo's docs/paper_exports --
    tests that need a real git repo explicitly re-monkeypatch this to their
    own temp repo path afterward."""
    monkeypatch.setattr(pe, "PROJECT_ROOT", tmp_path)


def _seed(conn, n_markets: int = 3):
    for i in range(n_markets):
        cid = f"0x{i}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, tier,
                                    active, closed, end_date_iso, venue, event_id)
               VALUES (?, ?, ?, 'politics', 'liquid', 1, 1, '2026-12-31T00:00:00+00:00',
                       'polymarket', ?)""",
            (cid, f"market-{i}", f"Question {i}?", f"evt-{i}"),
        )
        outcome = float(i % 2)
        db.record_resolution(conn, cid, "2026-07-01T00:00:00+00:00", outcome, False, "gamma")
        db.append_forecast(conn, {
            "ts": "2026-06-01T00:00:00+00:00",
            "condition_id": cid,
            "model_id": "m_test",
            "p_yes": 0.7 if outcome else 0.3,
            "p_market_at_ts": 0.5,
            "spread_at_ts": 0.02,
        })
    conn.commit()


def _init_git_repo(path):
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.local"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(args, cwd=path, capture_output=True, text=True, check=True)
    (path / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, text=True, check=True)


def _install_failing_precommit_hook(repo):
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


# --- paths ------------------------------------------------------------

def test_paper_export_paths_use_today_by_default(config):
    gz_path, meta_path = pe.paper_export_paths(config)
    assert gz_path.name == "2026-07-10.jsonl.gz"
    assert meta_path.name == "2026-07-10.jsonl.gz.meta.json"
    assert gz_path.parent.name == "paper_exports"


def test_paper_export_paths_honor_explicit_dt(config):
    dt = datetime(2026, 1, 5, tzinfo=timezone.utc)
    gz_path, meta_path = pe.paper_export_paths(config, dt=dt)
    assert gz_path.name == "2026-01-05.jsonl.gz"
    assert meta_path.name == "2026-01-05.jsonl.gz.meta.json"


# --- write_paper_export -------------------------------------------------

def test_write_paper_export_matches_export_paper_jsonl(config, conn):
    _seed(conn)
    result = pe.write_paper_export(conn, config)
    assert result["written"] is True
    assert result["row_count"] == 3

    gz_path, meta_path = pe.paper_export_paths(config)
    with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
        written_lines = fh.read().splitlines()
    direct_lines = list(export_paper_jsonl(conn))
    assert written_lines == direct_lines
    for line in written_lines:
        json.loads(line)  # valid JSON

    manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    assert manifest["row_count"] == 3
    assert manifest["fields"]


def test_write_paper_export_is_idempotent_same_day(config, conn):
    _seed(conn)
    first = pe.write_paper_export(conn, config)
    assert first["written"] is True

    second = pe.write_paper_export(conn, config)
    assert second == {"written": False, "reason": "already_exists",
                      "path": second["path"]}
    gz_path, _ = pe.paper_export_paths(config)
    assert second["path"] == str(gz_path)


# --- git orchestration -----------------------------------------------------

def test_commit_and_push_creates_real_git_commit(config, conn, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.setattr(pe, "PROJECT_ROOT", repo)

    _seed(conn)
    result = pe.commit_and_push_paper_export(config, conn)
    assert result["committed"] is True
    assert result["row_count"] == 3

    log = subprocess.run(["git", "log", "--name-only", "-1"], cwd=repo, capture_output=True, text=True)
    assert "docs/paper_exports/2026-07-10.jsonl.gz" in log.stdout
    assert "docs/paper_exports/2026-07-10.jsonl.gz.meta.json" in log.stdout


def test_commit_and_push_reverts_files_on_commit_failure_then_retry_succeeds(
    config, conn, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _install_failing_precommit_hook(repo)
    monkeypatch.setattr(pe, "PROJECT_ROOT", repo)

    _seed(conn)
    gz_path, meta_path = pe.paper_export_paths(config)

    result = pe.commit_and_push_paper_export(config, conn)
    assert "error" in result
    # Both newly-written files must be gone -- otherwise a retry would think
    # today's date was already exported and skip it, permanently orphaning it.
    assert not gz_path.exists()
    assert not meta_path.exists()

    (repo / ".git" / "hooks" / "pre-commit").unlink()
    retry = pe.commit_and_push_paper_export(config, conn)
    assert retry["committed"] is True

    log_count = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    )
    # init commit + exactly one paper-export commit
    assert len(log_count.stdout.strip().splitlines()) == 2


# --- job wrapper ------------------------------------------------------

def test_job_skipped_when_disabled(config):
    from lab import jobs

    config["paper_export"]["enabled"] = False
    result = jobs.run_paper_export_job(config)
    assert result == {"skipped": "disabled"}


def test_job_never_raises_when_commit_and_push_raises(config, monkeypatch):
    from lab import jobs

    def boom(_config, _conn):
        raise RuntimeError("boom")

    monkeypatch.setattr("lab.paper_export.commit_and_push_paper_export", boom)
    result = jobs.run_paper_export_job(config)
    assert result == {"error": "paper_export_failed"}


# --- gzip container (2026-09-08, PAP 9.28) ---------------------------------

def test_the_gz_round_trips_to_exactly_export_paper_jsonl(config, conn):
    """The container changed; the dataset did not."""
    _seed(conn)
    pe.write_paper_export(conn, config)
    gz_path, _ = pe.paper_export_paths(config)
    with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
        assert fh.read().splitlines() == list(export_paper_jsonl(conn))


def test_two_exports_of_the_same_rows_are_byte_identical(config, conn, tmp_path):
    """gzip stamps time.time() and a basename into its header unless told not
    to. §15 exists so a reviewer can re-derive an artifact and get the same
    bytes back; a refactor to a bare gzip.open() would silently end that, and
    this is the test that would notice."""
    _seed(conn)
    pe.write_paper_export(conn, config)
    gz_path, meta_path = pe.paper_export_paths(config)
    first = gz_path.read_bytes()

    gz_path.unlink()
    meta_path.unlink()
    pe.write_paper_export(conn, config)
    assert gz_path.read_bytes() == first


def test_a_manifest_failure_leaves_no_payload_behind(config, conn, monkeypatch):
    """The exists() gate treats a lone .gz as "already exported", so a payload
    written without its manifest would orphan that date permanently -- and the
    two-file pair is what docs/paper_export_schema.md promises reviewers."""
    _seed(conn)

    def boom(*a, **kw):
        raise RuntimeError("code_version failed")

    monkeypatch.setattr("lab.export.paper_export_manifest", boom)
    with pytest.raises(RuntimeError):
        pe.write_paper_export(conn, config)

    gz_path, meta_path = pe.paper_export_paths(config)
    assert not gz_path.exists() and not meta_path.exists()
    assert not list(gz_path.parent.glob("*.tmp")), "no litter either"


def test_a_legacy_plain_jsonl_for_today_still_counts_as_exported(config, conn):
    """Deploy-day only, and it matters exactly once: the extension changed, so
    without this a catch-up run would write a second export pair for a date
    already snapshotted as plain .jsonl."""
    _seed(conn)
    gz_path, _ = pe.paper_export_paths(config)
    legacy = gz_path.with_suffix("")           # .../2026-07-10.jsonl
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}\n", encoding="utf-8")

    result = pe.write_paper_export(conn, config)
    assert result["written"] is False and result["reason"] == "already_exists"
    assert not gz_path.exists()
