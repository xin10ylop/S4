# 09 — Multi-coin decision and implementation

**Date:** 2026-07-28 · **Scope:** should the 1h `close_snipe` edge be extended to the other six
hourly Up/Down coins, and what shipped as a result.

**Inputs:** `audit/M1_multicoin_recon.md` (recon), `audit/M2_dominance_arb.md` (dominance arb),
`audit/M3_multicoin_backtest.md` (backtest), `audit/M4_risk_guards.md` (guards), plus an
**independent adversarial verification pass** which re-ran the M3 harness end to end, re-derived
the settle reconciliation from raw Binance klines, re-fetched all seven resolution sources live,
recomputed M2 from the stored run records, and ran 36 fresh mutations against the guard suite.
**Where any document disagrees with the verifier, the verifier wins.** That rule changed one
recommendation in this document and it is the reason the headline is not what the run set out to
find.

---

## 0. The one-paragraph answer

The hourly family exists for seven coins, every one of them is provably wired to its own Binance
candle, and the frequency prize was real: 0.81 → 2.14 trades/day if all seven traded. **But the
edge does not travel.** Pooled across the six non-BTC coins the strategy earns +2.01¢/share on 107
fills — day-clustered *t* = 0.45, bootstrap P(EV ≤ 0) = 0.31, negative on the untouched TEST half,
negative after removing its three best days, and **negative under a mere +300 ms feed lag**. The one
coin that looked promising, ethereum, was never stress-tested by the document that recommended it;
when the verifier ran that grid, ETH's +9.94¢ fell to **+0.49¢ at a 3 s round trip**. So: **bitcoin
remains the only coin permitted to fill.** Ethereum ships in a new **shadow mode** — real signals,
real ledger rows, structurally incapable of dispatching a fill — which is the only thing that can
settle the question and costs nothing. The frequency problem is *not* solved by this run, and
saying otherwise would be the expensive kind of wrong.

---

## 1. THE DECISION, PER COIN

| coin | **DECISION** | the deciding number | verifier verdict |
|---|---|---|---|
| **bitcoin** | **ENABLE — unchanged, $250 clip** | +13.32¢/share on 66 fills, **t_day 3.30**, P(EV≤0)=0.000; unmoved across the entire stress grid (+12.7¢ to +16.0¢) | **CONFIRMED and strengthened.** Reproduced field-for-field; corroborated on a *second vendor tape* with a *second harness* (+15.49¢ vs +13.32¢, 100% side agreement on 36 shared closes) |
| **ethereum** | **SHADOW ONLY — evaluate + log, never fill** | +9.94¢ shipped → **+0.49¢ at a 3 s round trip** (t_day 1.94 → 0.10); +3.73¢ at +300 ms feed lag; +6.17¢ at a 2 Hz tick | **M3's ENABLE recommendation REFUTED.** "M3 recommends ETHEREUM and never stress-tests it… ETH's edge is smaller than one extra REST round trip" |
| **solana** | **DO NOT ENABLE** | −7.70¢/share, 57.1% win against 99.0% direction accuracy; **24% of its signals come from an exactly-zero underlying move** | CONFIRMED (P(EV≤0) = 0.844) |
| **xrp** | **DO NOT ENABLE** | −5.93¢/share; 18% zero-move signals; worst fill conversion (38.5%); best day = 105.6% of total P&L | CONFIRMED (P(EV≤0) = 0.717) |
| **dogecoin** | **DO NOT ENABLE** | +1.48¢ with a **[−32.8, +30.0]** CI on 11 fills; best day 79.8% of P&L, best 3 days 96.9%; $15 liftable depth | CONFIRMED (P(EV≤0) = 0.423) |
| **bnb** | **DO NOT ENABLE — the trap** | Best on TRAIN (+17.2¢), **worst on TEST (−29.6¢, −$6.14/day)**; late sign-flip rate **2.193% vs BTC's 1.044%** | CONFIRMED at exactly 2.193% on 1,915 closes/coin |
| **hype** | **DEFER INDEFINITELY — and strike it from the rankings** | Its +25.64¢/t_day 4.88 is an artifact: 13 fills, **7 of them "two-book" fills that saw no new quote at all**, 9 against >5 s stale books, **one trade = 52.8% of its P&L**. No reachable live feed (fapi.binance.com → HTTP 451) | **REFUTED as a result.** "Every quantitative property of HYPE's number is an artifact" |

**Net: one coin trades. One coin watches. Five stay off.**

### 1.1 What is *not* in doubt

The safety-critical question — is each coin wired to the feed it actually settles on? — is
**closed**, and closed harder than either audit closed it:

* All seven `resolutionSource` strings re-fetched live on 2026-07-28 match M1 verbatim, including
  HYPE's futures URL. 22,713 historical markets carry exactly one source per coin back to
  2026-03-15; no coin has ever switched feed. `outcome_0 == "Up"` on 22,713/22,713.
* Up/Down re-derived independently from each coin's own Binance 1s klines vs the vendor's resolved
  outcome: **1,915/1,915 = 100.000% for every coin**, 13,405 closes.
* **And the check has power**, which nobody had established: the verifier ran the 7×7 cross-feed
  confusion matrix. Diagonal **100.000%**, off-diagonal **67.6–84.3%**. A coin scored against the
  wrong feed does *not* reproduce, so 100% agreement is evidence rather than a tautology.

That is why the code below can ship a seven-coin feed table with confidence, even though only one
entry is allowed to trade.

---

## 2. EXPECTED COMBINED RESULT

### 2.1 Today's baseline, stated honestly

| measure | value | source |
|---|---|---|
| trades/day | **0.81** (backtest) / **~0.9** (live) | M3 §3 / 10 days live paper |
| ¢/share | **+13.32** (81-day backtest) / **+21.5** (live, n=8) | M3 §2 / live paper |
| **$/day** | **+$6.30** at the $250 clip (backtest) · **~+$4.8** realized live (+$48 / 10 days) | M3 §2 / live paper |

A note I owe you: the brief quotes today's baseline as **~$11–15/day**. I cannot reproduce that from
any tape in this repository. The 81-day backtest at shipped parameters says +$6.30/day at the $250
clip; ten days of live paper realized +$48, i.e. ~$4.8/day. The gap is capacity, not edge — M3 §6
measured the median *fillable* notional per BTC signal at **$10.51**, so the $250 cap binds on
almost nothing and the book, not the config, sets the size. **Treat $5–6/day as the defensible
baseline.** Everything below is stated against that.

