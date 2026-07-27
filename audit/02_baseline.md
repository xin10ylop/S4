# 02 — Backtest Baseline vs Live Paper (1h family only)

Audit date 2026-07-26. Live bot running 2026-07-16 18:18 UTC → 2026-07-26 12:31 UTC = **9.759 days**.
Sources: `results/bt_1h_snipe_{train,test}.csv`, `results/bt_1h_snipe_test_lat{2,3}.csv`,
`results/bt_1h_settle_test.csv`, `results/oos_summary.csv`, `docs/03_final_report.md`,
`docs/04_executability_audit.md`, `scripts/backtest_1h.py`, and a purpose-built re-run
(`live-semantics` backtest, described in §3) over the untouched OOS window.

---

## 0. Headline

| metric | live (n=7) | published BT OOS | **config-matched baseline** | verdict |
|---|---|---|---|---|
| signals/day | 1.127 | n/a (not modelled) | 1.192 | **IN LINE** (ratio 0.95) |
| fills/day | 0.717 | 1.247 | 0.740 | **IN LINE** (ratio 0.97) |
| signal→fill conv | 63.6% | 100% (implicit) | 62.1% | **IN LINE** |
| EV ¢/share | +20.95 | +9.83 | +12.97 | **BETTER on point est., NOT significant** |
| win rate | 0.857 (6/7) | 0.813 | 0.819 | **IN LINE** |
| avg entry px | 0.6246 | 0.703 | 0.676 | IN LINE (slightly cheaper) |
| shares/fill | 30.67 | 19.6 (@$25 cap) | 23.63 | **BETTER** (1.30×) — book-walking |
| $/day | $4.61 | $2.33 | $2.17 | BETTER 2.1×, = EV(1.6×) × size(1.3×) |
| settle_sweep fills | 0 / 1235 signals | 0.164/day | — | **IN LINE with the executability audit, dead as predicted** |

**The single most important correction in this audit:** the published backtest's
"1.25 trades/day" is *not* a fill rate and must not be compared to live fills. See §3.
Once corrected, **live frequency is essentially exactly on prediction** and
**live EV is a 76th-percentile draw from the baseline — not evidence of outperformance.**

---

## 1. The published backtest baseline (as-is)

Parameters read from `scripts/backtest_1h.py` (these are the numbers behind `oos_summary.csv`):
`EDGE_MIN_A = 0.05`, `SNIPE_FROM = -6` (t_rel ∈ {−6,−5,−4,−3,−2}, 5 eval seconds),
price band `0.30 < px < 0.99`, `FEE = 0.07·p·(1−p)`, `LAT = 1` (1 s), **`DOLLARS = 10.0`**
(a $10 per-event notional cap, *not* $25), sizing `min(ask_size, 10/px)` — **top of book only,
no book-walking**. One entry per window (`done_a` breaks the loop).

Denominators: 275 daily 1h quote files, no gaps. Train = 202 days / 4,784 windows
(2025-10-11 → 2026-04-30). Test/OOS = 73 days / 1,740 windows (2026-05-01 → 2026-07-12).

| | n | /day | /window | EV ¢/sh | win rate | sd ¢ | t | mean px | med px |
|---|---|---|---|---|---|---|---|---|---|
| TRAIN lat1 | 254 | 1.2574 | 0.05309 | **+20.02** | 0.8819 (224/254) | 33.3 | 9.59 | 0.669 | 0.650 |
| **TEST/OOS lat1** | **91** | **1.2466** | **0.05230** | **+9.83** | **0.8132 (74/91)** | 30.1 | 3.11 | 0.703 | 0.750 |
| TEST lat2 | 74 | 1.0137 | 0.04253 | +6.95 | 0.7568 (56/74) | 32.7 | 1.83 | 0.675 | 0.715 |
| TEST lat3 | 57 | 0.7808 | 0.03276 | +11.02 | 0.8421 (48/57) | 30.1 | 2.77 | 0.720 | 0.770 |

Matches `oos_summary.csv` exactly (1h close-snipe: n=91, 1.25/day, 9.82¢, wr 0.813). Good.

Entry timing is heavily front-loaded: OOS t_rel = −6 s for 70/91 trades (77%), −5 s for 7,
−4 s for 6, −3 s for 6, −2 s for 2. Sides balanced (46 up / 45 down).

