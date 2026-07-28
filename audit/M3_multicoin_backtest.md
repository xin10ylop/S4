# M3 — close_snipe backtested on every non-BTC hourly family

**Date:** 2026-07-27 (UTC) · **Independently re-verified 2026-07-28** (§1.4 — one published table
corrected, §4.3; no headline number changed) · **Scope:** run the shipped `close_snipe` strategy, at the shipped
parameters, on all seven Polymarket hourly Up/Down families, and decide whether the bot goes from
~0.9 trades/day to ~5-6/day.
**Sample:** 2026-05-08 → 2026-07-27 UTC, **81 days × 7 coins = 13,405 hourly closes** (1,915 per
coin), each priced off the Binance feed it actually resolves on.
This run **extended the multi-coin tape from 45 to 80 days** of quotes and 35 to 80 days of
underlying (`data/multicoin/`), which is what makes any of the numbers below testable.

---

## 0. Bottom line

**Frequency multiplies 2.6x. Edge does not replicate. Add ETHEREUM only; leave SOL, XRP, DOGE and
BNB off.**

| portfolio | fills | trades/day | ¢/share | win | t_day | $/day @$250 **FULL** | $/day **TRAIN** | $/day **TEST (untouched)** | $/day FULL, −best 3 days |
|---|---|---|---|---|---|---|---|---|---|
| **BTC only** (today's bot) | 66 | 0.81 | **+13.32** | 87.9% | **3.30** | **+6.30** | +7.25 | **+4.84** | +2.56 |
| **BTC + ETH** | 84 | 1.04 | +12.59 | 88.1% | **3.25** | **+8.29** | +9.74 | **+6.07** | **+3.82** |
| BTC + ETH + HYPE † | 97 | 1.20 | +14.3 | 89.7% | 4.41 | +10.25 | +12.01 | +7.56 | +5.30 |
| BTC+ETH+SOL+XRP | 120 | 1.48 | +6.7 | 80.0% | 1.87 | +7.41 | +7.63 | +7.06 | +3.02 |
| **ALL 7** | 173 | **2.14** | +6.33 | 80.3% | 1.34 | +8.49 | +12.50 | **+2.36** | +3.61 |
| **non-BTC only** | 107 | 1.32 | **+2.01** | 75.7% | **0.45** | **+2.20** | +5.25 | **−2.48** | **−0.60** |

† HYPE has **no live price feed reachable** (M1 §6) — it cannot be traded today at any size.

**The non-BTC pool measures +2.01 ¢/share on 107 fills over 81 days, with a day-block bootstrap 95%
CI of [−6.5 ¢, +9.7 ¢] and P(EV ≤ 0) = 0.30.** It is not distinguishable from zero. And it fails
every robustness cut this project requires:

* **untouched TEST half: −$2.48/day** (TRAIN +$5.25/day) — the whole apparent gain is in-sample;
* **remove the best 3 days: −$0.60/day**;
* **add 300 ms of underlying feed latency: −2.0 ¢/share** — the pooled non-BTC "edge" is smaller
  than one REST round trip (§10);
* six of the seven coins reject the model that generates it at p < 0.01 (§4.1).

Five findings, ordered by how much they change the plan:

1. **Only ETHEREUM survives**, and it is worth **+$1.99/day** (+32% on the BTC bot) at 0.22
   trades/day — not the 5-6x frequency this run was chasing. n = 18 fills, +9.94 ¢/share,
   P(EV ≤ 0) = 0.080; positive in TRAIN (+4.9¢, 12 fills) and TEST (+20.0¢, 6 fills). It is
   suggestive, not proven. §2, §9.
2. **`close_snipe` is not "the book has not caught up". It is a few points of edge over the
   market-implied price, and it earns them only in a MIDDLE band of the model's own confidence.**
   Pooled over all coins, win rate by `|z|` runs **51.6% → 86.3% → 95.1% → 71.4% → 66.7%** across
   `<1 / 1–2 / 2–5 / 5–10 / >10` — an inverted U, losing at *both* ends for two mechanically
   different reasons (§4.4 coarse-tick zero-move at the bottom, adverse selection against a pinned
   `fair` at the top). |z| ∈ (1,5] wins **91.1% on 112 fills (+11.7 ¢/share)** against |z| > 5 at
   **70.0% on 30 fills (−5.4 ¢/share)**, Fisher exact p = 0.0055 — but **that split was chosen after
   seeing the table, and the neighbouring `|z| ≤ 2` vs `> 5` split gives p = 0.81**, so read it as a
   lead, not a result. For BTC alone: |z| ≤ 5 → 96.2% / +20.4¢ on 52 fills; |z| > 5 → **57.1% /
   −12.9¢** on 14. **This is a property of the shipped BTC strategy, not of the new coins**, and it
   is the most actionable lead here — §7.1 shows it does not yet walk forward. §4.3.
3. **The M1 competition/staleness hypothesis is REJECTED.** Cross-coin, EV/share vs median in-band
   depth r = +0.15, vs log volume r = −0.16, vs median quote age r = −0.10, vs p90 quote age
   r = +0.24 — every r² ≤ 0.06 on n = 7. Within the 173 fills,
   `corr(log1p(book_age), pnl/share) = −0.086` and EV is flat-to-falling in book age.
   **Staler books do not pay more.** §5.
4. **BNB is the cautionary tale.** On TRAIN it was the best non-BTC coin (+17.2 ¢/share, 17 fills,
   t_day 1.58) and would have been the obvious first coin to enable. On the untouched TEST half it
   is **−29.6 ¢/share, −$6.14/day on 12 fills**. Its late sign-flip rate is **2.19% — 2.1x every
   other coin**, confirming M1 §3.5, and it is still the largest non-BTC signal generator. §9.
5. **A data-representation artifact carried 39% of the total P&L and had to be fixed mid-run.**
   The quote tape stores prices as float32, so a quote of exactly 0.30 reads back as
   0.30000001192092896 and slips past the bot's *strict* `price_min < ask` guard. Three of 176 fills
   were admitted by that artifact alone, worth **−$194.7**, including the single worst trade in the
   whole sample (SOL, `fair` = 0.500 exactly, 629 shares at 0.30, **−$198**). The live bot parses
   JSON decimals into float64 where 0.3 is 0.3 and the guard rejects it, so those fills would never
   have happened. **Every number in this document is post-fix.** §1.3c.

**Recommendation: add `ethereum` only, behind a coin allowlist, at a $25 clip, and treat the first
40 live fills as the real test. The frequency problem is NOT solved by this expansion.** §12.

---

## 1. What was run, and how it was verified

### 1.1 The harness is the validated one; only the data layer is new

`scripts/multicoin/replay_hourly.py` **imports** the bot primitives from
`scripts/fresh5m/replay.py` rather than re-deriving them:

| imported | source of truth |
|---|---|
| `evaluate_close_snipe_port` | `bot/polybot/strategy.py:evaluate_close_snipe` |
| `snipe_tau_bounds_port` | `strategy.snipe_tau_bounds` |
| `walk_asks_port` | `bot/polybot/fill_engine.py:walk_asks` |
| `fee_per_share`, `normal_cdf` | `fill_engine.fee_per_share`, `oracle.normal_cdf` |
| `BookTape`, `_t` (day-clustered t) | `replay.py` |

so `scripts/fresh5m/proptest.py` — 0 mismatches against the live bot on 80,000 randomised inputs,
audit/C2_harness.md §1.1 — covers this run unchanged. The window gate (tick from the early end of
the τ band toward the close, **one entry per window, first qualifying side**) is the same
`engine._maybe_snipe` mirror.

Parameters are `bot/config.yaml` verbatim: `edge_min` 0.03, `price_min` 0.30, `price_max` 0.99,
`vol_window_secs` 120, `sigma_1s_floor` 8e-6, `fair_cap` 0.98, `snipe_last_secs` 5.0,
`snipe_min_tau_secs` 2.5, `snipe_fill_margin_secs` 0.5, `latency_ms` 1500,
`max_walk_above_best` 0.03, `per_event_cap_usd` 250, `fee_rate` 0.07. The derived decision grid is
**τ ∈ {5, 4, 3} s**. Nothing was tuned for any headline number.

### 1.2 What is new, and why each choice is the honest one

* **Underlying = Binance 1s klines on that coin's own resolution symbol** (M1 §2: BTCUSDT, ETHUSDT,
  SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT spot; **HYPEUSDT USD-M futures**). No Chainlink anywhere —
  the hourly family does not settle on it.
