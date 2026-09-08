# Pre-Analysis Plan

**Committed:** 2026-07-06 (UTC). **Status:** primary document — first version, no addenda yet.

This document is dated and committed once, before the confirmatory window it defines opens. It is
never edited after commitment: any later change to hypotheses, outcomes, or exclusion rules is
appended below as a dated addendum (§9), never a silent rewrite — the same append-only discipline
the forecast ledger itself follows (`CLAUDE.md` guardrail 5).

Its evidentiary weight rests on [`docs/ledger_commitments.jsonl`](ledger_commitments.jsonl): a
nightly sha256 commitment over each closed day's appended `forecasts` rows, pushed to this public
repo (see [`src/lab/ledger_commitment.py`](../src/lab/ledger_commitment.py)). Together, the two
files let a reviewer confirm both *what* was predicted and *when* the hypotheses below were fixed,
without trusting the author's word for either.

## 1. Purpose

This project (`CLAUDE.md` §1) asks one question: can probability estimates for Polymarket event
outcomes be produced that are better calibrated than the market price itself, measured after
resolution. This plan fixes, in advance, which claims from that broader research program count as
confirmatory versus exploratory.

## 2. Primary hypotheses

- **H1 — Long-horizon underconfidence recalibration edge.** Polymarket prices are systematically
  underconfident far from resolution (calibration slope > 1 at long horizons, converging toward 1
  near resolution). `m1_debiased` and its hierarchical successor `m1_hier@polymarket` (`CLAUDE.md`
  §6, Phase 2 and Phase 12) are predicted to beat the market baseline (`m0_market`) on paired Brier
  skill in the ≥30-day horizon buckets.
- **H2 — Recalibration skill net of costs, P1/P2 categories.** Restricted to the two categories the
  edge research identifies as most model-drivable (`CLAUDE.md` §3 universe policy) — P1 (economic
  data releases and central-bank decisions) and P2 (weather markets) — the recalibration and
  structural models (`m1_debiased`/`m1_hier`, `m5_nowcast`) are predicted to show positive skill.
  "Net of costs" here means net of the shadow portfolio's simulated slippage and sizing frictions
  (`CLAUDE.md` §8); the dedicated fee-schedule/net-of-cost report line described in the broader
  Phase 15 task list is a later addition and does not gate this hypothesis's current confirmatory
  test — the shadow portfolio's existing simulated fill/slippage model is the net-of-cost proxy
  until that line ships.
- **H3 — Cross-venue lead-lag.** `m7_crossvenue`'s external-venue log-odds pool (Kalshi and
  Metaculus, `CLAUDE.md` §6) is predicted to show CLV-style predictive value — i.e., Polymarket's
  own price moves toward the external pool's view more often than the reverse — ahead of, not
  merely coincident with, Polymarket's own price adjustment.

## 3. Primary outcome measure

Paired Brier skill, `skill = mean(brier_market − brier_model)` over resolved, paired forecast rows,
per venue and category (`CLAUDE.md` §7). The **sole confirmatory claim statistic** is the
event-clustered, time-uniform anytime-valid confidence sequence
(`WSR asymptotic CS — [`src/lab/eval/anytime.py`](../src/lab/eval/anytime.py)`): a hypothesis is
supported only when this interval excludes zero in the predicted direction, at the honesty tier
appropriate to `n` (`CLAUDE.md` §7: n < 200 insufficient, 200 ≤ n < 500 preliminary, n ≥ 500
standard). The precision-weighted stratified skill estimator
(`[`src/lab/eval/stratified.py`](../src/lab/eval/stratified.py)`) must agree in direction and also
exclude zero as a required secondary check — it does not on its own establish a claim, per
`CLAUDE.md` §7's own framing of it as a check against the primary CS, not an independent test.
The paired-scoring machinery both statistics run on top of lives in
[`src/lab/eval/scoring.py`](../src/lab/eval/scoring.py).

## 4. Secondary / exploratory outcomes

Explicitly non-confirmatory, reported for context but not gating any hypothesis above: log loss,
CLV-style price-drift signal ahead of resolution, reliability-diagram calibration curves, and the
wealth-ledger's sleeping-expert-normalized cumulative log-growth (`cum_log_wealth / n_forecasts`,
`CLAUDE.md` §6/Phase 14). The shadow MWU ensemble-weighting challenger (Phase 14.1) is likewise
exploratory until it clears its own promotion gate.

## 5. Exclusion rules

Verbatim from `CLAUDE.md` §3's universe policy:

- **Structurally unforecastable, excluded as forecast targets:** all crypto/equity price-target
  markets at any horizon (the market price *is* the forecast for a martingale underlying);
  "will X say/tweet Y"-style novelty markets and anything with ambiguous resolution wording.
- **Tail-priced markets** (≥ 0.95 or ≤ 0.05) are excluded as forecast *targets* — residual edge
  there is dominated by oracle/dispute tail risk — but are **retained** in calibration statistics.
- **Null control:** a small random sample of sports markets, forecast by the cheap models only, is
  scored identically to every other category and shown in the same report table. A statistically
  significant "skill" finding there does not support any hypothesis above — it instead invalidates
  the run pending investigation into a broken harness (`CLAUDE.md` §3/§7).
- **Venue/provenance exclusions** (`CLAUDE.md` guardrail 16): Manifold (play money) is excluded
  from all skill claims — event mapping and M2 base rates only. Historical archives (GJP,
  PredictIt, the HF bootstrap dataset) feed M2 base rates only, never a skill claim for H1–H3.

## 6. Confirmatory window

This is the part most prone to being gotten wrong, so it is stated precisely:

- M1/M2/M5's parameters (recalibration curves, base rates, error distributions) were **fit** on the
  pre-existing historical bootstrap (`CLAUDE.md` Phase 2, walk-forward split, allowed under §7 —
  "statistical models may be backtested"). That fitting is not itself under test.
- What **is** confirmatory for H1/H2 is whether those already-fit, already-frozen model versions
  beat the market on forecasts made **after this document's commitment date (2026-07-06)**.
  Forecasts made and resolved before that date, using the same model versions, are exploratory —
  useful for monitoring, not for the claim.
- H3 (`m7_crossvenue`) follows the same rule as H1/H2 above — confirmatory only for forecasts made
  after 2026-07-06 — with one simplification in its favor: M7 is deterministic at forecast time (no
  LLM call, `CLAUDE.md` §6) and was never fit on the historical bootstrap the way M1/M2/M5 were, so
  there is no separate "already-fit" caveat to track for it.
- LLM-based models (M3/M3b) carry no primary hypothesis in §2, but the same confirmatory logic
  applies to them with an extra, stricter rule: guardrail 15 forbids ever backtesting an LLM model
  on pre-cutoff history, so their skill accrues *only* from forecasts made after each specific model
  version's own `registered_ts` — never retroactively, regardless of this document's date.
- A challenger version registered after 2026-07-06 (any `model_id@vN` promoted via the champion/
  challenger machinery, `CLAUDE.md` §6/§7.1) inherits this same confirmatory-window logic relative
  to its own `registered_ts`, not this document's date — each model version's track record starts
  when it starts, per guardrail 18.

## 7. Historical gap note

`docs/ledger_commitments.jsonl`'s first entry covers 2026-07-05 (the most recent fully-elapsed UTC
day as of this feature's deployment). Forecasts and resolutions recorded in the database before
that date exist and are used for the exploratory/monitoring purposes above, but were **not**
contemporaneously hash-committed — retroactively hashing them would carry no pre-registration
value and is deliberately not attempted (see the commit history of
[`src/lab/ledger_commitment.py`](../src/lab/ledger_commitment.py) for the reasoning). This is a
documented limitation, not a gap papered over.

## 8. Deviation policy

Any change to §2–§6 after 2026-07-06 — a new primary hypothesis, a changed exclusion rule, a
different primary outcome statistic — is recorded as a new, dated, appended section below (§9+),
never as an edit to §2–§7 above. A reviewer can always reconstruct exactly what was pre-registered
at any point in time by reading this file's own git history.

## 9. Addenda

**Addendum 9.1 (2026-07-09).** The confirmatory analysis window for H1–H3 closes at 2026-12-31
23:59 UTC. Forecasts frozen on or before that timestamp, resolving at any later date, remain in
the confirmatory set; forecasts frozen after it are exploratory for this paper and may seed a
future pre-registered window. Primary analyses will be executed once, after the freeze, exactly
as specified in §2–§6; the honesty-tier label corresponding to realized n will be reported as-is,
whatever it turns out to be.

**Addendum 9.2 (2026-07-09).** Two corrections surfaced by an independent verification audit
cross-checking this plan and `CLAUDE.md` against the actual codebase:

- (a) §3's reference to "WSR asymptotic CS" was a citation error. The confirmatory statistic
  (`src/lab/eval/anytime.py`) implements the normal-mixture uniform boundary of Howard, Ramdas,
  McAuliffe & Sekhon (2021, *Annals of Statistics* 49(2):1055-1080, arXiv:1810.08240), not
  Waudby-Smith & Ramdas (2020)'s distinct betting-based construction. This is a citation
  correction only — the statistic itself, its time-uniform coverage guarantee, and its role as
  the sole confirmatory claim statistic for H1–H3 are unchanged.
- (b) A pre-specified robustness check, implicit in the exclusion rules (§5) but not previously
  stated explicitly: primary analyses for H1–H3 will be re-run excluding forecasts on markets
  where `resolutions.disputed = 1`, reported as a named robustness check alongside the primary
  result — not a new primary outcome, and not a gate on any hypothesis in §2.

**Addendum 9.3 (2026-07-10).** Motivated by Gebele & Matthes (2026, arXiv 2605.31431), which shows
that a substantial share of apparent long-horizon underconfidence in near-certain prediction-market
contracts reflects settlement-induced discounting (delayed, collateral-locked redemption) rather
than belief miscalibration: as a pre-specified robustness check on H1 (not a change to its primary
specification), the confirmatory analysis will additionally report M1/M1.x skill separately for
(a) negRisk vs. non-negRisk markets, and (b) venues/periods with active collateral-yield programs
(e.g., Kalshi's APY, Polymarket's holding-rewards-eligible markets) vs. without — both mitigate the
settlement wedge per the cited mechanism (Gebele & Matthes §5.3: negRisk conversion compresses it,
yield-bearing collateral flattens its term structure). This stratification is exploratory relative
to the frozen primary hypotheses but is committed now, before any confirmatory data exists,
specifically to prevent this becoming a post hoc excuse in either direction if H1 resolves cleanly
or resolves to null.

