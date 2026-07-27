# 07 — Scale Audit: can this thing make real money?

**Date:** 2026-07-27 · **Mode:** PAPER throughout, no real money has ever been at risk.
**Inputs:** `audit/A1_chainlink.md`, `A2_replay.md`, `A3_5m_revalidation.md`, `A4_change_spec.md`,
`B1_impl_strategy.md`, `B2_impl_chainlink.md`, `docs/06_live_audit.md`, plus two adversarial
verification passes (oracle/edge, and code/implementation) that re-derived the key numbers from raw
data with independently written scripts.

---

## 0. TL;DR

| question | answer |
|---|---|
| Did the live bot behave correctly? | **Yes.** Independent shadow-replay reproduces it. No logic bug, no throttle, ~100% close coverage. |
| Is the Chainlink oracle solved? | **Yes, completely, and it is free.** Proven resolution source, live public feed wired and running, ~1.4 s lag, $0/month. |
| Can we trade 5m/15m now? | **Technically yes, evidentially no.** The oracle blocker is gone; the *edge* at the shipped parameters is not proven on fresh data. |
| Is the 5m edge real after the attacks? | **Not on the fresh 5-day sample.** +7.4¢ → **+4.7¢ (t=1.13)** after removing vendor-outage fills → **+0.8¢ (t=0.15)** under fee/latency/depth stress. The 43-day older sample still holds (+8.4¢, t=7.6). Unresolved. |
| Why is the P&L small? | Three small numbers multiplied. Two were self-imposed (cap, window); one is **structural to the 1h family** and cannot be fixed by tuning. |
| Can the strategy scale? | **Only by adding families.** 1h is hard-capped at ~1 trade/day and ~$11–15/day. The 5m path is now technically open but its edge is unproven at the parameters we ship. |
| Is it ready for real money? | **No.** 7 resolved trades; need 20 minimum, 30 preferred at the new clip. Three code defects must be fixed first. |

---

## 1. Did the live bot behave correctly?

**Yes. Confirmed by independent shadow-replay. No logic discrepancy was found.**

`audit/A2_replay.md` fetched the live period (2026-07-16 18:00Z → 07-25 23:00Z) fresh from the data
vendor — 222 hourly markets, 888 quote files, 52 order-book snapshots, 13 days of Binance 1s klines,
zero download failures — and re-ran the bot's exact `close_snipe` logic over every close.

The replay port was verified against the bot's own code, not eyeballed: 20,000 randomised inputs
through `replay.evaluate_second` vs `strategy.evaluate_close_snipe` → **0 mismatches**; 20,000
ladders through the fill walk vs `fill_engine.walk_asks` → **0 mismatches**.

| | **LIVE (10 days)** | **REPLAY (5-level ladder)** |
|---|---|---|
| closes evaluated | ~238 | **222 / 222 = 100%** |
| signals | 11 | 13 |
| fills | 7 | 8 |
| `book_moved_no_edge` | 4 | **4 — exact match** |
| shares | 214.69 | 144.65 |
| total P&L | **+$44.97** | +$59.09 |
| ¢/share | +20.95 | +40.85 |
| win rate | 85.7% (6/7) | 100% (8/8) |

Every gap is explained by inputs, not logic:

1. **13 vs 11 signals** — the replay evaluates 6 exact integer seconds; the live 1 Hz tick runs at a
   drifting sub-second phase, and 5 of the 13 signal windows qualified on only 1–2 of the 6 seconds.
   The live period also ran ~13 h past vendor coverage (238 vs 222 closes). Phase noise, not a defect.
2. **`book_moved_no_edge` 4 vs 4** — the one failure mode the audit cared about reproduces exactly.
3. **Shares 214.69 vs 144.65** — the live bot walks the *full* CLOB ladder; vendor snapshots stop at
   5 levels. Top-of-book only gives 91.92 shares, so moving 1 → 5 levels recovers over half the gap.
   Replay size is a lower bound, as expected.
4. **20.95¢ vs 40.85¢ and 6/7 vs 8/8** — input precision, and the sign is right. The live bot's price
   and volatility come from ~1 Hz REST polls: repeated/stale values depress realised sigma, which
   inflates `|z|` and therefore `fair`, so the live bot fires on asks the exact-kline model rejects.
   Statistically the win rates are indistinguishable: **P(8/8 | p=0.857) = 0.29**.

