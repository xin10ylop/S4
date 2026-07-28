# C4 — Sigma recalibration and 5m capacity

**Date:** 2026-07-28 · **Scope:** derive `sigma_1s_floor` and `vol_window_secs` for a 5-minute
contract from the Chainlink return process itself; run the pre-committed 6×4 grid in-sample (repo)
and out-of-sample (fresh); answer whether a recalibrated floor flips the 5m go/no-go; measure 5m
capacity from the 25-level book tape.

**Harness:** `scripts/fresh5m/replay.py` (audit/C2 — ported from `bot/polybot/strategy.py` and
`fill_engine.py`, 0 property-test mismatches on 80,000 randomised inputs), driven by
`scripts/c4/{sigma_dist,sigma_fit,grid,subperiods,decision,capacity,cap_report,ladder_cells,derived_floor}.py`.

---

## VERDICT

| question | answer |
|---|---|
| **What should the 5m floor be?** | **≈4 × 10⁻⁵** — 5× the shipped `8e-6`, and *above* the verifier's suggested `3e-5`. Three independent empirical routes agree (§1.4). |
| **Does recalibrating it change the DECISION?** | **No.** The verifier's `3e-5` makes things strictly worse. The derived `4e-5` throws up the **highest t_day in the study (2.390)** on the top-of-book tape — but that **falls to 2.007 under the bot's real ladder execution, 1.70 after dropping two known-bad data days, and 0.66 post-break**. It fails every frame. §2.5. |
| **Does the edge exist only at one floor?** | The **edge** does not (+4–5¢ at every cell). The **significance** briefly appeared to — 4e-5 spiking to 2.39 between neighbours at 1.83 and 1.60. **It was an artifact of the simplified top-of-book fill model**, plus p = 0.296 paired against the shipped floor, all grid p ≥ 0.72, and 1.28 effective independent tests. §2.5, §3.4. |
| **Do any cells pass the pre-committed rule?** | 2 of 8 under real ladder execution, both at floor **≤ 8e-6** with `vol_window` moved 120→60, both at t_day **2.02–2.05 against a threshold of 2.0**. Both fail on dropping two known-bad data days. **Zero of the 25 configurations tested (24 grid + the derived 4e-5) pass on the post-2026-06-27 or July-only frames.** §3.2–3.3. |
| **Capacity per 5m signal** | p10 **$8.03** · **p50 $80.99** · p90 **$491.72** inside the bot's own 3¢ walk bound. §4.1. |
| **What clip can 5m support?** | **$100–250.** Marginal return per $ of cap: $0.47/day ($25→$100), $0.25 ($100→$250), **$0.07** ($250→$500). Fills above $1,000 notional have **negative** EV. §4.3–4.4. |
| **Net recommendation** | **5m stays OFF**, and **`sigma_1s_floor` stays at 8e-6 in `config.yaml`.** 4e-5 is the right floor for the *Chainlink* families, but the knob is global and the live 1h family reads *Binance* — where 4e-5 would bind on **66.5%** of decisions instead of today's 7.8%. Make the knob per-family before anyone uses the new value. **§6.1 — read this before touching config.** |

The task asked me to say plainly whether this is a case of tuning a parameter to rescue a failing
result. **It came closer than I expected, and the honest account is in §2.5.** The verifier's `3e-5`
does not reproduce on 20+ clean days — it gives **+3.48¢ / t_day 1.47** under real execution, worse
than the shipped floor on both axes, against the claimed +3.66¢ → +6.40¢ improvement. But the floor I
derived independently from the return distribution, `4e-5`, threw up **t_day 2.390 — the highest
number in this study** — on the top-of-book tape. Re-measured with the bot's actual 25-level ladder
fill model on the same days it is **2.007**, then **1.70** once the two known-bad Chainlink days come
out, and **0.66** post-break. It fails every frame.

Two lessons worth carrying forward, both of which nearly caught me:

* **A grid can skip the value that matters.** 4e-5 was not in the 6×4 grid; it only appeared because
  §1 derived it from the return distribution before looking at any P&L. Had it been in the grid it
  would have been the cell to report.
* **A top-of-book fill model can manufacture significance.** The entire spike — 2.390 vs 2.007 — was
  the simplified execution model, on identical days and parameters.

---

## 0. What was run, and the discipline

### 0.1 Parameters — read, not guessed

From `bot/config.yaml` and `bot/polybot/strategy.py`:

```
edge_min 0.03 | price_min 0.30 | price_max 0.99 | fee 0.07·p·(1−p) taker, 0 maker
vol_window_secs 120 | sigma_1s_floor 8.0e-06 | fair_cap 0.98
snipe_last_secs 5.0 | snipe_min_tau_secs 2.5 | snipe_fill_margin_secs 0.5 | latency_ms 1500
  -> tau_lo = max(2.5, 1.5+0.5) = 2.5 -> band [2.5, 5.0] -> ticks tau in {5,4,3}
per_event_cap_usd 250 | max_walk_above_best 0.03
ONE entry per window, first qualifying tick, first qualifying side
fill: re-fetch the book latency_ms later, walk asks while edge > edge_min and price <= best+0.03
fair = Phi( ln(S_t/S_open) / (sigma_1s·sqrt(tau)) ), clipped to [0.02, 0.98]
winner: result_id (5m settles on Chainlink Data Streams)
book staleness: <= 5 s, rejected AT DECISION TIME (PRE convention — a stale book is handed to the
  evaluator as None, exactly what the live bot sees when it cannot fetch a book)
```

### 0.2 In-sample / out-of-sample discipline

| frame | days | windows | role |
|---|---|---|---|
| **repo_IS** — 2026-04-02→05-12 + 07-06/07 | 43 | 12,367 | **IN-SAMPLE. Tuning only.** |
| **fresh_OOS** — 2026-06-01→07-26 (ex 07-06/07) | 54 | 15,543 (quotes) / 3,889 (books) | **OUT-OF-SAMPLE. Reporting.** |

Every number below is labelled. No cell was chosen on fresh data and then reported on fresh data
without saying so.

Both tapes are **top-of-book** in the 6×4 grid, so the two periods are directly comparable
(repo → `daily/5m/bookcurves`, one level; fresh → `data/fresh5m/quotes`, real two-sided top of book
for **all 288 windows/day**, no candidate prefilter and therefore no selection bias). The 25-level
**ladder** runs (§2.4, §4) use `data/fresh5m/books` and are the bot's real execution.

### 0.3 CAUSALITY — enforced, and I state the result

Every decision reads only Chainlink reports with `server_timestamp_us <= t`. Publication lag is
~1.1 s at p50, so using observation time instead invents foresight. Measured over this run's own
decision grid (τ ∈ {5,4,3} at every 5m close):

| period | decisions | would use an **unpublished** report under `timestamp_us <= t` | median-day mean foresight |
|---|---|---|---|
| repo_IS | 37,152 | **36,715 (98.82%)** | 1.733 s |
| fresh_OOS | 46,656 | **45,291 (97.07%)** | 1.630 s |

C3 priced this trap at **+1.95¢/share** on the fresh tape and C2 at +1.89¢ on the repo tape. It is
switched off in every number in this document.

### 0.4 Coverage check on the books tape

`data/fresh5m/books` was fetched only for `candidates.parquet` windows, so I measured rather than
assumed the coverage: of the 854 fills the **unfiltered** quotes tape produces at the shipped cell,
**852 (99.77%)** also have a book, and 1,711 of 1,712 signals are present. The prefilter is a
near-exact superset; the capacity numbers in §4 are not selection-biased.

### 0.5 Verification

Every headline cell was recomputed from the raw per-trade parquet tapes independently of the
summariser. All matched to 3 decimal places:

```
grid quotes w120_f8e-6   nd=54 sig=1712 fills=854 tpd=15.81 ev=4.339c t_day=2.140 fill_rate=0.499
grid quotes w60_fnone    nd=54 sig=1791 fills=912 tpd=16.89 ev=4.619c t_day=2.500 fill_rate=0.509
ladder w60_f8e-06        nd=54 sig=1779 fills=897 tpd=16.61 ev=4.071c t_day=2.020 fill_rate=0.504
ladder w120_f8e-06       nd=54 sig=1724 fills=858 tpd=15.89 ev=3.758c t_day=1.746 fill_rate=0.498
ladder w60_f0            nd=54 sig=1793 fills=916 tpd=16.96 ev=4.048c t_day=2.047 fill_rate=0.511
```

---

## 1. What the 1s Chainlink return process actually is at a 5-minute horizon

The shipped `sigma_1s_floor = 8e-6` and `vol_window_secs = 120` were calibrated on the **1h** family
against **Binance 1s klines**. `oracle.ChainlinkOracle` already carries an explicit calibration
warning that the Chainlink feed moves nearly every second whereas Binance klines are mostly flat.
This section measures the actual object, **at real 5m decision instants** (t = close − τ, τ ∈ 2..5 s)
under strict publication causality — not on a convenience grid, because the decision instant is where
the parameter is used.

### 1.1 The raw 1s log-return distribution

`data/c4/sigma_raw_returns.csv`. Consecutive held prints, no forward fill (the `bot` convention).