* **Anchors from traded prints only.** `S_open` = the first `volume > 0` bar in `[open_s, close_s)`.
  M1 §2.3 showed the 1s archive back-fills silent seconds at `volume = 0`, which flips the settle on
  near-ties and hurts the thin coins most.
* **Causal `S_t`.** A 1s bar covering second `s` is only complete at `s+1`, so the newest price the
  bot could hold at decision instant `t` is the close of the newest `volume > 0` bar whose **bar end
  ≤ t**. `tau` runs from that print's observation instant to the close, so sub-second staleness is
  charged as risk rather than ignored — the same convention `replay.py` applies to Chainlink's
  publication lag. It costs 7 signals and 0.3 ¢/share (§10).
* **σ the way the live bot computes it.** `BinanceOracle.rolling_log_return_std` runs on a ~1 Hz REST
  poll of the last trade price, so silent seconds contribute a **zero** return. The offline analogue
  is a forward-filled 1-second grid, rolling std ddof=1 over 120 samples, min_periods 30 — which is
  exactly the "quiet polling underestimates vol" effect `sigma_1s_floor` exists to catch.
* **Two-book fill.** Signal at `t` against the book as of `t`; fill against the book **re-fetched at
  t + 1500 ms**. 19-62% of signals die there per coin (§3). Never the signal book.
* **Book staleness** from the venue's own last-update stamp (`timestamp_us`; M1 §3.2 verified it
  never moves while the book hash is unchanged). Applied PRE-decision: a stale book is handed to the
  evaluator as `None`, exactly as the live bot sees a book it could not fetch.
* **Day-clustered t** is the headline statistic; per-trade t is shown beside it and runs 1.2-2x hot.

### 1.3 Verification — four checks

**(a) Settle reconciliation: 100.000% on 13,405 hours.** Up/Down was re-derived from each coin's own
Binance candle (`close >= open ⇒ Up`, anchors from traded prints) and compared to Telonex's
`result_id` over the exact replay window:

| coin | symbol | closes | exact ties | settle agreement |
|---|---|---|---|---|
| bitcoin | BTCUSDT | 1,915 | 1 | **100.000%** |
| ethereum | ETHUSDT | 1,915 | 2 | **100.000%** |
| solana | SOLUSDT | 1,915 | 22 | **100.000%** |
| xrp | XRPUSDT | 1,915 | 18 | **100.000%** |
| dogecoin | DOGEUSDT | 1,915 | 15 | **100.000%** |
| bnb | BNBUSDT | 1,915 | 7 | **100.000%** |
| hype | HYPEUSDT (futures) | 1,915 | 1 | **100.000%** |

Zero disagreements. Every coin is wired to the feed it resolves on — the error class that cost this
project 3-for-3 losing fills is excluded by measurement, not assumption. Token orientation was
separately confirmed: the Up-token ask at τ=5 s averages 0.88-0.97 when Up won and 0.09-0.23 when
Down won, `corr(ask_up, up_won)` = 0.83-0.93 (0.57 on HYPE, whose book is far wider).

**(b) A second, independently-written replay agrees on every field.**
`scripts/multicoin/indep_hourly.py` re-implements the whole data layer from raw parquet in a
different style (its own anchor search, σ grid, book lookup, fee/fair arithmetic), importing nothing
from the harness. On **7 coins × 20 days (2026-07-01 … 07-20), 74 signals**:

```
bitcoin  harness=25 indep=25 both=25 left_only=0 right_only=0 | tau=0 side=0 outcome=0 fair=0
         book_age_sig=0 book_age_fil=0 avg_price=0 shares=0 pnl_per_share=0 won=0
ethereum 9/9 · solana 8/8 · xrp 9/9 · dogecoin 4/4 · bnb 17/17 · hype 2/2   — all 0
TOTAL DISCREPANCIES: 0
```

**(c) Two real bugs were found by (b) and fixed. Both were silent and both moved money.**

1. **Non-deterministic book selection.** **19.2% of quote rows share `timestamp_us` with another row
   on the same book.** Under an unstable sort, "the last update at or before t" is ambiguous, and
   the two implementations picked different books on ~5% of fills — up to 6¢ apart on the fill
   price. Both now sort by `(timestamp_us, local_timestamp_us)` with a stable mergesort.
2. **float32 price precision.** The tape stores prices as float32, so `0.30` returns as
   `0.30000001192092896` in float64 and passes the bot's **strict** `price_min < ask` guard. The
   live bot parses JSON decimals into float64, where `0.3 < 0.3` is False. Three of 176 fills
   existed only because of that 1.2e-8 gap; they carried **−$194.7 of P&L (39% of the sample
   total)**, and the largest was SOL 2026-06-14: `z = 0.000` exactly, `fair = 0.500`, 629 shares at
   $0.30 = $188.8 deployed, **−$198.08 — the worst single trade in 81 days × 7 coins.** Prices are
   now snapped to the venue's decimal tick (`np.round(..., 4)`) before any threshold comparison.
   **Any multi-coin number produced without both fixes is not reproducible.**

**(d) The harness reproduces the known BTC baseline on an independent tape.**

| | trades/day | ¢/share | win |
|---|---|---|---|
| BTC backtest (prior work) | ~0.9 | +9.8 | — |
| BTC live paper, 10 days, 8 trades | ~0.8 | +21.5 | 87.5% |
| **this harness, BTC, 81 days, 66 fills** | **0.81** | **+13.3** | **87.9%** |

Different vendor tape, different period; the win rate lands within 0.4 points of live and the trade
rate within 0.1/day.

### 1.4 Independent re-verification pass, 2026-07-28

A separate pass re-ran the harness end to end and recomputed the load-bearing statistics from the
trade tape with independently written code (different bootstrap seed, different binning, no reuse of
`m3_report.py`). What was checked and what it found:

