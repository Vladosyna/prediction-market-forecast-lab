"""M3 end-to-end on fixtures: evidence rows land, cost cap skips cleanly."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from lab.forecast import run_forecasts
from lab.models.base import MarketState
from lab.models.m3_evidence import (
    M3_BOUNDARY_BAND_HALF_WIDTH,
    M3Evidence,
    m3_boundary_randomized_ids,
    m3_target_ids,
)
from lab.news.extract import BudgetExceeded
from lab.news.providers import Article
from lab.store import db
from lab.store.snapshots import SnapshotStore, floor_ts_bucket
from lab.util import load_config, now_utc


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
    return cfg


class FakeProvider:
    def fetch(self, query, max_items=20):
        # A day old, not a calendar date: evidence decays as exp(-age/tau), tau 5
        # days, so a fixed 2026-07-01 shifted the price ~0.4 log-odds when this
        # was written (07-02), ~1e-9 by October and not at all by 2026-12-28 --
        # "for_yes evidence shifts up" below had been passing on rounding.
        published_ts = (now_utc() - timedelta(days=1)).isoformat(timespec="seconds")
        return [Article(title="Positive development for X", url="http://n/1",
                        source="fake", published_ts=published_ts,
                        summary="X moved closer to happening.")]


class FakeLlm:
    """Deterministic LLM stub with a controllable budget."""

    model = "fake-model"

    def __init__(self, budget_calls: int = 100) -> None:
        self.calls = 0
        self.budget_calls = budget_calls

    def complete(self, system, prompt, purpose, max_tokens=2000):
        if self.calls >= self.budget_calls:
            raise BudgetExceeded("cap")
        self.calls += 1
        payload = json.dumps([
            {"claim": "X moved closer", "direction": "for_yes", "strength": 2,
             "source_reliability": 2, "relevance": 0.8, "article_index": 0},
        ])
        return payload, {"tokens_in": 500, "tokens_out": 100, "cost_usd": 0.003}


def _seed_markets(conn, store, n=3):
    ts_bucket = floor_ts_bucket(now_utc(), 5)
    # Relative, not a calendar date: a fixed 2026-12-31T00:00 end date is past
    # from the first second of that day, eligible_market_states skips markets
    # past their end date, and the forecast pass these tests drive wrote nothing.
    end_date = (now_utc() + timedelta(days=400)).isoformat(timespec="seconds")
    for i in range(n):
        cid = f"0x{i}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'politics', 'Resolves YES if X.', ?,
                       ?, 'liquid', 1, 0, ?, 5000000)""",
            (cid, f"m-{i}", f"Will X{i} happen?", end_date, f"tok{i}", 1000000 - i),
        )
        store.append([{
            "ts": ts_bucket, "condition_id": cid, "token_id_yes": f"tok{i}",
            "best_bid": 0.58, "best_ask": 0.62, "mid": 0.60, "spread": 0.04,
            "bid_depth_usd": 1000.0, "ask_depth_usd": 1000.0, "last_trade_price": None,
        }])
    conn.commit()


def test_m3_end_to_end_writes_evidence_and_forecasts(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store)

    targets = m3_target_ids(conn, config)
    assert targets == ["0x0", "0x1", "0x2"]  # ordered by liquidity desc

    m3 = M3Evidence(conn, FakeLlm(), [FakeProvider()], config, targets)
    counts = run_forecasts(conn, store, [m3], config)
    assert counts["written"] == 3

    ev = conn.execute("SELECT * FROM evidence_runs").fetchall()
    assert len(ev) == 3
    dossier = json.loads(ev[0]["dossier_json"])
    # Auditability: dossier carries the full trail start-to-finish.
    for key in ("question", "resolution_criteria", "p_market", "articles",
                "evidence_items", "aggregation"):
        assert key in dossier

    fc = conn.execute("SELECT * FROM forecasts WHERE model_id='m3_evidence'").fetchall()
    assert len(fc) == 3
    assert all(f["evidence_run_id"] is not None for f in fc)
    assert all(f["p_yes"] > 0.60 for f in fc)  # for_yes evidence shifts up
    conn.close()