**Addendum 9.4 (2026-07-30).** H3's pre-registered external pool (§2) names Kalshi *and*
Metaculus. Metaculus access will not be obtained: on 2026-07-29 Metaculus declined this project's
researcher data request for recent data, offering access only to a 2023-and-earlier archive. That
archive cannot serve a design that scores *live* community predictions against contemporaneous
market prices as questions resolve, so the offer was not a partial fit but a non-fit. H3's realized
external pool is therefore **Kalshi-only for the entire confirmatory window**, and the paper will
report it as such rather than describing a two-venue pool it never had.

Nothing else changes: not the claim statistic, not the honesty tiers, not the exclusion rules, not
any hypothesis in §2. This addendum records an external constraint on realized data scope — a fact
about what could be collected — and is deliberately *not* a revision of the analysis plan. It is
filed under the same discipline as 9.1: a dated fact, not a specification changed after seeing how
the data behaved.

Two consequences worth stating now rather than at write-up. First, `m1_hier@metaculus` (the bare
recalibration call that would have fed a Metaculus quote into M7) will never be exercised on live
data; the M1.x family's realized scope is Polymarket and Kalshi. Second, M1.x's documented
limitation — that it trains on a Polymarket-only historical bootstrap and that between-venue
variance components are weakly identified with so few groups — stops being a caveat about a future
state and becomes a permanent property of this study, to be stated in those terms.

**Addendum 9.5 (2026-08-06).** A five-day operational incident materially changed the *shape* of
the forecast ledger, and this addendum records it before any confirmatory analysis is run.

**What happened.** From 2026-08-02T16:18 to 2026-08-06T16:15 the orchestrator was OOM-killed on a
~62-minute cycle (19 restarts; full technical account in `docs/OPERATIONS.md`). The nightly bundle
completed its forecast step on each attempt and died later, before recording success, so the hourly
missed-run catch-up re-ran it every hour. §6's forecast cadence is "once per market per day per
model, plus an extra forecast when |24h price move| > 0.10" — and that second clause carried no
minimum spacing, because under a once-daily bundle it cannot fire more than once a day. Running
hourly, it fired hourly.

**Measured effect.** In 2026-08-02..06 the ledger received 144,282 rows, of which **54,634 are
beyond one per (market, model, day)** — 38% of that window and **10.0% of the all-time ledger** at
the time of writing. Up to 25 forecasts landed on a single market-model-day (maximum 1 on every day
through 2026-08-01). The excess is concentrated on **1,030 of 5,562 markets (19%)** — and not at
random: the trigger selects markets whose price moved more than 0.10 in 24 hours, so the
over-represented rows are precisely the high-information, high-volatility market-days.

**Why nothing is retracted.** Every one of those rows is individually valid: written at its own
timestamp, paired with its own contemporaneous `p_market_at_ts` (guardrail 13's freshness check
applied unchanged), with no look-ahead. The ledger is append-only (§5) and its daily hashes are
already committed in `docs/ledger_commitments.jsonl`; deleting rows to tidy the record is exactly
the act that discipline exists to make detectable. They stay.

**What this plan commits to instead.** The primary outcome is unchanged: paired Brier skill with
event-clustered anytime-valid confidence sequences, over all resolved forecasts, exactly as
pre-registered. Clustering already absorbs the *dependence* these rows introduce (they are the same
markets), but it does not absorb the *weighting* — a row-weighted mean gives a 25×-duplicated
market-day 25× the influence. Therefore, as a pre-specified robustness check committed here before
the analysis window closes: the confirmatory analysis will additionally report the identical
model × venue × category × window matrix computed on a **deduplicated ledger — the first forecast
per (market, model, UTC day), which is the pre-registered cadence** — reported alongside, never
replacing, the primary result. A material disagreement in sign or in CS exclusion between the two
is itself the finding and will be reported as such.

This is the same construction as 9.2(b)'s disputed-market check and uses the same mechanism (a
parallel `window_label` suffix, never overwriting primary rows). It is filed under 9.1's discipline:
a dated operational fact and a robustness check specified before seeing its result — not a primary
specification changed after seeing how the data behaved.

**Forward fix, for completeness.** `forecast.price_move_min_hours` (default 6h) now gates the
price-move trigger. It is inert under the intended daily bundle — by the time that runs, the last
forecast is ~24h old — and exists solely so a catch-up storm cannot re-fire the same 24-hour move.
The incident window is bounded and closed; no data after 2026-08-06T16:15 is affected.

**Addendum 9.6 (2026-08-06).** M3's coverage parameter `forecast.m3_top_k` is raised from 20 to
120, effective this date. This is a deliberate, dated change to a **collection** parameter, made
before the confirmatory window closes and recorded here rather than discovered in the data.

**Why.** Measured on this date: M3 had accumulated **4 resolved event clusters** — 32 resolved rows
from 694 forecasts, a 4.6% resolution yield, because the priority-category liquid pool it draws
from is 92% longer than 30 days to resolution (921 of 1,001 candidates). At that rate two things
specified in this project reach the 2026-12-31 freeze with nothing to report: the Phase 7 M3
**aggregator** walk-forward refit, gated at `learn.m3_min_resolved: 150` resolved M3 forecasts and
never once triggered; and the Phase 15 **boundary-randomization experiment**, which has assigned
311 randomized and 383 non-randomized forecasts but has almost no resolved outcomes to identify a
marginal effect from. Cost was never the binding constraint: at $0.00081 per evidence run, K=120
costs ~$0.097/day against a $5.00/day cap.

**What this does not do.** It does not make an M3 skill claim reachable. 200 resolved event
clusters — this plan's own INSUFFICIENT boundary (§7) — is out of range for M3 under any K, and
**M3 and M3b will be reported at whatever honesty tier their realized n earns, which on present
evidence is INSUFFICIENT.** This addendum is filed to improve two *secondary* instruments, not to
rescue a primary claim, and it must not be read at write-up as having done the latter.

**Comparability, stated up front.** M3's covered population changes composition on this date: from
2026-08-06 it includes markets ranked 21..120 by the same deterministic liquidity ordering, which
are systematically less liquid than the first 20. The confirmatory analysis will therefore report
M3 results **split at 2026-08-06** as well as pooled, and will not present a pooled M3 figure
without that split alongside it. The ordering rule itself is unchanged — still liquidity-DESC
within priority categories, still no editorial judgment (guardrail 12) — and the randomization band
continues to sit at K±10, now 110..130.

Nothing else changes: not the primary outcome, not the claim statistic, not the honesty tiers, not
any hypothesis in §2. Filed under 9.1's discipline: a dated operational decision with its
consequences stated before its results are seen.

**Addendum 9.7 (2026-08-09).** The shadow portfolio's entry evaluation is restored from weekly to
**daily**, the cadence `CLAUDE.md` §8 has specified since inception ("Entry rule (evaluated daily on
liquid tier, using M4)"). This is recorded here because H2 names the shadow portfolio's simulated
slippage and sizing frictions as its net-of-cost proxy, and that proxy is computed from *realized*
trades — so how often entries are evaluated is load-bearing for a pre-registered hypothesis, not an
interpretability detail.

**What was found.** On this date the portfolio held 14 positions total — 3 resolved, 11 open —
after a month of operation. Three compounding causes, all operational:
(i) `schedule.shadow_cron` was weekly, not daily, from the start: a 7× reduction in entry
opportunities against the specified rule;
(ii) most of even those weekly firings never happened — every analytics cron job carried
APScheduler's 1-second default `misfire_grace_time`, so a firing landing on a busy event loop was
discarded silently (fixed the same day). Positions were opened on three dates only — 2026-07-09,
07-20 and 07-27 — and the latter two are Mondays, i.e. catch-up firings after the 168-hour control
window expired rather than scheduled runs;
(iii) exits are hold-to-resolution and the book is long-horizon by construction: of the 11 open
positions one resolves 2026-08-13 and the rest run to 2026-10-31, 11-30, 12-31 (four), 2027-01-03
and 2027-12-31. The entry rule requires |p_M4 − p_market| ≥ 0.05, and that disagreement concentrates
in exactly the long-horizon markets where the recalibration edge is hypothesised to live.

Capital was never the constraint: 25.5% of the simulated bankroll was deployed, the largest category
at 9.2% of its 20% cap.

**What changes and what does not.** Only the evaluation cadence, back to what §8 already said; the
entry filter, sizing, slippage model, fee schedule and hold-to-resolution exit are all untouched.
No hypothesis, outcome, statistic or honesty tier changes.

**Stated plainly, because it is the honest reading:** this does not retroactively create the trades
that were not opened between 2026-07-09 and 2026-08-09, and it cannot undo (iii). H2's net-of-cost
proxy will therefore rest on a trade population that is thin in absolute terms and thinner still in
*resolved* trades before the 2026-12-31 freeze, since most positions opened from here will not have
resolved by then. H2 will be reported at whatever honesty tier its realized resolved-trade count
earns, and if that count cannot support the net-of-cost comparison, the paper will say so rather
than report a P&L figure that reads as evidence. Filed under 9.1's discipline: a dated operational
correction with its limits stated before its results are seen.

**Addendum 9.8 (2026-08-09).** Kalshi's tier assignment is rekeyed, and Kalshi order-book depth is
collected for the first time. Both change the composition of the tradeable universe, so both are
recorded here before any confirmatory analysis runs.

**What was found.** Every one of the 4,822 Kalshi markets this lab tracks was assigned to the
`tail` tier, and none had ever been `liquid`. The cause is that `assign_kalshi_tier` gated the
liquid tier on Kalshi's own `liquidity_dollars` field, which reads **0.0 for every market Kalshi
publishes** — verified on this date across all 4,822 collected and against a live API sample. The
threshold could therefore never be met. Two consequences followed silently:
(i) the shadow portfolio scans the liquid tier only, so the entire Kalshi venue — including 2,092
markets resolving within seven days — was structurally excluded from it; and
(ii) `bid_depth_usd`/`ask_depth_usd` were written NULL for every Kalshi snapshot (167,564 rows on
this date, none with depth), so even had the tier been right, §8's own entry filter (top-of-book
depth ≥ $500) would have rejected every Kalshi market on a null.