| period | subset | n | **std** | MAD | zero-return frac | p90 abs | p99 abs | kurtosis |
|---|---|---|---|---|---|---|---|---|
| repo_IS | gap == 1 s only | 3,612,323 | **4.391e-05** | 4.60e-06 | 6.63% | 5.02e-05 | 1.80e-04 | 127.5 |
| repo_IS | all consecutive prints | 3,648,157 | 4.457e-05 | 4.65e-06 | 6.58% | 5.07e-05 | 1.82e-04 | 138.9 |
| fresh_OOS | gap == 1 s only | 4,418,644 | **5.168e-05** | 5.20e-06 | 2.85% | 5.89e-05 | 2.13e-04 | 96.9 |
| fresh_OOS | all consecutive prints | 4,480,775 | 5.417e-05 | 5.21e-06 | 2.81% | 6.01e-05 | 2.16e-04 | 5098.2 |

99.0% (repo) / 98.6% (fresh) of consecutive prints are exactly 1 s apart, so the "1s return" is a real
object and not an artifact of resampling.

In readable units, at a median BTC of $76,145 (repo) / $63,658 (fresh):

* **σ₁ₛ = 4.39e-05 (repo) / 5.17e-05 (fresh)** → a 1-sd 5-minute move of **0.076% / 0.090%**
  (≈ $58 / $57), i.e. **24.7% / 29.0% annualised**.
* **The shipped floor of 8e-6 is 5.5× BELOW the unconditional 1s volatility of the series it is
  supposed to floor** (18.2% of it on repo, 15.5% on fresh). It is not a floor in any meaningful
  sense; it is a divide-by-zero guard.

⚠️ The fresh "all consecutive prints" kurtosis of 5,098 is a data artifact, not a market event. The
largest |return| on each of the worst days spans a Chainlink **coverage gap**, not one second:
2026-06-05 (−0.0202 across a 5,045 s gap), 06-01 (6,573 s), 06-10 (12,939 s), 06-11 (29,033 s). The
`bot` sigma mode counts a return across a gap as if it were a 1s return, so it runs hot exactly on the
days C1 flagged for poor Chainlink coverage. **The `gap == 1 s only` row is the honest measurement**
and is what §1.4 uses.

### 1.2 The trailing estimator at real decision instants

`data/c4/sigma_hat_dist.csv`. σ̂ = std of 1s log returns over the trailing `vol_window_secs`, computed
by the live bot's own estimator (`ChainlinkOracle.rolling_log_return_std`, only the seconds actually
held). 48,790 (repo) / 59,947 (fresh) decision instants.

| period | window | p1 | p10 | **p50** | p90 | p99 | mean |
|---|---|---|---|---|---|---|---|
| repo_IS | 30 | 1.57e-06 | 7.15e-06 | **2.31e-05** | 5.91e-05 | 1.35e-04 | 3.00e-05 |
| repo_IS | 60 | 3.34e-06 | 1.00e-05 | **2.56e-05** | 6.08e-05 | 1.29e-04 | 3.21e-05 |
| repo_IS | **120** | 5.54e-06 | 1.22e-05 | **2.75e-05** | 6.20e-05 | 1.31e-04 | 3.38e-05 |
| repo_IS | 300 | 8.12e-06 | 1.48e-05 | **3.07e-05** | 6.55e-05 | 1.30e-04 | 3.71e-05 |
| fresh_OOS | 30 | 1.44e-06 | 6.55e-06 | **2.48e-05** | 6.97e-05 | 1.59e-04 | 3.42e-05 |
| fresh_OOS | 60 | 3.05e-06 | 9.62e-06 | **2.80e-05** | 7.28e-05 | 1.59e-04 | 3.68e-05 |
| fresh_OOS | **120** | 4.45e-06 | 1.22e-05 | **2.99e-05** | 7.46e-05 | 1.58e-04 | 3.88e-05 |
| fresh_OOS | 300 | 6.95e-06 | 1.50e-05 | **3.36e-05** | 7.85e-05 | 1.60e-04 | 4.19e-05 |

**How often the floor binds at all** (fraction of decisions with σ̂ below it):

| period | window | 8e-6 (shipped) | 3e-5 (verifier) | 5e-5 |
|---|---|---|---|---|
| repo_IS | 120 | **3.2%** | 56.3% | 83.4% |
| fresh_OOS | 120 | **4.1%** | 49.3% | 76.3% |
| fresh_OOS | 60 | **7.6%** | 53.8% | 78.3% |
| fresh_OOS | 30 | **12.5%** | 59.3% | 80.3% |

**The shipped floor is inert on 5m: it touches 3–4% of decisions at the shipped 120 s window.** A
recalibrated floor at 3e-5 touches half of them, and at 5e-5 three quarters. That is why the
verifier saw a large move from changing it — the floor stops being a guard and becomes the model.

### 1.3 The estimator systematically UNDERSTATES forward volatility

`data/c4/sigma_calibration.csv`. For each decision instant, the realized forward move to settle,
scaled to a 1s rate: `|ln(S_settle/S_t)| / sqrt(tau_eff)`. `tau_eff` is measured from the *observation*
time of the print we hold to the close, so the ~1.1 s of publication lag we cannot see is charged as
risk, not ignored.

Unconditionally:

| period | window | mean σ̂ | realized fwd 1s **RMS** | **ratio** |
|---|---|---|---|---|
| repo_IS | 30 | 3.00e-05 | 6.54e-05 | **2.18×** |
| repo_IS | 120 | 3.38e-05 | 6.54e-05 | **1.94×** |
| repo_IS | 300 | 3.71e-05 | 6.54e-05 | **1.76×** |
| fresh_OOS | 30 | 3.42e-05 | 6.34e-05 | **1.88×** |
| fresh_OOS | 120 | 3.88e-05 | 6.34e-05 | **1.63×** |
| fresh_OOS | 300 | 4.19e-05 | 6.34e-05 | **1.51×** |

And the miss is worst exactly where a floor is supposed to act — in the quiet tail. Bottom ventiles
of σ̂ at `vol_window = 120`:

| ventile | repo σ̂ | repo realized | ratio | fresh σ̂ | fresh realized | ratio |
|---|---|---|---|---|---|---|
| 1 (quietest 5%) | 7.05e-06 | **5.15e-05** | **7.31×** | 6.06e-06 | **3.02e-05** | **4.99×** |
| 2 | 1.08e-05 | 5.08e-05 | 4.68× | 1.04e-05 | 3.52e-05 | 3.39× |
| 3 | 1.33e-05 | 4.39e-05 | 3.30× | 1.32e-05 | 3.72e-05 | 2.81× |
| 4 | 1.55e-05 | 5.03e-05 | 3.25× | 1.56e-05 | 3.80e-05 | 2.43× |
| 5 | 1.74e-05 | 5.34e-05 | 3.07× | 1.78e-05 | 4.16e-05 | 2.33× |

**When the trailing estimator says 7e-6, the price actually moves at 3.0–5.2e-5.** A floor at 8e-6
does nothing about that. This is the single strongest piece of evidence in the whole document that the
shipped floor is wrong for 5m.

### 1.4 Three independent routes to the floor — they agree on ≈4e-5

I did **not** grid-search the floor against P&L to pick it. Three routes, none of which sees a
single trade:

| route | repo_IS | fresh_OOS |
|---|---|---|
| **(a)** unconditional 1s print volatility (§1.1) | 4.39e-05 | 5.17e-05 |
| **(b)** realized forward 1s vol conditional on σ̂ being in its quietest ventile (§1.3) | 5.15e-05 | 3.02e-05 |
| **(c)** log-loss-optimal floor for `fair = Phi(z)` over all 48,790 / 59,947 decisions (`sigma_fit.csv`, w=120) | 5.77e-05 | 3.90e-05 |

**All three land in 3–6 × 10⁻⁵. The centre of that range is ≈4 × 10⁻⁵ — 5× the shipped `8e-6` and
above the verifier's `3e-5`.** Route (c) is worth reading twice: it is a pure probability-calibration
fit, scored by log-loss, with no reference to prices, books, fills, or P&L.

Route (c) also rules out the alternative shapes. Fitting `sigma = k·σ̂` (multiplier only) or
`sigma = max(k·σ̂, F)` (both) against the same objective:

| period | w | shipped floor log-loss | best floor | best-floor log-loss | best multiplier | best-mult log-loss | best (k, F) |
|---|---|---|---|---|---|---|---|
| repo_IS | 120 | 0.20960 | **5.77e-05** | **0.19683** | 2.053 | 0.20062 | k=1.32, F=5.61e-05 |
| fresh_OOS | 120 | 0.14760 | **3.90e-05** | **0.14086** | 1.446 | 0.14630 | k=0.88, F=4.10e-05 |

The joint fit collapses onto the floor (k ≈ 0.9–1.3, i.e. no meaningful multiplier), so **a floor is
the right shape** — the estimator is fine on average and wrong in the quiet tail, which is exactly
what a floor fixes. The log-loss gain is real but modest: **6.1% (repo), 4.6% (fresh)**.

⚠️ Honest caveat on route (c): Brier score moves the *other* way on repo (0.05277 at 8e-6 →
0.05345 at the best floor). Log-loss is dominated by confident mistakes, Brier by the bulk. The floor
buys tail safety and costs a little bulk sharpness. I used log-loss because avoiding confident
mistakes is the entire reason `sigma_1s_floor` and `fair_cap` exist (`config.yaml` line 139 says so).

