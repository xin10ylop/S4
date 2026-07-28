# 08 — The 5m decision

**Date:** 2026-07-28 · **Mode:** PAPER throughout. No real money has ever been at risk.
**Inputs:** `audit/C1_fetch.md` (data), `C2_harness.md` (harness + control), `C3_decisive_run.md`
(the run), `C4_sigma_capacity.md` (sigma + capacity), plus an independent adversarial verification
pass that rebuilt the replay from scratch and re-ran the decision itself.
**Supersedes:** the open question in `docs/07_scale_audit.md` §3 and §6.2.

---

## 1. THE DECISION

# DO NOT ENABLE the 5m family.

`strategy.close_snipe.allowed_families` stays `["1h"]`. No config change is made. No parameter is
re-tuned to get a different answer.

The rule was fixed before any fresh number existed and required **all three** conditions. One fails
outright, one passes as a coin flip, and one passes on the letter while being economically zero.

| # | pre-committed condition | threshold | measured (56 fresh days, shipped params, book age ≤ 5 s, $250 clip) | verdict |
|---|---|---|---|---|
| **(a)** | corrected fresh EV at the **shipped** parameters | ≥ **+3.0 ¢/share** | **+3.584 ¢/share** (verifier's independent rebuild) · **+3.487 ¢** (C3 harness) | **MET — as a coin flip** |
| **(b)** | **DAY-LEVEL** t-stat, clustered by UTC day | ≥ **2.0** | **t_day = 1.732** (verifier) · **1.702** (C3) · two-sided p = 0.088 | **NOT MET** |
| **(c)** | EV positive under the full stress stack | > 0 | **+2.476 ¢/share** — but **−$0.60/day** at $250 and **t_day −0.932** | **MET on the letter, zero in substance** |

**The number that decided it is (b): t_day = 1.732 against a bar of 2.00.**

Three facts make that failure worse than a near-miss, not better:

1. **t_day reaches 2.0 at exactly one setting: no staleness filter at all.** Scanned at every
   threshold from 0.5 s to 60 s, t_day sits between **1.70 and 1.94**. It only reaches **3.15** when
   the filter is switched off — i.e. when the run is allowed to fill against order books that a
   vendor outage had frozen for up to 39 minutes. **Only the contamination produces significance.**
   The 5 s threshold is therefore not load-bearing and was not chosen to flatter anything; the
   decision is identical at 2 s, 5 s, 10 s, 30 s and 60 s.
2. **Condition (a) passes by 0.58 ¢ on a statistic whose 95 % day-bootstrap CI is [−0.36, +5.90] ¢.**
   P(true EV < 3.0 ¢) = **0.53**. Drop the single best of 56 days and (a) fails at +2.445 ¢; drop
   the best three and it is +1.736 ¢.
3. **On the 19 most recent, cleanest, most deployment-relevant days (2026-07-08 → 07-26), (a) also
   fails: +3.139 ¢ at t_day 0.917** on the verifier's numbers (+2.866 ¢ / 0.873 on C3's).
   **Post-break (2026-06-27 onward): +1.892 ¢, t_day 0.665.**

A negative result was a permitted outcome of this exercise. This is that outcome. The pre-committed
rule was applied as written, and it is not being renegotiated.

---

## 2. What the fresh data actually showed

### 2.1 Coverage obtained — nearly 3× what was asked for

| | |
|---|---|
| **Days** | **56** — 2026-06-01 → 2026-07-26 (~20 were requested) |
| **June** | **all 30 days.** The "no 5m book data for June at all" blind spot every prior analysis cited was a gap in *our local vault*, not at the vendor. It is now closed. |
| Markets | 16,121, **100 % resolved** |
| Quote tapes | 32,238 of 32,242 possible (99.99 %), 425,743,161 rows, 6.12 GB |
| Book depth (25 levels) | 3,986 candidate windows, 113,954,418 rows, 3.30 GB, **zero fetch failures** |
| Chainlink | 4.64 M reports with `server_timestamp_us` present on every row, 0 negative publish lags |
| Total | 9.66 GB |