### 2.2 What each option would deliver

| portfolio | trades/day | $/day @ $250 | t_day | $/day − best 3 days | max drawdown |
|---|---|---|---|---|---|
| **BTC only (shipped)** | 0.815 | **+6.30** | **3.30** | **+2.56** | **−$23.31** (4.6% of total) |
| BTC + ETH | 1.037 | +8.29 | 3.25 | +3.82 | −$22.42 |
| all 7 coins | 2.136 | +8.49 | — | +3.61 | −$138.39 |
| non-BTC pool alone | 1.321 | +2.20 | **0.45** | **−0.60** | **−$138.39 = 78% of everything it ever made** |

**Expected result of what actually shipped: unchanged at ~0.8–0.9 trades/day and ~$5–6/day.**
Ethereum in shadow mode adds **~0.22 signals/day of evidence and $0.00 of P&L.** That is the honest
number and I am not going to dress it up: this run did not increase revenue. It established which
of six candidate expansions were traps (five of them), built the machinery that makes the sixth
testable at zero risk, and closed several holes in the guard suite.

### 2.3 The correlation caveat, stated honestly

If ETH is ever promoted, **do not size the pair as two independent bets.** Measured:

* BTC–ETH **daily P&L correlation +0.61**. The pair's daily σ is **30.90** against **26.52** for an
  independent sum — sizing it as independent understates risk by **17%**. Combined $/day is
  additive and unaffected; only the risk is.
* Hourly returns across all seven coins correlate **0.67–0.90**, and **all seven settle the same
  direction in 44.9% of hours**. The "zero self-competition" premise from M1 is true for *order
  flow* (seven separate books, no queue competition) and **false for risk**.
* The tail is what matters and it is thin but real: joint late sign-flips occur at **25× the
  independence rate** for 3-coin hours. Observed fill concurrency over 81 days was 162 closes with
  a fill, 10 with ≥2, **0 all-lose**, worst concurrent hour −$0.43. So the observed joint tail is
  *smaller* than M1 feared — but it is 81 days of one regime, and the mechanism (a sharp
  market-wide move in the final seconds leaves every coin's resting ask stale in the same
  direction) is structural.

**Consequence for the circuit breaker:** it is calibrated for one coin at ~2 trades/day. At N coins
the daily loss distribution widens by roughly √N *before* the +0.61 correlation is applied, which
makes it worse than √N. **Because only bitcoin fills, the shipped calibration ($100/day, 4-loss
streak) is unchanged and still correct.** Re-derive it before promoting any coin — this is open
item M4-2 and it stays open.

---

## 3. THE DOMINANCE ARB — NOT TRADEABLE

**Verdict: ship nothing. The mechanism is proven; the execution is not; the gap between them is 1.5
seconds wide.**

The structure is exact — **0 dominance failures in 9,677 shared 5m×15m closes** — and violations
are frequent (5.7–16.6 actionable/day, ~9× `close_snipe`). None of that matters, because of one
number:

> **A crossing has a median life of 0.489 s. At the shipped 1,500 ms round trip only 12.8% of
> detections fill both legs; 53.1% fill exactly one leg, leaving naked directional risk.**

The latency-survival curve is the whole story: **100% at 0 ms → 47.6% at 250 ms → 28.6% at 500 ms →
18.8% at 1,000 ms → 12.8% at 1,500 ms.** Break-even is between 500 ms and 1 s. The prior "~3 s
crossing life" measurement does not reproduce (p90 is 2.20 s; only 6.3% last ≥3 s), and the earlier
"fill both legs at the next second" method was a one-book fill in disguise that assumed away the
entire problem.

Everything else points the same way: capacity is **negative** with size (+$0.43/day at $25 →
−$2.27/day at $100 → −$17.39/day at $250); the best rule is negative on the pre-committed TRAIN
window (−$2.14/day, t_day −2.02), negative on 31 of 43 days, and negative after dropping its best 3
days; only **31 both-leg fills exist in the whole 43-day sample** (2 in TRAIN); and a self-crossed-
book data defect produced 12.2% of signals but **68.7% of the P&L** until it was gated out.

**Two things the verifier added, both of which make it worse:**

1. **M2 §5's decomposition figures are wrong.** Published "+$407.00 matched / −$46.94 residual"
   recomputes to **+$151.78 / −$133.23**. The published pair sums to $8.37/day against M2's own
   $0.43/day headline — a 19× contradiction — *and* it contradicts the sentence it was quoted to
   support. The corrected figures do support it.
2. **The +$0.43/day headline is 96.4% naked-leg P&L.** Five of 141 residual events could not be
   aborted; the harness silently fell back to HOLD, and those 5 contributed **+$17.88 of the
   +$18.55 total**. M2 §8 states "NEVER hold the naked leg" as a hard rule. Under the stated
   policy the honest figure is **+$0.016/day**, or **−$0.54/day** if an un-abortable leg is charged
   as a loss.

Only two things could change the answer: cutting the round trip below ~500 ms (at 0 ms it would be
+$20.93/day, t_day 4.25, zero leg risk), or becoming a *maker* on one leg — a different problem
that has not been analysed. Extending to more family pairs multiplies opportunity count and does
nothing to survival, leg risk, or negative capacity. **Frequency was never this idea's problem.**

---

## 4. THE NEW RISK GUARDS AND WHAT EACH PROTECTS AGAINST

Three guards shipped in M4 and are unchanged. This run adds three more, repairs the M4 suite,
and fixes a poller defect that would have deadlocked the warmup gate at the second coin.

### 4.1 Inherited, unchanged (M4)