### 1.5 The vol window: the data barely cares

The window choice is usually presented as a P&L question. It should be a forecasting question, so I
measured forecasting skill directly — Spearman rank correlation between σ̂(w) and the realized
forward 1s move:

| period | w=30 | w=60 | w=120 | w=300 |
|---|---|---|---|---|
| repo_IS | +0.343 | +0.346 | **+0.348** | +0.346 |
| fresh_OOS | +0.398 | **+0.403** | +0.400 | +0.391 |

**The four windows are indistinguishable as volatility forecasters — a spread of 0.005 in rank
correlation.** Whatever P&L difference appears between `w=60` and `w=120` in §2, it is **not** a
volatility-forecasting improvement. Hold that thought; §3.4 is where it matters.

(The OLS variance regression is not usable on the fresh tape — the gap-spanning returns of §1.1 give
it R² = 0.43 at w=30 with a zero intercept, driven by a handful of points. The rank statistic above is
robust to them and is what I report.)

### 1.6 What the floor SHOULD be — summary

> **`sigma_1s_floor` for a 5-minute contract should be ≈4 × 10⁻⁵, not 8 × 10⁻⁶.**
> **`vol_window_secs` is not empirically determined; 60–120 s are equivalent forecasters.**

This is the answer to task item 1, derived before any P&L was looked at. Section 2 now asks what it
does to the money, and section 3 asks whether it changes the decision. It does not.

---

## 2. The 6×4 grid — tuned on repo (IS), reported on fresh (OOS)

`data/c4/grid.csv`. Top-of-book execution, $250 cap, book age ≤ 5 s (PRE), strict causality,
τ ∈ {5,4,3}, `edge_min` 0.03. `t_day` is the day-clustered statistic and is the pre-committed one.

### 2.1 IN-SAMPLE (repo, 43 days, 12,367 windows) — tuning frame only

| vol_win | floor | signals | fills | trades/day | **¢/share** | win | t_trade | **t_day** | $/day |
|---|---|---|---|---|---|---|---|---|---|
| 30 | none | 2,448 | 1,296 | 30.14 | +9.545 | 77.8% | 8.72 | 4.82 | 1,015 |
| 30 | 8e-6 | 2,431 | 1,277 | 29.70 | +9.566 | 77.6% | 8.65 | 4.72 | 997 |
| 30 | 1e-5 | 2,421 | 1,267 | 29.47 | +9.534 | 77.4% | 8.56 | 4.58 | 994 |
| 30 | 2e-5 | 2,308 | 1,186 | 27.58 | +9.505 | 76.5% | 8.12 | 4.21 | 965 |
| 30 | 3e-5 | 2,169 | 1,096 | 25.49 | +9.863 | 75.5% | 7.97 | 4.03 | 943 |
| 30 | 5e-5 | 1,916 | 919 | 21.37 | +10.048 | 72.1% | 7.14 | 3.26 | 906 |
| **60** | **none** | 2,423 | 1,246 | 28.98 | +9.785 | 77.4% | 8.68 | **4.94** | 1,023 |
| 60 | 8e-6 | 2,411 | 1,236 | 28.74 | +9.719 | 77.3% | 8.58 | 4.89 | 1,004 |
| 60 | 1e-5 | 2,406 | 1,229 | 28.58 | +9.695 | 77.2% | 8.51 | 4.74 | 1,002 |
| 60 | 2e-5 | 2,308 | 1,171 | 27.23 | +9.579 | 76.4% | 8.11 | 4.38 | 969 |
| 60 | 3e-5 | 2,173 | 1,088 | 25.30 | +9.938 | 75.5% | 7.99 | 4.12 | 951 |
| **60** | **5e-5** | 1,913 | 919 | 21.37 | **+10.106** | 72.1% | 7.17 | 3.37 | 908 |
| 120 | none | 2,391 | 1,243 | 28.91 | +9.120 | 76.7% | 7.99 | 4.79 | 983 |
| **120** | **8e-6 (shipped)** | 2,388 | 1,239 | 28.81 | **+9.182** | 76.8% | 8.03 | **4.75** | 983 |
| 120 | 1e-5 | 2,381 | 1,234 | 28.70 | +9.169 | 76.7% | 7.99 | 4.65 | 982 |
| 120 | 2e-5 | 2,300 | 1,188 | 27.63 | +9.193 | 76.2% | 7.79 | 4.36 | 973 |
| 120 | 3e-5 | 2,168 | 1,101 | 25.61 | +9.526 | 75.1% | 7.65 | 4.08 | 954 |
| 120 | 5e-5 | 1,912 | 932 | 21.67 | +9.628 | 71.9% | 6.85 | 3.16 | 906 |
| 300 | none | 2,318 | 1,185 | 27.56 | +9.300 | 76.0% | 7.87 | 4.40 | 968 |
| 300 | 8e-6 | 2,317 | 1,184 | 27.54 | +9.370 | 76.1% | 7.94 | 4.38 | 968 |
| 300 | 1e-5 | 2,313 | 1,182 | 27.49 | +9.371 | 76.1% | 7.93 | 4.38 | 968 |
| 300 | 2e-5 | 2,253 | 1,155 | 26.86 | +9.290 | 75.7% | 7.72 | 4.20 | 960 |
| 300 | 3e-5 | 2,137 | 1,090 | 25.35 | +9.695 | 75.0% | 7.74 | 4.08 | 952 |
| 300 | 5e-5 | 1,904 | 927 | 21.56 | +9.909 | 71.8% | 7.06 | 3.46 | 902 |

IS reading: **EV/share rises with the floor** (9.18 → 9.63 at w=120, monotone from 1e-5 up) while
**trades/day falls 28.8 → 21.7 and t_day falls 4.75 → 3.16**. The floor is a selectivity knob. If you
tune on EV/share you pick `w60_f5e-5`; if you tune on t_day you pick `w60_fnone`. Two defensible
in-sample criteria point at opposite ends of the floor axis — **which is already a warning that the
floor axis carries no information.**

### 2.2 OUT-OF-SAMPLE (fresh, 54 days, 15,543 windows) — the reporting frame

| vol_win | floor | signals | fills | trades/day | **¢/share** | win | t_trade | **t_day** | $/day |
|---|---|---|---|---|---|---|---|---|---|
| 30 | none | 1,830 | 949 | 17.57 | +3.974 | 74.5% | 3.06 | 1.92 | 76.3 |
| 30 | 8e-6 | 1,802 | 928 | 17.19 | +4.096 | 74.2% | 3.11 | **2.03** | 75.0 |
| 30 | 1e-5 | 1,784 | 915 | 16.94 | +3.920 | 73.8% | 2.94 | 1.92 | 72.9 |
| 30 | 2e-5 | 1,655 | 824 | 15.26 | +3.742 | 71.8% | 2.58 | 1.46 | 65.7 |
| 30 | 3e-5 | 1,507 | 746 | 13.82 | +3.536 | 69.8% | 2.26 | 1.18 | 74.4 |
| 30 | 5e-5 | 1,301 | 602 | 11.15 | +4.679 | 66.8% | 2.66 | 0.97 | 86.8 |
| **60** | **none** | 1,791 | 912 | 16.89 | +4.619 | 74.3% | 3.48 | **2.50** | 85.0 |
| **60** | **8e-6** | 1,770 | 892 | 16.52 | +4.629 | 74.0% | 3.44 | **2.47** | 82.4 |
| 60 | 1e-5 | 1,755 | 878 | 16.26 | +4.597 | 73.7% | 3.37 | **2.46** | 80.1 |
| 60 | 2e-5 | 1,646 | 808 | 14.96 | +4.550 | 72.3% | 3.12 | **2.47** | 71.1 |
| 60 | 3e-5 | 1,517 | 730 | 13.52 | +4.277 | 70.3% | 2.72 | 2.06 | 75.6 |
| 60 | 5e-5 | 1,303 | 597 | 11.06 | **+5.261** | 67.2% | 2.98 | 1.69 | 88.5 |
| 120 | none | 1,725 | 867 | 16.06 | +4.241 | 73.0% | 3.07 | **2.09** | 68.3 |
| **120** | **8e-6 (shipped)** | 1,712 | 854 | 15.82 | **+4.339** | 72.8% | 3.11 | **2.14** | 67.0 |
| 120 | 1e-5 | 1,703 | 844 | 15.63 | +4.229 | 72.5% | 3.00 | **2.05** | 65.6 |
| 120 | 2e-5 | 1,621 | 790 | 14.63 | +4.107 | 71.3% | 2.76 | 1.85 | 55.4 |
| 120 | **3e-5 (verifier)** | 1,502 | 719 | 13.32 | **+4.086** | 69.5% | 2.57 | **1.83** | 67.5 |
| 120 | 5e-5 | 1,305 | 597 | 11.06 | +5.010 | 66.8% | 2.84 | 1.60 | 85.0 |
| 300 | none | 1,624 | 816 | 15.11 | +4.412 | 72.2% | 3.07 | 1.66 | 62.3 |
| 300 | 8e-6 | 1,621 | 809 | 14.98 | +4.476 | 72.1% | 3.10 | 1.68 | 61.9 |
| 300 | 1e-5 | 1,617 | 800 | 14.82 | +4.393 | 71.8% | 3.01 | 1.59 | 60.9 |
| 300 | 2e-5 | 1,550 | 763 | 14.13 | +4.507 | 70.9% | 2.97 | 1.57 | 58.4 |
| 300 | 3e-5 | 1,455 | 705 | 13.06 | +4.576 | 69.6% | 2.85 | 1.74 | 66.2 |
| 300 | 5e-5 | 1,280 | 592 | 10.96 | +5.176 | 66.7% | 2.92 | 1.48 | 83.3 |