The fetched Chainlink feed was confirmed to *be* the settlement source: applying the resolution rule
reproduces Polymarket's own `result_id` on **15,619 / 15,622 = 99.98 %** of evaluable markets.

Two June days (**06-10, 06-11**) are unusable — Chainlink coverage 80.5 % and 49.5 %, with 3.6 h and
8.1 h holes. Seven more days are partially degraded. **Excluding them makes the result worse, not
better** (§2.5), so the headline is not being propped up by bad-data days.

### 2.2 The contamination was real, it was found, and it is worse at scale than in the 5-day sample

The verifier's original diagnosis — that 7 of A3's 108 fills executed against books frozen by a
vendor outage on 2026-07-21 — is confirmed for the fourth independent time and generalises badly.

Raw, with no staleness filter: **938 fills, +5.622 ¢/share, $14,686 total P&L.**

| cut | fills | % of fills | **% of raw P&L** | win rate | ¢/share |
|---|---|---|---|---|---|
| **book age > 5 s (removed by the primary run)** | **59** | **6.3 %** | **67.7 %** | **96.6 %** | **+39.05** |
| book age ≥ 20 s | 43 | 4.6 % | 55.2 % | **100.0 % (43 for 43)** | **+42.26** |
| book age > 300 s | 27 | 2.9 % | 41.0 % | 100.0 % | +46.85 |

Book age is **violently bimodal** — median 0.004 s, p90 0.042 s, p95 10.2 s, p99 1,222 s, max
2,365 s. There is no continuum: a book is either live-streaming at millisecond cadence or it is
frozen for minutes. A 43-for-43 win rate at +42 ¢/share is not a strategy; it is the signature of
filling against a quote the market left minutes ago.

**The 2026-07-21 04:07 outage is not a one-off. The same signature recurs six times in 56 days** —
2026-06-10, 06-19, 06-24, 07-01, 07-10 and 07-21 — and **every episode with more than four fills
begins between 04:05 and 04:15 UTC**, which is midnight ET, Polymarket's daily rollover. 28 of the
43 stale fills close in the 04:00–04:59 UTC hour, an hour that carries 4.17 % of closes and
**11–14.7× its share of every frozen book** on two independent vendor channels. This is a recurring
*scheduled* disturbance, not bad luck.

**Consequence for any future live 5m deployment: a book-age guard and a hard 04:00–05:00 UTC skip
are mandatory prerequisites, not nice-to-haves.** Without them a live bot would have sent real
orders into books frozen for up to 39 minutes on five separate days in this 56-day sample.

### 2.3 A second contamination the age filter cannot see

The verifier found a residual failure mode nobody had looked for: books whose *timestamps* are fresh
but whose *ladder content* has not changed. Recomputing, for all 880 accepted fills, the time since
the top-4 ask levels last actually moved: median content-age **0.009 s** with a median of 3,934
distinct ladder changes in the preceding 60 s — the books really are live. But **2 fills sit on
ladders unchanged for 54.0 s and 65.6 s behind fresh timestamps, they went 2-for-2 at +51.6 ¢/share,
and they carry 5.8 % of the filtered P&L.** Removing them takes EV to **+3.475 ¢** and t_day to
**1.617**.

So: the hypothesis that "something similar is still hiding" is refuted at scale and confirmed in
miniature — and it moves the result further away from enabling.

### 2.4 The corrected headline

56 days, shipped parameters, strict Chainlink publication causality, book age ≤ 5 s at both the
signal and the fill instant.

| | **$250 clip** | **$25 clip** |
|---|---|---|
| signals | 1,770 | 1,770 |
| **fills** | **880–881** | 880–881 |
| **trades/day** | **15.7** | 15.7 |
| fill rate (signal → fill) | **49.7 %** | 49.7 % |
| **EV** | **+3.584 ¢/share** (C3: +3.487) | **+3.820 ¢/share** |
| win rate | 72.7 % | 72.7 % |
| per-trade t | 2.53 | 2.77 |
| **DAY-LEVEL t (the decision statistic)** | **1.732** (C3: 1.702) | 1.910 |
| **total P&L** | **+$5,042** | +$1,266 |
| **$/day** | **+$90.04** | **+$22.61** |