| guard | state | protects against |
|---|---|---|
| **1. Adverse-size filter** | **OFF** (implemented, wired into both paper and live paths) | An informed seller dumping size seconds before close. **Measured and refuted on 1h BTC**: levels >5× the family median went 44/44 winners at +38.1¢/share vs +22.4¢ for normal levels; OLS size coefficient **+0.051, t = +2.87** — bigger is *better*. Turning it on would have discarded 69% of realized P&L to prevent a loss that never happened. The one dissenting reference definition (−10.6¢, p = 0.035, n = 19) is recorded in M4 §1.2b rather than buried; those trades are still +15.6¢ **profitable**. |
| **2. Warmup after restart** | **ON** — 60 oracle samples + 120 s uptime | A nearly-empty vol buffer after a deploy/crash makes σ read too small, which inflates \|z\| and pushes `fair` toward its cap — manufacturing edge that is not there. This is the mechanism behind the project's first −$25 loss. Cold-σ trades are worse at every threshold tested. |
| **3. Circuit breaker** | **ON** — $100/day (8% of $1,250) + 4-loss streak, UTC auto-reset, day-scoped override | A *bad day*. The per-event and open-notional caps bound one trade and simultaneous exposure; nothing bounded a losing run. Never would have fired on the observed tape (worst day −$41.47, longest streak 3) — this is a breaker for genuine breakage, not a variance throttle. |

### 4.2 New in this run

**Guard 4 — stale-book reject (`max_book_age_s: 5.0`, ON).**
Refuses to lift a book whose *CLOB-reported last-update stamp* is older than 5 s. Applied at the
decision point **and** inside both the paper and live fill paths. What it protects against: lifting
a quote that is stale *because it is about to be adversely selected*. Calibration is the point —
**BTC's oldest fill book across 66 shipped-parameter fills was 2.2 s** (median 0.30 s, zero fills
over 5 s), so at 5.0 s this guard would never have fired on the only coin that trades and changes
nothing validated. What it *would* have caught: HYPE drew **93.6%** of its P&L and BNB **46.5%** of
its *negative* P&L from fills against books older than 5 s, and applying the filter lifts the pooled
non-BTC result from +2.01¢ to +3.70¢/share. It **fails open** when the CLOB payload carries no
timestamp, deliberately — an upstream schema change must not silently halt all trading — and logs
when it does.

**Guard 5 — fail-closed per-coin oracle routing + coin allowlist (ON).**
Two mechanisms for one failure: *a market priced off another coin's underlying*. There is no
default oracle and no fallback branch anywhere on the path — `_oracle_for` returns `None` for any
coin without a verified feed, `_snipe_inputs` refuses rather than guessing, and a coin missing from
the verified feed table gets no oracle constructed at all. Separately,
`strategy.close_snipe.allowed_coins` (default `["bitcoin"]`) is enforced *independently of*
`allowed_families`, so widening the slug regex to seven coins cannot by itself widen what trades.
This is the error class that cost this project 3-for-3 losing fills; it now has a dedicated test
class (`TestNeverPricesOffTheWrongCoin`) and six mutations in the checker.

**Guard 6 — shadow mode (ON for ethereum).**
A coin in `shadow_coins` is evaluated against its own oracle and produces real signals in the
ledger, but the fill dispatch is *structurally* unreachable. A coin listed in **both** lists
resolves to shadow — a config contradiction resolves the safe way, not the fast way. This is how
live out-of-sample evidence gets collected for a coin whose backtest did not survive stress, at
zero risk.

### 4.3 Repairs to the M4 guard suite — 8 mutations that survived a green suite

The M4 document claimed "all guard mutations are caught by the test suite". **That claim was false**
and is withdrawn. The verifier wrote 36 mutations the project had not written; **8 survived a green
214-test run**:

* **Five in `config.py::risk_cfg`** — `daily.enabled → False`, `streak.enabled → False`,
  `max_daily_loss_pct 8 → 80`, `max_consecutive_losses 4 → 40`, `bankroll_usd 1250 → 12500`. Each
  silently hands a **pre-M4 config.yaml** a disabled or 10× looser circuit breaker — exactly the
  case M4 §4 promises is safe. Root cause: the existing lock read only `warmup_cfg`, and
  `WarmupGate({})` had a defaults assertion while `CircuitBreaker({})` had none.
* **Two in `risk.py`'s breaker defaults**, shadowed by `config.py` so shipped behaviour was
  unaffected — same missing test.
* **One in the warmup uptime threshold**: `uptime < min_uptime_secs` → `/2.0` (120 s → 60 s
  effective) left the suite green. The only uptime test used `uptime = 50`, so anything above 50
  still blocked; the undetected weakening window ran **120 s down to ~51 s**, a 2.4× silent
  loosening.

All eight are now closed, and **writing the test found a real defect**: `CircuitBreaker({})`
resolved **no daily limit at all** (`daily_limit = None`), because the bankroll/pct defaults lived
only in the config accessor. `risk.py` now carries its own second-line defaults, applied on an
explicit `is None` test rather than `dict.setdefault` — an explicit `bankroll_usd: null` in YAML
leaves the key *present* with a `None` value, which `setdefault` would keep and which would disable
the daily leg while `enabled: true` still read as armed. Only `enabled: false` may turn that guard
off now.

### 4.4 One more thing this run fixed: the oracle poller would have deadlocked

M4 measured the single-oracle poll rate at **0.642–0.825 polls/s** and flagged that the warmup gate
(60 samples per 120 s window) has only **~22% headroom** at the slow end. Polling N coins
*sequentially* multiplies the period by N and **deadlocks the warmup gate at N ≥ 2** — the bot sits
"warming up" forever and silently never trades, which M4 itself identified as a *worse* failure
than the one being guarded. Two changes:

1. Coins are polled **concurrently**, so the cycle costs ~max(latency) rather than ~sum(latency).
2. The sleep is now **rate-compensating** (`max(0.2, 1.0 − elapsed)` instead of a flat `1.0` *after*
   the fetch), which targets a true 1 Hz.

**Measured in the dry run (§6.1), with two oracles running: 0.992 polls/s per coin**, i.e. 119
samples in the trailing 120 s window against a gate of 60 — headroom **~98%**, up from M4's ~22% on
one oracle. This was M4 open item 3; it is closed for the two-coin case by measurement, and
`scripts/multicoin/dryrun_snipe.py` re-measures it for any larger case.

---

## 5. WHAT WAS REFUTED

Every claim the verification pass killed, in order of how much it would have cost.