**OOS reading — the answer to task item 2:**

* **Across the six grid floors the floor is BAD for t_day at every window** — flat up to 1e-5, then
  falling. At w=60: 2.50 → 2.47 → 2.46 → 2.47 → 2.06 → **1.69**. At w=120: 2.09 → 2.14 → 2.05 →
  1.85 → 1.83 → 1.60. **The verifier's `3e-5` gives 1.83 at the shipped window against the shipped
  floor's 2.14.** ⚠️ This apparent monotonicity **does not survive adding a seventh point**: the
  derived 4e-5, which the grid skips, spikes to **t_day 2.39** between the 1.83 and the 1.60. §2.5
  reports that in full and explains why it is noise. Read §2.5 before quoting this bullet.
* EV/share is essentially flat in the floor and then bumps at 5e-5 — a fewer-trades artifact, not an
  improvement (§3.4 shows the bump is not statistically distinguishable from zero).
* The **window**, not the floor, carries whatever signal exists: **`w=60` has the highest t_day at
  every one of the six floors, in-sample and out-of-sample.** At the shipped floor the full ordering
  is **w60 > w120 > w30 > w300** on *both* tapes (IS 4.89 / 4.75 / 4.72 / 4.38; OOS 2.47 / 2.14 /
  2.03 / 1.68). (The ordering of the lower three swaps around at other floors — at floors ≥ 2e-5
  w30 rather than w300 is worst OOS — so only the w=60 result is stable across the whole grid.)

### 2.3 IS → OOS transfer

`scripts/c4/subperiods.py`, Spearman rank correlation across the 24 cells:

| statistic | ρ (IS vs OOS) | p |
|---|---|---|
| trades/day | **+0.966** | <0.0001 |
| win rate | **+0.979** | <0.0001 |
| **t_day** | **+0.736** | <0.0001 |
| **EV/share** | **+0.474** | 0.019 |

Mechanical quantities (trades, win rate) transfer almost perfectly, as they must. The economic ones
transfer weakly. Best IS cell by EV (`w60_f5e-5`, +10.11¢) is also the best OOS cell by EV
(+5.26¢) — but its OOS t_day is 1.69, **10th-worst of the 24**.

**How much independent evidence is in 24 cells?** The daily-mean P&L series across the 24 cells have
a **mean pairwise correlation of 0.874** (min 0.701); the first principal component explains **88.0%**
of the variance, and the effective number of independent tests is **1.28**. The grid is one
experiment wearing 24 hats. That cuts both ways: there is no multiple-comparisons minefield here, but
equally **the grid cannot rescue the result**, because every cell is the same trade with a different
filter on top.

### 2.4 Real execution — 25-level ladder, $250 clip, the bot's actual fill model

`data/c4/ladder_cells.csv`. Fresh OOS, 54 days, 3,889 windows with real 25-level depth.

| cell | signals | fills | trades/day | **¢/share** | win | fill rate | **t_day** | $/day |
|---|---|---|---|---|---|---|---|---|
| **w60 · floor 0** | 1,793 | 916 | 16.96 | +4.048 | 74.3% | 51.1% | **2.047** | 114.4 |
| **w60 · 8e-6** | 1,779 | 897 | 16.61 | +4.071 | 74.0% | 50.4% | **2.020** | 112.1 |
| w60 · 3e-5 | 1,528 | 733 | 13.57 | +3.676 | 70.3% | 48.0% | 1.664 | 92.6 |
| w60 · 5e-5 | 1,314 | 598 | 11.07 | +4.655 | 67.1% | 45.5% | 1.372 | 96.7 |
| **w120 · 8e-6 (SHIPPED)** | 1,724 | 858 | 15.89 | **+3.758** | 72.8% | 49.8% | **1.746** | 95.0 |
| w120 · 3e-5 (verifier) | 1,513 | 722 | 13.37 | +3.479 | 69.5% | 47.7% | **1.468** | 88.2 |
| w120 · 5e-5 | 1,316 | 598 | 11.07 | +4.406 | 66.7% | 45.4% | 1.310 | 93.3 |
| w30 · 8e-6 | 1,809 | 928 | 17.19 | +3.503 | 74.1% | 51.3% | 1.591 | 108.5 |

**Under the bot's real execution, raising the floor from 8e-6 to 3e-5 at the shipped window moves
+3.758¢ / t_day 1.746 → +3.479¢ / t_day 1.468.** Both conditions get worse. This is the direct
out-of-sample refutation of the "+3.66 → +6.40¢" observation that motivated this task.

### 2.5 The DERIVED floor (4e-5), tested rather than interpolated

The 6×4 grid brackets the derived value (§1.6) at 3e-5 and 5e-5 but never evaluates it, so
`scripts/c4/derived_floor.py` runs 4e-5 itself.

**In-sample (repo, 43 days, top-of-book, $250):**

| cell | signals | fills | trades/day | ¢/share | win | **t_day** |
|---|---|---|---|---|---|---|
| w60 · **4e-5** | 2,036 | 1,000 | 23.26 | +9.641 | 73.7% | **3.370** |
| w120 · **4e-5** | 2,037 | 1,014 | 23.58 | +9.155 | 73.4% | **3.112** |

Compare to the neighbours at w=120 IS: 3e-5 → +9.526¢ / t_day 4.084, **4e-5 → +9.155¢ / 3.112**,
5e-5 → +9.628¢ / 3.162. **On the tuning tape the derived floor is the worst of the three on EV and
second-worst on t_day**, well below the shipped floor's 4.752. Nothing here would motivate adopting
it. Remember that when reading the next table.

**Out-of-sample — and this is the most interesting cell in the entire study.** I expected 4e-5 to land
between its neighbours. It does not:

| floor (w=120, fresh OOS, top-of-book) | fills | trades/day | ¢/share | win | fill rate | **t_day** |
|---|---|---|---|---|---|---|
| 8e-6 (shipped) | 854 | 15.82 | +4.339 | 72.8% | **49.9%** | 2.140 |
| 1e-5 | 844 | 15.63 | +4.229 | 72.5% | 49.6% | 2.051 |
| 2e-5 | 790 | 14.63 | +4.107 | 71.3% | 48.7% | 1.845 |
| 3e-5 (verifier) | 719 | 13.32 | +4.086 | 69.5% | 47.9% | 1.826 |
| **4e-5 (DERIVED)** | **657** | **12.17** | **+5.110** | 68.8% | **46.8%** | **2.390** |
| 5e-5 | 597 | 11.06 | +5.010 | 66.8% | 45.7% | 1.595 |

**The derived floor produces the single highest day-level t-statistic of any cell measured anywhere in
this document — 2.390 at w=120, 2.359 at w=60 — sitting in a local spike between neighbours at 1.83
and 1.60.** It is also unusually robust on the surface: leave-one-day-out never drops it below 2.0
(**54 of 54** LOO variants stay above, versus 24 of 54 for the best §3.3 cell), it survives dropping
the two bad-Chainlink days (t_day 2.09), and P(EV < 3¢) is only 0.22.

**It still fails the pre-committed rule — on the fill-rate condition in every frame, and on EV and
t_day in both post-break frames.**

| frame | days | fills | ¢/share | t_day | trades/day | **fill rate** | verdict |
|---|---|---|---|---|---|---|---|
| fresh 54d (all) | 54 | 657 | +5.11 | **2.39** ✓ | 12.17 ✓ | **46.8% ✗** | **FAIL** |
| fresh 52d (ex bad CL) | 52 | 643 | +4.84 | 2.09 ✓ | 12.37 ✓ | **47.0% ✗** | **FAIL** |
| pre-break (< 06-27) | 26 | 299 | +7.22 | 2.59 ✓ | 11.50 ✓ | **43.6% ✗** | **FAIL** |
| **post-break (≥ 06-27)** | 28 | 358 | +3.35 | **0.95 ✗** | 12.79 ✓ | 49.8% ✗ | **FAIL** |
| **July only (19d)** | 19 | 219 | **+2.70 ✗** | **0.91 ✗** | 11.53 ✓ | 50.1% ✓ | **FAIL** |

**And under the execution the bot actually performs, the spike is not there.** The 2.390 above is on
the top-of-book quotes tape (used in §2.2 for repo/fresh comparability). Re-run on the 25-level book
tape with the real ladder walk and the $250 clip:

| frame | w=120 · 4e-5 ladder | | w=60 · 4e-5 ladder | |
|---|---|---|---|---|
| | **¢/share** | **t_day** | **¢/share** | **t_day** |
| fresh 54d (all) | +4.402 | **2.007** | +4.389 | **1.935** |
| fresh 52d (ex bad CL) | +4.12 | **1.70** | +4.18 | **1.64** |
| pre-break (26d) | +6.66 | 2.35 | +6.27 | 2.06 |
| **post-break (28d)** | **+2.53** | **0.66** | **+2.77** | **0.75** |
| **July only (19d)** | **+2.16** | **0.74** | +3.03 | **0.98** |

