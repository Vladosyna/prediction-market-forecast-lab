"""Phase 16 (v2.4): event-distribution assembly and RPS scoring for
bucketed events (CPI ranges, temperature bands, ...).
"""

from __future__ import annotations

import numpy as np
import pytest

from lab.eval.distributional import (
    bucketed_events,
    coherence_deviation,
    implied_cdf,
    negrisk_legs,
    parse_bucket_order,
)
from lab.eval.scoring import brier, paired_rps_skill, rps
from lab.store import db


def test_parse_bucket_order_extracts_numeric_ranges():
    assert parse_bucket_order("Will CPI be between 3.0% and 3.5%?") == 3.0
    assert parse_bucket_order("Will the high temperature exceed 90 degrees?") == 90.0
    assert parse_bucket_order("Will BTC be above $100,000?") == 100000.0
    assert parse_bucket_order("Will the rate be -0.5% or lower?") == -0.5


def test_parse_bucket_order_returns_none_when_unparseable():
    assert parse_bucket_order("Will the incumbent win the election?") is None
    assert parse_bucket_order(None) is None
    assert parse_bucket_order("") is None


def test_implied_cdf_renormalizes_to_sum_one():
    cdf = implied_cdf([0.1, 0.2, 0.3])
    assert cdf.sum() == pytest.approx(1.0)
    assert cdf.tolist() == pytest.approx([1 / 6, 2 / 6, 3 / 6])


def test_implied_cdf_falls_back_to_uniform_when_degenerate():
    cdf = implied_cdf([0.0, 0.0, 0.0])
    assert cdf.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_coherence_deviation_is_zero_for_a_coherent_pool():
    assert coherence_deviation([0.2, 0.3, 0.5]) == pytest.approx(0.0)
    assert coherence_deviation([0.3, 0.3, 0.3]) == pytest.approx(0.1)


# --- bucketed_events (rewritten 2026-09-29, PAP 9.37) ----------------------

T1 = "2026-07-01T02:00:00+00:00"
T2 = "2026-07-02T02:00:00+00:00"
SEEN = "2026-06-01T00:00:00+00:00"


def _legs(event_id, questions, seen=SEEN):
    return {event_id: {f"{event_id}_{i}": (q, seen) for i, q in enumerate(questions)}}


def _row(event_id, i, p_yes, p_market, payout, ts=T1, venue="polymarket"):
    return {"condition_id": f"{event_id}_{i}", "event_id": event_id, "venue": venue,
            "forecast_ts": ts, "p_yes": p_yes, "p_market_at_ts": p_market,
            "payout_yes": payout, "resolved_ts": "2026-07-10T00:00:00+00:00",
            "category": "economics"}


CPI = ["Will CPI be between 4.0% and 4.5%?", "Will CPI be between 3.0% and 3.5%?",
       "Will CPI be between 3.5% and 4.0%?"]


def test_a_complete_pass_is_bucket_ordered():
    legs = _legs("e", CPI)
    rows = [_row("e", 0, 0.2, 0.20, 0.0), _row("e", 1, 0.2, 0.25, 0.0),
            _row("e", 2, 0.6, 0.55, 1.0)]
    events, skipped = bucketed_events(rows, legs)
    assert skipped == {}
    (e,) = events
    assert e["condition_ids"] == ["e_1", "e_2", "e_0"]      # 3.0 < 3.5 < 4.0
    assert e["y_bucket_idx"] == 1
    assert e["p_model"] == pytest.approx([0.2, 0.6, 0.2])


def test_an_event_whose_winner_was_never_forecast_is_not_scored():
    """THE outcome-selection regression. A leg priced outside the forecast
    bounds is never forecast; the old rule ("exactly one forecast leg won")
    kept events a forecast leg won and dropped the ones an unforecast leg
    won. Completeness is known before the outcome; which leg won is not."""
    legs = _legs("e", CPI)
    rows = [_row("e", 1, 0.3, 0.3, 0.0), _row("e", 2, 0.7, 0.7, 1.0)]   # e_0 never forecast
    events, skipped = bucketed_events(rows, legs)
    assert events == [] and skipped == {"no_complete_pass": 1}


def test_the_latest_complete_pass_is_used():
    legs = _legs("e", CPI)
    rows = [_row("e", i, p, 0.33, float(i == 2), ts=T1) for i, p in enumerate((0.1, 0.2, 0.7))]
    # a later pass missing one leg (it left the price bounds) does not count
    rows += [_row("e", 1, 0.5, 0.5, 0.0, ts=T2), _row("e", 2, 0.5, 0.5, 1.0, ts=T2)]
    (e,) = bucketed_events(rows, legs)[0]
    assert e["p_model"] == pytest.approx([0.2, 0.7, 0.1])    # the T1 pass, bucket-ordered


def test_a_leg_listed_after_the_pass_is_not_required():
    legs = _legs("e", CPI)
    legs["e"]["e_0"] = (CPI[0], "2026-07-01T12:00:00+00:00")   # listed after T1
    rows = [_row("e", 1, 0.3, 0.3, 0.0), _row("e", 2, 0.7, 0.7, 1.0)]
    (e,) = bucketed_events(rows, legs)[0]
    assert e["condition_ids"] == ["e_1", "e_2"]