**Data integrity all passed:** 1H candles rebuilt from the new 1s klines matched Binance's own REST
endpoint on **247/247** hours (so the replay's strike is bit-for-bit what the bot reads live), and
the vendor's `result_id` agreed with the Binance 1H rule on **222/222** markets.

**Nothing is throttling the bot.** What caps it is the market: at 3 s before the hour, of 258
standing quotes across 222 closes and both outcomes, **196 were ≤ $0.01 and 13 were > $0.99** — only
27 landed inside the tradeable `(0.30, 0.99)` band. **Only 25 of 222 closes (11%) had an in-band ask
at any snipe second**, and the bot signalled on 13 of those 25 (**52% conversion**). By six seconds
before the hour the hourly market is already priced as decided. There is nothing to buy in 89% of
closes, and no parameter fixes that.

**Limitation, stated plainly:** the repo has no live `fills.csv`/log for this period (the only
archive predates it), so this is an aggregate comparison, not trade-by-trade. Copying the server's
`bot/data/fills.csv` + `polybot.log` into the repo and joining on `market_slug` would upgrade
"consistent in aggregate" to "identical trade-by-trade"; `audit/A2_replay_trades.csv` is keyed for it.

---

## 2. Is the Chainlink oracle solved?

**Yes. Completely. It is proven, it is live, it is wired, and it costs nothing.**

This was the single biggest open question and it is now closed on all three sub-questions.

### 2.1 What resolves 5m/15m/4h — settled to 100.00000%

`sign(close − open)` computed from the captured `crypto_prices` series reproduces Polymarket's
on-chain `result_id` on **35,982 / 35,982** resolved 5m/15m/4h markets over 97 days —
**zero disagreements**, including all 254 markets that moved less than $0.50 over the whole window.
Per family: 5m 26,582/26,582; 15m 8,846/8,846; 4h 554/554.

The verifier re-derived this independently and **broke the circularity worry A1 raised about
itself**: 20,461 of the 35,982 markets (57%) have *no* reconstructed chainlink columns anywhere in
the repo, and they agree **100% on their own**. Split by provenance, both halves are 100%.

The verifier also **quantified the test's power**, which A1 never did: on the identical rows, Binance
is right **50.0%** of the time in the `|move| < $0.50` bucket where Chainlink is **254/254**. A
merely-correlated feed cannot produce that. Against a coin-flip null, 254/254 is p = 2⁻²⁵⁴.
Replicated out-of-sample on data neither audit had touched (fresh Jul 21–25): **1,345/1,345, 100%**.

**Refuted along the way:** the old "0.74 agreement for 5m" figure was blamed on a NaN trap. It was
not — the naive `NaN >= NaN → False` computation actually yields **98.63%**, not 0.74. The 0.74 was
some other bug (mis-join or polarity flip). Dead either way; the correct number is 1.000.

### 2.2 The live source, its latency, and its cost

| | |
|---|---|
| **Source** | `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`, filter `{"symbol":"btc/usd"}` |
| **What it carries** | The Chainlink Data Streams BTC/USD report verbatim, feed `0x00039d9e…ed75b8`, 18-decimal integers |
| **Auth** | **None.** No key, no contract, no signup. |
| **Cost** | **$0.** |
| **Latency** | Publish lag **p50 1.37–1.48 s, p99 2.02 s**; +~0.14 s network hop. End-to-end ~1.5 s from this sandbox; expect ~1.4 s from a colocated server. |
| **Coverage** | ~96.7% of seconds. The missing ~3% is upstream (the DON does not emit every second) and a second connection will not fix it — proved: an independent GMX path supplied **zero** of the 10 seconds RTDS missed in a 300 s window. |
| **Standby** | GMX `signed_prices/latest` — the same report, cryptographically **signed**, independent operator, ~0.5 s slower. Free. Already implemented. |
| **Liveness check** | Polygon on-chain aggregator, 33.8 s cadence — sanity only, never a signal. |

**Feed identity proven three independent ways** by the verifier: (a) every GMX blob decodes to the
mainnet BTC/USD feed ID and RTDS matched that signed report as exact 18-decimal integers on
**128/128** shared seconds (A1 got 266/266 on its own run); (b) the sibling Binance topic is visibly
a *different* series (median basis −$75.83, 38.69% repeats vs 0.00%) so there is no risk the code is
silently reading the wrong one; (c) live cadence/lag/precision match the historical capture.

**Chainlink's own Data Streams API is commercially gated** — verified: unauthenticated calls return
HTTP 400, billing is subscription-based, no public price list, "contact us" for mainnet. **We do not
need it.** It is a fallback only if Polymarket ever closes RTDS.

### 2.3 Is it wired? Yes — and correctly left disabled

`ChainlinkOracle` is implemented in `bot/polybot/oracle.py`, wired into the engine, and covered by
57 offline tests (132 pass in total). It has been run live end to end: first fresh print 1.25 s after
start, a complete 5m window followed strike → settle on the exact boundary seconds, winner decided on
18-decimal integers.

**Ground truth cross-check: 3/3 windows matched gamma's actual resolution** (B2's n=1 plus the
verifier's independent 2/2). That is three, not the 100%-of-a-day validation the turn-on procedure
requires.

**One real bug was caught only by running it live:** RTDS matches the subscribe `filters` value as a
literal string, so `json.dumps`'s default space after the colon silently kills the feed —
`'{"symbol": "btc/usd"}'` → snapshot delivered, **0 updates**; `'{"symbol":"btc/usd"}'` → **30
updates in 30 s**. Independently reproduced by the verifier. Fixed with `separators=(",",":")` and
pinned by a regression test. The failure was silent *and* masked by the standby — which is exactly
why the `n_rtds` vs `n_standby` health counters exist.

### 2.4 So — can we trade 5m/15m now?

**The oracle is not the blocker any more. The evidence is** (see §3). Two hard gates remain before
any short family flag is flipped, both already documented in `audit/B2_impl_chainlink.md` §6:

1. **`sigma_1s_floor` is mis-calibrated by 3.8×.** It ships at `8.0e-06`, tuned for Binance 1s klines
   which are ~76% flat. Chainlink moves nearly every second (4.66% repeats); the live measurement is
   **3.041e-05**. The floor feeds `fair_value_up` directly — leaving it too low inflates `|z|` and
   manufactures false certainty, the exact failure that cost the first live paper trade. It must be
   re-derived from the 97 days of `crypto_prices` on disk and set **per oracle**.
2. **The oracle-vs-gamma winner agreement is n=3**, not the required 24 h at 100%.

**Owner action required: none on procurement.** No contract to sign, no invoice, no vendor call. The
only remaining work is ours.

---

## 3. Is the 5m edge real on fresh data, after the verifier's attacks?

**No lookahead was found — the audit was clean. But the fresh-data result does not survive
correction, and its headline number is REFUTED as a robust estimate.**

### 3.1 What survived (good news, stated first)

The verifier attacked four lookahead vectors and found none:

- **Publication lag:** 0 of 7,200 decisions used a print published after the decision second. Same
  check on the 43-day frame: 0 of 61,835.
- **Volatility estimate:** rebuilt with a strict publication filter — **bit-identical on all 7,200
  rows**. Edge shrinks by exactly **0.00¢**.
- **Settle print:** minimum `tau_eff` across all decisions is 3.0 s; never touched.
- **Books:** all causal; every negative-age row is all-NaN, so no future book is readable.
- **Positive control** (to prove the detector works): injecting the settle print yields 97.5% win,
  +27.24¢/share, t=12.31. The honest run yields 74.1%, +7.42¢, t=1.84. Nothing like a lookahead
  signature.

The verifier's from-scratch rebuild of the whole evaluation frame reproduces A3's strike, price,
sigma and fair value with **0 mismatches on 7,200 rows**.

### 3.2 What was REFUTED — a defect A3 missed

**7 of the 108 fresh fills (6.5%) executed against order books frozen by a vendor data outage.**
14 of the 16 stale books in the entire 2,880-book sample share a single last-update instant on
2026-07-21 ~04:07 UTC; ages run **355 to 1,975 seconds**. All 7 won (7/7) at ~+46¢/share and account
for **53.6% of total P&L**. This is not lookahead — it is simulated liquidity that was never
observed to exist at the decision instant.

### 3.3 The degraded numbers — use these, not the optimistic ones

Fresh vendor data, 2026-07-21 → 07-25, all 1,440 windows:

| variant | ¢/share | trades/day | win | t (per-trade) | $/day @ $25 cap |
|---|---|---|---|---|---|
| A3 headline as published | +7.42 | 21.6 | 74.1% | 1.84 | $58.8 |
| **after removing outage fills** | **+4.72** | 20.2 | 72.3% | **1.13** (p=0.26) | $27.3 |
| both books fresh (≤5 s) | +4.30 | — | — | 1.02 | — |
| **at the parameters we actually ship** (`tau∈[2,5]`, `edge_min 0.03`) | **+3.66** | 22.8 | — | **0.96** | — |
| **shipped params + outage fix** | **+0.87** | — | — | **0.22** | — |
| at recalibrated `sigma_1s_floor` 3e-5 (before outage fix) | +6.40 | — | — | 1.29 | — |

Stress tests, each applied to the A3 baseline:

| stress | ¢/share | trades/day | t | $/day @ $25 |
|---|---|---|---|---|
| fee 0.07 → 0.10 alone | +6.88 | 21.4 | 1.69 | $53.3 |
| 50% of displayed depth alone | +7.30 | 21.6 | 1.81 | $54.2 |
| latency 1.5 s → 3.0 s alone (post-close fills blocked) | +6.61 | 13.6 | 1.35 | $30.5 |
| latency 3.0 s with the bot's own `tau_lo = latency+0.5` rule | **−2.69** | 6.2 | −0.35 | negative |
| **all three + outage fix** | **+0.77** | 11.8 | **0.15** | **−$2.0** (and **−$58/day at a $250 cap**) |

**Verdict: the 5-day fresh validation cannot carry the decision.** Any single stress is survivable;
together, with the contaminated fills removed, the edge is statistically indistinguishable from zero
and loses money at the raised clip.

**Also refuted:** A3's latency-3.0 s sensitivity row (+5.18¢, 71 fills) is not executable as written
— 2 of its 5 tau buckets fill *at or after* the market close.

### 3.4 The part that is still standing

The **43-day repo sample** (Apr 2 – May 12 + Jul 6/7) is far more robust and survives the same
correction: **+8.38¢/share, t=7.56** after removing stale-book fills (71 of 1,503 fills, 14% of P&L);
at the shipped parameters it gives **+10.89¢, t=10.33**. So the edge is *not* disproved — what is
contaminated is specifically the 5-day fresh validation that was supposed to prove it still exists
today.

Three other things remain true and matter:

- **The original `+14¢ at 52 trades/day` claim is reproducible** under its own conventions (51.8
  trades/day, +13.32¢, 77.2% win). The 52 → ~22–33 gap is almost entirely honest two-book execution:
  a signal must re-clear `edge_min` against a book re-fetched 1.5 s later, and **34% of signals die
  that way**. Plan on ~20 trades/day, never 52.
- **The edge is not stationary.** Apr 2–15: **−1.03¢**. Apr 16–30: +13.06¢. May 1–12: +19.66¢.
  Jul 6–7: +0.45¢. Jul 21–25: +7.4¢ (→ +4.7¢ corrected). Decay test Apr/May vs fresh July:
  Welch p = 0.050 — suggestive, not settled. **There is no 5m book data for June at all**, so the
  transition is unobserved.
- **5m is still ~10× more scalable than 1h even at half the edge.** Median fillable notional per
  signal is **$83–200** versus **$11.40** on 1h, and deep-book moments carry *more* edge, not less.

### 3.5 The shipped parameters were never validated on 5m — a real problem

A3's headline used `tau ∈ {6,5,4,3,2}` and `edge_min 0.05`. `bot/config.yaml` ships `tau ∈ [2,5]` and
`edge_min 0.03` — a window **derived entirely from 1h data**, where the book bulk-cancels before the
close. On 5m the book does *not* bulk-cancel, and the two datasets disagree about where the good end
of the window is: the repo tape says τ=4 is best, the fresh tape says **τ=6 is best (+9.89¢)** — the
one value the shipped window excludes. On fresh data the shipped combination gives **+3.66¢ (t=0.96)**
versus the advertised +7.42¢.

**Do not enable 5m on the strength of a number produced by neither configuration.**

---

## 4. What changed in the bot, and what to expect on 1h

### 4.1 The four strategy/sizing changes (shipped, PAPER)

| # | change | why |
|---|---|---|
| **i** | Snipe window bounded at **both** ends: `tau ∈ [2.0, 5.0]`, derived as `max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs)` | The old gate was `0 < tau ≤ 6` — **no lower bound at all**. It could fire at τ=0.02 s, sending an order ~1.9 s *after* the close. Measured: τ=1 signals filled **0 of 46** times, 42 on an empty book. In LIVE that is a real FAK order into a closed market. |
| **ii** | `edge_min` 0.05 → **0.03** | +22% fills, +11% P&L, higher t-stat in the new window (t 3.89 → 4.31). 0.02 adds nothing. |
| **iii** | `per_event_cap_usd` $25 → **$250**; `max_open_notional` $250 → **$1000** | The measured ~5× P&L lever. **Coupled to (i) and must not ship without it** — at $250 in the old window, P&L peaks then collapses and the worst single trade is −$258.75. |
| **iv** | `settle_sweep` **OFF** for 1h | 1,235 signals, 1,235 `empty_book`, **zero fills** in 10 days. Pure logging. Code path left in place as the re-entry point. |
| *addendum* | `settle_sweep` given its own `cap_usd: 25` | Otherwise re-enabling it would have silently inherited the $250 clip on zero evidence. |

Plus the ChainlinkOracle (§2.3), 21 + 57 new tests, and `engine.py`'s **first test coverage ever** —
`_maybe_snipe` was 100% untested before this. The new tests were verified to actually catch the
regression: restoring the old gate produces **4 failures**, not a vacuous pass.

Diff vs the pre-audit baseline: **2,440 insertions / 84 deletions** across `bot/`. **132/132 tests
pass.** Bot remains in PAPER; no live-mode guard was touched.

### 4.2 Expected 1h performance

Same 1,738-close out-of-sample tape (2026-05-01 → 07-12, 72.4 days), phase-averaged:

| | **old shipped** `(0,6]`, edge 0.05, cap $25 | **new shipped** `[2,5]`, edge 0.03, cap $250 |
|---|---|---|
| fills/day | 0.83 | **0.91** |
| ¢/share | 12.13 | **21.61** |
| win rate | 81.6% | **84.8%** |
| **modelled $/day** | **$1.98** | **$11.49** |
| worst single trade (modelled) | −$26 | −$45.26 |
| worst 10-trade run (modelled) | −$232 (at $250 in the old window) | −$16 |

**Realistic expectation: ~0.9 fills/day and roughly $11–15/day, versus $4.50/day today.**
The live bot has historically realised ~2.3× the replay's P&L on the same tape (it walks the full
CLOB ladder; the 1h vault tape is top-of-book only), which argues for the upper end — but 10 days is
a tiny sample and one trade was 69.5% of the replay P&L.

