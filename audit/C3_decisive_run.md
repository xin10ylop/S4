# C3 — The decisive 5m run

**Date:** 2026-07-27 · **Sample:** `data/fresh5m/`, **56 fresh days 2026-06-01 → 2026-07-26**,
3,986 windows with real 25-level depth · **Harness:** `scripts/fresh5m/replay.py` (audit/C2)
driven by `scripts/fresh5m/c3_run.py`.

---

## VERDICT: DO NOT ENABLE THE 5m FAMILY.

| pre-committed condition | threshold | measured (shipped params, 56 fresh days, staleness ≤ 5 s, $250 clip) | |
|---|---|---|---|
| **(a)** corrected fresh EV | ≥ **+3.0 ¢/share** | **+3.487 ¢/share** | **MET — barely** |
| **(b)** day-level t-stat | ≥ **2.0** | **t_day = 1.702** (p = 0.094) | **NOT MET** |
| **(c)** EV positive under the full stress stack | > 0 | **+2.476 ¢/share** — but **−$0.60/day** and t_day **−0.93** | **MET on the letter, zero in substance** |

**(b) fails, so the rule says do not enable, and I am not going to argue around it.** Three things
make the failure worse than a near-miss rather than better:

1. **t_day ≥ 2.0 is reached at exactly one staleness setting: none at all.** At *every* threshold
   from 2 s to 60 s the day-clustered t sits between 1.67 and 1.91. Only the **uncorrected** run —
   the one that fills against books frozen for up to 39 minutes — reaches t_day 3.12. The condition
   is met only by the contamination.
2. **Condition (a) passes by 0.49 ¢ on a statistic whose 95 % day-bootstrap CI is [−0.44, +5.90] ¢.**
   P(true EV < 3.0 ¢) = 0.55. Passing (a) is a coin flip, not a finding.
3. **On the 19 genuinely-new July days (2026-07-08 → 07-26) — the most recent, cleanest, most
   deployment-relevant slice — (a) also fails: +2.87 ¢/share, t_day 0.87.**

The 43-day historical sample really does hold an edge (re-measured here at **+9.22 ¢/share,
t_day 4.79** under identical code and the identical staleness filter). The fresh 56 days do not
reproduce it. The gap is statistically real at the day level (Welch p = 0.0070 against the
Apr 16–May 12 regime; p = 0.0353 against all 43 historical days).

---

## 0. What was run, and the causality attestation

Shipped parameters, read from `bot/config.yaml` + `bot/polybot/strategy.py`, not guessed:

```
edge_min 0.03 | price_min 0.30 | price_max 0.99 | vol_window_secs 120
sigma_1s_floor 8e-6 | fair_cap 0.98 | fee 0.07·p·(1−p) taker, 0 maker
snipe_last_secs 5.0 | snipe_min_tau_secs 2.5 | snipe_fill_margin_secs 0.5
latency_ms 1500  ->  tau_lo = max(2.5, 1.5+0.5) = 2.5  ->  band [2.5, 5.0]  ->  ticks tau ∈ {5,4,3}
per_event_cap_usd 250 (config ships 250; the $25 clip is reported alongside)
max_walk_above_best 0.03 | ONE entry per window, first qualifying tick, first qualifying side
fill: re-fetch the book latency_ms later and walk asks while edge > edge_min and price ≤ best+0.03
winner: result_id (5m settles on Chainlink; the Chainlink-recomputed winner is a robustness row)
```

**CAUSALITY — I enforced it and I state the result.** Every decision reads only Chainlink reports
with `server_timestamp_us ≤ t`. Over the 11,958 decision instants of this run, **11,638 (97.32 %)
would have used a report that had not yet been published** under the naive `timestamp_us ≤ t` rule
(median day 1.63 s of invented foresight; 2026-06-10 alone 462.8 s, the vendor backfill C1 flagged).

Running the trap deliberately prices it on this tape:

| | fills | ¢/share | win | t_day |
|---|---|---|---|---|
| **causal** (truth, what is reported everywhere below) | 881 | **+3.487** | 72.6 % | **1.702** |
| **non-causal** (`timestamp_us ≤ t`) | 917 | **+5.435** | 76.4 % | **3.379** |

