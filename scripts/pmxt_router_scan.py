"""Out-of-band pmxt Router scan for M7 cross-venue candidate matches.

NOT part of src/lab's runtime and NOT a pyproject.toml dependency. pmxt is a
unified prediction-market TRADING SDK (create_order/fetch_balance/
fetch_positions live alongside its read-only Router) whose hosted API key
can also authorize live trading -- Claude.md's tech-stack row / S12 says it
must never be imported into src/lab or run by the orchestrator. This script
is deliberately the ONLY place in this repo that imports pmxt, is run by its
OWN separate scheduled unit (pmxt-scan.timer on the VPS), and only ever calls
Router's read-only matching method -- never create_order, cancel_order, or
fetch_balance.

Run with:  uv run --with pmxt python scripts/pmxt_router_scan.py [--full | --batch N]
`uv run --with` installs pmxt into an ephemeral/cached environment for this
one invocation only -- pyproject.toml is never touched, so pmxt never
becomes part of this project's own declared dependency tree (the concern
Claude.md raises: "the scope guard greps our own src/, not installed
packages, so it wouldn't catch the drift").

Output: data/pmxt_candidates.json, a plain list of
{poly_condition_id, poly_question, kalshi_ticker, kalshi_title,
relation_type, confidence, scanned_ts}. This file is read-only input to
lab.models.m7_crossvenue.verify_pmxt_candidates, which is the only code
path that ever writes into data/markets_map.yaml -- nothing here is
auto-confirmed; a human still runs `lab map confirm`. New candidates are
added to whatever the file already holds, so a scan landing before the last
one's candidates were verified cannot erase them.

What it asks (2026-09-30): for each Polymarket market M7 could use -- open, in
a snapshotted tier, in a priority category -- that has no Kalshi pair yet,
pmxt's identity matches for that one market, by its slug. Until then the scan
ran ~20 fixed keyword searches at 20 clusters each behind an `updated_since`
watermark, so a cluster was seen once and never again: new confirmed pairs
fell from 123 in July to 21 in September while Kalshi's universe grew to 1,317
live series, and the 05:00 scan that day wrote nothing. A query-less crawl of
every identity cluster was tried the same afternoon and abandoned: pmxt served
the first page in 1.4 s and then held deeper pages until a server-side limit
(900 s) and an empty body. A lookup by slug answered in 0.8-2.2 s and returned
exactly the recorded Kalshi ticker for three known pairs.

Each market's last lookup is kept in data/pmxt_lookup_state.json, stamped
whether or not the lookup succeeded -- ordering on successes alone is how the
lab's resolution watchers and series rotation wedged, one after another. Never
looked-up markets go first, then the longest ago; a timed run takes a batch,
`--full` takes them all.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from lab.models.m7_crossvenue import load_markets_map  # noqa: E402
from lab.util import load_config  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "data" / "pmxt_candidates.json"
LOOKUP_STATE_PATH = REPO_ROOT / "data" / "pmxt_lookup_state.json"

# Markets per timed run (twice a day): a full pass over ~2,100 usable markets
# in under four days at ~3 s a lookup, ~15 minutes a run.
DEFAULT_BATCH = 300
# pmxt's Router takes no timeout and its client will wait out the server's own
# 900-second hold; a lookup gets this long on a worker thread, then is
# abandoned. A run stops after this many in a row, since the API is then down.
LOOKUP_TIMEOUT_S = 60
MAX_CONSECUTIVE_TIMEOUTS = 5
PACING_S = 1.5


def _known_pairs() -> tuple[set[tuple[str, str]], set[str]]:
    """((condition_id, external_id) pairs, condition_ids) already confirmed or
    proposed with Kalshi -- from ANY source, not just pmxt. A market that has
    one is not looked up again, and a pair already listed is never re-proposed."""
    data = load_markets_map()
    entries = [e for e in data.get("confirmed", []) + data.get("proposed", [])
               if e.get("venue") == "kalshi"]
    return ({(e["condition_id"], e["external_id"]) for e in entries},
            {e["condition_id"] for e in entries})


def _usable_polymarket_markets(config: dict) -> dict[str, str]:
    """{condition_id: slug} for Polymarket markets M7 can forecast: open, in a
    snapshotted tier, in a priority category (Phase 9: "priority categories
    only"). Read-only."""
    priority = set(config["universe"]["priority_categories"])
    db_path = REPO_ROOT / config["storage"]["db_path"]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {cid: slug for cid, slug, category in conn.execute(
            "SELECT condition_id, slug, category FROM markets "
            "WHERE COALESCE(venue, 'polymarket') = 'polymarket' AND active = 1 AND closed = 0 "
            "AND tier IN ('liquid', 'tail') AND slug IS NOT NULL") if category in priority}
    finally:
        conn.close()


def _lookup_order(usable: dict[str, str], paired: set[str], state: dict[str, str]) -> list[str]:
    """Condition ids to look up, never-looked-up first, then oldest lookup."""
    todo = [cid for cid in usable if cid not in paired]
    return sorted(todo, key=lambda cid: (state.get(cid) is not None, state.get(cid) or "", cid))


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (json.JSONDecodeError, OSError):
        return default


def _attr(obj, *names, default=None):
    """First NON-NONE attribute across possible pmxt schema spellings.

    Confirmed live: pmxt's UnifiedMarket is a pydantic-style model where every
    declared field always "exists" (hasattr is True) even when the venue
    didn't populate it -- e.g. contract_address is a real declared attribute
    on a Kalshi-origin object, just set to None. An earlier hasattr-based
    version of this helper stopped at the first candidate name that merely
    EXISTED, never falling through to a later name when the value was
    present-but-None -- which is exactly why kalshi_ticker kept resolving to
    None instead of falling through to a working field.
    """
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return val
    return default


def _dump(obj) -> str:
    """Best-effort raw repr for diagnosing an unknown pmxt object shape."""
    if hasattr(obj, "__dict__"):
        return repr(vars(obj))
    if hasattr(obj, "_asdict"):
        return repr(obj._asdict())
    if hasattr(obj, "model_dump"):  # pydantic v2
        return repr(obj.model_dump())
    return repr(obj)


def _with_timeout(fn, timeout_s: float):
    """(finished, result, error) for fn() run on a daemon thread; a call still
    running after timeout_s is abandoned rather than waited for."""
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 -- reported to the caller
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return False, None, None
    return True, box.get("result"), box.get("error")


def _candidate(cluster, now_iso: str) -> dict | None:
    """The Polymarket<->Kalshi pair in one identity cluster, as a candidate row."""
    confidence = _attr(cluster, "confidence", "score", default=0.0)
    cluster_markets = _attr(cluster, "markets", default=[]) or []
    poly = next((m for m in cluster_markets
                 if (_attr(m, "source_exchange", "venue", default="") or "").lower() == "polymarket"),
                None)
    kalshi = next((m for m in cluster_markets
                   if (_attr(m, "source_exchange", "venue", default="") or "").lower() == "kalshi"),
                  None)
    if poly is None or kalshi is None:
        return None
    # Both confirmed from a real run's raw dump: contract_address is
    # Polymarket's conditionId; Kalshi objects leave contract_address None but
    # carry the venue ticker in `slug` (Polymarket's own `slug` is a URL slug).
    poly_condition_id = _attr(poly, "contract_address", "market_id")
    poly_question = _attr(poly, "title", "question", "name")
    kalshi_ticker = _attr(kalshi, "slug", "contract_address", "market_id")
    if poly_condition_id is None or poly_question is None or kalshi_ticker is None:
        print(f"pmxt schema mismatch on a cluster market object: "
              f"poly={_dump(poly)} kalshi={_dump(kalshi)}")
        return None
    # Kalshi's market-level `title` is the broad EVENT question ("Who will win
    # the next presidential election?"); the outcome it resolves on lives in
    # the outcome labels. Sending the title alone made the downstream LLM check
    # reject every candidate it saw -- correctly, on that evidence (2026-07-28).
    kalshi_outcomes = [lbl for lbl in (_attr(o, "label", "name", "title")
                                       for o in (_attr(kalshi, "outcomes", default=[]) or []))
                       if lbl]
    return {
        "poly_condition_id": str(poly_condition_id),
        "poly_question": poly_question,
        "kalshi_ticker": str(kalshi_ticker),
        "kalshi_title": _attr(kalshi, "title", "question", "name", default=""),
        "kalshi_outcomes": kalshi_outcomes,
        "kalshi_description": _attr(kalshi, "description", default=None),
        "relation_type": "identity",
        "confidence": float(confidence),
        "scanned_ts": now_iso,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="look up every usable market")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help="markets to look up this run (ignored with --full)")
    args = parser.parse_args()
    api_key = os.environ.get("PMXT_API_KEY", "").strip()
    if not api_key:
        print("PMXT_API_KEY not set in .env -- nothing to do.")
        return

    import pmxt  # deliberately the only import site in this repo -- see module docstring

    config = load_config()
    router = pmxt.Router(pmxt_api_key=api_key)
    known_pairs, paired_markets = _known_pairs()
    usable = _usable_polymarket_markets(config)
    state: dict[str, str] = _load_json(LOOKUP_STATE_PATH, {})
    order = _lookup_order(usable, paired_markets, state)
    batch = order if args.full else order[:max(0, args.batch)]
    print(f"usable Polymarket markets {len(usable)}, already paired {len(paired_markets & set(usable))}, "
          f"to look up {len(order)}, this run {len(batch)}")

    existing = _load_json(OUTPUT_PATH, [])
    have = {(c["poly_condition_id"], c["kalshi_ticker"]) for c in existing}
    candidates = list(existing)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    looked = timeouts_in_row = failures = matched_markets = 0

    for i, cid in enumerate(batch):
        if i:
            time.sleep(PACING_S)   # polite pacing (brief guardrail 4, extended to pmxt's API)
        finished, clusters, error = _with_timeout(
            lambda slug=usable[cid]: router.fetch_matched_market_clusters(
                slug=slug, relation="identity", venues="polymarket,kalshi"),
            LOOKUP_TIMEOUT_S)
        state[cid] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        looked += 1
        if not finished:
            timeouts_in_row += 1
            failures += 1
            print(f"{cid[:12]}: no answer in {LOOKUP_TIMEOUT_S} s")
            if timeouts_in_row >= MAX_CONSECUTIVE_TIMEOUTS:
                print(f"{timeouts_in_row} lookups in a row timed out -- pmxt looks down, stopping")
                break
            continue
        timeouts_in_row = 0
        if error is not None:
            failures += 1
            print(f"{cid[:12]}: {type(error).__name__}: {str(error)[:80]}")
            continue
        found = False
        for cluster in clusters or []:
            cand = _candidate(cluster, now_iso)
            if cand is None or cand["poly_condition_id"] != cid:
                continue
            key = (cand["poly_condition_id"], cand["kalshi_ticker"])
            if key in known_pairs or key in have:
                continue
            have.add(key)
            candidates.append(cand)
            found = True
        matched_markets += found
        if looked % 50 == 0:
            LOOKUP_STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    LOOKUP_STATE_PATH.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"looked up {looked} market(s), {failures} failed, {matched_markets} with a new Kalshi match; "
          f"{len(candidates) - len(existing)} new candidate(s), {len(candidates)} in {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
