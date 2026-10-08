"""`lab status` gap detection on fixture data with a synthetic gap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from lab.collect.status import gap_windows, gather_status, snapshot_gaps
from lab.store import db
from lab.store.snapshots import SNAPSHOT_SCHEMA, SnapshotStore, floor_ts_bucket
from lab.util import load_config


def _frame(timestamps: list[str]) -> pl.DataFrame:
    rows = [
        {
            "ts": ts, "condition_id": "a", "token_id_yes": "tok",
            "best_bid": 0.49, "best_ask": 0.51, "mid": 0.5, "spread": 0.02,
            "bid_depth_usd": 1000.0, "ask_depth_usd": 900.0, "last_trade_price": None,
        }
        for ts in timestamps
    ]
    return pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)


def test_flags_synthetic_gap():
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)  # 12 buckets at 5-min cadence
    buckets = [start + timedelta(minutes=5 * i) for i in range(12)]
    # Drop two buckets in the middle: the synthetic gap.
    present = [b for i, b in enumerate(buckets) if i not in (5, 6)]
    df = _frame([floor_ts_bucket(b, 5) for b in present])

    assert snapshot_gaps(df, ["a"], 5, start, end) == 2


def test_full_coverage_has_no_gaps():
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    df = _frame([floor_ts_bucket(start + timedelta(minutes=5 * i), 5) for i in range(12)])
    assert snapshot_gaps(df, ["a"], 5, start, end) == 0


def test_gap_windows_returns_the_actual_missing_intervals():
    """Phase 17 item 5: snapshot_gaps' count is now derived FROM gap_windows,
    which callers like eval/clv.py's gap-aware drift need the actual
    intervals from, not just a count."""
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)  # 12 buckets at 5-min cadence
    buckets = [start + timedelta(minutes=5 * i) for i in range(12)]
    present = [b for i, b in enumerate(buckets) if i not in (5, 6)]
    df = _frame([floor_ts_bucket(b, 5) for b in present])

    windows = gap_windows(df, ["a"], 5, start, end)
    assert windows == [
        (start + timedelta(minutes=25), start + timedelta(minutes=30)),
        (start + timedelta(minutes=30), start + timedelta(minutes=35)),
    ]
    assert snapshot_gaps(df, ["a"], 5, start, end) == len(windows) == 2


def test_no_tracked_markets_reports_zero():
    assert snapshot_gaps(_frame([]), [], 5,
                         datetime(2026, 7, 2, tzinfo=timezone.utc),
                         datetime(2026, 7, 2, 1, tzinfo=timezone.utc)) == 0


def test_gap_windows_empty_window_returns_empty():
    """window_end <= window_start (n_buckets <= 0) -- no buckets to report."""
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    assert gap_windows(_frame(["2026-07-02T12:00:00+00:00"]), ["a"], 5, start, start) == []


def test_gap_windows_fully_uncovered_window():
    """Tracked markets exist but none has any row in range: every bucket gaps."""
    start = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15)  # 3 buckets at 5-min cadence
    df = _frame(["2026-07-01T00:00:00+00:00"])  # real data, but outside [start, end)
    windows = gap_windows(df, ["a"], 5, start, end)
    assert windows == [
        (start, start + timedelta(minutes=5)),
        (start + timedelta(minutes=5), start + timedelta(minutes=10)),
        (start + timedelta(minutes=10), start + timedelta(minutes=15)),
    ]


def test_gap_windows_matches_naive_reference_on_random_coverage():
    """Differential check: the sorted+bisect implementation against a
    deliberately naive, obviously-correct-by-inspection reference (the same
    predicate gap_windows itself documents: exists ts with
    bucket_start &lt;= ts &lt; bucket_end), across many random coverage patterns.
    Guards the exact optimization this test suite would otherwise only cover
    by hand-picked cases."""
    import random

    def naive_gap_windows(seen_iso, all_buckets):
        out = []
        for bstart, bend in all_buckets:
            s, e = bstart.isoformat(timespec="seconds"), bend.isoformat(timespec="seconds")
            if not any(s <= ts < e for ts in seen_iso):
                out.append((bstart, bend))
        return out

    rng = random.Random(20260720)
    start = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
    for trial in range(30):
        n_buckets = rng.randint(1, 60)
        end = start + timedelta(minutes=5 * n_buckets)
        all_buckets = [
            (start + timedelta(minutes=5 * i), start + timedelta(minutes=5 * (i + 1)))
            for i in range(n_buckets)
        ]
        present_idx = [i for i in range(n_buckets) if rng.random() < 0.6]
        present_iso = [floor_ts_bucket(start + timedelta(minutes=5 * i), 5) for i in present_idx]
        df = _frame(present_iso)

        got = gap_windows(df, ["a"], 5, start, end)
        want = naive_gap_windows(set(present_iso), all_buckets)
        assert got == want, f"trial {trial}: present_idx={present_idx}"


# --- per-venue status lines (Phase 10) --------------------------------------

def test_gather_status_reports_per_venue_health(tmp_path):
    config = load_config()
    config = {
        **config,
        "storage": {
            **config["storage"],
            "db_path": str(tmp_path / "lab.db"),
            "snapshots_dir": str(tmp_path / "snapshots"),
        },
    }
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "kalshi:T1", "venue": "kalshi", "venue_native_id": "T1",
        "slug": None, "question": "q", "category": "economics", "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })
    db.record_resolution(conn, "kalshi:T1", "2026-01-02T00:00:00Z", 1.0, False, "kalshi")
    db.upsert_market(conn, {
        "condition_id": "kalshi:T2", "venue": "kalshi", "venue_native_id": "T2",
        "slug": None, "question": "q2", "category": "economics", "description": "d2",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "tail",
    })  # closed, unresolved -- should count toward kalshi's closed_unresolved
    db.upsert_market(conn, {
        "condition_id": "manifold:M1", "venue": "manifold", "venue_native_id": "M1",
        "slug": None, "question": "q3", "category": "unknown", "description": None,
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": None, "volume_num": 5.0,
        "tier": "ignored",
    })
    conn.commit()
    conn.close()

    store = SnapshotStore(config["storage"]["snapshots_dir"])
    store.append([{
        "ts": floor_ts_bucket(datetime.now(timezone.utc), 5), "condition_id": "kalshi:T1",
        "token_id_yes": None, "best_bid": 0.5, "best_ask": 0.52, "mid": 0.51, "spread": 0.02,
        "bid_depth_usd": None, "ask_depth_usd": None, "last_trade_price": None,
        "bids_json": None, "asks_json": None, "venue": "kalshi",
    }])

    status = gather_status(config)
    assert status["venues"]["kalshi"]["markets"] == 2
    assert status["venues"]["kalshi"]["resolutions"] == 1
    assert status["venues"]["kalshi"]["closed_unresolved"] == 1
    assert status["venues"]["kalshi"]["last_snapshot_age_min"] is not None
    assert status["venues"]["metaculus"]["markets"] == 0
    assert status["venues"]["metaculus"]["last_snapshot_age_min"] is None
    assert status["venues"]["manifold"]["markets"] == 1
    assert "last_snapshot_age_min" not in status["venues"]["manifold"]


def test_kalshi_rotation_counts_live_series_without_a_correlated_scan(tmp_path):
    """The cursor query re-scanned every open Kalshi market once per cursor
    row; with ~200 dormant series joining the cursor a day it took the hourly
    health check from ~5.5 to ~11 minutes of CPU an hour (2026-10-01..08)."""
    from lab.collect.status import LIVE_SERIES_CURSOR_SQL, _kalshi_sync_rotation

    conn = db.connect(tmp_path / "lab.db")
    for ticker, open_ in (("KXFED-26DEC-T4.25", 1), ("KXFED-26DEC-T4.50", 1),
                          ("KXCPI-26OCT-B3", 0), ("SOLO", 1)):
        db.upsert_market(conn, {
            "condition_id": f"kalshi:{ticker}", "venue": "kalshi", "venue_native_id": ticker,
            "slug": None, "question": "q", "category": "economics", "description": "d",
            "end_date_iso": None, "token_id_yes": None, "token_id_no": None, "neg_risk": 0,
            "active": open_, "closed": 1 - open_, "liquidity_num": 1.0, "volume_num": 1.0,
            "tier": "tail"})
    for series, ts in (("KXFED", "2026-10-08T00:00:00+00:00"),   # live, two open markets
                       ("KXCPI", "2026-10-08T01:00:00+00:00"),   # only a closed market
                       ("SOLO", "2026-10-07T12:00:00+00:00"),    # ticker with no dash
                       ("KXDORMANT", "2026-10-01T00:00:00+00:00")):
        conn.execute("INSERT INTO kalshi_series_sync (series, attempted_ts) VALUES (?, ?)",
                     (series, ts))
    conn.commit()

    old = conn.execute("""
        SELECT s.attempted_ts AS ts FROM kalshi_series_sync s WHERE EXISTS (
          SELECT 1 FROM markets m WHERE m.venue = 'kalshi' AND m.active = 1 AND m.closed = 0
            AND substr(m.venue_native_id, 1, instr(m.venue_native_id || '-', '-') - 1) = s.series)
    """).fetchall()
    new = conn.execute(LIVE_SERIES_CURSOR_SQL).fetchall()
    assert sorted(r["ts"] for r in new) == sorted(r["ts"] for r in old) == [
        "2026-10-07T12:00:00+00:00", "2026-10-08T00:00:00+00:00"]
    plan = " ".join(str(tuple(r)) for r in conn.execute("EXPLAIN QUERY PLAN " + LIVE_SERIES_CURSOR_SQL))
    assert "CORRELATED" not in plan.upper()

    out = _kalshi_sync_rotation(conn, datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
    assert out["live_series_measured"] == 2 and out["series_with_cursor"] == 4
    assert out["age_h_max"] == 24.0
    conn.close()


# --- streaming gap detection (2026-07-28 render memory) --------------------

def test_streaming_gap_path_matches_the_whole_frame_path(tmp_path):
    """The report render used to materialise a 31-day (ts, condition_id) span
    -- ~600MB on the production host -- to derive what gap_windows actually
    needs: this tier's sorted unique timestamps. Reading day by day must give
    byte-identical windows, or the memory fix would have changed a reported
    number."""
    from datetime import datetime, timedelta, timezone

    from lab.collect.status import (
        cadence_buckets,
        gap_windows,
        gaps_from_timestamps,
        tier_snapshot_timestamps,
    )
    from lab.store.snapshots import SnapshotStore, utc_date_str

    store = SnapshotStore(tmp_path / "snapshots")
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=3)
    rows = []
    # Cover most 60-min buckets but deliberately leave two days with holes.
    for hour in range(72):
        if hour in (5, 6, 30, 31, 32, 60):
            continue
        ts = start + timedelta(hours=hour)
        rows.append({"ts": ts.isoformat(timespec="seconds"), "condition_id": "0x1",
                     "token_id_yes": "t", "best_bid": 0.4, "best_ask": 0.6,
                     "mid": 0.5, "spread": 0.2})
    rows.append({"ts": (start + timedelta(hours=5)).isoformat(timespec="seconds"),
                 "condition_id": "other", "token_id_yes": "t", "best_bid": 0.4,
                 "best_ask": 0.6, "mid": 0.5, "spread": 0.2})  # different tier
    store.append(rows)

    dates = sorted({utc_date_str(start + timedelta(hours=h)) for h in range(73)})
    whole_frame = store.read_range(dates, columns=["ts", "condition_id"])

    via_frame = gap_windows(whole_frame, ["0x1"], 60, start, end)
    via_stream = gaps_from_timestamps(
        tier_snapshot_timestamps(store, dates, ["0x1"]),
        cadence_buckets(60, start, end),
    )

    assert via_stream == via_frame
    assert via_frame, "fixture produced no gaps -- the test would prove nothing"


def test_tier_snapshot_timestamps_filters_to_the_tier():
    """A market outside the tier must not mask that tier's gap."""
    from datetime import datetime, timedelta, timezone

    from lab.collect.status import tier_snapshot_timestamps
    from lab.store.snapshots import SnapshotStore, utc_date_str
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = SnapshotStore(d)
        ts = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        store.append([{"ts": ts.isoformat(timespec="seconds"), "condition_id": "other",
                       "token_id_yes": "t", "best_bid": 0.4, "best_ask": 0.6,
                       "mid": 0.5, "spread": 0.2}])
        dates = [utc_date_str(ts)]
        assert tier_snapshot_timestamps(store, dates, ["0x1"]) == []
        assert tier_snapshot_timestamps(store, dates, ["other"]) == [
            ts.isoformat(timespec="seconds")]