**Frequency is unchanged, and that is the whole story of why the P&L is small.**

```
$4.64/day  =  0.72 fills/day  ×  30.7 shares/fill  ×  $0.2095 EV/share
```

The edge (third factor) is excellent. The second was self-imposed and is now fixed. **The first is
structural**: 24 hourly closes/day, only 11% have anything in the tradeable price band, 52% of those
convert. No parameter setting on any grid tested moves 1h past ~1 trade/day. The 1h family's ceiling
is roughly **$11–16/day**, and we are now most of the way to it.

### 4.3 Three honest caveats on the cap raise

1. **The cap binds on ~1 trade in 20.** Top-of-book notional at the fill instant is median $17.70;
   only **38.2%** of fill moments offer ≥$25 and **4.7%** offer ≥$250. Nineteen days out of twenty
   will look identical to today. The increment — and the entire tail — arrives in rare lumps.
2. **"Worst trade −$45.26" is NOT a bound** (REFUTED as such by the code verifier). It is a
   top-of-book artifact of a replay that consumes one level while the live bot walks the full ladder.
   The **structural** per-trade bound is **−$250**, and the global ceiling is
   `max_open_notional + per_event_cap = $1,250` (more if a second family is ever enabled), because
   `_would_exceed_global_cap` is evaluated *before* the new trade is sized.