**t_day 2.390 → 2.007 on the same days, same parameters, once the fill model is the real one.** Fill
rate 46.7%. Leave-one-day-out spans [1.757, 2.776] with only **19 of 54** variants above 2.0 (versus
54 of 54 on the top-of-book tape). Every frame FAILS.

> **The single most significant-looking number in this study was an artifact of the simplified
> execution model, and it did not survive being measured properly.** That is worth remembering the
> next time a top-of-book backtest produces an encouraging t-statistic.

Two further things kill it, and both are mechanical rather than unlucky:

1. **Fill rate 46.8% < 50%.** `docs/07` §6.5 wrote that condition for "the two-book execution
   assumption breaking down", and **raising the floor is a direct cause of it breaking down**: a higher
   σ pulls `fair` toward 0.5, so the edge at signal time is thinner, so more signals fail to re-clear
   `edge_min` against the book re-fetched 1.5 s later. Fill rate falls monotonically with the floor
   across the whole grid — 49.9% → 49.6% → 48.7% → 47.9% → **46.8%** → 45.7%. **The floor buys its
   apparent significance by discarding trades, and the rule already forbids paying that price.**

   ⚠️ **Disclosure, so this threshold is not being applied selectively: the SHIPPED configuration also
   fails it**, at **49.77%** under ladder execution (49.88% top-of-book) — by two tenths of a point.
   The 50% bar is a knife-edge for 5m at *every* setting; only the `w=60` and `w=30` cells clear it,
   and only just (50.4–51.3%). So condition (c) on its own is a weak discriminator, and I am not
   resting the verdict on it. **What is not marginal about the 4e-5 cell is the regime split:
   post-break t_day 0.95 and July-only +2.70¢ / t_day 0.91 fail conditions (a) and (b) outright.**
   The 46.8% is corroborating evidence, not the argument.

   That the shipped config sits at 49.8% is itself worth recording: `docs/07` guessed the two-book
   fill rate at "~50%" and the measurement landed on 49.8%. **The two-book execution assumption is
   marginal for 5m irrespective of sigma.**
2. **The t_day spike is entirely pre-break.** Pre-break 2.59, post-break **0.95**, July-only **0.91**
   with EV falling to +2.70¢, which also fails condition (a). The spike lives in the regime that C1
   dated as over and C3 showed does not generalise.

And the spike is not statistically real in the first place: **paired against the shipped floor on the
same 54 days, 4e-5 is worth +0.888¢/day with t = +1.06, p = 0.296** (w=60: +0.571¢, p = 0.536).
Consistent with §3.4(i), it is another draw of the same number.

> **This is the "one specific floor value" case the task asked me to watch for, and I am flagging it
> explicitly.** A grid of 24 cells plus one derived point produced exactly one configuration with a
> headline t_day near 2.4. Had I run only the derived value and reported the 54-day frame, this
> document would have said 5m passes condition (b) comfortably. It does not pass the *rule*, the
> excess significance is p = 0.30 against the shipped floor, and it evaporates post-break. **The
> honest reading is that the derived floor is the right calibration (§1.4) and the 2.39 is noise.**

### 2.6 Cells that look good only in-sample — flagged as required