Day means: n = 56, mean +2.838 ¢, sd 12.258 ¢. Day-clustered sign-flip permutation under a no-edge
null: **p = 0.044 one-sided, 0.088 two-sided** — roughly a 1-in-23 result, not the 1-in-40 that a
t ≥ 2.0 bar demands.

**Throughput is 15.7 trades/day — not the 52 originally claimed and not the ~20 the brief planned
for.** 48–50 % of signals die because the book, re-fetched 1.5 s later, no longer clears `edge_min`.
That is honest two-book execution, and it is worse than the 34 % attrition assumed. The prefilter is
not hiding fills: replaying the *full* 288-window/day quote universe with no prescreen at all, only
**3 of 879 fills (0.34 %)** land on windows the prescreen rejected.

**Why $250 makes only 4× the money of $25:** 660 of 881 fills are cap-bound at $25 but only 192 at
$250. Past ~$25 the binding constraint is displayed depth inside 3 ¢ of best, not the clip.

### 2.5 The P&L is severely concentrated, and no robustness cut survives

| cut | EV ¢/share | t_day |
|---|---|---|
| **primary, 56 days** | **+3.584** | **1.732** |
| drop best 1 day | +2.445 | 1.510 |
| drop best 3 days | +1.736 | 1.085 |
| exclude 06-10 / 06-11 (the two unusable Chainlink days) | — | 1.524 |
| exclude all 9 C1-flagged degraded days | — | 1.016 |
| hard-skip the 04:00–04:59 UTC hour | — | 1.822 |
| **post-break (2026-06-27 →)** | **+1.892** | **0.665** |
| **July 8–26 only** | **+3.139** | **0.917** |

The top 3 of 56 days carry **61.6 %** of dollar P&L; the top 5 carry 86.0 %. The top 10 of 880 trades
carry **71.0 %**, and the top 20 carry **119.5 %** — meaning the remaining 860 trades are net
negative in aggregate. Only **35 of 56 days are positive**. The single best day, 2026-06-11
(+24.4 ¢), is a day on which we hold **49.5 %** of the oracle prints.

### 2.6 The edge decayed — measurably, at the day level

Re-running the 43 historical repo days through the *identical* code, parameters and staleness filter:

| period | days | trades/day | ¢/share | t_day |
|---|---|---|---|---|
| HIST Apr 2–15 | 14 | 33.9 | +2.03 | 1.75 |
| HIST Apr 16–30 | 15 | 26.9 | **+12.73** | **4.71** |
| HIST May 1–12 | 12 | 28.8 | **+15.42** | 2.34 |
| **HIST all 43 days** | 43 | 28.9 | **+9.22** | **4.79** |
| FRESH Jun 1–12 | 12 | 10.3 | +5.32 | 2.03 |
| FRESH Jun 13–26 | 14 | 17.4 | +6.27 | 0.74 |
| FRESH Jun 27–Jul 7 | 11 | 18.6 | **+0.00** | −0.32 |
| FRESH Jul 8–26 | 19 | 16.3 | +2.87 | 0.87 |
| **FRESH all 56 days** | 56 | 15.7 | **+3.49** | **1.70** |

Welch tests on **day-level** means: HIST Apr 16–May 12 vs FRESH all 56 days **p = 0.0070**; HIST all
43 days vs FRESH all 56 days **p = 0.0353**. **The +9 to +15 ¢ regime is gone, and its absence is
statistically significant.** The historical edge was real — this is decay, not a measurement error
in the old work.

The June blind spot is filled, and it did not produce a clean answer. It produced a **lower, noisier
level**. Independently, the fetch dated a **3.2× step-down in top-of-book update intensity to
2026-06-27** (196.8/s in early June → 51.1/s in late July, never recovering). The 5m edge does drop
across that date (+5.95 ¢ before, +1.73 ¢ after) but at p = 0.39 — daily noise (sd 12.3 ¢) swamps a
4 ¢ shift over 56 days. Notably, **depth did not fall** at the break (median fillable $77.86 before
vs $82.54 after), so whatever changed on 2026-06-27 is a *pricing* change, and nothing in this work
identifies its cause.