| # | claim | status | what is true instead |
|---|---|---|---|
| 1 | **M3 §0/§12: "enable ethereum"** — the run's single actionable recommendation | **REFUTED** | ETH is never stress-tested by the document recommending it. §10's stress table has ALL7/non-BTC/BTC columns only. Per-coin: +9.94¢ → **+0.49¢ at 3 s latency** (t_day 1.94 → 0.10), +3.73¢ at +300 ms feed lag, +3.47¢ stacked, +6.17¢ at a 2 Hz tick. BTC over the same grid is unmoved. **ETH ships to shadow, not to production.** |
| 2 | **M2 §5: matched-pair +$407.00 / residual −$46.94** | **REFUTED** | Actual **+$151.78 / −$133.23**. The published pair sums to $8.37/day against M2's own $0.43/day headline (19×) and contradicts the sentence it was quoted to support. |
| 3 | **M2: "+$0.43/day, t_day 0.34" read as arbitrage P&L** | **REFUTED** | **96.4% of it (+$17.88 of +$18.55) is naked-leg P&L** from 5 events where the abort was not executable and the harness held to settlement — which M2 §8 forbids as a hard rule. Honest figure: **+$0.016/day**, or −$0.54/day if the un-abortable leg is charged as a loss. |
| 4 | **M3 §5: "the competition hypothesis is REJECTED"** | **REFUTED as overclaim** | The n=7 cross-coin regression has **no power**: under H0 the 95th percentile of \|r\| is **0.585**, and every r reported (0.043–0.243) is inside pure noise. The correct word is **UNSUPPORTED**. The version with power is the within-fill regression: coefficient **−0.0658, t = −1.72**, day-clustered over 64 clusters — right sign, not significant. The practical advice (do not chase thinner coins) stands *because there is no positive evidence*, not because the hypothesis was refuted. |
| 5 | **M4 §5: "all guard mutations are caught by the test suite"** | **REFUTED** | **8 of 36 adversarial mutations survived a green 214-test suite** (§4.3). Now closed, plus one real defect found in the writing. |
| 6 | **M3 §12: HYPE ranked #2 by statistical strength; "BTC+ETH+HYPE = +$10.25/day" portfolio row** | **REFUTED** | 13 fills, **7 one-book-in-disguise (53.8%)**, 9 against >5 s stale books, median book age 9.6 s, and **one trade = 52.8% of P&L**. Every quantitative property of that number is an artifact. Struck from the rankings, not footnoted. |
| 7 | **M1 §3.4: "4.40 signals/day → ~5–6 trades/day across the coins"** | **REFUTED by measurement** | Signals measured properly are 3.83/day (within 13% of the screen — the screen was good). **Trades are 2.14/day.** The two-book fill is the filter, and it is worse on the thin coins: BTC converts 64.1% of signals to fills, XRP 38.5%. M1 stated its screen was a frequency upper bound with no fill simulation; that reading was correct. |
| 8 | **M3 §4.3 headline "Fisher exact p = 0.0055" on the \|z\| profile** | **FLAGGED as an overclaim** | The cut point was chosen *after* seeing the bucket table; the neighbouring, equally defensible \|z\| ≤ 2 vs > 5 split gives **p = 0.81**, with no multiplicity correction. §7.1 had already shown the TRAIN-selected band fails walk-forward ($11.84 → $0.49/day). **Do not ship a \|z\| threshold.** |
| 9 | **M3 §4.3: "the strategy inverts where it is most confident" (monotone story)** | **CORRECTED** | A `pd.cut` bin-edge defect silently dropped the 8 fills with \|z\| exactly 0. The `<1` row read 23 fills / 65.2% win / +7.6¢; it is truly **31 fills / 51.6% win / −1.6¢**. The real shape is an **inverted U**: the strategy loses at *both* ends for two mechanically different reasons. |
| 10 | **M4: "every losing trade came from a normal-sized level"** | **CORRECTED** | True at thresholds ≥3×; at the 2× cut exactly one loser (ratio 2.06) sits on the LARGE side. |
| 11 | **M3 §8.2c concurrency: 164 closes / 11 with ≥2 / 1 all-lose hour** | **CORRECTED** | 162 / 10 / **0 all-lose**; worst concurrent hour −$0.43. The observed joint tail is *smaller* than published, so the sizing advice now rests on the flip-rate analysis rather than on an observed cluster. |
| 12 | **M1 §6.3 latency figures (417 / 5,492 / 344 ms)** — the one M1 claim the verifier could not re-verify (live measurement, no stored artifact) | **RE-MEASURED and CONFIRMED in shape** | Re-measured live 2026-07-28, 6 trials each, artifact now stored at `data/multicoin/book_latency.csv`: single `GET /book` **153.9 ms**, 14 sequential GETs **2,241.4 ms**, batched `POST /books` for 14 tokens **157.2 ms**, for 2 tokens **155.4 ms**. Absolute numbers differ (faster network today); **the shape is exactly as claimed — batch cost is independent of token count.** |

**Nothing above changes a decision in the conservative direction's favour by accident.** Items 1, 3
and 5 all push toward *less* trading, which is why the shipped configuration is more conservative
than any of the four audits recommended on its own.

### 5.1 What was confirmed and strengthened

Worth recording, because verification that only ever destroys is not verification:

* **BTC's edge is real and corroborated across vendors.** M4's replay runs on a completely
  different tape (2025-10-11 → 2026-07-12) with an independently written harness. On the 36 BTC
  closes where both produce a fill, **side agreement is 100.0%** and mean absolute fill-price
  difference is 3.9¢; over M3's exact window the M4 tape gives **+15.49¢ on 45 fills** against
  M3's +13.32¢ on 66. Two vendors, two harnesses, same sign and magnitude.
* **The edge has roughly halved and that is visible.** In the M4 tape: 2025-12 +33.9¢, 2026-01
  +35.3¢ (n=96), then 2026-03 → 07 in the +11 to +19¢ range. Real decay, not noise. Worth watching.
* **Two-book fills are genuinely applied**, and BTC/ETH have **zero** one-book-in-disguise fills
  (0/66 and 0/18) by a test neither audit ran.
* **No frozen-book catastrophe.** Max fill-book age across all 173 fills is 91.7 s; BTC's max is
  2.2 s. The overturned 5m result had 7 fills at 355–1,975 s sharing one frozen vendor timestamp.