| check | method | result |
|---|---|---|
| **End-to-end reproducibility** | `replay_hourly.py` re-run from scratch on all 7 coins (3m34s), output compared field-by-field against the stored `trades_shipped.parquet` | **310 signals, 28 fields, 0 discrepancies** (rtol 1e-12) |
| **Headline per-coin table (§2)** | ¢/share, win, trades/day, per-trade t, day-clustered t, $/day recomputed from the tape | **all 7 coins match to 4 s.f.** |
| **Day-clustered 95% CIs (§2)** | mean of daily means ± 1.96·SE, recomputed | **all 7 match exactly** ([+5.2,+20.6] BTC, [−0.1,+26.4] ETH, …) |
| **Bootstrap P(EV≤0) (§2)** | fresh 20,000-draw day-block bootstrap, independent seed | BTC 0.001, ETH 0.081, SOL 0.853, XRP 0.716, DOGE 0.420, BNB 0.566, HYPE 0.000, **non-BTC 0.309** — all within Monte-Carlo noise of the published 0.000/0.080/0.837/0.724/0.430/0.573/0.000/0.300 |
| **Settle reconciliation (§1.3a)** | Up/Down re-derived from Binance candles (traded-print anchors) for bitcoin, bnb, ethereum | **1,915/1,915 each — 100.000%**, reproduced |
| **Late sign-flip rates (§8.2b)** | recomputed over all 1,915 closes/coin | BTC **1.044%**, ETH **0.836%**, **BNB 2.193%** — exact match; BNB's 2.1x flip rate stands |
| **Zero-move signals (§4.4)** | count of signals with `z` exactly 0 | SOL **10 of 42**, XRP **7 of 39**, DOGE **1 of 19** — exact match |
| **Capacity percentiles (§6)** | p10/p50/p90 of fill-book notional recomputed | exact match (HYPE p10 $3.06 vs published $3.07, rounding) |
| **Stress grid (§10)** | cross-checked against `t11_stress.csv` | exact match on every cell quoted |
| **Win-vs-ask table (§4.2)** | recomputed with one-sided binomial vs mean fill ask | **all 9 rows match** (BTC +14.4 pt p=0.0037, non-BTC +3.2 pt p=0.268) |
| **§4.3 \|z\| bucket table** | recomputed | ⚠️ **one row wrong — corrected in place; see §4.3** |
| **§8.2c concurrency** | recounted from `t7c_concurrency.csv` and from the tape | ⚠️ **published 164/11/(7,1,3) is really 162/10/(7,0,3) — corrected in place** |
| **§5 book-age table** | recomputed three ways | correct; the age convention was undocumented and is now stated in §5 |

**Two defects were found. Both are reporting-script/transcription errors, neither is a harness or
data bug, and neither changes a headline number or the recommendation:**

1. **§4.3 `<1` row** — `pd.cut(..., right=True)` on bins starting at 0 dropped the 8 fills with
   `|z|` exactly zero. The corrected row is *worse* than the published one (51.6% win / −1.6¢
   against 65.2% / +7.6¢), so the correction moves in the conservative direction and sharpens the
   section's conclusion from "inverts at the top" to "loses at **both** ends". `m3_report.py` is
   patched with an assertion that the partition is total.
2. **§8.2c concurrency counts** — 162 closes / 10 concurrent, not 164 / 11, and there was **no
   all-lose concurrent hour** (the worst lost $0.43). This makes the observed joint tail *smaller*
   than published; the sizing advice in §8.2b rests on the flip-rate analysis, not on this count,
   and is unaffected.

While correcting §4.3 the verification pass also found that its **p = 0.0055 is split-dependent** —
the neighbouring `|z| ≤ 2` vs `> 5` split gives **p = 0.81**. That is now stated wherever the
p-value appears (§0 finding 2, §4.3, §13 item 4). It does not change any recommendation, because
§7.1 had already shown the |z| band fails walk-forward and the document already declined to ship it.

**Everything else in this document reproduced.** In particular the two claims the decision rests on —
non-BTC pooled +2.01 ¢/share with P(EV≤0) = 0.30, and the TRAIN→TEST collapse of BNB — are confirmed
by independent recomputation.

---

## 2. Per-coin headline — shipped parameters, full 81-day sample

`book_moved` = signal fired but the re-fetched fill book no longer had the edge. `empty` = the fill
book had no ask at all. CI is the day-clustered 95% interval on ¢/share; `P(EV≤0)` is a 4,000-draw
day-block bootstrap.

| coin | closes | signals | fills | book_moved | empty | **¢/share** | 95% CI | **P(EV≤0)** | win | trades/day | t_trade | **t_day** | **$/day @$250** | $/day @$25 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **bitcoin** | 1,915 | 103 | **66** | 32 | 5 | **+13.32** | [+5.2, +20.6] | **0.000** | 87.88% | 0.815 | 3.80 | **3.30** | **+6.30** | +2.10 |
| **ethereum** | 1,915 | 39 | 18 | 17 | 4 | **+9.94** | [−0.1, +26.4] | **0.080** | 88.89% | 0.222 | 1.35 | **1.94** | **+1.99** | +0.73 |
| solana | 1,915 | 42 | 21 | 19 | 2 | **−7.70** | [−27.0, +9.8] | 0.837 | 57.14% | 0.259 | −0.85 | −0.92 | −1.15 | −1.02 |
| xrp | 1,915 | 39 | 15 | 22 | 2 | **−5.93** | [−21.4, +14.5] | 0.724 | 66.67% | 0.185 | −0.67 | −0.38 | +0.27 | −0.09 |
| dogecoin | 1,915 | 19 | 11 | 7 | 1 | **+1.48** | [−32.8, +30.0] | 0.430 | 81.82% | 0.136 | 0.11 | −0.09 | +0.93 | +0.46 |
| bnb | 1,915 | 52 | 29 | 22 | 1 | **−2.16** | [−21.4, +14.6] | 0.573 | 72.41% | 0.358 | −0.25 | −0.37 | −1.81 | −0.48 |
| hype † | 1,915 | 16 | 13 | 1 | 2 | **+25.64** | [+15.3, +35.9] | 0.000 | 100.0% | 0.161 | 4.88 | 4.88 | +1.96 | +0.97 |
| **POOLED non-BTC** | 11,490 | 207 | **107** | 88 | 12 | **+2.01** | boot [−6.5, +9.7] | **0.300** | 75.70% | **1.321** | 0.54 | **0.45** | **+2.20** | — |

† **HYPE is not tradeable today.** M1 §6 established that `fapi.binance.com` is HTTP-451 from this
environment and `data-api.binance.vision` has no futures path, so the live bot has **no reachable
price feed for it**; the backtest used the bulk futures archive. Its 13 fills are also n < 20 and
**9 of them filled against books older than 5 s**. Treat +25.6¢ as a hypothesis, not a result.

**Concentration check** (full sample, $250 clip):

| | $/day | −best day | −best 3 days | ¢/share −best 3 days |
|---|---|---|---|---|
| BTC | +6.30 | +3.93 | +2.56 | +11.0 |
| BTC + ETH | +8.29 | +5.17 | **+3.82** | +10.4 |
| ALL 7 | +8.49 | +5.51 | +3.61 | +5.4 |
| **non-BTC** | +2.20 | +1.11 | **−0.60** | −0.4 |

The non-BTC pool's entire measured P&L is three days. BTC loses 59% to three days and BTC+ETH 54% —
neither is comfortable, but only the non-BTC pool crosses into negative.

---

## 3. Frequency — the funnel, and what it actually delivers

| coin | closes | signals | signal rate | book_moved | empty | fills | **fill conversion** | signals/day | **fills/day** |
|---|---|---|---|---|---|---|---|---|---|
| bitcoin | 1,915 | 103 | 5.38% | 32 | 5 | 66 | **64.1%** | 1.272 | **0.815** |
| ethereum | 1,915 | 39 | 2.04% | 17 | 4 | 18 | 46.2% | 0.481 | 0.222 |
| solana | 1,915 | 42 | 2.19% | 19 | 2 | 21 | 50.0% | 0.519 | 0.259 |
| xrp | 1,915 | 39 | 2.04% | 22 | 2 | 15 | **38.5%** | 0.481 | 0.185 |
| dogecoin | 1,915 | 19 | 0.99% | 7 | 1 | 11 | 57.9% | 0.235 | 0.136 |
| bnb | 1,915 | 52 | 2.72% | 22 | 1 | 29 | 55.8% | 0.642 | 0.358 |
| hype | 1,915 | 16 | 0.84% | 1 | 2 | 13 | 81.3% | 0.198 | 0.161 |
| **TOTAL** | 13,405 | **310** | 2.31% | 120 | 17 | **173** | 55.8% | **3.83** | **2.14** |