### 2.7 The causality trap — no lookahead, but a live trap sitting next to it

Every decision in every number above reads only Chainlink reports with `server_timestamp_us ≤ t`.
This is load-bearing: **97.3 % of the 11,958 decision instants would have used a report that had not
yet been published** under the obvious-but-wrong `timestamp_us ≤ t` rule (median day 1.63 s of
invented foresight; 2026-06-10 alone 462.8 s).

| | fills | ¢/share | **t_day** |
|---|---|---|---|
| **causal** (truth — everything reported here) | 880 | **+3.584** | **1.732** |
| non-causal (the trap) | 917 | +5.435 | **3.379** |

**The trap alone is worth +1.85 to +1.95 ¢/share and would single-handedly flip condition (b).** Two
independent tapes and three independent implementations agree on its size. Any 5m number produced
without this filter is inflated by roughly 2 ¢/share and should be discarded on sight.

---

## 3. Stress results — and where the edge dies

All rows on top of the corrected (age ≤ 5 s) baseline. Latency 3.0 s is applied **with the bot's own
`tau_lo = latency + 0.5` rule**, which correctly narrows the snipe band to [3.5, 5.0] → ticks
τ ∈ {5,4} and drops the τ=3 tick. This is the honest version, not the "keep the band and let orders
land after the close" version — verified directly against `bot/polybot/strategy.py`, not against a
port.

| variant | fills | trades/day | ¢/share | **t_day** | **$/day @$250** | $/day @$25 |
|---|---|---|---|---|---|---|
| **baseline: shipped + age ≤ 5 s** | 881 | 15.73 | **+3.487** | **1.702** | **+$89.88** | +$22.61 |
| fee 0.07 → **0.10** | 865 | 15.45 | +3.282 | 1.371 | +$91.07 | +$21.79 |
| **latency 1.5 → 3.0 s** (band narrowed) | **415** | **7.41** | +3.283 | **−0.503** | **+$14.05** | +$4.33 |
| 50 % of displayed depth | 881 | 15.73 | +3.442 | 1.674 | +$52.69 | +$18.58 |
| **ALL THREE TOGETHER** | **410** | **7.32** | **+2.476** | **−0.932** | **−$0.60** | +$0.05 |

**Where the edge dies: latency.** Not fees, not depth. Going 1.5 s → 3.0 s removes **53 % of all
fills** on its own and takes t_day from +1.70 to **−0.50**. Fees and depth are survivable
individually; the band-narrowing is not.

This matters more than a stress row usually would, for two reasons:

* The tau band's lower bound already has no measured margin. `docs/07` §5 item 3 recorded that two
  blocking `/book` fetches sit between the gate and the fill worker (δ_book median 0.315 s, cold
  0.791 s). A production server that is 1.5 s slower than this sandbox is not an exotic scenario —
  it is the scenario where 5m earns $14/day instead of $90/day, with a negative t-stat.
* On the 43-day historical tape the *same* knob **raised** ¢/share (+9.44 → +14.43). The two tapes
  genuinely disagree about the value of trading later. That disagreement is unexplained.

**Reading of condition (c).** Per-share EV stays positive (+2.476 ¢ at $250, +2.741 ¢ at $25), so on
the letter of the rule it is MET. In substance it is met vacuously: **−$0.60/day at the $250 clip**,
t_day **−0.932**, 95 % bootstrap on daily P&L **[−$43, +$41]**, and **−3.13 ¢/share over the most
recent 14 days**. The per-share number is positive while the dollar number is negative because the
losing fills are the ones that get size. There is nothing here to deploy.

Under the stress stack, by period: pre-break +$11.64/day · **post-break −$11.21/day** · **target
Jul 8–26 −$12.39/day** · **last 14 days −$24.97/day**.

---

## 4. Did the harness reproduce the verifier's control numbers?

**Yes — exactly, with no tolerance required. Everything downstream is therefore admissible.**

This check was the gate. Had it failed, the decision would have been NO by default.