3. **A single −$250 trade erases ~24 days of expected profit.** At 0.9 fills/day and ~$11/day, that
   is the risk shape now. It is acceptable in PAPER — collecting exactly this tail evidence is the
   point — and it is *not* acceptable as a first live setting.

---

## 5. Ranked next actions

Ranked by expected value per unit of work and risk. Items 1–4 are prerequisites for everything else.

| # | action | expected value | cost | risk if skipped |
|---|---|---|---|---|
| **1** | **Fetch 20–30 days of 5m book data and re-run with a `≤5 s` book-staleness filter, at the shipped parameters, at the recalibrated sigma floor.** | **Decisive.** This is the only experiment that decides whether the entire scale-up thesis exists. Outcome range is between **+$444/day** and **−$58/day** at a $250 clip. | ~2–4 h of fetching (measured: 2,880 files in ~800 s, 0 failures) + one replay run | We enable 5m on a 5-day sample whose headline is 54% vendor-outage artifact. This is the single largest live risk in the project. |
| **2** | **Re-derive `sigma_1s_floor` per oracle** from the 97 days of `crypto_prices` on disk. | Mandatory gate. Current value is **3.8× too low** for Chainlink; too low a floor manufactures false certainty — the exact failure that cost the first live paper trade. | ~1 h | Every 5m signal is computed with an inflated `\|z\|`. |
| **3** | **Fix the `tau_lo` fill-margin defect.** `tau_lo` accounts only for simulated latency, not for the two blocking `/book` fetches that sit between the gate and the fill worker. Measured δ_book: **median 0.315 s, cold 0.791 s** → the real margin at the band edge is **+0.19 s median and −0.29 s on a cold connection**, i.e. at or after the close. | Closes defect D1 properly. Quick fix: `snipe_min_tau_secs: 2.5`. Better: record `t_submit` and feed an EWMA back into the bound. Best: get the book fetches off the critical path. | 1 line, or ~1 h for the proper fix | The change that exists to stop orders arriving after the close still lets them arrive after the close ~half the time at the band edge. Harmless in PAPER, real money in LIVE. |
| **4** | **Add an explicit family allowlist for `close_snipe`.** The unconditional `family != "1h"` guard was **deleted** in the refactor; a single YAML boolean now arms 5m/15m/4h at the $250 clip with 1h-calibrated `edge_min` and window. Verified empirically. | Prevents an accidental 12× frequency increase at a 10× clip on never-validated parameters. Also fix the false "guard untouched" row in `B1_impl_strategy.md` §2. | ~1 h incl. a test | One config typo arms three unvalidated families. |
| **5** | **Add behavioural tests for `fair_cap`, `sigma_1s_floor` and `_place_live_order`.** All three can be **deleted with 132/132 tests still green** — the existing tests assert YAML *values*, not that any code reads them. `_place_live_order` is the only code path that spends real money and has **no test at all**. | `fair_cap` saturation is literally the machinery that produced the −$258.75 trade. At a 10× clip a silent regression there is a $250-per-trade error. | ~2 h | The safety table in B1 §2 is an over-claim and would be a false sign-off artifact. |
| **6** | **Run the 24 h oracle-vs-gamma winner validation** (`ChainlinkOracle.winner()` vs gamma `closed==true` on every 5m close, ~288/day). Bar is **100%**. | Turns n=3 into n≈288. Cheap, no money at risk, and it is the acceptance test the turn-on procedure already specifies. | ~1 day wall-clock, ~2 h to write the logger | Enabling 5m on three observations. |
| **7** | **Fix defect D2 before any short family is enabled.** `engine._tick` iterates markets in discovery order (not by `close_ts`) with blocking REST book fetches inside one tick, all sharing a single `now`. | Invisible with one 1h market in-window; becomes real immediately at 288 5m closes/day, making recorded `tau` hundreds of ms optimistic — against a band whose lower bound already has no margin (item 3). | ~2–4 h | The 5m families inherit a systematic timing error on day one. |
| **8** | **Keep the 1h paper bot running at $250 to 20–30 resolved trades.** | This is how the ladder-depth tail evidence that the top-of-book replay *cannot* provide gets collected. ~$11–15/day of paper P&L, and the data that unlocks the live go/no-go. | Zero — it is already running | We go live on 7 trades. |
| **9** | *Then* enable 5m `close_snipe` in PAPER only, at `per_event_cap_usd` **$25–50**, one family, `settle_sweep` off. | The real scale test. 288 closes/day means ~20 trades/day — statistical significance in days, not months. | config + monitoring | — |
| **10** | Re-test `settle_sweep` for short families **from scratch**. | The exact Chainlink settle print removes the *specific* failure that killed it (3/3 Binance-disagreement fills, all lost), but the underlying adverse-selection question — are the cheap asks left standing at settle time there because someone else already knows? — is unanswered. | a study, not a flag flip | Repeating a known loss. |
| — | **Real money.** | See §6. | — | — |