* **The two-book fill is the single largest filter and it is worse on the thin coins.** BTC converts
  64.1% of signals into fills; XRP 38.5%, ETH 46.2%. This is the same mechanism that kills 34% of
  5m signals; on the thin hourly coins it kills up to 62%.
* **M1's frequency screen was good on signals and 1.8x optimistic on trades.** M1 §3.4 projected
  4.40 indicative signals/day; measured properly (causal `S_t`, real σ, the τ ∈ {5,4,3} grid, one
  entry per window) it is **3.83 signals/day** — within 13%. But signals are not trades: after the
  two-book fill it is **2.14 fills/day**. M1 stated explicitly that its screen was a frequency upper
  bound with no fill simulation and no PnL claim; that reading was correct, and the gap is the fill
  stage.

**So: 0.81 → 2.14 trades/day, a 2.6x increase, not the 5-6x hoped for. And on the untouched TEST
half it does not pay.**

---

## 4. Why it does not replicate — the mechanism

### 4.1 Every coin's realised win rate falls far below its own direction accuracy

`close_snipe`'s ceiling is "at τ = 5 s, does `sign(S_t − S_open)` already equal the settle?" That is
measured on **all 1,915 closes per coin**, so it has real power. The realised win rate is measured
on the fills. The gap is adverse selection.

| coin | direction accuracy @τ=5s (n=1,915) | realised win on fills | n fills | **shortfall** | one-sided binomial p |
|---|---|---|---|---|---|
| bitcoin | 98.96% | 87.88% | 66 | **−11.1 pt** | **4.7e-07** |
| ethereum | 99.16% | 88.89% | 18 | −10.3 pt | 0.0098 |
| solana | 99.01% | 57.14% | 21 | **−41.9 pt** | 2.5e-13 |
| xrp | 99.06% | 66.67% | 15 | −32.4 pt | 2.0e-07 |
| dogecoin | 99.16% | 81.82% | 11 | −17.4 pt | 0.0037 |
| bnb | **97.81%** | 72.41% | 29 | −25.4 pt | 1.5e-07 |
| hype | 99.22% | 100.0% | 13 | +0.8 pt | 1.00 |

**Six of seven coins reject the "the book just hasn't caught up" model at p < 0.01.** The closes
where a mispriced ask is still standing 5 s before the close are systematically the closes where the
model is wrong.

### 4.2 The right benchmark is the ask, not the model

Since `EV = win − ask − fee`, the honest question is *does the strategy beat the market-implied
probability it pays?*

| coin | n | realised win | mean fill ask | **edge over the market (pt)** | P(win > ask) | ¢/share |
|---|---|---|---|---|---|---|
| bitcoin | 66 | 0.8788 | 0.7344 | **+14.4** | **0.0037** | +13.3 |
| hype | 13 | 1.0000 | 0.7323 | +26.8 | 0.017 | +25.6 |
| ethereum | 18 | 0.8889 | 0.7783 | **+11.1** | 0.204 | +9.9 |
| dogecoin | 11 | 0.8182 | 0.7927 | +2.6 | 0.593 | +1.5 |
| bnb | 29 | 0.7241 | 0.7341 | −1.0 | 0.641 | −2.2 |
| xrp | 15 | 0.6667 | 0.7147 | −4.8 | 0.764 | −5.9 |
| solana | 21 | 0.5714 | 0.6348 | −6.3 | 0.798 | −7.7 |
| **ALL 7** | 173 | 0.8035 | 0.7287 | **+7.5** | **0.0146** | +6.3 |
| **non-BTC** | 107 | 0.7570 | 0.7251 | **+3.2** | **0.268** | +2.0 |

**The real edge is a few points over the market price, not the ~25 points the model's `fair` claims.**
BTC clears it; ETH and HYPE point the right way without significance; nothing else does, and the
non-BTC pool does not.

### 4.3 The confidence profile: an inverted U — the model loses at both ends of its own `|z|`

Pooled over all seven coins, by the model's own `|z|` at entry:

| \|z\| | fills | win rate | mean ask | ¢/share |
|---|---|---|---|---|
| **< 1** | **31** | **51.6%** | 0.515 | **−1.6** |
| 1 – 2 | 51 | 86.3% | 0.766 | +8.5 |
| **2 – 5** | **61** | **95.1%** | 0.798 | **+14.3** |
| **5 – 10** | 21 | **71.4%** | 0.754 | **−5.0** |
| **> 10** | 9 | **66.7%** | 0.720 | **−6.3** |
| **all** | **173** | 80.3% | 0.729 | +6.3 |

> **Correction (2026-07-28 verification pass).** The `<1` row was previously published as
> *23 fills / 65.2% / +7.6¢*. That was a binning defect in the reporting script, not in the harness:
> `pd.cut(..., right=True)` on left-closed bins starting at 0 silently drops rows with `|z|` **exactly
> zero**, and there are **8 such fills** — the zero-move closes of §4.4 (6 SOL, 2 XRP), which won
> **1 of 8** at **−27.9 ¢/share**. Dropping them removed the worst fills from the worst bucket. The
> corrected row is above; rows summing to 173 rather than 165 is the check that now passes.
> **No headline number changes** — every aggregate below excludes the `<1` bucket by construction —
> and the correction *strengthens* the section's conclusion: the model loses money at both ends of
> its own confidence scale, not only at the top.

**With the `<1` row corrected the shape is an inverted U, not a monotone inversion: the strategy
loses at BOTH ends of its own confidence scale and makes all of its money in a middle band.**

* **Low end** (`|z| < 1`, 31 fills, 51.6%, −1.6¢) — the coarse-tick / zero-move failure of §4.4:
  `z ≈ 0` ⇒ `fair ≈ 0.500` on both sides, and `edge_min` fires against any ask ≤ 0.455 on zero
  information. It is a coin flip bought at a discount that isn't one.
* **High end** (`|z| > 5`, 30 fills, 70.0%, −5.4¢) — `fair` is pinned at the 0.98 clip and the bot
  lifts *any* ask below ~0.94. The only reason such an ask is still standing is that somebody
  watching more than a 1-second last-trade print disagrees, and they are right more often than the
  model.

**|z| ∈ (1,5]: 112 fills, 91.1% win, +11.7 ¢/share versus |z| > 5: 30 fills, 70.0% win,
−5.4 ¢/share — Fisher exact p = 0.0055** (2×2 = [[102,10],[21,9]]). For BTC alone: |z| ≤ 5 → 52
fills, 96.2%, **+20.4¢**; |z| > 5 → 14 fills, 57.1%, **−12.9¢**.

> ⚠️ **That p-value is a post-hoc split and must be read as such.** The (1,5] boundary was chosen
> after seeing the bucket table. The neighbouring split `|z| ≤ 2` vs `|z| > 5` — equally defensible
> a priori — gives **p = 0.81**, because it pools the bad low end with the good middle. With five
> buckets there are many admissible cut points and no multiplicity correction has been applied, so
> **treat p = 0.0055 as "worth investigating", not as an established effect.** The corroboration that
> does not depend on a chosen cut point is the monotone win-rate profile itself
> (51.6 → 86.3 → 95.1 → 71.4 → 66.7%) and the fact that the two ends have *independent, mechanically
> different* explanations. §7.1 then shows a |z| band does not walk forward.

**This is a property of the shipped BTC strategy, not of the new coins,** and it belongs in the BTC
workstream. §7.1 shows it is not yet a shippable rule.

### 4.4 The coarse-tick failure mode — why SOL and XRP specifically lose

`fair = Φ(ln(S_t/S_open) / (σ√τ))`. When the underlying has not moved *at all* since the hour open,
`z = 0`, `fair = 0.500` on both sides, and `edge_min = 0.03` fires against **any ask ≤ 0.455** — on
zero information.