| published control figure | reproduced |
|---|---|
| `+8.38 ¢/share, t = 7.56` after removing stale fills | **+8.3756 ¢, t_trade 7.5635** |
| `71 of 1,503 fills, 14 % of P&L` removed | **71 of 1,503, 14.04 %** |
| `+10.89 ¢, t = 10.33` at shipped params | **+10.8918 ¢, t_trade 10.3346** |
| the 5-day fresh sample's `7 stale fills, 100 % win, ~+46 ¢, 53.6 % of P&L` | **7 fills, 100 %, +46.40 ¢, 52.3 %** |

Four further independent checks, all clean:

1. **Property test:** 0 mismatches on 80,000 randomised inputs across all four functions ported from
   `bot/polybot/strategy.py` and `fill_engine.py`, with 1,089 of 20,000 inputs exercising the
   positive signal path (not just the NaN/None guards).
2. **Determinism:** re-running the control driver end-to-end regenerated all 9 result CSVs
   **byte-identically**.
3. **A second, independently-written replay** (own Chainlink loader, causal index, σ grid, strike
   rule, book tape, window gate, fill walk; imports nothing from the harness) agrees on 5 days:
   275 vs 275 signals, 153 vs 153 fills, identical EV, **0 field mismatches** at 1e-9 tolerance.
4. **The verifier's own from-scratch rebuild** of the whole 56-day decision agrees with the harness:
   1,770 vs 1,770 signals, 880 vs 881 fills, **0 mismatches** on tau, side, book age and strike,
   EV +3.584 vs +3.487 ¢, t_day 1.732 vs 1.702. **Where the two differ, the independent numbers are
   marginally MORE favourable to 5m — and they still fail (b).** The failure is not an artefact of
   one implementation.

Three defects were found in the process and are recorded rather than buried. None changes the
decision; two must be fixed before the harness is reused:

* **Latent causality defect (must fix before reuse).** `ChainlinkFeed.strike()` / `settle()` /
  `sigma_at_obs()` in `scripts/fresh5m/replay.py` are indexed on *observation* time and are not
  filtered by `server_timestamp_us`. 201 of 11,958 decision instants (1.68 %) use a strike print that
  had not been published. Measured P&L impact on this run: **exactly $0** (the trade tape is
  identical to the cent, 880 fills both ways, and 0 of 11,958 sigma windows contain an unpublished
  print). On a tape with larger publish lags it would import foresight silently. Push the publish
  filter into `strike()` and `_build_sigma()` the way it already is in `latest_at()`.
* **Reporting hazard (must fix before the tables are reused).** The C4 6×4 sigma grid is computed on
  a **top-of-book** tape. Switching only the fill model — from the bot's real 25-level ladder walk to
  a single level, on identical days and parameters — moves EV +3.584 → +4.153 ¢ and **t_day 1.732 →
  2.088**. The extra depth the bot actually takes is worth **−0.52 ¢/share** (161 shares at 0.6789
  vs 98 shares at 0.6736; the walk exceeds one level on 60.8 % of fills). **Anyone quoting a 5m
  t_day above 2 from a top-of-book backtest is quoting a bot that does not exist.** Those tables
  need the ladder caveat inline.
* **Documentation errors corrected:** the published `+8.38 ¢` figure was computed at a **30 s**
  staleness threshold, not the ≤5 s that `docs/07` §6 recommends (only 30 s yields exactly 71 fills
  and 14 %); and `+10.89 ¢` is the **uncorrected** shipped run, not a corrected one — corrected at
  5 s it is +9.33 ¢. Quote the right pairs. Separately, the σ percentiles published in C2 caveat 4
  were biased ~20 % high and have been re-measured over 9.72 M seconds.

---

## 5. Because the answer is NO: what would have to be true, and is it worth chasing?

### 5.1 What specifically would have to be true to revisit

Revisiting is only defensible if **all** of the following hold. They are stated now so they cannot be
softened later.

1. **Day-level t ≥ 2.0 at the shipped parameters, under the bot's real 25-level ladder fill model,
   on post-2026-06-27 data only.** The pre-break regime is now known not to generalise. Across all
   24 sigma cells tested, **zero reach t_day 2.0 post-break** and 22 of 24 fail even the +3 ¢ EV
   condition. A top-of-book number does not count (§4).
