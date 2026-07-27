# M3 — close_snipe backtested on every non-BTC hourly family

**Date:** 2026-07-27 (UTC) · **Scope:** run the shipped `close_snipe` strategy, at the shipped
parameters, on all seven Polymarket hourly Up/Down families, and decide whether the bot goes from
~0.9 trades/day to ~5-6/day.
**Sample:** 2026-05-08 → 2026-07-27 UTC, **81 days × 7 coins = 13,405 hourly closes** (1,915 per
coin), against the Binance feed each coin actually resolves on.

---

## 0. Bottom line

**The frequency multiplies. The edge does not. Do not enable SOL, XRP, DOGE or BNB.**

At the shipped parameters, running all seven coins raises trading frequency **2.7x**
(0.81 → 2.17 fills/day) and moves **$/day from $6.30 to $6.09** — i.e. the entire frequency gain is
given back, and then some, in worse per-trade edge. On the untouched **TEST** half the seven-coin
portfolio makes **$2.29/day against BTC-alone's $4.84/day**.

| portfolio | fills | trades/day | ¢/share | win | t_day | **$/day @ $250** |
|---|---|---|---|---|---|---|
| **BTC only** (today's bot) | 66 | 0.81 | **+13.32** | 87.9% | **3.30** | **+6.30** |
| BTC + ETH | 85 | 1.05 | +13.25 | 88.2% | **3.47** | **+8.36** |
| BTC + ETH + HYPE | 98 | 1.21 | +14.9 | 89.8% | 4.59 | +10.32 |
| BTC + ETH + SOL + XRP | 123 | 1.52 | +6.6 | 78.9% | 2.04 | +5.00 |
| **ALL 7** | 176 | **2.17** | +6.25 | 79.5% | 1.49 | **+6.09** |
| **non-BTC only** | 110 | 1.36 | **+2.01** | 74.5% | 0.65 | **−0.21** |

**The pooled non-BTC edge is +2.0 ¢/share with a day-block bootstrap 95% CI of
[−6.4 ¢, +9.7 ¢] and P(EV ≤ 0) = 0.31, on 110 fills over 81 days. It is indistinguishable from
zero, and its $/day point estimate is negative.** That is the decision statistic for this run.

Five findings, in order of how much they change the plan:

1. **Only ETHEREUM survives as a real expansion candidate**, and it is worth about
   **+$2.06/day** (+33% on top of BTC) at 0.23 trades/day — not the 5-6x frequency the run was
   chasing. n = 19 fills; P(EV ≤ 0) = 0.042. It is positive in both TRAIN (+9.8¢, 13 fills) and
   TEST (+20.0¢, 6 fills). §2, §9.
2. **`close_snipe` is not "the book has not caught up yet". It is a modest edge over the
   market-implied probability, and it INVERTS where the model is most confident.** Pooled over all
   coins: at |z| ∈ (1, 5] it wins **91.1% on 112 fills (+11.7 ¢/share)**; at |z| > 5 it wins
   **70.0% on 30 fills (−5.4 ¢/share)** — **Fisher exact p = 0.0055**. Every coin's realised win
   rate falls far below its own unconditional direction accuracy (BTC 87.9% vs 98.96%,
   p < 1e-4). This is adverse selection, it is significant, and **it applies to the shipped BTC bot
   too**. §4.
3. **The competition/staleness hypothesis from M1 §3.2-3.3 is REJECTED.** Across coins, EV/share
   regressed on median book depth (r = +0.16), on log median volume (r = −0.12), on median quote
   age (r = −0.12) and on p90 quote age (r = +0.22) — every r² ≤ 0.05 on n = 7. Within the fill
   sample, `corr(log1p(book_age), pnl/share) = −0.093` over 176 fills and EV is *flat to falling*
   in book age. **Thinner, staler books do not pay more.** §5.
4. **BNB is the cautionary tale this project needed.** On TRAIN it was the best non-BTC coin
   (+17.2 ¢/share, 17 fills, t_day 1.58). On the untouched TEST half it is **−29.6 ¢/share,
   −$6.14/day on 12 fills**. Its measured late sign-flip rate is 2.19% — 2.1x the next worst coin,
   confirming M1 §3.5 — and it is still the largest non-BTC signal generator (0.36 fills/day). A
   naive rollout would have put the most new volume in the worst coin. §9.
5. **Capacity is far below the $250 cap on every coin, BTC included.** Median fillable notional per
   signal is **$5.9-$10.2** and the $250 cap binds on 0-6% of fills. Going from a $25 to a $250
   clip raises BTC's $/day from $2.10 to $6.30 — 3x, not 10x. **The clip is not the constraint and
   more coins is not a capacity story.** §6.

**Recommendation: add `ethereum` only, behind a coin allowlist, at a $25 clip, and paper it.
Leave `solana`, `xrp`, `dogecoin`, `bnb` off. `hype` stays deferred (§2, §12).** The frequency
problem is NOT solved by this expansion and should be attacked elsewhere.

---

## 1. What was run, and how it was verified

### 1.1 The harness is the validated one, extended only at the data layer

`scripts/multicoin/replay_hourly.py` **imports** the bot primitives from
`scripts/fresh5m/replay.py` rather than re-deriving them:

| imported | source of truth |
|---|---|
| `evaluate_close_snipe_port` | `bot/polybot/strategy.py:evaluate_close_snipe` |
| `snipe_tau_bounds_port` | `strategy.snipe_tau_bounds` |
| `walk_asks_port` | `bot/polybot/fill_engine.py:walk_asks` |
| `fee_per_share`, `normal_cdf` | `fill_engine.fee_per_share`, `oracle.normal_cdf` |
| `BookTape`, `_t` (day-clustered t) | `replay.py` |

so `scripts/fresh5m/proptest.py` (0 mismatches vs the live bot on 80,000 randomised inputs,
audit/C2_harness.md §1.1) covers this run unchanged. The window gate — tick from the early end of
the τ band toward the close, **one entry per window, first qualifying side** — is the same
`engine._maybe_snipe` mirror.

Parameters are `bot/config.yaml` verbatim: `edge_min` 0.03, `price_min` 0.30, `price_max` 0.99,
`vol_window_secs` 120, `sigma_1s_floor` 8e-6, `fair_cap` 0.98, `snipe_last_secs` 5.0,
`snipe_min_tau_secs` 2.5, `snipe_fill_margin_secs` 0.5, `latency_ms` 1500,
`max_walk_above_best` 0.03, `per_event_cap_usd` 250, `fee_rate` 0.07. The derived decision grid is
**τ ∈ {5, 4, 3} s**.

### 1.2 What is new, and why each piece is the honest choice

* **Underlying = Binance 1s klines on that coin's own resolution symbol** (M1 §2: BTCUSDT,
  ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT spot; **HYPEUSDT USD-M futures**). No Chainlink
  anywhere — the hourly family does not settle on it.
