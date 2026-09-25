"""The sports null control: eligibility sampling and scoring membership.

The control is the harness's own placebo (§4.7) -- apparent skill on sports is
supposed to indicate a broken instrument. Until 2026-07-28 it was itself
broken: `null_control_ids` sampled from every sports market ever seen, ~80% of
them already resolved and ~93% in the `ignored` tier, while the forecast loop
only considers `tier IN ('liquid','tail') AND active = 1 AND closed = 0`. Of
30 ids drawn on production data, 27 were already closed and **none** survived
the loop, so the control produced zero forecasts for the whole collection
period while reporting as configured.
"""

from __future__ import annotations

import pytest

from lab.forecast import null_control_ids, null_control_ids_by_venue
from lab.store import db
from lab.util import load_config, now_utc_iso


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _seed_market(conn, cid, venue="polymarket", category="sports",
                 tier="tail", active=1, closed=0):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-01-01T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": active, "closed": closed,
        "liquidity_num": 1.0, "volume_num": 1.0, "tier": tier,
    })


def _seed_forecast(conn, cid, model_id="m0_market"):
    db.append_forecast(conn, {
        "ts": now_utc_iso(), "condition_id": cid, "model_id": model_id,
        "p_yes": 0.5, "p_market_at_ts": 0.5,
    })


# --- eligibility sampling ---------------------------------------------------

def test_sample_only_contains_forecastable_markets(conn):
    """THE regression test. With ineligible markets vastly outnumbering
    eligible ones -- the production ratio -- an unfiltered sample draws
    almost nothing forecastable, which is how the control silently died."""
    config = load_config()
    for i in range(200):
        _seed_market(conn, f"closed_{i}", tier="ignored", active=0, closed=1)
    for i in range(5):
        _seed_market(conn, f"live_{i}", tier="liquid", active=1, closed=0)
    conn.commit()

    ids = null_control_ids(conn, config)

    assert ids, "sample is empty -- the control would forecast nothing"
    assert ids == {f"live_{i}" for i in range(5)}, (
        "sample contains markets the forecast loop will never consider"
    )


def test_sample_respects_configured_size(conn):
    config = load_config()
    size = config["universe"]["null_control"]["sample_size"]
    for i in range(size * 3):
        _seed_market(conn, f"live_{i}", tier="liquid")
    conn.commit()

    assert len(null_control_ids(conn, config)) == size


def test_sample_is_deterministic_for_a_fixed_pool(conn):
    """Same seed, same pool -> same cohort, so a day's coverage is
    reproducible from the committed seed (Phase 15)."""
    config = load_config()
    for i in range(100):
        _seed_market(conn, f"live_{i}", tier="liquid")
    conn.commit()

    assert null_control_ids(conn, config) == null_control_ids(conn, config)


def test_sample_excludes_non_sports(conn):
    config = load_config()
    for i in range(10):
        _seed_market(conn, f"sport_{i}", tier="liquid")
        _seed_market(conn, f"pol_{i}", category="politics", tier="liquid")
    conn.commit()

    assert all(cid.startswith("sport_") for cid in null_control_ids(conn, config))


# --- scoring membership -----------------------------------------------------

def test_scoring_membership_is_what_was_forecast(conn):
    """Membership is read off the ledger, not re-drawn: the eligible pool
    turns over, so a re-draw would both miss real observations and admit
    markets never forecast."""
    config = load_config()
    _seed_market(conn, "forecast_me", tier="liquid")
    _seed_market(conn, "never_forecast", tier="liquid")
    _seed_forecast(conn, "forecast_me")
    conn.commit()

    result = null_control_ids_by_venue(conn, config)
    assert result["polymarket"] == {"forecast_me"}


def test_scoring_membership_keeps_markets_that_since_closed(conn):
    """The case a re-draw gets wrong: forecast while eligible, resolved
    later. Those resolved rows ARE the null control's observations and must
    still be scored once the market can no longer be sampled."""
    config = load_config()
    _seed_market(conn, "was_live", tier="liquid", active=1, closed=0)
    _seed_forecast(conn, "was_live")
    conn.commit()
    # Market closes, as every sports market eventually does.
    _seed_market(conn, "was_live", tier="ignored", active=0, closed=1)
    conn.commit()

    assert null_control_ids(conn, config) == set(), "closed market is still sampled"
    assert null_control_ids_by_venue(conn, config)["polymarket"] == {"was_live"}