**The publication-lag trap is worth +1.95 ¢/share here and would single-handedly flip condition (b)
from 1.70 to 3.38.** Any 5m number produced without this filter is inflated by ~2 ¢ and should be
discarded. (C2 measured +1.89 ¢ on the repo tape by the same route; two tapes, same answer.)

Also verified before trusting a single number: the column-limited book loader used by the driver is
asserted byte-identical to `replay.load_fresh_day` on two full days (`--verify-loader`, tapes,
timestamps, ladders and `result_id` all equal).

---

## 1. Headline

### 1.1 Primary result

**56 days · shipped parameters · strict causality · book age ≤ 5 s at both the signal and the fill
instant (PRE convention: a stale book is handed to the evaluator as `None`, exactly as the live bot
sees a book it could not fetch).**

| | $250 clip | $25 clip |
|---|---|---|
| signals | 1,770 | 1,770 |
| **fills** | **881** | **881** |
| **trades/day** | **15.73** | 15.73 |
| **EV** | **+3.487 ¢/share** | **+3.820 ¢/share** |
| **win rate** | **72.64 %** | 72.64 % |
| per-trade t | 2.529 | 2.771 |
| **DAY-LEVEL t (pre-committed)** | **1.702** | **1.910** |
| **total P&L** | **+$5,033** | **+$1,266** |
| **P&L/day** | **+$89.88** | **+$22.61** |
| day-level t on daily $ P&L | 1.72 | 2.26 |
| 95 % day-bootstrap on $/day | [−$11, +$194] | [+$3, +$42] |
| return on deployed notional | 5.21 % | 6.67 % |

Day means: n = 56, mean +2.792 ¢, sd 12.278, **two-sided p = 0.094**.
Day-bootstrap 95 % CI on EV/share **[−0.44, +5.90] ¢**; P(EV ≤ 0) = 4.2 %; **P(EV < 3.0 ¢) = 54.7 %**.
Day-block bootstrap of pooled EV **[+0.27, +6.48] ¢**; P(EV ≤ 0) = 1.8 %; P(EV < 3.0 ¢) = 38.5 %.

Leave-one-day-out on t_day spans **1.48 → 2.36**. It crosses 2.0 only when 2026-07-08 (a −37.7 ¢
day) is removed. The result is one day away from passing (b) and one day away from 1.48 — which is
the definition of underpowered, not of a near-miss.

### 1.2 Staleness sensitivity (the required table)

**PRE convention** (rejected at decision time — the honest one, and what a live bot with a book-age
guard would actually do), shipped params, $250:

| max book age | fills | trades/day | ¢/share | win | t_trade | **t_day** | $/day |
|---|---|---|---|---|---|---|---|
| **2 s** | 880 | 15.71 | +3.438 | 72.61 % | 2.492 | **1.673** | $85.79 |
| **5 s (PRIMARY)** | **881** | **15.73** | **+3.487** | **72.64 %** | **2.529** | **1.702** | **$89.88** |
| **10 s** | 892 | 15.93 | +3.900 | 72.87 % | 2.846 | **1.855** | $116.24 |
| **20 s** | 896 | 16.00 | +3.920 | 72.88 % | 2.868 | **1.839** | $118.50 |
| **30 s** | 897 | 16.02 | +3.968 | 72.91 % | 2.905 | **1.845** | $120.70 |
| **60 s** | 899 | 16.05 | +4.081 | 72.97 % | 2.989 | **1.906** | $132.24 |
| **unlimited** | 938 | 16.75 | **+5.622** | 74.09 % | 4.217 | **3.118** | $262.26 |

**POST convention** (drop after the fact — the `docs/07` §3.4 convention, from the same raw tape):

| max book age | fills kept | fills dropped | % of raw P&L dropped | ¢/share | t_trade | **t_day** |
|---|---|---|---|---|---|---|
| 2 s | 878 | 60 | **69.3 %** | +3.328 | 2.411 | 1.540 |
| **5 s** | **879** | **59** | **67.7 %** | **+3.378** | 2.448 | **1.569** |
| 10 s | 891 | 47 | 56.1 % | +3.842 | 2.803 | 1.770 |
| 20 s | 895 | 43 | 55.3 % | +3.862 | 2.825 | 1.766 |
| 30 s | 896 | 42 | 54.4 % | +3.910 | 2.862 | 1.772 |
| 60 s | 898 | 40 | 50.0 % | +4.023 | 2.946 | 1.855 |
| unlimited | 938 | — | — | +5.622 | 4.217 | 3.118 |