* **Anchors from traded prints only.** `S_open` = the first `volume > 0` bar in `[open_s, close_s)`.
  M1 §2.3 showed the 1s archive back-fills silent seconds at `volume = 0`, which flips the settle
  on near-ties, and it hurts the thin coins most.
* **Causal `S_t`.** A 1s bar covering second `s` is only complete at `s+1`, so the newest price the
  bot could hold at decision instant `t` is the close of the newest `volume > 0` bar whose **bar
  end ≤ t**. `tau` is then measured from that print's observation instant to the close, so the
  sub-second staleness is charged as risk rather than ignored (the same convention `replay.py`
  applies to Chainlink's publication lag). Charging it costs 7 signals across all coins (§10).
* **σ the way the live bot computes it.** `BinanceOracle.rolling_log_return_std` runs on a ~1 Hz
  REST poll of the *last trade price*, so silent seconds contribute a **zero** return. The offline
  analogue is a forward-filled 1-second grid, rolling std ddof=1 over 120 samples, min_periods 30.
  (This is precisely the "quiet polling underestimates vol" effect `sigma_1s_floor` exists for.)
* **Two-book fill.** Signal at `t` against the book as of `t`; fill against the book **re-fetched at
  t + 1500 ms**. 34-55% of signals die there (§3) — never the signal book.
* **Book staleness** from the venue's own last-update stamp (`timestamp_us`; M1 §3.2 verified it
  never moves while the book hash is unchanged). Reported PRE-decision, i.e. a stale book is handed
  to the evaluator as `None`, exactly as the live bot sees a book it could not fetch.
* **Day-clustered t** is the headline statistic throughout; per-trade t is shown next to it and
  runs ~1.2-2x hot.

### 1.3 Verification — three checks, all passed

**(a) Settle reconciliation: 100.000% on 13,405 hours.** The Up/Down outcome was re-derived from
each coin's own Binance candle (`close >= open ⇒ Up`, anchors from traded prints) and compared to
Telonex's `result_id` over the exact replay window:

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
project 3-for-3 losing fills is excluded by measurement, not by assumption.

**(b) A second, independently-written replay agrees on every field.**
`scripts/multicoin/indep_hourly.py` re-implements the whole data layer from raw parquet in a
different style (its own anchor search, σ grid, book lookup, fee/fair arithmetic) importing nothing
from the harness. On **7 coins × 20 days (2026-07-01 … 07-20), 74 signals**:

```
bitcoin  harness=25 indep=25 both=25 left_only=0 right_only=0 | tau=0 side=0 outcome=0 fair=0
         book_age_sig=0 book_age_fil=0 avg_price=0 shares=0 pnl_per_share=0 won=0
ethereum  9/9 · solana 8/8 · xrp 9/9 · dogecoin 4/4 · bnb 17/17 · hype 2/2   — all 0
TOTAL DISCREPANCIES: 0
```

**(c) A real determinism bug was found by (b) and fixed.** **19.2% of quote rows share
`timestamp_us` with another row on the same book.** With an unstable sort, "the last update at or
before t" is ambiguous, and the two implementations picked different books on ~5% of fills (up to
6¢ apart on the fill price). Both now sort by `(timestamp_us, local_timestamp_us)` with a stable
mergesort. Any earlier multi-coin number produced without that tie-break is not reproducible.

**(d) The harness reproduces the known BTC baseline on an independent tape.**

| | trades/day | ¢/share | win |
|---|---|---|---|
| BTC backtest (prior work) | ~0.9 | +9.8 | — |
| BTC live paper, 10 days, 8 trades | ~0.8 | +21.5 | 87.5% |
| **this harness, BTC, 81 days, 66 fills** | **0.81** | **+13.3** | **87.9%** |

Different vendor tape, different period, and the win rate lands within 0.4 points of live.

---

## 2. Per-coin headline — shipped parameters, full 81-day sample

`book_moved` = signal fired, the re-fetched fill book no longer had the edge (the two-book filter).
`empty` = the fill book had no ask at all. CI is the day-clustered 95% interval on ¢/share;
`P(EV≤0)` is a 4,000-draw day-block bootstrap.