This matters because H2's net-of-cost proxy is computed from *realized* shadow trades, and the
short-horizon stream that could produce resolved trades before the freeze is overwhelmingly
Kalshi's: Polymarket's liquid tier carried six candidates under 30 days to resolution on this date,
out of 481.

**What changes.** Kalshi tiers on traded volume and open interest, which are populated
(`min_volume: 5000`, `min_open_interest: 100`, chosen from the live distribution: volume > 5,000
selects 788 of 4,822 markets). Top-of-book depth is now recorded from `yes_bid_size_fp`/
`yes_ask_size_fp`, which arrive with the market object the collector already fetches — no extra
request — as price × size, the same USD notional the Polymarket depth columns hold. A missing quote
stays NULL and never becomes 0.0.

**What this is expected to yield, measured rather than hoped.** Kalshi books are far thinner at the
top than Polymarket's: across the 60 highest-volume Kalshi markets, median non-zero top-of-book
depth is **$16** against **$415** for Polymarket's liquid tier, and only 6.7% (bid) / 13.3% (ask)
clear §8's $500 threshold, against 48% on Polymarket. The realistic effect is therefore on the order
of dozens of newly eligible Kalshi markets, not thousands — but dozens of *short-horizon* ones,
against the current zero. No filter, threshold or sizing rule in §8 is relaxed to achieve this; if
Kalshi markets do not clear the same $500 bar Polymarket markets face, they are not traded.

**Watch item, recorded now.** `tier` also selects the price-freshness bound (guardrail 13: 15 min
for liquid, 90 for tail), while the tier-wide Kalshi snapshot round runs every 15 minutes. Kalshi
markets promoted to `liquid` therefore sit near that bound, and if the round lengthens, forecasts on
them would be skipped as stale rather than paired against an old price — the safe direction, but a
coverage loss. Kalshi forecast counts will be checked against their pre-change level, and this
addendum amended if the bound has to move.

**Addendum 9.9 (2026-08-10).** Kalshi's tier assignment moves onto the lab's own measured
order-book depth, using the same thresholds already applied to Polymarket. This supersedes the
volume/open-interest keys introduced one day earlier in 9.8, which are retained only as a fallback
for markets not yet snapshotted.

**Why now, and why this rather than tuning the proxies.** `CLAUDE.md` Phase 17 item 2 already
requires tiering on collected depth rather than venue-reported fields, and Polymarket was moved to
it on 2026-07-07. Kalshi could not follow because no depth was collected for it; that changed on
2026-08-09 (addendum 9.8). With depth in hand, keeping Kalshi on volume and open interest would
have left the two venues on different definitions of the same word — and the proxies are the weaker
signal in both directions: lifetime volume says nothing about whether anyone is quoting now, and a
market can be deeply quoted with no volume recorded at all.

**One bar, both venues.** The measured distributions are close enough to share the existing
`universe.tiers.*.min_depth_usd` thresholds rather than inventing venue-specific ones: on
2026-08-10, per-market top-of-book depth was p25 $10 / p50 $45 on Kalshi against p25 $9 / p50 $66 on
Polymarket. At the shared liquid bar ($250) this selects 484 of 4,803 Kalshi markets, against 26
under 9.8's open-interest rule. Polymarket's assignment is unchanged — verified by differential
comparison against the previous implementation across every depth × liquidity × volume combination
tested, with zero mismatches.

**A contract defect found and fixed in the same pass.** `_depth_lookup` documents that a market with
no depth data is *absent* from its result, so tiering falls back rather than treating it as $0. The
implementation summed `fill_null(0)` over both columns, so a row whose quote had no size on either
side became a measured $0 and tiered `ignored`. This was unobservable while Polymarket was the only
venue with depth (every row has it); on Kalshi 1,715 of 4,803 markets were in exactly that state,
and shipping the change without this fix would have excluded all of them from the universe. The
implementation now matches its documented contract for both venues.

**Also closed here:** Kalshi universe exclusions were never written to `universe_log`, though this
venue carries roughly 80% of the lab's daily forecast rows. Phase 15's commitment — that "why isn't
X in the ledger" is answerable for every considered market — now holds for Kalshi too.

**What this does not change.** No hypothesis, outcome, statistic or honesty tier. The shadow
portfolio's entry filters (§8) are untouched: a Kalshi market still has to clear the same $500
top-of-book depth, the same 0.03 spread and the same 0.05 edge as a Polymarket one. Widening the
liquid tier changes which markets are *considered*, never the bar they must clear.

**Watch items.** The liquid tier drives one order-book-ladder request per market per 15-minute
round, so 484 markets adds ~0.5 req/s against Kalshi's ~10 (guardrail 8) -- the round's duration
will be checked and this addendum amended if it crowds. And 9.8's freshness watch item stands:
`tier` also selects the price-freshness bound, and a much larger liquid tier is a much larger
exposure to it. Kalshi forecast counts have not dropped so far (11,164 → 13,507 across 08-07..08-10)
and will keep being compared against that level.

**Addendum 9.10 (2026-08-10).** Measured consequences of 9.9, recorded before the reassignment
takes effect rather than after, and one decision stated explicitly so it cannot be re-read later as
convenient.

Applying the shared depth bar to the 4,808 live Kalshi markets moves them as follows: **444 to
`liquid`** (from 26), 1,865 to `tail`, **649 to `ignored`**, and 1,876 fall through to the
volume/open-interest fallback because they have no measured depth yet. Twenty-two of the 26 markets
9.8's open-interest rule had made liquid stay liquid; eleven drop to `tail` and eleven to `ignored`
on their actual books, which is the point of preferring measurement to a proxy.

**The 649 exclusions are the part that costs something.** They are all `economics`, and 154 of them
(24%) resolve within seven days — a *higher* short-horizon share than the 444 being promoted (5%).
Short-horizon resolved observations are this study's scarcest resource, so this cuts against the
direction 9.7–9.9 were working in. Two facts about them: their measured top-of-book depth is below
$10, and none of the 649 has ever produced a resolved forecast to date.

**The exclusion stands, and the reason is not n.** A market with under $10 of top-of-book depth has
no price anyone is meaningfully making, and this study's entire claim is skill measured *against the
market price*. Pairing a forecast with a quote that thin does not produce a weak observation; it
produces a comparison whose baseline is noise, in both directions. The same bar applies to
Polymarket and lands at the same place in each venue's own distribution (roughly p25: $9 on
Polymarket, $10 on Kalshi), so this is not a venue-specific harshness either. Choosing a laxer bar
for Kalshi after seeing that it would retain more short-horizon markets would be fitting the
specification to the sample — the precise move the pre-analysis discipline exists to prevent — and
is therefore not made.

Realized Kalshi forecast volume will be reported before and after this change, so the coverage cost
appears in the paper as a number rather than as an absence.

**Addendum 9.11 (2026-08-10).** A defect in Kalshi's universe sync put forecasts into the ledger on
markets that had already ended. This addendum records it, its measured effect, and a pre-specified
robustness check — before the confirmatory analysis is run.

**What happened.** `sync_kalshi_universe` bounded each cycle at `max_series_per_sync` (40) but
walked a fixed category and API order and simply stopped at the cap. The same head of the list was
re-synced every hour and the tail was never reached at all. Because only a sync refreshes a market's
`active`/`closed` flags, markets in the starved tail stayed flagged open indefinitely. Measured on
2026-08-10 across 5,009 Kalshi markets in ~285 series: **82% had not been re-synced in over three
days**, and **1,772 were still flagged active with an end date in the past**.

Those markets stayed in the forecast-eligible set, so **39,583 forecasts were written on Kalshi
markets already past their end date at the moment of writing** — across 549 distinct markets, and
all 39,583 have since resolved, so all are in the scoring population. That is **38% of Kalshi's
resolved rows** (8,028 of 21,356 for `m1_debiased`).

**Why it matters, and in which direction.** A market past its end date has stopped trading and its
outcome is determined; the price we pair against is a frozen last quote. Both the model and the
market baseline therefore sit on the known answer and the paired Brier difference collapses toward
zero. This is dilution, not inflation — measured on the live data, excluding these rows moves Kalshi
skill *away* from zero in every case:

| model | all rows | past-dated only | live-only |
|---|---|---|---|
| `m1_debiased` | −0.001852 | −0.000759 | **−0.002510** |
| `m1_hier@kalshi` | −0.001380 | −0.000773 | **−0.001747** |
| `m4_ensemble` | +0.000935 | +0.001201 | **+0.000779** |

The bias is conservative for a positive skill claim, but it is still a specification defect, and it
inflates n — this study's binding constraint — by 38% on its largest venue with rows that carry
almost no information. The honesty tiers (§7) are computed on that n.

**What is committed.** The rows stay: the ledger is append-only and their hashes are already in
`docs/ledger_commitments.jsonl`. As a pre-specified robustness check, the confirmatory analysis will
report the identical model × venue × category × window matrix **excluding forecasts written on or
after their market's end date**, alongside — never replacing — the primary result. This is the same
construction as 9.5's deduplicated-ledger check and 9.2(b)'s disputed-market check, and uses the
same parallel `window_label` mechanism.

**Forward fixes, both landed today.** The forecast loop now refuses any market past its own end date,
independent of how fresh the sync is. And the sync's cap became a rotation: series are ordered
least-recently-synced first (never-synced ahead of all), so the bound stays a politeness limit
rather than a permanent cutoff. The incident window is closed as of 2026-08-10.

**Addendum 9.12 (2026-08-10).** Phase 15's microstructure covariates on the forecast ledger begin
today. They were specified in `CLAUDE.md` §5 and in Phase 15's own acceptance criteria ("covariate
columns populate on live forecasts") when the phase was written, and were never implemented: an
audit on this date found only `spread_at_ts`, which predates Phase 15, and a code comment recording
the rest as "a separate sub-task" that was then never picked up.

From 2026-08-10, every forecast row carries `depth_covariate` (top-of-book depth in USD, from the
same snapshot that supplies `p_market_at_ts`), `volume_24h` (the venue's own 24-hour volume, from the
market object the universe sync already fetches), and `hour_utc`, alongside the `spread_at_ts` that
was already there. `trades_24h` remains NULL: neither venue returns a 24-hour trade count on the
objects the collector already fetches, and adding a per-market Data API call is not free at the
collector's current load. The paper will report it as not collected rather than as missing data.

