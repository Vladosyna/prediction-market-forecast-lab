"""M5 adapters (fixture-driven) and M6 coherence scanner."""

from __future__ import annotations

import pytest

from lab.models.base import MarketState
from lab.models.m5_nowcast import M5Nowcast, MacroAdapter, parse_weather_question
from lab.models.m6_consistency import check_link, scan_negrisk_event


def _state(question, category="weather", p=0.5) -> MarketState:
    return MarketState(
        condition_id="0x1", question=question, category=category, description="d",
        end_date_iso="2026-07-05T00:00:00+00:00", tier="liquid", p_market=p,
        spread=0.02, snapshot_ts="2026-07-02T12:00:00+00:00", days_to_resolution=3.0,
    )


def test_parse_weather_question():
    spec = parse_weather_question("Will the highest temperature in NYC be above 90 degrees F?")
    assert spec == {"city": "nyc", "threshold": 90.0, "unit": "fahrenheit", "above": True}
    assert parse_weather_question("Will the Fed cut rates?") is None
    assert parse_weather_question("Will the highest temperature in Gotham be above 90?") is None


class StubWeatherAdapter:
    """Fixture adapter: 20 ensemble members, 15 exceed the threshold."""

    def covers(self, market):
        return "temperature" in (market.question or "")

    def probability(self, market):
        members = [88.0] * 5 + [92.0] * 15
        p = 15 / 20
        p = (p * 20 + 1) / 22
        return p, {"adapter": "weather", "n_members": 20, "members": members}


def test_m5_uses_adapter_and_stores_trace():
    m5 = M5Nowcast(adapters=[StubWeatherAdapter()])
    res = m5.forecast(_state("highest temperature in NYC above 90"), {})
    assert res is not None
    assert res.p_yes == pytest.approx((0.75 * 20 + 1) / 22)
    assert res.meta["n_members"] == 20        # the stored input trace
    assert "members" in res.meta


def test_m5_abstains_without_coverage():
    m5 = M5Nowcast(adapters=[StubWeatherAdapter()])
    assert m5.forecast(_state("Will X win the election?", category="politics"), {}) is None


def test_macro_adapter_uses_fred_series(monkeypatch):
    adapter = MacroAdapter(api_key="test-key")
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 2.5)
    out = adapter.probability(
        _state("Will Q3 GDP growth be above 2.0%?", category="economics"))
    assert out is not None
    p, trace = out
    assert trace["series"] == "GDPNOW"
    assert trace["nowcast"] == 2.5
    assert p > 0.5   # nowcast 2.5 sits above the 2.0 threshold


def test_macro_adapter_pce_and_direction(monkeypatch):
    adapter = MacroAdapter(api_key="test-key")
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 1.0)
    out = adapter.probability(
        _state("Will PCE growth be below 2.0% this quarter?", category="economics"))
    assert out is not None
    p, trace = out
    assert trace["series"] == "PCENOW"
    assert trace["direction"] == "below"
    assert p > 0.5   # nowcast 1.0 is below the 2.0 threshold


def test_macro_adapter_abstains_without_key():
    adapter = MacroAdapter(api_key="")   # explicit empty key disables FRED
    assert adapter.covers(_state("Will GDP growth be above 2.0%?", category="economics"))
    assert adapter.probability(
        _state("Will GDP growth be above 2.0%?", category="economics")) is None


def test_macro_adapter_sd_override_from_artifact(monkeypatch):
    """A fitted m5_macro_sd artifact overrides the hand-set default (learn/refit.py)."""
    adapter = MacroAdapter(api_key="test-key", sd_overrides={"GDPNOW": 1.23})
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 2.5)
    out = adapter.probability(_state("Will Q3 GDP growth be above 2.0%?", category="economics"))
    assert out is not None
    _, trace = out
    assert trace["sd"] == 1.23


def test_macro_adapter_sd_override_missing_series_falls_back(monkeypatch):
    adapter = MacroAdapter(api_key="test-key", sd_overrides={"PCENOW": 0.99})
    monkeypatch.setattr(adapter, "_latest_nowcast", lambda sid: 2.5)
    out = adapter.probability(_state("Will Q3 GDP growth be above 2.0%?", category="economics"))
    _, trace = out
    assert trace["sd"] == 0.55  # GDPNOW's hand-set default, unaffected by a PCENOW override


def test_m5nowcast_wires_macro_artifact_into_default_adapters():
    m5 = M5Nowcast(macro_artifact={"series": {"GDPNOW": {"sd": 0.42}}})
    macro = next(a for a in m5.adapters if isinstance(a, MacroAdapter))
    assert macro._sd_overrides == {"GDPNOW": 0.42}


def test_m6_flags_incoherent_negrisk():
    legs = [
        {"condition_id": "a", "p_yes": 0.50},
        {"condition_id": "b", "p_yes": 0.40},
        {"condition_id": "c", "p_yes": 0.25},
    ]  # sums to 1.15
    scan = scan_negrisk_event(legs)
    assert scan["incoherent"]
    assert scan["deviation"] == pytest.approx(0.15)
    assert len(scan["corrections"]) >= 1
    for c in scan["corrections"]:
        assert c["p_coherent"] == pytest.approx(c["p_market"] / 1.15)


def test_m6_silent_on_coherent_negrisk():
    legs = [
        {"condition_id": "a", "p_yes": 0.55},
        {"condition_id": "b", "p_yes": 0.30},
        {"condition_id": "c", "p_yes": 0.16},
    ]  # sums to 1.01 -- inside tolerance
    scan = scan_negrisk_event(legs)
    assert not scan["incoherent"]
    assert scan["corrections"] == []