| coin | closes | signals | fills | book_moved | empty | **¢/share** | 95% CI | P(EV≤0) | win | trades/day | t_trade | **t_day** | **$/day @$250** | $/day @$25 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **bitcoin** | 1,915 | 104 | **66** | 33 | 5 | **+13.32** | [+5.2, +20.6] | **0.000** | 87.88% | 0.815 | 3.80 | **3.30** | **+6.30** | +2.10 |
| **ethereum** | 1,915 | 39 | 19 | 16 | 4 | **+13.03** | [+2.5, +30.8] | **0.042** | 89.47% | 0.235 | 1.71 | **2.30** | **+2.06** | +0.79 |
| solana | 1,915 | 43 | 22 | 19 | 2 | **−8.78** | [−27.4, +7.5] | 0.877 | 54.55% | 0.272 | −1.00 | −1.12 | **−3.60** | −1.34 |
| xrp | 1,915 | 40 | 16 | 22 | 2 | **−7.53** | [−22.6, +11.7] | 0.791 | 62.50% | 0.198 | −0.90 | −0.63 | +0.24 | −0.12 |
| dogecoin | 1,915 | 19 | 11 | 7 | 1 | **+1.48** | [−32.8, +30.0] | 0.430 | 81.82% | 0.136 | 0.11 | −0.09 | +0.93 | +0.46 |
| bnb | 1,915 | 54 | 29 | 24 | 1 | **−2.16** | [−21.4, +14.6] | 0.573 | 72.41% | 0.358 | −0.25 | −0.37 | −1.81 | −0.48 |
| hype † | 1,915 | 17 | 13 | 2 | 2 | **+25.64** | [+15.3, +35.9] | 0.000 | 100.0% | 0.161 | 4.88 | 4.88 | +1.96 | +0.97 |
| **POOLED non-BTC** | 11,490 | 212 | **110** | 90 | 12 | **+2.01** | boot [−6.4, +9.7] | **0.307** | 74.55% | **1.358** | 0.54 | **0.65** | **−0.21** | — |

† **HYPE is not tradeable today.** M1 §6 established that `fapi.binance.com` is HTTP-451 from this
environment and `data-api.binance.vision` has no futures path, so the live bot has **no reachable
price feed for it**; the backtest used the bulk futures archive. Its 13 fills are also n < 20 and
9 of them filled against books older than 5 s. Treat +25.6¢ as a hypothesis, not a result.

**Removing the best day and the best 3 days** (concentration check, full sample, $250 clip):

| | $/day | −best day | −best 3 days | ¢/share −best 3 days |
|---|---|---|---|---|
| BTC | +6.30 | +3.93 | +2.56 | +11.0 |
| BTC + ETH | +8.36 | +5.24 | +3.90 | +11.2 |
| ALL 7 | +6.09 | +3.07 | **+1.38** | +5.0 |
| non-BTC | −0.21 | −1.33 | **−2.60** | −0.1 |

The seven-coin portfolio loses 77% of its P&L to three days. BTC alone loses 59%. Neither is
comfortable; the seven-coin version is worse.

---

## 3. Frequency — the funnel, and what it actually delivers

| coin | closes | signals | signal rate | book_moved | empty | fills | **fill conversion** | signals/day | **fills/day** |
|---|---|---|---|---|---|---|---|---|---|
| bitcoin | 1,915 | 104 | 5.43% | 33 | 5 | 66 | **63.5%** | 1.284 | **0.815** |
| ethereum | 1,915 | 39 | 2.04% | 16 | 4 | 19 | 48.7% | 0.482 | 0.235 |
| solana | 1,915 | 43 | 2.25% | 19 | 2 | 22 | 51.2% | 0.531 | 0.272 |
| xrp | 1,915 | 40 | 2.09% | 22 | 2 | 16 | **40.0%** | 0.494 | 0.198 |
| dogecoin | 1,915 | 19 | 0.99% | 7 | 1 | 11 | 57.9% | 0.235 | 0.136 |
| bnb | 1,915 | 54 | 2.82% | 24 | 1 | 29 | 53.7% | 0.667 | 0.358 |
| hype | 1,915 | 17 | 0.89% | 2 | 2 | 13 | 76.5% | 0.210 | 0.161 |
| **TOTAL** | 13,405 | **316** | 2.36% | 123 | 17 | **176** | 55.7% | **3.90** | **2.17** |

Two things to read here.

* **The 2-book fill is the single largest filter and it is worse on the thin coins.** BTC converts
  63.5% of signals into fills; XRP 40.0%, ETH 48.7%. This is the mechanism the project already
  knows kills 34% of 5m signals; on the thin hourly coins it kills up to 60%.
* **M1's frequency screen was close on signals and 2x optimistic on trades.** M1 §3.4 projected
  4.40 indicative signals/day across the seven coins; measured properly — causal `S_t`, real σ, the
  τ ∈ {5,4,3} grid, one entry per window — it is **3.90 signals/day**, i.e. the screen was good to
  13%. But signals are not trades: after the two-book fill it is **2.17 fills/day**, **2.0x below
  the screen**. M1 was explicit that its number was an upper bound on frequency with no fill
  simulation and no PnL claim; that reading was correct and the gap is exactly the fill stage.

**So: 0.81 → 2.17 trades/day, a 2.7x increase, not the 5-6x hoped for. And it does not pay.**

---

## 4. Why it does not pay — the mechanism

### 4.1 Every coin's realised win rate is far below its own direction accuracy

`close_snipe`'s theoretical ceiling is "at τ = 5 s, does `sign(S_t − S_open)` already equal the
settle?" That is measured on **all 1,915 closes per coin**, so it has real power. The realised win
rate on fills is measured on the fills. The gap is adverse selection.

| coin | direction accuracy @τ=5s (n=1,915) | realised win on fills | n fills | **shortfall** | one-sided binomial p |
|---|---|---|---|---|---|
| bitcoin | 98.96% | 87.88% | 66 | **−11.1 pt** | **4.7e-07** |
| ethereum | 99.16% | 89.47% | 19 | −9.7 pt | 0.0109 |
| solana | 99.01% | 54.55% | 22 | **−44.5 pt** | 5.4e-15 |
| xrp | 99.06% | 62.50% | 16 | −36.6 pt | 5.1e-09 |
| dogecoin | 99.16% | 81.82% | 11 | −17.4 pt | 0.0037 |
| bnb | **97.81%** | 72.41% | 29 | −25.4 pt | 1.5e-07 |
| hype | 99.22% | 100.0% | 13 | +0.8 pt | 1.00 |

**Six of seven coins reject the "the book just hasn't caught up" model at p < 0.02.** The closes
where a mispriced ask is still standing 5 s before the close are systematically the closes where
the model is wrong.

