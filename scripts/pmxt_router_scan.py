"""Out-of-band pmxt Router scan for M7 cross-venue candidate matches.

NOT part of src/lab's runtime and NOT a pyproject.toml dependency. pmxt is a
unified prediction-market TRADING SDK (create_order/fetch_balance/
fetch_positions live alongside its read-only Router) whose hosted API key
can also authorize live trading -- Claude.md's tech-stack row / S12 says it
must never be imported into src/lab or run by the orchestrator. This script
is deliberately the ONLY place in this repo that imports pmxt, is run by its
OWN separate Windows Scheduled Task (see install-pmxt-scan-task.ps1), and
only ever calls Router's read-only search/matching methods -- never
create_order, cancel_order, or fetch_balance.

Run with:  uv run --with pmxt python scripts/pmxt_router_scan.py [--full]
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
auto-confirmed; a human still runs `lab map confirm`.

What it reads (2026-09-30): every Polymarket<->Kalshi "identity" cluster pmxt
holds, page by page, rather than the ~20 short keyword searches it used to
run at 20 clusters each. Those searches were a workaround for pmxt ignoring
descriptive phrases; with the watermark below they also meant a cluster was
seen once and never again, and new confirmed pairs fell from 123 in July to
21 in September while Kalshi's universe grew from ~285 to 1,317 live series.
Only pairs whose Polymarket leg is in this lab's own open, snapshotted
universe in a priority category become candidates -- the markets M7 can
actually forecast; the rest would cost an LLM check and a human review for a
pair nothing could use.

Two independent ways this script avoids re-spending pmxt API calls (and
downstream LLM verification calls) on pairs already handled: (1)
data/pmxt_scan_state.json tracks the timestamp of the last successful scan
and passes it as `updated_since`, so pmxt itself only returns clusters it has
touched since then (`--full` ignores it for a one-off complete pass); (2)
candidates already present in data/markets_map.yaml's `confirmed` or
`proposed` lists are filtered out before being written to the output file at
all, regardless of what pmxt returns.

NOTE ON FIELD NAMES: pmxt's exact Router response schema (attribute names on
its Market/Cluster objects) was assembled from partial public docs and could
not be live-tested from the assistant session that wrote this script (the
same "run out-of-band, by a human" boundary this script exists to respect
also blocked testing it inline). On first real run, if you see a message
starting "pmxt schema mismatch", paste the printed raw object dump back for
a quick field-name fix -- the script is written to fail loud with that dump
rather than silently write wrong or empty candidates.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
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
STATE_PATH = REPO_ROOT / "data" / "pmxt_scan_state.json"


def _load_last_scan_ts() -> str | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("last_scan_ts")
    except (json.JSONDecodeError, OSError):
        return None


def _save_last_scan_ts(ts: str) -> None:
    STATE_PATH.write_text(json.dumps({"last_scan_ts": ts}), encoding="utf-8")


def _known_pairs() -> set[tuple[str, str]]:
    """(condition_id, external_id) pairs already confirmed or proposed --
    from ANY source, not just pmxt. Skipping these here saves an LLM
    verification call downstream and keeps the candidates file focused on
    genuinely new suggestions; updated_since (below) already does most of
    the work of not re-fetching unchanged pmxt matches in the first place."""
    data = load_markets_map()
    return {(e["condition_id"], e["external_id"])
           for e in data.get("confirmed", []) + data.get("proposed", [])
           if e.get("venue") == "kalshi"}


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


PAGE_SIZE = 100
# 10,000 clusters: a bound on a runaway crawl, far above the Polymarket<->Kalshi
# identity clusters pmxt returned on 2026-09-30.
MAX_PAGES = 100


def _usable_polymarket_ids(config: dict) -> set[str]:
    """Polymarket markets M7 can forecast: open, in a snapshotted tier, in a
    priority category (Phase 9: "priority categories only"). Read-only."""
    priority = set(config["universe"]["priority_categories"])
    db_path = REPO_ROOT / config["storage"]["db_path"]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {cid for cid, category in conn.execute(
            "SELECT condition_id, category FROM markets "
            "WHERE COALESCE(venue, 'polymarket') = 'polymarket' AND active = 1 AND closed = 0 "
            "AND tier IN ('liquid', 'tail')") if category in priority}
    finally:
        conn.close()


def _crawl(router, updated_since: str | None):
    """Every Polymarket<->Kalshi identity cluster, a page at a time.
    Yields clusters; returns True only if the crawl reached its end."""
    for page in range(MAX_PAGES):
        if page:
            # Polite pacing (brief guardrail 4, extended to pmxt's own API):
            # back-to-back calls produced empty-body failures in first-run
            # testing, consistent with a rate limit on pmxt's side.
            time.sleep(1.5)
        kwargs = {"relation": "identity", "venues": "polymarket,kalshi", "min_venues": 2,
                  "min_confidence": 0.5, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
        if updated_since:
            kwargs["updated_since"] = updated_since
        batch = router.fetch_matched_market_clusters(**kwargs)
        print(f"page {page}: {len(batch)} cluster(s)")
        yield from batch
        if len(batch) < PAGE_SIZE:
            return True
    print(f"stopped at MAX_PAGES={MAX_PAGES}; the next scan continues from the watermark")
    return False


def _dump(obj) -> str:
    """Best-effort raw repr for diagnosing an unknown pmxt object shape."""
    if hasattr(obj, "__dict__"):
        return repr(vars(obj))
    if hasattr(obj, "_asdict"):
        return repr(obj._asdict())
    if hasattr(obj, "model_dump"):  # pydantic v2
        return repr(obj.model_dump())
    return repr(obj)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="ignore the watermark: re-read every identity cluster pmxt holds")
    args = parser.parse_args()
    api_key = os.environ.get("PMXT_API_KEY", "").strip()
    if not api_key:
        print("PMXT_API_KEY not set in .env -- nothing to do.")
        return

    import pmxt  # deliberately the only import site in this repo -- see module docstring

    config = load_config()
    router = pmxt.Router(pmxt_api_key=api_key)

    known_pairs = _known_pairs()
    usable = _usable_polymarket_ids(config)
    last_scan_ts = None if args.full else _load_last_scan_ts()
    if last_scan_ts:
        print(f"updated_since={last_scan_ts} ({len(known_pairs)} pair(s) already known, will be skipped)")
    else:
        print(f"no prior scan state -- full scan ({len(known_pairs)} pair(s) already known, will be skipped)")

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    skipped_known = 0

    # Diagnostics -- printed regardless of outcome so a 0-candidate run is
    # distinguishable from "the schema guesses are all wrong and everything
    # got silently filtered out before ever reaching a schema-mismatch check".
    total_clusters = 0
    skipped_outside_universe = 0
    dumped_cluster_sample = False
    dumped_poly_kalshi_pair = False

    complete = False
    crawl = _crawl(router, last_scan_ts)
    while True:
        try:
            cluster = next(crawl)
        except StopIteration as done:
            complete = bool(done.value)
            break
        except Exception as exc:  # noqa: BLE001 -- keep what was read, keep the old watermark
            print(f"fetch_matched_market_clusters failed mid-crawl: {exc}")
            break
        total_clusters += 1
        if not dumped_cluster_sample:
            print(f"sample cluster object (first one seen): {_dump(cluster)}")
            dumped_cluster_sample = True
        confidence = _attr(cluster, "confidence", "score", default=0.0)
        cluster_markets = _attr(cluster, "markets", default=[]) or []

        poly = next((mkt for mkt in cluster_markets
                    if (_attr(mkt, "source_exchange", "venue", default="") or "").lower() == "polymarket"),
                   None)
        kalshi = next((mkt for mkt in cluster_markets
                      if (_attr(mkt, "source_exchange", "venue", default="") or "").lower() == "kalshi"),
                     None)
        if poly is None or kalshi is None:
            continue  # cluster matched, but not a Polymarket<->Kalshi pair

        if not dumped_poly_kalshi_pair:
            # Print unconditionally, not just on a None-mismatch: contract_address
            # is CONFIRMED as Polymarket's conditionId, but NOT confirmed as
            # Kalshi's ticker -- the MarketOutcome schema separately documents
            # "Market Ticker for Kalshi" living on outcome_id, one level down,
            # which may mean the market-level ticker is elsewhere entirely
            # (slug? source_metadata?). Seeing the real values side by side is
            # the only way to resolve this rather than guessing again.
            print(f"first Polymarket<->Kalshi pair found -- poly={_dump(poly)}")
            print(f"                                          kalshi={_dump(kalshi)}")
            dumped_poly_kalshi_pair = True

        # Both confirmed from a real run's raw dump: contract_address is
        # Polymarket's conditionId (e.g. '0xe017...'); Kalshi objects
        # leave contract_address None entirely, but populate `slug` with
        # the real venue ticker (e.g. 'KXPRESPERSON-28-NHAL') -- Polymarket's
        # own `slug` is a URL slug, not useful, so this priority order is
        # deliberately different per venue rather than one shared list.
        poly_condition_id = _attr(poly, "contract_address", "market_id")
        poly_question = _attr(poly, "title", "question", "name")
        kalshi_ticker = _attr(kalshi, "slug", "contract_address", "market_id")
        kalshi_title = _attr(kalshi, "title", "question", "name", default="")
        if poly_condition_id is None or poly_question is None or kalshi_ticker is None:
            print(f"pmxt schema mismatch on a cluster market object: "
                 f"poly={_dump(poly)} kalshi={_dump(kalshi)}")
            continue

        key = (str(poly_condition_id), str(kalshi_ticker))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if key[0] not in usable:
            skipped_outside_universe += 1
            continue
        if key in known_pairs:
            # Already confirmed or already sitting in `proposed` (from
            # pmxt or the LLM path) -- skip so the candidates file, and
            # the LLM verification pass that consumes it, both stay
            # focused on genuinely new suggestions.
            skipped_known += 1
            continue
        # Kalshi's market-level `title` is the broad EVENT question
        # ("Who will win the next presidential election?"); the outcome it
        # actually resolves on lives in the ticker suffix and the outcome
        # labels ("Nikki Haley" / "Not Nikki Haley"). Sending the title
        # alone made the downstream LLM check reject every candidate it
        # ever saw -- correctly, on the evidence it was given (2026-07-28).
        kalshi_outcomes = [
            lbl for lbl in (
                _attr(o, "label", "name", "title")
                for o in (_attr(kalshi, "outcomes", default=[]) or [])
            ) if lbl
        ]
        candidates.append({
            "poly_condition_id": str(poly_condition_id),
            "poly_question": poly_question,
            "kalshi_ticker": str(kalshi_ticker),
            "kalshi_title": kalshi_title,
            "kalshi_outcomes": kalshi_outcomes,
            "kalshi_description": _attr(kalshi, "description", default=None),
            "relation_type": "identity",
            "confidence": float(confidence),
            "scanned_ts": now_iso,
        })

    print(f"diagnostics: total_clusters_seen={total_clusters} skipped_already_known={skipped_known} "
          f"skipped_outside_universe={skipped_outside_universe} usable_polymarket={len(usable)}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"wrote {len(candidates)} candidate(s) to {OUTPUT_PATH}")

    # Only advance the watermark after a fully successful pass -- if this run
    # crashed partway through, the next run should still see everything from
    # last_scan_ts onward rather than silently skipping whatever it missed.
    if complete:
        _save_last_scan_ts(now_iso)
    else:
        print("crawl incomplete -- watermark left where it was")


if __name__ == "__main__":
    main()