def test_scoring_membership_is_scoped_per_venue(conn):
    config = load_config()
    for i in range(3):
        _seed_market(conn, f"poly_{i}", "polymarket", tier="liquid")
        _seed_forecast(conn, f"poly_{i}")
        _seed_market(conn, f"kalshi:T{i}", "kalshi", tier="liquid")
        _seed_forecast(conn, f"kalshi:T{i}")
    _seed_market(conn, "metaculus:Q1", "metaculus", tier="liquid")
    _seed_forecast(conn, "metaculus:Q1")
    conn.commit()

    result = null_control_ids_by_venue(conn, config)

    assert set(result.keys()) == {"polymarket", "kalshi"}  # forecastable venues only
    assert result["polymarket"] == {f"poly_{i}" for i in range(3)}
    assert result["kalshi"] == {f"kalshi:T{i}" for i in range(3)}
    assert result["polymarket"].isdisjoint(result["kalshi"])


def test_scoring_membership_includes_m6_negrisk_sweep(conn):
    """M6 scans every negRisk event regardless of category (the documented
    §3.5 exception). A sports market it forecasts is equally a placebo
    observation and belongs in the control, not in the primary tables."""
    config = load_config()
    _seed_market(conn, "m6_only", tier="liquid")
    _seed_forecast(conn, "m6_only", model_id="m6_consistency")
    conn.commit()

    assert null_control_ids_by_venue(conn, config)["polymarket"] == {"m6_only"}


# --- the price-move trigger needs its own spacing (2026-08-02..06) ----------

def test_price_move_trigger_cannot_refire_within_its_minimum_spacing():
    """It reads a 24-HOUR move, so without spacing an hourly re-run writes the
    same event over and over. During the crash loop that produced up to 25
    forecasts for one market-day and 54,634 excess rows in five days -- 10% of
    the whole ledger, concentrated on the volatile markets the trigger selects.
    """
    from datetime import timedelta

    from lab.forecast import _due
    from lab.store import db
    from lab.util import now_utc

    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    conn = db.connect(tmp / "lab.db")
    try:
        config = {"forecast": {"cadence_hours": 24, "price_move_trigger": 0.10,
                               "price_move_min_hours": 6}}
        big_move = 0.5   # far over the trigger

        # No prior forecast -> always due.
        assert _due(conn, "0x1", "m0_market", config, big_move) is True

        conn.execute(
            "INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts)"
            " VALUES (?, '0x1', 'm0_market', 0.5, 0.5)",
            ((now_utc() - timedelta(hours=1)).isoformat(timespec="seconds"),))
        conn.commit()
        # One hour later, same 24h move: NOT due. This is the whole fix.
        assert _due(conn, "0x1", "m0_market", config, big_move) is False

        # A second market, last forecast past the spacing. (Not a DELETE on the
        # first: the ledger's authorizer forbids it, which is the point of it.)
        conn.execute(
            "INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts)"
            " VALUES (?, '0x2', 'm0_market', 0.5, 0.5)",
            ((now_utc() - timedelta(hours=7)).isoformat(timespec="seconds"),))
        conn.commit()
        # Past the spacing, a genuine move still earns its extra forecast.
        assert _due(conn, "0x2", "m0_market", config, big_move) is True
        # ...and a small move still does not.
        assert _due(conn, "0x2", "m0_market", config, 0.01) is False
    finally:
        conn.close()


def test_daily_cadence_is_unaffected_by_the_spacing_guard():
    """The guard must be inert in normal operation: a market with no price move
    still gets its once-a-day forecast."""
    from datetime import timedelta

    from lab.forecast import _due
    from lab.store import db
    from lab.util import now_utc

    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    conn = db.connect(tmp / "lab.db")
    try:
        config = {"forecast": {"cadence_hours": 24, "price_move_trigger": 0.10,
                               "price_move_min_hours": 6}}
        conn.execute(
            "INSERT INTO forecasts (ts, condition_id, model_id, p_yes, p_market_at_ts)"
            " VALUES (?, '0x1', 'm0_market', 0.5, 0.5)",
            ((now_utc() - timedelta(hours=25)).isoformat(timespec="seconds"),))
        conn.commit()
        assert _due(conn, "0x1", "m0_market", config, None) is True
        assert _due(conn, "0x1", "m0_market", config, 0.0) is True
    finally:
        conn.close()


# --- a market past its end date is not forecastable (2026-08-10) ------------