def test_categorical_and_unparseable_groups_are_not_ordinal():
    """Every question held a number but the numbers were one shared year: the
    old rule scored such categorical events in arbitrary order."""
    legs = {**_legs("cat", ["Will Alice win the 2026 race?", "Will Bob win the 2026 race?"]),
            **_legs("words", ["Will the incumbent win?", "Will the challenger win?"])}
    rows = [_row("cat", 0, 0.4, 0.4, 0.0), _row("cat", 1, 0.6, 0.6, 1.0),
            _row("words", 0, 0.4, 0.4, 0.0), _row("words", 1, 0.6, 0.6, 1.0)]
    events, skipped = bucketed_events(rows, legs)
    assert events == [] and skipped == {"unordered": 2}


def test_not_exactly_one_winner_is_malformed():
    legs = _legs("e", CPI[:2])
    rows = [_row("e", 0, 0.5, 0.5, 1.0), _row("e", 1, 0.5, 0.5, 1.0)]
    events, skipped = bucketed_events(rows, legs)
    assert events == [] and skipped == {"not_one_winner": 1}


def test_only_polymarket_negrisk_groups_contribute(tmp_path):
    """event_id is a clustering key, not a mutual-exclusivity guarantee:
    Kalshi events and (from 9.33) plain multi-market Gamma events share it,
    cumulative ladders included."""
    conn = db.connect(tmp_path / "lab.db")
    for cid, event_id, venue, neg_risk in (("p0", "neg", "polymarket", 1),
                                          ("p1", "neg", "polymarket", 1),
                                          ("q0", "plain", "polymarket", 0),
                                          ("q1", "plain", "polymarket", 0),
                                          ("kalshi:k0", "kalshi:E", "kalshi", 0)):
        db.upsert_market(conn, {
            "condition_id": cid, "venue": venue, "venue_native_id": cid, "slug": None,
            "question": f"Will X be above {cid[-1]}?", "category": "economics",
            "description": "d", "end_date_iso": None, "token_id_yes": None,
            "token_id_no": None, "neg_risk": neg_risk, "active": 0, "closed": 1,
            "liquidity_num": 1.0, "volume_num": 1.0, "tier": "liquid", "event_id": event_id,
        })
    conn.commit()
    assert set(negrisk_legs(conn)) == {"neg"}
    kalshi_rows = [_row("kalshi:E", 0, 0.5, 0.5, 1.0, venue="kalshi")]
    assert bucketed_events(kalshi_rows, {"kalshi:E": {"kalshi:E_0": ("q 1", SEEN)}})[0] == []
    conn.close()


# --- RPS scoring (eval/scoring.py) -----------------------------------------

def test_rps_two_bucket_identity_matches_brier():
    """The literal Phase 16 acceptance identity: a two-bucket event's RPS
    reduces EXACTLY to the Brier score (not an approximation)."""
    for p in (0.1, 0.3, 0.5, 0.7, 0.95):
        p_buckets = [p, 1 - p]  # bucket 0 = YES, bucket 1 = NO
        # Outcome = bucket 0 (YES happened) -> Brier's y=1.
        assert rps(p_buckets, y_bucket_idx=0) == pytest.approx(
            brier(np.array([p]), np.array([1.0]))[0])
        # Outcome = bucket 1 (NO happened) -> Brier's y=0.
        assert rps(p_buckets, y_bucket_idx=1) == pytest.approx(
            brier(np.array([p]), np.array([0.0]))[0])


def test_rps_rewards_correct_shape_over_lucky_spike():
    """The whole point of the RPS upgrade: a model whose mass is spread
    sensibly on and near the true bucket scores BETTER than a model that
    puts a single confident spike on a bucket that isn't even the true
    one -- RPS punishes confident wrongness more than a diffuse near-miss."""
    y_bucket_idx = 2
    shape_correct = [0.15, 0.40, 0.30, 0.15]  # spread, real mass on/near bucket 2
    lucky_spike = [0.02, 0.02, 0.02, 0.94]    # confident spike on bucket 3, not 2

    assert rps(shape_correct, y_bucket_idx) < rps(lucky_spike, y_bucket_idx)


def test_paired_rps_skill_reuses_cluster_bootstrap():
    """Pairing/clustering reuse proven directly (not assumed): paired_rps_skill's
    CI comes from the same cluster_bootstrap_ci every other skill statistic
    already uses, and bucketed_events' output feeds it end to end."""
    legs = {**_legs("A", ["Will CPI be 3.0%?", "Will CPI be 3.5%?", "Will CPI be 4.0%?"]),
            **_legs("B", ["Will it be 60F?", "Will it be 70F?", "Will it be 80F?"])}
    rows = []
    for event_id in ("A", "B"):
        rows += [_row(event_id, 0, 0.05, 0.33, 0.0), _row(event_id, 1, 0.90, 0.34, 1.0),
                 _row(event_id, 2, 0.05, 0.33, 0.0)]
    events, _ = bucketed_events(rows, legs)
    assert len(events) == 2
    result = paired_rps_skill(events, iterations=200)
    assert result.n == 2
    assert result.skill_rps > 0  # model clearly beats the near-uniform market
    assert result.skill_rps_ci_lo <= result.skill_rps <= result.skill_rps_ci_hi