### 4.2 The right benchmark is the ask, not the model

Because `EV = win − ask − fee`, the honest question is *does the strategy beat the market-implied
probability it pays?*

| coin | n | realised win | mean fill ask | **edge over the market (pt)** | P(win > ask) | ¢/share |
|---|---|---|---|---|---|---|
| bitcoin | 66 | 0.8788 | 0.7344 | **+14.4** | **0.0037** | +13.3 |
| ethereum | 19 | 0.8947 | 0.7532 | **+14.2** | 0.117 | +13.0 |
| hype | 13 | 1.0000 | 0.7323 | +26.8 | 0.017 | +25.6 |
| dogecoin | 11 | 0.8182 | 0.7927 | +2.6 | 0.593 | +1.5 |
| bnb | 29 | 0.7241 | 0.7341 | −1.0 | 0.641 | −2.2 |
| xrp | 16 | 0.6250 | 0.6888 | −6.4 | 0.797 | −7.5 |
| solana | 22 | 0.5455 | 0.6195 | −7.4 | 0.826 | −8.8 |
| **ALL 7** | 176 | 0.7955 | 0.7214 | **+7.4** | **0.015** | +6.3 |
| **non-BTC** | 110 | 0.7455 | 0.7135 | **+3.2** | **0.266** | +2.0 |

**The real edge is a few points over the market price, not the 25 points the model's `fair`
claims.** BTC and (weakly) ETH clear it; nothing else does, and the non-BTC pool does not.

### 4.3 The inversion: the model is least accurate exactly where it is most confident

Pooled over all seven coins, by the model's own `|z|` at entry:

| \|z\| | fills | win rate | mean ask | ¢/share |
|---|---|---|---|---|
| < 1 | 24 | 66.7% | 0.549 | +10.1 |
| 1 – 2 | 51 | 86.3% | 0.766 | +8.5 |
| **2 – 5** | **61** | **95.1%** | 0.798 | **+14.3** |
| **5 – 10** | 21 | **71.4%** | 0.754 | **−5.0** |
| **> 10** | 9 | **66.7%** | 0.720 | **−6.3** |

**|z| ∈ (1,5]: 112 fills, 91.1% win, +11.7 ¢/share. |z| > 5: 30 fills, 70.0% win, −5.4 ¢/share.
Fisher exact p = 0.0055.** For BTC alone the split is 52 fills at 96.2% / +20.4¢ versus 14 fills at
57.1% / **−12.9¢**.

The economics are obvious once stated: when `|z|` is enormous, `fair` is pinned at the 0.98 clip and
the bot will lift *any* ask below ~0.94. The only reason such an ask exists is that somebody who is
watching more than a 1-second last-trade print disagrees — and they are right more often than the
model. **This is a property of the shipped BTC strategy, not of the new coins.** It is the single
most actionable finding in this document and it belongs in the BTC workstream.

### 4.4 The coarse-tick failure mode — why SOL and XRP specifically lose

`fair` is `Φ(ln(S_t/S_open) / (σ√τ))`. When the underlying has not moved *at all* since the hour
open, `z = 0`, `fair = 0.500` on both sides, and `edge_min = 0.03` fires against **any ask ≤ 0.455**
— on zero information.

| coin | underlying tick | tick in bp | closes within 1 tick of open @τ=5s | **signals from an exactly-zero move** |
|---|---|---|---|---|
| bitcoin | $0.01 | 0.0016 | 0.00% | **0 of 104** |
| ethereum | $0.01 | 0.055 | 0.10% | 0 of 39 |
| bnb | $0.01 | 0.170 | 0.10% | 0 of 54 |
| hype | $0.001 | 0.160 | 0.21% | 0 of 17 |
| xrp | $0.0001 | 0.875 | 0.99% | **8 of 40 (20%)** |
| **solana** | $0.01 | **1.298** | 1.25% | **11 of 43 (26%)** |
| dogecoin | $0.00001 | 1.130 | 1.25% | 1 of 19 |

Zero-move closes are 1.25% of SOL's closes but **26% of SOL's signals** — a 21x over-representation,
because that is exactly when the market offers something the model thinks is cheap. The consequence
is visible directly:

| coin | \|z\| < 2 fills | win | ¢/share | \|z\| ≥ 2 fills | win | ¢/share |
|---|---|---|---|---|---|---|
| solana | 19 | **47.4%** | **−11.8** | 3 | 100% | +10.6 |
| xrp | 11 | **54.5%** | **−8.3** | 5 | 80% | −5.8 |
| bitcoin | 28 | 96.4% | +24.5 | 38 | 81.6% | +5.1 |

**On SOL and XRP the strategy is mostly buying coin flips at a discount that isn't one.** This is a
structural defect of the Gaussian fair value on coarse-tick underlyings, not a tuning question.

---

## 5. The competition hypothesis — TESTED AND REJECTED

M1 §3.2-3.3 established that thinner coins carry staler quotes and hypothesised that this is a
durable structural reason to trade them. **It is not borne out.**

**Cross-coin regression of ¢/share on liquidity (n = 7 coins):**

| x | slope | Pearson r | r² | t | fill-weighted slope |
|---|---|---|---|---|---|
| log10(median market volume) | −0.0176 | **−0.125** | 0.016 | −0.28 | +0.033 |
| median in-band ask notional | +0.0048 | +0.163 | 0.027 | +0.37 | +0.009 |
| median quote age | −0.0250 | −0.124 | 0.015 | −0.28 | −0.062 |
| p90 quote age | +0.0018 | +0.216 | 0.047 | +0.49 | −0.001 |
| fraction of quotes older than 5 s | −0.1368 | −0.080 | 0.007 | −0.18 | −0.556 |

Every relationship is a coin toss, and the signs flip when weighted by fill count. **n = 7 has no
power** — that is the honest caveat — so the *within*-sample test matters more:

**Within-sample (176 fills, all coins pooled), EV by book age at the fill:**

