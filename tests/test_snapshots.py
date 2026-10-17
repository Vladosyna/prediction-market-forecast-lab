"""Parquet snapshot store: dedup on (ts, condition_id), bucket flooring."""

from __future__ import annotations

from datetime import datetime, timezone

from lab.store.snapshots import SNAPSHOT_SCHEMA, SnapshotStore, floor_ts_bucket


def _row(ts: str, cid: str, mid: float = 0.5) -> dict:
    return {
        "ts": ts,
        "condition_id": cid,
        "token_id_yes": "tok-" + cid,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "mid": mid,
        "spread": 0.02,
        "bid_depth_usd": 1000.0,
        "ask_depth_usd": 900.0,
        "last_trade_price": None,
    }


def test_floor_ts_bucket():
    ts = datetime(2026, 7, 2, 13, 47, 33, tzinfo=timezone.utc)
    assert floor_ts_bucket(ts, 5) == "2026-07-02T13:45:00+00:00"
    assert floor_ts_bucket(ts, 60) == "2026-07-02T13:00:00+00:00"


def test_append_dedups_on_ts_and_market(tmp_path):
    store = SnapshotStore(tmp_path)
    ts = "2026-07-02T13:45:00+00:00"

    assert store.append([_row(ts, "a"), _row(ts, "b")]) == 2
    # Restart replays the same bucket: nothing new may be written.
    assert store.append([_row(ts, "a", mid=0.9), _row(ts, "b")]) == 0
    # New bucket writes normally.
    assert store.append([_row("2026-07-02T13:50:00+00:00", "a")]) == 1

    df = store.read_range(["2026-07-02"])
    assert len(df) == 3
    # Original row survived the duplicate with a different mid.
    kept = df.filter((df["ts"] == ts) & (df["condition_id"] == "a"))
    assert kept["mid"].item() == 0.5


def test_append_straddles_midnight(tmp_path):
    store = SnapshotStore(tmp_path)
    rows = [
        _row("2026-07-02T23:55:00+00:00", "a"),
        _row("2026-07-03T00:00:00+00:00", "a"),
    ]
    assert store.append(rows) == 2
    assert len(store.read_range(["2026-07-02"])) == 1
    assert len(store.read_range(["2026-07-03"])) == 1
    assert len(store.read_range(["2026-07-02", "2026-07-03"])) == 2


def test_latest_per_market(tmp_path):
    store = SnapshotStore(tmp_path)
    store.append([
        _row("2026-07-02T13:45:00+00:00", "a", mid=0.4),
        _row("2026-07-02T13:50:00+00:00", "a", mid=0.6),
    ])
    latest = store.latest_per_market(["2026-07-02"])
    assert latest["mid"].item() == 0.6


def test_depth_columns_roundtrip(tmp_path):
    import json

    store = SnapshotStore(tmp_path)
    row = _row("2026-07-02T13:45:00+00:00", "a")
    row["bids_json"] = json.dumps([[0.49, 100.0], [0.48, 250.0]])
    row["asks_json"] = json.dumps([[0.51, 80.0], [0.53, 300.0]])
    store.append([row])
    df = store.read_range(["2026-07-02"])
    bids = json.loads(df["bids_json"].item())
    assert bids == [[0.49, 100.0], [0.48, 250.0]]  # best-first, lossless


def test_read_range_skips_corrupt_partition(tmp_path):
    """A single truncated/0-byte partition (an interrupted write) must not
    abort the whole read -- it's skipped loudly so status/CLV/report keep
    working on the remaining days instead of the orchestrator crash-looping
    on one bad file."""
    store = SnapshotStore(tmp_path)
    store.append([_row("2026-07-02T13:45:00+00:00", "a", mid=0.4)])
    store.append([_row("2026-07-04T13:45:00+00:00", "b", mid=0.6)])
    # Corrupt the middle day the way an interrupted flush would: a 0-byte file.
    (tmp_path / "date=2026-07-03").mkdir(parents=True)
    (tmp_path / "date=2026-07-03" / "snapshots.parquet").write_bytes(b"")

    df = store.read_range(["2026-07-02", "2026-07-03", "2026-07-04"])
    assert set(df.get_column("condition_id").to_list()) == {"a", "b"}  # both good days survive
    # Same resilience on the projected path (which reads schema first).
    proj = store.read_range(["2026-07-02", "2026-07-03", "2026-07-04"],
                            columns=["ts", "condition_id", "mid"])
    assert set(proj.get_column("condition_id").to_list()) == {"a", "b"}