2. **EV ≥ +3.0 ¢/share with P(true EV < 3 ¢) below ~0.25**, not the 0.53 measured here. That means a
   sample where the conclusion does not turn on the best one or three days.
3. **The result must survive removing the best 3 days.** Today it does not (+1.736 ¢, t_day 1.085).
4. **The result must survive excluding every day with degraded Chainlink coverage.** Today it gets
   *worse* (t_day 1.016 on 47 clean days), which is at least honest, but it is not a pass.
5. **Something must explain the 2026-06-27 pricing change.** Depth did not fall; the edge halved.
   Until that is understood, any future 5m number is a draw from an unmodelled regime process.

### 5.2 What data would be needed

**Roughly 100 more clean days — about 3 to 4 more months of waiting.**

That is a measurement, not a guess. At the observed daily mean of +2.79 ¢ and daily sd of 12.26 ¢:

| n days | what it buys |
|---|---|
| 56 (today) | **~38 % power** to reach t_day 2.0 *even if the observed effect is exactly true* |
| ~78 | the point estimate first crosses t_day 2.0 — i.e. barely, and only if nothing decays further |
| **~157** | **80 % power.** This is the honest requirement. |

The pipeline is one command and the fetch is cheap (56 days, 9.66 GB, ~zero failures). The binding
constraint is **wall-clock time**, not effort. Note also that the two most recent sub-periods are the
weakest in the whole sample (post-break +1.89 ¢ / t_day 0.665; July 8–26 +3.14 ¢ / t_day 0.917), so
the base case for another 100 days is *not* that the number drifts upward.

### 5.3 Is it worth pursuing at all, versus accepting ~$11–15/day on 1h?

**My recommendation: stop active work on 5m. Do not schedule the 100-day re-run as a project.
Re-run it opportunistically, for free, in ~3 months, if and only if the 1h bot is still running.**

The honest case *for* pursuing it: if the residual +3.5 ¢ at 15.7 trades/day is real, it is
**~$90/day at a $250 clip** — six to eight times the entire 1h business. That is a large enough prize
that "we could not prove it" is not the same as "it is not there". P(EV ≤ 0) is only 1.3–4.2 %.

The case *against*, which I find stronger:

* **The prize is not $90/day under the conditions we would actually trade in.** Post-break it is
  +1.89 ¢/share at t_day 0.665, and under the stress stack the recent period is **negative**
  (−$11 to −$25/day). The $90/day figure requires the *pre-break* regime, which is dated and gone.
* **The scalability argument was overstated.** `docs/07` §3.4's "5m is ~10× more scalable than 1h"
  compared a walked-ladder 5m number to a top-of-book 1h number. Apples-to-apples it is
  **$27.33 vs $10.51–11.40 = 2.4–2.6×** per signal. The real 5m advantage is the trade *count*
  (~16/day vs ~0.9), not the depth — and the count only pays if the per-trade edge is real.
* **Everything cheap has been tried and none of it moves EV.** Across 24 sigma/vol-window cells the
  minimum paired p-value is **0.72**; the cells have mean pairwise daily-P&L correlation 0.874 and
  **1.28 effective independent tests**. They are one experiment, not 24. The derived σ floor (4e-5)
  produced the study's highest t_day (2.390) on the top-of-book tape and collapsed to 2.007 → 1.70 →
  0.66 under real execution, bad-day exclusion and the post-break frame. **A grid cannot rescue this,
  and a finer grid would only find more spurious spikes.**
* **There is a structural reason no volatility recalibration can work.** On the traded subset, the
  model's `fair` is **15–18 percentage points overconfident at every floor value** (predicts 0.921,
  wins 0.740). The gap is invariant to the floor because raising σ lowers both `fair` and the trigger
  threshold together. The strategy is profitable — where it is profitable — because the ask (mean
  0.666) sits far below the realized win rate (0.740), **not** because `fair` is accurate. Improving
  the volatility model cannot improve the edge; it can only reselect trades. And raising the floor
  deletes *better*-than-average trades (the 190 dropped at 3e-5 had an 88.9 % win rate and +6.21 ¢).
