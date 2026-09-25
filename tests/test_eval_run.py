"""Per-venue x per-category eval grouping (brief section 7/11, Phase 11)."""

from __future__ import annotations

import pytest

from lab.eval.run import ALL_CATEGORIES, run_eval
from lab.store import db
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


def _seed(conn, cid, venue, category, n=1):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-12-31T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "liquid",
    })
    ts = now_utc().isoformat(timespec="seconds")
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": "m0_market",
        "p_yes": 0.6, "p_market_at_ts": 0.5,
    })
    db.record_resolution(conn, cid, ts, 1.0, False, "gamma")


def test_run_eval_produces_rows_per_venue_and_category(config, monkeypatch):
    # Grouping, not exclusion policy: `_seed` stamps forecasts "now", and a
    # "now" inside a declared Kalshi exclusion window (as 2026-09-25 was)
    # would remove the Kalshi row this test is about.
    monkeypatch.setattr("lab.eval.run.KALSHI_EXCLUSION_WINDOWS", ())
    conn = db.connect(config["storage"]["db_path"])
    _seed(conn, "poly_econ", "polymarket", "economics")
    _seed(conn, "poly_pol", "polymarket", "politics")
    _seed(conn, "kalshi:econ", "kalshi", "economics")
    conn.commit()

    run_eval(conn, config)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM eval_runs WHERE model_id = 'm0_market' AND window_label = 'all_time'"
    )]
    keys = {(r["venue"], r["category"]) for r in rows}
    assert ("polymarket", "economics") in keys
    assert ("polymarket", "politics") in keys
    assert ("kalshi", "economics") in keys
    # ALL-categories aggregate row exists per venue.
    assert ("polymarket", ALL_CATEGORIES) in keys
    assert ("kalshi", ALL_CATEGORIES) in keys
    # Metaculus/Manifold are not forecastable -> no rows for them.
    assert not any(r["venue"] == "metaculus" for r in rows)
    assert not any(r["venue"] == "manifold" for r in rows)

    # New Phase 11 columns are populated, not left NULL, for a fresh row.
    poly_econ = next(r for r in rows if (r["venue"], r["category"]) == ("polymarket", "economics"))
    assert poly_econ["n_event_clusters"] == 1
    assert poly_econ["cs_lo"] is not None and poly_econ["cs_hi"] is not None
    conn.close()


def _seed_disputed(conn, cid, venue, category, disputed):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": category, "description": "d",
        "end_date_iso": "2026-12-31T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "liquid",
    })
    ts = now_utc().isoformat(timespec="seconds")
    db.append_forecast(conn, {
        "ts": ts, "condition_id": cid, "model_id": "m0_market",
        "p_yes": 0.6, "p_market_at_ts": 0.5,
    })
    db.record_resolution(conn, cid, ts, 1.0, disputed, "gamma")


def test_include_disputed_adds_a_separate_row_without_touching_the_primary_one(config):
    """PAP Addendum 9.2(b): the default (include_disputed=False) path is
    completely unchanged -- disputed markets stay excluded, same as before
    this option existed. include_disputed=True is a genuinely different,
    additional comparison: it must pick up the disputed row the default
    path drops, and it must land under a distinctly-suffixed window_label
    rather than overwriting/mixing into the primary eval_runs row."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_disputed(conn, "clean", "polymarket", "economics", disputed=False)
    _seed_disputed(conn, "disputed", "polymarket", "economics", disputed=True)
    conn.commit()

    run_eval(conn, config)
    run_eval(conn, config, include_disputed=True)

    primary = conn.execute(
        "SELECT n FROM eval_runs WHERE model_id='m0_market' AND window_label='all_time' "
        "AND venue='polymarket' AND category='economics' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    inclusive = conn.execute(
        "SELECT n FROM eval_runs WHERE model_id='m0_market' "
        "AND window_label='all_time_disputed_inclusive' "
        "AND venue='polymarket' AND category='economics' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert primary["n"] == 1  # unchanged: only the clean market
    assert inclusive["n"] == 2  # both markets, disputed one now included
    conn.close()


def _seed_bucket_event(conn, event_id, model_id, ts, category="economics", true_idx=1):
    """One resolved 3-leg negRisk-style bucketed event, bucket-orderable via
    each question's numeric value (Phase 16). Legs are also ordinary resolved
    forecast rows, so they double as `evaluate_model`'s binary paired input."""
    for i, order in enumerate([3.0, 3.5, 4.0]):
        cid = f"{event_id}_{i}"
        db.upsert_market(conn, {
            "condition_id": cid, "venue": "polymarket", "venue_native_id": cid,
            "slug": None, "question": f"Will CPI be {order}%?", "category": category,
            "description": "d", "end_date_iso": "2026-12-31T00:00:00Z",
            "token_id_yes": None, "token_id_no": None, "neg_risk": 1,
            "active": 0, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
            "tier": "liquid", "event_id": event_id,
        })
        payout = 1.0 if i == true_idx else 0.0
        db.append_forecast(conn, {
            "ts": ts, "condition_id": cid, "model_id": model_id,
            "p_yes": 0.6 if i == true_idx else 0.2, "p_market_at_ts": 0.33,
        })
        db.record_resolution(conn, cid, ts, payout, False, "gamma")