def test_m6_link_violation():
    link = {"kind": "implies", "narrow": "n", "broad": "b"}
    # Narrow event priced ABOVE broad -> logically impossible.
    v = check_link(link, {"n": 0.6, "b": 0.4})
    assert v is not None and v["violation"] == pytest.approx(0.2)
    assert check_link(link, {"n": 0.3, "b": 0.5}) is None


# --- M6 freshness, completeness and cadence (2026-10-11, PAP 9.44) ----------

def _m6_config(tmp_path):
    from lab.util import load_config

    config = load_config()
    config["storage"] = {**config["storage"], "db_path": str(tmp_path / "lab.db"),
                         "snapshots_dir": str(tmp_path / "snapshots")}
    return config


def _market(conn, cid, tier="liquid", category="politics"):
    conn.execute("INSERT INTO markets (condition_id, venue, category, tier, active, closed)"
                 " VALUES (?, 'polymarket', ?, ?, 1, 0)", (cid, category, tier))


def test_m6_prices_only_what_guardrail_13_accepts(tmp_path):
    """M6 took any mid from the last two days, so a leg could be priced, and a
    forecast paired, a day stale. The bound is the market's own tier's."""
    from datetime import timedelta

    import polars as pl

    from lab.models.m6_consistency import fresh_prices
    from lab.store import db
    from lab.util import now_utc

    config = _m6_config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    _market(conn, "0xliq", "liquid")
    _market(conn, "0xtail", "tail")
    conn.commit()
    now = now_utc()
    latest = pl.DataFrame([
        {"ts": (now - timedelta(minutes=20)).isoformat(), "condition_id": "0xliq", "mid": 0.4, "spread": 0.02},
        {"ts": (now - timedelta(minutes=60)).isoformat(), "condition_id": "0xtail", "mid": 0.3, "spread": 0.03},
        {"ts": (now - timedelta(minutes=100)).isoformat(), "condition_id": "0xnone", "mid": 0.2, "spread": None},
        {"ts": (now - timedelta(minutes=5)).isoformat(), "condition_id": "0xnomid", "mid": None, "spread": None},
    ])
    assert fresh_prices(conn, latest, config, now) == {"0xtail": {"p_yes": 0.3, "spread": 0.03}}
    conn.close()


def test_m6_scans_an_event_only_when_every_open_leg_is_freshly_priced(tmp_path, monkeypatch):
    """A leg missing from the sum pulled it below 1 by that leg's own
    probability, which read as incoherence and pushed every other leg up.
    Closed legs cannot win and are left out of the sum on purpose."""
    import asyncio

    from lab.models import m6_consistency
    from lab.store import db
    from lab.store.snapshots import SnapshotStore, floor_ts_bucket
    from lab.util import now_utc

    config = _m6_config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    store = SnapshotStore(config["storage"]["snapshots_dir"])
    ts = floor_ts_bucket(now_utc(), 5)
    priced = {"a1": 0.3, "a2": 0.3, "a3": 0.3, "b1": 0.5, "c1": 0.6, "c2": 0.4}
    for cid, mid in priced.items():
        _market(conn, cid)
        store.append([{"ts": ts, "condition_id": cid, "mid": mid, "spread": 0.02}])
    _market(conn, "b2")          # open, never priced
    conn.commit()

    def leg(cid, closed=False):
        return {"conditionId": cid, "active": True, "closed": closed}

    events = [
        {"slug": "a", "negRisk": True, "markets": [leg("a1"), leg("a2"), leg("a3")]},
        {"slug": "b", "negRisk": True, "markets": [leg("b1"), leg("b2")]},
        {"slug": "c", "negRisk": True, "markets": [leg("c1"), leg("c2"), leg("c3", closed=True)]},
    ]

    class FakeGamma:
        def __init__(self, bucket):
            pass

        async def get_json(self, path, params=None):
            return events

        async def aclose(self):
            pass

    monkeypatch.setattr("lab.api.gamma.GammaClient", FakeGamma)
    monkeypatch.setattr(m6_consistency, "load_links", lambda: [])
    findings = asyncio.run(m6_consistency.scan_universe(conn, store, config))
    conn.close()

    by_slug = {f["event_slug"]: f for f in findings}
    assert set(by_slug) == {"a", "c"}, "event b has an unpriced open leg and must be skipped"
    assert by_slug["a"]["incoherent"]
    assert {c["condition_id"] for c in by_slug["a"]["corrections"]} == {"a1", "a2", "a3"}
    assert all(c["spread"] == pytest.approx(0.02) for c in by_slug["a"]["corrections"])
    assert by_slug["c"]["n_legs"] == 2 and not by_slug["c"]["incoherent"]


def test_m6_writes_once_a_day_against_the_price_it_was_computed_from(tmp_path):
    """No due check meant any second pass in a day wrote M6 again (30, 43 and
    37 markets on 2026-10-07, 10-09 and 10-11). And the row is paired against
    the price its coherent value came from, not a later re-read."""
    from lab.models.m6_consistency import write_m6_forecasts
    from lab.store import db

    config = _m6_config(tmp_path)
    conn = db.connect(config["storage"]["db_path"])
    _market(conn, "0xa1")
    conn.commit()
    findings = [{"incoherent": True, "corrections": [
        {"condition_id": "0xa1", "p_market": 0.30, "p_coherent": 1 / 3, "spread": 0.02}]}]

    assert write_m6_forecasts(conn, None, findings, config) == 1
    assert write_m6_forecasts(conn, None, findings, config) == 0
    row = conn.execute("SELECT p_yes, p_market_at_ts, spread_at_ts FROM forecasts "
                       "WHERE model_id = 'm6_consistency'").fetchone()
    assert row["p_market_at_ts"] == pytest.approx(0.30) and row["spread_at_ts"] == pytest.approx(0.02)
    assert row["p_yes"] == pytest.approx(1 / 3)
    conn.close()
