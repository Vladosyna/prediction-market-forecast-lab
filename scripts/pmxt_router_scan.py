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
auto-confirmed; a human still runs `lab map confirm`. Every 50 lookups, new
candidates are appended to whatever the file holds at that moment, and the
verify job removes only the candidates it got through, so a scan and a verify
running at the same time (both are scheduled at 17:00) never erase each
other's work.

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

pmxt's hosted API leaves a large share of these requests unanswered: on
2026-09-30, 10 of 20 random lookups gave nothing in 20 s while the rest
answered in about 1.5 s, and slugs that had hung answered at once when asked
again. Its generated REST layer hands urllib3 `timeout=None` -- wait forever --
unless a call brings its own timeout, which the Router's methods never do, so
socket.setdefaulttimeout() cannot bound them either (tried that day). Every
client here is given a real one (_bounded): an unanswered request is closed
and urllib3 asks again. Lookups run a few at a time, new ones start at most
once a second, and a run stops when nearly every recent lookup has failed.
Only one scan runs at a time; a second exits rather than race the first over
the same files.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
LOCK_PATH = REPO_ROOT / "data" / "pmxt_scan.lock"

# Markets per timed run (twice a day): a full pass over ~2,100 usable markets
# in under four days.
DEFAULT_BATCH = 300
# Per HTTP request. Answers came in ~1.5 s (8.4 s at the slowest seen); a
# request that times out is asked again by urllib3, up to its default 3 retries.
LOOKUP_TIMEOUT_S = 15
WORKERS = 3
START_INTERVAL_S = 1.0          # at most one new lookup a second
FAIL_WINDOW, FAIL_STOP_SHARE = 30, 0.9
_END = object()


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