def test_eval_runs_rps_columns_populate_only_with_enough_bucketed_events(config):
    """Phase 16 wiring: rps/rps_market on an eval_runs row stay NULL below
    config's min_bucketed_events (20), and populate once that many bucketed
    events exist for that model/venue/category/window."""
    conn = db.connect(config["storage"]["db_path"])
    ts = now_utc().isoformat(timespec="seconds")
    for i in range(19):
        _seed_bucket_event(conn, f"evt{i}", "m0_market", ts)
    conn.commit()

    run_eval(conn, config)
    row = conn.execute(
        "SELECT rps, rps_market FROM eval_runs WHERE model_id='m0_market' "
        "AND window_label='all_time' AND venue='polymarket' AND category='economics' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["rps"] is None
    assert row["rps_market"] is None

    _seed_bucket_event(conn, "evt19", "m0_market", ts)  # 20th event crosses the threshold
    conn.commit()
    run_eval(conn, config)
    row = conn.execute(
        "SELECT rps, rps_market FROM eval_runs WHERE model_id='m0_market' "
        "AND window_label='all_time' AND venue='polymarket' AND category='economics' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["rps"] is not None
    assert row["rps_market"] is not None
    conn.close()


# --- H1's horizon buckets must actually be scored (2026-08-10) --------------

def test_realized_horizon_bucket_classification():
    """`m1_resolved_rows`' own convention: resolved_ts - forecast_ts, not the
    market's stated end date, so a market that settles early or late is
    bucketed by what actually happened."""
    from lab.eval.run import _realized_horizon_bucket as _horizon_bucket

    def row(ts, res):
        return {"forecast_ts": ts, "resolved_ts": res}

    assert _horizon_bucket(row("2026-01-01T00:00:00+00:00", "2026-01-04T00:00:00+00:00")) == "lt7d"
    assert _horizon_bucket(row("2026-01-01T00:00:00+00:00", "2026-01-20T00:00:00+00:00")) == "7to30d"
    assert _horizon_bucket(row("2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00")) == "30to90d"
    assert _horizon_bucket(row("2026-01-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00")) == "gt90d"
    # a forecast written at or after resolution is not a horizon observation
    assert _horizon_bucket(row("2026-01-05T00:00:00+00:00", "2026-01-01T00:00:00+00:00")) is None
    assert _horizon_bucket(row(None, "2026-01-01T00:00:00+00:00")) is None


def test_run_eval_emits_horizon_bucket_rows():
    """H1 is stated over horizon buckets ("paired Brier skill in the >=30-day
    horizon buckets"). Until 2026-08-10 run_eval's dimensions were
    model x venue x category x window with no horizon at all, so the primary
    hypothesis had no primary statistic and its realized n went unseen for
    months -- 13-33 event clusters against the plan's own 200-cluster floor.
    """
    import inspect

    from lab.eval import run as evalrun

    src = inspect.getsource(evalrun.run_eval)
    assert "_stated_horizon_bucket" in src, "run_eval no longer buckets by horizon"
    assert 'f"{label}_{tag}_{bucket}"' in src, (
        "horizon rows must carry their own window_label so they never overwrite "
        "a primary row"
    )


def test_each_eval_row_is_committed_as_it_is_written(config, monkeypatch):
    """run_eval used to commit once per MODEL, so the transaction opened by the
    first eval_runs INSERT stayed open through every bootstrap and confidence
    sequence for the rest of that model -- minutes at a time -- while collector
    jobs died at busy_timeout behind it. Each row must be committed on write."""
    import lab.eval.run as er

    conn = db.connect(config["storage"]["db_path"])
    _seed(conn, "poly_econ", "polymarket", "economics")
    _seed(conn, "poly_pol", "polymarket", "politics")
    conn.commit()

    open_after_insert: list[bool] = []
    real = er.evaluate_model

    def spy(conn_, *a, **kw):
        out = real(conn_, *a, **kw)
        open_after_insert.append(conn_.in_transaction)
        return out

    monkeypatch.setattr(er, "evaluate_model", spy)
    er.run_eval(conn, config)
    assert open_after_insert and not any(open_after_insert)
    conn.close()


# --- pre-registered windows, enforced in code (2026-09-25) --------------------

def _seed_at(conn, cid, venue, forecast_ts):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": "economics", "description": "d",
        "end_date_iso": "2026-12-31T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 100.0, "volume_num": 100.0,
        "tier": "liquid",
    })
    db.append_forecast(conn, {"ts": forecast_ts, "condition_id": cid, "model_id": "m0_market",
                              "p_yes": 0.6, "p_market_at_ts": 0.5})
    db.record_resolution(conn, cid, forecast_ts, 1.0, False, "gamma")