### Signals vs fills in the published backtest
**The published backtest does not distinguish them.** It computes `fair` from Binance at
second `t`, then tests the edge condition against the book at `t + LAT` — the *fill* book.
A window where the edge existed at `t` but evaporated by `t+1` simply produces no row.
So `n = 91` is a *fill* count with an implicit **100% signal→fill conversion**, and there is
no published "signals/day" number for 1h close-snipe. This is exactly the bucket the live bot
reports as `book_moved_no_edge`.

### Entry-price distribution (OOS, n=91)
Deciles: `0.31, 0.41, 0.48, 0.61, 0.69, 0.75, 0.81, 0.85, 0.90, 0.91, 0.94`

| bucket | 0.30–0.40 | 0.40–0.50 | 0.50–0.60 | 0.60–0.70 | 0.70–0.80 | 0.80–0.90 | 0.90–0.99 |
|---|---|---|---|---|---|---|---|
| OOS n | 8 | 15 | 3 | 14 | 12 | 26 | 13 |
| train n | 17 | 43 | 57 | 20 | 28 | 58 | 31 |

Distinctly **bimodal** — a longshot cluster near 0.31–0.50 and a favourite cluster near
0.80–0.94, with a hole at 0.50–0.60 OOS. Live's volume-weighted 0.6246 is a plausible draw.

### Available shares per trade — reconstructed exactly
The CSV's `shares` column is censored (`min(szq, 10/px)`), so I re-derived the true
top-of-book depth `szq` from `data/data/processed/daily/1h/quotes/*.parquet` at the exact
fill second (`wts + duration + t_rel + 1`). **Reconstruction verified 91/91 and 254/254
(100%) against the recorded `shares`.**

| | mean | med | d1 | d2 | d3 | d4 | d5 | d6 | d7 | d8 | d9 | max |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OOS szq (shares) | 65.5 | 16.8 | 5.0 | 5.0 | 5.0 | 10.0 | 16.8 | 24.5 | 55.7 | 100.0 | 132.3 | 1,126 |
| TRAIN szq | 1,200.5 | 64.6 | 6.8 | 10.5 | 22.7 | 40.2 | 64.6 | 100.0 | 170.6 | 260.4 | 850.7 | 51,852 |

Available **dollars** at top of book: OOS mean $45.8, median $13.9, q25 $3.8, q75 $48.4, q90 $105.9.
Train mean $681.6, median $45.6. **Depth collapsed by roughly 14× (median 64.6 → 16.8 shares)
between the train and test eras** — a material liquidity regime change that the EV headline hides.

### How often would a $25 notional cap bind?

| cap | OOS binds | OOS mean sh | OOS $/trade | OOS $/day | TRAIN binds | TRAIN $/day |
|---|---|---|---|---|---|---|
| $10 (what the BT used) | 53.8% (49/91) | 10.3 | $7.09 | $1.17 | 73.6% (187/254) | $4.34 |
| **$25 (live config)** | **34.1% (31/91)** | **19.6** | **$13.73** | **$2.33** | **61.0% (155/254)** | **$9.17** |
| $50 | 24.2% (22/91) | 30.1 | $21.22 | $3.76 | 48.0% | $16.42 |
| $100 | 11.0% (10/91) | 41.9 | $29.77 | $7.22 | 31.1% | $27.99 |

(Top-of-book only; live book-walking to +3¢ makes these lower bounds.)
Also: **8/91 OOS trades (8.8%) had `szq < 5` shares — below Polymarket's 5-share minimum —
and are therefore unfillable live.** Train only 2.4%.

---

## 2. Monthly / period stability — is the edge decaying?

| month | days | n | trades/day | EV ¢/sh | win rate | med px | split |
|---|---|---|---|---|---|---|---|
| 2025-10 | 21 | 10 | 0.48 | +16.07 | 0.900 | 0.79 | train |
| 2025-11 | 30 | 21 | 0.70 | +11.13 | 0.810 | 0.68 | train |
| 2025-12 | 31 | 31 | 1.00 | **+33.55** | 1.000 | 0.64 | train |
| 2026-01 | 31 | 107 | **3.45** | **+33.21** | 0.991 | 0.54 | train |
| 2026-02 | 28 | 9 | 0.32 | +8.42 | 0.889 | 0.84 | train |
| 2026-03 | 31 | 39 | 1.26 | **−1.32** | 0.692 | 0.80 | train |
| 2026-04 | 30 | 37 | 1.23 | **+1.95** | 0.703 | 0.68 | train |
| 2026-05 | 31 | 54 | 1.74 | +8.52 | 0.778 | 0.69 | **test** |
| 2026-06 | 30 | 28 | 0.93 | +7.79 | 0.821 | 0.83 | **test** |
| 2026-07 (1–12) | 12 | 9 | 0.75 | **+24.00** | 1.000 | 0.85 | **test** |