def test_tier_snapshot_timestamps_empty_tier_is_empty():
    from lab.collect.status import tier_snapshot_timestamps

    assert tier_snapshot_timestamps(None, ["2026-07-01"], []) == []


# --- cgroup memory budget (2026-07-30 box wedge) --------------------------

def _fake_systemd(monkeypatch, caps: dict, mem_total_kb: int = 967 * 1024):
    """Stand in for systemctl + /proc/meminfo so the check is testable on any
    host, including the Windows laptop where neither exists."""
    import builtins
    import subprocess as sp

    from lab.collect import status as st

    monkeypatch.setattr(st.shutil if hasattr(st, "shutil") else __import__("shutil"),
                        "which", lambda _n: "/usr/bin/systemctl", raising=False)
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/systemctl")

    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/meminfo":
            import io
            return io.StringIO(f"MemTotal:       {mem_total_kb} kB\n")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)

    class _Res:
        def __init__(self, out):
            self.stdout = out

    def fake_run(cmd, **k):
        unit = cmd[2]
        return _Res(caps.get(unit, ""))

    monkeypatch.setattr(sp, "run", fake_run)


def test_memory_budget_flags_the_real_oversubscription(monkeypatch):
    """THE regression case, with the exact numbers that wedged the box:
    lab-run 900M + lab-dashboard 600M on a 967M host. Neither unit breaches
    its own cap, so no kernel OOM ever fires -- the machine just thrashes."""
    from lab.collect.status import memory_budget

    _fake_systemd(monkeypatch, {
        "lab-run.service": str(900 * 1024 * 1024),
        "lab-dashboard.service": str(600 * 1024 * 1024),
    })
    b = memory_budget()
    assert b is not None
    assert b["oversubscribed"] is True
    assert b["capped_total_bytes"] > b["physical_bytes"]