**Consequence to state plainly.** The brief requires these to be "populated going forward, never
backfilled by reconstruction", and that rule is kept. Forecast rows written before today therefore
have NULL covariates, and the heterogeneity analyses that use them — the pre-registered exclusion
and stratification work is unaffected, but any depth-, volume- or hour-conditioned split is not —
run on the window from 2026-08-10 to the 2026-12-31 freeze, not on the full collection period. That
window is stated with each such result rather than left implicit, and no covariate is reconstructed
for earlier rows even where the archive would technically permit it: a reconstructed covariate is a
different measurement from a frozen one, and mixing them silently is the failure this rule exists to
prevent.

Nothing else changes: no hypothesis, outcome, statistic, honesty tier or exclusion rule.

**Addendum 9.13 (2026-08-10).** H1's primary statistic was never being computed, and its realized
sample size is far smaller than this plan assumed. Both facts are recorded here, before the
confirmatory analysis, because the second is the one that matters for what this paper can claim.

**The measurement gap.** §2 states H1 over horizon buckets — `m1_debiased` and `m1_hier@polymarket`
beating `m0_market` "on paired Brier skill in the ≥30-day horizon buckets". `run_eval`'s dimensions
were model × venue × category × window, with no horizon dimension at all, so no row in `eval_runs`
has ever corresponded to H1's stated stratification. Everything downstream that keys off an
`eval_runs` row — the anytime-valid confidence sequence, event-cluster counts, honesty tiers, the
report — therefore never covered the primary hypothesis either. Fixed on this date: horizon buckets
are scored through the same machinery as every other cell, under `window_label` suffixes
(`all_time_h_30to90d` and so on) that sit alongside primary rows and never overwrite them.