def test_append_is_atomic_no_tmp_left_behind(tmp_path):
    """append() writes via a temp file + atomic rename, so a completed append
    leaves exactly the partition file and no stray .tmp -- the mechanism that
    prevents the 0-byte/half-written partitions read_range now defends against."""
    store = SnapshotStore(tmp_path)
    store.append([_row("2026-07-02T13:45:00+00:00", "a")])
    store.append([_row("2026-07-02T13:50:00+00:00", "b")])  # read-modify-write path
    part_dir = tmp_path / "date=2026-07-02"
    files = sorted(p.name for p in part_dir.iterdir())
    assert files == ["snapshots-13.parquet"]  # one hour's file, no leftover .tmp
    assert len(store.read_range(["2026-07-02"])) == 2  # both rows intact


# --- one file per hour (2026-10-17): an append rewrites only its own hour -----

def test_append_writes_one_file_per_hour_and_dedups_across_them(tmp_path):
    store = SnapshotStore(tmp_path)
    assert store.append([_row("2026-07-02T13:45:00+00:00", "a"),
                         _row("2026-07-02T14:00:00+00:00", "a")]) == 2
    day = tmp_path / "date=2026-07-02"
    assert sorted(p.name for p in day.iterdir()) == ["snapshots-13.parquet",
                                                     "snapshots-14.parquet"]
    # A replay of either hour's key is a duplicate, whichever file holds it.
    assert store.append([_row("2026-07-02T13:45:00+00:00", "a", mid=0.9),
                         _row("2026-07-02T14:00:00+00:00", "a", mid=0.9)]) == 0
    assert store.read_range(["2026-07-02"])["mid"].to_list() == [0.5, 0.5]


def test_switch_over_day_reads_and_dedups_against_the_whole_day_file(tmp_path):
    """The morning of the switch-over lives in the legacy single file; the
    afternoon goes to hour files. Both are one partition to every reader, and
    a key already in the legacy file is still a duplicate."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    legacy = tmp_path / "date=2026-10-17" / "snapshots.parquet"
    legacy.parent.mkdir(parents=True)
    pl.DataFrame([{c: _row("2026-10-17T15:00:00+00:00", "a").get(c, "polymarket"
                                                                 if c == "venue" else None)
                   for c in SNAPSHOT_SCHEMA}], schema=SNAPSHOT_SCHEMA).write_parquet(legacy)

    assert store.append([_row("2026-10-17T15:00:00+00:00", "a", mid=0.9)]) == 0
    assert store.append([_row("2026-10-17T15:05:00+00:00", "a", mid=0.7)]) == 1
    assert [p.name for p in store.partition_files("2026-10-17")] == [
        "snapshots.parquet", "snapshots-15.parquet"]
    df = store.read_range(["2026-10-17"])
    assert df["mid"].to_list() == [0.5, 0.7]
    assert store.latest_per_market(["2026-10-17"])["mid"].item() == 0.7


def test_append_keeps_first_write_semantics_on_a_random_sequence(tmp_path):
    """The layout change must not change WHAT is stored: against a reference
    that keeps the first row written per (ts, condition_id), a random sequence
    of overlapping batches yields exactly the same rows."""
    import random

    rng = random.Random(7)
    store = SnapshotStore(tmp_path)
    reference: dict[tuple[str, str], float] = {}
    for _ in range(40):
        # Unique keys within a batch: a round never snapshots a market twice
        # in one bucket, and append has never deduplicated inside a batch.
        batch_by_key: dict[tuple[str, str], dict] = {}
        for _ in range(rng.randint(1, 6)):
            ts = f"2026-07-0{rng.choice([2, 3])}T{rng.choice([0, 1, 13, 23]):02d}:" \
                 f"{rng.choice([0, 5, 55]):02d}:00+00:00"
            row = _row(ts, rng.choice("abc"), mid=round(rng.random(), 3))
            batch_by_key.setdefault((ts, row["condition_id"]), row)
        batch = list(batch_by_key.values())
        expected_new = sum(1 for key in batch_by_key if key not in reference)
        written = store.append(batch)
        for r in batch:
            reference.setdefault((r["ts"], r["condition_id"]), r["mid"])
        assert written == expected_new
    df = store.read_range(["2026-07-02", "2026-07-03"])
    got = {(t, c): m for t, c, m in df.select("ts", "condition_id", "mid").iter_rows()}
    assert got == reference
    assert df.height == len(reference)


def test_append_reads_in_full_only_its_own_hours_file(tmp_path, monkeypatch):
    """The point of the change: no append materialises the rest of the day."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    for h in range(5):
        store.append([_row(f"2026-07-02T{h:02d}:00:00+00:00", "a")])
    read: list[str] = []
    real = pl.read_parquet

    def spy(path, *a, **k):
        read.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(pl, "read_parquet", spy)
    assert store.append([_row("2026-07-02T03:05:00+00:00", "b")]) == 1
    assert read == [str(tmp_path / "date=2026-07-02" / "snapshots-03.parquet")]