| cell | IS ¢/share | IS t_day | OOS ¢/share | OOS t_day | flag |
|---|---|---|---|---|---|
| `w60_f5e-5` | **+10.106** (best IS) | 3.37 | +5.261 (best OOS) | **1.69** | **EV transfers, significance does not.** Highest EV on both tapes and among the least significant on both. It buys ¢/share by cutting trades/day 28.7→21.4 (IS) and 16.5→11.1 (OOS). |
| `w30_f5e-5` | +10.048 | 3.26 | +4.679 | **0.97** | **Worst OOS t_day of all 24.** |
| `w300_f3e-5` | +9.695 | 4.08 | +4.576 | 1.74 | Long window looks fine IS (t_day 4.08, mid-pack) but no w=300 cell reaches t_day 2.0 OOS at any floor. |
| `w120_f3e-5` | +9.526 | 4.08 | +4.086 | **1.83** | **The verifier's recommendation.** Reasonable IS, below the shipped floor OOS on both EV and t_day. |
| `w60_fnone` / `w60_f8e-6` | +9.785 / +9.719 | **4.94 / 4.89** (best IS t_day) | +4.619 / +4.629 | **2.50 / 2.47** (best grid OOS t_day) | **Does NOT look good only in-sample** — it is the best grid cell on both tapes by t_day. §3.3 is why it still is not enough. |
| `w120_f4e-5` (derived, off-grid) | +9.155 | 3.11 (**below** the shipped floor's 4.75) | +5.110 | **2.390** top-of-book → **2.007** ladder | **The inverse mirage: worse in-sample, best out-of-sample — and only on the simplified fill model.** A real improvement shows up on the tuning tape too, and survives the real execution model. This does neither. §2.5. |

No cell in the grid is a classic in-sample mirage — good IS, negative OOS. Every one of the 24 is
positive on both tapes, and the dominant failure mode is **decay**: the whole surface drops by roughly
half from IS to OOS, uniformly. The one genuine warning sign is the last row, and it points the *other*
way — the derived 4e-5 cell is mid-pack in-sample and best out-of-sample. **A parameter that helps only
on the tape you did not tune on is not a parameter that helps.**

---

## 3. Does a recalibrated floor change the DECISION?

### 3.1 The pre-committed rule, quoted verbatim

`docs/07_scale_audit.md` §6.5:

> **5m `close_snipe`:** on 20–30 days of fresh book data, with a ≤5 s book-staleness filter, at the
> shipped parameters and the recalibrated sigma floor:
> - EV/share below **+3¢**, or day-level t below 2 → **do not trade 5m at all**.
> - Realised fill rate below ~50% of signals (the two-book execution assumption breaking down).
> - Realised trades/day below ~10.

Four conditions. `scripts/c4/decision.py` evaluates all four for every cell and every frame, so
"does the floor flip a condition" is answered by counting.

### 3.2 The count

**Top-of-book, $250, 24 cells** (`data/c4/decision.csv`):

| frame | days | cells **PASSING all four** | pass EV≥3¢ | pass t_day≥2 | pass fill≥50% | pass ≥10/day |
|---|---|---|---|---|---|---|
| fresh 54d (all) | 54 | **5 / 24** | 24 | 9 | 8 | 24 |
| fresh 52d (excluding 2026-06-10/11, the two bad-Chainlink days C1 flagged at 80.5%/49.5% coverage) | 52 | **3 / 24** | 24 | 4 | 11 | 24 |
| pre-break (< 2026-06-27) | 26 | **0 / 24** | 24 | 17 | **0** | 24 |
| **post-break (≥ 2026-06-27)** | 28 | **0 / 24** | **2** | **0** | 20 | 24 |
| **July only (2026-07-08→26)** | 19 | **0 / 24** | 19 | **0** | 16 | 24 |

**Real ladder execution, $250, 8 cells** (`data/c4/decision_ladder.csv`): **2 of 8 pass on fresh 54d**
(`w60_f0`, `w60_f8e-6`), **0 of 8 on fresh 52d**, **0 of 8 post-break**, **0 of 8 July-only**.

**The derived 4e-5 (off-grid, §2.5): 0 of 5 frames pass**, at either window, on *either* execution
model. Top-of-book it clears EV and t_day on the 54-day frame but fails the fill-rate condition
(46.8%) everywhere and fails t_day post-break (0.95). **Under real ladder execution it fails t_day on
the 54-day frame too (2.007 → 1.70 excluding the bad-Chainlink days), and post-break gives +2.53¢ at
t_day 0.66.**

Every *grid* cell that passes anywhere has floor ≤ 1e-5:

| frame | passing cells |
|---|---|
| fresh 54d (top-of-book) | w30·8e-6, w60·none, w60·8e-6, w60·1e-5, w120·none |
| fresh 52d (top-of-book) | w60·none, w60·8e-6, w60·1e-5 |
| fresh 54d (ladder) | w60·floor 0, w60·8e-6 |

> **Not one cell with a floor at or above 2e-5 passes the rule in any frame, on either tape, under
> either execution model.** The recalibrated floor does not flip any condition in the direction of
> trading. It flips condition (b) in the direction of NOT trading.

### 3.3 The two cells that do pass, and why they are not enough

`w60 · floor 8e-6` under real ladder execution: **+4.071¢, t_day 2.020, 16.61 trades/day, 50.4% fill
rate.** All four conditions met. Taken at face value that is a PASS. It is not enough, for five
reasons, each measured:

1. **The margin is 1%.** t_day 2.020 against a threshold of 2.000. `w60_f0` gives 2.047.
2. **One day removes it.** Leave-one-day-out on t_day spans **[1.804, 2.704]**; the minimum is below
   2.0, so a single day is load-bearing. Only **24 of 54** leave-one-out variants stay above 2.0 —
   a coin flip. The three most load-bearing days are 2026-07-13 (+22.1¢, LOO t_day 1.804),
   **2026-06-11 (+21.2¢, LOO 1.810)** and 2026-06-01 (+20.3¢, LOO 1.817).
3. **Dropping the two known-bad data days kills it.** 2026-06-10 and 06-11 have 80.5% / 49.5%
   Chainlink coverage (C1). Excluding them: t_day **2.020 → 1.801**, and `w60_f0` **2.047 → 1.827**.
   Neither passes. Note the overlap with point 2: **2026-06-11 — a day on which we hold barely half
   the oracle prints — is the second-most load-bearing day in the entire sample.** These are days we
   already know we should not trust, and the pass depends on them.
4. **Condition (a) is a coin flip.** Day-bootstrap 95% CI on EV/share is **[+0.09, +6.01]¢**, and
   **P(EV < 3¢) = 46.2%**. Passing "+3¢" is not a finding.
5. **It is entirely a pre-break phenomenon.** Split at the 2026-06-27 liquidity break C1 dated:

   | cell | pre-break (26d) | post-break (28d) | July only (19d) |
   |---|---|---|---|
   | w60·8e-6 (ladder) | +6.248¢, t_day 2.118 | **+2.351¢, t_day 0.810** | +4.050¢, t_day 1.162 |
   | w60·floor 0 (ladder) | +6.284¢, t_day 2.118 | **+2.323¢, t_day 0.832** | +3.822¢, t_day 1.078 |
   | w120·8e-6 (SHIPPED) | +5.787¢, t_day 1.819 | **+2.250¢, t_day 0.739** | +3.438¢, t_day 1.019 |

   **Across all 24 top-of-book cells, post-break the best EV is +3.36¢ (w30·5e-5, but at t_day 0.36)
   and the best t_day is 1.22 (w60·2e-5, at +2.77¢). 22 of 24 fail even the +3¢ EV condition and
   0 of 24 reach t_day 2.0.** Post-break is the regime the bot would actually trade in.

### 3.4 Why the floor CANNOT rescue this — three measurements

This is the part I want on the record, because it explains *why* no floor value works and stops
anyone re-running the grid hoping for a different answer.

**(i) No parameter change in the grid moves EV/share detectably.** Paired day-level t-tests, fresh
OOS, 54 days, same days on both sides:

| change | mean daily ΔEV | paired t | **p** |
|---|---|---|---|
| w60: floor none → 8e-6 | −0.007¢ | −0.06 | 0.954 |
| w60: floor none → 1e-5 | −0.004¢ | −0.02 | 0.985 |
| w60: floor none → 2e-5 | +0.105¢ | +0.22 | 0.827 |
| w60: floor none → **3e-5** | −0.090¢ | −0.11 | 0.913 |
| w60: floor none → **5e-5** | −0.208¢ | −0.17 | 0.862 |
| **vol_window 120 → 60** at shipped floor (quotes) | +0.195¢ | +0.36 | 0.720 |
| **vol_window 120 → 60** at shipped floor (ladder) | +0.197¢ | +0.36 | 0.718 |

> **Every p-value is ≥ 0.72.** Not one cell in the 6×4 grid is distinguishable from any other on
> expected value. The t_day spread from 1.31 to 2.50 across cells is **not** a spread in edge — it is
> a spread in how many trades each filter leaves behind and therefore in day-level variance.
> **The `w120 → w60` change that flips the pass/fail bit is a +0.20¢ move with p = 0.72.**

(The pairing is by day, not by trade — the two cells being compared hold overlapping but different
trade sets, so each daily mean rests on a slightly different n. That is an approximation, but the
p-values sit so far from significance that no refinement of it changes the conclusion.)

That is the mechanism by which backtests lie, named precisely: a threshold at t_day = 2.0, a family of
statistically identical configurations whose t_day happens to range 1.31–2.50, and a temptation to
report the one above the line. I am not doing that. §1.5 already showed the two windows are equally
good volatility forecasters, so there is no mechanism by which w=60 could be genuinely better — it
just drops a slightly different set of trades.

**(ii) The floor removes BETTER-than-average trades, not worse ones.** Matching fills one-to-one
between the no-floor cell and each floored cell at w=60 (fresh OOS), and scoring the trades the floor
*deleted*:

| floor | kept | kept ¢/share | kept win | **DROPPED** | **dropped ¢/share** | **dropped win** |
|---|---|---|---|---|---|---|
| 8e-6 | 892 | +4.63 | 74.0% | 20 | **+4.35** | **90.0%** |
| 1e-5 | 878 | +4.60 | 73.7% | 34 | **+5.16** | **91.2%** |
| 2e-5 | 805 | +4.54 | 72.3% | 107 | **+5.24** | **89.7%** |
| **3e-5** | 722 | +4.20 | 70.5% | **190** | **+6.21** | **88.9%** |
| 5e-5 | 580 | +5.43 | 68.3% | 332 | +3.20 | 84.9% |

Same picture in-sample (repo, w=60): floor 3e-5 deletes 167 fills worth +9.05¢ at 89.2% win;
floor 8e-6 deletes 10 fills worth **+16.62¢** at 90.0% win.

**Raising the floor to the verifier's 3e-5 throws away 190 of 912 trades whose realized win rate was
88.9% and whose realized EV was +6.21¢ — better on both counts than the 722 it keeps.** A floor is
supposed to suppress false certainty in quiet markets. On 5m it suppresses the *calm, high-conviction,
high-priced* trades, which are the ones that actually won. That is the opposite of a noise filter, and
it is why every floored cell has lower t_day.

**(iii) `fair` is 15–18 points overconfident on the traded subset, at EVERY floor.** The floor was
fitted in §1.4 on *all* decision instants — but 98%+ of those are never traded (912 fills out of
59,947 instants). On the subset the bot actually fills:

| period | cell | fills | mean `fair` | actual win rate | **gap** | mean ask | ¢/share |
|---|---|---|---|---|---|---|---|
| fresh_OOS | w60 · none | 912 | 0.9226 | 0.7434 | **−0.179** | 0.6688 | +4.62 |
| fresh_OOS | w60 · 8e-6 | 892 | 0.9206 | 0.7399 | **−0.181** | 0.6658 | +4.63 |
| fresh_OOS | w60 · 3e-5 | 730 | 0.8923 | 0.7027 | **−0.190** | 0.6317 | +4.28 |
| fresh_OOS | w60 · 5e-5 | 597 | 0.8494 | 0.6717 | **−0.178** | 0.5944 | +5.26 |
| repo_IS | w60 · none | 1,246 | 0.9285 | 0.7745 | **−0.154** | 0.6451 | +9.79 |
| repo_IS | w60 · 5e-5 | 919 | 0.8714 | 0.7214 | **−0.150** | 0.5887 | +10.11 |

**The gap is invariant to the floor** — −0.179 at no floor, −0.178 at 5e-5. Raising sigma lowers
`fair`, but it lowers the trigger threshold by the same amount, so the traded population shifts and
the miscalibration follows it exactly.

The reason is visible in the reliability table (fresh OOS, w60, 8e-6):

| `fair` bucket | n | predicted | actual | mean ask | ¢/share |
|---|---|---|---|---|---|
| (0.90, 0.95] | 80 | 0.929 | **0.663** | 0.685 | **−4.13** |
| (0.95, 0.98] | 620 | 0.978 | **0.792** | 0.708 | +4.98 |

**The strategy is not profitable because `fair` is accurate. It is profitable because the ask
(0.666 mean) sits far below the realized win rate (0.740) — a 7.4¢ gross gap that survives fees.**
`fair` is a monotone trigger with a large, stable, floor-independent bias. Improving the volatility
model therefore cannot improve the edge; it can only change which trades the trigger selects. That is
the structural reason the recalibration cannot rescue 5m, and it would remain true at any floor value
anyone proposes.

### 3.5 The direct answer

> **Does a recalibrated `sigma_1s_floor` change the 5m decision? No — but not for the reason I
> expected, and the honest answer needs both halves.**
>
> The verifier's `3e-5` makes things worse on both pre-committed statistics (t_day 1.746 → 1.468 at
> the shipped window under real execution). **The empirically derived `4e-5` did something more
> awkward: on the top-of-book tape it produced the highest day-level t-statistic anywhere in this
> study (2.390).** Measured with the bot's real ladder fill model on the same 54 days it is
> **2.007**; excluding the two bad-Chainlink days **1.70**; **post-break 0.66** (+2.53¢) and
> **July-only 0.74** (+2.16¢). Fill rate 46.7%. **It fails all five frames.**
>
> **Not one cell — grid or derived, at any floor, at any window, on either execution model — passes
> all four conditions on the post-break or July-only frames.** The cells that pass on the full 54-day
> frame all sit at floors ≤ 1e-5, pass by ~1%, and fail once two known-bad data days are removed.
>
> **Does the edge appear only at one specific floor value?** The *edge* does not — it is present at
> every floor and window at roughly +4–5¢ OOS. But the *significance* very nearly does: one value out
> of seven tested produces t_day 2.39 while its immediate neighbours give 1.83 and 1.60. **I am
> flagging that as noise, not signal, and here is the evidence rather than the assertion:** paired
> against the shipped floor on the same 54 days the derived floor is worth +0.89¢/day at **p = 0.296**;
> across the whole grid no floor or window change moves EV at **p < 0.72** (§3.4(i)); the 24 grid
> cells are 88% one principal component with **1.28 effective independent tests** (§2.3); and the
> spike is confined to the pre-break regime. What varies across this parameter surface is day-level
> variance and trade count, not expected value.
>
> Reporting the 2.390 cell as a pass would have been the textbook version of the failure this task
> warned about — a threshold at 2.0, a family of statistically indistinguishable configurations, and
> one of them above the line. **C3's verdict stands: do not enable the 5m family.**