Trend tests:
- Per-trade OLS on full sample (n=345): slope **−0.118 ¢/share/day, p < 0.0001** → −10.6¢ per 90 days.
- Per-trade OLS on **OOS only** (n=91): slope **+0.145 ¢/share/day, p = 0.335** — not significant.
- OLS on the 10 monthly EVs: −1.10 ¢/month, **p = 0.448, r² = 0.074** — not significant.
- OOS halves: May 1–Jun 6 = +10.34¢ (n=61) → Jun 8–Jul 12 = +8.78¢ (n=30). Flat.
- OOS last 30 days (Jun 13–Jul 12): n=28, 0.93/day, +11.28¢, wr 0.857.

**Verdict: a large one-off level shift, not an ongoing decay.** The Dec–Jan peak (+33¢ at up to
3.45 trades/day) was a high-volatility regime that contributed 138 of 345 trades (40%). The
significant full-sample downtrend is entirely that step-down; it is *already fully priced into the
+9.8¢ OOS number*. Inside the OOS window there is no detectable further decay.

**But the honest downside is in the train data, not the trend line:** March 2026 (−1.32¢, wr 0.692)
and April 2026 (+1.95¢, wr 0.703) were two consecutive months where this strategy was
**flat-to-negative at ~1.25 trades/day**. That is the realistic bad-regime scenario, and it is
2 months out of 10. Any live scaling decision must survive a repeat of Mar–Apr.

---

## 3. The methodology correction: "decide-then-fill" vs "look-at-the-fill-book"

The published backtest tests the edge **only** against the post-latency book. The live bot:
1. evaluates at `t` on `book[t]` → logs a **signal**;
2. waits `latency_ms = 1500`;
3. re-checks; if the edge is gone → `book_moved_no_edge`.

Set-theoretically the live bot fills on `{edge at book[t]} ∩ {edge at book[t+lat]}`, while the
backtest counts `{edge at book[t+lat]}` alone. The backtest therefore **structurally cannot
produce a book_moved_no_edge outcome and overstates the achievable fill rate.** Comparing live
fills (0.717/day) to the published 1.247/day and concluding "live is 43% short" is an artifact.

So I re-ran the OOS window (73 days, unchanged) with **live semantics and live config**:
`fair_cap = 0.98`, `sigma_1s_floor = 8e-6`, min order 5 shares, edge_min 0.05, last-6 s,
price band 0.30–0.99, edge required at *both* `book[t]` and `book[t+LAT]`.
(Script: `/tmp/.../scratchpad/live_sem.py`; outputs `livesem_lat{1,2}.csv`.)

| | signals/day | fills/day | conversion | EV ¢/sh | win rate | mean px | sh/fill @$25 | $/day | $25 binds |
|---|---|---|---|---|---|---|---|---|---|
| lat = 1 s | 1.192 | 0.890 | 74.7% | +10.34 | 0.800 (52/65) | 0.684 | 23.3 | $2.06 | 40.0% |
| lat = 2 s | 1.192 | 0.589 | 49.4% | +15.60 | 0.837 (36/43) | 0.668 | 23.9 | $2.28 | 41.9% |
| **interp. to 1.5 s (live)** | **1.192** | **0.740** | **62.1%** | **+12.97** | **0.819** | **0.676** | **23.6** | **$2.17** | **40.9%** |

Side effects of the live-only config knobs, measured separately on the published trade set:
- `fair_cap = 0.98` kills 6/91 OOS trades (6.6%) — and those 6 averaged only **+5.92¢**, so the
  cap is mildly *accretive* (survivors +10.10¢). On train it kills 19/254, which averaged +0.79¢.
  53.8% of OOS trades had raw `fair ≥ 0.98`, so the cap touches many trades but rarely kills them.