def test_markets_past_their_end_date_are_not_forecast():
    """39,583 forecasts had been written on already-ended Kalshi markets -- 38%
    of that venue's scoring population -- because `active`/`closed` are only as
    fresh as the last universe sync and Kalshi's was starving its own tail.

    These are not weak observations. Trading has stopped, the outcome is
    determined, and both the model and the market baseline sit on the known
    answer, so the paired Brier difference collapses toward zero: measured on
    the live data, excluding them moved m1_debiased's Kalshi skill from
    -0.00185 to -0.00251 and m4_ensemble's from +0.00094 to +0.00078. They
    dilute the effect and inflate n at the same time.
    """
    from datetime import timedelta
    from pathlib import Path
    import tempfile

    import polars as pl

    from lab.forecast import eligible_market_states
    from lab.store import db
    from lab.store.snapshots import SnapshotStore, floor_ts_bucket
    from lab.util import load_config, now_utc

    tmp = Path(tempfile.mkdtemp())
    config = load_config()
    config["storage"] = {**config["storage"],
                         "db_path": str(tmp / "lab.db"),
                         "snapshots_dir": str(tmp / "snapshots")}
    conn = db.connect(tmp / "lab.db")
    store = SnapshotStore(str(tmp / "snapshots"))
    now = now_utc()
    ts = floor_ts_bucket(now, 5)
    try:
        for cid, end in (("0xlive", now + timedelta(days=3)),
                         ("0xended", now - timedelta(days=2)),
                         ("0xtoday", now - timedelta(minutes=1))):
            conn.execute(
                "INSERT INTO markets (condition_id, question, category, description,"
                " end_date_iso, venue, tier, active, closed) VALUES (?,?,?,?,?,"
                "'kalshi','liquid',1,0)",
                (cid, "Q?", "economics", "rules", end.isoformat(timespec="seconds")))
            store.append([{
                "ts": ts, "condition_id": cid, "token_id_yes": None,
                "best_bid": 0.49, "best_ask": 0.51, "mid": 0.5, "spread": 0.02,
                "bid_depth_usd": 900.0, "ask_depth_usd": 900.0,
                "last_trade_price": None, "venue": "kalshi"}])
        conn.commit()

        ids = {s.condition_id for s in eligible_market_states(conn, store, config)}
    finally:
        conn.close()

    assert "0xlive" in ids, "a market still trading must stay eligible"
    assert "0xended" not in ids, "a market two days past its end date is not forecastable"
    assert "0xtoday" not in ids, "past the end date by a minute is still past it"


# --- Phase 15 microstructure covariates (implemented 2026-08-10) ------------

def test_forecast_rows_carry_the_phase_15_covariates():
    """Specified in CLAUDE.md section 5 and in Phase 15's acceptance criteria
    since the phase was written, and never implemented -- only `spread_at_ts`,
    which predates Phase 15, existed. The brief is explicit that these are
    "populated going forward, never backfilled by reconstruction", so every day
    without them was a day lost.
    """
    from datetime import timedelta
    from pathlib import Path
    import tempfile

    from lab.forecast import run_forecasts
    from lab.models.base import ForecastResult
    from lab.store import db
    from lab.store.snapshots import SnapshotStore, floor_ts_bucket
    from lab.util import load_config, now_utc

    class _Fixed:
        model_id = "m0_market"

        def forecast(self, state, context=None):
            return ForecastResult(p_yes=0.5, meta={})

    tmp = Path(tempfile.mkdtemp())
    config = load_config()
    config["storage"] = {**config["storage"], "db_path": str(tmp / "lab.db"),
                         "snapshots_dir": str(tmp / "snapshots")}
    conn = db.connect(tmp / "lab.db")
    store = SnapshotStore(str(tmp / "snapshots"))
    now = now_utc()
    try:
        conn.execute(
            "INSERT INTO markets (condition_id, question, category, description,"
            " end_date_iso, venue, tier, active, closed, volume_24h_num)"
            " VALUES ('0x1','Q','economics','rules',?,'kalshi','liquid',1,0,4242.0)",
            ((now + timedelta(days=5)).isoformat(timespec="seconds"),))
        store.append([{
            "ts": floor_ts_bucket(now, 5), "condition_id": "0x1", "token_id_yes": None,
            "best_bid": 0.49, "best_ask": 0.51, "mid": 0.5, "spread": 0.02,
            "bid_depth_usd": 700.0, "ask_depth_usd": 300.0,
            "last_trade_price": None, "venue": "kalshi"}])
        conn.commit()

        run_forecasts(conn, store, [_Fixed()], config)
        row = conn.execute(
            "SELECT depth_covariate, volume_24h, trades_24h, hour_utc, spread_at_ts"
            " FROM forecasts WHERE condition_id='0x1'").fetchone()
    finally:
        conn.close()

    assert row is not None, "no forecast written -- fixture is wrong"
    assert row["depth_covariate"] == pytest.approx(1000.0)   # both sides summed
    assert row["volume_24h"] == pytest.approx(4242.0)        # from the venue's own object
    assert row["hour_utc"] == now.hour
    assert row["spread_at_ts"] == pytest.approx(0.02)
    assert row["trades_24h"] is None    # no venue reports it on what we already fetch