**What computing it revealed.** Resolved paired forecasts on Polymarket, by M1's own horizon
buckets, in **event clusters** (the unit §7's honesty tiers count):

| model | <7d | 7–30d | **30–90d** | **>90d** |
|---|---|---|---|---|
| `m1_debiased` | 993 | 316 | **33** | **22** |
| `m1_hier@polymarket` | 771 | 267 | **13** | **18** |

H1's pre-registered stratum holds **13–33 clusters against this plan's own 200-cluster INSUFFICIENT
floor** — roughly an order of magnitude short, while the short-horizon buckets it is *not* stated
over are well populated. The cause is structural rather than operational: a forecast enters the
≥30-day stratum only once its market resolves, which for that stratum is by construction ≥30 days
later, and the confirmatory window closes 2026-12-31.

**What this plan commits to.** No change to H1, its statistic, or the honesty tiers: the discipline
that matters here is reporting the tier the realized n earns. On present trajectory **H1 will be
reported as INSUFFICIENT DATA in its own pre-registered stratum**, and the paper will say so plainly
rather than substituting the well-populated short-horizon buckets, which test a different claim —
substituting them after seeing which strata filled would be precisely the specification-fitting this
plan exists to prevent. The horizon-bucket table will be reported in full, including the
short-horizon cells, so the reader sees both the result and why the stratum H1 names is thin.

Recorded now rather than at write-up so that the shortfall is a pre-registered expectation, not a
post-hoc discovery. Had the statistic been computed when the pipeline was built, this would have
been visible months earlier.

**Addendum 9.14 (2026-08-10).** Correcting addendum 9.13's projection, filed the same day. 9.13
concluded that "on present trajectory H1 will be reported as INSUFFICIENT DATA in its own
pre-registered stratum". That conclusion was reached by reading a stock of resolved observations
without checking how old the stratum was, how fast it was filling, or how many open markets could
still enter it. All three were measurable, and all three say otherwise.

**The stratum is ten days old, not starved.** The earliest Polymarket forecast in the ledger is
2026-07-02. A forecast can enter the ≥30-day stratum only once its market resolves at least thirty
days after it was written, so the first such observation could not exist before about 2026-08-01.
In the ten days since, the stratum has accrued **79 event clusters** — roughly eight per day.

**The pipeline is large enough.** Polymarket markets currently in the forecastable universe whose
end dates fall at least thirty days from now and before the freeze number **1,159 event clusters**
— 611 that would land in the 30–90d bucket and 548 in >90d. Both exceed this plan's 200-cluster
INSUFFICIENT floor several times over, and the daily accrual rate is consistent with that pipeline.

**Two corrections of record, not of specification.** First, 9.13's per-bucket table (13–33 clusters)
was computed with `end_date_iso − forecast_ts`, while the statistic actually implemented and stored
in `eval_runs` buckets on `resolved_ts − forecast_ts`, matching `m1_resolved_rows`. The stored
statistic is the one this plan refers to; 9.13's table is superseded and should not be quoted.
Second, the expectation 9.13 recorded is withdrawn: on present measurement H1 is expected to clear
the 200-cluster floor in both ≥30-day buckets well before 2026-12-31.

**What does not change, and is the point of filing this rather than editing 9.13.** The commitment
stands exactly as written: H1 is reported at whatever honesty tier its realized n earns, with no
substitution of the well-populated short-horizon buckets, whichever way the n turns out. This
addendum revises a *sample-size forecast* on better measurement, before any skill result in the
stratum has been examined — not a specification, and not a threshold.

**The residual risk is timing, not sample size.** Those 1,159 clusters count only if their markets
resolve on or before 2026-12-31, and prediction-market end dates slip. Accrual against this ceiling
will be tracked monthly and reported with the result, so a shortfall appears as a measured slippage
rather than as an unexplained thin cell.

**Addendum 9.15 (2026-08-11).** Phase 15's crowd-size covariates, audited and partly closed.

The phase lists three — "Metaculus forecaster counts, Polymarket holder counts (Data API), Kalshi
open interest — stored with snapshots" — and none was being stored. Their status now:

- **Kalshi open interest: collected from today.** The collector already fetches the field (it tiers
  on it since addendum 9.8), so storing it in the snapshot rows costs no request. Forward-only,
  like every other Phase 15 covariate: earlier partitions keep NULL and nothing is reconstructed.
- **Metaculus forecaster counts: will never exist.** Addendum 9.4 records that Metaculus access was
  declined; this covariate goes with it.
- **Polymarket holder counts: not collected, and this is a decision rather than an oversight.** They
  require a per-market Data API call, and the collector sustains ~9.6 req/s against its budget
  already. The paper will report them as not collected rather than as missing data — the same
  treatment `trades_24h` gets under addendum 9.12.

**Consequence.** Any crowd-size-conditioned analysis is Kalshi-only, on the window from 2026-08-11
to the freeze, and will be reported with that scope stated rather than implied. No hypothesis,
outcome, statistic, honesty tier or exclusion rule changes.

Recorded because the audit that found this also found `m3b_direct` — the §6 experiment comparing
deterministic aggregation against a direct LLM probability — has produced zero forecasts to date. It
is specified as optional and is not part of any pre-registered hypothesis, so its absence changes
nothing in this plan; it is noted here so that its absence is on the record rather than discovered
at write-up.

**Addendum 9.16 (2026-08-11).** `m3b_direct` begins producing forecasts today. §6 of `CLAUDE.md`
specifies it as an optional experiment — the same LLM states a probability directly instead of the
deterministic aggregator computing one from extracted evidence — and describes the comparison as "a
genuinely useful result either way". It had been fully implemented and never wired into the model
list, so it had produced zero forecasts since the project began.

**What makes it a clean comparison.** M3b runs on the *same* markets as M3 (it shares M3's target
list), reads the *same* dossier M3 has just written for that market, and calls the *same* model. The
arms therefore differ in one thing only: whether the final probability comes from
`news/aggregate.py`'s deterministic log-odds arithmetic or from the LLM stating a number. It is
appended after M3 in the model list so it reads today's dossier rather than a stale one, and it is
not in `POOLABLE` — a comparison arm must not feed the ensemble that is partly being compared.

**Standing under this plan.** M3b gates no pre-registered hypothesis and is not a primary or
secondary outcome; it is an exploratory comparison reported as such. Like any new `model_id`
(`CLAUDE.md` §6's forward-only rule), it earns skill only from forecasts written after today and is
never scored against history that predates it — the append-only ledger enforces that mechanically.

**Cost and scope, stated because they bound what it can show.** One additional completion per M3
target, about 120 a day, with no additional retrieval; combined M3 + M3b spend is on the order of
$0.15/day against a $5.00 cap. Starting today, it has roughly 142 days to the freeze, so its
resolved-forecast count will be small — on M3's own observed resolution yield, plausibly tens of
event clusters rather than hundreds. It will be reported at whatever honesty tier that earns, and a
null result on this comparison is a reportable result, not a failed experiment.
**Addendum 9.17 (2026-08-18).** Kalshi's forecast coverage collapsed for six consecutive days,
2026-08-11 through 2026-08-16 inclusive, and is disclosed here rather than repaired.

**The measurement.** Distinct Kalshi markets receiving at least one forecast per day: 2,676 on
08-10, then **15, 20, 20, 20, 21, 23** on 08-11 through 08-16, then 2,021 on 08-17 and 1,936 on
08-18. Under one percent of the venue's normal coverage, for six days. It took the rest of the
pipeline with it: the models that forecast the whole eligible universe under guardrail 12
(`m0_market`, `m1_debiased`, `m2_baserate`) fell from ~3,300 rows a day to ~500, and
**`m5_nowcast` wrote exactly zero forecasts on all six days** — 5,167 of its 5,554 lifetime rows
are Kalshi, so the venue *is* that model's population, and `CLAUDE.md` §6 calls its class the
highest-conviction edge in the design.

**The cause, established by elimination rather than assumed.** Four candidate mechanisms were
measured and cleared. Universe membership: `universe_log` records 0 to 15 Kalshi exclusions a day
across the window, so nothing was being excluded at sync time. Snapshot availability: 3,242 to
3,538 distinct Kalshi markets were snapshotted on each of those days. Snapshot freshness: the last
Kalshi round before the 02:00 UTC forecast pass was stamped 01:58 on **every** day in the window,
an age of two minutes. Price validity: zero null `mid` values, with 2,164 to 2,458 markets inside
`forecast_price_bounds` daily.

What did change is the end-date guard added to `eligible_market_states` on 2026-08-10 — addendum
9.11's own remedy for forecasts written on already-ended Kalshi markets. Kalshi's universe sync was
still starving at that point (9.11: 82% of markets un-resynced for over three days), so
`end_date_iso` was stale across most of the venue and recorded dates that had already passed. **A
correct guard fired on incorrect data.** The survivors prove it: of the 20 Kalshi markets forecast
on 2026-08-14, 9 were `liquid` and 11 `tail`, and every one carries an end date between 2026-09 and
2029-11 — exactly the markets whose stale end dates had not yet elapsed. The same tier split on
2026-08-18 is 12 `liquid` and 1,911 `tail`. Coverage returned when the sync-rotation fix reached
this host on 2026-08-17; no forecastable Kalshi market carries a past end date today (0 of 3,920).

**Why it stayed invisible for six days.** The guard counts and logs its own exclusions
(`forecast: skipped markets already past their end date`). Nothing reads that counter, and the
host's journal retention is roughly sixteen hours under the collector's log volume, so by the time
anyone looked the evidence of the first five days was gone. This is a monitoring gap, not a
measurement one, and it is recorded here because the same shape will recur.

**Standing under this plan.** Not repairable. The ledger is append-only and forecasts cannot be
written retroactively without destroying freeze semantics — the point of the ledger. The window
**2026-08-11 to 2026-08-16 inclusive is therefore an exclusion window for any Kalshi-population
statistic**, excluded rather than down-weighted. The forecasts written inside it are not themselves
suspect — each was frozen against a fresh price like any other — but the ~20 markets a day that
survived are a *long-dated* subset selected by the very defect under discussion, so they must never
be read as a random sample of the venue. Kalshi's accrual toward H1 and H2 loses six days; the
realized counts will be reported as they stand, at whatever honesty tier they earn, with this
window shown rather than smoothed over.

**Addendum 9.18 (2026-08-18).** The M4 ensemble silently produced almost nothing on three days, and
its pairing timestamp changes from today. Both are disclosed here; neither is repaired.

**The measurement.** The ratio of `m4_ensemble` rows to `m0_market` rows is exactly 1.00 on every
day since 2026-07-29 except three: 2026-08-11 (173 against 733, 0.24), 2026-08-14 (7 against 524,
0.01) and 2026-08-16 (12 against 499, 0.02). On those days the ensemble has, for practical
purposes, no forecasts at all.

**The cause.** `run_forecast_job` makes two passes — the base models, then the ensemble — with M3's
LLM calls and the M6/M7 scans between them, 13 to 18 minutes. The second pass re-derived its own
eligibility, which re-applied guardrail 13's 15-minute liquid freshness window to prices the first
pass had already frozen. On days when the collector landed no snapshot round inside that gap, the
ensemble was offered almost no markets. This is a defect of the pipeline, not of the model: M4
pools rows the base pass has already written, so it must be offered the same markets and paired
against the same price those rows were paired against.

**A second finding of the same shape, affecting every day rather than three.** Because each pass
stamped its own timestamp, M4's `p_market_at_ts` was read up to 18 minutes after the prices its own
members had been pooled from. Every M4 row is internally consistent as written, but its paired
baseline was not the baseline its inputs saw — a quiet instance of exactly the pairing corruption
`CLAUDE.md` §7 calls the worst failure class in the system. The magnitude is bounded by 18 minutes
of price drift and is not estimated here; it is disclosed, not corrected in the ledger.

**What changes from 2026-08-18.** Both passes now share one eligibility view and one freeze
timestamp. M4 rows written from today therefore carry the base pass's timestamp and its price;
rows written before today carry their own write time and a price up to 18 minutes younger than
their inputs. **This is a declared discontinuity in the M4 series at 2026-08-18.** A robustness
check re-running M4's paired skill on post-2026-08-18 forecasts alone is committed here, alongside
the primary estimate over the full window.

**Consequences beyond M4.** §8's shadow portfolio evaluates its entry rule on M4, so 2026-08-11,
08-14 and 08-16 produced no simulated entries either. Those three days are excluded from any
per-day shadow-portfolio statistic for the same reason as 9.17's window: the absence is a pipeline
artifact, not a decision not to trade.
**Addendum 9.19 (2026-08-18).** Kalshi's liquid tier is collected on its own five-minute cadence
from today. This restores markets to the forecast population that were being dropped silently, so
it is a population change recorded before it takes effect.

**What was wrong.** One snapshot job covered the whole Kalshi venue on one interval, so the liquid
tier inherited the tail's cadence. Measured 2026-08-17, a full round took about sixty minutes for
3,966 markets plus 461 order books — 1.2 requests/second against a configured ceiling of 8 — so
APScheduler dropped every second firing of the nominal thirty-minute interval and the realized grid
was hourly. Guardrail 13 allows a liquid-tier price fifteen minutes of age before a forecast may
not be paired against it. The two numbers are irreconcilable: measured on 2026-08-18, the median
snapshot age across Kalshi's 464 liquid markets was 50.2 minutes and **443 of the 464 failed the
freshness gate**, every night, silently. Those 464 are exactly the markets addenda 9.9 and 9.10
promoted onto the measured-depth bar — the subset this plan argued was the venue's most
informative.

**Why the round was slow, and what changed.** It was latency-bound, not rate-bound: every request
awaited the previous one, so the round reached 15% of the rate the limiter already permitted.
Snapshot rounds now issue bounded-concurrent requests (8 in flight for Kalshi), and the venue is
split into a five-minute liquid round and a thirty-minute tail round. **Politeness is unchanged and
is not a matter of judgement here:** the per-venue token bucket caps the request *rate* exactly as
before, so guardrail 8 holds identically at any concurrency; what changed is only how much of the
already-permitted rate a round can reach. Expected round costs at the 8 req/s ceiling: ~928
requests (~2 min) for the liquid round, ~3,500 (~7.3 min) for the tail — each inside its interval,
with headroom.

**Standing under this plan.** Two effects, both declared. First, Kalshi's liquid-tier snapshot grid
becomes 5-minute from 2026-08-18, against a ~60-minute grid before it; any analysis using
Kalshi snapshot spacing — H3's lead-lag work in particular — must treat 2026-08-18 as a grid
discontinuity and not pool across it. Second, and larger, roughly 443 Kalshi liquid markets a day
re-enter the forecastable population from today, having been absent since the tier was created.
Their absence was not random: they are the venue's deepest-book markets, so the pre-2026-08-18
Kalshi forecast population is biased *away* from its own liquid tier. Kalshi skill estimates
spanning the boundary must report this, and a robustness check restricted to post-2026-08-18
Kalshi forecasts is committed here alongside the primary estimate.

**Not applied to Polymarket.** The same latency-bound pattern exists there (the liquid round takes
~300s for 1,179 markets, 3.9 req/s against a ceiling of 10, which starves the tail to a ~7-hour
realized cadence with 1,508 of 2,393 tail markets past guardrail 13's 90-minute bound and 831
carrying no snapshot at all). The mechanism is now in place but its concurrency is left at 1 —
today's exact behaviour — pending a deliberate decision, which will get its own addendum. Recording
the measurement here so that the decision, whichever way it goes, is on the record before it is
made rather than after.
**Addendum 9.20 (2026-08-18).** The collector was discarding most of its own scheduled firings, and
the Polymarket tail's realized cadence was therefore about seven hours against a configured sixty
minutes. Fixed today; recorded because it changes the observation grid and the forecastable
population, not because the configuration changed.

**The measurement.** Over a 10h45m window on 2026-08-18 the collector completed **146 liquid
snapshot rounds and exactly one tail round**, against roughly eleven scheduled tail firings, with an
APScheduler misfire warning for each of the rest — and for every other collector job as well.
Consequences at forecast time, measured the same day: the median snapshot age across Polymarket's
2,393 forecastable tail markets was **410 minutes** against guardrail 13's 90-minute bound, 1,508 of
them failed that bound, and 844 carried no snapshot at all in the preceding two days. Those 844 are
not marginal: they include markets with $32M, $12M and $4M of recorded volume, and 485 of them had
been re-synced that same morning, so they were live and tracked and simply never reached.

**The cause.** APScheduler's default `misfire_grace_time` is one second: a firing whose scheduled
moment passes while the event loop is busy is discarded rather than delayed. A snapshot round issues
back-to-back requests for minutes at a time, so the loop is busy essentially always. The same defect
had already been found and fixed for the hourly `health_check` job in a 2026-07-14 audit, whose
comment states it exactly — "without an explicit override every firing was silently discarded as a
misfire" — but the fix was never generalized to the collector's own jobs. Every collector interval
job now carries a grace of one full period with `coalesce`, so a firing may run late but never
outlives its successor or replays a backlog.

**Standing under this plan.** Two declared effects, both from 2026-08-18. First, Polymarket's
tail-tier snapshot grid moves from a ~7-hour realized spacing to its configured 60 minutes; any
analysis of snapshot spacing, and H3's lead-lag work in particular, must treat this as a grid
discontinuity and not pool across it. Second, on the order of 1,500 Polymarket tail markets a day
re-enter the forecastable population, having been excluded by the freshness gate rather than by any
rule in this plan. Their absence was not random — it fell on whichever markets the starved tail
round happened not to reach — so the pre-2026-08-18 Polymarket tail population is a convenience
sample of itself. A robustness check restricted to post-2026-08-18 Polymarket forecasts is committed
here alongside the primary estimate, matching the commitments in 9.18 and 9.19.

**What was NOT changed.** `collect.snapshot_concurrency` remains 1. The round is latency-bound (3.9
req/s against a permitted 10), so raising it would cut the tail round further, but the misfire fix
alone restores the configured cadence, and the concurrency choice is left as a separate, deliberate
decision rather than folded into a defect fix.
**Addendum 9.21 (2026-08-18).** Polymarket snapshot rounds become concurrent today. This is a margin
decision, not a rescue, and it is filed separately from 9.20 because it is a choice rather than a
defect fix.

**Why it is being made.** 9.20's misfire fix restored the Polymarket tail to its configured
sixty-minute cadence, and the result was measured four hours later: median tail snapshot age fell
from 410 minutes to 71.4, and the share past guardrail 13's ninety-minute bound fell from 63% (1,508
of 2,393) to 2% (35). That is inside the bound but with very little to spare, because the round
itself still takes about twelve minutes at one request in flight — a market snapshotted early in a
round is already twelve minutes older than one snapshotted late, on top of the sixty-minute spacing.
A single slow or skipped round therefore pushes a large share of the tail past the bound and out of
the forecast population, which is precisely the failure mode 9.17 and 9.20 document. Raising
in-flight requests to eight brings the round to roughly five minutes and restores the margin.

**Politeness is unchanged, and this is not a judgement call.** The per-venue token bucket caps the
request *rate* exactly as before, so guardrail 8 holds identically; concurrency changes only how
much of the already-permitted rate a latency-bound round can reach. Measured sequential throughput
was 3.9 req/s against a configured ceiling of 10, so the ceiling — not the loop — becomes the
binding constraint after this change, which is the intended state.

**Standing under this plan.** The effect is a further tightening of the Polymarket snapshot grid on
the same date as 9.19 and 9.20. **All three changes land on 2026-08-18, so the analysis faces one
boundary rather than three**: any estimate whose population or grid depends on snapshot freshness —
H3's lead-lag work most directly — should treat 2026-08-18 as a single discontinuity and not pool
across it. The post-2026-08-18 robustness checks already committed in 9.18, 9.19 and 9.20 cover this
change as well; no additional check is introduced, because a fourth restriction to the same date
would report the same rows.
**Addendum 9.22 (2026-08-22).** M7's confirmed cross-venue pairs were mostly unusable, and the
repair grows that model's population. Recorded before the repair runs.

**The measurement.** `data/markets_map.yaml` holds 192 human-confirmed Polymarket-Kalshi pairs.
M7 has been producing forecasts on **101 markets a day** — and the gap is not selection, it is a
write defect. 113 of the 192 confirmed Kalshi legs (59%) exist in `markets` only as stubs: category
NULL, `active = 0`, `tier = 'ignored'`. `tracked_kalshi_markets` selects on `active = 1`, so those
legs were never snapshotted, and a pair whose external leg has no price cannot contribute an
external quote to the pool.

**The cause.** The confirmation path's `backfill_kalshi_metadata` wrote four columns with a bare
`UPDATE` — question, description, end_date_iso, last_synced_ts — while the collector's own path
writes the full row through `kalshi_market_row` plus a tier assignment. The two write paths drifted,
and the backfill's early return (`skip if question is set`) then made the half-written state
permanent: once its own partial write had filled `question`, every later call left the row alone.
The same NULL category is what crashed the monthly `lab learn` on this date (see the fix committed
alongside).

**What changes.** The backfill now writes the same full row through the same builder, gating on
`category` rather than `question` so a genuinely collector-synced leg is still left untouched while
a stub gets completed. A bounded repair pass over already-confirmed pairs runs inside the existing
twice-daily pmxt-verify job, ahead of its LLM step, so it costs no tokens and drains the backlog
over successive runs rather than in one burst.

**Standing under this plan.** M7's population grows from roughly 101 markets a day toward the
confirmed set of 192, phased over the days the bounded repair takes. **This is recovery of coverage
that was always confirmed, never a change to the matching rule** — no pair is added, removed or
re-judged here; `markets_map.yaml` is untouched, and the propose-then-confirm contract (guardrail
16, §6 M7) is unchanged. The pairs entering the pool are exactly those a human had already
confirmed, in the order the repair reaches them, which is not a research-relevant ordering.

Two consequences to carry into the analysis. First, M7's coverage is **not comparable across
2026-08-22**: its per-day n roughly doubles, so any M7 statistic pooling across that date mixes two
coverage regimes. Second, the 113 recovered pairs have **no external price history before their
repair date**, so M7 forecasts on them begin then — H3's lead-lag work, which needs both legs'
histories, gains these pairs only forward. A robustness check restricted to post-2026-08-22 M7
forecasts is committed here alongside the primary estimate, matching 9.18-9.21.
**Addendum 9.23 (2026-08-22).** The null-control restriction now binds on every write path. This
removes markets from two models' populations, so it is recorded with the exclusion rule it implies.

**The measurement.** §3 keeps "a small random sample of sports markets in the ledger, forecast by
the cheap models only" — a seeded sample of 30, because sports are near-efficient and exist here as
a falsification check. That restriction was implemented in `eligible_market_states` and nowhere
else, and two models write outside it. Over the week to 2026-08-22, `m6_consistency` had written on
**62** sports markets and `m7_crossvenue` on **31**, against a sample of 30 — and **59 and 29 of
those respectively were markets the filtered path had never produced**, so they were not a superset
drawn the same way; they were unfiltered.

It showed up asymmetrically in scoring, which is how the two failures differ. M6's sports forecasts
land in the `null_control` window with **n = 420**, against roughly 170 for every model that goes
through the filter — a control population two and a half times too large and selected by the
coherence scanner rather than by the seed. M7 does not appear in that window at all despite 241
sports rows, so its sports forecasts were being scored in the ordinary per-category tables instead.

**Why M7's case is the graver one.** Its markets come from `markets_map.yaml`, i.e. from pairs a
human confirmed. Editorial selection reaching the null control is precisely what guardrail 12
exists to forbid, and the null control is the one place where an uncontrolled population destroys
the instrument rather than merely widening it: a control whose membership is not controlled is not a
control.

**What changes.** A single predicate, `drop_null_control_outsiders`, now sits beside the sample it
consults and is applied by all three write paths. Non-sports markets are untouched, so the change is
a no-op on the overwhelming majority of every batch.

**Standing under this plan.** M6 and M7 lose sports markets outside the sample from 2026-08-22
onward. The rows already written stay in the ledger — it is append-only, and this plan repairs
nothing retroactively — but they are **excluded from every reported statistic**: sports-category
forecasts from `m6_consistency` and `m7_crossvenue` dated before 2026-08-22 are pooled neither with
the null control nor with the main per-category tables. This is an exclusion, not a robustness
check, because the population they were drawn from is not one this plan ever specified.

**Read alongside 9.22, same date, opposite direction.** 9.22 recovers confirmed cross-venue pairs
and grows M7's population; this shrinks it by the sports markets that should never have been in it.
They are independent — one is coverage that was always confirmed, the other is coverage that was
never sanctioned — and the net effect on M7's per-day n is the sum of both, so neither should be
read as explaining the other.
**Addendum 9.24 (2026-08-25).** H3 is reclassified from a primary hypothesis (§2) to an exploratory
outcome (§4). The decision is made on realized statistical power alone, filed before the
confirmatory window closes rather than reported as a failed test after it.

**The measurement.** H3 asks whether `m7_crossvenue`'s external pool leads Polymarket's own price.
Scored the way §3 requires — resolved, paired, clustered on `event_id` — M7 holds **1,594 rows that
compress to 13 event clusters**. The clustering is not a defect here; it is the plan working as
specified, because the same underlying event listed on two venues is one observation of the world.

**Why more time does not fix it.** This is a ceiling, not slow accrual. M7 forecasts roughly 101
markets a day drawn from 192 human-confirmed pairs (up from ~79 usable after 9.22's repair), and
those pairs concentrate onto few distinct events. The §7 floor for even a preliminary claim is 200
clusters. Eighteen weeks at the observed rate does not close a gap of that size, and neither would
doubling the confirmed-pair set — the binding constraint is distinct events, not pairs.

**What this decision is, and is not, based on.** It rests on `n` and nothing else. **No H3 skill
estimate was consulted in reaching it.** That statement needs one qualification to be accurate
rather than merely reassuring: M7 skill rows have been computed nightly into `eval_runs` since
2026-08-01 and are rendered in the weekly report, so the estimates exist and were available. The
claim here is the narrower and checkable one — the reclassification is a function of the cluster
count, which was knowable in August and is stated above, and not of any outcome. A reader who
doubts that can verify the arithmetic without reference to any skill number.

**What changes and what does not.** §2 and §4 are not edited; this plan is append-only and this
addendum governs. H3 continues to be computed, reported, and shown in the same tables as everything
else, at whatever honesty tier its realized `n` earns — which on present trajectory is INSUFFICIENT.
It simply no longer gates a confirmatory claim, and no H3 result will be presented as one. The
cross-venue collection behind it (Phase 10, the markets_map propose-then-confirm flow, guardrail 16)
is unaffected and continues.

**Why file it now.** The power was knowable on 2026-08-23, when it was measured. Reporting a failed
confirmatory test at the freeze, on a hypothesis whose insufficiency was established four months
earlier and left standing, would be a worse account of what happened than this one.
**Addendum 9.25 (2026-08-26).** Kalshi had no event clustering at all, so every confidence interval
computed for that venue was computed over an inflated `n`. Corrected today, in the direction of
LESS power, before the confirmatory window closes.

**What was wrong.** §3 makes the event cluster the unit of analysis, and `CLAUDE.md` §7 states the
reason plainly: "clustering by venue-market would overstate n. Naive CIs would lie." Polymarket has
complied since Phase 16 — `universe.py::_link_negrisk_legs` groups a negRisk event's legs into one
shared `event_id` at sync time, and 72.8% of its resolved scored rows carry one. **Kalshi carried
one on 2 rows out of 28,634.** It had no equivalent mechanism: `KalshiMarket.event_ticker` is parsed
from every market response and appears exactly once in the whole codebase, in the field declaration.
It was never stored and never used.

This matters because Kalshi markets are overwhelmingly mutually-exclusive legs of ONE question:
`KXUSPPIYOY-26AUG13` is 24 threshold buckets of a single PPI release, all sharing one question
string; `KXPRESPERSON-28` is 18 candidates in one election. Each leg was counted as an independent
observation of the world.

**Magnitude, measured on the confirmatory window.**

| | as reported | correctly clustered | |
|---|---|---|---|
| Kalshi, all resolved | 1,649 (STANDARD) | **243** (PRELIMINARY) | ×6.8 |
| **Kalshi, H1's ≥30-day bucket** | 193 | **22** | ×8.8 |
| Polymarket, all resolved | 1,058 | 1,058 | ×1.0 |
| Polymarket, ≥30-day | 43 | 43 | ×1.0 |

**Verification, because a correction this large should not rest on a heuristic.** Three independent
checks. The grouping is semantically one event (identical question text across threshold legs; one
election across candidate legs). The asymmetry is structural, not incidental — Polymarket's 72,617
linked markets resolve to 6,445 events, of which all but 7 come from negRisk grouping. And the
derivation `SERIES-EVENT` from the ticker was checked against the live Kalshi API on **40 randomly
sampled tickers drawn from markets this lab has actually forecast: 40 matches, 0 mismatches**. That
last check is what licenses backfilling already-resolved markets, which the universe sync will never
revisit and whose grouping therefore could not be corrected later.

**Consequences for this plan.** H1's realized power is lower than any previous statement of it.
Kalshi's ≥30-day bucket is 22 event clusters, and its in-flight pipeline of ~606 markets is ~69
events, so that venue cannot reach even the preliminary tier by the freeze. **H1 now rests entirely
on Polymarket** — the venue the hypothesis actually names — where the counts are unchanged because
they were always correct. Kalshi remains useful at 243 clusters across the full window, which is a
legitimate preliminary tier; it is simply not what it appeared to be.

**Superseded results.** Every `eval_runs` row for a Kalshi venue written before today used the
inflated clustering, in `n`, in the cluster bootstrap and in the anytime-valid confidence sequence.
Those rows are superseded, not deleted — the table is a record of what was computed when. Any Kalshi
figure quoted from before 2026-08-26 must be recomputed before it appears in the paper, and no
Kalshi interval from that period should be read as valid.

**Direction, stated because it is the whole defence of this addendum.** This correction removes
power. It was found by auditing whether the study can answer its own questions, it makes the answer
worse, and it is filed before the freeze rather than discovered afterwards. A correction that moved
the other way would deserve far more suspicion than this one.

**Addendum 9.26 (2026-09-07).** Kalshi's forecast coverage collapsed again, this time because the
football season listed more sports markets than the venue's snapshot round could carry. From today
the venue-wide Kalshi rounds no longer snapshot sports markets outside the null-control sample. The
lost days are disclosed as an exclusion window; the change to what is collected is declared here
before it takes effect.

**The measurement.** Distinct Kalshi markets receiving at least one forecast per day ran 1,936 to
2,774 through 2026-08-29. Then: **10** on 08-30, 2,744 on 08-31, 2,384 on 09-01, and **12, 10, 12,
12, 33, 10** on 09-02 through 09-07 — under half a percent of normal coverage for six of the last
nine days. Forecast rows for the venue fell from ~12,000/day to 50–165.

**The cause, measured rather than inferred.** Kalshi listed **13,290 new sports markets between
2026-08-20 and today** — `KXCYCLINGSTAGE` (1,000), `KXNCAAFCFPPOLL` (360), `KXMLBTEAMTOTAL` (315),
`KXNCAAFTOPCFPPOLL` (300), `KXNFLRACE` (240) and their season-mates, median volume 0. Sports is now
**11,625 of the 16,673 Kalshi markets in the snapshotted tiers, 69.7%**. The tail round grew with
it and stopped fitting its own cadence: the last three rounds covered 16,046–16,181 markets and
completed at 23:42, 01:08 and 02:39, **~86 minutes apart against a configured 30-minute interval**,
so APScheduler skipped two of every three firings (`max_instances=1`) and the realized grid sits at
guardrail 13's 90-minute tail bound with no margin at all. The forecast pass of 2026-09-07 02:00
duly logged **4,959 markets skipped on stale prices** and ran on 1,571 eligible markets across both
venues. The starved markets are economics and politics: `eligible_market_states` applies the sports
filter before the freshness check, so not one of those 4,959 was a sports market.

**Why this is a policy failure rather than a capacity failure.** Under §3 sports is the null
control — "a small random sample of sports markets in the ledger, forecast by the cheap models
only", 30 markets, seeded. The other 11,595 can never receive a forecast: the eligibility filter
drops them first, and addendum 9.23 closed the two write paths that had been reaching them anyway.
The venue's snapshot budget was therefore spending ~70% of itself on a population this plan
excludes by construction, and the cost of that was paid by the population it studies.

**The change.** From today the venue-wide Kalshi snapshot rounds (`liquid` and `tail`) collect only
markets that can become forecast targets: everything except sports markets outside the sample. The
tail round drops from ~16,050 markets to ~4,400, which at the throughput just measured (3.1
markets/s) is ~24 minutes — inside its 30-minute interval. Reversible in configuration
(`universe.null_control.snapshot_unsampled: true` restores the previous behaviour exactly).

Three scope limits, stated because each was a live alternative:

- **The per-pair high-frequency job is not filtered.** Phase 17 item 3's confirmed cross-venue
  pairs are snapshotted from an explicit id list, and their value is the price series itself (H3's
  lead-lag, now exploratory under 9.24), not any forecast written against it. A confirmed sports
  pair keeps both legs at full cadence.
- **The resolution watcher is not filtered.** It still polls every Kalshi market, sports included,
  so outcomes keep accruing for M1/M1.x recalibration fits and M2 base rates. That dilutes the
  watcher's round-robin by the same ~70%; it is measured, left alone, and named here rather than
  bundled into a change whose research consequences would be different in kind.
- **Polymarket is unchanged.** Its rounds are not budget-bound after addendum 9.21 (tail median
  snapshot age 34.4 min against a 90-minute bound), so the same filter would buy nothing there —
  and it would cost something. Tiering falls back to venue-reported figures when a market has no
  recent snapshot to measure depth from, and Polymarket's fallback tail bar is real
  (`min_liquidity` 1000 / `min_volume` 5000), so an unsampled sports market could drop out of
  `tier IN ('liquid','tail')`, leave the pool the control samples from, and never be drawable
  again. Kalshi's fallback tail bar is `min_volume: 0, min_open_interest: 0`, which every market
  clears, so on that venue the pool after this change is the pool before it. **The null control's
  sampling frame is unchanged, and a test asserts the config fact that makes that true.**

**What is lost.** Snapshot history for ~11,600 Kalshi sports markets stops today and cannot be
recovered later; a future study of sports-market microstructure on this venue would not find it
here. That is the trade being made, and it is made in favour of the population this plan exists to
measure. One second-order effect is bounded and disclosed: when a market enters the null-control
sample as an older one resolves, it carries no fresh snapshot until the next round, so its first
forecast may be skipped under guardrail 13 — at most one round (5 or 30 minutes) per market
entering the sample.

**Standing under this plan.** Not repairable, for the same reason 9.17 was not: the ledger is
append-only and forecasts cannot be written retroactively without destroying freeze semantics.
**2026-08-30 and 2026-09-02 to 2026-09-07 inclusive are exclusion windows for any
Kalshi-population statistic**, excluded rather than down-weighted. The 10–33 markets a day that
survived are not suspect individually — each was frozen against a fresh price — but they are
whichever markets the starved round happened to reach in time, a subset selected by the defect
itself, and must never be read as a random sample of the venue. Kalshi loses seven days of accrual
toward H1 and H2, on top of 9.17's six.

**On the monitoring gap 9.17 predicted would recur.** It recurred, and this time the instrument
saw it: the coverage watchdog added on 2026-08-26 fired on 2026-09-02, its first real day, naming
six model/venue series at 0% of their own fourteen-day median. What it could not do is tell anyone
— §12 forbids this lab from notifying, and the watchdog writes to the log and to `lab status`,
which a travelling operator reads when they read it. Five of the seven lost days are that gap, not
a detection failure. The dead-man heartbeat of Phase 18 covers a dead collector; nothing covers a
collector that is alive and collecting the wrong markets.

**Direction.** This change removes data (sports snapshots) and the disclosure removes power (seven
more days of Kalshi). Both were found by auditing whether the study can still answer its own
questions, both make the answer worse before they make it better, and both are filed before the
freeze.

**Addendum 9.27 (2026-09-08).** The Kalshi universe sync's rotation was not slow, it was stalled,
and had been re-walking the same dead series while the ones carrying live markets went unvisited
for a measured median of sixteen days. The rotation is repaired today. This is a research-population
change and is declared before it takes effect.

**The measurement.** 837 Kalshi series are known to this lab; 606 of them carry at least one open
market. Hours since that series was last synced, over those 606: **p25 209, p50 383, p75 543,
p90 770, max 1,199**. The configured rotation — `max_series_per_sync: 40` hourly, of which ~32 go
to known series — implies a **26-hour** worst case. The realized median is **14.7× that**, and
three consecutive cycles on 2026-09-07 (12:05, 13:05, 14:05 UTC) logged `markets_seen: 0`.

**The mechanism, established in code rather than inferred.** `_series_sync_order` ordered known
series by `MAX(last_synced_ts)` taken from that series' market rows, but `sync_kalshi_universe`
fetches `markets_for_series(status="open")` and upserts only what comes back. A series that returns
no open markets therefore advances nothing at all: its key stays frozen, it remains the oldest, and
because Python's sort is stable it is handed back at the head of every subsequent cycle. With 231
dead series against ~32 known slots the known half of the rotation was not merely slow — it was
**fixed**. The fetch-failure path had the same shape: `continue` with nothing stamped.

**The change.** A dedicated cursor table (`kalshi_series_sync`) records what the sync *attempted*,
stamped for every series it walks — productive, empty, or failed alike — and the rotation orders on
that. This is the identical rule `collect/resolutions.py` adopted on 2026-07-25 after the same class
of wedge, whose own docstring says a candidate must rotate to the back whether or not the fetch
achieved anything. The cursor deliberately does **not** re-key the known/unseen partition: doing so
would migrate every discovery-touched series into `known`, growing it from 837 toward the venue's
11,547 — which is precisely the never-seen-first rotation `_series_sync_order`'s own docstring
records shipping and failing on 2026-08-10. A test asserts the partition stays derived from the
markets table alone. `SCHEMA_VERSION` moves 13 → 14 for the added table; the change is additive, so
nothing in §13's forecast contract breaks, but the value is carried in every paper-export manifest
and is bumped rather than left drifting.

Two companion defects of the same class are fixed alongside, because restoring the rotation feeds
both: `unresolved_kalshi_markets` had `LIMIT` with no `ORDER BY` and `watch_kalshi_resolutions`
stamped nothing, i.e. exactly the head-of-scan wedge the Gamma watcher was cured of in July; and
`unresolved_closed_markets` had never been narrowed to Polymarket after Phase 10, so it was
fetching — and stamping `resolution_checked_ts` on — rows from venues Gamma has never heard of.

**Population effects, in order of how much they matter.**

1. **Discovery, and this is the one that matters.** A series unvisited for a median of 383 hours
   contributes no newly listed market to the universe at all. Those markets are not excluded by any
   rule in this plan; they were never seen. The size of that omission **cannot be quantified from
   our own data** — we cannot count what we never observed — which is itself the honest statement
   to record. From today the worst case is bounded at ~26 hours.
2. **Re-admission through fresh end dates.** `end_date_iso` freshness gates the hard end-date
   exclusion in `eligible_market_states`, the guard whose firing on stale dates produced the
   six-day blackout of 9.17. Measured today the affected population is **48 markets** (39 sports,
   9 other) and the nightly counter reads 156 — small, and declared anyway, because it is the
   mechanism rather than the current magnitude that earned 9.17.
3. **Re-tiering.** `tier` is written only by this sync, and it selects the snapshot round, whether
   a book is fetched, guardrail 13's freshness bound, shadow-portfolio eligibility, M3's top-K
   frame and M7's proposal set. Kalshi markets whose tier has been frozen for weeks will move on
   the changeover date; expect a step change in shadow entries.
4. **A pre-registered covariate changes staleness, not meaning.** `volume_24h` is frozen verbatim
   into every forecast row. Its staleness falls from a ~383-hour median to a ~26-hour worst case.
   It is *not* becoming a true 24-hour figure — no achievable rotation delivers that — and any
   heterogeneity analysis conditioning on it is discontinuous here.
5. **Silent horizon re-bucketing.** Current end dates will select different M1/M1.x recalibration
   curves through the horizon buckets, with no counter firing anywhere.
6. **Null-control cohort churn.** `null_control_ids` re-draws its seeded sample over the whole
   sorted eligible pool, so any pool change rotates the forward cohort on that date. Historical
   scoring is unaffected: `null_control_ids_by_venue` reads membership off the ledger.
7. **Scoring accrues marginally faster.** Narrowing the Gamma watcher returns 1,009 of its 10,944
   candidate rows (961 Manifold, 48 Kalshi) to Polymarket — 9.2% of each cycle. The `lab status`
   backlog figure drops by that amount on the changeover date: **that is the number becoming true,
   not the backlog draining.**

**Two numbers corrected before they entered this record.** An intermediate analysis put the Gamma
watcher's foreign-venue pollution at 18,079 rows and the stale-end-date population at 215. Measured
against the live database on 2026-09-08 they are **1,009** and **48**. The defects are real; those
magnitudes were not, and the smaller ones are what this addendum asserts.

**What was deliberately not done.** `max_series_per_sync` was not raised in the same change — the
cursor fix alone restores the designed ~26-hour rotation, and buying below 24 hours is a guardrail-8
politeness decision that needs the newly measured `cycle_seconds` in hand first; v2.12 held
`snapshot_concurrency` at 1 through five fixes for exactly this reason. Sports series were not
deprioritised to buy rotation speed: the sync is the only path by which a Kalshi market enters the
universe at all, so starving it would starve the pool the null control samples from, and a series
visited every ~80 hours would ingest only the markets open at that instant — length-biasing the
placebo toward long-horizon markets, which is 9.17's own reasoning turned on the control itself.
The discovery half of the rotation is wedged the same way on a key we do not control (unseen series
ordered by the venue's `last_updated_ts`); fixing it would sweep ~10,700 dormant series and expand
the universe, so it is recorded as measured-and-unchanged rather than folded in here.

**Direction.** This one cuts the other way from 9.26 and 9.17: it *adds* observations rather than
removing them, which is exactly why the discontinuity is declared in advance and in this much
detail. A change that quietly increases n is more dangerous to a pre-registered study than one that
reduces it.

**Addendum 9.28 (2026-09-08).** The 2026-09-06 ledger commitment was committed on time and
published late. This is a disclosure, not a change to any hypothesis, exclusion rule or statistic.

**What happened.** The weekly replication export (`docs/paper_exports/YYYY-MM-DD.jsonl`, §15's
"replication export", automated since v2.8) is a full cumulative re-dump of every resolved
forecast, so it has grown roughly linearly: 0.67 MB on 2026-07-10, 52.7 MB on 08-10, 95.9 MB on
08-30, and **115.1 MB on 09-06**. GitHub rejects any file over 100 MB with a pre-receive hook, and
that hook scans **every blob in the pushed range** — so the oversized export inside commit
`5ae68dc` refused the entire push, taking with it the M7 markets-map commit and, the point of this
note, the **ledger commitment for 2026-09-06**. `config.yaml` states in as many words that an
unpushed commitment on a public repo verifies nothing, and §7 of this plan sets the precedent that
gaps in the commitment record are documented rather than papered over. The value of a
pre-registration is its timestamp, so the lapse is recorded here: committed 2026-09-06, published
2026-09-08, cause as above.

The failure was silent for the same structural reason v2.12's ledger-push defect was:
`gitutil._is_non_fast_forward` matches on the literal `[rejected]` plus a divergence reason, and
GitHub's `! [remote rejected] … (pre-receive hook declined)` is a different rejection class, so the
job logged `pushed: false` and returned normally. That is the same "logs it and returns" shape
recorded three times before in this project; it is named again here rather than treated as new.

**What changes.** From 2026-09-13 the weekly snapshot is written as `YYYY-MM-DD.jsonl.gz` with a
matching `.jsonl.gz.meta.json`. Measured on the 2026-08-30 export, gzip -9 gives **16.5×**
(96.1 MB → 5.8 MB), which stays inside the limit through the 2026-12-31 freeze even at the highest
weekly growth rate yet observed (+30.3 MB over 08-10→08-18). The **dataset is unchanged** — same
rows, same fields, same query, same manifest; only the container differs, and a test asserts the
gzip round-trips to exactly the uncompressed row stream. The stream is written with `mtime=0` and a
fixed internal filename so two exports of identical rows are byte-identical, which §15's
re-derivability claim requires.

**Everything through 2026-08-30 stays plain `.jsonl` and stays published, unrewritten.** This
repository's history *is* the pre-registration record; rewriting published commits to tidy a file
extension would damage the thing the record exists to provide. The consequence is a split series,
and it is stated in `docs/paper_export_schema.md` with its date because a consumer globbing
`*.jsonl` would otherwise get a silent partial read.

**Not done, deliberately.** Parquet compresses this data far harder than gzip (~70× on a measured
sample) and was rejected anyway: it changes the artifact type that §13/§15 committed to and that
every already-published file embodies, where gzip changes only the container. Git LFS was rejected
on this project's own 2026-08-25 policy — LFS is for what plain git cannot carry, GitHub never
garbage-collects LFS objects, and on a public repo every stranger's clone bills the maintainer's
bandwidth. Incremental exports and any narrowing of the export's row scope were rejected outright:
the first changes the dataset contract, the second would be a research-population change smuggled
in as a size fix.

**One defect found while doing this and deliberately left open.** The manifest has no content
digest — `paper_export_manifest` returns only `{code_version, schema_version, generated_at,
row_count, fields}` — so a published export is verifiable today only by counting its lines, and
Phase 15's own acceptance criterion ("the `--paper` export round-trips through a validation
script") has no such script in the repository. Adding a digest is not a one-line change and must
not be folded into a size fix: it requires a deterministic sort *inside the paper export only*,
never in the shared `resolved_forecast_rows`, whose row order feeds an **unstable** `argsort` in the
per-cluster resolution ordering — so a well-meaning `ORDER BY` there could move the pre-registered
anytime-valid confidence sequence with no change in data. It is filed here so it is not lost, and
so that the current, weaker verifiability of the published exports is on the record.

**Addendum 9.29 (2026-09-08).** The other half of the Kalshi rotation is unwedged today, and it
expands the observed universe. Filed separately from 9.27 because that was a defect fix and this is
a choice, taken with the numbers 9.27's own instrumentation produced.

**What was wrong.** 9.27 repaired the ordering of *known* series. The discovery slice — 20% of each
cycle, reserved for series we have never seen — was wedged the same way on a key we do not own:
unseen series were ordered by the venue's `last_updated_ts` alone, so a series that yielded nothing
kept whatever position that key gave it forever and the slice re-walked the same head every cycle.
The pool it exists to sweep is ~10,700 series (Kalshi lists 11,564; 837 are known to us). The
attempt cursor now orders that half too: never-attempted series first, keeping their recency
ordering among themselves; already-walked ones rotate to the back.

**Why this needs an addendum when 9.27's half arguably did not.** No configured policy changes —
the 20% discovery budget, the category map, the exclusions and the tiering thresholds are all
untouched, and this only makes an existing allocation function. But the *effect* is that markets
which were never observed will now be observed, and a pre-registered study does not get to call
that a mere bug fix. **The universe grows, gradually, from today.**

**Scale, so the growth is not mistaken later for a data anomaly.** With `max_series_per_sync` at
48 (below), 9 discovery slots per hourly cycle sweep ~10,700 unseen series in roughly **50 days** —
so the first full pass completes near the end of October, against a 2026-12-31 freeze. Most of that
pool is dormant: this venue lists ~10,500 series against the ~285 that were carrying open markets
when the rotation was first studied, so the great majority of those cycles will return nothing.
Newly discovered markets are forward-only by construction — they receive forecasts from their
discovery date and are never backfilled — so any time-trend analysis must treat discovery date as a
covariate rather than assume a stationary population.

**`max_series_per_sync` 40 → 48**, in the same change and stated as its own decision. This is a
guardrail-8 politeness question, not a defect, and it was deliberately not folded into 9.27 —
v2.12 held `snapshot_concurrency` at 1 through five consecutive fixes for exactly this reason. It
is taken now because 9.27's new `cycle_seconds` counter answered the only open question: the first
measured cycle completed 40 series in **61 seconds** against a 3,600-second interval, so runtime
was never the binding constraint. 48 gives ~39 known slots, i.e. a **~21.5-hour** worst-case
rotation over 837 known series instead of ~26. In request terms this is 48 an hour, about 0.013
req/s against a ceiling of 8 — the limiter cannot notice.

**A third, smaller correction, and it moves 9.27's own arithmetic.** The first post-fix cycle
logged `series: 40` but stamped only **33** distinct series: a series returned under more than one
configured category appeared twice in the candidate list and was fetched twice. So the cap was
budgeting *slots*, not series, and the "~26-hour rotation" 9.27 asserts was really ~31. Candidates
are now deduplicated before partitioning, which restores the arithmetic that addendum states.

**What to watch, named in advance.** The snapshot round is the constraint this touches: the Kalshi
tail is 4,551 markets and ~15–27 minutes against a 30-minute interval after 9.26, and newly
discovered non-sports markets add to it. 9.26's filter protects against a repeat of the sports
flood — unsampled sports are never snapshotted whatever discovery finds — but a large economics or
politics discovery could still crowd the round, and the coverage watchdog plus the new sync-rotation
line in `lab status` are where that would show.

**Direction.** Like 9.27 and unlike 9.26, this one adds observations. That is the direction that
deserves more suspicion, not less, in a study whose claims rest on a pre-registered population —
hence the scale estimate, the completion date, and the forward-only caveat stated here rather than
discovered in the data later.
