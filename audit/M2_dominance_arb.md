# M2 — Cross-family dominance arbitrage (5m × 15m BTC Up/Down)

**Date:** 2026-07-27 · **Scope:** establish whether the 5m/15m shared-close dominance violation is
tradeable under the C2 fill discipline (two-book fills, staleness, causality, day-clustered stats,
train/test, drop-best-days).

---

## VERDICT — NOT TRADEABLE. Ship nothing.

The structure is **real and exact** (0 dominance failures in 9,677 shared closes). The violations are
**frequent** (5.7–16.6 actionable/day, ~9× close_snipe's rate). The arbitrage, *when both legs fill*,
is **large and never loses** (+27.8 ¢/pair-share, 100 % win, worst case +1.03 ¢).

It is still untradeable, for one reason:

> **A dominance crossing has a median life of 0.489 seconds. At the bot's 1,500 ms round trip only
> 12.8 % of detections still fill both legs at the detected price, and 53 % of attempts fill exactly
> one leg — a naked directional position on a coin the strategy deliberately does not predict.**

After paying for that leg risk the best defensible configuration earns **+$0.43/day at a $25 clip
(t_day = 0.34, 95 % CI [−1.87, +3.09])** over 43 days, is **negative on the pre-committed 14-day
training window (−$2.14/day, t_day = −2.02)**, is **negative on 31 of 43 days**, and its single best
day is **162 % of its entire P&L**. Removing the best 3 days leaves **−$1.37/day**. It gets **worse**
as the clip grows: −$2.27/day at $100, −$17.39/day at $250.

The prior measurement (+11.4 ¢/pair train, +17.8 ¢ OOS, 304 opportunities, median violation life
"~3 seconds") does not reproduce. Three of its inputs are wrong, and each one inflates the result:

| prior claim | measured here |
|---|---|
| median violation life ~3 s | **0.489 s** (p90 = 2.20 s; only 6.3 % last ≥ 3 s) |
| "fill BOTH legs at the next second" | a one-second, both-legs-guaranteed fill is a **one-book fill in disguise** — it assumes away the 53–58 % single-leg rate that is the entire problem |
| train +11.4 ¢ → OOS +17.8 ¢ | **train is negative** on every rule tested (−$2.14 to −$6.29/day); only the test window is positive |

Leg risk does not merely reduce the edge. **It is larger than the edge.** Matched-pair P&L over the
43 days is +$407 (at $25 clips); the naked residual costs −$47 if unwound immediately and swings
±$17 per event if held. The arbitrage exists; the ability to capture both halves of it does not.

Everything below is the evidence.

---

## 1. The structure is exact — and the free option inside it is bigger than the arb

The last 5 m window of each 15 m window shares one Chainlink settle print `C`. With
`O5` = 5 m strike (first Chainlink observation at/after `wts15+600`), `O15` = 15 m strike (at/after
`wts15`), write `O_lo = min(O5,O15)`, `O_hi = max(O5,O15)` and buy

```
1 × Up   on the LOW-strike market
1 × Down on the HIGH-strike market
```

| close lands | Up(lo) | Down(hi) | payoff |
|---|---|---|---|
| `C < O_lo` | 0 | 1 | **$1** |
| `O_lo ≤ C < O_hi` | 1 | 1 | **$2** |
| `C ≥ O_hi` | 1 | 0 | **$1** |

So the combo's fair value is `1 + P(O_lo ≤ C < O_hi) ≥ 1`, and **any cost below $1 is a hard
arbitrage, not a bet.** Because the vault reconstructs the Down ask as `1 − bid_Up`, the entry
condition is literally a crossed cross-market quote:

```
cost = ask_UpLow + (1 − bid_UpHigh) < 1   ⇔   ask_UpLow < bid_UpHigh
```

### 1.1 Structural check: 9,677 shared closes, zero failures

`scripts/m2/check_dominance.py` — every shared close on every day with Chainlink coverage
(2026-04-02 … 2026-07-15, 105 days), strikes from `crypto_prices`, winners from
`windows_all.result_id` (the vendor's settlement, independent of prices):

```
shared closes checked      9,677     payoff == $1   6,449
payoff == 0 (broken)           0     payoff == $2   3,228  (33.36%)
identical strikes O5==O15      0
Chainlink-derived winner vs vendor result_id:  5m 99.979%   15m 99.979%
Chainlink 'between' vs payoff==2:              99.959%
|O5 − O15| (the 10-minute BTC move): median $57.45, p75 $108, p90 $185, p99 $447
```

**Zero counterexamples.** The dominance identity holds on every observed shared close, and my
independently derived strikes reproduce the vendor's settlement on 99.98 % of markets. The idea is
sound; only execution is in question.

### 1.2 The $2 bonus is a lottery, not part of the arbitrage — keep them separate

`P(payoff = $2) = 33.4 %` unconditionally, so a pair bought at exactly $1 has EV $1.33. That is
tempting and it is **not arbitrage**: it is a long-volatility bet that the close lands between two
strikes, and pricing it requires predicting the coin — the very thing this idea was supposed to
avoid. Conditional on a violation actually firing, `P(payoff = $2)` collapses to **16–21 %**, because
violations concentrate where the two markets have nearly the same fair value.

I therefore report the arbitrage separately from the bonus throughout. For the best defensible rule:

```
both-leg fills, n=31:  EV +27.78 ¢/pair-share overall
                       EV +11.69 ¢/pair-share on the 26 pairs that paid only $1  (worst +1.03 ¢)
                       payoff=$2 on 5 of 31 (16.1 %) — this is where the other 16 ¢ comes from
```

**Any configuration below whose `pair_win%` is under 100 % is no longer an arbitrage** — it is paying
≥ $1 for a combo that may only return $1, and living off the bonus. Flagged where it happens.

---

## 2. Data integrity — three checks, one of which changes the answer by 69 %

### 2.1 The Down leg is reconstructed, so I priced the reconstruction error

The repo vault stores only the Up token's book; `ask_Down = 1 − bid_Up`. Verified against the **real
25-level Down-token books** in `data/fresh5m/books/` (read-only, another workflow's directory) on
2026-06-03, 07-15, 07-25 — 2,588,906 snapshot pairs matched within 10 ms inside the same window:

```
median error            0.000000        mean error   −0.000284  (−0.03 ¢, i.e. reconstruction is
p99                    ±0.000000                                 marginally OPTIMISTIC)
recon cheaper by ≥0.5 ¢   1.0 %         recon dearer by ≥0.5 ¢   0.4 %
p99.9                  −0.080 (−8 ¢)
size(ask_Down) == size(bid_Up)          exact on 95.4–96.8 %
```

The identity is essentially exact (Polymarket's CLOB mirrors the two token views), but the 1 % tail
is real vendor desync and it is optimistic in the direction that manufactures signals. **Priced as a
stress knob** (`dn_haircut`, charged at both signal and fill): a 0.5 ¢ haircut is carried in every
"final" configuration below. Its cost: −2.5 ¢/attempt at the flat gate (R4).

### 2.2 Self-crossed books are a vendor defect — and they carried 69 % of the P&L

A book quoting `ask ≤ bid` cannot exist on a matching engine. In the vault they are rare:

```
5m  bookcurves : 5,401,866 two-sided rows → 54 self-crossed (0.001 %),  spread >5 ¢ on  2.61 %
15m bookcurves : 1,284,091 two-sided rows → 124 self-crossed (0.010 %), spread >5 ¢ on 51.05 %
5m  quotes tape: 1.178 % self-crossed          fresh5m 25-level books: 0.755 %
```

But they are **wildly over-represented among dominance signals**, because a book whose two sides
disagree with each other will trivially disagree with another market's book:

```
identical rule, only difference = whether self-crossed books are allowed
  allowed   : 614 signals (14.28/day)   total P&L +$161.35
  rejected  : 539 signals (12.53/day)   total P&L  +$50.47
  → 12.2 % of signals carried 68.7 % of the P&L, and they are fiction.
```

**`require_uncrossed=True` is the default in everything reported below.** Anyone measuring this idea
without that filter will overstate it by ~3×. (Worked example, 2026-04-25 wts15=1777152600: the 15 m
book shows bid 0.110 / ask 0.010 and the 5 m book bid 0.490 / ask 0.080 — both internally crossed,
both "profitable", both meaningless.)

Widening the sanity filter to a maximum touch spread pushes the result negative (R1): at `max_spread
≤ 10 ¢` the flat result is **−$1.28/day**. This matters because the 15 m book is wider than 5 ¢ half
the time — a large share of the remaining "crossings" are one tight book against one vacuum.

### 2.3 The 15 m book/window/strike alignment is validated, not assumed

The whole analysis depends on `wts` meaning "window open second" for the 15 m family, and on the
strike being the first Chainlink observation at/after it. Decisive test — at a fixed time-to-close,
does the market's own mid agree with `price_now ≥ strike`?

| family | τ | mean mid \| price>strike | mean mid \| price<strike | P(mid>0.5 == Up won) |
|---|---|---|---|---|
| 5m | 120 s | 0.739 | 0.249 | 78.9 % |
| 5m | 30 s | 0.793 | 0.181 | 87.0 % |
| **15m** | **120 s** | **0.867** | **0.117** | **89.6 %** |
| **15m** | **30 s** | **0.861** | **0.076** | **93.9 %** |
| 15m | 3 s | 0.634 | 0.439 | 59.2 % |

The mapping is correct. Note the collapse at τ = 3 s in **both** families: in the last seconds the
books thin out, only 120 of 288 windows even have a two-sided quote, and the mid stops meaning
anything. That is a caution about the late-τ signals, not about the alignment.

### 2.4 Causality and staleness

* `O15` is fixed 10 minutes before the decision window opens. `O5` is fixed at the window open but is
  only **knowable** once its Chainlink report is published (`server_timestamp_us`, ~1.1 s later);
  every decision grid starts at `max(wts5, pub5)`. Decisions before publication are dropped.
* Every book read carries an age. Books at a crossing are **fresh** — median age 0.000 s, p90 0.246 s,
  p99 0.34 s — so unlike the 5 m close_snipe result (C2 §3), staleness is *not* what is generating
  these signals. The staleness sweep confirms it is worth almost nothing here: 1 s / 5 s / 30 s / ∞
  give −$3.48 / −$2.88 / −$2.57 / −$2.44 per day (R4). Kept at 5 s anyway.
* The tape clock is switchable between the exchange timestamp and the capture host's
  `local_timestamp_us` (median +9 ms, p99 +129 ms). Using the honest local clock changes the flat
  result from −$2.88 to −$0.74/day; it is used in the "final" configuration.

---

## 3. The opportunity set — frequent, and gone in half a second

`scripts/m2/census.py`, 43 days with 5 m books + 15 m books + Chainlink
(2026-04-02 … 05-12 and 07-06 … 07-07), 4,036 shared closes. Every maximal contiguous run of
`ask_UpLow < bid_UpHigh` on the event grid of both books, inside the legal decision window:

```
6,836 crossings  =  159/day raw    |  19.7 % of shared closes contain at least one
time-weighted: the floor is crossed 0.628 % of the available time (1.86 s out of 297 s per close)

LIFETIME OF A CROSSING            gap at start (¢)      book age at start (s)
  p10   0.020 s                     median   1.0          median  0.000
  p25   0.248 s                     p75      3.0          p90     0.246
  med   0.489 s                     p90      8.0          p99     0.336
  p75   0.998 s                     p99     63.7
  p90   2.201 s
  p95   3.497 s     P(life ≥ 0.25 s) 72.0 %   P(≥ 0.5 s) 45.5 %
  p99   8.499 s     P(life ≥ 1.0 s)  24.4 %   P(≥ 1.5 s) 15.6 %   P(≥ 3.0 s) 6.3 %
```

**Median 0.489 s, not 3 s.** And the lifetime is *independent of how big the crossing is* — a 10 ¢
dislocation dies as fast as a 1 ¢ one:

| gap at start | n | median life | still crossed 1.5 s later |
|---|---|---|---|
| < 1 ¢ | 2,761 | 0.482 s | 29.4 % |
| 1–2 ¢ | 1,674 | 0.490 s | 32.3 % |
| 2–3.5 ¢ | 965 | 0.473 s | 29.6 % |
| 3.5–5 ¢ | 405 | 0.496 s | 33.6 % |
| > 5 ¢ | 1,031 | 0.498 s | 37.7 % |

So there is no "wait for a big one" escape hatch: raising the gate reduces frequency without buying
any extra time. This is what makes the whole thing a pure latency race.

Under the actual trading rule (one entry per shared close, first qualifying instant, book-sanity and
freshness enforced) the actionable rate is **5.7–16.6 signals/day** depending on the gate — versus
close_snipe's 0.9 trades/day. Frequency was never the problem here.

---

## 4. THE KILLER NUMBER — 12.8 % of detections fill both legs

Both legs are marketable-limit (IOC) takers sent at the detection instant `t`, landing at
`t + latency`, each filling **only against its own book re-fetched at `t + latency`**, each only if
its own limit (the price that was detected, `slack_frac=0`) is still available. Partial fills are
allowed and carried.

**Fee-aware gate + 1 ¢, book-sanity, persistence 500 ms, 0.5 ¢ Down haircut, local clock — the
"final" rule. Everything else identical, only latency varies:**

| latency | signals/day | attempts | both legs | one leg | none | **survival** | leg-risk rate | EV/pair | $/day @$25 | t_day | −best3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **0 ms** | 5.79 | 249 | 249 | 0 | 0 | **100.0 %** | 0.0 % | +18.4 ¢ | **+20.93** | 4.25 | +13.98 |
| **250 ms** | 5.79 | 248 | 118 | 106 | 24 | **47.6 %** | 42.7 % | +23.3 ¢ | +11.15 | 2.87 | +5.47 |
| **500 ms** | 5.79 | 248 | 71 | 125 | 52 | **28.6 %** | 50.4 % | +18.8 ¢ | +3.13 | 1.24 | −0.51 |
| **1000 ms** | 5.72 | 245 | 46 | 118 | 81 | **18.8 %** | 48.2 % | +25.2 ¢ | +2.75 | 1.61 | +0.52 |
| **1500 ms** (shipped) | 5.67 | 243 | 31 | 129 | 83 | **12.8 %** | 53.1 % | +27.8 ¢ | **+0.43** | 0.34 | −1.37 |

Same shape on every other rule. At the plain fee gate: 100 % → 41.0 % → 20.0 % → 8.2 % → 6.3 %.
At the flat 0.5 ¢ gate: 100 % → 48.8 % → 27.8 % → 13.6 % → 9.7 %.

**Break-even latency is between 500 ms and 1 s.** The bot's shipped `latency_ms` is 1,500 ms.

Two secondary diagnostics, both consistent:

```
gap at signal → gap 1.5 s later:   +6.80 ¢ → −1.68 ¢ (mean)
still crossed at fill:              30.0 %      (but only 17.7 % still at or better than the
                                                 detected prices — a crossing can survive while
                                                 both quotes move against you)
median violation life from signal:  0.49 s
```

The gap between "still crossed" (30 %) and "both legs fill at my price" (12.8 %) is the difference
between seeing an arbitrage and getting it.

---

## 5. LEG RISK — bigger than the edge, and no policy fixes it

53–58 % of attempts fill **exactly one leg**. That leaves a naked long in a binary that settles on a
coin move — precisely the exposure this strategy was supposed to be free of. Three policies, all
tested on the same fills ("final", $25 clip, 129 single-leg events):

| policy | mean | median | p05 | p95 | min | max | win % |
|---|---|---|---|---|---|---|---|
| **HOLD to settlement** | −$0.10 | −$1.60 | −$16.61 | +$17.31 | −$23.25 | +$23.93 | 40.3 % |
| **ABORT** (cross back at `t+2×latency`) | **−$1.02** | −$0.99 | −$3.72 | +$1.29 | −$8.52 | +$12.05 | 11.6 % |
| **COMPLETE** (market the missing leg) | +$1.26 | −$0.28 | −$3.61 | +$21.63 | −$10.45 | +$28.08 | 41.1 % |

* **HOLD is a coin flip.** The naked leg wins 40.3 % of the time; the mean is ~0 and the spread is
  ±$17 on a $25 clip. That is not an arbitrage book, that is a punt with extra steps.
* **ABORT is the only policy consistent with the thesis, and it costs −$1.02 per event.** It is
  executable 96.9 % of the time, at a median unwind slip of −1.5 ¢ (p05 −10.4 ¢). This is the honest
  number and it is what every headline in this document uses.
* **COMPLETE looks best (+$1.26) and is the most misleading.** Completing at market means paying
  whatever the book asks, so the pair can cost more than $1 — its positive mean comes from the
  `payoff = $2` lottery (median is *negative*, p95 +$21.6). It is a volatility bet wearing an
  arbitrage's clothes. Ruled out on principle, not on P&L.

**Decomposition over the whole sample ("final", $25 clip, 243 attempts):**

```
matched-pair P&L        +$407.00   (+$1.272/attempt)   <- the arbitrage
residual, ABORT          −$46.94   (−$0.147/attempt)   <- the cost of missing a leg
residual, HOLD          +$193.60   (+$0.605/attempt)   <- ±$17 tail, ~zero mean, not an arb
```

At the plain fee gate the residual is more punishing than the pairs are rewarding:
matched pairs +$328 (+$0.534/att) against an abort cost of −$167 (−$0.272/att) on 539 attempts.

### 5.1 The one thing that raises survival also destroys the arbitrage

Giving each leg slack (a limit worse than the detected quote) does raise the both-legs rate —
12.8 % → 36.2 % at `slack = 50 %` of the gap — and produces the best-looking P&L in the whole study
(+$4.79/day, t_day 1.91). **But `pair_win%` drops from 100 % to 88.6 %**: 11 % of those "arbitrage"
pairs cost more than $1 and returned $1. It is no longer a floor violation, it is the P(between)
lottery again, and its train window is −$2.76/day. Rejected.

The `market=True` variant (no limits at all, always fill) reaches 93.8 % survival and +$17.6/day —
and `pair_win%` falls to 24.4 %, `EV/pair` to +7.3 ¢, worst attempt −$24.77. That is the naive model
the C2 discipline exists to prevent; it is included only to price the mistake.

---

## 6. Depth — not the binding constraint at $25, and it inverts above it

Measured at the **fill instant** on the re-fetched books, size-unbounded, within a 3 ¢ walk:

| | p05 | p25 | median | p75 | p95 |
|---|---|---|---|---|---|
| pair-shares at the touch | 4.3 | 10.0 | **30.8** | 91.9 | 334.8 |
| pair-shares within 3 ¢ | 6.8 | 55.5 | **203.0** | 373.8 | 1,345.3 |
| pair $ within 3 ¢ | $5.2 | $55.0 | **$196.3** | $364.1 | $1,315.8 |

A median opportunity supports ~$196 of paired notional inside a 3 ¢ walk, but only ~$31 **at the
touch** — and a strict at-the-quote IOC only gets the touch. So $25 clips are comfortably inside
depth; $100 and $250 are not, and the resulting imbalance turns straight into leg risk:

| clip | $/day (abort) | t_day | −best1 | −best3 | worst day |
|---|---|---|---|---|---|
| **$25** | **+$0.43** | 0.34 | −$0.28 | −$1.37 | −$11.86 |
| **$100** | **−$2.27** | −0.49 | −$4.65 | −$9.05 | — |
| **$250** | **−$17.39** | −2.25 | −$20.99 | −$27.25 | — |

**Scaling makes it worse, monotonically.** Every extra dollar of clip buys more unmatched residual,
not more matched pairs. The strategy has negative capacity beyond ~$25/opportunity — the opposite of
what a portfolio needs. Halving or quartering available depth (`depth_fraction` 0.5 / 0.25, a proxy
for racing other takers) moves the flat rule from −$2.88 to −$3.71 and −$4.89/day.

---

## 7. Results, in full, under the required reporting rules

All at $25 clips, 1,500 ms latency, per-window fee from `windows_all` (0.072 in April, 0.0704 in May,
0.070 in July — i.e. `0.07·p·(1−p)` per share per leg, up to 3.6 ¢/pair at p≈0.5), abort policy,
book-sanity on, freshness ≤ 5 s.

### 7.1 The candidate rules

| rule | sig/day | attempts | both | single | none | surv % | EV/pair | pair win | $/day | **t_day** | −best1 | −best3 | worst attempt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| flat gate > 0 | 16.63 | 715 | 70 | 455 | 190 | 9.8 % | +19.7 ¢ | 52.9 % | −$2.88 | −0.85 | −$4.85 | −$8.37 | −$17.65 |
| flat gate > 3.5 ¢ | 12.05 | 518 | 31 | 305 | 182 | 6.0 % | +46.4 ¢ | 100 % | +$1.57 | 0.45 | −$0.54 | −$4.29 | −$17.65 |
| flat gate > 8 ¢ | 7.56 | 325 | 23 | 169 | 133 | 7.1 % | +59.7 ¢ | 100 % | +$3.46 | 0.96 | +$1.21 | −$2.45 | −$15.19 |
| fee gate + 1 ¢ | 12.54 | 539 | 34 | 314 | 191 | 6.3 % | +42.7 ¢ | 100 % | +$1.17 | 0.33 | −$0.99 | −$4.86 | −$17.65 |
| **"final"** (fee+1 ¢, spread ≤ 10 ¢, persist 500 ms, 0.5 ¢ haircut, local clock) | 5.67 | 243 | 31 | 129 | 83 | 12.8 % | +27.8 ¢ | 100 % | **+$0.43** | **0.34** | −$0.28 | −$1.37 | −$8.52 |
| *fee+1 ¢, slack 50 %* ✗ | 12.54 | 539 | 98 | 365 | 76 | 18.2 % | +31.4 ¢ | **86.7 %** | +$6.44 | 1.34 | +$2.87 | −$0.71 | −$17.65 |
| *"final", slack 50 %* ✗ | 5.67 | 243 | 88 | 134 | 21 | 36.2 % | +19.6 ¢ | **88.6 %** | +$4.79 | 1.91 | +$3.57 | +$1.21 | −$9.27 |

✗ = not an arbitrage (`pair_win% < 100 %`); shown for completeness only.

**A fee-aware gate is the economically correct one** and it is worth stating: the taker fee is
`0.07·p·(1−p)` per leg, so the required crossing is 3.6 ¢ at p≈0.5 but only 0.6 ¢ at p≈0.1/0.9. A
flat 3.5 ¢ gate throws away every cheap tail opportunity; requiring
`gap > fee(ask_lo) + fee(1−bid_hi) + 1 ¢` recovers them and lifts `pair_win%` to 100 % at 12.5
signals/day instead of 12.0. It does not rescue the strategy, but it is the rule to use if anyone
ever revisits this.

### 7.2 Day-clustered statistics with bootstrap CIs

Day is the cluster unit — 20,000 bootstrap resamples of the 43 (or 29) daily P&L totals:

```
                                    $/day     95 % CI            t_day   −best1   −best3   worst day
final            ALL 43 days       +$0.43   [ −1.87, +3.09]      +0.34   −$0.28   −$1.37    −$11.86
final            TRAIN 14 days     −$2.14   [ −4.15, −0.12]      −2.02   −$2.87   −$3.37     −$9.25
final            TEST  29 days     +$1.67   [ −1.58, +5.35]      +0.93   +$0.66   −$0.96    −$11.86

fee gate +1 ¢    ALL 43 days       +$1.17   [ −4.93, +8.84]      +0.33   −$0.99   −$4.86    −$22.10
fee gate +1 ¢    TRAIN 14 days     −$6.12   [−10.60, −1.28]      −2.47   −$7.71   −$9.49    −$22.10
fee gate +1 ¢    TEST  29 days     +$4.70   [ −4.01,+15.53]      +0.92   +$1.58   −$4.17    −$19.07

flat gate >8 ¢   ALL 43 days       +$3.46   [ −2.73,+11.21]      +0.96   +$1.21   −$2.45    −$24.98
flat gate >8 ¢   TRAIN 14 days     −$6.29   [−11.96, −0.73]      −2.11   −$8.11   −$9.56    −$24.98
flat gate >8 ¢   TEST  29 days     +$8.16   [ −0.27,+18.85]      +1.64   +$4.96   −$0.38     −$8.05
```

**Not one arbitrage-preserving configuration reaches t_day = 2 on the full sample.** Every one of
them is significantly *negative* on the pre-committed training window.

### 7.3 Train / test discipline

Train = 2026-04-02 … 04-15 (14 days). Test = 2026-04-16 … 05-12 + 07-06 … 07-07 (29 days), which
reproduces the prior study's "29 days" exactly. Nothing was tuned on the test window.

| rule | TRAIN $/day | TRAIN t_day | TRAIN both-leg fills | TEST $/day | TEST t_day | TEST both-leg fills |
|---|---|---|---|---|---|---|
| flat > 3.5 ¢ | −$6.13 | −2.76 | **2** | +$5.28 | 1.06 | 29 |
| flat > 8 ¢ | −$6.29 | −2.11 | **1** | +$8.16 | 1.64 | 22 |
| fee gate + 1 ¢ | −$6.12 | −2.47 | **4** | +$4.70 | 0.92 | 30 |
| **final** | −$2.14 | −2.02 | **2** | +$1.67 | 0.93 | 29 |

Two readings, and both are disqualifying:

1. **The training window loses money on every rule.** Had this been evaluated in the intended order
   — tune on old data, deploy, report on new — it would never have been deployed.
2. **Train has 1–4 both-leg fills in 14 days.** `n < 20 proves nothing`, and n = 2 proves less than
   nothing. Even the 43-day totals only reach 31 fills. The "positive test window" rests on 29 fills
   spread over 29 days.

### 7.4 Concentration — the P&L is 3 days

```
final            : 10 positive days, 2 flat, 31 NEGATIVE.  total +$18.55 over 43 days.
                   best single day = 162 % of total P&L.  best 3 days = 395 % of total.
fee gate + 1 ¢   :  8 positive days, 35 NEGATIVE.          total +$50.47.
                   best single day = 182 % of total.       best 3 days = 485 % of total.
                   top 3 days are 2026-05-01, 05-02, 05-03 — one consecutive regime.
```

A strategy whose entire lifetime P&L is one 3-day episode, in a 43-day sample, with a negative median
day, is not a strategy. It is a description of 2026-05-01 through 05-03.

### 7.5 $/day at the requested clips

| clip | fee gate + 1 ¢ | **final** | final, TEST-29 only |
|---|---|---|---|
| **$25** | +$1.17 (t 0.33) | **+$0.43 (t 0.34)** | +$1.67 (t 0.93) |
| **$100** | −$2.53 (t −0.21) | **−$2.27 (t −0.49)** | — |
| **$250** | −$10.52 (t −0.40) | **−$17.39 (t −2.25)** | — |

For scale: live close_snipe paper trading returns ~$4.8/day on 0.9 trades/day at $250 clips. This
strategy at its best defensible setting returns $0.43/day at $25 and **loses** money at $250.

### 7.6 Time-to-close split

| bucket | sig/day | surv % | EV/pair | $/day | t_day | −best3 |
|---|---|---|---|---|---|---|
| all τ | 12.54 | 6.3 % | +42.7 ¢ | +$1.17 | 0.33 | −$4.86 |
| **τ ≤ 30 s** | 6.93 | 7.7 % | +54.5 ¢ | +$5.59 | 1.39 | −$0.89 |
| τ > 30 s | 8.42 | 4.7 % | +21.9 ¢ | −$4.38 | −6.45 | −$5.13 |

All of the (weak) signal is in the last 30 seconds — exactly where §2.3 shows both books thin out and
the mid stops being informative. That is not reassuring; it says the residual edge lives in the least
reliable part of the tape.

---

## 8. If someone insists on trying anyway — the concrete logic

I do not recommend running this. If it is run, this is the only defensible form, and it should be run
**paper-only against the live feed to measure real fill rates**, because the whole question is a
latency race that a historical tape can only bound.

```
ENTRY  (evaluate on every book update of either market, one entry per shared close)
  1. Both markets present, both books TWO-SIDED and UNCROSSED (ask > bid), both touch
     spreads <= 10c, both book ages <= 5s.
  2. O5 published (Chainlink server_timestamp <= now). Determine low-strike market.
  3. gap = bid_UpHigh - ask_UpLow - 0.005          (0.5c Down-reconstruction haircut)
     need = 0.07*ask_UpLow*(1-ask_UpLow)
          + 0.07*dn*(1-dn)  where dn = 1 - bid_UpHigh + 0.005
          + 0.01                                    (1c margin)
     require gap > need.
  4. PERSISTENCE: the crossing must have been continuously present for >= 500 ms.
  5. Order must land >= 500 ms before the close.

EXECUTE
  6. Send BOTH legs simultaneously as IOC marketable limits at EXACTLY the detected prices
     (no slack - slack converts the arbitrage into a volatility bet, see S5.1).
     Size: min(25 shares, top-of-book size on both legs). DO NOT scale up - capacity is negative.

ABORT  (this is not optional)
  7. If exactly one leg fills, immediately cross back out of the residual with an IOC.
     Executable 96.9 % of the time at a median -1.5c slip. Expected cost -$1.02/event.
     NEVER hold the naked leg (+-$17 on a $25 clip, 40 % win rate) and NEVER "complete"
     at market (turns the position into a P(between) lottery).
  8. Hard stop: if the day's abort count exceeds 15, stop for the day.

EXPECTED, on 43 days of tape: 5.7 signals/day, 0.72 both-leg fills/day, +27.8c per matched
pair-share, and +$0.43/day net of leg risk with a 95 % CI spanning zero.
```

**What would actually change the answer.** The latency frontier (§4) is unambiguous: this is
tradeable only below ~500 ms round trip, and comfortably profitable only near zero. Concretely,
+$20.93/day at $25 clips (t_day 4.25, 100 % survival, no leg risk at all) if orders could land
instantly. So the only interventions worth anything are:

1. **Cut round-trip latency below 500 ms.** Co-location, a persistent authenticated CLOB session with
   pre-warmed nonces, order construction done *before* the signal, and a websocket book feed rather
   than REST. `audit/01_constraints.md` already flags the 1,500 ms figure as costing 36 % of
   close_snipe signals; here it costs 87 % of them.
2. **Become a maker on one leg.** Resting an order at the dominance-implied bound turns the race into
   queue priority and removes one taker fee. Completely different problem; not analysed here.
3. **Nothing else.** Gate thresholds, fee-aware gating, persistence, Down-leg haircuts, staleness
   thresholds, clock convention, order type, clip size, depth fraction and τ windows were all swept
   — 59 configurations over the same 4,036 shared closes — and **none of them moves t_day past 2
   while keeping `pair_win% = 100 %`.**

**Extension that does not help.** The 5 m, 15 m, 1 h and 4 h families all share the top-of-hour close,
so the same lattice exists for 5m×1h, 15m×1h, 5m×4h, etc. (`daily/1h/quotes` covers 275 days,
`daily/4h/bookcurves` 206 days.) That multiplies the *opportunity count* several-fold. It does
nothing to the survival rate, the leg-risk rate, or the negative capacity — which are what kill it.
Frequency was never this idea's problem.

---

## 9. What was built, and where the numbers live

| file | what |
|---|---|
| `scripts/m2/check_dominance.py` | structural check, 9,677 shared closes, 0 failures |
| `scripts/m2/dom.py` | the replay: tapes, ladder walk, two-book fills, leg-risk policies, all stress knobs |
| `scripts/m2/census.py` | every crossing, its lifetime and its survival, independent of any trading rule |
| `scripts/m2/run_m2.py` | 59-configuration driver, one pass over the 43 days |
| `scripts/m2/analyse.py`, `final.py`, `report.py` | the tables above |
| `data/multicoin/m2/dominance_structural.parquet` | 9,677 shared closes with strikes, outcomes, payoffs |
| `data/multicoin/m2/census_runs.parquet` | 6,836 crossings |
| `data/multicoin/m2/census_ticks.parquet` | per-window time-weighted crossing census |
| `data/multicoin/m2/runs/*.parquet` | one record per shared close per configuration (59 files) |
| `logs/m2_run3.log`, `logs/m2_report.log`, `logs/m2_census.log` | raw run output |

`dom.py` imports `fee_per_share`, `ChainlinkFeed` and `load_chainlink` from the validated C2 harness
(`scripts/fresh5m/replay.py`) rather than re-deriving them, so the fee model, the strike rule and the
causality rule are the same code that reproduces the published control numbers to 4 significant
figures. `data/fresh5m/` was read only, never written.

**Arithmetic audit.** `pnl_per_pair` recomputed from first principles
(`payoff − px_a − fee(px_a) − px_b − fee(px_b)`) on every both-leg fill: max absolute error
2.2 × 10⁻¹⁶. Limit discipline verified: `px_a ≤ ask_detected` and `px_b ≤ 1 − bid_detected` on 100 %
of fills; `gross cost < $1` on 100 % of fills; `payoff ≥ $1` on 100 % of signals.

---

## 10. Sample-size honesty

The load-bearing statistics rest on small numbers and I am not going to dress them up:

* **43 days** of overlapping 5 m + 15 m + Chainlink coverage. That is the entire universe available
  in the vault; `crypto_prices` starts 2026-04-02 and 5 m `bookcurves` end 2026-07-07.
* **31 both-leg fills** for the "final" rule across those 43 days — spread over 15 days. The train
  window contains **2**.
* Consequently the +27.8 ¢/pair-share figure has a day-clustered t of 3.58 on 15 day-clusters, and
  the $/day figure has a t of 0.34 on 43. The first says the arbitrage is real when you get it; the
  second says you do not get it often enough to matter.
* Only 5 of the 31 matched pairs paid $2. The `P(between)` estimate conditional on a violation
  (16.1 %) is therefore based on 5 events and should not be relied on for anything.

**Bottom line: the mechanism is proven, the execution is not, and the gap between them is 1.5 seconds
wide.**