def test_evidence_is_bounded_by_the_ledger_timestamp_not_retrieval_time(config):
    """Guardrail 11 against the timestamp the ROW carries. The pass freezes one
    ts for every row; retrieval runs minutes later (p50 7, max 29 measured),
    and bounding evidence at that later moment let items published after the
    row's own timestamp into its forecast -- 5 of 44,616 before 2026-09-29."""
    class TwoArticles:
        def fetch(self, query, max_items=20):
            return [Article(title="Before the freeze", url="http://n/before", source="fake",
                            published_ts="2026-09-26T01:59:00+00:00", summary="."),
                    Article(title="After the freeze", url="http://n/after", source="fake",
                            published_ts="2026-09-26T02:10:00+00:00", summary=".")]

    seen: list[str] = []

    class RecordingLlm(FakeLlm):
        def complete(self, system, prompt, purpose, max_tokens=2000):
            seen.append(prompt)
            return super().complete(system, prompt, purpose, max_tokens)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store, n=1)
    m3 = M3Evidence(conn, RecordingLlm(), [TwoArticles()], config, ["0x0"])
    state = MarketState(condition_id="0x0", question="Will X0 happen?", category="politics",
                        description="d", end_date_iso="2026-12-31T00:00:00+00:00",
                        tier="liquid", p_market=0.6, spread=0.04,
                        snapshot_ts="2026-09-26T02:00:00+00:00", days_to_resolution=96.0)
    result = m3.forecast(state, {"ts": "2026-09-26T02:00:00+00:00"})
    assert result is not None
    assert "After the freeze" not in seen[0], "the LLM must never see post-freeze news"
    dossier = json.loads(conn.execute("SELECT dossier_json FROM evidence_runs").fetchone()[0])
    assert dossier["forecast_ts"] == "2026-09-26T02:00:00+00:00"
    assert [a["url"] for a in dossier["articles"]] == ["http://n/before"]
    assert dossier["articles_after_forecast_ts"] == 1
    conn.close()


def test_m3_evidence_tags_randomized_forecasts(config):
    """M3Evidence.forecast() sets m3_randomized/m3_random_seed on the
    ForecastResult based on membership in randomized_ids, and run_forecasts()
    persists both through append_forecast() into the ledger."""
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store, n=3)

    m3 = M3Evidence(conn, FakeLlm(), [FakeProvider()], config, ["0x0", "0x1", "0x2"],
                    randomized_ids={"0x1"}, random_seed="test-seed-7")
    run_forecasts(conn, store, [m3], config)

    rows = {r["condition_id"]: r for r in conn.execute(
        "SELECT condition_id, m3_randomized, m3_random_seed FROM forecasts WHERE model_id='m3_evidence'"
    )}
    assert rows["0x1"]["m3_randomized"] == 1
    assert rows["0x1"]["m3_random_seed"] == "test-seed-7"
    assert rows["0x0"]["m3_randomized"] == 0
    assert rows["0x0"]["m3_random_seed"] is None
    conn.close()


def _band_seed_count(config) -> int:
    """Enough candidates for the configured K plus the whole boundary band.

    Deliberately derived, not a literal: these tests exercise the band logic,
    which is independent of how much coverage an operator has bought. Hard-coded
    at 40 they broke the moment `m3_top_k` moved 20 -> 120 (2026-08-06), which
    told us nothing about the band.
    """
    return int(config["forecast"]["m3_top_k"]) + M3_BOUNDARY_BAND_HALF_WIDTH + 20