**Explicitly NOT recommended:**
- Carrying `per_event_cap_usd: 250` into LIVE. That is a PAPER setting.
- `edge_max` filters or edge ramps — both tested and rejected (they kill winners and do not remove the tail).
- Widening the snipe window. Confirmed strongly: −4.85¢/share at τ=15, −6.99¢ at τ=60.
- Lowering `price_min` below 0.30. Load-bearing: 0.30 → 100% win / 40.9¢; 0.10 → 63.6% win / 3.8¢.
- Buying a Chainlink Data Streams subscription. Unnecessary; the same report is free.

---

## 6. Honest status

### 6.1 Proven

- **The live bot does exactly what its code says.** Independently replayed; 0/40,000 equivalence
  mismatches; `book_moved_no_edge` matches 4-for-4; ~100% close coverage; nothing throttling it.
- **The resolution source for 5m/15m/4h is Chainlink Data Streams BTC/USD**, feed `0x00039d9e…ed75b8`
  — 35,982/35,982, zero disagreements, non-circular, with the test's power quantified (Binance is a
  coin flip on the same near-tie rows) and replicated out-of-sample at 1,345/1,345.
- **That feed is available live, for free, with no credentials**, at ~1.4 s publish lag, and the
  implementation reads the correct feed (proven three ways, including a byte-exact integer match
  against the signed report).