| coin | underlying tick | tick in bp | closes within 1 tick of open @τ=5s | **signals from an exactly-zero move** |
|---|---|---|---|---|
| bitcoin | $0.01 | 0.0016 | 0.00% | **0 of 103** |
| ethereum | $0.01 | 0.055 | 0.10% | 0 of 39 |
| bnb | $0.01 | 0.170 | 0.10% | 0 of 52 |
| hype | $0.001 | 0.160 | 0.21% | 0 of 16 |
| xrp | $0.0001 | 0.875 | 0.99% | **7 of 39 (18%)** |
| **solana** | $0.01 | **1.298** | 1.25% | **10 of 42 (24%)** |
| dogecoin | $0.00001 | 1.130 | 1.25% | 1 of 19 |

Zero-move closes are 1.25% of SOL's closes but **24% of SOL's signals** — a 19x over-representation,
because that is exactly when the market offers something the model thinks is cheap. The consequence:

| coin | \|z\| < 2 fills | win | ¢/share | \|z\| ≥ 2 fills | win | ¢/share |
|---|---|---|---|---|---|---|
| solana | 18 | **50.0%** | **−10.8** | 3 | 100% | +10.6 |
| xrp | 10 | **60.0%** | **−6.0** | 5 | 80% | −5.8 |
| bnb | 10 | 50.0% | −14.8 | 19 | 84.2% | +4.5 |
| bitcoin | 28 | 96.4% | +24.5 | 38 | 81.6% | +5.1 |

**On SOL and XRP the strategy is mostly buying coin flips at a discount that isn't one.** That is a
structural defect of a Gaussian fair value on a coarse-tick underlying, not a tuning question — and
the worst single trade in the sample (§1.3c) was exactly this case.

---

## 5. The competition hypothesis — TESTED AND REJECTED

M1 §3.2-3.3 established that thinner coins carry staler quotes and hypothesised that this is a
durable structural reason to trade them. **It is not borne out.**

**Cross-coin regression of ¢/share on liquidity (n = 7 coins):**

| x | slope | Pearson r | r² | t | fill-weighted slope |
|---|---|---|---|---|---|
| log10(median market volume) | −0.0219 | −0.164 | 0.027 | −0.37 | +0.030 |
| median in-band ask notional | +0.0040 | +0.150 | 0.022 | +0.34 | +0.008 |
| median quote age | −0.0191 | −0.101 | 0.010 | −0.23 | −0.057 |
| p90 quote age | +0.0019 | +0.243 | 0.059 | +0.56 | −0.001 |
| fraction of quotes older than 5 s | −0.0688 | −0.043 | 0.002 | −0.10 | −0.512 |

Every relationship is a coin toss and the signs flip under fill weighting. **n = 7 has no power** —
so the *within*-sample test matters more:

**Within-sample (173 fills, all coins pooled), EV by book age.** *Age here is
`max(signal-book age, fill-book age)`* — the worse of the two books the trade touched, which is the
conservative reading of "how stale was the quote I traded against"
(`m3_report.py:255`). Bucketing on the fill book alone gives 129/24/8/9/3 and
`r = −0.090`; on the signal book alone 109/26/17/18/3 and `r = −0.071`. **All three give the same
answer**, which is the point.

| book age | fills | win | ¢/share | mean ask |
|---|---|---|---|---|
| < 0.5 s | 94 | 81.9% | **+8.9** | 0.719 |
| 0.5 – 2 s | 35 | 80.0% | +3.0 | 0.759 |
| 2 – 5 s | 21 | 76.2% | +6.9 | 0.680 |
| 5 – 30 s | 19 | 79.0% | +2.4 | 0.754 |
| > 30 s | 4 | 75.0% | **−8.9** | 0.830 |

`corr(log1p(book_age), pnl/share) = −0.086`. **If anything the edge is best against the FRESHEST
books.** Per coin, stale (age > 5 s) fills are BNB 8 @ −13.4¢, DOGE 1 @ −90.6¢, ETH 1 @ −77.3¢,
SOL 2 @ +21.9¢, XRP 2 @ +12.2¢, HYPE 9 @ +24.1¢ — noise, and the two that look good have n ≤ 9.

**Verdict: staleness is a symptom of a thin market, not a source of edge. A resting quote nobody has
refreshed is usually resting because nobody wants the other side.** This removes the main argument
for expanding toward *less* liquid coins, and it predicts that capacity does not "run out" in the
way M1 imagined — there was never extra edge down there to run out of.

The M1 staleness measurements themselves reproduce on this longer sample at τ = 5 s:

| coin | frac of closes with an ask in [0.30,0.99] | median quote age | p90 | p99 | frac > 5 s |
|---|---|---|---|---|---|
| bitcoin | 8.9% | 1.05 s | 8.8 s | 43 s | 18.4% |
| ethereum | 4.9% | 2.11 s | 12.9 s | 59 s | 25.5% |
| solana | 6.1% | 2.59 s | 15.1 s | 54 s | 31.5% |
| xrp | 6.5% | 2.65 s | 18.3 s | 60 s | 34.0% |
| dogecoin | 9.7% | 2.42 s | 40.0 s | 350 s | 37.4% |
| bnb | 12.1% | 1.66 s | 40.8 s | 112 s | 34.2% |
| hype | 24.1% | 2.79 s | 46.1 s | 123 s | 40.4% |

The ladder is real and roughly monotone in volume. It just does not produce money.

---

## 6. Capacity — the $250 cap is not the binding constraint anywhere

Fillable notional per signal, measured on the **re-fetched fill book** inside the 3¢ walk bound —
this is what a clip can actually be:

| coin | signals with a fill book | p10 | **p50** | p90 | mean | median FILLED notional | frac of fills at the $250 cap |
|---|---|---|---|---|---|---|---|
| bitcoin | 98 | $2.65 | **$10.51** | $99.19 | $35.80 | $10.51 | **1.5%** |
| ethereum | 35 | $2.65 | **$9.00** | $53.27 | $46.31 | $12.14 | 5.6% |
| solana | 40 | $2.10 | **$7.52** | $30.96 | $12.82 | $6.27 | 0.0% |
| xrp | 37 | $2.40 | **$6.05** | $70.68 | $34.35 | $9.90 | 6.7% |
| dogecoin | 18 | $1.22 | **$9.90** | $27.41 | $13.95 | $10.61 | 0.0% |
| bnb | 51 | $3.50 | **$8.25** | $35.50 | $24.29 | $8.25 | 3.4% |
| hype | 14 | $3.07 | **$8.76** | $72.35 | $27.33 | $8.53 | 0.0% |

**Every coin, including BTC, offers a median of $6-$12 per signal.** M1 §3.3's $250-$400 live
capacity figures came from 2 closes per coin and full 5-level ladders; this is 81 days of the
statistically solid top-of-book measurement, and M1 §3.9 already flagged that the historical tape is
the number to size against. It is.

| coin | $/day @ $250 | $/day @ $25 | ratio | mean notional/fill @$250 |
|---|---|---|---|---|
| bitcoin | +6.30 | +2.10 | 3.0x | $33.3 |
| ethereum | +1.99 | +0.73 | 2.7x | $40.6 |
| solana | −1.15 | −1.02 | 1.1x | $9.3 |
| bnb | −1.81 | −0.48 | 3.8x | $23.9 |

A 10x clip buys ~3x P&L because depth binds. **Per-coin `per_event_cap_usd` calibration (M1 change
spec item 6) matters much less than it looked — the book, not the config, sets the size.** It still
matters as a *loss* limiter: BTC's p90 is $99, one BNB fill took $135 of a losing book, and the
worst trade in the sample deployed $189 on SOL.