* **The opportunity cost is real.** Every hour spent on 5m is an hour not spent on the things that
  actually gate real money: 30 resolved paper trades at the $250 clip, the `_place_live_order` test,
  the 24 h oracle-vs-gamma validation, and the tau_lo margin instrumentation.

**So: accept ~$11–15/day on 1h as the honest ceiling of the current strategy.** `docs/07` §6.5
pre-committed to exactly this conclusion — *"if 20–30 corrected fresh days put 5m below +3 ¢/share,
then the only scalable family is gone and this strategy's honest ceiling is ~$11–16/day on 1h. At
that point the correct decision is to stop adding engineering."* Fifty-six days put it at +3.5 ¢ with
a day-level t of 1.73, which is the same answer with a slightly larger point estimate: **not
provable, not deployable.**

The adjacent multi-coin work points the same way: of six non-BTC hourly families, only ETH survives,
worth **+$2/day** (+32 % on the BTC bot) at 0.22 trades/day, and the non-BTC pool as a whole is
−$2.48/day on its untouched test half. There is no cheap 10× hiding anywhere in this design.

### 5.4 If 5m is ever turned on anyway — the non-negotiable prerequisites

Recorded so they cannot be skipped in a hurry later.

| # | prerequisite | why |
|---|---|---|
| 1 | **Book-age guard** (reject any fill whose book age at the decision instant exceeds ~5–20 s) | Removes exactly the failure mode that supplied 53.6 % of A3's claimed P&L and 67.7 % of this run's raw P&L. Costs 0.471 % of decision instants. |
| 2 | **Hard skip of the 04:00–05:00 UTC hour** | Six freeze episodes in 56 days, all starting 04:05–04:15 UTC. Without it a live bot sends real orders into books frozen for up to 39 minutes. |
| 3 | **`sigma_1s_floor` made per-family before any Chainlink family is enabled** | It is currently a single global value, but 1h reads Binance and 5m/15m/4h read Chainlink. See §6 below — this is a live-safety finding. |
| 4 | **Fix the harness causality defect** (§4) before any number from it is trusted again | Zero impact today, silent foresight import on a worse tape. |
| 5 | **`per_event_cap_usd` ≤ $250, and the adverse-size guard active** | Fills above $1,000 notional measure **−0.82 ¢/share** (n=29); above $2,000, **−1.65 ¢/share** (n=12). Worst single fill: **−$8,407**. Marginal $/day per $ of cap: $0.47 ($25→$100), $0.25 ($100→$250), **$0.07** ($250→$500), negative beyond $1,000. |
| 6 | **The pre-registered next test is `fair_cap`, not sigma** | ~86 % of 5m decisions saturate `fair_cap`, so it — not σ — sets the model's tail probability most of the time. And the frame must be post-2026-06-27 data only. |

---

## 6. A live-safety finding, unrelated to the verdict but urgent

`strategy.close_snipe.sigma_1s_floor` is a **single global config value**, applied in
`strategy.evaluate_close_snipe` regardless of which oracle produced σ̂ — but `engine._snipe_inputs`
feeds **1h from Binance** and **5m/15m/4h from Chainlink**.

The calibration work derived a floor of **4.0e-05** for the Chainlink families (three independent
routes: unconditional 1s vol, realized-forward vol conditional on a quiet trailing estimate, and
log-loss-optimal calibration over 108,737 decision instants — all landing on 3–6e-5). That value is
correct for Chainlink. **Writing it to `config.yaml` today would change only the live, working 1h
family**, on a calibration performed for a different feed:

| floor | binds on % of **live 1h** decision seconds |
|---|---|
| **8e-6 (shipped)** | **7.8 %** |
| 3e-5 (previously proposed) | 50.0 % |
| **4e-5 (derived)** | **66.5 %** |

That would damp `fair` toward 0.5 on two thirds of all 1h ticks — a large, untested behavioural
change to the only strategy currently earning money.