- **The 5m backtest contains no lookahead of any kind.** A strictly causal rebuild shrinks the edge
  by 0.00¢, with a working positive control to prove the detector functions.
- **The `[2,5]` window beats `(0,6]` at a raised clip on two independent datasets**, and its lower
  bound is a genuine correctness fix (0/46 fills at τ=1).
- **1h frequency is structurally capped at ~1 trade/day.** Confirmed on the full parameter grid and,
  independently, by the price-band census (89% of closes have nothing tradeable).

### 6.2 Not proven / still open

- **The 5m edge at the parameters we ship.** +3.66¢ (t=0.96) on fresh data, +0.87¢ (t=0.22) after the
  outage fix, −$58/day at a $250 clip under stress. The 43-day older sample says +10.89¢ (t=10.33).
  **Unresolved, and it is the whole scale-up thesis.**
- **Whether the July regime is the new steady state or a dip.** Decay p = 0.050. June is unobserved.
- **The realised loss tail at a $250 clip.** Every tail figure quoted is a top-of-book lower bound.
- **Oracle-vs-gamma agreement beyond n=3.**
- **RTDS rate limits, connection limits, uptime SLA, and whether Polymarket keeps it public.**
  Undocumented. It is a single point of failure; the GMX standby is built but has never been
  exercised under a real RTDS outage.