* **A float32 precision artifact was real and carried 39% of the sample**: exactly 3 fills exist
  only pre-fix, totalling **−$194.75** against a pre-fix sample total of +$493.28. Fixed.

### 5.2 Defects this run found in its own work

Listed here rather than buried in the implementation section, because a run that reports only the
defects it found in *other people's* work is not being straight.

1. **`CircuitBreaker({})` resolved no daily limit at all.** Found while writing the test that closes
   the verifier's five `risk_cfg` mutations. The bankroll/pct defaults lived only in the config
   accessor, so the breaker object itself had `daily_limit = None` — the daily leg silently never
   fires. Shipped behaviour was unaffected (config.py always supplied them) but the second line of
   defence did not exist. Fixed with defaults in `risk.py`, plus the subtlety that an explicit
   `bankroll_usd: null` in YAML must be overridden too — `dict.setdefault` would have kept the
   `None` and left `enabled: true` reading as armed while doing nothing.
2. **The broad `/events` discovery scan ignored `hourly_coins`.** Found by the live dry run: after
   `_HOURLY_RE` widened to seven coins, the scan ingested bnb, hype, solana, xrp and dogecoin
   markets regardless of config. Not unsafe — the coin allowlist refused every one of them and said
   so — but the bot was tracking and cache-warming markets it cannot even price. Fixed, with a
   regression test.
3. **Live books report NEGATIVE ages.** Found by the live dry run: −1.05 s, which is physically
   impossible and therefore proof this host's clock is at least a second behind the CLOB. The
   direction is harmless (the stale-book guard under-blocks); the opposite direction is not, since a
   host drifting *ahead* would make every book look stale and **silently halt all trading**.
   Reported, and NTP is now a stated requirement (§8 item 11).
4. **My own first fix for (3) was decorative.** The mutation checker showed the negative-age clamp
   is a no-op — for any positive bound, `max(0, age) > L` and `age > L` agree on every input. The
   mutation removing it survived, correctly. The clamp stays as intent, the *warning* is what is
   tested, and the code now says which is which. A guard that cannot be mutated is not a guard.

---

## 6. WHAT SHIPPED (code)

All changes are in `bot/`. **Full suite: 307 tests + 23 subtests, all passing** (was 214).
Mutation checker extended from 26 to 54 mutations: **54/54 killed**, zero survivors, zero stale
anchors.

> **Commit note.** I did not run `git commit` at any point. A concurrent process in this repository
> committed my in-flight edits as `152809e` ("M5 multi-coin infrastructure…") while this work was
> still going — the same thing that happened to the M4 run (`940fa5f`), and it is recorded there as
> a blocker too. I have not tried to undo it: the commit is a mid-flight snapshot, later edits are
> still uncommitted in the working tree, and reverting risks destroying another agent's concurrent
> work. **The working tree, not that commit, is the reviewable state** — it is the thing the 307
> passing tests and the dry run below were run against. Someone should decide whether `152809e`
> should stand, be amended, or be split.

| area | change | file |
|---|---|---|
| Market model | `_HOURLY_RE` generalised to all seven coins with a named `coin` capture group; `Market.coin` is a **required** field; `_hourly_slug(coin, dt)`; short families pinned to bitcoin | `polybot/polymarket.py` |
| Feed table | `HOURLY_COIN_FEEDS: coin → (symbol, venue)` with full provenance; `coin_feed()` returns `None` for anything unknown | `polybot/oracle.py` |
| Venue support | `BinanceOracle(venue=…)` — spot (`/api/v3` on data-api.binance.vision) vs USD-M futures (`/fapi/v1` on fapi.binance.com); unknown venue **raises**; `price_at_second` returns `None` on futures (no 1s kline interval) rather than substituting a 1m open | `polybot/oracle.py` |
| Oracle routing | `coin_oracles` dict built only for coins we may price; `_oracle_for` **fails closed**; `_snipe_inputs` refuses rather than guessing | `polybot/engine.py` |
| Batched books | `ClobClientREST.get_books()` over `POST /books`; `Engine._fetch_books()` falls back per token on partial/failed/raising batch, and on any client without the method | `polybot/polymarket.py`, `engine.py` |
| Coin allowlist | `allowed_coins` (default `["bitcoin"]`) enforced independently of `allowed_families` | `polybot/config.py`, `engine.py` |
| Shadow mode | `shadow_coins`; gate placed **after** the signal record so evidence is still collected; both-lists conflict resolves to shadow | `polybot/config.py`, `engine.py` |
| Stale-book guard | `OrderBook.book_ts` / `.age_secs()` / `.tick_size` parsed from the CLOB payload; `book_is_stale()` shared by the decision point and both fill paths | `polybot/polymarket.py`, `fill_engine.py`, `execution.py`, `engine.py` |
| Per-coin caps | `sizing.per_coin_cap_usd`, falling back to the shared cap | `polybot/config.py`, `engine.py` |
| Poller | concurrent multi-oracle poll + rate-compensating sleep | `polybot/engine.py` |
| Breaker defaults | second-line defaults in `risk.py` with an explicit `is None` test | `polybot/risk.py` |
| Observability | boot banner lists every coin's symbol/venue/cap/fill-permission; `status.json` gains `guards.warmup.coins{}` per coin; every signal now records `coin` / `shadow` / `book_age_s`; `main.py markets` prints the resolved MODE per market | `polybot/engine.py`, `ledger.py`, `main.py` |
| Discovery | the broad `/events` scan is filtered by `hourly_coins` (it was ingesting all seven coins after the regex widened — found in the live dry run) | `polybot/polymarket.py` |
| Clock skew | negative book ages clamped to zero and reported once (live books read -1.05 s) | `polybot/fill_engine.py` |

### 6.1 Dry run — the wiring, verified against live gamma / CLOB / Binance

`scripts/multicoin/dryrun_snipe.py` drives the real engine over real discovery,
real oracles and real books, shifting exactly one thing (the close timestamp) so
`tau` lands inside the snipe band without waiting up to an hour for a top-of-hour
close. It is a **wiring** test — the book is priced for a close an hour away
while the model is told the close is 3.5 s away, so the "edge" it prints is an
artifact of the shifted clock and means nothing about P&L.