**Action: leave `config.yaml` at `8.0e-06`. Record `4.0e-05` as the Chainlink-family value in the
comments as dead config. Make the knob per-family before any Chainlink family is ever enabled.**
Separately, the finding that the trailing estimator understates forward volatility by 1.5–2.2× was
measured on Chainlink but the mechanism (short window, fat tails) is probably not feed-specific — so
**1h's floor may also be wrong**. That must be measured on the Binance tape against 1h outcomes
before anything changes. Nothing in this document authorises that change.

---

## 7. Honest limits — what this run could NOT establish

1. **Whether the residual +3.5 ¢ is a genuinely smaller edge or zero.** This is the central
   limitation. At 56 days the test has ~38 % power. P(EV ≤ 0) is only 1.3–4.2 %, so "there is
   nothing there" is *not* established either. **The correct statement is: not proven, in either
   direction, and not deployable on that basis.**
2. **Why the edge halved on 2026-06-27.** The break is dated and sharp (3.2× step-down in quote
   intensity, never recovering), but **depth did not fall**, so it is a pricing change and nothing
   here identifies its cause. Without a mechanism, the regime process is unmodelled.
3. **Whether the 06-27 break *caused* the P&L drop.** Direction is right (+5.95 ¢ before, +1.73 ¢
   after) but p = 0.39. Daily noise swamps it.
4. **Why the two tapes disagree about latency.** Raising latency to 3.0 s *raised* ¢/share on the
   43-day historical tape (+9.44 → +14.43) and is *catastrophic* on the fresh tape (t_day −0.50).
   Same code produces both. Unexplained.
5. **Production-server latency.** Everything was measured through this sandbox's HTTPS proxy. Since
   latency is the single knob that kills 5m, this is not a footnote — it is the largest un-measured
   input to the whole question. Verify on the real server; do not assume.
6. **Market impact and queue position.** Every capacity number is a static-book replay: the book is
   assumed to sit still while we consume it. $250 is defensible on the depth measurement (median
   $80.99 fillable per signal inside the 3 ¢ walk bound); above it is untested and the marginal
   return is already $0.07/day per $ of cap.
7. **Live constraints the replay does not model**, all of which would *reduce* fills — the engine's
   $1,000 global open-notional cap, the warmup gate, and the circuit breaker. The replay is
   optimistic on execution, not pessimistic.
8. **A 1h-specific sigma recalibration was not performed** (§6), and `fair_cap` — the bigger lever on
   5m — was scanned but not changed.
9. **Whether a slower, more selective 5m variant works.** The 0.85–0.95 avg-fill-price bucket is
   **30 % of fills, 91.6 % win rate, and loses money after fees** (+0.34 ¢/share, −$237). That is the
   obvious variance-reduction target, and it was not pursued — deliberately, because tuning a filter
   into a failing result after seeing the table is exactly the move this decision rule exists to
   prevent. It is a pre-registered lead for a future run with more data, not a rescue for this one.
10. **This run says nothing about 15m or 4h.** They remain off, unmeasured, and behind the same
    allowlist.

---

## 8. Bottom line for the owner

We fetched 56 fresh days including all 30 days of June — the blind spot every prior analysis flagged
— rebuilt the replay twice independently, proved the harness reproduces the verifier's control
numbers to four significant figures, and applied the decision rule exactly as it was written down
beforehand.

**The 5m family does not clear the bar. Day-level t is 1.73 against a required 2.00, and the only way
to get above 2.00 is to leave the vendor-outage fills in — which is precisely the error that started
this whole exercise.** The historical edge was real; it decayed, significantly (p = 0.007 against the
April–May regime), somewhere inside a June window we can now see but cannot explain.

Nothing changes in the bot. `allowed_families` stays `["1h"]`, `sigma_1s_floor` stays at `8.0e-06`,
and the paper bot keeps running on 1h at ~$11–15/day toward the 30 resolved trades that gate real
money.

The scale-up thesis this project was built around is **not confirmed**. That is a real answer,
obtained cheaply, with no money at risk — and it is worth more than a green light that turned out to
be seven fills against a frozen order book.
