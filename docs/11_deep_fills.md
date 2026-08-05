# 11 — What Actually Makes The Money, and How Much Per Month

**Date:** 2026-08-05 · **Question:** the P&L is dominated by a handful of trades. What are they,
can we get more of them, and what is the realistic monthly number?

**Data:** `data/multicoin/m3/trades_shipped.parquet` (66 BTC fills, 79 days) + the production tape
(10 resolved fills, 9 days). Both at the shipped `per_event_cap_usd: 250`.

---

## 0. Answers up front

| question | answer |
|---|---|
| What makes the money? | **Fill size, and nothing else.** Top 10 of 66 backtest fills = **84% of P&L**. Live: 2 of 10 = **93%**. |
| What makes a fill deep? | **Only how much size is resting at the best ask.** Deep fills had $180.74 of book vs $8.65 for the rest — a 21× gap. Every other variable is identical. |
| Can we predict them? | **No.** \|z\|, tau, sigma, fair and book age are statistically indistinguishable between deep and shallow fills. |
| Can we select for them? | **No — and that is the point.** You cannot know the book is deep until the signal fires, and by then you already take everything profitable. |
| Realistic monthly P&L | **median $209, IQR $132–298, 5–95% $46–450.** Losing month ~1%. §3. |
| Biggest open lever | The whole backtest is **top-of-book only** — it never walked past level 1. Live full-ladder capacity is unmeasured and is a **floor**, not a ceiling. §4. |

---

## 1. The money is in a handful of fills

Backtest, 66 BTC fills sorted by size, $250 cap:

| top N fills | notional | % of notional | P&L | % of P&L | win rate |
|---|---|---|---|---|---|
| 1 | $250 | 11.4% | $78.96 | 15.5% | 100% |
| 2 | $467 | 21.2% | $101.51 | 19.9% | 100% |
| 5 | $956 | 43.5% | $169.17 | 33.2% | 100% |
| **10** | **$1,394** | **63.4%** | **$429.57** | **84.2%** | **100%** |
| 20 | $1,844 | 83.9% | $410.33 | 80.4% | 90% |
| 66 | $2,199 | 100% | $510.24 | 100% | 87.9% |

By notional bucket — this is the clearest cut in the whole dataset:

| notional | n | win | P&L | % of P&L | return on notional |
|---|---|---|---|---|---|
| $0–10 | 32 | 84.4% | +$35.80 | 7.0% | 25.2% |
| $10–25 | 15 | 86.7% | +$38.63 | 7.6% | 16.3% |
| $25–50 | 5 | 80.0% | **−$13.98** | −2.7% | −7.4% |
| **$50–100** | **8** | **100%** | **+$154.06** | **30.2%** | **27.2%** |
| **$100–250** | **6** | **100%** | **+$295.73** | **58.0%** | **27.8%** |

**14 fills over $50 went 14-for-14 and produced 88% of all P&L.** The other 52 fills, between them,
produced $60.

The live tape says the same thing louder: 2 fills of 10 produced $123.21 of $132.86.

**Why the big ones win more often is not a mystery and not a separate effect.** A deep book at a
mispriced level means a large resting order somebody has not repriced. The same staleness that
leaves size there leaves the price wrong. Deep fills had a median ask of **$0.765** and a median
signal edge of **0.2024**, against **$0.835** and **0.1258** for the rest — cheaper *and* more
mispriced.

---

## 2. There is nothing to select on

Top 10 fills by notional vs the other 56:

| variable | deep (n=10) | shallow (n=56) | usable? |
|---|---|---|---|
| **book depth at signal** | **$180.74** | **$8.65** | — this *is* the definition |
| median \|z\| | 2.4213 | 2.4882 | **no** |
| median tau | 5.00 | 5.00 | **no** |
| median fair | 0.9800 | 0.9800 | **no** |
| median book age | 0.165 s | 0.147 s | **no** |
| fraction fair-pinned | 0.70 | 0.55 | weak, and §9 of docs/10 is the cautionary tale |
| median ask | 0.765 | 0.835 | consequence, not predictor |
| median signal edge | 0.2024 | 0.1258 | consequence, not predictor |

**The deep fills are not a different species of signal.** They are the ordinary signal, landing on
a book that happened to have size. There is no filter, no time-of-day, no volatility regime, no |z|
band that finds them in advance. This is also exactly why the |z| gate backfired (docs/10 §9): it
tried to select on a variable that does not separate deep from shallow, and by accident removed two
of the deep ones.

**The operational consequence is the opposite of a filter: take every qualifying signal.** The
strategy earns by being present when a deep book appears. Anything that reduces participation —
a gate, a tighter band, a smaller cap — reduces the chance of being there. That is the single most
important design conclusion in this document.

---

## 3. How much per month

Bootstrapped: Poisson-distributed fill count at the pooled rate (0.864 fills/day → **26.3
fills/month**), trade P&L resampled from the observed distribution, $250 cap.

| source | mean | median | IQR | 5–95% | P(losing month) |
|---|---|---|---|---|---|
| backtest only (n=66) | $203 | $189 | $118–273 | $38–417 | 1.5% |
| live only (n=10) | $348 | $333 | $231–450 | $109–639 | 0.0% |
| **pooled (n=76)** | **$222** | **$209** | **$132–298** | **$46–450** | **1.2%** |

Naive point estimates for comparison:

| | |
|---|---|
| backtest $510.24 / 79 d | **$196/month** |
| live $132.86 / 9 d | $449/month — **93% from two trades, do not plan on this** |
| pooled $643.10 / 88 d | $222/month |

**Plan on ~$200/month. Expect months between $130 and $300. A month under $50 is a 1-in-20 event
and is not evidence that anything broke.**