**Caveat, stated plainly: the hourly quote tape is TOP OF BOOK ONLY**, so every fill is a one-level
walk. Per-share EV is unaffected (a one-level walk fills at the best ask by construction; C2 §8
caveat 2); **$ P&L is a LOWER BOUND.** `markets.parquet` shows a `book_snapshot_5` channel exists
for these markets and was not fetched — that is how to tighten this.

---

## 7. Parameter sanity per coin — TRAIN ONLY, and the shipped values are not the problem

Measured over TRAIN (49 days, 1,170 closes/coin) at τ = 5 s. **No parameter was changed for any
headline number in this document.**

| coin | σ p05 | σ p50 | σ p95 | **`sigma_1s_floor` 8e-6 binds** | median \|z\| | frac \|z\|>10 | median \|return\| |
|---|---|---|---|---|---|---|---|
| bitcoin | 6.20e-06 | 2.83e-05 | 9.79e-05 | **7.8%** | 31.7 | 82.6% | 20.4 bp |
| ethereum | 1.04e-05 | 3.81e-05 | 1.38e-04 | 3.2% | 27.3 | 81.8% | 24.1 bp |
| bnb | 1.47e-05 | 3.78e-05 | 1.18e-04 | 0.26% | 25.7 | 79.8% | 22.7 bp |
| xrp | 2.92e-05 | 5.46e-05 | 1.47e-04 | **0%** | 20.3 | 74.8% | 28.0 bp |
| dogecoin | 3.66e-05 | 7.15e-05 | 1.59e-04 | **0%** | 17.9 | 69.3% | 32.8 bp |
| solana | 4.97e-05 | 7.79e-05 | 1.77e-04 | **0%** | 16.4 | 66.8% | 30.7 bp |
| hype | 5.35e-05 | 1.47e-04 | 3.11e-04 | **0%** | 20.7 | 74.3% | 66.0 bp |

**`sigma_1s_floor = 8e-6` is NOT grossly wrong for the more volatile coins — it binds *less* on them
(0% on SOL/XRP/DOGE/HYPE vs 7.8% on BTC), which is exactly what a floor should do.** DOGE's median
|z| is *lower* than BTC's, not higher. The BTC-calibrated floor and the 120 s window transfer without
distortion; the losses are not a calibration artifact.

`sigma_1s_floor` × `edge_min` grid, **TRAIN only** (`t6b_param_sweep_train.csv`): raising the floor
to 5e-5 costs BTC nearly half its edge (+10.2¢ → +5.7¢) and does **not** rescue SOL
(−12.9¢ → −10.6¢) or XRP (−1.3¢ → −1.3¢). Raising `edge_min` to 0.10 does not rescue them either
(SOL −21.7¢, XRP −5.9¢). **No parameter setting tested makes SOL or XRP positive on TRAIN.**

### 7.1 The one guard worth quantifying — and it does not survive walk-forward

§4.3's inversion suggests a `|z|` band. Selected on **TRAIN** (49 days), then evaluated **once** on
the untouched **TEST** (32 days), $250 clip:

| rule | ALL 7 ¢/share | ALL 7 $/day | non-BTC $/day | BTC $/day |
|---|---|---|---|---|
| **TRAIN** shipped | +7.5 | +12.50 | +5.25 | +7.25 |
| **TRAIN** `1 ≤ \|z\| ≤ 5` ← best on TRAIN | **+14.5** | **+11.84** | +3.21 | +8.63 |
| **TRAIN** `2 ≤ \|z\| ≤ 5` | +13.3 | +8.23 | +2.01 | +6.22 |
| **TRAIN** `\|z\| ≥ 2` | +9.2 | +9.59 | +4.36 | +5.24 |
| **TEST** shipped | +3.7 | +2.36 | **−2.48** | +4.84 |
| **TEST** `1 ≤ \|z\| ≤ 5` | +6.5 | **+0.49** | **−2.56** | +3.05 |
| **TEST** `2 ≤ \|z\| ≤ 5` | +14.4 | +4.68 | +2.23 (n=11) | +2.45 |
| **TEST** `\|z\| ≥ 2` | +5.8 | +6.00 | +2.13 (n=20) | +3.87 |

**The TRAIN-selected rule fails out of sample** ($11.84 → $0.49/day) and it *lowers* BTC's $/day in
both halves. Two runners-up look better on TEST but on 11-20 fills, which is exactly the sample size
this project has learned not to trust (see BNB, §9).

**Report: the |z| profile is a real property of the fill sample (§4.3) but its significance is
split-dependent (p = 0.0055 on the chosen cut, p = 0.81 on the neighbouring one). A |z| band is NOT
a validated trading rule on this evidence and must not be shipped.** The right
next step is to test it on the BTC tape specifically, where n is largest and where the effect is
strongest (52 fills at +20.4¢ vs 14 at −12.9¢).

---

## 8. Portfolio view — correlation is not the problem; edge is

### 8.1 Running everything at once (repeated from §0 with t-stats)

| portfolio | fills | trades/day | ¢/share | win | t_day (¢/share) | $/day @$250 |
|---|---|---|---|---|---|---|
| ALL 7 | 173 | 2.14 | +6.33 | 80.3% | 1.34 | +8.49 |
| non-BTC | 107 | 1.32 | +2.01 | 75.7% | 0.45 | +2.20 |
| BTC+ETH+SOL+XRP | 120 | 1.48 | +6.73 | 80.0% | 1.87 | +7.41 |
| **BTC+ETH** | 84 | 1.04 | **+12.59** | 88.1% | **3.25** | **+8.29** |
| BTC only | 66 | 0.81 | +13.32 | 87.9% | 3.30 | +6.30 |

### 8.2 Do the coins mis-price at the same moments? Mostly not.

**(a) Daily P&L correlation** (81 days, 0 on no-trade days) is near-useless at 0.14-0.36 fills/day
per coin — most cells are structural zeros. The only readable number is **BTC-ETH +0.61**; every
other |r| ≤ 0.22. Read it as "the two coins that actually trade, trade together".

**(b) Late sign flips — n = 1,915 closes per coin, the high-power version.** A "flip" is
`sign(S_t − S_open)` at τ = 5 s disagreeing with the settle. It is the event behind every loss and
it is measurable on *every* close, not only on fills.

| coin | bitcoin | ethereum | solana | xrp | dogecoin | **bnb** | hype |
|---|---|---|---|---|---|---|---|
| flip rate | 1.044% | 0.836% | 0.992% | 0.940% | 0.836% | **2.193%** | 0.783% |

Pairwise φ correlations of flip events are **0.00-0.15** (BTC-XRP 0.150 largest). Joint occurrence
versus independence:

| coins flipping in the same hour | observed | expected if independent |
|---|---|---|
| 0 | 1,782 | 1,773.6 |
| 1 | 122 | 137.0 |
| **2** | **9** | 4.4 |
| **3** | **2** | 0.08 |
| ≥4 | 0 | 0.00 |

**M1 §3.6 worried that seven coins are one levered crypto-beta bet. On the quantity that matters —
the last-5-seconds flip — they are close to independent.** Hourly *returns* correlate 0.67-0.90
(M1), but that correlated variance is already resolved before entry, exactly as M1's own mitigation
argued. There is a real but small joint tail: 3-coin hours occur twice where 0.08 are expected
(25x), so sizing should assume 3 concurrent losers is possible, not 1.

**(c) Actual concurrency is negligible.** Of **162** closes that produced any fill, only **10 had ≥2
concurrent fills** — 9 pairs and one triple — and they broke **7 all-win, 0 all-lose, 3 mixed**. The
worst concurrent hour in 81 days lost **$0.43**. `max_open_notional` $1,000 never binds.
*(Corrected 2026-07-28 from a published 164 / 11 / 7-1-3; the stored `t7c_concurrency.csv` has 162
rows summing to 173 fills and contains no all-lose hour. The correction makes the joint-tail risk
look smaller, not larger, so §8.2b's "assume 3 concurrent losers is possible" sizing advice stands
on the flip-rate analysis rather than on any observed cluster.)*
**Correlated edges are not why the expansion fails. It fails because five of the six new coins have
no measurable edge.**

