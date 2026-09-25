"""Resolution finality rules: record final payout, never a first report."""

from __future__ import annotations

from lab.api.gamma import GammaMarket
from lab.collect.resolutions import extract_final_payout


def _market(**kwargs) -> GammaMarket:
    base = {
        "conditionId": "0x1",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "closed": True,
        "outcomePrices": '["1", "0"]',
    }
    base.update(kwargs)
    return GammaMarket.model_validate(base)


def test_final_yes_payout():
    assert extract_final_payout(_market()) == (1.0, False)
    assert extract_final_payout(_market(outcomePrices='["0", "1"]')) == (0.0, False)


def test_not_final_while_open_or_unsettled():
    assert extract_final_payout(_market(closed=False)) is None
    # Price hasn't collapsed to 0/1 -> not a final payout.
    assert extract_final_payout(_market(outcomePrices='["0.97", "0.03"]')) is None


def test_open_dispute_blocks_recording():
    disputed_open = _market(umaResolutionStatuses='["disputed"]')
    assert extract_final_payout(disputed_open) is None

    disputed_final = _market(umaResolutionStatuses='["disputed", "resolved"]')
    assert extract_final_payout(disputed_final) == (1.0, True)


# --- watcher scan ordering (2026-07-25 stall) -------------------------------

import asyncio

import pytest

from lab.collect.resolutions import (
    resolution_backlog_size,
    unresolved_closed_markets,
    watch_resolutions,
)
from lab.store import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lab.db")
    yield c
    c.close()


def _add_market(conn, cid, closed=1, end="2026-01-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO markets (condition_id, category, tier, active, closed, end_date_iso) "
        "VALUES (?, 'politics', 'liquid', 0, ?, ?)", (cid, closed, end))


class _NeverFinalGamma:
    """Gamma that reports every market as closed but never with a final
    payout -- the exact population that had wedged the head of the scan."""

    def __init__(self):
        self.fetched = []

    async def market_by_condition(self, condition_id):
        self.fetched.append(condition_id)
        return None


def test_scan_advances_instead_of_rescanning_the_same_head(conn):
    """THE regression test. Before the fix the query had no ORDER BY, so every
    cycle returned the same `limit` rows in scan order; markets Gamma never
    finalizes sat permanently at the head and nothing behind them was ever
    reached."""
    for i in range(10):
        _add_market(conn, f"0x{i}")
    conn.commit()

    gamma = _NeverFinalGamma()
    asyncio.run(watch_resolutions(gamma, conn, limit=3))
    first = list(gamma.fetched)

    gamma2 = _NeverFinalGamma()
    asyncio.run(watch_resolutions(gamma2, conn, limit=3))
    second = list(gamma2.fetched)

    assert len(first) == 3 and len(second) == 3
    assert not set(first) & set(second), (
        f"watcher re-scanned the same markets: {first} then {second}"
    )


def test_whole_backlog_is_swept_not_just_the_head(conn):
    """Repeated cycles must eventually reach every candidate -- the property
    that actually keeps the paper's scored sample unbiased."""
    for i in range(9):
        _add_market(conn, f"0x{i}")
    conn.commit()

    seen = set()
    for _ in range(3):
        gamma = _NeverFinalGamma()
        asyncio.run(watch_resolutions(gamma, conn, limit=3))
        seen.update(gamma.fetched)
    assert len(seen) == 9, f"only reached {len(seen)}/9 markets"


def test_failed_fetch_still_rotates_to_the_back(conn):
    """A market that always errors must not re-wedge the head -- stamping only
    on success would rebuild the bug with a different population."""
    class _AlwaysFails:
        def __init__(self):
            self.fetched = []

        async def market_by_condition(self, condition_id):
            self.fetched.append(condition_id)
            raise RuntimeError("gamma down")

    for i in range(6):
        _add_market(conn, f"0x{i}")
    conn.commit()

    g1 = _AlwaysFails()
    asyncio.run(watch_resolutions(g1, conn, limit=2))
    g2 = _AlwaysFails()
    asyncio.run(watch_resolutions(g2, conn, limit=2))
    assert not set(g1.fetched) & set(g2.fetched)


def test_fresh_closure_jumps_ahead_of_the_old_backlog(conn):
    """NULL resolution_checked_ts sorts first, so a newly-closed market is
    picked up on the next cycle rather than waiting out a full sweep."""
    for i in range(5):
        _add_market(conn, f"old{i}")
    conn.commit()
    asyncio.run(watch_resolutions(_NeverFinalGamma(), conn, limit=5))  # stamp them all

    _add_market(conn, "brand_new")
    conn.commit()

    gamma = _NeverFinalGamma()
    asyncio.run(watch_resolutions(gamma, conn, limit=1))
    assert gamma.fetched == ["brand_new"]


def test_backlog_size_counts_the_watchers_real_working_set(conn):
    """`lab status` reported only the closed=1 half (17k of a real 42k), which
    is why the stall stayed invisible. This counts what the watcher selects."""
    _add_market(conn, "closed_one", closed=1)
    _add_market(conn, "past_end_not_closed", closed=0, end="2020-01-01T00:00:00+00:00")
    _add_market(conn, "still_running", closed=0, end="2099-01-01T00:00:00+00:00")
    conn.commit()

    assert resolution_backlog_size(conn) == 2
    assert set(unresolved_closed_markets(conn, limit=10)) == {
        "closed_one", "past_end_not_closed"}


# --- venue resolution time (2026-09-25) ---------------------------------------

def test_venue_timestamps_in_every_observed_format_normalise_to_utc():
    from lab.util import parse_venue_ts

    assert parse_venue_ts("2026-09-14T13:35:38.40392Z") == "2026-09-14T13:35:38+00:00"
    assert parse_venue_ts("2026-09-25 06:26:15+00") == "2026-09-25T06:26:15+00:00"
    assert parse_venue_ts("2026-09-25 06:35:18.320219+00") == "2026-09-25T06:35:18+00:00"
    assert parse_venue_ts("2026-09-25 06:35:18.320219+00:00:00") == "2026-09-25T06:35:18+00:00"
    assert parse_venue_ts("2026-09-25T06:35:18+00:00") == "2026-09-25T06:35:18+00:00"
    # a non-UTC offset has never been seen; refuse it rather than shift it
    assert parse_venue_ts("2026-09-25 06:35:18+05") is None
    assert parse_venue_ts("") is None and parse_venue_ts(None) is None
    assert parse_venue_ts("not a time") is None


def test_the_venue_time_is_recorded_and_never_erased_by_a_replay(tmp_path):
    from lab.store import db as dbm

    conn = dbm.connect(tmp_path / "lab.db")
    conn.execute("INSERT INTO markets (condition_id, question) VALUES ('c', 'q')")
    dbm.record_resolution(conn, "c", "2026-09-25T07:00:00+00:00", 1.0, False, "gamma",
                          venue_resolved_ts="2026-09-20T12:00:00+00:00")
    dbm.record_resolution(conn, "c", "2026-09-25T07:30:00+00:00", 1.0, False, "gamma")
    row = conn.execute("SELECT resolved_ts, venue_resolved_ts FROM resolutions").fetchone()
    assert row["venue_resolved_ts"] == "2026-09-20T12:00:00+00:00"
    assert row["resolved_ts"] == "2026-09-25T07:30:00+00:00"
    conn.close()