def _write_json(path: Path, value, **dump_kwargs) -> None:
    """Whole or not at all: the verify job reads the candidates file while a
    scan runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, **dump_kwargs), encoding="utf-8")
    tmp.replace(path)


def _save(new_candidates: list[dict], state: dict, candidates_path: Path, state_path: Path) -> int:
    """Append this run's unsaved candidates to what the candidates file holds
    NOW -- the verify job consumes it at 17:00 and 21:00, often mid-scan -- then
    write the lookup stamps. In that order, a run killed between the two asks
    those markets again rather than losing their candidates. Returns how many
    were appended."""
    current = _load_json(candidates_path, [])
    have = {(c.get("poly_condition_id"), c.get("kalshi_ticker")) for c in current}
    added = [c for c in new_candidates if (c["poly_condition_id"], c["kalshi_ticker"]) not in have]
    _write_json(candidates_path, current + added, indent=2)
    _write_json(state_path, state, sort_keys=True)
    return len(added)


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


def _bounded(router, timeout_s: float = LOOKUP_TIMEOUT_S):
    """`router` with a timeout on every HTTP request it makes. pmxt's generated
    ApiClient.call_api takes one (`_request_timeout`) that the Router never
    passes; a pmxt release without it fails here instead of hanging."""
    api_client = router._api_client
    call_api = api_client.call_api
    if "_request_timeout" not in inspect.signature(call_api).parameters:
        raise RuntimeError("pmxt's ApiClient.call_api no longer takes _request_timeout -- "
                           "the scan cannot bound its requests")

    def call_api_bounded(*args, **kwargs):
        kwargs.setdefault("_request_timeout", timeout_s)
        return call_api(*args, **kwargs)

    api_client.call_api = call_api_bounded
    return router


def _run_lookups(jobs, lookup, workers: int = WORKERS, start_interval_s: float = START_INTERVAL_S,
                 clock=time.monotonic, sleep=time.sleep):
    """Yield (job, result, error) for each job, at most `workers` lookups in
    flight and a new one started at most once per start_interval_s. Each lookup
    bounds itself (_bounded); none is abandoned. Stops starting lookups once
    FAIL_STOP_SHARE of the last FAIL_WINDOW failed -- pmxt is then down, and
    stamping the rest as looked up would send them to the back unasked."""
    jobs = iter(jobs)
    recent: list[bool] = []
    last_start = None
    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        running: dict = {}
        while True:
            while not stop and len(running) < workers:
                job = next(jobs, _END)
                if job is _END:
                    stop = True
                    break
                if last_start is not None:
                    sleep(max(0.0, start_interval_s - (clock() - last_start)))
                last_start = clock()
                running[pool.submit(lookup, job)] = job
            if not running:
                return
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                job, error = running.pop(future), future.exception()
                recent = (recent + [error is not None])[-FAIL_WINDOW:]
                yield job, None if error else future.result(), error
            if not stop and len(recent) == FAIL_WINDOW and sum(recent) >= FAIL_STOP_SHARE * FAIL_WINDOW:
                print(f"{sum(recent)} of the last {FAIL_WINDOW} lookups failed -- pmxt looks down, stopping")
                stop = True


def _lock_or_none(path: Path):
    """An open, exclusively locked handle held for the run, or None when another
    scan holds it: two scans at once (the 17:00 timer beside a manual --full)
    would each rewrite the candidate and lookup-state files from their own
    start-of-run copy. Windows has no fcntl, and no timer runs the scan there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a")
    try:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except BlockingIOError:
        handle.close()
        return None
    return handle


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
    lock = _lock_or_none(LOCK_PATH)
    if lock is None:
        print("another pmxt scan is running -- exiting")
        return

    import pmxt  # deliberately the only import site in this repo -- see module docstring

    _bounded(pmxt.Router(pmxt_api_key=api_key))  # a pmxt that cannot be bounded stops the run here
    config = load_config()
    known_pairs, paired_markets = _known_pairs()
    usable = _usable_polymarket_markets(config)
    state: dict[str, str] = _load_json(LOOKUP_STATE_PATH, {})
    order = _lookup_order(usable, paired_markets, state)
    batch = order if args.full else order[:max(0, args.batch)]
    print(f"usable Polymarket markets {len(usable)}, already paired {len(paired_markets & set(usable))}, "
          f"to look up {len(order)}, this run {len(batch)}")

    have = known_pairs | {(c.get("poly_condition_id"), c.get("kalshi_ticker"))
                          for c in _load_json(OUTPUT_PATH, [])}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    looked = failures = matched_markets = appended = 0
    unsaved: list[dict] = []

    def lookup(cid):
        # A client per lookup: pmxt does not say its client is thread-safe.
        router = _bounded(pmxt.Router(pmxt_api_key=api_key))
        return router.fetch_matched_market_clusters(
            slug=usable[cid], relation="identity", venues="polymarket,kalshi")

    for cid, clusters, error in _run_lookups(batch, lookup):
        state[cid] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        looked += 1
        if error is not None:
            failures += 1
            if failures <= 5:
                print(f"{cid[:12]} {usable[cid][:50]}: {type(error).__name__}: {str(error)[:160]}")
        else:
            found = False
            for cluster in clusters or []:
                cand = _candidate(cluster, now_iso)
                if cand is None or cand["poly_condition_id"] != cid:
                    continue
                key = (cand["poly_condition_id"], cand["kalshi_ticker"])
                if key in have:
                    continue
                have.add(key)
                unsaved.append(cand)
                found = True
            matched_markets += found
        if looked % 50 == 0:
            appended += _save(unsaved, state, OUTPUT_PATH, LOOKUP_STATE_PATH)
            unsaved.clear()
            print(f"{looked} looked up, {failures} failed, {matched_markets} matched")

    appended += _save(unsaved, state, OUTPUT_PATH, LOOKUP_STATE_PATH)
    print(f"looked up {looked} market(s), {failures} failed, {matched_markets} with a new Kalshi match; "
          f"{appended} new candidate(s) written to {OUTPUT_PATH.name}")


if __name__ == "__main__":
    main()