def _n(conn, venue, label):
    row = conn.execute(
        "SELECT n FROM eval_runs WHERE model_id='m0_market' AND venue=? AND window_label=? "
        "AND category=? ORDER BY id DESC LIMIT 1", (venue, label, ALL_CATEGORIES)).fetchone()
    return row["n"] if row else 0


def test_kalshi_exclusion_windows_bind_every_eval_row_but_not_the_export(config):
    """PAP 9.17/9.26/9.30 exclude these forecast dates "for any
    Kalshi-population statistic". Until 2026-09-25 no code applied them, so
    every Kalshi number -- the daily-read CS included -- contained them. The
    paper export must stay complete: it is the record, exclusions are analysis."""
    from lab.export import export_paper_rows

    conn = db.connect(config["storage"]["db_path"])
    _seed_at(conn, "kalshi:in_917", "kalshi", "2026-08-13T02:00:00+00:00")
    _seed_at(conn, "kalshi:in_930", "kalshi", "2026-09-20T02:00:00+00:00")
    _seed_at(conn, "kalshi:clean", "kalshi", "2026-09-10T02:00:00+00:00")
    _seed_at(conn, "poly:same_day", "polymarket", "2026-08-13T02:00:00+00:00")
    conn.commit()
    run_eval(conn, config)

    assert _n(conn, "kalshi", "all_time") == 1, "only the forecast outside every window"
    assert _n(conn, "polymarket", "all_time") == 1, "the windows are Kalshi's alone"
    exported = {r["condition_id"] for r in export_paper_rows(conn)}
    assert {"kalshi:in_917", "kalshi:in_930", "kalshi:clean", "poly:same_day"} <= exported
    conn.close()


def test_the_confirmatory_window_is_the_pap_section_6_sample(config):
    """PAP §6: confirmatory = forecasts made after the 2026-07-06 commitment.
    No eval_runs row corresponded to that sample until 2026-09-25."""
    conn = db.connect(config["storage"]["db_path"])
    _seed_at(conn, "poly:before", "polymarket", "2026-07-05T23:59:59+00:00")
    _seed_at(conn, "poly:after", "polymarket", "2026-07-06T00:00:00+00:00")
    conn.commit()
    run_eval(conn, config)
    assert _n(conn, "polymarket", "confirmatory") == 1
    assert _n(conn, "polymarket", "all_time") == 2
    conn.close()



# --- horizon definition (2026-09-25, PAP 9.31) --------------------------------