PRE and POST agree to within 0.11 ¢ at every threshold, which is the internal check C2 §3 specified.

**Why 5 s is the primary threshold.** It was pre-committed in C2 §7 (`--max-book-age 5`) before any
fresh number existed, and it is the value `docs/07` §6 recommends. It is also the right number on
the evidence: C1 measured this exact book channel at **median age 0.00 s and p99 0.1–0.2 s on
unaffected days**, so 5 s is 25–50× the p99 of a live book and cannot be rejecting live liquidity,
while every observed freeze is ≥ 20 s. The choice is not load-bearing — **no threshold reaches
t_day 2.0**, so the decision is identical at 2 s, 5 s, 10 s, 30 s or 60 s. Only "unlimited" passes,
and §2 shows exactly what "unlimited" is buying.

### 1.3 Why $250 makes only 4× the money of $25

660 of 881 fills are cap-bound at $25 but only 192 of 881 at $250: past ~$25 the binding constraint
is **displayed depth inside 3 ¢ of best**, not the clip. Ten times the clip buys four times the P&L.

---

## 2. The contamination accounting

Raw (no staleness filter): **938 fills, +5.622 ¢/share, $14,686.56 total P&L.**

Book age at the decision instant (max of the signal instant and the fill instant), seconds —
**violently bimodal, exactly as on the repo tape**: median **0.004**, p90 **0.042**, p95 **10.2**,
p99 **1,222**, max **2,365**. There is no continuum: a book is either live-streaming at
millisecond cadence or it is frozen for minutes.

| cut | fills | % of fills | P&L | **% of raw P&L** | win | ¢/share |
|---|---|---|---|---|---|---|
| age > 2 s | 60 | 6.4 % | $10,177 | **69.3 %** | 96.7 % | +39.18 |
| **age > 5 s (excluded by the primary run)** | **59** | **6.3 %** | **$9,947** | **67.7 %** | **96.6 %** | **+39.05** |
| age > 10 s | 47 | 5.0 % | $8,239 | 56.1 % | 97.9 % | +39.37 |
| age > 20 s | 43 | 4.6 % | $8,114 | **55.2 %** | **100.0 %** | +42.26 |
| age > 30 s | 42 | 4.5 % | $7,990 | 54.4 % | 100.0 % | +42.14 |
| age > 300 s | 27 | 2.9 % | $6,014 | 41.0 % | 100.0 % | +46.85 |

**Answer to the question as asked: the 5 s filter excludes 59 of 938 fills (6.3 %) and they carry
67.7 % of raw P&L.** The 43 fills on books frozen ≥ 20 s went **43 for 43** at **+42.3 ¢/share** —
that is not a strategy, it is the signature of filling against a quote the market left minutes ago.
Fresh fills (age ≤ 5 s) win 72.6 % at +3.5 ¢; stale fills win 96.6 % at +39.1 ¢.

The largest of them are unmistakable: 2026-07-01 04:45 filled the Up token at 0.513 against a book
last updated **2,364 seconds (39 min) earlier**, pinned at 0.51/0.50 while BTC moved, for
+47.0 ¢/share × 487 shares = $229. Twenty-seven such fills exist.

### 2.1 Yes — the 2026-07-21 04:07 outage signature recurs, five more times

Clustering the ≥ 20 s stale fills into consecutive-close episodes:

| day | first close (UTC) | last | fills | max book age | wins | P&L | ¢/share |
|---|---|---|---|---|---|---|---|
| **2026-07-01** | **04:15** | 04:45 | **7** | **2,365 s** | 7/7 | $1,624 | +47.3 |
| **2026-06-24** | **04:15** | 04:40 | **6** | **2,043 s** | 6/6 | $1,378 | +47.0 |
| **2026-07-21** | **04:15** | 04:40 | **6** | **1,923 s** | 6/6 | $1,338 | +46.2 | ← *the A3 one* |
| **2026-06-19** | **04:10** | 04:30 | **5** | **1,481 s** | 5/5 | $859 | +42.7 |
| 2026-06-19 | 05:05 | 05:10 | 2 | 699 s | 2/2 | $419 | +45.7 |
| **2026-06-10** | **04:10** | 04:15 | 2 | 585 s | 2/2 | $524 | +50.2 |
| 2026-06-03 | 15:25 | 15:35 | 3 | 149 s | 3/3 | $492 | +46.9 |
| 2026-06-03 | 12:20 | 12:20 | 1 | 320 s | 1/1 | $251 | +49.3 |
| 2026-06-05 | 12:30 | 12:30 | 1 | 143 s | 1/1 | $169 | +46.4 |
| 2026-06-02 | 10:30 | 10:40 | 3 | 97 s | 3/3 | $97 | +11.1 |
| 2026-07-10 | 04:10 | 04:10 | 1 | 205 s | 1/1 | $100 | +28.1 |
| 2026-07-24 | 04:05 | 04:05 | 1 | 82 s | 1/1 | $205 | +44.3 |
| + 5 more single-fill blips (59–180 s) | | | 5 | | 5/5 | $657 | |
| **TOTAL** | | | **43** | | **43/43 (100 %)** | **$8,114 = 55.2 % of raw P&L** | **+42.3** |

**Every episode with more than four fills starts between 04:05 and 04:15 UTC** — midnight ET,
Polymarket's daily rollover, precisely the recurring hazard C1 §6 identified from a different
direction. **28 of the 43 stale fills close in the 04:00–04:59 UTC hour and carry 41.0 % of raw
P&L, from an hour that holds 4.17 % of closes.** The A3 episode is not an unlucky one-off; it is one
draw from a scheduled monthly-recurring disturbance that appears **6 times in 56 days**.

Cross-check that these are real venue silence and not a capture artefact: the borderline cluster
(9 fills at signal-age 4.0 s / fill-age 5.5 s on 06-03 and 06-05) was pulled apart row by row. Those
windows received **11 book updates in 60 seconds, all at an unchanged 0.51 @ 334.42**, against a
median of **13,000–15,000 updates per window-outcome** on the same days. They are frozen books, and
excluding them is correct.

### 2.2 The prefilter did not hide fills

`data/fresh5m/books/` holds depth only for the 3,314 windows the fetch prescreen kept, plus 672
randomly-chosen **rejected** windows as controls. Those 672 controls produced **1 signal and 1
fill** — a 0.149 % leak rate. Scaled to all 12,807 rejected windows that is ~19 missed fills over 56
days = **0.34/day, 2.2 % of the fill count**. Trades/day is 15.7, not 20 and certainly not 52.

### 2.3 The two-book execution tax is worse than assumed

1,818 signals → 938 raw fills. **48.4 % of signals die because the book re-fetched 1.5 s later no
longer clears `edge_min`** — against the 34 % attrition the brief planned for. Zero signals hit an
empty book (5m books, unlike 1h, survive to the close).

---

## 3. The stress stack

All rows on top of the corrected (age ≤ 5 s) baseline. Latency 3.0 s is applied **with the bot's own
`tau_lo = latency + 0.5` rule**, which narrows the band to [3.5, 5.0] → ticks τ ∈ {5, 4} and halves
the fill count — that is the honest version, not the "keep the band, land after the close" version.

| variant | fills | trades/day | ¢/share | win | t_trade | **t_day** | **$/day @$250** | **$/day @$25** |
|---|---|---|---|---|---|---|---|---|
| **baseline: shipped + age ≤ 5 s** | 881 | 15.73 | **+3.487** | 72.6 % | 2.529 | **1.702** | **+$89.88** | **+$22.61** |
| fee 0.07 → **0.10** | 865 | 15.45 | **+3.282** | 73.2 % | 2.384 | **1.371** | +$91.07 | +$21.79 |
| latency 1.5 → **3.0 s** (band narrowed) | 415 | 7.41 | **+3.283** | 71.3 % | 1.695 | **−0.503** | +$14.05 | +$4.33 |
| **50 % of displayed depth** | 881 | 15.73 | **+3.442** | 72.6 % | 2.496 | **1.674** | +$52.69 | +$18.58 |
| **ALL THREE TOGETHER** | **410** | **7.32** | **+2.476** | 71.0 % | 1.274 | **−0.932** | **−$0.60** | **+$0.05** |

$25-clip per-share EVs run ~0.3 ¢ higher throughout (+3.820 / +3.614 / +3.643 / +3.703 / **+2.741**)
because the smaller clip stops walking sooner and averages a better price.