- min-order 5 shares removes a further 8/91 (8.8%).
- Both filters together: 77/91 → **1.055 trades/day**, EV +8.47¢.

---

## 4. Live vs baseline, with arithmetic

**Live exposure.** 2026-07-16 18:18 → 2026-07-26 12:31 UTC = 9.7590 days.
Eval ticks 1,426 ÷ 6 ticks/window = **237.7 windows evaluated**; calendar predicts
9.759 × 24 = 234.2 windows → 6.09 ticks/window, i.e. **coverage is essentially complete**
(no missed-window problem). I use 237.7 windows as the exposure denominator.

Live derived quantities:
- signals/day = 11 / 9.759 = **1.1272**; per window = 11 / 237.7 = **0.04628**
- fills/day = 7 / 9.759 = **0.7173**; per window = 7 / 237.7 = **0.02945**
- conversion = 7/11 = **0.6364**; `book_moved_no_edge` = 4/11 = 0.3636
- shares/fill = 214.69 / 7 = **30.67**; $/fill = 134.09 / 7 = **$19.16**
- volume-weighted entry px = 134.09 / 214.69 = **0.62456**
- EV/share = 44.97 / 214.69 = **$0.20947 = 20.95 ¢** (this is *share-weighted*, matching how the
  bot reports it; the BT's 9.83¢ is *unweighted per trade* — the BT's share-weighted OOS
  equivalent is 9.10¢ @$10 cap / 9.55¢ @$25 cap, so the weighting choice is worth <0.5¢ here)
- $/day = 44.97 / 9.759 = **$4.608**; deployed 134.09/9.759 = **$13.74/day**

### 4a. FREQUENCY — **IN LINE** (both signals and fills)

Poisson tests against the config-matched baseline, exposure 237.7 windows:

| | expected λ | observed | ratio | P(X ≤ obs) | P(X ≥ obs) | verdict |
|---|---|---|---|---|---|---|
| signals | 1.192/24 × 237.7 = **11.80** | 11 | 0.932 | 0.484 | 0.632 | **IN LINE** |
| fills | 0.740/24 × 237.7 = **7.33** | 7 | 0.956 | 0.550 | 0.598 | **IN LINE** |

Both sit within half a percentile point of the median. Exact 95% count intervals: signals
[5.5, 19.7], fills [2.8, 14.4] — 11 and 7 are dead centre.

For completeness, against the **uncorrected** published rate (0.05230/window → λ = 12.43):
fills observed 7, ratio 0.563, P(X ≤ 7) = 0.0723. Even that naive comparison does not reach
significance, and it is the wrong comparison anyway (§3).

Signal→fill conversion 63.6% live vs 62.1% predicted — **IN LINE**, arguably marginally better,
which is what you'd expect from a 1500 ms budget sitting below the 2 s bracket.

### 4b. EV — **BETTER on the point estimate, NOT statistically distinguishable**

Live +20.95¢ vs baseline +12.97¢ → **ratio 1.615**. Against the raw published OOS +9.83¢ → 2.13×.

Bootstrap the baseline (pooled live-semantics fills, n = 108, EV +12.43¢, wr 0.815),
200,000 resamples of size 7:
- median 7-trade mean = +13.10¢; 95% range **[−11.41, +32.61]¢**
- **P(7-trade mean ≥ 20.95¢) = 0.240** → live EV is the **76th percentile** of what the
  baseline routinely produces at n=7
- P(≥6 wins) = 0.619; joint P(mean ≥ 20.95¢ AND ≥6 wins) = **0.239**

Two-sample z on live vs published OOS: diff = +11.12¢, SE = 14.63¢, **z = 0.76, p = 0.447**.

**Verdict: BETTER but well inside noise.** There is no statistical basis to claim live EV
exceeds backtest. The correct reading is "not worse," which at n=7 is all the data can say.

Dollar cross-check: at baseline EV the 214.69 shares would have returned $27.85
(at raw OOS EV, $21.10) versus the realized $44.97. The $17–24 gap is one favourable tail draw.

### 4c. 95% CONFIDENCE INTERVALS AT n = 7