---

## 4. Capacity at 5m

`scripts/c4/capacity.py` + `cap_report.py`, on `data/fresh5m/books` (25 levels, both tokens, real
vendor update timestamps), 54 fresh days, shipped parameters, 858 filled signals. Coverage verified
at 99.77% (§0.4).

The bound is **not** the whole book. `fill_engine.walk_asks` stops at the first level violating any of:
price > best_ask + 0.03 · price outside (0.30, 0.99) · `fair − price − fee(price) ≤ edge_min` (with
`fair` frozen at signal time) · USD cap exhausted. Capacity is the notional in the levels surviving
**all** of those.

### 4.1 Fillable notional per signal — the deliverable

| measure | n | p10 | p25 | **p50** | p75 | **p90** | p95 | mean |
|---|---|---|---|---|---|---|---|---|
| top level only | 858 | $3.78 | $9.02 | **$27.33** | $92.27 | $242.87 | $438.11 | $118.46 |
| within 3¢ of best (+ price band) | 858 | $9.25 | $27.40 | **$92.10** | $249.97 | $591.63 | $932.28 | $247.99 |
| **…and still clearing `edge_min` (= true `walk_asks` capacity)** | 858 | **$8.03** | $24.85 | **$80.99** | $210.76 | **$491.72** | $758.25 | $215.70 |
| whole 25-level book (reference only) | 858 | $1,027.90 | $1,831.11 | $4,168.97 | $7,352.39 | $12,414.96 | $14,196.98 | $5,446.31 |

**The honest per-signal capacity is the third row: p10 $8.03 · p50 $80.99 · p90 $491.72.**

Split at the 2026-06-27 liquidity break — depth did **not** deteriorate:

| frame | measure | n | p10 | p50 | p90 | mean |
|---|---|---|---|---|---|---|
| pre-break (Jun 1–26) | walk_asks capacity | 366 | $7.65 | $77.86 | $412.25 | $186.50 |
| post-break (Jun 27–Jul 26) | walk_asks capacity | 492 | $8.40 | $82.54 | $552.44 | $237.42 |

Worth stating clearly: **the post-break collapse in edge (§3.3) is not a liquidity collapse.** The
book got slightly *deeper* after 06-27 while the edge halved. Whatever changed, it changed the
pricing, not the depth.

### 4.2 Correcting the docs/07 comparison against 1h

`docs/07` §3.4 says "median fillable notional per signal is **$83–200** versus **$11.40** on 1h", and
concludes 5m is "~10× more scalable". Both halves need care:

* The **$83–200** is right at the bottom end: my measured median is **$80.99** (walk bound) or
  **$92.10** (3¢ band only). The **$200** is not a median — it is close to the **mean** ($215.70),
  which is dragged up by a long right tail.
* The **$11.40** (docs/06 §4, 87 OOS 1h signals) is a **top-of-book** measurement, and M3 §6
  independently puts BTC 1h at a **$10.51** median on a one-level walk (98 signals, 81 days).
  Comparing a walked-ladder 5m number to a top-of-book 1h number overstates the ratio.

**Apples to apples:** 5m top-of-book median **$27.33** vs 1h top-of-book median **$10.51–11.40** →
**5m is ~2.4–2.6× deeper per signal, not 7–17×.** On the walked measure 5m reaches $80.99, but there
is no published 1h walked figure to compare it against. The scalability case for 5m rests mainly on
**~16 signals/day vs ~0.9**, not on per-signal depth.

### 4.3 Marginal EV by clip — the required $25 / $100 / $250 / $500 table

Same 858 fills, only the cap changes (`data/c4/table_capacity_caps.csv`, `capacity_caps.csv`):

| cap | trades/day | **¢/share** | mean clip actually filled | deployed/day | **$/day** | return on notional | t_day (¢/share) | t_day ($ P&L) | **marginal $/day per $ of cap** | worst trade | worst day |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **$25** | 15.89 | **+4.092** | $21.58 | $343 | **$22.85** | **6.66%** | 1.954 | **2.256** | — | −$26 | −$133 |
| **$100** | 15.89 | +3.866 | $64.45 | $1,024 | **$57.80** | 5.64% | 1.817 | 2.155 | **+$0.466** | −$105 | −$355 |
| **$250** | 15.89 | +3.758 | $109.94 | $1,747 | **$95.05** | 5.44% | 1.746 | 1.765 | **+$0.248** | −$262 | −$788 |
| **$500** | 15.89 | +3.712 | $146.36 | $2,325 | **$111.86** | 4.81% | 1.719 | 1.381 | **+$0.067** | −$518 | −$1,461 |
| $1,000 | 15.89 | +3.695 | $173.93 | $2,763 | $142.50 | 5.16% | 1.710 | 1.275 | +$0.061 | −$1,035 | −$2,472 |
| uncapped | 15.89 | +3.687 | $215.70 | $3,427 | $98.18 | 2.86% | 1.706 | **0.406** | **−$0.044** | **−$8,407** | **−$9,224** |

What binds, at each cap (`table_capacity_binding.csv`):

| cap | fraction where the **cap** binds | fraction where the **book** binds | median actually filled |
|---|---|---|---|
| $25 | **74.8%** | 25.2% | $25.00 |
| $100 | 43.1% | 56.9% | $80.99 |
| $250 | **21.6%** | **78.4%** | $80.99 |
| $500 | 9.7% | 90.3% | $80.99 |
| $1,000 | 3.4% | 96.6% | $80.99 |

**At $250 the book, not the config, is the binding constraint 78% of the time, and the average clip
actually filled is $110 — not $250.**

Drawdown scales faster than P&L:

| cap | 54-day total | max drawdown | DD in days of mean P&L | negative days |
|---|---|---|---|---|
| $25 | $1,234 | −$296 | 13.0 | 22/54 |
| $100 | $3,121 | −$1,002 | 17.3 | 19/54 |
| $250 | $5,133 | −$1,808 | 19.0 | 19/54 |
| $500 | $6,040 | −$2,662 | 23.8 | 20/54 |
| $1,000 | $7,695 | −$4,025 | 28.2 | 18/54 |

### 4.4 Adverse size — big fills are bad fills

From the uncapped run, grouped by realized notional:

| fills with notional > | n | win rate | total P&L | **¢/share** |
|---|---|---|---|---|
| $250 | 185 | 81.6% | +$2,485 | **+1.49** |
| $500 | 83 | 83.1% | +$1,531 | **+1.13** |
| $1,000 | 29 | 79.3% | **−$266** | **−0.82** |
| $2,000 | 12 | 75.0% | **−$3,330** | **−1.65** |

Win rate stays high while ¢/share goes negative: the large fills are large *because* the book was
deep, and deep books at the 5m close are deep because someone is willing to sell size — which is
precisely when they are right. The single worst fill is 2026-07-05, $8,123 notional at 0.500,
lost, −$8,407. This independently reproduces the adverse-size effect M4 measured on 1h and is the
reason `per_event_cap_usd` must stay finite regardless of measured depth.

### 4.5 What cap 5m could support

> **$100–250, with $250 the sensible ceiling.**
>
> * Below $100 the config is the constraint (the cap binds on 43–75% of fills) and depth is wasted.
> * At $250 the book binds 78% of the time; the average clip filled is $110; marginal return is
>   already down to **$0.25/day per $ of cap** from $0.47 in the $25→$100 step.
> * $250→$500 buys **$0.067/day per $ of cap** — a 3.7× drop — while the worst single day doubles
>   (−$788 → −$1,461) and day-level significance falls (t_day on $ P&L 1.77 → 1.38).
> * Above ~$1,000 per fill, EV/share is **negative** (§4.4). Uncapped is strictly worse than $1,000
>   on every statistic including total P&L.
>
> Note the direction of the significance gradient: **the $25 clip is the *most* statistically
> significant configuration (t_day on $ P&L = 2.256) and the uncapped one is the least (0.406).**
> Scaling the clip buys P&L at the cost of confidence, which is the opposite of what a capacity
> argument usually assumes.

**All of §4 is conditional on §3.** These are the capacity numbers *if* the edge were real. On the
fresh out-of-sample tape at the shipped parameters the edge is +3.76¢ at t_day 1.75, and post-break
it is +2.25¢ at t_day 0.74. Sizing an edge that has not cleared its own significance bar is how a
$95/day expectation becomes a $788 losing day.

---

## 5. What this run did NOT establish

1. **It did not re-test the C3 verdict** — it re-ran the same 54 fresh days through the same harness
   with different sigma parameters. The 2-day difference from C3's 56 (07-06/07 belong to the repo
   tape) explains the small gap between C3's +3.487¢/t_day 1.702 and this document's
   +3.758¢/t_day 1.746 at the shipped cell.
