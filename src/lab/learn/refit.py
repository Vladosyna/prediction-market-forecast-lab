"""Parameter fits: M1 logistic recalibration curves, M2 category base rates,
and M5's macro surprise distribution.

Used by the Phase 2 bootstrap and re-used by the monthly `lab learn` job.
Artifacts are versioned JSON under data/models/; the ACTIVE.json pointer
selects which version each model loads. Fitters never touch live artifacts --
promotion is an explicit pointer update.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from scipy.optimize import minimize

from lab.util import PROJECT_ROOT, now_utc_iso

log = logging.getLogger(__name__)

HORIZON_BUCKETS = {
    "lt7d": (0, 7),
    "7to30d": (7, 30),
    "30to90d": (30, 90),
    "gt90d": (90, 10_000),
}


def _obs_len(observations: Any) -> int:
    """Row count for either input shape (a DataFrame is not truthy-testable)."""
    return len(observations)


def _is_empty(rows: Any) -> bool:
    """Emptiness test that a polars DataFrame survives.

    `not df` raises TypeError ("the truth value of a DataFrame is ambiguous"),
    which is how the walk-forward guard failed the first time the bootstrap set
    was handed through as a frame. Anything without a length keeps the old
    truthiness semantics, so a generator caller is unaffected.
    """
    if rows is None:
        return True
    try:
        return len(rows) == 0
    except TypeError:
        return not rows


def _obs_columns(observations: Any, names: tuple[str, ...],
                 defaults: dict[str, Any] | None = None) -> dict[str, np.ndarray]:
    """Column arrays from either a list of mappings or a polars DataFrame.

    Both M1 fitters read a fixed handful of columns and immediately turn them
    into numpy arrays, so the row-of-dicts shape is pure overhead for a large
    training set. The bootstrap set is 1,967,376 rows: as Python dicts it cost
    ~1.8GB RSS -- more than the production host physically has, which is how it
    was found (2026-08-05, `lab learn` OOM-killed even with swap). Handed the
    DataFrame directly, the same fit reads five contiguous arrays.

    The list-of-mappings path is unchanged and stays the one every other
    caller (the live holdout, every test fixture) uses. `defaults` supplies a
    constant column for a name the input does not carry -- the bootstrap file
    has no `venue` column, and both shapes must agree that those rows are
    'polymarket' (M1.x's own convention).
    """
    defaults = defaults or {}
    n = _obs_len(observations)
    if hasattr(observations, "get_column"):          # polars DataFrame
        present = set(observations.columns)
        out: dict[str, np.ndarray] = {}
        for name in names:
            if name in present:
                out[name] = observations.get_column(name).to_numpy()
            elif name in defaults:
                out[name] = np.full(n, defaults[name])
            else:
                raise KeyError(f"observations are missing required column {name!r}")
        return out
    out = {}
    for name in names:
        if name in defaults:
            out[name] = np.array([o.get(name, defaults[name]) for o in observations])
        else:
            out[name] = np.array([o[name] for o in observations])
    return out


EPS = 1e-6


def logit(p: np.ndarray | float) -> np.ndarray | float:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    return 1 / (1 + np.exp(-x))


def bucket_for_days(days: float) -> str | None:
    for name, (lo, hi) in HORIZON_BUCKETS.items():
        if lo <= days < hi:
            return name
    return None


# --- learning safeguards (Phase 7.1) --------------------------------------

class WalkForwardError(ValueError):
    """Raised when a refit is asked to fit without a validation window."""


def assert_walk_forward(train: Any, validation: Any) -> None:
    """Structural guard (brief section 6): a refit with no train/validation split
    is a bug. A refit call missing either window raises rather than silently
    fitting on full history.
    """
    if _is_empty(train):
        raise WalkForwardError("refit requires a non-empty training window")
    if _is_empty(validation):
        raise WalkForwardError("refit requires a non-empty validation window")


def _bound_scalar(old: float, new: float, max_step_pct: float) -> float:
    """Clamp `new` to within +/-max_step_pct (relative) of `old`."""
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return new
    if old == 0:
        return float(new)  # relative step undefined against zero -- allow, caller logs
    span = abs(old) * max_step_pct
    return float(min(max(new, old - span), old + span))


def bound_step(old: Any, new: Any, max_step_pct: float) -> Any:
    """Recursively clamp each live numeric parameter's move to +/-max_step_pct.

    Mirrors the artifact structure (nested dicts like M1 buckets or M4 category
    weights). Keys present only in `new` pass through unbounded (a brand-new
    bucket/category has no incumbent to step from). Non-numeric leaves pass
    through. One noisy month becomes a slow lean, not a lurch (brief section 6).
    """
    if isinstance(old, dict) and isinstance(new, dict):
        return {k: (bound_step(old[k], v, max_step_pct) if k in old else v)
                for k, v in new.items()}
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
            and not isinstance(old, bool) and not isinstance(new, bool):
        return _bound_scalar(old, new, max_step_pct)
    return new


def fit_logistic_recalibration(p_market: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """MLE fit of y ~ sigmoid(alpha + beta * logit(p_market)).

    beta > 1 means the market is underconfident (extremizing helps).
    """
    x = logit(np.asarray(p_market, dtype=float))
    y = np.asarray(y, dtype=float)

    def nll(params: np.ndarray) -> float:
        a, b = params
        p = np.clip(sigmoid(a + b * x), EPS, 1 - EPS)
        return -float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))

    res = minimize(nll, x0=np.array([0.0, 1.0]), method="Nelder-Mead")
    alpha, beta = res.x
    return {"alpha": float(alpha), "beta": float(beta), "n": int(len(y)), "nll": float(res.fun)}


def isotonic_fit(p_market: np.ndarray, y: np.ndarray, n_bins: int = 20) -> list[dict]:
    """Binned empirical rates made monotone via pool-adjacent-violators.

    Sanity companion to the logistic fit -- large disagreement between the two
    flags a badly specified parametric curve.
    """
    order = np.argsort(p_market)
    p_sorted, y_sorted = p_market[order], y[order]
    bins = np.array_split(np.arange(len(p_sorted)), min(n_bins, max(1, len(p_sorted) // 10)))
    centers = [float(np.mean(p_sorted[b])) for b in bins if len(b)]
    rates = [float(np.mean(y_sorted[b])) for b in bins if len(b)]
    weights = [len(b) for b in bins if len(b)]

    # Pool adjacent violators to enforce monotone non-decreasing rates.
    vals = [[r, w] for r, w in zip(rates, weights)]
    i = 0
    while i < len(vals) - 1:
        if vals[i][0] > vals[i + 1][0] + EPS:
            merged_w = vals[i][1] + vals[i + 1][1]
            merged_r = (vals[i][0] * vals[i][1] + vals[i + 1][0] * vals[i + 1][1]) / merged_w
            vals[i] = [merged_r, merged_w]
            del vals[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    # Expand pooled values back over the original bins.
    iso_rates: list[float] = []
    idx = 0
    remaining = vals[idx][1] if vals else 0
    for w in weights:
        while remaining <= 0 and idx + 1 < len(vals):
            idx += 1
            remaining = vals[idx][1]
        iso_rates.append(vals[idx][0] if vals else 0.0)
        remaining -= w
    return [
        {"p_center": c, "empirical": r, "isotonic": ir, "n": w}
        for c, r, ir, w in zip(centers, rates, iso_rates, weights)
    ]


def fit_m1_curves(observations: list[dict]) -> dict[str, Any]:
    """Fit per-horizon-bucket recalibration curves.

    observations: [{p_market, outcome, days_to_resolution}, ...], or a polars
    DataFrame carrying the same columns (see `_obs_columns`).
    """
    artifact: dict[str, Any] = {"kind": "m1_curves", "fitted_at": now_utc_iso(), "buckets": {}}
    if _obs_len(observations) == 0:
        log.warning("m1 fit: no observations")
        return artifact
    cols = _obs_columns(observations, ("p_market", "outcome", "days_to_resolution"))
    p_all = cols["p_market"].astype(float)
    y_all = cols["outcome"].astype(float)
    d_all = cols["days_to_resolution"].astype(float)
    for name in HORIZON_BUCKETS:
        lo, hi = HORIZON_BUCKETS[name]
        mask = (d_all >= lo) & (d_all < hi)
        n_bucket = int(np.count_nonzero(mask))
        if n_bucket < 100:
            log.warning("m1 fit: bucket has too few observations, skipping",
                        extra={"ctx": {"bucket": name, "n": n_bucket}})
            continue
        p_b, y_b = p_all[mask], y_all[mask]
        fit = fit_logistic_recalibration(p_b, y_b)
        iso = isotonic_fit(p_b, y_b)
        iso_dev = max(
            (abs(float(sigmoid(fit["alpha"] + fit["beta"] * logit(b["p_center"]))) - b["isotonic"])
             for b in iso),
            default=0.0,
        )
        artifact["buckets"][name] = {**fit, "isotonic_bins": iso, "max_iso_deviation": iso_dev}
    return artifact


HIER_ALLOWED_VENUES = ("polymarket", "kalshi", "metaculus")  # guardrail 16: never manifold/archives


def fit_m1_hier_curves(
    observations: list[dict], min_bucket_n: int = 100, min_venue_n: int = 5
) -> dict[str, Any]:
    """M1.x hierarchical recalibration (Phase 12, CLAUDE.md M1.x): one global
    logistic curve per horizon bucket plus a ridge-shrunk per-venue offset,
    logit(p_hat) = (alpha_g + alpha_v) + (beta_g + beta_v) * logit(p_market).

    The ridge penalty on each venue's offset scales as (bucket_n / n_v): a
    venue with few observations in a bucket (e.g. Metaculus, ~40 resolved/
    month) is pulled hard toward the global curve -- partial pooling; a venue
    with many observations (e.g. Kalshi at scale) can diverge freely where its
    own data demands it. Venues below min_venue_n in a bucket get no offset
    parameter at all (M1Hier falls back to the global-only curve for them --
    the same outcome as an infinitely-shrunk offset, without wasting a
    parameter on noise).

    observations: [{p_market, outcome, days_to_resolution, venue}, ...], or a
    polars DataFrame carrying the same columns (see `_obs_columns`); a missing
    `venue` column means 'polymarket', as the bootstrap training set has none.
    Guardrail 16: fits only on polymarket/kalshi/metaculus rows -- any other
    venue tag is dropped before fitting, a second explicit backstop beyond
    whatever filtering the caller already did.
    """
    artifact: dict[str, Any] = {"kind": "m1_hier_curves", "fitted_at": now_utc_iso(), "buckets": {}}
    if _obs_len(observations) == 0:
        log.warning("m1_hier fit: no observations")
        return artifact
    cols = _obs_columns(observations, ("p_market", "outcome", "days_to_resolution", "venue"),
                        defaults={"venue": "polymarket"})
    allowed = np.isin(cols["venue"], HIER_ALLOWED_VENUES)
    if not allowed.any():
        log.warning("m1_hier fit: no observations")
        return artifact
    p_all = cols["p_market"].astype(float)[allowed]
    y_all = cols["outcome"].astype(float)[allowed]
    d_all = cols["days_to_resolution"].astype(float)[allowed]
    v_all = cols["venue"][allowed]

    for name in HORIZON_BUCKETS:
        lo, hi = HORIZON_BUCKETS[name]
        in_bucket = (d_all >= lo) & (d_all < hi)
        bucket_n = int(np.count_nonzero(in_bucket))
        if bucket_n < min_bucket_n:
            log.warning("m1_hier fit: bucket has too few observations, skipping",
                        extra={"ctx": {"bucket": name, "n": bucket_n}})
            continue

        p = p_all[in_bucket]
        y = y_all[in_bucket]
        venues = v_all[in_bucket]
        x = logit(p)

        venue_counts = {v: int(np.sum(venues == v)) for v in sorted(set(venues.tolist()))}
        fit_venues = [v for v in venue_counts if venue_counts[v] >= min_venue_n]
        venue_idx = {v: i for i, v in enumerate(fit_venues)}
        venue_mask = {v: (venues == v) for v in fit_venues}
        n_params = 2 + 2 * len(fit_venues)

        def unpack(params: np.ndarray) -> tuple[float, float, dict[str, tuple[float, float]]]:
            offsets = {v: (float(params[2 + 2 * i]), float(params[2 + 2 * i + 1]))
                      for v, i in venue_idx.items()}
            return float(params[0]), float(params[1]), offsets

        def objective(params: np.ndarray, _venue_mask=venue_mask, _venue_counts=venue_counts,
                     _bucket_n=bucket_n, _x=x, _y=y) -> float:
            alpha_g, beta_g, offsets = unpack(params)
            alpha = np.full(_bucket_n, alpha_g)
            beta = np.full(_bucket_n, beta_g)
            penalty = 0.0
            for v, (av, bv) in offsets.items():
                alpha[_venue_mask[v]] += av
                beta[_venue_mask[v]] += bv
                penalty += (_bucket_n / _venue_counts[v]) * (av ** 2 + bv ** 2)
            p_hat = np.clip(sigmoid(alpha + beta * _x), EPS, 1 - EPS)
            nll = -float(np.sum(_y * np.log(p_hat) + (1 - _y) * np.log(1 - p_hat)))
            return nll + penalty

        x0 = np.zeros(n_params)
        x0[1] = 1.0  # beta_g starting point, same convention as fit_logistic_recalibration
        res = minimize(objective, x0, method="L-BFGS-B")
        alpha_g, beta_g, offsets = unpack(res.x)

        artifact["buckets"][name] = {
            "global": {"alpha": alpha_g, "beta": beta_g, "n": bucket_n},
            "venues": {
                v: {"alpha_offset": av, "beta_offset": bv, "n": venue_counts[v]}
                for v, (av, bv) in offsets.items()
            },
            "nll": float(res.fun),
        }
    return artifact


def fit_m2_baserates(rows: Any, min_n: int = 50) -> dict[str, Any]:
    """Category base rates from resolved markets: [{category, outcome}, ...],
    or a polars DataFrame with those columns (see `_obs_columns`) -- the
    bootstrap archive has 221,991 markets, and expanding them into dicts just
    to read two fields is the same waste `fit_m1_curves` stopped doing."""
    artifact: dict[str, Any] = {"kind": "m2_baserates", "fitted_at": now_utc_iso(), "categories": {}}
    by_cat: dict[str, list[float]] = {}
    if _is_empty(rows):
        return artifact
    cols = _obs_columns(rows, ("category", "outcome"))
    uncategorized = 0
    for cat, outcome in zip(cols["category"].tolist(), cols["outcome"].tolist()):
        # A market with no category cannot contribute to a PER-category base
        # rate, so it is dropped -- but counted into the artifact rather than
        # dropped silently, because the count is the only signal that the
        # taxonomy has a gap (an unmapped Kalshi series yields NULL here).
        #
        # Before 2026-08-22 a None simply became a dict key, and the sort
        # below then raised `'<' not supported between NoneType and str`,
        # taking the whole monthly `lab learn` down. Four resolved Kalshi
        # markets first seen on 08-14 and 08-17 were enough to do it -- which
        # is why the 08-05 run passed and every run after those dates would
        # have failed, silently, inside a timer unit nothing watches.
        if cat is None:
            uncategorized += 1
            continue
        by_cat.setdefault(cat, []).append(float(outcome))
    if uncategorized:
        artifact["skipped_uncategorized"] = uncategorized
        log.warning("m2 base rates: dropped markets with no category",
                    extra={"ctx": {"count": uncategorized}})
    for cat, outcomes in sorted(by_cat.items()):
        if len(outcomes) < min_n:
            continue
        artifact["categories"][cat] = {
            "base_rate": float(np.mean(outcomes)),
            "n": len(outcomes),
        }
    return artifact


# --- M5 macro surprise distribution ---------------------------------------

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

# Actual BEA release paired against each Atlanta Fed nowcast series. Both are
# published on FRED as ONE ROW PER QUARTER (date = quarter start), not daily --
# confirmed live: GDPNOW/PCENOW each return exactly one observation per
# calendar quarter. GDPNow/PCENow methodology revises a quarter's row only
# until that quarter's own BEA advance estimate lands, then it's fixed
# forever, so the plain (no realtime_start/end) observation history already
# gives the frozen final-pre-release nowcast for every completed quarter --
# no ALFRED vintage query needed, just a same-quarter join.
M5_MACRO_ACTUAL_SERIES = {
    "GDPNOW": "A191RL1Q225SBEA",   # Real GDP, % change SAAR
    "PCENOW": "DPCERL1Q225SBEA",   # Real PCE, % change SAAR
}


def fetch_fred_series(series_id: str, api_key: str) -> list[dict[str, Any]]:
    """Full observation history for a FRED series: [{date, value}, ...]."""
    resp = httpx.get(FRED_OBS_URL, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "asc", "limit": 100000,
    }, timeout=30)
    resp.raise_for_status()
    out: list[dict[str, Any]] = []
    for obs in resp.json().get("observations", []):
        v = obs.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append({"date": obs["date"], "value": float(v)})
        except (TypeError, ValueError):
            continue
    return out


def pair_nowcast_surprises(
    nowcast_rows: list[dict[str, Any]], actual_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pair each realized quarterly release with that quarter's frozen nowcast.

    Both series are one row per calendar quarter (date = quarter start), so
    this is a plain join on `date` -- quarters with no actual release yet
    (the current, still-updating quarter) simply have nothing to join to and
    are dropped, not guessed.
    """
    actual_by_date = {a["date"]: a["value"] for a in actual_rows}
    pairs: list[dict[str, Any]] = []
    for n in sorted(nowcast_rows, key=lambda r: r["date"]):
        if n["date"] not in actual_by_date:
            continue
        actual = actual_by_date[n["date"]]
        pairs.append({
            "period": n["date"], "nowcast": n["value"], "actual": actual,
            "surprise": actual - n["value"],
        })
    return pairs


def fit_m5_macro_sd(
    train_pairs: list[dict[str, Any]], validation_pairs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Empirical sd of (actual - nowcast) surprises, walk-forward validated.

    Pure arithmetic over FRED's own point-in-time nowcast history and the
    realized BEA release -- no LLM call and no lab-generated forecast needed,
    safe on the same footing as the M1/M2 refits (brief section 6).
    """
    assert_walk_forward(train_pairs, validation_pairs)
    train_surprises = np.array([p["surprise"] for p in train_pairs], dtype=float)
    sd = float(np.std(train_surprises, ddof=1)) if len(train_surprises) > 1 \
        else abs(float(train_surprises[0]))
    sd = max(sd, EPS)
    val_surprises = np.array([p["surprise"] for p in validation_pairs], dtype=float)
    val_nll = float(np.sum(0.5 * np.log(2 * np.pi * sd ** 2) + val_surprises ** 2 / (2 * sd ** 2)))
    return {
        "sd": sd, "n_train": len(train_pairs), "n_validation": len(validation_pairs),
        "val_nll": val_nll,
    }


def fit_m5_macro_sds(
    api_key: str, *, min_quarters: int = 12, validation_quarters: int = 8,
    fetch: Any = fetch_fred_series,
) -> dict[str, Any]:
    """Walk-forward sd fit for every FRED_SERIES in m5_nowcast.MacroAdapter.

    Splits each series' paired (nowcast, actual) history chronologically:
    all but the trailing `validation_quarters` train the sd, the rest hold
    out for the walk-forward check `assert_walk_forward` requires. Series
    with fewer than `min_quarters` paired releases are skipped, not guessed.
    """
    artifact: dict[str, Any] = {"kind": "m5_macro_sd", "fitted_at": now_utc_iso(), "series": {}}
    for series_id, actual_series_id in M5_MACRO_ACTUAL_SERIES.items():
        nowcast_rows = fetch(series_id, api_key)
        actual_rows = fetch(actual_series_id, api_key)
        pairs = pair_nowcast_surprises(nowcast_rows, actual_rows)
        if len(pairs) < min_quarters:
            log.warning("m5 macro fit: too few paired quarters, skipping",
                        extra={"ctx": {"series": series_id, "n": len(pairs)}})
            continue
        train, validation = pairs[:-validation_quarters], pairs[-validation_quarters:]
        fit = fit_m5_macro_sd(train, validation)
        artifact["series"][series_id] = {**fit, "actual_series": actual_series_id}
    return artifact


# --- artifact store -------------------------------------------------------

def models_dir(config: dict[str, Any]) -> Path:
    d = Path(config["storage"]["models_dir"])
    d = d if d.is_absolute() else PROJECT_ROOT / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_artifact(config: dict[str, Any], name: str, artifact: dict[str, Any],
                  promote: bool = True) -> Path:
    """Write the next version of `name`; optionally promote it in ACTIVE.json."""
    d = models_dir(config)
    # The highest existing number + 1, never the count + 1, and never over an
    # existing file (2026-10-14). With m2_baserates_v5.json absent, the count
    # handed out v6 three times -- on 07-07, 08-02 and 10-14 -- and each save
    # silently replaced an artifact the registry still pointed at, breaking
    # the "content immutable once written" rule rollback depends on.
    pattern = re.compile(rf"{re.escape(name)}_v(\d+)\.json")
    numbers = [int(m.group(1)) for f in d.glob(f"{name}_v*.json") if (m := pattern.fullmatch(f.name))]
    version = max(numbers, default=0) + 1
    artifact = {**artifact, "version": version}
    path = d / f"{name}_v{version}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(artifact, indent=2))
    if promote:
        active_path = d / "ACTIVE.json"
        active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else {}
        active[name] = path.name
        active_path.write_text(json.dumps(active, indent=2), encoding="utf-8")
    log.info("artifact saved", extra={"ctx": {"name": name, "version": version, "promoted": promote}})
    return path


def load_active_artifact(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    d = models_dir(config)
    active_path = d / "ACTIVE.json"
    if not active_path.exists():
        return None
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if name not in active:
        return None
    return json.loads((d / active[name]).read_text(encoding="utf-8"))
