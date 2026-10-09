"""M7 cross-venue signal (Phase 9): mapping propose-then-confirm flow, the
deterministic log-odds pool, and the ledger writer -- fixture-driven per the
brief's Phase 9 acceptance criterion ("fixtures acceptable")."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import timedelta

import pytest

from lab.learn.refit import save_artifact, sigmoid
from lab.models.base import ForecastResult
from lab.models.m7_crossvenue import (
    commit_and_push_markets_map,
    confirm_match,
    confirmed_by_condition,
    kalshi_propose_candidates,
    link_confirmed_event,
    load_markets_map,
    load_pmxt_candidates,
    pool_log_odds,
    propose_matches,
    reject_match,
    save_markets_map,
    scan_confirmed_pairs,
    verify_pmxt_candidates,
    write_m7_forecasts,
)
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


def test_pool_log_odds_averages_in_logit_space():
    # Two venues agreeing exactly: pool equals that shared value.
    assert pool_log_odds([0.7, 0.7]) == pytest.approx(0.7)
    # One confident YES, one confident NO -> pools back toward 0.5.
    assert pool_log_odds([0.9, 0.1]) == pytest.approx(0.5, abs=1e-9)
    with pytest.raises(ValueError):
        pool_log_odds([])


def test_pool_log_odds_a_eff_default_is_identity():
    from lab.learn.refit import logit, sigmoid

    plain = pool_log_odds([0.6, 0.8])
    extremized = pool_log_odds([0.6, 0.8], a_eff=2.0)
    assert plain == pytest.approx(pool_log_odds([0.6, 0.8], a_eff=1.0))
    expected = float(sigmoid(2.0 * (logit(0.6) + logit(0.8)) / 2))
    assert extremized == pytest.approx(expected)
    assert extremized != pytest.approx(plain)


def test_markets_map_roundtrip(tmp_path):
    path = tmp_path / "markets_map.yaml"
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"}],
            "proposed": []}
    save_markets_map(data, path)
    loaded = load_markets_map(path)
    assert loaded["confirmed"] == data["confirmed"]
    assert loaded["proposed"] == []


def test_load_markets_map_missing_file_returns_empty(tmp_path):
    loaded = load_markets_map(tmp_path / "does_not_exist.yaml")
    assert loaded == {"confirmed": [], "proposed": []}


def test_confirm_match_moves_proposed_to_confirmed():
    data = {
        "confirmed": [],
        "proposed": [{"condition_id": "0x1", "question": "q", "venue": "kalshi",
                     "external_id": "T1", "external_question": "eq",
                     "rationale": "same event", "confidence": 0.9,
                     "proposed_ts": "2026-07-01T00:00:00+00:00"}],
    }
    assert confirm_match(data, "0x1", "kalshi") is True
    assert data["proposed"] == []
    assert len(data["confirmed"]) == 1
    entry = data["confirmed"][0]
    assert entry["condition_id"] == "0x1" and entry["external_id"] == "T1"
    assert "confirmed_ts" in entry
    # LLM-only fields don't belong on a confirmed (human-owned) entry.
    assert "rationale" not in entry and "confidence" not in entry


def test_confirm_match_idempotent():
    data = {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1",
                          "confirmed_ts": "t"}], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is True
    assert len(data["confirmed"]) == 1  # no duplicate


def test_confirm_match_hand_curated_without_prior_proposal():
    """A human can confirm a Metaculus pair directly -- `propose` can't reach
    Metaculus (api/metaculus.py), so this is the only path for that venue."""
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "metaculus", external_id="12345") is True
    assert data["confirmed"][0]["external_id"] == "12345"


def test_confirm_match_returns_false_with_nothing_to_confirm():
    data = {"confirmed": [], "proposed": []}
    assert confirm_match(data, "0x1", "kalshi") is False
    assert data["confirmed"] == []


def test_reject_match_removes_a_proposed_entry():
    """The human's other verdict: a real observed case is the LLM proposing a
    pair whose own rationale says the events don't match (different office,
    different year) yet still returning confidence=1.0 -- reject removes it
    from `proposed` without ever touching `confirmed`."""
    data = {
        "confirmed": [],
        "proposed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "T1",
                     "rationale": "different offices, do not match", "confidence": 1.0}],
    }
    assert reject_match(data, "0x1", "kalshi", "T1") is True
    assert data["proposed"] == []
    assert data["confirmed"] == []


def test_reject_match_returns_false_when_nothing_to_reject():
    data = {"confirmed": [], "proposed": []}
    assert reject_match(data, "0x1", "kalshi", "T1") is False


def test_reject_match_only_removes_the_matching_external_id():
    """Two candidates proposed for the same condition_id/venue (e.g. the
    Tom Steyer / two different CA governor tickers case) -- rejecting one
    must not remove the other."""
    data = {
        "confirmed": [],
        "proposed": [
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"},
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T2"},
        ],
    }
    assert reject_match(data, "0x1", "kalshi", "T1") is True
    assert len(data["proposed"]) == 1
    assert data["proposed"][0]["external_id"] == "T2"


def test_link_confirmed_event_creates_event_linking_both_markets(config):
    """Phase 10 acceptance: a confirmed match creates an event linking >=2
    venue-markets, using the Polymarket market's own question as the title."""
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "0x1", "venue": "polymarket", "venue_native_id": "0x1",
        "slug": "s", "question": "Will X happen?", "category": "politics",
        "description": "d", "end_date_iso": "2026-12-31T00:00:00+00:00",
        "token_id_yes": "1", "token_id_no": "2", "neg_risk": 0,
        "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })
    conn.commit()

    event_id = link_confirmed_event(conn, "0x1", "kalshi", "T1")

    rows = {r["condition_id"]: r["event_id"]
           for r in conn.execute("SELECT condition_id, event_id FROM markets")}
    assert rows["0x1"] == event_id
    assert rows["kalshi:T1"] == event_id
    ev = conn.execute("SELECT title FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert ev["title"] == "Will X happen?"
    conn.close()


def test_confirmed_by_condition_excludes_proposed():
    """The core of 'a proposed-but-unconfirmed pair is NOT forecast': the
    scan path only ever sees confirmed_by_condition()'s output."""
    data = {
        "confirmed": [{"condition_id": "0xA", "venue": "kalshi", "external_id": "T1"}],
        "proposed": [{"condition_id": "0xB", "venue": "kalshi", "external_id": "T2"}],
    }
    by_cid = confirmed_by_condition(data)
    assert set(by_cid) == {"0xA"}


def _seed_market_with_snapshot(conn, store, cid: str, mid: float = 0.5):
    # Relative, not a calendar date: a fixed 2026-12-31 fell inside 90 days on
    # 2026-10-02 and moved the m1_hier test below out of its gt90d bucket.
    end_date = (now_utc() + timedelta(days=400)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES (?, ?, ?, 'politics', 'd', ?, ?, 'liquid', 1, 0, 200000, 2000000)""",
        (cid, cid, f"Question for {cid}?", end_date, f"tok-{cid}"),
    )
    store.append([{
        "ts": floor_ts_bucket(now_utc(), 5), "condition_id": cid, "token_id_yes": f"tok-{cid}",
        "best_bid": mid - 0.02, "best_ask": mid + 0.02, "mid": mid, "spread": 0.04,
        "bid_depth_usd": 1000.0, "ask_depth_usd": 1000.0, "last_trade_price": None,
    }])


def test_write_m7_forecasts_writes_five_confirmed_pairs_and_skips_unconfirmed(config):
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    cids = [f"0x{i}" for i in range(5)]
    for cid in cids:
        _seed_market_with_snapshot(conn, store, cid)
    # A 6th market stands in for a proposed-but-unconfirmed pair: it has its
    # own snapshot but no entry in `results`, so it must never be forecast.
    _seed_market_with_snapshot(conn, store, "0xUNCONFIRMED")
    conn.commit()

    results = {
        cid: ForecastResult(
            p_yes=0.6 + 0.01 * i,
            meta={"quotes": [{"venue": "kalshi", "external_id": f"T{i}", "price": 0.6 + 0.01 * i,
                              "fetched_ts": "2026-07-03T00:00:00+00:00"}], "n_pooled": 1},
        )
        for i, cid in enumerate(cids)
    }

    written = write_m7_forecasts(conn, store, results, config)
    assert written == 5

    rows = conn.execute("SELECT * FROM forecasts WHERE model_id='m7_crossvenue'").fetchall()
    assert len(rows) == 5
    assert {r["condition_id"] for r in rows} == set(cids)
    assert all(r["p_market_at_ts"] == pytest.approx(0.5) for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM forecasts WHERE condition_id='0xUNCONFIRMED'"
    ).fetchone()["n"] == 0
    conn.close()


class FakeMetaculusClient:
    def __init__(self, bucket, raw_cp: float = 0.5) -> None:
        self.raw_cp = raw_cp

    async def question(self, question_id):
        return type("Q", (), {"community_prediction": self.raw_cp})()

    async def aclose(self) -> None:
        pass


class FakeKalshiClientNoMatch:
    def __init__(self, bucket) -> None:
        pass

    async def market(self, ticker):
        return None

    async def aclose(self) -> None:
        pass


def test_scan_confirmed_pairs_recalibrates_metaculus_cp_via_m1_hier(config, tmp_path, monkeypatch):
    """Phase 12: a confirmed Metaculus pair's community prediction is
    recalibrated through the m1_hier_curves metaculus offset before pooling,
    when an active artifact with a matching bucket fit is present."""
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)
    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientNoMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")  # end_date_iso far out -> gt90d bucket
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "metaculus", "external_id": "123"}],
         "proposed": []},
        map_path,
    )
    save_artifact(config, "m1_hier_curves", {
        "kind": "m1_hier_curves",
        "buckets": {
            "gt90d": {
                "global": {"alpha": 0.0, "beta": 1.0, "n": 500},
                "venues": {"metaculus": {"alpha_offset": 1.0, "beta_offset": 0.0, "n": 40}},
            },
        },
    })

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    quote = results["0x1"].meta["quotes"][0]
    assert quote["recalibrated"] is True
    assert quote["price"] == pytest.approx(float(sigmoid(1.0)))  # alpha_offset=1.0, raw cp=0.5 -> logit=0
    conn.close()


def test_scan_confirmed_pairs_skips_market_when_metaculus_cp_is_none(config, tmp_path, monkeypatch):
    """Clean abstain path (CLAUDE.md guardrail: record NULL, never impute):
    when Metaculus's community prediction is None (e.g. this account's
    data-access tier gates it, per api/metaculus.py's module docstring) and
    no other venue is paired, no quote is pooled and no forecast row is
    produced for that market -- not a zero-price quote, not a crash."""
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient",
                         lambda bucket: FakeMetaculusClient(bucket, raw_cp=None))
    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientNoMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "metaculus", "external_id": "123"}],
         "proposed": []},
        map_path,
    )

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" not in results
    conn.close()


def test_scan_confirmed_pairs_leaves_cp_unchanged_without_artifact(config, tmp_path, monkeypatch):
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)
    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientNoMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "metaculus", "external_id": "123"}],
         "proposed": []},
        map_path,
    )
    # No m1_hier_curves artifact saved -> ACTIVE.json has no entry for it.

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    quote = results["0x1"].meta["quotes"][0]
    assert quote["recalibrated"] is False
    assert quote["price"] == pytest.approx(0.5)  # raw CP, unchanged
    conn.close()


def test_scan_confirmed_pairs_applies_m7_extremization_artifact(config, tmp_path, monkeypatch):
    """Phase 13: an active m7_extremization artifact extremizes the pooled
    quote using the ACTUAL number of venues pooled for this market."""
    monkeypatch.setattr("lab.api.metaculus.MetaculusClient", FakeMetaculusClient)

    class FakeKalshiClientWithMatch:
        def __init__(self, bucket) -> None:
            pass

        async def market(self, ticker):
            return type("M", (), {"yes_price": 0.6})()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", FakeKalshiClientWithMatch)

    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    _seed_market_with_snapshot(conn, store, "0x1")
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [
            {"condition_id": "0x1", "venue": "metaculus", "external_id": "123"},
            {"condition_id": "0x1", "venue": "kalshi", "external_id": "T1"},
        ], "proposed": []},
        map_path,
    )
    save_artifact(config, "m7_extremization", {
        "kind": "m7_extremization",
        "categories": {"_all": {"a": 2.0, "rho_bar": 0.0}},
    })

    results = asyncio.run(scan_confirmed_pairs(conn, store, config, markets_map_path=map_path))

    assert "0x1" in results
    plain_pooled = pool_log_odds([0.5, 0.6])  # raw metaculus CP + kalshi price, no extremization
    extremized_pooled = pool_log_odds([0.5, 0.6], a_eff=2.0)
    assert results["0x1"].meta["extremization_a_eff"] == pytest.approx(2.0)  # rho_bar=0, n=2 -> full a
    assert results["0x1"].p_yes == pytest.approx(extremized_pooled)
    assert results["0x1"].p_yes != pytest.approx(plain_pooled)
    conn.close()


class FakeKalshiCandidate:
    def __init__(self, ticker, title):
        self.ticker = ticker
        self.title = title


class FakeLlm:
    def __init__(self, response: dict):
        self.response = response
        self.calls = 0

    def complete(self, system, prompt, purpose, max_tokens=2000):
        self.calls += 1
        return json.dumps(self.response), {"tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001}


class FakeKalshiCategoryClient:
    """Real Kalshi category -> series -> markets fan-out, faked. Also serves
    Sports/Entertainment/World series to prove they're correctly excluded --
    a market cap/priority-category filter bug would silently include them."""

    def __init__(self, series_by_cat: dict, markets_by_series: dict):
        self.series_by_cat = series_by_cat
        self.markets_by_series = markets_by_series
        self.categories_queried: list[str] = []

    async def series_by_category(self, category):
        self.categories_queried.append(category)
        return self.series_by_cat.get(category, [])

    async def markets_for_series(self, series_ticker, status="open", **kwargs):
        return self.markets_by_series.get(series_ticker, [])


def test_kalshi_propose_candidates_only_queries_priority_category_series(config):
    """The real live bug this fixes: a bare open_markets(limit=200) pulled
    whatever Kalshi considers globally 'open' -- verified live to be
    dominated by garbled multi-leg sports-combo products, crowding out the
    handful of real Economics/Politics/Weather markets entirely (0 candidates
    ever proposed). This must query ONLY the Kalshi category names whose
    categories.yaml mapping lands in our priority_categories."""
    kalshi = FakeKalshiCategoryClient(
        series_by_cat={
            "Economics": [{"ticker": "ECON-SERIES"}],
            "Politics": [{"ticker": "POL-SERIES"}],
            "Climate and Weather": [{"ticker": "WX-SERIES"}],
            "Elections": [{"ticker": "ELEC-SERIES"}],
            "Sports": [{"ticker": "SPORTS-SERIES"}],
            "Entertainment": [{"ticker": "ENT-SERIES"}],
            "World": [{"ticker": "WORLD-SERIES"}],
        },
        markets_by_series={
            "ECON-SERIES": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March")],
            "POL-SERIES": [FakeKalshiCandidate("PRES28", "2028 presidential race")],
            "WX-SERIES": [FakeKalshiCandidate("TEMPNYC", "NYC high temp Friday")],
            "ELEC-SERIES": [FakeKalshiCandidate("SENATE28", "2028 Senate control")],
            "SPORTS-SERIES": [FakeKalshiCandidate("KXMVE1", "yes Argentina advances,yes 7+ corners")],
            "ENT-SERIES": [FakeKalshiCandidate("OSCAR", "Best Picture winner")],
            "WORLD-SERIES": [FakeKalshiCandidate("UN1", "UN Security Council vote")],
        },
    )

    candidates = asyncio.run(kalshi_propose_candidates(kalshi, config))

    # config.yaml's priority_categories covers P1-P4 (economics, weather,
    # politics, geopolitics, entertainment) -- Sports is the one Kalshi
    # category with no priority-category mapping (sports is the null
    # control, never a forecast target), so it's the one that must be
    # excluded here. Result is keyed by OUR internal category, not flat.
    all_tickers = {c.ticker for cat_list in candidates.values() for c in cat_list}
    assert all_tickers == {"FEDMAR", "PRES28", "TEMPNYC", "SENATE28", "UN1", "OSCAR"}
    assert "KXMVE1" not in all_tickers  # the real garbled sports-combo ticker this fix excludes
    assert "Sports" not in kalshi.categories_queried
    # Politics AND Elections both map to "politics" -- both contribute here.
    assert {c.ticker for c in candidates["politics"]} == {"PRES28", "SENATE28"}
    assert {c.ticker for c in candidates["economics"]} == {"FEDMAR"}
    assert {c.ticker for c in candidates["weather"]} == {"TEMPNYC"}


class RecordingFakeLlm:
    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, system, prompt, purpose, max_tokens=2000):
        self.prompts.append(prompt)
        return json.dumps({"matches": []}), {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0001}


def test_propose_matches_gives_every_priority_category_a_fair_share(config, tmp_path):
    """Real live bug this fixes: a single dominant negRisk event (many
    high-volume legs in one category, e.g. "will [name] win 2028") must not
    crowd out every propose slot via one global ORDER BY volume_num DESC --
    each priority category gets its own even share of top_k regardless."""
    conn = db.connect(config["storage"]["db_path"])
    for i in range(10):  # 10 high-volume politics markets
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'politics', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?)""",
            (f"0xpol{i}", f"pol{i}", f"Will person {i} win?", 50_000_000 - i),
        )
    for i in range(2):  # only 2, much lower-volume weather markets
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num)
               VALUES (?, ?, ?, 'weather', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?)""",
            (f"0xwx{i}", f"wx{i}", f"Will it rain in city {i}?", 1000 - i),
        )
    conn.commit()

    config["universe"]["priority_categories"] = ["politics", "weather"]
    config["cross_venue"]["propose_top_k"] = 4  # -> 2 slots per category

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    candidates = {"politics": [FakeKalshiCandidate("X", "irrelevant")],
                 "weather": [FakeKalshiCandidate("Y", "irrelevant")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 4  # 2 politics + 2 weather, not 4 politics
    assert any("rain" in p for p in llm.prompts)  # weather got a slot at all
    assert sum("person" in p for p in llm.prompts) == 2  # politics didn't take all 4
    conn.close()


def test_propose_matches_dedupes_by_event_within_a_category(config, tmp_path):
    """Real live bug this fixes: one negRisk event's legs (e.g. 5 mutually
    exclusive "Fed hikes/cuts/holds" buckets, all high-volume, all one
    event_id) must not fill a whole category's per-category share by
    themselves -- distinct topics should get a chance too."""
    conn = db.connect(config["storage"]["db_path"])
    for i in range(5):  # one negRisk event, 5 legs, all high volume
        conn.execute(
            """INSERT INTO markets (condition_id, slug, question, category, description,
                                    end_date_iso, token_id_yes, tier, active, closed,
                                    liquidity_num, volume_num, event_id)
               VALUES (?, ?, ?, 'economics', 'd', '2026-12-31T00:00:00+00:00', 'tok',
                       'liquid', 1, 0, 200000, ?, 'evt_fed_july')""",
            (f"0xfed{i}", f"fed{i}", f"Will the Fed do thing {i} in July?", 10_000_000 - i),
        )
    # A distinct, lower-volume economics event that should still get a slot.
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num, event_id)
           VALUES ('0xcpi', 'cpi', 'Will CPI print above 3%?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 500, 'evt_cpi')"""
    )
    conn.commit()

    config["universe"]["priority_categories"] = ["economics"]
    config["cross_venue"]["propose_top_k"] = 2  # -> 2 slots, all in economics

    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    candidates = {"economics": [FakeKalshiCandidate("X", "irrelevant")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 2
    assert any("CPI" in p for p in llm.prompts)  # the distinct event got a slot
    assert sum("Fed do thing" in p for p in llm.prompts) == 1  # only ONE Fed leg, not both slots
    conn.close()


def test_kalshi_propose_candidates_gives_every_category_a_series_share(config):
    """Real live bug found via the user's own question: a single global
    series_seen counter across ALL Kalshi categories meant one category with
    many series (Economics has 601 on real Kalshi) silently consumed the
    entire budget, so every prior live run in this session only ever fetched
    Economics series -- Weather/Politics/Elections/World/Entertainment got
    zero, despite the category filter itself working. Each Kalshi category
    gets its own guaranteed series share (propose_series_per_category),
    decoupled from the hourly universe-sync's max_series_per_sync."""
    kalshi = FakeKalshiCategoryClient(
        series_by_cat={
            # Economics alone has more series than the whole per-category cap.
            "Economics": [{"ticker": f"ECON-{i}"} for i in range(20)],
            "Politics": [{"ticker": "POL-SERIES"}],
            "Climate and Weather": [{"ticker": "WX-SERIES"}],
        },
        markets_by_series={
            **{f"ECON-{i}": [FakeKalshiCandidate(f"E{i}", f"econ market {i}")] for i in range(20)},
            "POL-SERIES": [FakeKalshiCandidate("PRES28", "2028 presidential race")],
            "WX-SERIES": [FakeKalshiCandidate("TEMPNYC", "NYC high temp Friday")],
        },
    )
    config["universe"]["priority_categories"] = ["economics", "politics", "weather"]
    config["cross_venue"]["propose_series_per_category"] = 2

    candidates = asyncio.run(kalshi_propose_candidates(kalshi, config))

    all_tickers = {c.ticker for cat_list in candidates.values() for c in cat_list}
    assert "PRES28" in all_tickers  # would be starved to zero before this fix
    assert "TEMPNYC" in all_tickers  # would be starved to zero before this fix
    assert len(candidates["economics"]) == 2  # Economics capped too, not all 20


def test_propose_matches_never_shows_a_market_another_categorys_candidates(config, tmp_path):
    """The bug the user suspected: kalshi_candidates used to be one flat list
    shown to EVERY Polymarket market regardless of category -- a weather
    market got shown politics/entertainment candidates too, diluting the
    LLM's judgment and burning tokens on options that can never be a real
    match. Candidates must be scoped to each market's own category."""
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0xwx', 'wx', 'Will it rain in NYC?', 'weather', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    config["universe"]["priority_categories"] = ["weather"]
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = RecordingFakeLlm()
    # Politics candidates present, but NOT under "weather" -- must never
    # reach the weather market's prompt.
    candidates = {"politics": [FakeKalshiCandidate("PRES28", "2028 presidential race")]}
    propose_matches(conn, config, candidates, llm, markets_map_path=map_path)

    assert len(llm.prompts) == 0  # no weather candidates at all -> market skipped, LLM never called
    conn.close()


def test_propose_matches_appends_to_proposed_not_confirmed(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)

    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85,
                               "rationale": "same FOMC meeting"}]})
    candidates = {"economics": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]}

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert len(proposals) == 1
    assert proposals[0]["external_id"] == "FEDMAR"
    assert proposals[0]["venue"] == "kalshi"

    data = load_markets_map(map_path)
    assert data["confirmed"] == []  # propose never writes to confirmed
    assert len(data["proposed"]) == 1
    conn.close()