def test_the_stated_horizon_is_primary_and_does_not_look_at_the_outcome():
    """Bucketing by realized horizon conditions on the outcome: a "by date"
    market resolves early when the event happens. The primary bucket must be
    what was knowable when the forecast was made."""
    from lab.eval.run import _realized_horizon_bucket, _stated_horizon_bucket

    early_yes = {"forecast_ts": "2026-07-10T00:00:00+00:00",
                 "end_date_iso": "2026-10-10T00:00:00+00:00",     # stated: ~92 days
                 "resolved_ts": "2026-07-15T00:00:00+00:00",      # happened early
                 "days_to_resolution_at_ts": None}
    assert _stated_horizon_bucket(early_yes) == "gt90d"
    assert _realized_horizon_bucket(early_yes) == "lt7d"

    frozen = {**early_yes, "days_to_resolution_at_ts": 45.0}
    assert _stated_horizon_bucket(frozen) == "30to90d", "the frozen value wins over the proxy"


def test_confirmatory_carries_both_definitions_and_other_windows_only_the_primary(config):
    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "poly:h", "venue": "polymarket", "venue_native_id": "poly:h",
        "slug": None, "question": "q", "category": "politics", "description": "d",
        "end_date_iso": "2026-12-31T00:00:00Z", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })
    db.append_forecast(conn, {"ts": "2026-09-10T02:00:00+00:00", "condition_id": "poly:h",
                              "model_id": "m0_market", "p_yes": 0.6, "p_market_at_ts": 0.5,
                              "days_to_resolution_at_ts": 40.0})
    db.record_resolution(conn, "poly:h", "2026-09-12T00:00:00+00:00", 1.0, False, "gamma")
    conn.commit()
    run_eval(conn, config)
    labels = {r["window_label"] for r in conn.execute(
        "SELECT window_label FROM eval_runs WHERE model_id='m0_market' AND venue='polymarket'")}
    assert "confirmatory_hs_30to90d" in labels
    assert "confirmatory_hr_lt7d" in labels
    assert "all_time_hs_30to90d" in labels
    assert not any(l.startswith("all_time_hr_") or "_h_" in l for l in labels)
    conn.close()


def test_the_m1_refit_learns_on_the_horizon_it_is_applied_on(config):
    """M1 picks its curve from the STATED horizon at forecast time; the lab's
    own refit fitted on the realized one -- a train/serve skew that also
    conditions on the outcome."""
    from lab.learn.loop import m1_resolved_rows

    conn = db.connect(config["storage"]["db_path"])
    db.upsert_market(conn, {
        "condition_id": "poly:r", "venue": "polymarket", "venue_native_id": "poly:r",
        "slug": None, "question": "q", "category": "politics", "description": "d",
        "end_date_iso": "2026-11-08T02:00:00+00:00", "token_id_yes": None, "token_id_no": None,
        "neg_risk": 0, "active": 1, "closed": 1, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })
    db.append_forecast(conn, {"ts": "2026-09-09T02:00:00+00:00", "condition_id": "poly:r",
                              "model_id": "m0_market", "p_yes": 0.5, "p_market_at_ts": 0.5})
    db.record_resolution(conn, "poly:r", "2026-09-14T02:00:00+00:00", 1.0, False, "gamma")
    conn.commit()
    (row,) = m1_resolved_rows(conn)
    assert row["days_to_resolution"] == pytest.approx(60.0, abs=0.01), "stated, not the 5 realized"
    conn.close()


# --- pre-registered robustness checks as code (2026-09-25) -------------------

def _mk(conn, cid, venue="polymarket", end="2026-12-31T00:00:00+00:00", neg_risk=0):
    db.upsert_market(conn, {
        "condition_id": cid, "venue": venue, "venue_native_id": cid,
        "slug": None, "question": f"q {cid}", "category": "politics", "description": "d",
        "end_date_iso": end, "token_id_yes": None, "token_id_no": None,
        "neg_risk": neg_risk, "active": 1, "closed": 1, "liquidity_num": 1.0, "volume_num": 1.0,
        "tier": "liquid",
    })


def _fc(conn, cid, ts, model="m1_debiased"):
    db.append_forecast(conn, {"ts": ts, "condition_id": cid, "model_id": model,
                              "p_yes": 0.6, "p_market_at_ts": 0.5})