| book age | fills | win | ¢/share | mean ask |
|---|---|---|---|---|
| < 0.5 s | 95 | 82.1% | **+9.5** | 0.714 |
| 0.5 – 2 s | 37 | 75.7% | +1.2 | 0.735 |
| 2 – 5 s | 21 | 76.2% | +6.9 | 0.680 |
| 5 – 30 s | 19 | 79.0% | +2.4 | 0.754 |
| > 30 s | 4 | 75.0% | **−8.9** | 0.830 |

`corr(log1p(book_age), pnl/share) = −0.093`. **If anything the edge is best against the FRESHEST
books.** Per-coin, stale (age > 5 s) fills are: BNB 8 fills at −13.4¢, DOGE 1 at −90.6¢,
ETH 1 at −77.3¢, SOL 2 at +21.9¢, XRP 2 at +12.2¢, HYPE 9 at +24.1¢. There is no signal, only
noise, and the two coins where stale fills look good (HYPE, XRP) have n ≤ 9.

**Verdict: staleness is a symptom of a thin market, not a source of edge. A resting quote nobody has
refreshed is usually resting because nobody wants the other side.** This also removes the main
argument for expanding toward *less* liquid coins.

The M1 staleness measurements themselves reproduce, on this longer sample and at τ = 5 s:

| coin | frac of closes with an ask in [0.30,0.99] | median quote age | p90 | p99 | frac > 5 s |
|---|---|---|---|---|---|
| bitcoin | 8.9% | 1.05 s | 8.8 s | 43 s | 18.4% |
| ethereum | 4.9% | 2.11 s | 12.8 s | 59 s | 25.5% |
| solana | 6.1% | 2.59 s | 15.1 s | 54 s | 31.5% |
| xrp | 6.5% | 2.65 s | 18.3 s | 60 s | 34.0% |
| dogecoin | 9.9% | 2.42 s | 40.0 s | 350 s | 37.4% |
| bnb | 12.2% | 1.66 s | 40.8 s | 112 s | 34.2% |
| hype | 24.2% | 2.79 s | 46.1 s | 123 s | 40.4% |

The staleness ladder is real and monotone-ish in volume, exactly as M1 said. It just does not
produce money.

---

## 6. Capacity — the $250 cap is not the binding constraint anywhere

Fillable notional per signal, measured on the **re-fetched fill book** inside the 3¢ walk bound
(this is what a clip can actually be):

| coin | signals with a fill book | p10 | **p50** | p90 | mean | median FILLED notional | frac of fills at the $250 cap |
|---|---|---|---|---|---|---|---|
| bitcoin | 99 | $2.39 | **$10.21** | $98.08 | $35.46 | $10.51 | **1.5%** |
| ethereum | 35 | $2.65 | **$9.00** | $53.27 | $46.31 | $9.40 | 5.3% |
| solana | 41 | $2.10 | **$7.65** | $31.77 | $17.12 | $6.84 | 0.0% |
| xrp | 38 | $2.31 | **$5.92** | $65.14 | $33.50 | $9.50 | 6.3% |
| dogecoin | 18 | $1.22 | **$9.90** | $27.41 | $13.96 | $10.61 | 0.0% |
| bnb | 53 | $2.46 | **$8.12** | $34.47 | $23.40 | $8.25 | 3.5% |
| hype | 15 | $2.87 | **$8.53** | $72.29 | $25.71 | $8.53 | 0.0% |

**Every coin, including BTC, offers a median of $6-$10 per signal.** M1 §3.3's $250-$400 live
capacity figures came from 2 closes per coin and full 5-level ladders; this is 81 days of the
statistically solid top-of-book measurement, and M1 §3.9 already flagged that the historical tape
is the number to size against. It is.

Consequence for the $/day question:

| coin | $/day @ $250 | $/day @ $25 | ratio | mean notional/fill @$250 |
|---|---|---|---|---|
| bitcoin | +6.30 | +2.10 | 3.0x | $33.3 |
| ethereum | +2.06 | +0.79 | 2.6x | $38.6 |
| solana | −3.60 | −1.34 | 2.7x | $17.5 |
| bnb | −1.81 | −0.48 | 3.8x | $23.9 |

A 10x clip buys ~3x P&L because depth binds. **Per-coin `per_event_cap_usd` calibration (M1 change
spec item 6) matters much less than it looked — the book, not the config, sets the size.** The
tail matters though: BTC's p90 is $98 and one BNB fill took $135 of a losing book, so a per-coin cap
is still worth having as a *loss* limiter.

Caveat, stated plainly: **the hourly quote tape is top-of-book only**, so these are one-level walks.
Per-share EV is unaffected (a one-level walk fills at the best ask by construction; C2 §8 caveat 2);
**$ P&L is a lower bound.** `markets.parquet` shows a `book_snapshot_5` channel exists for these
markets and was not fetched — that is the way to tighten this.

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

**`sigma_1s_floor = 8e-6` is NOT grossly wrong for the volatile coins — it binds *less* on them
(0% on SOL/XRP/DOGE/HYPE vs 7.8% on BTC), which is the correct behaviour for a floor.** DOGE's
median |z| is *lower* than BTC's, not higher. The BTC-calibrated floor and the 120 s window transfer
without distortion; the losses are not a calibration artifact.

`sigma_1s_floor` × `edge_min` grid, **TRAIN only** (`data/multicoin/m3/t6b_param_sweep_train.csv`):
raising the floor to 2e-5 changes almost nothing; 5e-5 costs BTC half its edge (+10.2¢ → +5.7¢) and
does not rescue SOL (−13.9¢ → −11.9¢) or XRP. Raising `edge_min` to 0.10 does not rescue SOL
(−22.8¢) or XRP (−6.0¢) either. **No parameter setting tested makes SOL or XRP positive on TRAIN.**

### 7.1 The one guard worth quantifying — and it does not survive walk-forward