```
DRY RUN — M5 multi-coin decision path against LIVE gamma / CLOB / Binance
mode            : PAPER
discover coins  : ['bitcoin', 'ethereum']
allowed (fill)  : ['bitcoin']
shadow (no fill): ['ethereum']
max_book_age_s  : 5.0
oracle          : bitcoin   -> BTCUSDT@spot  cap=$250
oracle          : ethereum  -> ETHUSDT@spot  cap=$25

--- warming up (120s; M4 guard 2) — measuring poll rate per coin ---
  t+   10s  bitcoin=12  ethereum=12
  ...
  t+  120s  bitcoin=120  ethereum=120

  poll rate per coin (samples inside the trailing 120s window):
    bitcoin    119 samples / 120s = 0.992 polls/s  (gate needs 60)
    ethereum   119 samples / 120s = 0.992 polls/s  (gate needs 60)

  warmup: ready=True reason=warm samples=119/60 uptime=132/120s

--- decision path on 2 live 1h market(s) (close shifted to tau=3.5s) ---

  [bitcoin] bitcoin-up-or-down-july-28-2026-8am-et
    oracle       : BTCUSDT@spot  last=63477.99
    book UP   : best_ask=0.58 levels=42 tick=0.01 clob_age=-1.05s stale=False
    book DOWN : best_ask=0.43 levels=57 tick=0.01 clob_age=-1.05s stale=False

  [ethereum] ethereum-up-or-down-july-28-2026-8am-et
    oracle       : ETHUSDT@spot  last=1877.78
    book UP   : best_ask=0.58 levels=25 tick=0.01 clob_age=-1.01s stale=False
    book DOWN : best_ask=0.43 levels=26 tick=0.01 clob_age=-1.01s stale=False

--- signals recorded ---
  bitcoin-up-or-down-july-28-2026-8am-et
    coin=bitcoin  shadow=False  book_age_s=-1.085  side=up  fair=0.9800 ask=0.5800
  ethereum-up-or-down-july-28-2026-8am-et
    coin=ethereum shadow=True   book_age_s=-1.11   side=up  fair=0.9800 ask=0.5800

--- fill attempts recorded ---
  bitcoin-up-or-down-july-28-2026-8am-et   outcome=filled  shares=426.17  cost=$250.00

--- the safety assertion this dry run exists for ---
  fills dispatched for a SHADOW coin: 0 (correct)
```

**What this confirms, live:** BTC is priced off BTCUSDT (63,477.99) and ETH off
ETHUSDT (1,877.78) — the routing is real, not a code path that happens to be
untaken; both books arrive in one batched call; both signals are recorded with
`coin` / `shadow` / `book_age_s`; **ethereum produced a real signal and zero
fills**; bitcoin filled normally through the 1.5 s latency and book re-fetch.

**And the poll-rate fix is confirmed: 0.992 polls/s per coin with TWO oracles
running**, against M4's 0.642–0.825 with one. Warmup headroom against the
60-sample gate goes from ~22% to ~98%. §4.4's deadlock is closed by measurement,
not by argument.

**One real defect found by the dry run, now fixed.** The books reported
**negative ages (−1.05 s)** — impossible, and therefore proof that this host's
clock is at least a second behind the CLOB. The direction is safe (the guard
under-blocks by the offset) but the opposite direction is not: a host drifting a
few seconds *ahead* would make every book look stale and **silently halt all
trading**, which is the same failure class as the warmup deadlock. Negative ages
are now clamped to zero, clock skew is reported once, loudly, and it is stated
plainly that **the production host must run NTP** — no single timestamp can
detect a clock that runs ahead.

A second defect the dry run found: the broad `/events` discovery scan
pattern-matched all seven coins regardless of `discovery.hourly_coins`, so the
bot tracked and cache-warmed bnb/hype/solana/xrp/dogecoin markets. Nothing
unsafe happened — the coin allowlist refused every one of them, loudly, which is
the guard working — but discovery breadth must come from one place. Fixed, with
a regression test.

The mutation checker also now treats a **stale anchor** — a mutation whose target
text no longer exists in the source, so the mutation never actually runs — as a
failure rather than a silent pass. It earned that immediately: the clock-skew
edit above moved the `book_is_stale` return line, and the mutation meant to prove
that predicate is tested went unapplied. Silently not testing a guard is the same
failure as not catching it.

It then earned its keep a second time, against me. I had added a mutation for the
negative-age "clamp" and it **survived** — correctly, because `max(0.0, age) > L`
and `age > L` are *identical* for every real `age` whenever `L > 0`, and the
function returns early when `L <= 0`. **The clamp is a no-op, not a guard.** The
behaviour that actually protects anything is the skew *warning*, so that is what
the checker mutates now, and `fill_engine.book_is_stale` says so in as many
words. Recording it here rather than quietly deleting the mutation: "we removed
the mutation" and "we tested the guard" are different sentences.

### 6.2 What the 1h BTC strategy itself actually gained

The brief asked for the 1h strategy to be made better. Honestly: **no change raises its expected
$/day.** Three raise its robustness, and one candidate improvement was examined and rejected.

1. **A denser volatility buffer.** `sigma_1s` is the std of 1 s log returns over the trailing 120 s
   of oracle polls, and the poller now runs at a measured **0.992 polls/s** instead of 0.642–0.825.
   That is ~45% more samples behind every `fair` the bot computes, and it is the same statistic the
   warmup guard exists to protect. Better inputs, same rule.
2. **~0.15 s more fill margin at the same `tau_lo`.** One batched `POST /books` (≈155 ms) replaces
   two sequential GETs (≈308 ms), so the gap between the gate and the order arriving shrinks. Spent
   on margin, not on a wider window — see below.
3. **A stale-book reject that never fires on BTC** (its worst fill book in 66 fills was 2.2 s
   against a 5.0 s bound) but is armed if the book behaviour ever changes.

**Rejected: the `|z|` band.** BTC's `|z| ≤ 5` subset is 52 fills at +20.4¢/share against +13.3¢
overall, which is the most interesting number in the project. It is not shippable: the
TRAIN-selected band collapses out of sample ($11.84 → $0.49/day) and the headline p-value moves
0.0055 → 0.81 under a neighbouring cut point. §7 item 3 says what to do instead.