---

## 9. Train / test, and the BNB lesson

TRAIN = 2026-05-08 … 06-25 (49 days). TEST = 2026-06-26 … 07-27 (32 days), untouched until §7.1.
The shipped parameters were fixed by the BTC work *before* this run, so no headline number is a
tuned number; the split measures temporal stability.

| coin | TRAIN fills | TRAIN ¢/share | TRAIN t_day | TEST fills | TEST ¢/share | TEST t_day | TEST $/day |
|---|---|---|---|---|---|---|---|
| **bitcoin** | 45 | +10.2 | 2.33 | 21 | **+20.0** | 2.32 | **+4.84** |
| **ethereum** | 12 | +4.9 | 0.97 | 6 | **+20.0** | 6.42 | **+1.23** |
| hype | 9 | +26.3 | 4.82 | 4 | +24.1 | 1.80 | +1.49 |
| solana | 17 | **−12.9** | −1.24 | 4 | +14.4 | 7.83 | +0.07 |
| xrp | 10 | −1.3 | 0.34 | 5 | −15.1 | −0.95 | +0.92 |
| dogecoin | 9 | +8.7 | 0.42 | 2 | −31.1 | −0.69 | −0.05 |
| **bnb** | 17 | **+17.2** | **1.58** | 12 | **−29.6** | −1.43 | **−6.14** |

**BNB is why "n < 20 proves nothing" is a rule in this project.** On TRAIN it was the best non-BTC
coin by ¢/share and the natural first coin to enable. On TEST it is the worst thing in the table by
a wide margin. Its 2.19% flip rate (§8.2b) — 2.1x every other coin, directly confirming M1 §3.5 —
is the structural reason: BNB has the lowest hourly volatility, so its outcome stays undecided into
the final seconds.

**Only bitcoin, ethereum and hype are positive in both halves.**

---

## 10. Stress

All seven coins, each knob alone and then stacked (`t11_stress.csv`), $250 clip:

| variant | ALL 7 fills | ALL 7 ¢/share | ALL 7 $/day | non-BTC ¢/share | non-BTC $/day | BTC ¢/share |
|---|---|---|---|---|---|---|
| **shipped** | 173 | +6.33 | +8.49 | +2.01 | +2.20 | +13.32 |
| fee 0.07 → 0.10 | 171 | +5.75 | +8.10 | +1.40 | +1.98 | +12.85 |
| depth 50% | 173 | +6.33 | +4.54 | +2.01 | +1.32 | +13.32 |
| latency 1500 → 3000 ms (band → τ∈{5,4}) | 109 | +7.20 | +5.48 | **−0.27** | +2.53 | +16.01 |
| book age ≤ 5 s (pre-decision) | 155 | +7.80 | +8.15 | +3.70 | +1.85 | +13.32 |
| **underlying feed lag +300 ms** | 170 | +3.51 | +8.07 | **−2.00** | +2.03 | +13.11 |
| τ nominal (do NOT charge print staleness) | 180 | +5.99 | +8.72 | +1.59 | +2.29 | +13.23 |
| **ALL stacked** | 94 | +6.53 | +2.30 | **−2.71** | +1.01 | +15.38 |

Reading:

* **BTC is robust to every knob** (+12.9¢ to +16.0¢). **non-BTC is not.** A 300 ms underlying feed
  lag alone takes the non-BTC pool from +2.0¢ to **−2.0¢**; the full stack takes it to **−2.7¢**.
  The pooled non-BTC "edge" is smaller than one REST round trip. That is the single clearest
  statement of how thin it is.
* Charging the underlying print's sub-second staleness into `tau` (this harness's default) costs 7
  signals and 0.3¢ — conservative, and it drives nothing.
* The book-staleness filter *raises* ¢/share on every frame, consistent with §5: stale fills are
  marginally bad, not good.
* non-BTC `$/day` stays mildly positive under most single knobs while `¢/share` goes negative —
  because the surviving $ P&L sits in a handful of large-notional wins. Trust the per-share column.

---

## 11. How much live data would it take to know?

Fills needed for a two-sided t = 1.96 against a *true* +10 ¢/share edge, at each coin's own measured
per-fill SD, converted to calendar days at its own fill rate; ×4 fills for day-clustering (C2 §1.3
measured t_day ≈ 0.5 × t_trade).

| coin | fills so far | SD per fill | fills/day | fills needed | **days needed (per-trade)** | **days needed (day-clustered)** | observed t_day |
|---|---|---|---|---|---|---|---|
| bitcoin | 66 | 0.285 | 0.815 | 31 | **38** | **153** | 3.30 |
| hype | 13 | 0.190 | 0.160 | 14 | 86 | 344 | 4.88 |
| ethereum | 18 | 0.313 | 0.222 | 38 | **170** | **678** | 1.94 |
| xrp | 15 | 0.341 | 0.185 | 45 | 241 | 962 | −0.38 |
| solana | 21 | 0.417 | 0.259 | 67 | 258 | 1,030 | −0.92 |
| bnb | 29 | 0.462 | 0.358 | 82 | 229 | 917 | −0.37 |
| dogecoin | 11 | 0.435 | 0.136 | 73 | 535 | 2,139 | −0.09 |

**No non-BTC coin can be validated by live paper trading in a useful timeframe at these fill
rates** — ETH needs ~6 months for a per-trade t and ~2 years day-clustered. Two consequences: the
decision has to be made on the historical tape (as here), and "paper it and see" is an operational
check, not a statistical one. It is also an argument for enabling ETH *cheaply* rather than waiting
for significance that will not arrive.

---

## 12. Recommendation

**Ranked by expected $/day** (full sample, $250 clip): bitcoin +6.30 · ethereum +1.99 ·
hype +1.96 · dogecoin +0.93 · xrp +0.27 · bnb −1.81 · solana −1.15.

**Ranked by statistical strength** (day-clustered t / bootstrap P(EV≤0)): bitcoin (3.30 / 0.000) ·
hype (4.88 / 0.000, n = 13, untradeable) · ethereum (1.94 / 0.080) · dogecoin (−0.09 / 0.43) ·
bnb (−0.37 / 0.57) · xrp (−0.38 / 0.72) · solana (−0.92 / 0.84).

**The two rankings agree** — informative in itself, and different from what M1's frequency screen
implied (it ranked BNB the largest new contributor; BNB is second-worst on both axes here).

| coin | verdict |
|---|---|
| **ethereum** | **ENABLE as an experiment**, behind a coin allowlist, at **$25** `per_event_cap_usd`. +9.9 ¢/share, +$1.99/day at $250 / +$0.73/day at $25, positive in both halves, P(EV≤0)=0.080. Not significant — expect ~+32% on the BTC bot and treat the first 40 live fills as the test. |
| **solana** | **DO NOT ENABLE.** −7.7¢, 57.1% win vs 99.0% direction accuracy; 24% of its signals are zero-information zero-move closes (§4.4). |
| **xrp** | **DO NOT ENABLE.** −5.9¢; 18% zero-move signals; worst fill conversion (38.5%). |
| **dogecoin** | **DO NOT ENABLE.** +1.5¢ with a [−32.8, +30.0] CI on 11 fills; sign flips between halves. |
| **bnb** | **DO NOT ENABLE.** −2.2¢ full sample, −29.6¢ / −$6.14/day on TEST, 2.19% flip rate. The most dangerous coin on the list *because* it looked good on TRAIN. |
| **hype** | **DEFER** (unchanged from M1). No reachable live feed; 13 fills; $8.8 median capacity; 9 of 13 fills against >5 s stale books. |