§4.3's inversion suggests a `|z|` band. Selected on **TRAIN** (49 days), then evaluated **once** on
the untouched **TEST** (32 days):

| rule | frame | ALL 7 ¢/share | ALL 7 $/day | non-BTC $/day | BTC $/day |
|---|---|---|---|---|---|
| shipped | TRAIN | +7.7 | +8.57 | +1.32 | +7.25 |
| **1 ≤ \|z\| ≤ 5** (best on TRAIN) | TRAIN | **+14.5** | **+11.84** | +3.21 | +8.63 |
| 2 ≤ \|z\| ≤ 5 | TRAIN | +13.3 | +8.23 | +2.01 | +6.22 |
| shipped | **TEST** | +3.1 | +2.29 | **−2.55** | +4.84 |
| **1 ≤ \|z\| ≤ 5** | **TEST** | +6.5 | **+0.49** | **−2.56** | +3.05 |
| 2 ≤ \|z\| ≤ 5 | **TEST** | +14.4 | +4.68 | +2.23 (n=11) | +2.45 |

**The TRAIN-selected rule fails out of sample** ($11.84 → $0.49/day). The runner-up does better on
TEST but on 19 fills. **Report: the |z| inversion is a real, significant property of the fill
sample (§4.3, p = 0.0055); a |z| band is NOT yet a validated trading rule and must not be shipped
on this evidence.** It should be tested on the BTC tape specifically, where n is largest, before
being considered.

---

## 8. Portfolio view — correlation is not the problem, edge is

### 8.1 Running everything at once

| portfolio | fills | trades/day | ¢/share | win | t_day (¢/share) | $/day @$250 |
|---|---|---|---|---|---|---|
| ALL 7 | 176 | 2.17 | +6.25 | 79.5% | 1.49 | +6.09 |
| non-BTC | 110 | 1.36 | +2.01 | 74.5% | 0.65 | −0.21 |
| BTC+ETH+SOL+XRP | 123 | 1.52 | +6.61 | 78.9% | 2.04 | +5.00 |
| **BTC+ETH** | 85 | 1.05 | **+13.25** | 88.2% | **3.47** | **+8.36** |
| BTC only | 66 | 0.81 | +13.32 | 87.9% | 3.30 | +6.30 |

### 8.2 Do the coins mis-price at the same moments? Mostly no.

Two measurements, because the day-level P&L correlation is nearly meaningless at 0.14-0.36
fills/day per coin (most cells are structural zeros):

**(a) Daily P&L correlation** (81 days, 0 on no-trade days): BTC-ETH **+0.61**; everything else
|r| ≤ 0.22 except a spurious SOL-HYPE −0.887 driven by a handful of overlapping days. Read only the
BTC-ETH number, and read it as "the two coins that actually trade, trade together".

**(b) Late sign flips — n = 1,915 closes per coin, the high-power version.** A "flip" is
`sign(S_t − S_open)` at τ = 5 s disagreeing with the settle. This is the event that causes every
loss, and it is measurable on *every* close rather than only on fills.

| coin | bitcoin | ethereum | solana | xrp | dogecoin | **bnb** | hype |
|---|---|---|---|---|---|---|---|
| flip rate | 1.044% | 0.836% | 0.992% | 0.940% | 0.836% | **2.193%** | 0.783% |

Pairwise φ correlations of the flip events are **0.00-0.15** (BTC-XRP 0.150 is the largest). Joint
occurrence versus independence:

| coins flipping in the same hour | observed | expected if independent |
|---|---|---|
| 0 | 1,782 | 1,773.6 |
| 1 | 122 | 137.0 |
| **2** | **9** | 4.4 |
| **3** | **2** | 0.08 |
| ≥4 | 0 | 0.00 |

**M1 §3.6 worried that seven coins are one levered crypto-beta bet. On the quantity that matters —
the last-5-seconds flip — they are close to independent.** Hourly *returns* correlate 0.67-0.90
(M1), but that correlated variance is already resolved before entry, exactly as M1's mitigation
argued. There is a real but small joint tail: 3-coin hours occur 2 times where 0.08 are expected
(25x), so a sizing rule should assume 3 concurrent losers is possible, not 1.

**(c) Actual concurrency is negligible anyway.** Of 164 closes that produced any fill, only **11 had
≥2 concurrent fills** (7 all-win, 1 all-lose, 3 mixed). `max_open_notional` $1,000 never binds.
**Correlated edges are not why the expansion fails. The expansion fails because five of the six new
coins have no edge.**

---

## 9. Train / test and the BNB lesson

TRAIN = 2026-05-08 … 06-25 (49 days). TEST = 2026-06-26 … 07-27 (32 days), untouched until §7.1.
The shipped parameters were fixed by the BTC work *before* this run, so no headline number is a
tuned number; the split measures temporal stability.

| coin | TRAIN fills | TRAIN ¢/share | TRAIN t_day | TEST fills | TEST ¢/share | TEST t_day | TEST $/day |
|---|---|---|---|---|---|---|---|
| **bitcoin** | 45 | +10.2 | 2.33 | 21 | **+20.0** | 2.32 | **+4.84** |
| **ethereum** | 13 | +9.8 | 1.43 | 6 | **+20.0** | 6.42 | **+1.23** |
| hype | 9 | +26.3 | 4.82 | 4 | +24.1 | 1.80 | +1.49 |
| solana | 18 | **−13.9** | −1.45 | 4 | +14.4 | 7.83 | +0.07 |
| xrp | 10 | −1.3 | 0.34 | 6 | −17.8 | −1.35 | +0.85 |
| dogecoin | 9 | +8.7 | 0.42 | 2 | −31.1 | −0.69 | −0.05 |
| **bnb** | 17 | **+17.2** | **1.58** | 12 | **−29.6** | −1.43 | **−6.14** |