def test_memory_budget_accepts_the_applied_fix(monkeypatch):
    """The caps actually deployed after the incident: 700M + 200M = 900M,
    under 967M."""
    from lab.collect.status import memory_budget

    _fake_systemd(monkeypatch, {
        "lab-run.service": str(700 * 1024 * 1024),
        "lab-dashboard.service": str(200 * 1024 * 1024),
    })
    assert memory_budget()["oversubscribed"] is False


def test_memory_budget_treats_exact_equality_as_oversubscribed(monkeypatch):
    """Caps summing to exactly RAM leave nothing for the kernel, sshd or
    journald -- that is the wedge, not the safe boundary."""
    from lab.collect.status import memory_budget

    _fake_systemd(monkeypatch, {
        "lab-run.service": str(967 * 1024 * 1024),
        "lab-dashboard.service": "infinity",
    })
    assert memory_budget()["oversubscribed"] is True


def test_memory_budget_records_uncapped_units_rather_than_counting_zero(monkeypatch):
    """An uncapped unit contributes no bound to the sum, but pretending it
    contributes nothing to the MACHINE would be the same blind spot again --
    so it is named, not silently dropped."""
    from lab.collect.status import memory_budget

    _fake_systemd(monkeypatch, {
        "lab-run.service": str(400 * 1024 * 1024),
        "lab-dashboard.service": "infinity",
    })
    b = memory_budget()
    assert b["uncapped_units"] == ["lab-dashboard.service"]
    assert b["caps"]["lab-dashboard.service"] is None
    assert b["capped_total_bytes"] == 400 * 1024 * 1024