**Method note (important):** the bot reports aggregates only, not per-trade `pnl_share`. For a
binary-payoff contract at fixed entry price, EV/share is an exact affine function of the win rate:
`EV = p − px − fee(px)`. At the realized volume-weighted `px = 0.62456`,
`fee = 0.07 × 0.62456 × 0.37544 = 0.016417`, so **EV = p − 0.64098**.
I reconstruct the 7 trades as 6 wins at `+0.35902` and 1 loss at `−0.64098`. This reproduces the
reported mean to within 0.7¢ (reconstructed 21.62¢ vs reported 20.95¢; the residual is
price heterogeneity across the 7 fills), so the reconstruction is sound. sd = 37.80¢, SE = 14.29¢.

| method | 95% CI on EV ¢/share |
|---|---|
| Normal (z = 1.96) | **[−7.05, +48.95]** |
| Student-t(6) | [−14.01, +55.90] |
| **Bootstrap, 200k resamples, percentile** ← preferred | **[−6.96, +35.90]** |
| implied by Clopper–Pearson win-rate CI | [−21.97, +35.54] |

**I use the bootstrap.** The n=7 statistic is discrete with only 8 attainable values; the normal
approximation is symmetric and materially overstates the upper tail (+48.95¢ is not attainable
without 7/7). Bootstrap P(mean ≤ 0) = **0.0648**.

Win rate 6/7 = 0.8571:
- **Clopper–Pearson exact 95%: [0.4213, 0.9964]**
- Wilson 95%: [0.4869, 0.9743]

**Every interval contains zero EV / contains 0.50 win rate. n = 7 establishes nothing about EV.**

### 4d. THE EXPLICIT QUESTION — P(≥6 wins | n=7, true p = 0.5)

```
P(X ≥ 6 | n=7, p=0.5) = [C(7,6) + C(7,7)] / 2^7 = (7 + 1)/128 = 8/128 = 1/16
                      = 0.0625 = 6.25%
   P(exactly 6) = 7/128  = 0.054688
   P(exactly 7) = 1/128  = 0.007813
```

**6.25%.** A one-sided exact binomial test of H₀: p = 0.5 yields **p = 0.0625 — it does not
clear the 5% bar.** Six-of-seven is what a fair coin delivers one time in sixteen.

For calibration, P(≥6 wins) under other hypotheses:
| true p | 0.50 | 0.60 | 0.70 | 0.757 (BT lat2) | 0.813 (BT OOS) | 0.882 (BT train) |
|---|---|---|---|---|---|---|
| P(≥6/7) | 0.0625 | 0.159 | 0.329 | 0.462 | 0.613 | 0.804 |

Likelihood ratio, backtest hypothesis vs coin-flip: 0.6133 / 0.0625 = **9.8 : 1**. Suggestive,
consistent with the edge being real — but a ~10:1 LR from a single 7-observation window is not
a basis for committing capital.

### 4e. SIZE — **BETTER, and explained**

Live 30.67 shares/fill vs baseline 23.63 (**1.298×**); live $19.16/fill vs $15.97.
This is the expected direction: the backtest consumes **top of book only**, while the live bot
walks up to `max_walk_above_best = 0.03`. Live is capturing ~30% more size per fill from
deeper levels. Live deployed $13.74/day vs baseline $11.82/day.

$/day: live $4.61 vs baseline $2.17 = **2.12×**, which decomposes as EV 1.615× × size 1.298×
= 2.10×. Both factors are individually inside noise; the product is not independent evidence.

$25 cap: baseline says it binds **~41%** of fills. Live avg fill $19.16 = 77% of cap, consistent
with a cap that binds on a large minority. **There is real headroom to raise it** — see §6.

---

## 5. settle_sweep 1h — 1,235 signals, 1,235 empty_book, 0 fills

- Published backtest OOS (`bt_1h_settle_test.csv`): n = 12 fills, **0.164/day**, EV +17.21¢, wr 1.000.
- Expected live fills over 9.759 days = 0.164 × 9.759 = **1.60**. P(X = 0 | λ = 1.60) = **0.201**.
  So **0 fills is not yet statistically inconsistent** with the backtest on count alone;
  you would need ~18 continuous days of zeros (λ ≈ 3.0) to reject at 5%.
- **But the count test is the weak test.** The informative datum is that **100% of 1,235 signal
  evaluations found an empty book** — not a thin book, an *empty* one. 1,235 / 237.7 = 5.2
  sweep evaluations per window, and not one found resting winner-side size.