def test_unmeasured_depth_is_null_not_zero():
    """0.0 asserts "measured, and there is none" -- a different claim from "not
    measured", and the one that would quietly bias any depth-conditioned
    heterogeneity split."""
    from lab.forecast import _depth_usd

    assert _depth_usd({"bid_depth_usd": 700.0, "ask_depth_usd": 300.0}) == pytest.approx(1000.0)
    assert _depth_usd({"bid_depth_usd": 700.0, "ask_depth_usd": None}) == pytest.approx(700.0)
    assert _depth_usd({"bid_depth_usd": None, "ask_depth_usd": None}) is None


def _sports_market(conn, cid: str, category: str = "sports"):
    from lab.store import db
    db.upsert_market(conn, {
        "condition_id": cid, "venue": "polymarket", "venue_native_id": cid,
        "slug": None, "question": "q", "category": category, "description": "d",
        "end_date_iso": "2027-01-01T00:00:00Z", "token_id_yes": "1", "token_id_no": "2",
        "neg_risk": 0, "active": 1, "closed": 0, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })


def test_null_control_restriction_holds_on_every_write_path(tmp_path):
    """Regression, 2026-08-22. §3's null control is a UNIVERSE policy: a small
    seeded sample of sports markets, and every other sports market is
    deliberately not a forecast target. It was enforced only inside
    `eligible_market_states`, so the two models that write outside it were
    unrestricted -- m6_consistency had written on 62 sports markets and
    m7_crossvenue on 31, against a sample of 30, and 59 and 29 of those
    respectively were markets the filtered path had never produced. A control
    whose membership is not controlled is not a control."""
    from lab.forecast import drop_null_control_outsiders, null_control_ids
    from lab.store import db
    from lab.util import load_config

    conn = db.connect(tmp_path / "lab.db")
    config = load_config()
    config = {**config, "universe": {**config["universe"],
                                     "null_control": {"category": "sports",
                                                      "sample_size": 3, "random_seed": 42}}}
    for i in range(20):
        _sports_market(conn, f"0xs{i:02d}")
    for i in range(3):
        _sports_market(conn, f"0xp{i}", category="politics")
    conn.commit()

    sampled = null_control_ids(conn, config)
    assert len(sampled) == 3

    everything = [f"0xs{i:02d}" for i in range(20)] + ["0xp0", "0xp1", "0xp2"]
    allowed = drop_null_control_outsiders(conn, config, everything)

    # non-sports is never touched by this policy
    assert {"0xp0", "0xp1", "0xp2"} <= allowed
    # of the sports markets, only the sampled ones survive
    assert {c for c in allowed if c.startswith("0xs")} == sampled
    assert len(allowed) == 3 + 3
    conn.close()


def test_null_control_filter_is_a_no_op_without_sports(tmp_path):
    """The batches these two models write are usually all non-sports; the
    filter must not cost a sample draw or change anything there."""
    from lab.forecast import drop_null_control_outsiders
    from lab.store import db
    from lab.util import load_config

    conn = db.connect(tmp_path / "lab.db")
    for i in range(4):
        _sports_market(conn, f"0xp{i}", category="politics")
    conn.commit()

    ids = [f"0xp{i}" for i in range(4)]
    assert drop_null_control_outsiders(conn, load_config(), ids) == set(ids)
    assert drop_null_control_outsiders(conn, load_config(), []) == set()
    conn.close()


def test_no_write_transaction_spans_a_models_work(tmp_path):
    """2026-09-25: a 250-row batch commit bounded the forecast pass's write lock
    by row count, not by time -- so the lock stayed held across M3's LLM calls
    and M5's network requests, and collector jobs waiting on it died at
    busy_timeout (112-168 a day). Every model's forecast() must now start with
    no transaction open on the connection."""
    from lab.forecast import run_forecasts
    from lab.models.base import ForecastResult, MarketState

    config = load_config()
    c = db.connect(tmp_path / "lab.db")
    for i in range(3):
        _seed_market(c, f"m{i}", category="economics", tier="liquid")
    c.commit()

    observed: list[bool] = []

    class _Probe:
        def __init__(self, model_id):
            self.model_id = model_id

        def forecast(self, market, context):
            observed.append(c.in_transaction)
            return ForecastResult(p_yes=0.5, meta={})

    states = [MarketState(condition_id=f"m{i}", question="q", category="economics",
                          description="d", end_date_iso="2027-01-01T00:00:00Z", tier="liquid",
                          p_market=0.5, spread=0.01, snapshot_ts=now_utc_iso(),
                          days_to_resolution=90.0, venue="polymarket")
              for i in range(3)]
    from lab.store.snapshots import SnapshotStore

    run_forecasts(c, SnapshotStore(tmp_path / "snapshots"),
                  [_Probe("m0_market"), _Probe("m1_debiased")], config, states=states)
    assert observed and not any(observed), (
        "a model started its work while the previous write was still uncommitted"
    )
    c.close()