**Implementation, if ETH is enabled** — everything in M1 §7 still applies; two items become
mandatory rather than advisory, and one is now cheaper than M1 thought:

1. **`strategy.close_snipe.allowed_coins`, defaulting to `["bitcoin"]`** (M1 §7 item 4). With five
   of six new coins measured at or below zero, widening `_HOURLY_RE` without a coin allowlist is a
   direct route to −$1.81/day (BNB) or −$1.15/day (SOL).
2. **One oracle per symbol, failing closed** (M1 §7 item 3). ETH priced off BTC's underlying would
   be silent and catastrophic.
3. **Snap CLOB prices to the tick before comparing against `price_min`/`price_max`** — §1.3c. This
   is a live-bot concern too if any code path ever stores a price as float32.
4. `POST /books` batching (M1 §7 item 1) is **not required for two coins** — 4 tokens sequential is
   ~1.6 s, which fits the 2.5 s band but eats most of it. Do it anyway; it is cheap and it lowers
   `tau_lo`.
5. **$25 cap for ETH, not $250** (§6): the median fill is $9-12 and the p90 is $53, so a large cap
   buys little upside and sizes the tail losses.

**The frequency problem is not solved.** BTC + ETH is 1.04 trades/day against BTC's 0.81. If more
frequency is the goal, this document says to look at the 5m/15m families across coins (M1 §1.3: 383
open 5m markets in a 30 h window against 28 hourly) rather than at more hourly coins — with the
warning that those settle on Chainlink and the corrected 5m number at shipped parameters is
+0.87¢ / t = 0.22 (C2 §6). The other candidate is §4.3: **investigating the |z| profile on BTC may be
worth more than any new coin** — BTC's |z| ≤ 5 subset is 52 fills at +20.4 ¢/share against +13.3¢
for the whole. That is a research lead on the largest sample this project has, not a rule to ship:
§7.1 shows the TRAIN-selected band fails out of sample, and §4.3 shows the p-value moves from 0.0055
to 0.81 under a neighbouring cut point. The right next step is to find the *mechanism* on BTC, not
to fit a threshold.

---

## 13. What is NOT established

1. **$ P&L is a lower bound.** The hourly quote tape is **top of book only**, so every fill is a
   one-level walk. Per-share EV is unaffected; total P&L and the capacity table would improve with
   the `book_snapshot_5` channel, which exists for these markets and was not fetched.
2. **n is small everywhere except BTC.** 66 BTC fills, 29 BNB, 21 SOL, 18 ETH, 15 XRP, 13 HYPE,
   11 DOGE. The *pooled* non-BTC result (107 fills) is the only non-BTC statement with real power,
   and it says "indistinguishable from zero". No per-coin verdict above is more certain than its CI.
3. **The ETH recommendation rests on 18 fills** and P(EV≤0) = 0.080. Its $/day bootstrap CI
   [+$0.54, +$3.95] excludes zero only because its two losing fills happened to be small-notional.
   That is luck, not structure.
4. **The |z| effect is measured, not explained, and its headline p-value is split-dependent.**
   §4.3 shows *that* both tails of the confidence scale lose; it does not identify what the
   counterparty knows at the top end. Candidate explanations (order-book-derived price,
   cross-exchange quotes, sub-tick information) are untested. The p = 0.0055 comes from a cut point
   chosen after seeing the data — `|z| ≤ 2` vs `> 5` gives p = 0.81 — and no multiplicity correction
   has been applied. Until a mechanism is confirmed, a |z| band is curve-fitting, and §7.1 shows it
   does not walk forward.
5. **Concurrency under live load is still unmeasured** (M1 §6 item 8), though §8.2c shows only 11
   closes in 81 days had ≥2 concurrent fills, so the risk is smaller than M1 feared.
6. **HYPE's live feed remains unverified from production** (M1 §6 item 1). Nothing here changes it.
7. **81 days, one regime** (2026-05-08 → 07-27). BTC's edge has now been measured on three tapes and
   periods at +9.8¢ / +13.3¢ / +21.5¢, so it is stable in sign; every non-BTC coin has exactly one
   measurement.
8. **The TEST half is now used.** §7.1 spent it on the |z| guard. Any further rule selection needs a
   new out-of-sample period — the tape can be extended back to 2026-03-15 (`markets.parquet`), which
   would roughly double the sample again.

---

## 14. Files

| path | what |
|---|---|
| `scripts/multicoin/replay_hourly.py` | the harness (imports the property-tested bot primitives from `scripts/fresh5m/replay.py`) |
| `scripts/multicoin/indep_hourly.py` | independently-written second replay of the data layer; 0 discrepancies (§1.3b) |
| `scripts/multicoin/m3_report.py` | regenerates every table below. **Patched 2026-07-28**: the §4.3 `\|z\|` binning dropped `\|z\|` exactly 0 (`pd.cut` left edge 0 with `right=True`); the bins are now left-open below zero and a `assert` requires the partition to be total, so the failure cannot recur silently. It also now prints both the (1,5] and the ≤2 split against `\|z\|>5`, so §4.3's split-sensitivity caveat is reproducible. |
| `data/multicoin/m3/trades_shipped.parquet` | every signal, 7 coins, 81 days: fair, z, book ages, fill, P&L |
| `data/multicoin/m3/trades_shipped_float32raw.parquet` | the pre-fix tape, kept so §1.3c is reproducible |
| `t0_settle.csv` | §1.3a settle reconciliation |
| `t1_headline_full.csv`, `t1b_train_test.csv`, `t1c_clip.csv`, `t15_combos.csv` | §2, §8.1, §9 |
| `t2_staleness.csv`, `t2b_stale_fills.csv` | §5 staleness sweep |
| `t3_funnel.csv` | §3 |
| `t4b_competition_crosscoin.csv`, `t4c_ev_by_bookage.csv` | §5 |
| `t45_liquidity_capacity.csv`, `t5b_capacity_per_signal.csv` | §6 |
| `t6_param_sanity.csv`, `t6b_param_sweep_train.csv` | §7 (TRAIN only) |
| `t7b_daily_pnl_corr.csv`, `t7c_concurrency.csv`, `t8d_flip_corr.csv` | §8 |
| `t8_edge_decomposition.csv`, `t8b_degenerate.csv`, `t8c_adverse_selection.csv`, `t12_win_vs_ask.csv`, `t13_tick.csv` | §4 |
| `t9_bootstrap.csv`, `t9b_win_by_z.csv` | §2, §4.3 |
| `t10_power.csv` | §11 |
| `t11_stress.csv` | §10 |
| `t14_guards.csv` | §7.1 |
| `data/multicoin/quotes/<coin>/<D>.parquet` | extended 45 → **80 days** by this run (2026-05-08 → 07-26) |
| `data/multicoin/binance/<SYM>/<D>.parquet` | extended 35 → **80 days** by this run |

(All `t*.csv` live in `data/multicoin/m3/`.)

Reproduce:

```bash
python3 scripts/multicoin/fetch.py verify --start 2026-05-08 --end 2026-07-26   # §1.3a
python3 scripts/multicoin/m3_report.py                                          # every table
python3 scripts/multicoin/indep_hourly.py bitcoin,ethereum,solana,xrp,dogecoin,bnb,hype \
  $(python3 -c "import pandas as pd;print(','.join(pd.date_range('2026-07-01','2026-07-20').strftime('%Y-%m-%d')))")
python3 scripts/multicoin/replay_hourly.py --coins ethereum --cap-usd 25        # the recommendation
```