def _seed_many_markets(conn, n):
    for i in range(n):
        cid = f"0x{i:04d}"
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, tier, active, closed, liquidity_num, volume_num)
               VALUES (?, ?, ?, 'politics', 'Resolves YES if X.', '2026-12-31T00:00:00+00:00',
                       'liquid', 1, 0, ?, 5000000)""",
            (cid, f"m-{i}", f"Will X{i} happen?", 1_000_000 - i),
        )
    conn.commit()


def test_boundary_randomization_covers_exactly_k_when_enough_candidates(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_many_markets(conn, _band_seed_count(config))
    k = config["forecast"]["m3_top_k"]

    final_ids, chosen, seed = m3_boundary_randomized_ids(conn, config)
    assert len(final_ids) == k
    assert chosen <= set(final_ids)
    conn.close()


def test_boundary_randomization_guaranteed_and_excluded_segments_are_fixed(config):
    """Markets ranked strictly above the K-10..K+10 band always make it in;
    markets ranked strictly below the band never do -- only the band itself
    (the coin-flip region) is randomized."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_many_markets(conn, _band_seed_count(config))
    k = config["forecast"]["m3_top_k"]
    half = M3_BOUNDARY_BAND_HALF_WIDTH

    full_order = m3_target_ids(conn, {**config, "forecast": {**config["forecast"], "m3_top_k": 999}})
    guaranteed = set(full_order[:max(0, k - half)])
    below_band = set(full_order[k + half:])

    final_ids, chosen, seed = m3_boundary_randomized_ids(conn, config)
    final_set = set(final_ids)
    assert guaranteed <= final_set
    assert not (below_band & final_set)
    conn.close()


def test_boundary_randomization_reproducible_from_same_seed(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_many_markets(conn, _band_seed_count(config))

    final_a, chosen_a, seed_a = m3_boundary_randomized_ids(conn, config)
    final_b, chosen_b, seed_b = m3_boundary_randomized_ids(conn, config)

    assert final_a == final_b
    assert chosen_a == chosen_b
    assert seed_a == seed_b
    conn.close()


def test_boundary_randomization_different_seed_picks_different_band_members(config):
    conn = db.connect(config["storage"]["db_path"])
    _seed_many_markets(conn, _band_seed_count(config))

    cfg_a = {**config, "forecast": {**config["forecast"], "m3_boundary_random_seed": "seed-a"}}
    cfg_b = {**config, "forecast": {**config["forecast"], "m3_boundary_random_seed": "seed-b"}}
    _, chosen_a, _ = m3_boundary_randomized_ids(conn, cfg_a)
    _, chosen_b, _ = m3_boundary_randomized_ids(conn, cfg_b)

    assert chosen_a != chosen_b
    conn.close()


def test_cost_cap_breach_skips_remaining(config, caplog):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_markets(conn, store)

    llm = FakeLlm(budget_calls=1)  # cap hits after the first market
    m3 = M3Evidence(conn, llm, [FakeProvider()], config, m3_target_ids(conn, config))
    counts = run_forecasts(conn, store, [m3], config)

    assert counts["written"] == 1
    assert llm.calls == 1  # no further calls after the breach
    assert any("cost cap hit" in r.message for r in caplog.records)
    conn.close()


def test_m3b_is_wired_on_the_same_targets_and_after_m3(config):
    """The experiment is the PAIRING: same LLM, same markets, same dossier, and
    the only difference is whether the final number comes from the
    deterministic aggregator or straight from the model (brief section 6). It
    had been implemented and never wired in, so it produced zero forecasts
    until 2026-08-11.
    """
    import inspect

    from lab import forecast as fc

    src = inspect.getsource(fc.build_default_models)
    assert "M3bDirect(conn, llm, config, target_ids)" in src, (
        "M3b must share M3's target list, or the arms are not comparable"
    )
    # ordering: M3 writes the dossier that M3b reads, so it must come first
    assert src.index("M3Evidence(") < src.index("M3bDirect("), (
        "M3b must be appended after M3 so it reads today's dossier, not yesterday's"
    )


def test_m3b_is_never_an_ensemble_member():
    """It is a comparison arm. Pooling it into M4 would let the thing being
    measured contribute to the thing measuring it."""
    from lab.models.m4_ensemble import POOLABLE

    assert not any("m3b" in m for m in POOLABLE)