def test_propose_matches_skips_already_proposed_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES ('0x1', 's', 'Will the Fed cut rates in March?', 'economics', 'd',
                   '2026-12-31T00:00:00+00:00', 'tok', 'liquid', 1, 0, 200000, 5000000)"""
    )
    conn.commit()
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [], "proposed": [{"condition_id": "0x1", "venue": "kalshi",
                                       "external_id": "FEDMAR", "confidence": 0.5}]},
        map_path,
    )
    llm = FakeLlm({"matches": [{"external_id": "FEDMAR", "confidence": 0.85, "rationale": "x"}]})
    candidates = {"economics": [FakeKalshiCandidate("FEDMAR", "Fed cuts rates in March FOMC meeting")]}

    proposals = propose_matches(conn, config, candidates, llm, markets_map_path=map_path)
    assert proposals == []  # already proposed -- LLM not asked to re-propose it
    assert llm.calls == 0
    conn.close()


def test_load_pmxt_candidates_missing_file_returns_empty(tmp_path):
    assert load_pmxt_candidates(tmp_path / "does_not_exist.json") == []


def test_load_pmxt_candidates_malformed_json_returns_empty(tmp_path):
    path = tmp_path / "pmxt_candidates.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_pmxt_candidates(path) == []


def _insert_market(conn, condition_id="0x1", question="Will the Fed cut rates in March?",
                   description="FOMC statement decides"):
    conn.execute(
        """INSERT INTO markets (condition_id, slug, question, category, description,
                                end_date_iso, token_id_yes, tier, active, closed,
                                liquidity_num, volume_num)
           VALUES (?, 's', ?, 'economics', ?, '2026-12-31T00:00:00+00:00', 'tok',
                   'liquid', 1, 0, 200000, 5000000)""",
        (condition_id, question, description),
    )
    conn.commit()


def test_verify_pmxt_candidates_no_file_returns_empty_without_calling_llm(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm,
                                       candidates_path=tmp_path / "missing.json",
                                       markets_map_path=tmp_path / "markets_map.yaml")
    assert proposals == []
    assert llm.calls == 0
    conn.close()


def test_verify_pmxt_candidates_appends_matched_pair_with_pmxt_source(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "Fed cuts rates in March FOMC meeting",
        "relation_type": "identity", "confidence": 0.77, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "same FOMC decision"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert len(proposals) == 1
    p = proposals[0]
    assert p["condition_id"] == "0x1" and p["venue"] == "kalshi" and p["external_id"] == "FEDMAR"
    assert p["source"] == "pmxt"
    assert p["source_meta"] == {"relation_type": "identity", "pmxt_confidence": 0.77}
    assert llm.calls == 1

    data = load_markets_map(map_path)
    assert data["confirmed"] == []  # never auto-confirms
    assert len(data["proposed"]) == 1

    # File consumed so a stale scan isn't re-verified forever.
    assert json.loads(cand_path.read_text(encoding="utf-8")) == []
    conn.close()


def test_verify_pmxt_candidates_skips_llm_rejected_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "totally unrelated market",
        "relation_type": "loose", "confidence": 0.4, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": False, "confidence": 0.1, "rationale": "different events"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert load_markets_map(map_path)["proposed"] == []
    # Still consumed even when rejected -- a stale rejected pair shouldn't be
    # re-asked forever either.
    assert json.loads(cand_path.read_text(encoding="utf-8")) == []
    conn.close()


def test_verify_pmxt_candidates_skips_already_confirmed_or_proposed_pair(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map(
        {"confirmed": [{"condition_id": "0x1", "venue": "kalshi", "external_id": "FEDMAR"}],
         "proposed": []},
        map_path,
    )
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "Fed cuts rates in March FOMC meeting",
        "relation_type": "identity", "confidence": 0.9, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert llm.calls == 0  # already confirmed -- never re-asked
    conn.close()


def test_verify_pmxt_candidates_skips_malformed_entry_missing_fields(config, tmp_path):
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([
        {"poly_question": "missing poly_condition_id", "kalshi_ticker": "X"},
        {"poly_condition_id": "0x1", "poly_question": "missing kalshi_ticker"},
    ]), encoding="utf-8")

    llm = FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert proposals == []
    assert llm.calls == 0
    conn.close()


def _pmxt_candidate(condition_id, ticker):
    return {"poly_condition_id": condition_id, "poly_question": f"q {condition_id}",
            "kalshi_ticker": ticker, "kalshi_title": f"t {ticker}", "relation_type": "identity",
            "confidence": 0.9, "scanned_ts": "2026-09-30T17:00:00+00:00"}


def test_verify_pmxt_candidates_keeps_what_the_scan_adds_while_it_runs(config, tmp_path):
    """A scan is one lookup per market now and runs for minutes to hours
    beside the 17:00/21:00 verify; clearing the whole file at the end erased
    whatever the scan had appended in the meantime."""
    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([_pmxt_candidate("0x1", "FEDMAR")]), encoding="utf-8")

    class ScanAppendsMeanwhile(FakeLlm):
        def complete(self, system, prompt, purpose, max_tokens=2000):
            current = json.loads(cand_path.read_text(encoding="utf-8"))
            cand_path.write_text(json.dumps(current + [_pmxt_candidate("0x9", "LATER")]),
                                 encoding="utf-8")
            return super().complete(system, prompt, purpose, max_tokens)

    llm = ScanAppendsMeanwhile({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert [p["external_id"] for p in proposals] == ["FEDMAR"]
    assert json.loads(cand_path.read_text(encoding="utf-8")) == [_pmxt_candidate("0x9", "LATER")]
    conn.close()


def test_verify_pmxt_candidates_stops_at_the_llm_cap_keeping_proposals_and_the_rest(config, tmp_path):
    from lab.news.extract import BudgetExceeded

    conn = db.connect(config["storage"]["db_path"])
    for cid in ("0x1", "0x2", "0x3"):
        _insert_market(conn, condition_id=cid)
    map_path = tmp_path / "markets_map.yaml"
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    cand_path = tmp_path / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([_pmxt_candidate("0x1", "A"), _pmxt_candidate("0x2", "B"),
                                     _pmxt_candidate("0x3", "C")]), encoding="utf-8")

    class CapAfterOne(FakeLlm):
        def complete(self, system, prompt, purpose, max_tokens=2000):
            if self.calls == 1:
                raise BudgetExceeded("daily LLM cap reached")
            return super().complete(system, prompt, purpose, max_tokens)

    llm = CapAfterOne({"match": True, "confidence": 0.9, "rationale": "x"})
    proposals = verify_pmxt_candidates(conn, config, llm, candidates_path=cand_path,
                                       markets_map_path=map_path)

    assert [p["external_id"] for p in proposals] == ["A"]
    assert [p["external_id"] for p in load_markets_map(map_path)["proposed"]] == ["A"]
    assert [c["kalshi_ticker"] for c in json.loads(cand_path.read_text(encoding="utf-8"))] == ["B", "C"]
    conn.close()


# --- commit_and_push_markets_map (multi-host pmxt: needed once the
# scan+verify cycle can run on a host that isn't the one M7 reads at forecast
# time -- see jobs.run_pmxt_verify_job) ------------------------------------

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


def test_commit_and_push_markets_map_is_noop_when_unchanged(config, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    map_path = repo / "data" / "markets_map.yaml"
    map_path.parent.mkdir(parents=True)
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    # Committed as part of "init" so git sees no pending changes to it.
    subprocess.run(["git", "add", "data/markets_map.yaml"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed markets_map"], cwd=repo, capture_output=True, text=True, check=True)
    monkeypatch.setattr("lab.models.m7_crossvenue.PROJECT_ROOT", repo)

    result = commit_and_push_markets_map(config, path=map_path)
    assert result == {"committed": False, "reason": "no_changes"}


def test_commit_and_push_markets_map_commits_pending_changes(config, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    map_path = repo / "data" / "markets_map.yaml"
    map_path.parent.mkdir(parents=True)
    save_markets_map({"confirmed": [], "proposed": [{"condition_id": "0x1", "venue": "kalshi",
                                                      "external_id": "FEDMAR"}]}, map_path)
    monkeypatch.setattr("lab.models.m7_crossvenue.PROJECT_ROOT", repo)

    config = {**config, "cross_venue": {"markets_map_push": False}}
    result = commit_and_push_markets_map(config, path=map_path)
    assert result["committed"] is True
    assert "pushed" not in result  # push disabled by config

    log = subprocess.run(["git", "log", "--name-only", "-1"], cwd=repo, capture_output=True, text=True)
    assert "data/markets_map.yaml" in log.stdout

    # A second call with nothing new pending is a clean no-op.
    assert commit_and_push_markets_map(config, path=map_path) == {"committed": False, "reason": "no_changes"}


def test_run_pmxt_verify_job_skips_cleanly_with_no_llm_and_makes_no_commit(config, tmp_path, monkeypatch):
    from lab import jobs

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    map_path = repo / "data" / "markets_map.yaml"
    map_path.parent.mkdir(parents=True)
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    subprocess.run(["git", "add", "data/markets_map.yaml"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, capture_output=True, text=True, check=True)
    monkeypatch.setattr("lab.models.m7_crossvenue.PROJECT_ROOT", repo)
    monkeypatch.setattr("lab.models.m7_crossvenue.DEFAULT_MAP_PATH", map_path)
    monkeypatch.setattr("lab.news.extract.create_llm_client", lambda *a, **k: None)

    result = jobs.run_pmxt_verify_job({**config, "cross_venue": {"markets_map_push": False}})
    assert result["skipped"] == "no_llm"
    # No LLM -> verify_pmxt_candidates never ran -> nothing to commit either.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_run_pmxt_verify_job_commits_when_it_produces_new_proposals(config, tmp_path, monkeypatch):
    from lab import jobs

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    map_path = repo / "data" / "markets_map.yaml"
    map_path.parent.mkdir(parents=True)
    save_markets_map({"confirmed": [], "proposed": []}, map_path)
    subprocess.run(["git", "add", "data/markets_map.yaml"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, capture_output=True, text=True, check=True)

    cand_path = repo / "data" / "pmxt_candidates.json"
    cand_path.write_text(json.dumps([{
        "poly_condition_id": "0x1", "poly_question": "Will the Fed cut rates in March?",
        "kalshi_ticker": "FEDMAR", "kalshi_title": "Fed cuts rates in March FOMC meeting",
        "relation_type": "identity", "confidence": 0.77, "scanned_ts": "2026-07-08T00:00:00+00:00",
    }]), encoding="utf-8")

    monkeypatch.setattr("lab.models.m7_crossvenue.PROJECT_ROOT", repo)
    monkeypatch.setattr("lab.models.m7_crossvenue.DEFAULT_MAP_PATH", map_path)
    monkeypatch.setattr("lab.models.m7_crossvenue.DEFAULT_PMXT_CANDIDATES_PATH", cand_path)
    monkeypatch.setattr("lab.news.extract.create_llm_client",
                        lambda *a, **k: FakeLlm({"match": True, "confidence": 0.9, "rationale": "x"}))

    conn = db.connect(config["storage"]["db_path"])
    _insert_market(conn)
    conn.close()

    result = jobs.run_pmxt_verify_job({**config, "cross_venue": {"markets_map_push": False}})
    assert result["new_proposals"] == 1
    assert result["git"]["committed"] is True

    log = subprocess.run(["git", "log", "--name-only", "-1"], cwd=repo, capture_output=True, text=True)
    assert "data/markets_map.yaml" in log.stdout
    assert len(load_markets_map(map_path)["proposed"]) == 1


# --- pmxt verify prompt: Kalshi identity lives in ticker + outcomes --------

def test_pmxt_prompt_carries_ticker_and_outcomes():
    """The 2026-07-28 defect: the prompt showed only Kalshi's market-level
    title, which for a per-candidate market is the broad EVENT question. The
    verifier then rejected every candidate -- correctly, on that evidence.
    Ticker and outcome labels are what actually identify the market."""
    from lab.models.m7_crossvenue import _pmxt_verify_prompt

    prompt = _pmxt_verify_prompt(
        "Will Nikki Haley win the 2028 US Presidential Election?",
        "Resolves YES if Haley is inaugurated in 2029.",
        "Who will win the next presidential election?",
        "identity", 0.95,
        kalshi_ticker="KXPRESPERSON-28-NHAL",
        kalshi_outcomes=["Nikki Haley", "Not Nikki Haley"],
        kalshi_description="If Nikki Haley is the next person inaugurated...",
    )

    assert "KXPRESPERSON-28-NHAL" in prompt
    assert "Nikki Haley" in prompt and "Not Nikki Haley" in prompt
    assert "If Nikki Haley is the next person inaugurated" in prompt
    # The disambiguating instruction must be present, or the title still
    # dominates the judgement.
    assert "ticker suffix" in prompt
    # Mirror case: a negRisk Polymarket leg carries the whole set's criteria.
    assert "negRisk" in prompt


def test_pmxt_prompt_omits_absent_optional_fields():
    """Older candidate files carry no ticker/outcomes; the prompt must stay
    well-formed rather than emitting empty labelled sections."""
    from lab.models.m7_crossvenue import _pmxt_verify_prompt

    prompt = _pmxt_verify_prompt("Q?", None, "K title", "identity", 0.5)

    assert "KALSHI TICKER:" not in prompt
    assert "KALSHI OUTCOMES:" not in prompt
    assert "KALSHI RESOLUTION CRITERIA:" not in prompt
    assert "SUGGESTED KALSHI MATCH: K title" in prompt


# --- confirmed pairs must be auditable: Kalshi metadata backfill -----------

@pytest.fixture()
def backfill_conn(tmp_path):
    from lab.store import db as dbmod

    c = dbmod.connect(tmp_path / "lab.db")
    yield c
    c.close()


def test_backfill_populates_an_empty_placeholder(backfill_conn, monkeypatch):
    """db.link_event leaves a bare placeholder for a side the venue collector
    hasn't synced, and for confirmed pairs it never syncs them -- 24 of 26 sat
    with question=NULL for three weeks. An empty row makes the pair
    unauditable: there is no resolution text to compare against."""
    from lab.models import m7_crossvenue as m7
    from lab.store import db as dbmod

    from lab.api.kalshi import KalshiMarket

    # A real KalshiMarket, not a duck-typed stand-in: since 2026-08-22 the
    # backfill writes the same full row the collector writes (category,
    # active/closed, tier, volumes), so it needs the same fields.
    _Market = KalshiMarket.model_validate({
        "ticker": "KXGOVCA-26-XBEC",
        "title": "Will Xavier Becerra win the California governorship?",
        "rules_primary": "If Xavier Becerra is elected...",
        "close_time": "2027-11-03T15:00:00Z", "status": "active",
        "volume_fp": "9000", "open_interest_fp": "500",
    })

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def market(self, ticker):
            return _Market

        async def aclose(self):
            pass

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _Client)
    cid = dbmod.venue_condition_id("kalshi", "KXGOVCA-26-XBEC")
    backfill_conn.execute(
        "INSERT INTO markets (condition_id, venue, venue_native_id, tier, active, closed) "
        "VALUES (?, 'kalshi', 'KXGOVCA-26-XBEC', 'ignored', 0, 0)", (cid,))
    backfill_conn.commit()

    assert m7.backfill_kalshi_metadata(backfill_conn, "KXGOVCA-26-XBEC") is True
    row = backfill_conn.execute(
        "SELECT question, description FROM markets WHERE condition_id = ?", (cid,)).fetchone()
    assert row["question"] == _Market.title
    # and it is now a leg the collector will actually snapshot
    active = backfill_conn.execute(
        "SELECT active, category FROM markets WHERE condition_id = ?", (cid,)).fetchone()
    assert active["active"] == 1 and active["category"]
    assert "Xavier Becerra is elected" in row["description"]


def test_backfill_never_overwrites_a_collected_row(backfill_conn, monkeypatch):
    """The venue collector is authoritative where it has run."""
    from lab.models import m7_crossvenue as m7
    from lab.store import db as dbmod

    def _boom(*a, **k):
        raise AssertionError("network hit despite the row already being populated")

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _boom)
    cid = dbmod.venue_condition_id("kalshi", "KXFEDDECISION-26SEP-H25")
    backfill_conn.execute(
        "INSERT INTO markets (condition_id, venue, venue_native_id, question, tier, active, closed) "
        "VALUES (?, 'kalshi', 'KXFEDDECISION-26SEP-H25', 'real question', 'tail', 1, 0)", (cid,))
    backfill_conn.commit()

    assert m7.backfill_kalshi_metadata(backfill_conn, "KXFEDDECISION-26SEP-H25") is False
    assert backfill_conn.execute(
        "SELECT question FROM markets WHERE condition_id = ?", (cid,)
    ).fetchone()["question"] == "real question"


def test_backfill_failure_does_not_break_confirmation(backfill_conn, monkeypatch):
    """Guardrail 9: a confirmation must not fail because Kalshi is briefly
    unreachable -- the placeholder just stays empty."""
    from lab.models import m7_crossvenue as m7
    from lab.store import db as dbmod

    class _Failing:
        def __init__(self, *a, **k):
            pass

        async def market(self, ticker):
            raise RuntimeError("kalshi down")

        async def aclose(self):
            pass

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _Failing)
    cid = dbmod.venue_condition_id("kalshi", "KXTEST-1")
    backfill_conn.execute(
        "INSERT INTO markets (condition_id, venue, venue_native_id, tier, active, closed) "
        "VALUES (?, 'kalshi', 'KXTEST-1', 'ignored', 0, 0)", (cid,))
    backfill_conn.commit()

    assert m7.backfill_kalshi_metadata(backfill_conn, "KXTEST-1") is False


def _fake_client(market=None):
    """A Kalshi client stub. With no argument it answers each request with a
    market carrying THAT ticker -- the repair keys the row off the fetched
    market, so a stub that always returns one ticker would collapse several
    repairs into a single row."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def market(self, ticker):
            return market if market is not None else _kalshi_market(ticker)

        async def aclose(self):
            pass

    return _Client