def test_memory_budget_returns_none_without_systemd(monkeypatch):
    """The operator's laptop has no cgroups; the check must say 'not
    applicable' rather than invent a verdict."""
    import shutil

    from lab.collect.status import memory_budget

    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert memory_budget() is None


def test_format_status_warns_loudly_when_oversubscribed():
    """The whole point is that it cannot be read as healthy."""
    from lab.collect.status import format_status

    gb = 1024 ** 3
    out = format_status({
        "ts": "2026-07-30T12:00:00+00:00",
        "markets_by_tier": {}, "forecast_rows": 0, "resolutions": 0, "tiers": {},
        "resolution_watcher": {"closed_unresolved": 0, "backlog": 0,
                               "never_checked": 0, "oldest_check_age_h": None},
        "venues": {}, "llm_spend_today_usd": 0.0, "llm_daily_cap_usd": 5.0,
        "memory_budget": {
            "physical_bytes": int(0.94 * gb),
            "caps": {"lab-run.service": int(0.88 * gb),
                     "lab-dashboard.service": int(0.59 * gb)},
            "capped_total_bytes": int(1.47 * gb),
            "uncapped_units": [],
            "oversubscribed": True,
        },
    })
    assert "OVERSUBSCRIBED" in out
    assert "memory caps:" in out