2. **`fair_cap` was scanned but not changed.** ~86% of 5m decisions saturate it, so it, not sigma,
   sets the model's tail probability most of the time. `faircap_scan.csv`: at the derived floor,
   log-loss improves monotonically out to 0.99 on the fresh tape (0.1409 at 0.98 → 0.1381 at 0.99)
   while on repo it is optimal at 0.98. The two tapes disagree, the effect is small, and I did not
   pursue it. **If anyone revisits 5m, `fair_cap` is a better lead than `sigma_1s_floor`.**
3. **The 4e-5 floor was not validated against 1h P&L.** §6.1 measures the Binance σ̂ distribution and
   shows a 4e-5 floor would bind on 66.5% of live 1h decisions versus 7.8% today — enough to
   establish that the change is *dangerous*, not enough to say what it would do to 1h returns. No 1h
   backtest at the new floor was run. **This document does not authorise changing the shipped value.**
4. **The regime break is dated but unexplained.** Depth did not fall (§4.1), so the post-06-27 halving
   of the edge is a pricing change. Nothing here identifies what changed.
5. **No lookahead search was re-run.** C2/C3 established that a strictly causal rebuild costs 0.00¢;
   this run enforces causality (§0.3) but does not re-audit it.
6. **The floor grid is 7 points, not a continuum.** §2.5 showed that inserting one extra point between
   3e-5 and 5e-5 produced the study's highest t_day. I did not scan the floor finely, and I am not
   going to — a finer scan would find more such spikes, which is the point of §3.4(i), not a reason to
   keep looking. Anyone tempted to run one should read §2.5 first.
7. **The `w=60` result was not chased.** `w=60` beats `w=120` on t_day at every floor on both tapes
   (§2.2), which is a more consistent pattern than the 4e-5 spike. It is still only +0.20¢ at p = 0.72
   (§3.4(i)) and §1.5 shows no forecasting basis for it, so I left it. If 5m is ever revisited with
   more data, `w=60` is the one parameter change with a *consistent* sign across both tapes.

---

## 6. Recommendations

1. **5m stays OFF.** `strategy.close_snipe.allowed_families = ["1h"]` is unchanged. The
   pre-committed rule fails on the day-level t-statistic and no sigma parameter fixes it.
   **Do not re-run this grid hoping for a different cell** — §3.4 shows all 24 are statistically
   indistinguishable in EV, so a different cell is a different draw of the same number.
2. **DO NOT change the shipped `sigma_1s_floor` yet — see §6.1. The derived 4e-5 is correct for the
   Chainlink families and would be dangerous to apply to the live 1h family, and the config knob is
   currently global.**
3. **Leave `vol_window_secs` at 120.** w=60 scores marginally better on both tapes, but §1.5 shows the
   two are indistinguishable as volatility forecasters and §3.4 shows the P&L difference is
   +0.20¢ at p = 0.72. Changing it would be tuning on noise, and its only visible effect is to move a
   t-statistic across a threshold.
4. **If 5m is ever revisited, the pre-registered next test is `fair_cap`, not sigma** (§5.2), and the
   frame must be **post-2026-06-27 data only** — the pre-break regime is now known not to generalise.
5. **If 5m ever turns on, cap at $250 and never above** (§4.5), with the M4 adverse-size guard
   active: fills above ~$1,000 notional are measurably negative-EV.
6. **Correct the `docs/07` §3.4 capacity claim.** "$83–200 vs $11.40, ~10× more scalable" compares a
   walked-ladder 5m number to a top-of-book 1h number. Apples to apples it is **$27.33 vs $10.51–11.40
   ≈ 2.4–2.6×** (§4.2). The scalability case for 5m is the trade *count*, not the depth.

### 6.1 Why the derived floor must NOT be shipped as-is — a safety finding

I intended to recommend adopting 4e-5 on calibration grounds regardless of the 5m verdict. Checking
the wiring before recommending it changed the answer, so I am recording that here.

`bot/polybot/engine.py:_snipe_inputs` feeds each family the oracle that actually resolves it:

```
1h            -> BinanceOracle.rolling_log_return_std(vol_window_secs)
5m / 15m / 4h -> ChainlinkOracle.rolling_log_return_std(vol_window_secs)
```

but `sigma_1s_floor` is a **single global value** in `config.yaml` (`strategy.close_snipe`), applied in
`strategy.evaluate_close_snipe` regardless of which oracle produced σ̂. **Everything in §1 was measured
on the Chainlink series. With 5m off, changing that global value today would change only the live,
working, profitable 1h family — on a calibration I did not perform for it.**

So I measured the Binance series too (`data/data/processed/binance/klines_1s`, 2026-07-01→26, the
tape the live 1h family reads):

| | Chainlink (fresh) | **Binance 1s klines** |
|---|---|---|
| 1s log-return std | 5.17e-05 | **4.57e-05** |
| zero-return fraction | 2.8% | **48.5%** |
| trailing-120 s σ̂ p10 | 1.22e-05 | 9.68e-06 |
| trailing-120 s σ̂ **p50** | 2.99e-05 | **3.00e-05** |
| σ̂ p90 | 7.46e-05 | 7.10e-05 |

The two series carry the **same** volatility (same asset), and — despite Binance klines being flat
half the time — their trailing σ̂ distributions are nearly identical. That is what makes the change
dangerous rather than harmless:

| floor | binds on % of **1h** decision seconds |
|---|---|
| **8e-6 (shipped)** | **7.8%** |
| 2e-5 | 28.6% |
| 3e-5 | 50.0% |
| **4e-5 (derived)** | **66.5%** |

> **Raising the global floor to 4e-5 would take the live 1h family from a floor that binds on 8% of
> decisions to one that binds on 66% — damping `fair` toward 0.5 on two thirds of all 1h ticks.**
> On the only strategy currently making money (~$11–15/day), that is a large, untested behavioural
> change justified by a measurement on a different feed.

**Therefore:**

* **Do not change `strategy.close_snipe.sigma_1s_floor` in `config.yaml` now.** Leave it at `8.0e-06`.
* **Record `4.0e-05` as the calibrated floor for Chainlink-resolved short-horizon families
  (5m/15m/4h).** It is correct, it is derived from three independent routes (§1.4), and it is dead
  config until one of those families is enabled.
* **Make the knob per-family before it is ever used** — `sigma_1s_floor` should live beside the
  oracle selection in `_snipe_inputs`, not as one global number, precisely because the two feeds are
  calibrated separately. This is a prerequisite for enabling any Chainlink family, and it should be
  filed alongside the `docs/07` §5 item 5 behavioural tests.
* **A 1h-specific recalibration is a separate, worthwhile piece of work** — §1.3's finding that the
  trailing estimator understates forward vol by 1.5–2.2× was measured on Chainlink but the mechanism
  (short window, fat tails) is not feed-specific, so 1h's floor may well also be wrong. It must be
  measured on the Binance tape against 1h outcomes before anything is changed, and this document does
  **not** authorise that change.

---

## 7. Files

| path | contents |
|---|---|
| `scripts/c4/sigma_dist.py` | §1.1–1.3 — raw returns, σ̂ distribution, forward-vol calibration, `fair` reliability |
| `scripts/c4/sigma_fit.py` | §1.4 — floor vs multiplier vs both, by Brier and log-loss; `fair_cap` scan |
| `scripts/c4/grid.py` | §2.1–2.2 — the 6×4 grid on both tapes |
| `scripts/c4/subperiods.py` | §2.3 — IS→OOS rank transfer, sub-period splits, day bootstraps |
| `scripts/c4/ladder_cells.py` | §2.4 — real 25-level execution at the cells that matter |
| `scripts/c4/derived_floor.py` | §2.5 — the derived 4e-5 floor (new in this run) |
| `scripts/c4/decision.py` | §3.2 — all four pre-committed conditions × every cell × every frame |
| `scripts/c4/capacity.py`, `cap_report.py` | §4 — depth profile, cap sweep, adverse size |
| `data/c4/sigma_raw_returns.csv` | §1.1 |
| `data/c4/sigma_hat_dist.csv` | §1.2 |
| `data/c4/sigma_calibration.csv` | §1.3 |
| `data/c4/sigma_fit.csv`, `faircap_scan.csv`, `fair_calibration.csv` | §1.4, §3.4(ii), §5.2 |
| `data/c4/grid.csv` | §2.1–2.2 |
| `data/c4/subperiods.csv` | §2.3, §2.6 |
| `data/c4/ladder_cells.csv` | §2.4 |
| `data/c4/derived_floor.csv`, `derived_floor_ladder.csv`, `derived_*_f4e-05.parquet` | §2.5 — the derived 4e-5 floor |
| `data/c4/decision.csv`, `decision_ladder.csv` | §3.2 |
| `data/c4/table_capacity_depth.csv`, `_depth_split.csv`, `_binding.csv`, `_caps.csv` | §4.1–4.3 |
| `data/c4/capacity_profile.parquet`, `capacity_caps.csv` | §4 |
| `data/c4/trades_{repo_IS,fresh_OOS}_w{30,60,120,300}_f*.parquet` | 48 per-trade tapes, the grid |
| `data/c4/ladder_*.parquet`, `cap_trades_*.parquet`, `derived_*.parquet` | per-trade tapes, real execution |
| `data/c4/decisions_{repo_IS,fresh_OOS}.parquet` | 108,737 decision instants with σ̂ at all four windows |