def _kalshi_market(ticker: str, status: str = "active"):
    from lab.api.kalshi import KalshiMarket
    return KalshiMarket.model_validate({
        "ticker": ticker, "title": "Will X happen?", "status": status,
        "rules_primary": "Resolves YES if X.", "close_time": "2027-01-01T00:00:00Z",
        "volume_fp": "9000", "open_interest_fp": "500",
    })


def test_backfill_completes_the_leg_instead_of_leaving_a_stub(tmp_path, monkeypatch):
    """Regression, 2026-08-22: the confirmation path wrote four columns with a
    bare UPDATE and left category NULL, active 0, tier 'ignored'.
    `tracked_kalshi_markets` selects on `active = 1`, so the Kalshi leg of a
    confirmed pair was never snapshotted and M7 could not use it -- 113 of 192
    confirmed pairs (59%) were in that state, while M7 covered 101 markets a
    day. The leg must come out of this the same shape the collector writes."""
    import lab.models.m7_crossvenue as m7
    from lab.store import db
    from lab.util import load_config

    conn = db.connect(tmp_path / "lab.db")
    # a sibling the collector already categorized -- the series carries it
    db.upsert_market(conn, {
        "condition_id": "kalshi:KXGOVCA-26-AAA", "venue": "kalshi",
        "venue_native_id": "KXGOVCA-26-AAA", "slug": None, "question": "sibling",
        "category": "politics", "description": "d", "end_date_iso": "2027-01-01T00:00:00Z",
        "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 1, "closed": 0,
        "liquidity_num": 0.0, "volume_num": 1.0, "tier": "tail",
    })
    conn.commit()

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _fake_client(_kalshi_market("KXGOVCA-26-BBB")))
    assert m7.backfill_kalshi_metadata(conn, "KXGOVCA-26-BBB", load_config()) is True

    row = conn.execute("SELECT category, active, closed, tier, question FROM markets "
                       "WHERE condition_id = ?", ("kalshi:KXGOVCA-26-BBB",)).fetchone()
    assert row["category"] == "politics"      # inherited from the series sibling
    assert row["active"] == 1                 # the whole point: it gets snapshotted now
    assert row["closed"] == 0
    assert row["tier"] in ("liquid", "tail")  # never left as the placeholder 'ignored'
    assert row["question"] == "Will X happen?"
    conn.close()