- **Production-server latency.** Everything was measured through this sandbox's HTTPS proxy. Expect
  ~0.1–0.2 s better; **verify, do not assume** — the whole timing frontier depends on it.
- **Market impact and queue position.** Every capacity number is a static-book replay. $250 is
  defensible; above it is untested.
- **The Chainlink publish lag is drifting upward** (daily p50 1.01 s on Jul 18 → 1.36 s on Jul 24).
  If that continues it directly erodes the signal.

### 6.3 REFUTED — claims that were in our own documents and are now dead

Reported here rather than quietly dropped.

| claim | status |
|---|---|
| `docs/06` §5.1 "later is dramatically better; −2s/−1s yields 17–25¢" | **REFUTED.** Assumed a 1 s signal→fill gap and the same book for signal and fill. At the shipped 1.5 s gap, τ=1 fills **0/46**. No significant gradient inside τ=2…6. |
| `docs/06` §6 lever 2 "snipe only in the last 2–3s" | **REFUTED on two independent datasets.** `[1,3]` earns $38.77 vs `[1,6]`'s $59.09 on the live period, and the lowest total P&L of any candidate OOS. Would have taken the live period from 8 fills to ~1. |
| `docs/06` §4 "cap table monotone to $1,000" | **REFUTED.** True only after the window is narrowed. In the old window P&L peaks near $250 then collapses ($473 at $500, $28 at $1,000). |
| `A2` §4 implied "keep τ=6" | **REFUTED at a raised clip.** Holds only at $25; at $250 every losing trade in `(0,6]` came from τ≥5. |
| `A3` fresh headline "+7.42¢/share, t=1.84" as a robust estimate | **REFUTED.** 53.6% of that P&L comes from 7 fills against books frozen by a 2026-07-21 vendor outage (ages 355–1,975 s). Honest figure: **+4.72¢, t=1.13**. |
| `A3` latency-3.0 s row (+5.18¢) | **REFUTED as executable.** 2 of its 5 tau buckets fill at or after the market close. |
| `A1` "the 0.74 figure was a NaN artefact" | **REFUTED.** The NaN-trap computation gives 98.63%, not 0.74. Conclusion (100%) unaffected; the diagnosis was wrong. |
| `A1` §2(a) exact match vs `windows.parquet` | **Flagged circular by A1 itself and confirmed circular.** Those columns are a reconstruction of the same capture. The `result_id` test is the real one — and the verifier broke the circularity independently. |
| `B1` §2 safety table: "non-1h `close_snipe` defensive guard — untouched" | **FALSE.** The guard was deleted and replaced with per-family dispatch. |
| `B1` §2: `fair_cap` / `sigma_1s_floor` / `max_walk_above_best` "pinned by tests" | **OVER-CLAIM.** All three can be deleted with 132/132 tests still green; the tests assert YAML values, not code behaviour. |
| `A4`/`B1` "worst single trade −$45.26" as the tail | **REFUTED as a bound.** Top-of-book artifact. Structural bound is **−$250/trade**, **−$1,250** global. |
| `A4`/`B1` "0.5 s fill margin at `tau_lo`" | **REFUTED by measurement.** Real margin is **+0.19 s median, −0.29 s cold**, because two blocking book fetches sit between the gate and the fill worker. |
| `B2` "publish lag p50 ~1.0 s", "125 tests" | **Corrected** to 1.482 s and 132 (self-corrected in B2). |