**BNB is the reason "n < 20 proves nothing" is a rule in this project.** On TRAIN it was the best
non-BTC coin by ¢/share and would have been the natural first coin to enable. On TEST it is the
worst thing in the table by a wide margin. Its 2.19% flip rate (§8.2b) — 2.1x every other coin, and
the direct confirmation of M1 §3.5 — is the structural reason: BNB has the lowest hourly volatility,
so its outcome stays undecided into the final seconds.

Only **bitcoin, ethereum and hype are positive in both halves.**

---

## 10. Stress

All seven coins, each knob alone and then stacked (`data/multicoin/m3/t11_stress.csv`):

| variant | ALL 7 fills | ALL 7 ¢/share | ALL 7 $/day | non-BTC ¢/share | non-BTC $/day | BTC ¢/share |
|---|---|---|---|---|---|---|
| **shipped** | 176 | +6.25 | +6.09 | +2.01 | −0.21 | +13.32 |
| fee 0.07 → 0.10 | 174 | +5.67 | +5.65 | +1.39 | −0.47 | +12.85 |
| depth 50% | 176 | +6.25 | +3.34 | +2.01 | +0.12 | +13.32 |
| latency 1500 → 3000 ms (band narrows to τ∈{5,4}) | 111 | +6.50 | +2.90 | −0.79 | +0.06 | +15.08 |
| book age ≤ 5 s (pre-decision) | 158 | +7.68 | +5.75 | +3.64 | −0.55 | +13.32 |
| underlying feed lag +300 ms | 173 | +3.48 | +5.67 | −1.90 | −0.38 | +13.11 |
| τ nominal (do NOT charge print staleness) | 183 | +5.92 | +6.32 | +1.60 | −0.12 | +13.23 |
| **ALL stacked** | 96 | +5.72 | +0.99 | **−3.33** | −0.26 | +14.41 |

Reading:

* **BTC is robust to every knob** (+12.9¢ to +15.1¢). **non-BTC is not**: a 300 ms feed lag alone
  takes the non-BTC pool from +2.0¢ to **−1.9¢**, i.e. the pooled non-BTC "edge" is smaller than the
  effect of one REST round trip. That is the clearest single statement of how thin it is.
* Charging the underlying print's sub-second staleness into `tau` (this harness's default) costs 7
  signals and 0.3¢ — a conservative choice that does not drive anything.
* The book-staleness filter *raises* ¢/share slightly on every frame, consistent with §5: stale
  fills are marginally bad, not good.

---

## 11. How much live data would it take to know?

Fills needed for a two-sided t = 1.96 against a *true* +10 ¢/share edge, at each coin's own measured
per-fill SD, converted to calendar days at its own fill rate; ×4 fills for day-clustering (C2 §1.3
measured t_day ≈ 0.5 × t_trade).

| coin | fills so far | SD per fill | fills/day | fills needed | **days needed (per-trade)** | **days needed (day-clustered)** | observed t_day |
|---|---|---|---|---|---|---|---|
| bitcoin | 66 | 0.285 | 0.815 | 31 | **38** | **153** | 3.30 |
| hype | 13 | 0.190 | 0.160 | 14 | 86 | 344 | 4.88 |
| ethereum | 19 | 0.333 | 0.235 | 43 | **181** | **725** | 2.30 |
| xrp | 16 | 0.335 | 0.198 | 43 | 218 | 873 | −0.63 |
| bnb | 29 | 0.462 | 0.358 | 82 | 229 | 917 | −0.37 |
| solana | 22 | 0.410 | 0.272 | 65 | 238 | 951 | −1.12 |
| dogecoin | 11 | 0.435 | 0.136 | 73 | 535 | 2,139 | −0.09 |

**No non-BTC coin can be validated by live paper trading in a useful timeframe at these fill
rates — ETH needs ~6 months of live fills for a per-trade t, ~2 years day-clustered.** This is the
strongest argument for making the decision on the 81-day historical tape (as here) and for treating
"paper it and see" as an operational check, not a statistical one. It is also an argument for
enabling ETH *cheaply* (a $25 clip) rather than waiting for significance that will not arrive.

---

## 12. Recommendation

**Ranked by expected $/day** (full sample, $250 clip): bitcoin +6.30 · ethereum +2.06 ·
hype +1.96 · dogecoin +0.93 · xrp +0.24 · bnb −1.81 · solana −3.60.

**Ranked by statistical strength** (day-clustered t / bootstrap P(EV≤0)): bitcoin (3.30 / 0.000) ·
hype (4.88 / 0.000, but n=13 and untradeable) · ethereum (2.30 / 0.042) · dogecoin (−0.09 / 0.43) ·
bnb (−0.37 / 0.57) · xrp (−0.63 / 0.79) · solana (−1.12 / 0.88).

**The two rankings agree** — which is itself informative, and different from what M1's frequency
screen implied (it ranked BNB the largest new contributor; BNB is second-worst on both axes).

| coin | verdict |
|---|---|
| **ethereum** | **ENABLE**, behind a coin allowlist, at **$25** `per_event_cap_usd`, positive in both halves, +$2.06/day, P(EV≤0)=0.042. Worth ~+33% on the BTC bot. |
| **solana** | **DO NOT ENABLE.** −8.8¢, −$3.60/day; 26% of its signals come from zero-information zero-move closes (§4.4). |
| **xrp** | **DO NOT ENABLE.** −7.5¢; 20% zero-move signals; worst fill conversion (40%). |
| **dogecoin** | **DO NOT ENABLE.** +1.5¢ with a [−32.8, +30.0] CI on 11 fills; flips sign between halves. |
| **bnb** | **DO NOT ENABLE.** −2.2¢ full sample, −29.6¢ / −$6.14/day on TEST, 2.19% flip rate. The most dangerous coin on the list because it looked good on TRAIN. |
| **hype** | **DEFER** (unchanged from M1). Not reachable by any live feed available here; 13 fills; $8.5 median capacity. |

**Implementation, if ETH is enabled** — everything in M1 §7 still applies, and two items become
*mandatory* rather than advisory:

1. **`strategy.close_snipe.allowed_coins`, defaulting to `["bitcoin"]`** (M1 §7 item 4). With five
   of six new coins measured as loss-making, widening `_HOURLY_RE` without a coin allowlist is a
   direct route to −$3.60/day.
2. **One oracle per symbol, failing closed** (M1 §7 item 3). ETH priced off BTC's underlying would
   be silent and catastrophic.
3. `POST /books` batching (M1 §7 item 1) is **not** required for two coins — 4 tokens sequential is
   ~1.6 s, which still fits the 2.5 s band but eats most of it. Batch it anyway; it is cheap and it
   lowers `tau_lo`.
4. A **$25** cap for ETH, not $250: §6 shows the median fill is $9 and the p90 is $53, so a large
   cap buys little upside and sizes the tail losses.

**The frequency problem is not solved.** BTC + ETH is 1.05 trades/day against BTC's 0.81. If more
frequency is the goal, this document says to look at the 5m/15m families across coins (M1 §1.3:
383 open 5m markets in a 30 h window against 28 hourly) rather than at more hourly coins — with the
warning that those settle on Chainlink and the corrected 5m number at shipped parameters is
+0.87¢ / t = 0.22 (C2 §6).

---

## 13. What is NOT established

1. **$ P&L is a lower bound.** The hourly quote tape is **top-of-book only**, so every fill is a
   one-level walk. Per-share EV is unaffected; total P&L and the capacity table would improve with
   the `book_snapshot_5` channel, which exists for these markets (`markets.parquet`) and was not
   fetched.
2. **n is small everywhere except BTC.** 66 BTC fills, 29 BNB, 22 SOL, 19 ETH, 16 XRP, 13 HYPE,
   11 DOGE. The *pooled* non-BTC result (110 fills) is the only non-BTC statement with real power,
   and it says "indistinguishable from zero". No per-coin verdict above should be read as more
   certain than its CI.
3. **The ETH recommendation rests on 19 fills.** Its bootstrap $/day CI is [$0.61, $4.03] and it
   excludes zero only because its two losing fills happened to be small-notional. That is luck, not
   structure. Enable it at $25 and treat the first 40 live fills as the real test.
4. **The |z| inversion is measured, not explained.** §4.3 shows *that* high-confidence fills lose;
   it does not identify what the counterparty knows. Candidate explanations (order-book-derived
   price, cross-exchange quotes, sub-tick information) are untested. Until one is confirmed, a |z|
   cap is curve-fitting.
5. **Concurrency under live load is still unmeasured** (M1 §6 item 8), though §8.2c shows only 11
   closes in 81 days had ≥2 concurrent fills, so the risk is smaller than M1 feared.
6. **HYPE's live feed remains unverified from production** (M1 §6 item 1). Nothing here changes it.
7. **81 days, one regime.** 2026-05-08 → 07-27 only. The BTC edge has now been measured on three
   different tapes and periods and lands at +9.8¢ / +13.3¢ / +21.5¢, so it is stable in sign;
   the non-BTC coins have one measurement each.

---

## 14. Files

| path | what |
|---|---|
| `scripts/multicoin/replay_hourly.py` | the harness (imports the property-tested bot primitives from `scripts/fresh5m/replay.py`) |
| `scripts/multicoin/indep_hourly.py` | independently-written second replay of the data layer; 0 discrepancies (§1.3b) |
| `scripts/multicoin/m3_report.py` | regenerates every table below |
| `data/multicoin/m3/trades_shipped.parquet` | every signal, all 7 coins, 81 days, with fair/z/book ages/fill/P&L |
| `data/multicoin/m3/t0_settle.csv` | §1.3a settle reconciliation |
| `data/multicoin/m3/t1_headline_full.csv`, `t1b_train_test.csv`, `t1c_clip.csv` | §2, §9 |
| `data/multicoin/m3/t2_staleness.csv`, `t2b_stale_fills.csv` | §5 staleness sweep |
| `data/multicoin/m3/t3_funnel.csv` | §3 |
| `data/multicoin/m3/t4b_competition_crosscoin.csv`, `t4c_ev_by_bookage.csv` | §5 |
| `data/multicoin/m3/t45_liquidity_capacity.csv`, `t5b_capacity_per_signal.csv` | §6 |
| `data/multicoin/m3/t6_param_sanity.csv`, `t6b_param_sweep_train.csv` | §7 (TRAIN only) |
| `data/multicoin/m3/t7b_daily_pnl_corr.csv`, `t7c_concurrency.csv`, `t8d_flip_corr.csv` | §8 |
| `data/multicoin/m3/t8_edge_decomposition.csv`, `t8b_degenerate.csv`, `t8c_adverse_selection.csv` | §4 |
| `data/multicoin/m3/t9_bootstrap.csv`, `t9b_win_by_z.csv`, `t12_win_vs_ask.csv`, `t13_tick.csv` | §4, §2 |
| `data/multicoin/m3/t10_power.csv` | §11 |
| `data/multicoin/m3/t11_stress.csv` | §10 |
| `data/multicoin/m3/t14_guard_train.csv`, `t14_guard_test.csv`, `t15_combos.csv` | §7.1, §0 |
| `data/multicoin/quotes/<coin>/<D>.parquet` | extended from 45 to **80 days** by this run (2026-05-08 → 07-26) |
| `data/multicoin/binance/<SYM>/<D>.parquet` | extended from 35 to **80 days** by this run |

Reproduce:

```bash
python3 scripts/multicoin/m3_report.py                       # every table
python3 scripts/multicoin/indep_hourly.py bitcoin,ethereum,solana,xrp,dogecoin,bnb,hype \
        $(python3 -c "import pandas as pd;print(','.join(pd.date_range('2026-07-01','2026-07-20').strftime('%Y-%m-%d')))")
python3 scripts/multicoin/replay_hourly.py --coins ethereum --cap-usd 25
```