- This corroborates `docs/04_executability_audit.md` precisely: only **2%** of 1h closes ever
  show a cheap winner ask, with **0.2 s median and 0.2 s p90 persistence**, plus the live
  snapshot showing winner asks empty while the loser side carries 983k shares at 0.01.

**Verdict: IN LINE with the executability audit (which already declared it dead), WORSE than the
original backtest (which never modelled sub-second persistence).** The +17.21¢/0.164-per-day
figure in `oos_summary.csv` should be treated as **not executable** and removed from any forward
P&L projection. Keep the leg running in measurement mode — it costs nothing — but expect zero,
and do not fund it. The `empty_book` reason code is doing its job.

---

## 6. Conclusions, caveats, and what would change the answer

1. **Frequency is validated.** Signals 11 vs 11.80 expected, fills 7 vs 7.33 expected. The signal
   generator, the Binance oracle wiring, the σ estimator and the edge test are all behaving as
   the research predicted. The 36% `book_moved_no_edge` rate is not a defect — it is the
   previously-unmodelled attrition, and at 1500 ms it lands where the 1–2 s bracket says it should.
2. **EV is unproven, in both directions.** 95% CI on live EV/share = [−7.0, +35.9]¢ (bootstrap).
   6-of-7 wins has a 6.25% chance under a coin flip. Live is a 76th-percentile draw from the
   baseline. **Do not scale on the strength of +20.95¢.**
3. **Sample size needed.** To get a 95% EV interval excluding zero you need roughly
   n ≥ (1.96 σ / EV)². At σ = 30.1¢ and the baseline EV = 12.97¢ → **n ≈ 21 fills ≈ 29 more days**
   at the current 0.717 fills/day. At the optimistic live EV = 20.95¢ (σ = 37.8¢) → n ≈ 13 fills
   ≈ 18 days. **Budget 30–45 more days of paper before the EV number carries weight.**
4. **The live window may be a favourable regime.** The vault ends 2026-07-12; the bot started
   2026-07-16, so there is *zero* historical overlap with the live period. The nearest analog,
   OOS Jul 1–12, ran **+24.00¢ on n=9 with a 1.000 win rate** (per-trade ¢:
   6.5, 7.5, 18.9, 37.3, 6.5, 50.3, 14.1, 65.5, 9.4). Live +20.95¢ is a clean continuation of
   *that fortnight*, not of the +9.8¢ OOS average. Mar–Apr 2026 (−1.3¢, +2.0¢) shows what the
   other regime looks like.
5. **Liquidity has thinned structurally.** Median top-of-book depth at signal time fell from
   64.6 shares (train) to 16.8 (OOS) — ~14× on the dollar measure ($681 → $46 mean).
   8.8% of OOS signals were below the 5-share minimum. Live's 30.67 shares/fill is healthy
   against that backdrop and suggests book-walking is genuinely adding capacity.
6. **The $25 cap binds ~41% of the time** and live fills average $19.16 (77% of cap).
   Raising the cap is the highest-confidence lever available — it is a *sizing* change backed by
   measured depth, not an *edge* claim. But it should wait for (3): doubling size doubles the
   variance of an EV estimate that currently spans zero. Recommended order of operations:
   confirm EV over 30+ more days at $25, then raise.
7. **Watch for a divergence tell.** If fills/day holds near 0.74 while EV drifts toward the
   Mar–Apr regime (~0¢), that is edge decay, not execution failure. If fills/day collapses while
   EV holds, that is competition arriving at the book. The two failure modes are separable in
   this instrumentation — that is the main value of having signals, fills and reason codes logged.

## Reproduction
- `/tmp/claude-0/-home-user-S4/481385a7-e66e-52ff-943a-8c87efc9551d/scratchpad/an1.py` — published-backtest summary stats
- `.../an2.py` — exact top-of-book depth reconstruction from raw quotes (91/91, 254/254 verified)
- `.../an3.py` — monthly stability, trend tests, share-weighted EV
- `.../an4.py` — live-vs-published comparison, CIs, binomial
- `.../an5.py` — fair_cap / sigma-floor / min-order filter effects
- `.../live_sem.py` — **live-semantics OOS re-run** (the §3 correction); `livesem_lat{1,2}.csv`
- `.../an6.py` — final config-matched comparison and Poisson tests