Why the spread is so wide, in one line: the top 10 of 76 pooled fills are **81.5%** of all P&L, and
the mean fill ($8.46) is **4.1×** the median fill ($2.08). Most months are quiet and the number
comes from a few deep fills you cannot schedule. A month with no deep fill makes ~$50; a month with
two makes ~$400. That is the business.

**On the $250 cap:** at ~26 fills/month it binds on roughly 1 fill in 66 (backtest) — but that one
fill was 15.5% of all P&L. The cap is not costing much *on average* and is doing real work on the
tail. §5 of docs/10 stands: $250 is right, $400 captures the last 2%, above that nothing — **subject
to §4 below.**

---

## 4. The measurement that would move all of this — the backtest is top-of-book only

`scripts/multicoin/replay_hourly.py:227` reads *"BOOK TAPE (Telonex hourly quotes: **TOP OF
BOOK**, both outcomes)"*. Confirmed empirically: **`n_levels == 1` on all 66 fills.** The backtest
has never walked past the best ask, not once.

Two things follow, and the first one is a correction to docs/10:

1. **`max_walk_above_best` is untestable on this data.** I swept it from 0.01 to 0.20 and every run
   returned byte-identical results — 66 fills, $6.2993/day — because there is no level 2 to walk to.
   My docs/10 §3 classification of fills as "WALK/EDGE bound" was wrong; they were bound by the
   *single level the tape contains*.
2. **Every capacity number in docs/10 §5 is a top-of-book FLOOR, not a ceiling.** "$285.29 max
   fillable", "above $400 earns exactly zero", "book binds on 98% of trades at a $2,250 bankroll" —
   all measured with one level of book. The live bot walks the full ladder.

This also re-explains the "live fills ran 4× fatter than the depth model" flag in docs/10 §3: I was
comparing full-ladder live fills against a top-of-book backtest. The 7 subsequent live fills
averaging $9.92 against a $10.51 backtest median (docs/10 §9.4) says those were thin books where
top-of-book ≈ full ladder — consistent, and it does not tell us about the deep ones.

### 4.1 MEASURED (2026-08-05) — the deep fills are multi-level, and both bounds bind

`bot/scripts/ladder_depth.sql` on the production tape:

| trade | levels | best → last | walked | cost | P&L | what bound it |
|---|---|---|---|---|---|---|
| **07-28 4am** | **4** | 0.70 → 0.73 | **0.0300** | $250.00 | **+$95.96** | **`$250` cap AND the 3¢ walk, simultaneously** |
| 07-29 11pm | 2 | 0.83 → 0.85 | 0.0200 | $149.10 | +$27.25 | book exhausted |
| **08-05 12am** | **4** | 0.91 → 0.94 | **0.0300** | $27.78 | +$2.08 | **3¢ walk bound** |
| 07-31 5pm | 1 | 0.79 | 0.0000 | $13.67 | +$3.43 | book exhausted |
| 08-03 3am | 2 | 0.85 → 0.87 | 0.0200 | $8.60 | +$1.32 | book exhausted |
| 07-30 6am | 2 | 0.69 → 0.71 | 0.0200 | $7.00 | +$2.85 | book exhausted |
| 08-04 5am | 2 | 0.69 → 0.71 | 0.0200 | $7.00 | +$2.85 | book exhausted |
| 08-01 6am | 2 | 0.57 → 0.59 | 0.0200 | $5.80 | −$5.97 | book exhausted |
| 08-02 9am | 2 | 0.33 → 0.35 | 0.0200 | $3.40 | +$6.44 | book exhausted |
| 08-04 11am | 2 | 0.31 → 0.33 | 0.0200 | $3.20 | −$3.35 | book exhausted |

**Live walks a mean of 2.3 levels and a max of 4. The backtest walks 1, always.**

The 07-28 ladder ends at `(0.73, 9.567808219178117)` — a fractional share count, which is the
`per_event_cap_usd` truncating mid-level. So that trade was stopped by the **cap** *and* was
simultaneously sitting exactly on the **3¢ walk bound**. Both constraints binding at once, on the
trade that produced 72% of all P&L to date.

**The decisive number:** on that trade, top-of-book alone was 51.45 shares = **$36.02**, i.e.
**14% of the $250 actually filled**. On 07-29 it was 62%. The top-of-book backtest is not slightly
conservative on the deep fills — it is missing most of them.

**Therefore:** every capacity figure in docs/10 §5 is void as a ceiling. `max_walk_above_best` IS a
live lever the backtest cannot see. Whether to move it — and whether to move the cap with it — is a
separate question, because walking deeper raises the average price and therefore the break-even win
rate, and because the deepest walks happen on saturated-fair signals which are the worst-calibrated
part of the model. That analysis is in §6.

**Still true: do not relax `max_walk_above_best` on speculation.** It is 0.03 because the first live
loss (−$25.41) came from walking 10¢+ deep against a 7σ jump.

---

## 5. What this changes and what it does not

**Changes:**
- The monthly expectation is now a distribution, not a point: **$209 median, $132–298 IQR**.
- The capacity ceiling in docs/10 §5 is downgraded from "measured" to "**top-of-book floor**".
- `max_walk_above_best` is promoted from a settled guard to an **open, measurable question**.

**Does not change:**
- The funding verdict. Still 10 of 30 resolved trades, still `per_event_cap_usd: 25` for the first
  live clip, still the wallet/redemption path to build, still the $4 diagnostic order.
- The design conclusion. **Take every qualifying signal.** The edge is participation, not selection.
- Compounding (docs/10 §6). Still book-limited, still pointless above ~$2,250 of bankroll — and if
  §4 resolves toward deeper live books, that ceiling moves up but the shape does not.