**Reading of condition (c).** EV/share stays positive (+2.48 ¢ at $250, +2.74 ¢ at $25), so on the
letter of the rule **(c) is MET**. In substance it is met vacuously: the same stack produces
**−$0.60/day at the $250 clip and +$0.05/day at $25**, with **t_day = −0.93** and a 95 % bootstrap
on daily P&L of **[−$43, +$41]**. The per-share number is positive while the dollar number is
negative because the losing fills are the ones that get size. There is nothing here to deploy.

Two non-obvious details worth recording:
* **fee 0.10 raises $/day slightly** (+$91.07 vs +$89.88) while lowering ¢/share. A higher fee
  raises the per-level `edge` bar, which prunes the marginal high-price levels — the same
  self-selection that makes the fee stress less punishing than it looks and the latency stress more.
* **Latency is the killer, not fees or depth.** Going 1.5 s → 3.0 s removes 53 % of fills and takes
  t_day from +1.70 to **−0.503** on its own. On the 43-day repo tape the identical knob *raised*
  ¢/share (C2 §5: +9.44 → +14.43). The tapes genuinely disagree about the value of trading later.

---

## 4. Time structure

### 4.1 By week (PRE 5 s, $250)

| ISO week | days | fills | ¢/share | win | P&L | t_trade | t_day |
|---|---|---|---|---|---|---|---|
| 2026-W23 (Jun 1–7) | 7 | 69 | **+7.16** | 75.4 % | +$1,274 | 1.58 | 1.81 |
| 2026-W24 (Jun 8–14) | 7 | 134 | **+8.18** | 76.9 % | +$272 | 2.21 | 1.75 |
| 2026-W25 (Jun 15–21) | 7 | 108 | +2.46 | 70.4 % | −$306 | 0.62 | −0.60 |
| 2026-W26 (Jun 22–28) | 7 | 111 | **+7.61** | 75.7 % | +$1,245 | 2.08 | 2.86 |
| 2026-W27 (Jun 29–Jul 5) | 7 | 131 | **−4.04** | 65.6 % | −$524 | −1.02 | −1.50 |
| 2026-W28 (Jul 6–12) | 7 | 72 | +5.61 | 76.4 % | +$1,353 | 1.22 | 0.51 |
| 2026-W29 (Jul 13–19) | 7 | 118 | +3.99 | 72.9 % | +$1,732 | 1.10 | 0.88 |
| 2026-W30 (Jul 20–26) | 7 | 138 | **+0.20** | 71.0 % | −$15 | 0.06 | 0.09 |

Month split: **June +6.231 ¢ (447 fills, 74.7 % win) · July +0.662 ¢ (434 fills, 70.5 % win).**
Full per-day table: `data/c3/by_day.csv`. Daily ¢/share swings from **−37.7 ¢ (07-08)** to
**+24.4 ¢ (06-11)**; daily sd is 12.3 ¢ against a mean of 2.8 ¢.

### 4.2 The regime ladder — historical and fresh, same code, same filter

To compare like with like I re-ran the 43 historical days (`2026-04-02 → 05-12` + `07-06/07`)
through the identical harness at the identical shipped parameters and the identical 5 s staleness
filter (`data/c3/repo43_ship_pre5.parquet`). The brief's quoted regime figures were computed under
A3 conventions with no staleness correction; the numbers below are the corrected, comparable ones.

| period | days | trades/day | **¢/share** | win | t_trade | **t_day** |
|---|---|---|---|---|---|---|
| HIST Apr 2–15 | 14 | 33.9 | **+2.03** (brief: −1.03) | 72.8 % | 1.09 | 1.75 |
| HIST Apr 16–30 | 15 | 26.9 | **+12.73** (brief: +13.06) | 79.9 % | 6.60 | **4.71** |
| HIST May 1–12 | 12 | 28.8 | **+15.42** (brief: +19.66) | 78.8 % | 7.21 | **2.34** |
| HIST Jul 6–7 | 2 | 9.0 | **+1.27** (brief: +0.45) | 72.2 % | 0.12 | 0.25 |
| **HIST all 43 days** | 43 | 28.9 | **+9.22** | 76.8 % | 8.08 | **4.79** |
| FRESH Jun 1–12 | 12 | 10.3 | **+5.32** | 74.8 % | 1.39 | 2.03 |
| FRESH Jun 13–26 | 14 | 17.4 | **+6.27** | 73.8 % | 2.42 | 0.74 |
| FRESH Jun 27–Jul 7 | 11 | 18.6 | **+0.00** | 69.6 % | 0.00 | −0.32 |
| FRESH Jul 8–26 | 19 | 16.3 | **+2.87** | 72.9 % | 1.29 | 0.87 |
| **FRESH all 56 days** | 56 | 15.7 | **+3.49** | 72.6 % | 2.53 | **1.70** |