def test_read_range_column_projection(tmp_path):
    """Projected reads return exactly the requested columns with identical
    values, and never materialize the ones not asked for (the whole point:
    skipping the heavy bids_json/asks_json blobs)."""
    store = SnapshotStore(tmp_path)
    store.append([
        _row("2026-07-02T13:45:00+00:00", "a", mid=0.4),
        _row("2026-07-02T13:50:00+00:00", "b", mid=0.7),
    ])
    proj = store.read_range(["2026-07-02"], columns=["ts", "condition_id", "mid"])
    assert proj.columns == ["ts", "condition_id", "mid"]
    assert set(proj.get_column("condition_id").to_list()) == {"a", "b"}
    assert proj.filter(proj["condition_id"] == "a")["mid"].item() == 0.4
    # Same rows as an unprojected read -- projection changes width, not length.
    assert len(proj) == len(store.read_range(["2026-07-02"]))


def test_read_range_projection_missing_range_returns_projected_schema(tmp_path):
    """An empty range with a projection returns an empty frame carrying just
    the projected columns, not the full schema -- so callers can index the
    columns they asked for without a KeyError."""
    store = SnapshotStore(tmp_path)
    empty = store.read_range(["2099-01-01"], columns=["ts", "condition_id", "mid"])
    assert empty.is_empty()
    assert empty.columns == ["ts", "condition_id", "mid"]