Two more measured-and-rejected alternatives, recorded so nobody re-proposes them: an `edge_max`
filter (flat $676 → $480 → $274 → $72 as the bar tightens, worst trade still −$209), and an edge ramp
requiring a higher bar at high tau ($131/$155/$124 vs $136 flat).

### 6.4 How many more paper trades before real money

- **Current: 7 resolved trades.** `docs/06` §8 set the bar at **20+**.
- At the new ~0.9 fills/day that is **~14 more days** to reach 20.
- **Recommendation: raise the bar to 30 resolved trades at the $250 clip (~26 more days).** The 10×
  clip means the loss distribution, not the mean, is what we are now uncertain about, and the only
  evidence that can settle it is realised full-ladder fills.
- **Hard prerequisites before any live money, regardless of trade count:** items 3, 4 and 5 in §5 —
  the `tau_lo` margin fix, the family allowlist, and behavioural tests on `fair_cap` /
  `sigma_1s_floor` / `_place_live_order`.
- **First live clip: $25–50, not $250.** Step up only after 20 resolved *live* trades.
- If 5m turns on and validates, the trade count arrives ~24× faster and this timeline collapses from
  weeks to days — which is precisely why item 1 in §5 is ranked first.

### 6.5 What would falsify the edge

Pre-commit to these now, so they are not renegotiated later.

**1h `close_snipe`:**
- EV/share below **+8¢** over the full sample, or t-stat below 2 — the threshold already set in
  `docs/06` §8.
- Win rate drifting below **70%**.
- Any rolling 20-trade window with negative total P&L.
- Any fill recorded with an effective submit time at or after the close (instrument this — it is
  currently unmeasured and item 3 says it happens).

**5m `close_snipe`:** on 20–30 days of fresh book data, with a ≤5 s book-staleness filter, at the
shipped parameters and the recalibrated sigma floor:
- EV/share below **+3¢**, or day-level t below 2 → **do not trade 5m at all**.
- Realised fill rate below ~50% of signals (the two-book execution assumption breaking down).
- Realised trades/day below ~10.

**The oracle:**
- **Any** disagreement between `ChainlinkOracle.winner()` and gamma's resolution (`closed == true`)
  over the 24 h validation. The bar is 100% and it should be met exactly; anything less is a
  transport problem, not an analysis problem.
- `n_rtds` collapsing toward zero while `n_standby` carries the load — the silent subscribe-filter
  failure mode.
- Publish lag p50 drifting above ~2 s, or `coverage_300s` falling below ~0.90.

**Whole-thesis falsifier:** if 20–30 corrected fresh days put 5m below +3¢/share, then the *only*
scalable family is gone and this strategy's honest ceiling is **~$11–16/day on 1h**. At that point
the correct decision is to stop adding engineering and either run it as a small standing position or
shut it down — not to keep tuning parameters against a frequency ceiling that no parameter can move.

---

## 7. The bottom line for the owner

The P&L is small for one reason that was our fault and one that was not. Ours: a $25 clip and an
unbounded snipe window — both now fixed, worth roughly **$4.50/day → $11–15/day**. Not ours: the 1h
family only offers ~1 tradeable close per day, and no amount of tuning changes that.

Scaling therefore depends entirely on the short families. The technical blocker there is **fully
solved and cost nothing** — we can read Polymarket's actual resolution oracle, live, for free, and we
have proven it is the right one to five decimal places of agreement across 35,982 markets.

What is *not* solved is whether the 5m edge is still there. The fresh five-day validation looked
good, then a verifier found that more than half of its profit came from seven fills against an order
book that a data vendor had frozen during an outage. Corrected, it is statistically indistinguishable
from zero; at the parameters we actually ship, it is +0.87¢/share with a t-stat of 0.22.

So: **one experiment stands between a $12/day curiosity and a real business**, it costs a few hours
of data fetching and no money, and it is ranked first. Everything else — including any real money —
should wait for its answer.