### 6.3 Deliberate non-changes

* **`snipe_min_tau_secs` stays at 2.5**, even though batching bought back ~0.15 s. Spending it on
  frequency (tau_lo → 2.0) does not survive the tail: the worst single fetch in the same
  measurement run was **704 ms**, giving 2.0 − 0.70 − 1.5 = **−0.2 s** — an order arriving *after*
  the close on a cold connection, which is the exact failure 2.5 was introduced to fix. The
  batching win is spent on **robustness**, raising the median fill margin from +0.685 s to +0.845 s.
* **No `|z|` band shipped.** §5 item 8.
* **No breaker re-calibration.** It is correct for one coin, and one coin is what fills.

### 6.4 The rule for promoting ethereum out of shadow

**First, what the shadow tape can and cannot say.** A shadow coin records a signal — side, `fair`,
`ask`, `book_age_s`, and the market resolves normally — so a **counterfactual** per-share P&L is
computable as `(1 if the side won else 0) − ask − fee(ask)`. That number is **optimistic by exactly
the filter this project keeps learning about the hard way**: it is a *signal-time* fill. The real
bot fills against a book re-fetched 1.5 s later, and ETH converts only **46.2%** of signals into
fills (BTC 64.1%). So the shadow counterfactual is an upper bound, and the promotion bar has to be
set above where the honest number would need to be, not at it.

Pre-committed, so it cannot be rationalised later. Promote **only if all five hold**:

1. **≥ 40 shadow signals** accumulated (~180 days at 0.22 signals/day — slow on purpose; M3 §11
   says a per-trade *t* needs ~170 days and a day-clustered one ~678).
2. Shadow **counterfactual** ¢/share **≥ +15¢** with a day-clustered *t* ≥ 2.0 — deliberately far
   above the +9.94¢ backtest, because this figure is a signal-time upper bound (above) and because
   ETH's backtest number itself did not survive stress.
3. The same tape **survives the stress grid that killed the backtest**: recompute at +300 ms feed
   lag and a 3 s round trip; require **> +5¢** on both.
4. **Concentration check**: ≥ +8¢ counterfactual after dropping the best 3 days. ETH's backtest
   best day was 38.6% of its P&L and its best 3 were 68.9%; a repeat of that is a coin flip
   wearing a t-statistic.
5. Re-run `scripts/m4/replay_depth.py` **for ethereum** — the adverse-size OFF default rests on
   BTC-1h evidence only.

Then: `allowed_coins: ["bitcoin", "ethereum"]`, remove it from `shadow_coins`, **keep the $25 cap**,
and re-derive the breaker thresholds for two coins *first*.

**The cheaper intermediate step, if 180 days is too slow.** Let a shadow coin run the paper fill
*simulation* (which is not a trade — the bot is in paper mode) and tag the attempt so it is excluded
from P&L aggregation and, critically, from the circuit breaker's daily-loss and losing-streak
inputs. That converts the shadow tape from a signal-time upper bound into a genuine two-book
counterfactual and makes bar 2 meaningful at its honest level. It was **not** done here for one
reason: it requires changing what the circuit breaker reads, and the breaker is the guard that
bounds real money. Widening its inputs to filter simulated rows is exactly the kind of change that
should land on its own, with its own mutation tests, not as a rider on a multi-coin refactor.

---

## 7. RANKED NEXT ACTIONS

Ranked by expected value per unit of effort, with the reasoning stated rather than implied.

1. **Do nothing new to the strategy; let bitcoin run and let ethereum shadow.** The single most
   valuable thing available is *more BTC samples*, because BTC is the only coin whose edge has ever
   been confirmed and its measured decay (+34¢ → +15¢ over eight months) is the largest live risk
   to the whole enterprise. Cost: zero.

2. **Extend the tape backwards to 2026-03-15 and buy a fresh out-of-sample window.** The TEST half
   is now spent — M3 §7.1 used it on the `|z|` guard — so *no further rule selection is
   statistically legitimate* until a new window exists. `markets.parquet` already reaches
   2026-03-15, which roughly doubles the sample. This is the precondition for items 3 and 4.

3. **Find the mechanism behind the inverted-U `|z|` profile on BTC.** This is the highest-value
   *research* lead in the project because it sits on the largest sample it has: BTC's `|z| ≤ 5`
   subset is 52 fills at **+20.4¢/share** against +13.3¢ for the whole. But it is a lead, not a
   rule — the TRAIN-selected band collapses out of sample ($11.84 → $0.49/day) and its p-value
   moves 0.0055 → 0.81 under a neighbouring cut. **Find what the counterparty knows at the top end
   before fitting any threshold.** Candidates: order-book-derived pricing, cross-exchange quotes,
   sub-tick information. None tested.

4. **Fetch `book_snapshot_5` for the hourly markets and redo the capacity table.** Every $ figure
   in M3 is a *lower bound* because the hourly quote tape is top-of-book only, so every fill is a
   one-level walk. Median fillable notional of $6–12 per signal is the binding constraint on
   revenue — bigger than the edge, bigger than frequency. If real depth is materially better, the
   $/day arithmetic changes for BTC *today*, with no new coin and no new risk.

5. **Run `scripts/multicoin/dryrun_snipe.py` on the production host** — for the poll rate (this
   sandbox measured 0.992 polls/s per coin; the number that raised the alarm, 0.642, was a
   production number) and, more importantly, **to check the clock**. The dry run here found this
   host reading ~1 s *behind* the CLOB, which is the harmless direction; a host running *ahead*
   would make every book look stale and silently halt trading, and no single timestamp can detect
   that. **Confirm NTP is running before this ships anywhere.** Cheap; closes M4 open item 3.

6. **Re-derive the circuit-breaker thresholds for a two-coin portfolio** — *before* any promotion,
   not after. √N understates it because of the +0.61 correlation. M4 open item 2.

7. **Leave the dominance arb alone unless the round trip drops below ~500 ms.** At 0 ms it is
   +$20.93/day with t_day 4.25 and zero leg risk, so the idea is not dead — the *execution* is.
   Revisit only alongside a genuine latency project, and use the fee-aware gate (required crossing
   is 3.6¢ at p≈0.5 but only 0.6¢ at p≈0.1/0.9), which lifts pair win% to 100% and was never used.