def test_read_range_projection_tolerates_legacy_partition(tmp_path):
    """Projecting a column an older partition never had (columns are additive
    over time, e.g. `venue`) must not raise -- the absent column comes back
    null via the diagonal concat, exactly as a full read already handles it."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    # A legacy partition with no `venue` column at all.
    legacy_schema = {k: v for k, v in SNAPSHOT_SCHEMA.items() if k != "venue"}
    old = {k: (_row("2026-07-02T13:40:00+00:00", "a").get(k)) for k in legacy_schema}
    path = tmp_path / "date=2026-07-02" / "snapshots.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame([old], schema=legacy_schema).write_parquet(path)
    # A newer partition that does carry `venue`.
    store.append([_row("2026-07-03T13:45:00+00:00", "a")])

    df = store.read_range(["2026-07-02", "2026-07-03"], columns=["ts", "condition_id", "venue"])
    assert "venue" in df.columns
    assert len(df) == 2
    legacy_row = df.filter(df["ts"] == "2026-07-02T13:40:00+00:00")
    assert legacy_row["venue"].item() is None       # absent -> null, not a crash
    new_row = df.filter(df["ts"] == "2026-07-03T13:45:00+00:00")
    assert new_row["venue"].item() == "polymarket"  # default applied on append


def test_old_partitions_without_depth_still_merge(tmp_path):
    """Partitions written before the depth columns existed read as nulls."""
    import polars as pl

    store = SnapshotStore(tmp_path)
    # Simulate a pre-depth partition: same layout minus the new columns.
    old = _row("2026-07-02T13:40:00+00:00", "a")
    old.pop("bids_json", None), old.pop("asks_json", None)
    legacy_schema = {k: v for k, v in SNAPSHOT_SCHEMA.items()
                     if k not in ("bids_json", "asks_json")}
    path = tmp_path / "date=2026-07-02" / "snapshots.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame([old], schema=legacy_schema).write_parquet(path)

    # New-schema rows append into the same partition without breaking.
    assert store.append([_row("2026-07-02T13:45:00+00:00", "a")]) == 1
    df = store.read_range(["2026-07-02"]).sort("ts")
    assert len(df) == 2
    assert df["bids_json"][0] is None      # legacy row -> null depth
    assert df["mid"][0] == 0.5             # legacy data intact


# --- latest_per_market: projected + day-at-a-time (2026-08-06 OOM) ----------

def _multi_day_store(tmp_path) -> SnapshotStore:
    """Three days, several markets, written out of chronological order so a
    naive 'last row appended' would give a different answer than 'latest ts'."""
    store = SnapshotStore(str(tmp_path / "snapshots"))
    plan = [
        ("2026-03-01T00:00:00+00:00", "0xA", 0.10),
        ("2026-03-03T05:00:00+00:00", "0xA", 0.30),   # newest for A, written first
        ("2026-03-02T12:00:00+00:00", "0xA", 0.20),
        ("2026-03-01T09:00:00+00:00", "0xB", 0.60),
        ("2026-03-02T23:55:00+00:00", "0xB", 0.65),   # newest for B, mid-day boundary
        ("2026-03-03T00:05:00+00:00", "0xC", 0.90),   # C exists on one day only
    ]
    for ts, cid, mid in plan:
        row = _row(ts, cid, mid)
        row["bids_json"] = '[[0.49, 100]]'
        row["asks_json"] = '[[0.51, 100]]'
        row["venue"] = "polymarket"
        store.append([row])
    return store


def test_latest_per_market_matches_the_whole_window_reduction(tmp_path):
    """Reducing day-by-day must give the same rows as reducing one concatenated
    frame -- that equivalence is the whole licence for bounding the peak."""
    store = _multi_day_store(tmp_path)
    dates = ["2026-03-03", "2026-03-02", "2026-03-01"]

    got = store.latest_per_market(dates).sort("condition_id")
    from lab.store.snapshots import LATEST_PER_MARKET_COLUMNS
    whole = (store.read_range(dates, columns=LATEST_PER_MARKET_COLUMNS)
             .sort("ts").group_by("condition_id").last().sort("condition_id"))

    assert got.to_dicts() == whole.to_dicts()
    assert dict(zip(got["condition_id"], got["mid"])) == {"0xA": 0.30, "0xB": 0.65, "0xC": 0.90}


def test_latest_per_market_leaves_the_order_book_blobs_on_disk(tmp_path):
    """They are the bulk of a row's bytes and nothing in src/lab reads them
    back. Unprojected this call cost ~578MB and OOM-killed the orchestrator
    hourly (2026-08-06); projected, the same window costs ~24MB."""
    store = _multi_day_store(tmp_path)
    df = store.latest_per_market(["2026-03-03", "2026-03-02", "2026-03-01"])

    assert "bids_json" not in df.columns
    assert "asks_json" not in df.columns
    assert {"ts", "condition_id", "mid", "spread",
            "bid_depth_usd", "ask_depth_usd", "venue"} <= set(df.columns)
    # ...and a caller that explicitly wants them still can.
    with_books = store.latest_per_market(["2026-03-03"], columns=list(SNAPSHOT_SCHEMA))
    assert with_books["bids_json"][0] == '[[0.49, 100]]'


def test_latest_per_market_returns_a_typed_empty_frame_for_missing_dates(tmp_path):
    store = _multi_day_store(tmp_path)
    empty = store.latest_per_market(["2020-01-01", "2020-01-02"])
    assert empty.is_empty()
    assert "bids_json" not in empty.columns
    assert "condition_id" in empty.columns   # typed, so callers can still select


def test_read_range_condition_id_filter_matches_reading_everything(tmp_path):
    """The filter is a memory optimisation, so it has to be provably a no-op on
    the result: same rows, same order, as reading the window and filtering after."""
    import polars as pl

    store = _multi_day_store(tmp_path)
    dates = ["2026-03-01", "2026-03-02", "2026-03-03"]

    filtered = store.read_range(dates, columns=["ts", "condition_id", "mid"],
                                condition_ids=["0xA", "0xC"])
    whole = store.read_range(dates, columns=["ts", "condition_id", "mid"])
    expected = whole.filter(pl.col("condition_id").is_in(["0xA", "0xC"]))

    assert filtered.to_dicts() == expected.to_dicts()
    assert set(filtered["condition_id"]) == {"0xA", "0xC"}
    assert filtered.height < whole.height, "fixture has nothing to filter out"


def test_read_range_filter_works_without_a_projection(tmp_path):
    store = _multi_day_store(tmp_path)
    df = store.read_range(["2026-03-03"], condition_ids=["0xC"])
    assert set(df["condition_id"]) == {"0xC"}
    assert "bids_json" in df.columns, "no projection asked for -- full schema expected"


def test_read_range_filter_on_an_absent_market_returns_a_typed_empty_frame(tmp_path):
    store = _multi_day_store(tmp_path)
    df = store.read_range(["2026-03-01", "2026-03-02"],
                          columns=["ts", "condition_id", "mid"], condition_ids=["0xZZZ"])
    assert df.is_empty()
    assert df.columns == ["ts", "condition_id", "mid"]


def test_clv_validity_check_reads_only_the_null_control_markets(tmp_path, monkeypatch):
    """The read that OOM-killed the orchestrator hourly (2026-08-02..06): it
    pulled every partition in the archive to score a handful of sports markets,
    then built a Python index over all of it."""
    from lab.eval import clv

    seen: list = []

    class _Spy:
        def read_range(self, dates, columns=None, condition_ids=None):
            seen.append((columns, condition_ids))
            import polars as pl
            from lab.store.snapshots import SNAPSHOT_SCHEMA
            cols = columns or list(SNAPSHOT_SCHEMA)
            return pl.DataFrame(schema={c: SNAPSHOT_SCHEMA[c] for c in cols})

    from lab.store import db
    conn = db.connect(tmp_path / "lab.db")
    try:
        conn.execute("INSERT INTO markets (condition_id, question, category, venue, tier,"
                     " active, closed) VALUES ('0xs1','q','sports','polymarket','liquid',1,0)")
        conn.execute("INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts)"
                     " VALUES ('2026-03-01T00:00:00+00:00','0xs1','m0_market',0.4,0.5)")
        conn.execute("INSERT INTO resolutions (condition_id, resolved_ts, payout_yes, disputed)"
                     " VALUES ('0xs1','2026-03-05T00:00:00+00:00',1.0,0)")
        conn.commit()
        from lab.util import load_config
        cfg = load_config()
        clv.clv_validity_check(conn, cfg, _Spy())
    finally:
        conn.close()

    assert seen, "clv_validity_check never reached the snapshot read -- fixture is wrong"
    for columns, condition_ids in seen:
        assert condition_ids is not None, "clv_validity_check is reading every market again"
        assert set(condition_ids) == {"0xs1"}
        assert columns == ["ts", "condition_id", "mid"]