Welch tests on **day-level** means (the pre-committed clustering):

| comparison | day means | Welch |
|---|---|---|
| HIST Apr 16–May 12 vs FRESH all 56 d | +10.43 ¢/day vs +2.79 ¢/day | t = +2.80, **p = 0.0070** |
| HIST all 43 d vs FRESH all 56 d | +7.69 vs +2.79 | t = +2.14, **p = 0.0353** |
| HIST all 43 d vs FRESH Jul 8–26 | +7.69 vs +2.89 | t = +1.30, p = 0.2030 |
| FRESH Jun 1–26 vs FRESH Jun 27–Jul 26 | +4.30 vs +1.48 | t = +0.86, p = 0.3924 |

### 4.3 Decayed regime, or noise? Both, and the distinction does not matter here

* **Against the historical peak: decayed, significantly.** The fresh 56 days sit 7.6 ¢/day below
  Apr 16–May 12 with p = 0.0070, and 4.9 ¢ below the whole 43-day sample with p = 0.0353. This is
  not the fresh sample being small — it is 56 days against 27, and the difference clears at the
  day level. **The +9 to +15 ¢ regime is gone.**
* **Within the fresh period: cannot distinguish decay from noise.** C1 dated a 3.2× step-down in
  top-of-book update intensity to **2026-06-27**. The 5m edge does drop across that date
  (+5.95 ¢ before, +1.73 ¢ after) — but with p = 0.39 that is not a detectable step. Daily noise
  (sd 12.3 ¢) swamps a 4 ¢ shift over 56 days. **The June blind spot is now filled and it did not
  produce a clean answer; it produced a lower, noisier level.**
* Note also that the *June* leg is the stronger one (+6.23 ¢) and July is the weaker one
  (+0.66 ¢). If anything the fresh data argues the decay is *ongoing* rather than a single step,
  but no sub-period reaches t_day 2.0 except Jun 1–12 (2.03, 12 days, 10 fills/day, and it contains
  the two Chainlink-degraded days).