def test_the_check_filters_do_what_their_addenda_say():
    from lab.eval.run import _before_end_date, _first_per_market_day, _since

    rows = [
        {"condition_id": "a", "forecast_ts": "2026-08-20T09:00:00+00:00", "end_date_iso": None},
        {"condition_id": "a", "forecast_ts": "2026-08-20T02:00:00+00:00", "end_date_iso": None},
        {"condition_id": "a", "forecast_ts": "2026-08-21T02:00:00+00:00", "end_date_iso": None},
    ]
    kept = _first_per_market_day(rows)
    assert [r["forecast_ts"][:13] for r in kept] == ["2026-08-20T02", "2026-08-21T02"], "9.5"

    late = [{"condition_id": "k", "forecast_ts": "2026-08-12T02:00:00+00:00",
             "end_date_iso": "2026-08-10T00:00:00Z"},
            {"condition_id": "k2", "forecast_ts": "2026-08-12T02:00:00+00:00",
             "end_date_iso": "2026-09-10T00:00:00Z"}]
    assert [r["condition_id"] for r in _before_end_date(late)] == ["k2"], "9.11"

    assert [r["forecast_ts"][:10] for r in _since("2026-08-21")(rows)] == ["2026-08-21"]


def test_every_check_writes_only_under_its_own_label(config):
    """Alongside, never replacing: a robustness pass must not write a single
    primary-labelled row, and the model-scoped checks touch only their models."""
    from lab.eval.run import ROBUSTNESS_CHECKS, run_robustness_checks

    conn = db.connect(config["storage"]["db_path"])
    _mk(conn, "p:plain")
    _mk(conn, "p:neg", neg_risk=1)
    for cid in ("p:plain", "p:neg"):
        _fc(conn, cid, "2026-09-10T02:00:00+00:00")
        _fc(conn, cid, "2026-09-10T02:00:00+00:00", model="m7_crossvenue")
        db.record_resolution(conn, cid, "2026-09-12T00:00:00+00:00", 1.0, False, "gamma")
    conn.commit()

    done = run_robustness_checks(conn, config)
    assert set(done) == set(ROBUSTNESS_CHECKS)
    labels = [(r["model_id"], r["window_label"]) for r in conn.execute(
        "SELECT model_id, window_label FROM eval_runs")]
    suffixes = {spec.get("suffix", "") for spec in ROBUSTNESS_CHECKS.values()} - {""}
    assert all(l.endswith(tuple(suffixes)) or "_disputed_inclusive" in l for _, l in labels), (
        "a robustness pass wrote a primary-labelled row")
    assert any(l.endswith("_negrisk") for _, l in labels)
    assert not any(m == "m7_crossvenue" and l.endswith(("_negrisk", "_non_negrisk"))
                   for m, l in labels), "9.3(a) is scoped to the M1 family"
    assert not any(m != "m7_crossvenue" and l.endswith("_since_20260822")
                   for m, l in labels), "9.22 is scoped to M7"
    conn.close()


def test_an_unknown_check_is_an_error_not_a_silent_skip(config):
    from lab.eval.run import run_robustness_checks

    conn = db.connect(config["storage"]["db_path"])
    with pytest.raises(ValueError):
        run_robustness_checks(conn, config, names=["no_such_check"])
    conn.close()


def test_the_confidence_sequence_does_not_depend_on_row_order():
    """The CS is the sole confirmatory statistic. Its input sequence must be a
    function of the data, not of the order SQLite happens to return rows in --
    ties in resolved_ts used to be broken by an unstable sort over an
    unordered query."""
    import numpy as np

    from lab.eval.anytime import confidence_sequence
    from lab.eval.run import _per_cluster_diffs_in_resolution_order

    rng = np.random.default_rng(3)
    n = 400
    clusters = np.array([f"e{i % 90}" for i in range(n)])
    # heavy ties: only 12 distinct resolution seconds
    resolved = [f"2026-09-{10 + (i % 12):02d}T02:00:00+00:00" for i in range(n)]
    diffs = rng.normal(0.0, 0.1, n)

    base = _per_cluster_diffs_in_resolution_order(diffs, clusters, resolved)
    ref = confidence_sequence(base)
    for seed in range(5):
        perm = np.random.default_rng(seed).permutation(n)
        got = _per_cluster_diffs_in_resolution_order(
            diffs[perm], clusters[perm], [resolved[i] for i in perm])
        np.testing.assert_allclose(got, base)
        cs = confidence_sequence(got)
        assert (cs.lo, cs.hi) == pytest.approx((ref.lo, ref.hi))