def test_backfill_leaves_a_collector_synced_leg_alone(tmp_path, monkeypatch):
    """The gate moved from `question` to `category`, so it must still refuse to
    touch a row the collector owns -- otherwise the repair would overwrite real
    universe state with a single-ticker fetch."""
    import lab.models.m7_crossvenue as m7
    from lab.store import db
    from lab.util import load_config

    conn = db.connect(tmp_path / "lab.db")
    db.upsert_market(conn, {
        "condition_id": "kalshi:KXGOVCA-26-CCC", "venue": "kalshi",
        "venue_native_id": "KXGOVCA-26-CCC", "slug": None, "question": "real",
        "category": "politics", "description": "d", "end_date_iso": "2027-01-01T00:00:00Z",
        "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 1, "closed": 0,
        "liquidity_num": 0.0, "volume_num": 1.0, "tier": "liquid",
    })
    conn.commit()

    class _MustNotFetch:
        def __init__(self, *a, **k):
            raise AssertionError("a collector-synced leg must not be re-fetched")

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _MustNotFetch)
    assert m7.backfill_kalshi_metadata(conn, "KXGOVCA-26-CCC", load_config()) is False
    conn.close()


def test_repair_pass_finds_and_bounds_incomplete_legs(tmp_path, monkeypatch):
    """The repair walks confirmed pairs, touches only incomplete legs, and is
    bounded per run so a backlog drains politely instead of in one burst."""
    import lab.models.m7_crossvenue as m7
    from lab.store import db
    from lab.util import load_config

    conn = db.connect(tmp_path / "lab.db")
    for i in range(5):
        db.upsert_market(conn, {
            "condition_id": f"kalshi:KXT-26-{i}", "venue": "kalshi",
            "venue_native_id": f"KXT-26-{i}", "slug": None, "question": "stub",
            "category": None, "description": None, "end_date_iso": None,
            "token_id_yes": None, "token_id_no": None, "neg_risk": 0, "active": 0, "closed": 0,
            "liquidity_num": None, "volume_num": None, "tier": "ignored",
        })
    conn.commit()

    map_path = tmp_path / "markets_map.yaml"
    m7.save_markets_map({"confirmed": [
        {"condition_id": f"0x{i}", "venue": "kalshi", "external_id": f"KXT-26-{i}"}
        for i in range(5)], "proposed": []}, map_path)

    monkeypatch.setattr("lab.api.kalshi.KalshiClient", _fake_client())
    res = m7.repair_confirmed_kalshi_legs(conn, load_config(), limit=2, path=map_path)

    assert res["checked"] == 5
    assert res["incomplete"] == 5
    assert res["repaired"] == 2          # bounded, not all five at once
    assert conn.execute("SELECT COUNT(*) FROM markets WHERE venue='kalshi' AND active=1"
                        ).fetchone()[0] == 2
    conn.close()