* **Excluding the Chainlink-degraded days makes the result *worse*, not better** — so the headline
  is not being propped up by bad-data days:

  | sample | days | fills/day | ¢/share | t_day | $/day |
  |---|---|---|---|---|---|
  | all 56 days (primary) | 56 | 15.73 | +3.487 | **1.702** | +$89.88 |
  | ex 06-10, 06-11 (C1's two unusable days) | 54 | 16.04 | +3.361 | **1.493** | +$95.18 |
  | ex all 9 days C1 flags as degraded | 47 | 16.68 | +3.266 | **1.017** | +$88.46 |

  (June alone, ex 06-10/06-11: 28 days, +6.073 ¢, t_day 2.031.)

### 4.4 The full stress stack, by period

| period | fills/day | ¢/share | t_day | $/day @$250 |
|---|---|---|---|---|
| June (all) | 7.07 | +4.63 | −0.74 | +$9.55 |
| pre-break Jun 1–26 | 6.92 | +4.66 | −0.91 | +$11.64 |
| post-break Jun 27–Jul 26 | 7.67 | +0.77 | −0.34 | **−$11.21** |
| target Jul 8–26 | 7.16 | +1.17 | +0.06 | **−$12.39** |
| last 14 days Jul 13–26 | 7.14 | **−3.13** | −0.38 | **−$24.97** |

**Under the stress stack the most recent two weeks are outright negative per share.**

---

## 5. Robustness — every knob was checked and none rescues the result

All at PRE 5 s, $250, 56 days:

| variant | fills | trades/day | ¢/share | t_trade | **t_day** |
|---|---|---|---|---|---|
| **primary (causal, σ-floor 8e-6, ffill σ, result_id winner)** | 881 | 15.73 | **+3.487** | 2.529 | **1.702** |
| σ_1s floor 8e-6 → **3e-5** | 740 | 13.21 | +3.475 | 2.214 | 1.518 |
| σ via `oracle.py` convention (held prints, no ffill) | 876 | 15.64 | +3.696 | 2.688 | 1.767 |
| winner recomputed from Chainlink instead of `result_id` | 872 | 15.57 | +3.560 | 2.572 | 1.813 |
| *(non-causal — the trap, shown for contrast only)* | *917* | *16.38* | *+5.435* | *4.210* | *3.379* |
| operational: hard-skip the 04:00–04:59 UTC hour | 838 | 14.96 | +3.704 | 2.619 | 1.791 |

* **The `sigma_1s_floor` recalibration does not change the decision.** Moving 8e-6 → 3e-5 costs
  16 % of fills for **−0.01 ¢/share** and lowers t_day to 1.518. (C2 §10.3 is right that 3e-5 is
  ~the median of the Chainlink σ distribution and therefore not a floor at all; that remains an open
  design question, but it is not what is standing between this strategy and a green light.)
* **Every convention knob is worth ≤ 0.21 ¢/share.** The negative result is not an artefact of the
  σ convention, the winner source, or the vol floor.
* **Skipping the 04h rollover hour is mandatory for any live 5m deployment** (it removes the freeze
  window entirely) and it costs nothing on the corrected tape — but it buys only +0.09 t_day. It is
  a safety measure, not a fix.

**Fill composition, for the record.** Mean fill price 0.679; median pnl/share **+11.3 ¢** against a
mean of +3.5 ¢ (the distribution is left-skewed: p5 = −77.8 ¢, p95 = +58.3 ¢). Levels walked: 1 in
348 fills, 2 in 278, 3 in 197, 4 in 58 — the 5-level ladder is genuinely used, unlike the repo
control. Sides balanced (457 down / 424 up). By price bucket:

| avg fill price | fills | ¢/share | win | P&L |
|---|---|---|---|---|
| 0.30–0.50 | 231 | +5.16 | 46.8 % | +$1,967 |
| 0.50–0.70 | 191 | +6.16 | 67.5 % | +$2,291 |
| 0.70–0.85 | 197 | +3.13 | 82.7 % | +$1,013 |
| 0.85–0.95 | 262 | **+0.34** | 91.6 % | **−$237** |

The expensive near-certain end of the book — where a naive reading of a 91.6 % win rate looks
safest — is where the strategy loses money after fees on the fresh tape.

---

## 6. The three conditions, stated explicitly

> **(a) corrected fresh-data EV ≥ +3.0 ¢/share at the SHIPPED parameters**
> **MET.** +3.487 ¢/share ($250 clip) / +3.820 ¢ ($25 clip), 56 days, book age ≤ 5 s, strict
> causality. Caveats stated rather than buried: the 95 % day-bootstrap CI is [−0.44, +5.90] ¢,
> P(true EV < 3.0 ¢) = 0.55, and on the 19 most recent days it is +2.87 ¢, i.e. **(a) fails on the
> target period.**

> **(b) DAY-LEVEL t-stat ≥ 2.0**
> **NOT MET. t_day = 1.702** ($250) / 1.910 ($25); one-sample p = 0.094 on 56 daily means.
> It is below 2.0 at **every** staleness threshold from 2 s to 60 s (1.67–1.91) and reaches 3.12
> only with the staleness filter switched off entirely. It is 0.87 on the target July period and
> negative on three of the eight fresh weeks.

> **(c) EV remains POSITIVE under the full stress stack (fee 0.10, latency 3.0 s with the tau_lo
> widening, 50 % depth)**
> **MET on the letter: +2.476 ¢/share ($250) / +2.741 ¢ ($25).**
> In substance it is a zero: **−$0.60/day at $250, +$0.05/day at $25**, t_day **−0.93**, 95 %
> bootstrap on daily P&L [−$43, +$41], and **−3.13 ¢/share over the most recent 14 days**.

**One of three conditions fails outright and a second passes only as a coin flip. The pre-committed
rule is `ALL of (a),(b),(c)`. It is not satisfied. DO NOT ENABLE the 5m family.
`strategy.close_snipe.allowed_families` stays `["1h"]`.**

---

## 7. What this run settled, and what it did not

**Settled:**
1. **The A3 +7.42 ¢ / 21.6 trades-per-day claim is dead.** At shipped parameters on its own five
   days this harness measures +0.93 ¢/share, t_day 0.41 — and −7.20 ¢ under the stress stack.
2. **The verifier's contamination diagnosis was correct and is worse at scale.** 6.3 % of raw fills
   carry 67.7 % of raw P&L; the ≥ 20 s subset went 43/43 at +42.3 ¢/share.
3. **The 04:00 UTC freeze is a recurring scheduled hazard, not an accident** — 6 episodes in 56
   days, 41 % of raw P&L. Any live 5m deployment needs a book-age guard *and* an 04h skip. This is
   now confirmed on the depth channel by a third independent derivation.
4. **There is no lookahead in the harness, but there is a live trap next to it.** Enforcing
   `server_timestamp_us ≤ t` costs 1.95 ¢/share and 1.68 of t_day; 97.3 % of decisions would touch
   an unpublished report without it.
5. **June exists and is now measured.** The blind spot is filled: +6.23 ¢/share across 30 June days
   at t_day 2.29 — better than July, worse than April/May, and not enough to carry the sample.
6. **The historical edge was real.** Re-measured under the same corrected pipeline the 43 repo days
   give +9.22 ¢/share at t_day 4.79. The fresh period is significantly below it (p = 0.0070 vs the
   peak regime). This is decay, not a measurement error in the old work.
7. **Honest throughput is ~15.7 fills/day, not 20 and not 52.** Half of all signals die on the
   second book fetch.

**Not settled:**
* Whether the residual +3.5 ¢ is a genuinely smaller edge or zero. Daily means are +2.79 ¢ with
  sd 12.28 ¢, so **at 56 days this test has only ~38 % power to reach t_day = 2.0 even if the
  observed effect is exactly true**. The point estimate would first cross 2.0 at **n ≈ 78 days**
  (~22 more) and 80 % power needs **n ≈ 157 days** (~100 more). Alternatively, cut the variance:
  the 0.85–0.95 price bucket is 30 % of fills and −$237 of P&L.
* Whether the 2026-06-27 liquidity step *caused* the drop. The direction is right but p = 0.39.
* Whether a properly recalibrated `sigma_1s_floor` (C2 §10.3 shows 3e-5 is a median, not a floor)
  would help. It does not at 3e-5; a p05-shaped floor near 9e-6–1.4e-5 is untested and is a
  separate work item, not a reason to defer this verdict.

**Recommendation.** Leave 5m disabled. Re-run this exact pipeline (it is one command) after another
~100 clean days if the question is worth reopening; the decision rule should not be renegotiated in
the meantime. If it is reopened, the two changes most likely to matter are a real book-age guard
plus an 04:00–05:00 UTC skip (mandatory regardless), and dropping the 0.85–0.99 price band.

---

## 8. Files

| path | what |
|---|---|
| `scripts/fresh5m/c3_run.py` | the driver — 21 parameter sets over the 56-day fresh tape, one book load per day, `--verify-loader` asserts the fast loader ≡ `replay.load_fresh_day` |
| `scripts/fresh5m/c3_report.py` | §1–§2, §3, §4.1, §5 tables |
| `scripts/fresh5m/c3_extra.py` | §2.1 episodes, §4.2 regime ladder + Welch, bootstraps, prefilter leakage, 04h variant |
| `data/c3/trades_*.parquet` | per-variant trade tapes (21 variants × 56 days) |
| `data/c3/repo43_ship_pre5.parquet` | the 43 historical days re-run at shipped params + 5 s filter |
| `data/c3/by_day.csv` | per-day EV/fills/win/P&L for the primary run |
| `data/c3/stale_fills.csv` | all 59 excluded fills, itemised |
| `data/c3/causality.parquet`, `per_day.parquet` | per-day causality audit and window counts |

Reproduce:

```bash
python3 scripts/fresh5m/c3_run.py --days-from 2026-06-01 --days-to 2026-07-26 \
        --out-dir data/c3 --verify-loader
python3 scripts/fresh5m/c3_report.py
python3 scripts/fresh5m/c3_extra.py
```