8. **Never** enable HYPE. $45 median volume/market, ~$2.30 liftable capacity, no reachable live
   feed, and a headline number that is entirely artifact. The work would cost more than the coin
   can ever return.

---

## 8. THE HONEST LIMITS OF WHAT THIS RUN ESTABLISHED

1. **This run did not increase revenue.** Expected $/day is unchanged. It removed five ways to lose
   money, built the machinery to test the sixth safely, and repaired eight holes in the guard suite.
   That is worth doing; it is not what was hoped for.

2. **The frequency problem is not solved and this document does not know how to solve it.** One
   market closes per hour; only **5.38%** of BTC hourly closes produce a signal at shipped
   parameters and only **64.1%** of those convert to a fill (M3 §3), giving 0.815 fills/day; and
   parameter tuning was already measured across a full grid. More hourly coins was the best
   available idea and it failed on edge quality, not on execution.

3. **n is small everywhere except BTC.** 66 BTC fills; 29 BNB, 21 SOL, 18 ETH, 15 XRP, 13 HYPE, 11
   DOGE. The *pooled* non-BTC result (107 fills) is the only non-BTC statement with real power and
   it says "indistinguishable from zero". **No per-coin verdict in §1 is more certain than its CI**
   — DOGE's is [−32.8, +30.0]. "DO NOT ENABLE" for the middle coins means *no evidence of edge*,
   not *proof of no edge*.

4. **81 days of one regime** (2026-05-08 → 07-27) for every non-BTC coin. BTC has now been measured
   on three tapes and periods (+9.8¢ / +13.3¢ / +21.5¢ live) so it is stable in sign; every other
   coin has exactly one measurement.

5. **Every $ figure is a lower bound** (top-of-book tape only), and every ¢/share figure is not.

6. **The TEST half is spent.** Any further rule selection needs the new out-of-sample window in
   §7 item 2. This is a real constraint, not a formality.

7. **Shadow mode is an operational check, not a statistical one, and its P&L number is an upper
   bound.** A shadow signal is never filled, so the only P&L computable from it is a *signal-time*
   counterfactual — and ETH converts just 46.2% of signals into real fills. The gap between those
   two is the single largest filter in this whole strategy. At 0.22 signals/day ETH also needs
   ~180 days to reach 40 signals, and M3 §11 is explicit that no non-BTC coin can be validated by
   live paper trading in a useful timeframe. Shadow mode's honest job is to catch *wiring* and
   *execution* errors early and to start the clock — not to prove an edge. §6.4 states the bar it
   would have to clear and the cheaper intermediate step that would make that bar meaningful.

8. **Concurrency under real load is still unmeasured.** All seven coins close on the hour, so a
   multi-coin bot faces up to 7 simultaneous decisions in one 2.5–5 s window. The batched fetch
   (155 ms for 14 tokens) suggests this is fine and it was measured *idle*, not under decision load.
   With one coin filling and one shadowing, the exposure today is 4 tokens.

9. **The live-mode order path remains partially unverified**, unchanged from before: `_place_live_order`
   does not parse a filled-share count out of the CLOB response, so LIVE fills are provisional until
   reconciled. Nothing in this run touched that, and nothing in this run should be read as clearance
   to go live.

10. **`bankroll_usd: 1250` is still a derived config number**, not a measured account balance — it
    is `max_open_notional + per_event_cap_usd`. The daily stop is 8% of whatever that says, so if
    real capital is ever attached this must be set to the actual bankroll or **the most important
    stop in the system is silently mis-sized.**

11. **The stale-book guard inherits the host's clock.** It compares the CLOB's timestamp to local
    time, so a clock running ahead inflates every age. Negative ages are clamped and skew is
    reported (§6.1), but a *forward* drift is indistinguishable from genuinely stale books and
    would halt trading. NTP on the production host is a hard requirement of shipping this guard,
    not a nicety.

12. **`max_book_age_s: 5.0` is calibrated on BTC, where it never binds.** That is deliberate — it
    changes nothing validated — but it also means the threshold itself is *untested against a real
    blocking event*. Its value should be re-derived from the coin's own book-age distribution
    before any coin whose p90 age approaches it (SOL 6.97 s, BNB 9.14 s) is ever enabled.

---

## 9. Files

| path | what |
|---|---|
| `bot/polybot/polymarket.py` | seven-coin slug model, `Market.coin`, batched `POST /books`, book timestamp/tick parsing |
| `bot/polybot/oracle.py` | `HOURLY_COIN_FEEDS` verified feed table, spot/futures venue support |
| `bot/polybot/engine.py` | per-coin oracles, fail-closed routing, coin allowlist, shadow gate, batched fetch, stale-book guard, concurrent poller |
| `bot/polybot/config.py` | `hourly_coins` / `allowed_coins` / `shadow_coins` / `per_coin_cap_usd` / `max_book_age_s`, all defaulting to today's behaviour |
| `bot/polybot/fill_engine.py`, `execution.py` | `book_is_stale` in both the paper and live order paths |
| `bot/polybot/risk.py` | second-line circuit-breaker defaults |
| `bot/polybot/ledger.py` | every signal records `coin` / `shadow` / `book_age_s` |
| `bot/polybot/main.py` | `markets` prints the resolved MODE per market; `status` prints per-coin warmup |
| `bot/README.md` | "Which coins trade" section; guard list updated to eight |
| `bot/config.yaml` | the decision, encoded, with the measured reason beside every value |
| `bot/tests/test_multicoin.py` | **new** — 88 tests incl. `TestNeverPricesOffTheWrongCoin` |
| `bot/tests/test_risk_guards.py` | 78 -> 83 tests; the 5 new ones close the surviving guard mutations |
| `scripts/m4/mutation_check.py` | 26 -> 54 mutations (verifier's 8 + 2 second-line + M5's 18); a stale anchor now FAILS the run, and a label filter allows partial re-runs |
| `scripts/multicoin/measure_book_latency.py` | re-measures M1 §6.3 and stores the artifact |
| `scripts/multicoin/dryrun_snipe.py` | end-to-end wiring dry run against live gamma / CLOB / Binance (§6.1) |
| `data/multicoin/book_latency.csv` | the artifact the verifier could not find |
