# M4 — the three missing risk guards

**Scope.** An audit found three protections the bot lacked, each mapping to a
failure mode this project has already suffered. All three are now implemented
in `bot/polybot/`, config-driven, backward compatible, and covered by tests
that provably fail when the guard is removed.

**Headline result.** Two of the three shipped **ON**. The third — the
adverse-size filter — shipped **OFF by default because the measurement refuted
its premise**, and the evidence is in §1. It is fully implemented, wired into
both the paper and the LIVE order path, and one config line away from active.

| Guard | Where | Default | Basis for the default |
|---|---|---|---|
| 1. Adverse-size filter | `strategy.close_snipe.adverse_size` | **OFF** | 6,523 1h closes: large offers won *more*, not less (§1) |
| 2. Warmup after restart | `strategy.close_snipe.warmup` | **ON** — 60 samples + 120s | 19,569 evaluation ticks: cold sigma manufactures 25.5% extra, much worse trades (§2) |
| 3. Daily loss limit + streak brake | `risk:` | **ON** — $100/day, 4 in a row | 121 trading days: worst day −$41.47, longest streak 3 (§3) |

Nothing pre-existing was weakened. `fair_cap 0.98`, `sigma_1s_floor 8e-6`,
`max_walk_above_best 0.03`, the `[2.5, 5.0]s` tau band, the three live-mode
gates and `allowed_families: ["1h"]` are all unchanged and now have explicit
regression tests (`TestNoExistingGuardWeakened`).

---

## 0. What was measured, and on what

The replay in `scripts/m4/replay_depth.py` reproduces the **shipped** bot on
historical tape:

- tau band `[2.5, 5.0]s` → integer signal seconds tau ∈ {3, 4, 5}
- `edge_min 0.03`, price ∈ (0.30, 0.99), `vol_window 120s`,
  `sigma_1s_floor 8e-6`, `fair_cap 0.98`
- `fair = Φ(ln(S_t/S_open) / (sigma_1s·√tau))`, one entry per window, first side
- fill evaluated against the book **2 seconds after the signal** (latency
  1500 ms → the next full second at or after +1.5 s) with `fair` frozen at
  signal time — the same honesty rule as the live paper engine

Data: `data/data/processed/daily/1h/quotes` (275 days of top-of-book),
`data/data/processed/binance/klines_1s`, `data/windows_all.parquet`
(`result_id` per window).

**Sanity check against reality:** 6,523 windows → 343 signals (5.3%) → **236
fills = 0.87 fills/day**, versus the live paper run's ~0.9 trades/day. Realized
+25.4c/share at 94.1% win, versus the live run's +21.5c/share at 87.5% over 8
trades. The replay reproduces the live tape's frequency and is in the right
place on economics, so conclusions drawn from it transfer.

Significance throughout is a **day-clustered bootstrap** (trades on the same
UTC day resampled together) because BTC regimes cluster within a day and
per-trade t-stats overstate confidence. For binary win/lose comparisons the
more conservative Fisher exact test is also reported.

---

## 1. Adverse-size filter — implemented, measured, shipped OFF

### 1.1 The statistic

A fixed share count is useless across coins (a BTC hourly book quotes tens of
shares near the close, a DOGE book thousands), so the guard is relative:

```
size_ratio = level.size / median(last N observed in-band ask-level sizes for this family)
```

The **in-band restriction is load-bearing, not cosmetic.** Median best-ask size
by price bucket, last 60 days of 1h quotes, final 10s before close:

| price bucket | (0,0.1] | (0.1,0.3] | (0.3,0.5] | (0.5,0.7] | (0.7,0.9] | (0.9,0.95] | (0.95,0.99] | (0.99,1.0] |
|---|---|---|---|---|---|---|---|---|
| median size | **24,190** | 14.0 | 11.2 | 10.6 | 19.4 | 32.3 | 37.5 | 141.4 |

The ~free "lottery ticket" side of an already-decided market carries three
orders of magnitude more size than a genuinely contested quote. A reference
pooled across all prices scores a perfectly normal contested offer at 0.01x —
the statistic becomes noise. (This was the first version I measured; it
produced an incoherent ratio distribution with median 0.01 and had to be
discarded.) Only levels inside `(price_min, price_max)` — the band the
strategy is allowed to buy in — are recorded.

Samples come from books the engine **already fetches** inside the snipe window
plus the fill-time re-fetch, so the statistic adds zero REST traffic. The
replay emulates exactly that: best-ask size at tau ∈ {5,4,3,2,1}, in-band only,
rolling deque of the last 200 observations, reference = median, computed
strictly from windows *before* the one being traded (no lookahead), inert until
30 samples exist.

Observed ratio distribution on the 233 fills that had an established reference:
p50 **0.80x**, p75 **3.4x**, p90 **11.9x**, max 2273x. Median reference size
58.6 shares.

### 1.2 The result: the hypothesis is refuted, with the sign reversed

Shipped parameters, 233 fills, 117 days, family-rolling per-side reference:

| threshold | LARGE n | LARGE win | LARGE c/share | normal n | normal win | normal c/share | diff (day-clustered) | p |
|---|---|---|---|---|---|---|---|---|
| >2x  | 77 | 0.987 | +32.67 | 156 | 0.917 | +21.79 | **+10.88c** | 0.001 |
| >3x  | 62 | **1.000** | +34.40 | 171 | 0.918 | +22.12 | **+12.29c** | 0.000 |
| >5x  | 44 | **1.000** | +38.09 | 189 | 0.926 | +22.43 | **+15.66c** | 0.000 |
| >8x  | 29 | **1.000** | +37.08 | 204 | 0.931 | +23.72 | **+13.36c** | 0.001 |
| >10x | 28 | **1.000** | +36.72 | 205 | 0.932 | +23.84 | **+12.88c** | 0.002 |

**Every one of the 14 losing trades came from a normal-sized level.** Worst
LARGE (>5x) trade: **+$14.91**. Worst normal trade: **−$45.26**.

### 1.3 Confound controls

Large offers sit at lower prices (0.60 vs 0.69 average), and a lower price
mechanically pays more per share when it wins. Three controls
(`scripts/m4/controls_depth.py`):

1. **Within price bucket.** In (0.45, 0.60] — where most of the mass is —
   LARGE n=28 at 100% win / +46.97c vs normal n=57 at 87.7% / +35.05c, diff
   **+11.92c, p=0.002**. In (0.60,0.75] and (0.75,0.99] the difference is
   ~0 (−1.9c p=0.325 and +0.6c p=0.863). Nowhere is it negative and
   significant.
2. **OLS with price as a covariate.** `pnl_per_share ~ log10(size_ratio) + fill_px`:
   `log10(size_ratio)` coefficient **+0.0511 (t = +2.87)**, `fill_px`
   −0.5485 (t = −6.58). Bigger is *better* after controlling for price.
3. **Binary win/lose (price-free).** LARGE 44/44 vs normal 175/189 (92.6%).
   Day-clustered p<0.001; **Fisher exact p = 0.078** — the honest read is
   "suggestive, not established" for the win-rate leg specifically. The
   per-share leg is significant on both.

**Independent higher-power replication.** Because 236 trades is thin, the same
replay was re-run with a deliberately loosened filter (`edge_min 0.01`, taus
3–8) purely to enlarge the sample: **359 fills, 168 days**. The OLS coefficient
reproduces almost exactly — `log10(size_ratio)` **+0.0506 (t = +2.58)** — and
LARGE offers lose 5/72 (6.9%) versus normal 36/287 (12.5%). Same sign, same
magnitude, on a sample built from different trades.

Stability: first half of the sample LARGE 35 trades 100% win / +40.35c vs
normal +29.63c; second half LARGE 9 / 100% / +29.31c vs normal 87% / +11.96c.
The gap narrows with the general decay of the edge but does not invert.

### 1.4 What enabling it would have cost

| threshold | mode `skip` | mode `cap` |
|---|---|---|
| 3x | $2,212 of $10,351 (**21%**) | $6,762 (65%) |
| 5x | $3,235 (**31%**) | $8,784 (85%) |
| 8x | $5,400 (**52%**) | $9,638 (93%) |
| 10x | $5,631 (**54%**) | $9,951 (96%) |

At the 8x default, `skip` mode discards **48% of realized PnL** to prevent
losses that never occurred. That is the case for shipping OFF.

### 1.5 Why the settle_sweep analogy does not carry

The audit's premise came from a real event: `settle_sweep` took 3 fills and
lost 3-for-3 to adverse selection on 2026-07-15. But the mechanism there was
**an information asymmetry about the resolution feed** — the sellers were
watching Chainlink, the bot was reading Binance, and the only cheap "winner"
asks left standing were the ones the market knew were actually losers. Size was
incidental; the poison was the wrong feed.

`close_snipe` has no such asymmetry. Its input is the *public* BTC spot price.
A counterparty resting a large cheap offer seconds before the close is a market
maker whose quote is stale relative to spot — which is precisely the edge —
not someone who knows something the bot does not. The measurement says exactly
that: big offers are stale liquidity, and taking them is the *better* half of
the strategy.

### 1.6 Power, and when to revisit

With 236 trades and 14 losses this test can detect a catastrophic effect (the
settle_sweep event was ~−100c/share) but not a subtle one; the 95% CI on a
20-trade subgroup is roughly ±11c/share. Two things would change the answer:

- **A new coin.** ETH/SOL/DOGE/XRP hourly books have different maker
  populations. The statistic is scale-free and the code is per-family; re-run
  `scripts/m4/replay_depth.py` on that family's tape before enabling or before
  assuming it stays off.
- **Live mode.** Everything above is a paper/backtest population. A real taker
  changes who is willing to rest size against it.

Turning it on is one line: `strategy.close_snipe.adverse_size.enabled: true`.
`mode: cap` is the gentler setting (85% of PnL retained at 5x) and is the
default mode for that reason.

### 1.7 Implementation notes

- `bot/polybot/depth.py` — `DepthTracker` (thread-safe rolling per-family
  history, in-band only, `reference()` returns `None` below `min_samples`) and
  `max_level_shares()`, the single place that answers "does the filter bind
  right now?".
- `bot/polybot/fill_engine.py::walk_asks` — `max_level_shares` +
  `anomalous_mode` ∈ {`cap`, `skip`}. `cap` takes at most the ceiling and keeps
  walking; `skip` takes nothing from that level and keeps walking.
- **`best_price` is anchored on the first PRICE-eligible level, before the size
  filter can remove it.** Otherwise skipping the top-of-book level would
  silently re-baseline `max_walk_above_best` one level deeper — a size guard
  that quietly widens the slippage guard. There is a dedicated test
  (`test_skip_cannot_rebaseline_the_walk_bound`) and a mutation for it.
- Threaded into `ExecutionRouter.place_taker_buy` **and**
  `_place_live_order` — a guard that only exists in the simulator is not a
  guard.
- New fill outcome `adverse_size_blocked`, distinct from
  `book_moved_no_edge`: the edge was there, our own guard declined it. Recorded
  in the ledger `meta_json` with the ceiling, the mode, levels capped/skipped
  and shares suppressed, and raised as an `adverse_size` status event.
- Reference and sample counts appear in `status.json` under
  `guards.depth_reference` even while the filter is off, so the statistic can
  be watched before anyone flips the switch.

---

## 2. Warmup after restart — ON, threshold derived from data

### 2.1 The mechanism

`fair = Φ(ln(S_t/S_open) / (sigma_1s·√tau))`. `sigma_1s` is the std of 1s log
returns over a rolling 120s buffer of oracle polls. After a restart (deploy,
watchdog, OOM, crash) that buffer is nearly empty — and
`BinanceOracle.rolling_log_return_std` requires only `len(series) >= 3` and two
returns, so **the bot was willing to trade three seconds after boot on a sigma
built from two returns**. Too small a sigma inflates |z| and pushes `fair`
toward `fair_cap`, manufacturing edge. That is the mechanism behind this
project's first −$25 loss.

### 2.2 (a) How wrong is a cold sigma?

`sigma_n / sigma_120` over 19,569 evaluation ticks on 6,523 closes
(`scripts/m4/warmup_sigma.py`):

| n | p05 | p25 | median | p75 | p95 | P(<0.7x) | P(<0.5x) |
|---|---|---|---|---|---|---|---|
| 3 | 0.098 | 0.199 | **0.355** | 0.814 | 2.747 | 0.717 | **0.626** |
| 5 | 0.118 | 0.247 | 0.470 | 1.000 | 2.922 | 0.631 | 0.524 |
| 10 | 0.160 | 0.346 | 0.662 | 1.096 | 2.605 | 0.523 | 0.390 |
| 20 | 0.220 | 0.476 | 0.813 | 1.167 | 2.096 | 0.418 | 0.269 |
| 30 | 0.278 | 0.570 | 0.882 | 1.183 | 1.807 | 0.351 | 0.192 |
| 45 | 0.355 | 0.680 | 0.944 | 1.178 | 1.562 | 0.268 | 0.125 |
| 60 | 0.461 | 0.791 | **1.000** | 1.193 | 1.403 | 0.176 | 0.065 |
| 90 | 0.626 | 0.904 | 1.011 | 1.111 | 1.157 | 0.078 | 0.022 |

The bias is in the dangerous direction: at n=3 the estimate is **half or less**
of the true value 63% of the time.

### 2.3 (b) How much does that move `fair`?

|fair(sigma_n) − fair(sigma_120)| at traded taus:

| n | median | p99 | P(>0.03) | P(>0.10) |
|---|---|---|---|---|
| 3 | 0.0000 | 0.185 | 3.34% | 2.18% |
| 30 | 0.0000 | 0.099 | 2.61% | 0.96% |
| 60 | 0.0000 | 0.056 | 1.97% | 0.37% |
| 90 | 0.0000 | 0.027 | 0.79% | 0.11% |

Median error is zero because near the close `fair` is usually saturated at
`fair_cap` under both sigmas — **the fair cap absorbs most of the warmup
error**, which is one reason this never produced a visible disaster. The
residual 2–3% of ticks is where the money is, and it is concentrated exactly on
the uncertain closes the strategy trades.

### 2.4 (c) The number that sets the threshold: decision error

Signals that exist **only** because sigma was cold, against the warm tape
(243 trades, 93.4% win, **+25.02c/share**) — `scripts/m4/warmup_decision.py`:

| n | cold-only trades | contamination | cold-only win | cold-only c/share | diff vs warm | p |
|---|---|---|---|---|---|---|
| 3 | 62 | **+25.5%** | 0.919 | +9.21 | **−15.81c** | 0.000 |
| 5 | 51 | +21.0% | 0.843 | +3.66 | −21.36c | 0.000 |
| 10 | 37 | +15.2% | 0.811 | +1.99 | −23.03c | 0.000 |
| 20 | 29 | +11.9% | 0.862 | +5.87 | −19.15c | 0.000 |
| 30 | 20 | +8.2% | 0.800 | +0.53 | −24.49c | 0.000 |
| 45 | 15 | +6.2% | 0.733 | **−3.83** | −28.84c | 0.000 |
| 60 | 8 | **+3.3%** | 0.875 | +1.18 | −23.84c | 0.000 |
| 90 | 6 | +2.5% | 1.000 | +13.02 | −11.99c | 0.002 |
| 120 | 0 | 0% | — | — | — | — |

The current, unguarded behaviour (n=3) inflates the tape by a quarter with
trades worth a third of a normal one — and at n=45 the cold-only cohort is
outright **loss-making**. Cold-sigma trades are significantly worse at every n.

### 2.5 The cost of waiting is ~zero, so the threshold is set by robustness

At 0.884 warm trades/day:

| warmup | trades forgone per restart |
|---|---|
| 60s | 0.00061 |
| 120s | 0.00123 |
| 600s | 0.00614 |

Even a ten-minute lockout costs six thousandths of a trade per restart. So the
threshold is not an opportunity-cost trade-off — it is chosen purely for
robustness, and the binding constraint is **the opposite failure**: a threshold
so high the bot never becomes warm and silently never trades.

**Shipped: `min_oracle_samples: 60`, `min_uptime_secs: 120`.**

- 60 samples in a 120s window cuts contamination from 25.5% to 3.3%.
- It is deliberately **not 90 or 120**. The oracle loop polls at
  `(1.0s wait + fetch latency)`, and the dry run below measured a real steady
  state of **99 samples per 120s (0.825 polls/s)**. A 90-sample gate would sit
  9 samples from a permanent lockout on a slightly slower link; 60 tolerates an
  effective 2.0s poll period.
- `min_uptime_secs: 120` — one full vol window of wall clock — is an
  independent condition, because a burst of duplicate polls after a network
  stall can satisfy a sample count but not a clock, and because it guarantees a
  discovery pass has completed.

### 2.6 Implementation notes

- `bot/polybot/risk.py::WarmupGate`. `started_at` is process start, so a deploy
  or watchdog restart resets it — that is the point.
- `BinanceOracle.n_samples()` and `ChainlinkOracle.n_samples()` count samples
  in the trailing window **anchored on the wall clock, not on the newest
  print**. A poller that died 10 minutes ago still *holds* 120 points; anchored
  on the newest point that reads as a full buffer, anchored on now it correctly
  reads 0. Tested and mutated.
- An oracle with no `n_samples` method, a `None` oracle, or one that raises are
  all treated as **0 samples**, never as warm.
- Checked in `_maybe_snipe` **before** any book fetch, so a warming process also
  stops burning REST calls it cannot act on.
- Logged every 15s from the tick loop (not only when a snipe window is open):
  after a restart the next 1h close can be an hour away, and "silently not
  trading" must never look like "nothing to trade". Completion is announced
  once at INFO.

---

## 3. Daily loss limit / circuit breaker — ON

### 3.1 Calibration from the tape

236 fills over 121 trading days at shipped parameters:

| statistic | value |
|---|---|
| worst single day | **−$41.47** |
| days worse than −$25 | 2 of 121 (1.7%) |
| days worse than −$50 | **0 of 121** |
| longest consecutive-loss run | **3** (once; every other run was 1) |
| trades/day | median 1, mean 1.95, max 13 |

**Shipped: `bankroll_usd 1250`, `max_daily_loss_pct 8.0` → $100/day;
`max_consecutive_losses: 4`.** Neither would have fired once in 121 days. That
is deliberate: this is a breaker for genuine breakage (wrong feed, wrong side,
regime change), not a variance throttle. `bankroll_usd: 1250` is the bot's true
exposure ceiling — `max_open_notional` ($1,000) is evaluated *before* the new
trade is sized, so the real ceiling is `+ per_event_cap_usd` ($250).

One full-clip loss (~−$250 at price 0.5) trips the daily limit immediately.
That has never happened at these parameters (largest observed single loss:
−$45.26), and if it does, stopping for the day and making a human look is the
correct response. `python -m polybot.main resume` releases it in one command.

**These are calibrated for ~2 trades/day on one coin. Re-derive them when the
multicoin work lands** — at ~6 coins the daily loss distribution widens by
roughly √6.

### 3.2 Semantics

- **Realized only.** Computed from `resolutions` since UTC midnight; unrealized
  marks never trip it.
- **Auto-reset at UTC midnight** falls out of the arithmetic (both the PnL sum
  and the streak are scoped to `>= utc_midnight()`), so there is no cron, no
  timer, and no state anyone has to remember to clear.
- **Streaks count resolved markets, not fill rows.** A market that filled on
  both a close_snipe and a settle_sweep leg resolved once and is one outcome.
- **Tighter limit wins.** If both `max_daily_loss_pct` and
  `max_daily_loss_usd` are set, the smaller applies — combining two safety
  limits the loose way is how guards get quietly widened.
- **Applies to every strategy that opens a position**, close_snipe and
  settle_sweep. A daily stop one strategy can walk around is not a daily stop.
- Checked before the book fetches *and* re-checked immediately before the fill
  is dispatched, because a resolution can land in between. Evaluated on the
  **wall clock**, not the tick's `now`, since UTC days and realized PnL are
  wall-clock facts.

### 3.3 The manual override is not an off switch

`python -m polybot.main resume` writes `bot/data/risk_override.json`. It is:

- **scoped to one UTC day** — an override from yesterday is ignored (the file
  is left on disk for forensics);
- **re-arming** — it records the daily PnL at which it was granted and the last
  resolved trade, so the breaker trips again if the day loses *another full
  limit*, or if a new losing streak forms after the override. "Second chance,
  same size."
- **fail-safe** — an unreadable/corrupt override file is treated as *no*
  override, never as a granted one.

`python -m polybot.main halt` deletes it and re-arms immediately.

### 3.4 Surfacing

`status.json` gains a `guards` block:

```json
{
  "warmup": {"ready": false, "reason": "insufficient_oracle_samples",
             "oracle_samples": 14, "required_oracle_samples": 60,
             "uptime_secs": 20.6, "required_uptime_secs": 120.0, "oracle": "binance"},
  "risk": {"tripped": false, "reasons": [], "daily_realized_pnl": 0.0,
           "daily_loss_limit_usd": 100.0, "consecutive_losses": 0,
           "max_consecutive_losses": 4, "override_active": false,
           "override": null, "utc_day": "2026-07-27"},
  "depth_reference": {"enabled": false, "mode": "cap", "max_size_ratio": 8.0,
                      "families": {}},
  "trading_blocked": true
}
```

`guards.trading_blocked` is the single boolean an alerting rule should watch:
"alive but deliberately not trading" otherwise looks exactly like "alive with
no signals". The `status` CLI prints a `Risk guards (M4)` section with a
`!! TRADING BLOCKED !!` banner, the reasons, and the resume command.

---

## 4. Backward compatibility

Every new key is read through a defaulting accessor in `config.py`
(`adverse_size_cfg`, `warmup_cfg`, `risk_cfg`, `risk_override_path`), so a
`config.yaml` written before M4 loads unchanged and gets the **safe** defaults:
warmup ON at 60/120, breaker ON at 8%/4, adverse-size filter OFF.
`walk_asks(..., max_level_shares=None)` is exactly the pre-M4 walk — verified
by `test_off_by_default_takes_full_depth`. `_run_fill` gained two keyword
arguments with defaults; the settle_sweep call site is unchanged.

---

## 5. Tests, and proof they are behavioural

**Full suite: 212 tests, all passing** (was 136 before M4; +76).

```
$ cd bot && python3 -m pytest tests -q
212 passed in 6.77s
```

The brief called this out explicitly — "the suite once passed while a guard
could be deleted entirely". So the tests are backed by a mutation check:
`scripts/m4/mutation_check.py` copies `bot/` to a temp dir, applies 23
mutations that delete or neuter a guard, and requires the suite to go red for
each one.

```
$ python3 scripts/m4/mutation_check.py
baseline: GREEN  (212 passed in 7.78s)
  killed    G2 delete warmup gate from _maybe_snipe
  killed    G2 WarmupGate.check always ready
  killed    G2 warmup uptime condition removed
  killed    G2 missing-n_samples oracle treated as warm
  killed    G2 n_samples anchored on newest point instead of the clock
  killed    G3 delete breaker check from _maybe_snipe (pre-book)
  killed    G3 delete breaker check from _maybe_settle
  killed    G3 daily loss limit never trips
  killed    G3 consecutive-loss brake never trips
  killed    G3 combine pct/usd limits the LOOSE way
  killed    G3 override is not scoped to a UTC day
  killed    G3 corrupt override resumes trading
  killed    G3 streak does not reset at UTC midnight
  killed    G3 pnl_today ignores the UTC-midnight boundary
  killed    G3 consecutive_losses ignores since_ts
  killed    G3 a winning trade does not reset the streak
  killed    G1 adverse-size filter removed from walk_asks
  killed    G1 skip mode re-baselines the walk bound
  killed    G1 depth reference pools out-of-band levels
  killed    G1 reference returned before min_samples
  killed    G1 filter not threaded into the live order path
  killed    G1 engine never computes a level ceiling
  killed    G1/G2/G3 guards missing from status.json

23/23 mutations killed by the suite
All guard mutations are caught by the test suite.
```

Two mutations survived the first pass (`corrupt override resumes trading`,
`streak does not reset at UTC midnight`) — both were real gaps where the test
asserted an outcome the mutant happened to preserve. Two tests were added
(`test_corrupt_override_does_not_resume_trading` now also asserts
`override_active is False`; `test_breaker_auto_resets_at_utc_midnight` runs the
real `Ledger` + real `CircuitBreaker` end-to-end) and both mutants now die.

Note also that adding guard 2 immediately turned **6 pre-existing tests red** —
the warmup gate was blocking fills those tests expected. Those tests now call
`tests.test_engine_gating.warm()` explicitly, which is itself the property we
want: the guard is on by default and cannot be bypassed by accident.

---

## 6. Dry run

```
$ cd bot && POLYBOT_PORT=8917 timeout 90 python3 -m polybot.main run
2026-07-27T23:09:39.158Z INFO    polybot.engine: starting engine: mode=PAPER
2026-07-27T23:09:39.159Z INFO    polybot.engine: close_snipe window: tau in [2.50, 5.00]s (latency_ms=1500)
2026-07-27T23:09:39.159Z INFO    polybot.engine: M4 guard 1 adverse_size: enabled=False mode=cap max_size_ratio=8.0 history_n=200 min_samples=30
2026-07-27T23:09:39.159Z INFO    polybot.engine: M4 guard 2 warmup: enabled=True min_oracle_samples=60 min_uptime_secs=120 (vol_window=120s)
2026-07-27T23:09:39.159Z INFO    polybot.engine: M4 guard 3 circuit breaker: daily_limit=$100.00 consecutive_loss_brake=4 override_path=/home/user/S4/bot/data/risk_override.json
2026-07-27T23:09:39.160Z INFO    polybot.engine: status HTTP server listening on :8917 (/status, /health)
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-6pm-et family=1h close=2026-07-27T23:00:00+00:00
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-7pm-et family=1h close=2026-07-28T00:00:00+00:00
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-8pm-et family=1h close=2026-07-28T01:00:00+00:00
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-9pm-et family=1h close=2026-07-28T02:00:00+00:00
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-10pm-et family=1h close=2026-07-28T03:00:00+00:00
2026-07-27T23:09:42.269Z INFO    polybot.engine: discovered market: btc-updown-5m-1785193200 family=5m close=2026-07-27T23:05:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785193500 family=5m close=2026-07-27T23:10:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785193800 family=5m close=2026-07-27T23:15:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785194100 family=5m close=2026-07-27T23:20:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-15m-1785193200 family=15m close=2026-07-27T23:15:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-15m-1785194100 family=15m close=2026-07-27T23:30:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-15m-1785195000 family=15m close=2026-07-27T23:45:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-4h-1785182400 family=4h close=2026-07-28T00:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-4h-1785196800 family=4h close=2026-07-28T04:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-4h-1785211200 family=4h close=2026-07-28T08:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-15m-1785279600 family=15m close=2026-07-28T23:15:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785279600 family=5m close=2026-07-28T23:05:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785279300 family=5m close=2026-07-28T23:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: bitcoin-up-or-down-july-29-2026-7pm-et family=1h close=2026-07-30T00:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785279000 family=5m close=2026-07-28T22:55:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-15m-1785278700 family=15m close=2026-07-28T23:00:00+00:00
2026-07-27T23:09:42.270Z INFO    polybot.engine: discovered market: btc-updown-5m-1785278700 family=5m close=2026-07-28T22:50:00+00:00
2026-07-27T23:09:42.271Z INFO    polybot.engine: discovery: 22 markets tracked (22 new)
2026-07-27T23:09:42.271Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_oracle_samples): family=startup oracle_samples=1/60 uptime=3/120s — refusing to trade on a thin vol buffer
2026-07-27T23:09:57.280Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_oracle_samples): family=startup oracle_samples=13/60 uptime=18/120s — refusing to trade on a thin vol buffer
2026-07-27T23:10:12.288Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_oracle_samples): family=startup oracle_samples=24/60 uptime=33/120s — refusing to trade on a thin vol buffer
2026-07-27T23:10:12.506Z INFO    polybot.engine: discovered market: btc-updown-5m-1785194400 family=5m close=2026-07-27T23:25:00+00:00
2026-07-27T23:10:12.506Z INFO    polybot.engine: discovery: 20 markets tracked (1 new)
2026-07-27T23:10:27.294Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_oracle_samples): family=startup oracle_samples=36/60 uptime=48/120s — refusing to trade on a thin vol buffer
2026-07-27T23:10:42.305Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_oracle_samples): family=startup oracle_samples=48/60 uptime=63/120s — refusing to trade on a thin vol buffer
2026-07-27T23:10:44.063Z INFO    polybot.engine: discovery: 20 markets tracked (0 new)
2026-07-27T23:10:57.309Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_uptime): family=startup oracle_samples=61/60 uptime=78/120s — refusing to trade on a thin vol buffer
2026-07-27T23:11:08.943Z INFO    polybot.engine: stopping engine
```

All three guards announce themselves at boot. The warmup gate is visibly
counting up (1 → 13 → 24 → 36 → 48 → 61 samples) and correctly switches its
reason from `insufficient_oracle_samples` to `insufficient_uptime` once the
sample count clears 60 at t=78s.

A longer run to observe completion (same command, `timeout 170`):

```
23:15:21.054Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_uptime): family=startup oracle_samples=63/60 uptime=77/120s
23:15:36.057Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_uptime): family=startup oracle_samples=76/60 uptime=92/120s
23:15:51.066Z WARNING polybot.risk: close_snipe WARMING UP (insufficient_uptime): family=startup oracle_samples=88/60 uptime=107/120s
23:16:04.069Z INFO    polybot.risk: close_snipe WARMUP COMPLETE: 99 oracle samples, uptime 120s — trading enabled
```

**Measured steady-state poll rate: 99 samples per 120s = 0.825/s.** This is the
empirical justification for choosing 60 over 90: a 90-sample gate would sit 9
samples away from a permanent lockout on this very machine.

Live status endpoint mid-warmup, showing all three guards and the
`trading_blocked` flag:

```
$ curl -s localhost:8917/status | jq .guards
{
  "warmup": {"ready": false, "reason": "insufficient_oracle_samples",
             "oracle_samples": 14, "required_oracle_samples": 60,
             "uptime_secs": 20.6, "required_uptime_secs": 120.0, "oracle": "binance"},
  "risk": {"tripped": false, "reasons": [], "daily_realized_pnl": 0.0,
           "daily_loss_limit_usd": 100.0, "consecutive_losses": 0,
           "max_consecutive_losses": 4, "override_active": false,
           "override": null, "utc_day": "2026-07-27"},
  "depth_reference": {"enabled": false, "mode": "cap", "max_size_ratio": 8.0, "families": {}},
  "trading_blocked": true
}
```

Status CLI once warm:

```
$ python3 -m polybot.main status
--- Risk guards (M4) ---
  trading allowed
  warmup:  ready=True (warm)  oracle_samples=99/60  uptime=167.3/120.0s
  breaker: tripped=False  daily_realized=$0.00  limit=$100.00  consecutive_losses=0/4  utc_day=2026-07-27
  adverse-size filter: enabled=False mode=cap max_size_ratio=8.0
```

Override CLI:

```
$ python3 -m polybot.main resume
Circuit breaker is NOT tripped — nothing to resume.
  daily realized: $0.00   consecutive losses: 0
  (use --force to pre-authorise an override for today anyway)

$ python3 -m polybot.main resume --force
WARNING polybot.risk: RISK OVERRIDE GRANTED for 2026-07-27 at daily pnl $0.00 — trading resumes;
                      the breaker re-arms if the day loses another full limit
Override written to /home/user/S4/bot/data/risk_override.json
  utc_day=2026-07-27  pnl_at_override=$0.00
  breaker now tripped=False

$ python3 -m polybot.main halt
WARNING polybot.risk: risk override cleared
Override cleared.
  breaker tripped=False  daily realized=$0.00  consecutive losses=0
```

No git commit was made.

---

## 7. Files

| File | Change |
|---|---|
| `bot/polybot/depth.py` | **new** — `DepthTracker`, `max_level_shares` (guard 1 statistic) |
| `bot/polybot/risk.py` | **new** — `WarmupGate` (guard 2), `CircuitBreaker` (guard 3), UTC helpers |
| `bot/polybot/fill_engine.py` | `walk_asks` size filter; `WalkResult` bookkeeping; `adverse_size_blocked` outcome |
| `bot/polybot/execution.py` | filter threaded through paper **and** live order paths |
| `bot/polybot/engine.py` | guard construction, warmup + breaker gating, depth observation, boot logging, `_publish_guard_status` |
| `bot/polybot/ledger.py` | `recent_trade_pnls`, `consecutive_losses`, `n_resolved_trades` |
| `bot/polybot/oracle.py` | `n_samples()` on both oracles (wall-clock anchored) |
| `bot/polybot/config.py` | `adverse_size_cfg`, `warmup_cfg`, `risk_cfg`, `risk_override_path` (all defaulting) |
| `bot/polybot/status_server.py` | `set_guards`, `guards` block + `trading_blocked` |
| `bot/polybot/main.py` | `Risk guards (M4)` status section; `resume` / `halt` subcommands |
| `bot/config.yaml` | three commented config blocks with the measurements that set each default |
| `bot/README.md` | "Risk guards (M4)" section, file tree, test instructions |
| `bot/tests/test_risk_guards.py` | **new** — 74 behavioural tests |
| `bot/tests/test_engine_gating.py` | `n_samples`/breaker methods on stubs, explicit `warm()` helper |
| `scripts/m4/replay_depth.py` | shipped-parameter replay recording depth statistics |
| `scripts/m4/analyse_depth.py` | large-vs-normal outcome comparison, three reference definitions |
| `scripts/m4/controls_depth.py` | price/regime confound controls |
| `scripts/m4/warmup_sigma.py` | sigma_n vs sigma_120, fair error, decision error |
| `scripts/m4/warmup_decision.py` | cold-only trade economics + lockout cost |
| `scripts/m4/mutation_check.py` | 23-mutation proof that the guard tests are behavioural |

Artifacts: `data/c2/m4_depth_fills.parquet`, `m4_depth_evals.parquet`,
`m4_depth_fills_wide.parquet`, `m4_warmup.parquet`.

---

## 8. Open items

1. **The adverse-size filter is off on BTC-1h evidence only.** Before enabling
   `close_snipe` for a new coin family, re-run `scripts/m4/replay_depth.py` on
   that family's tape. The mechanism it guards is real; it simply is not what a
   large offer means on this book.
2. **Circuit-breaker thresholds are calibrated for one coin at ~2 trades/day.**
   Re-derive `max_daily_loss_pct` and `max_consecutive_losses` from the
   multicoin daily-PnL distribution before running ~6 coins.
3. **The warmup threshold is tied to the observed 0.825 polls/s.** If the
   oracle loop is ever changed (websocket feed, different cadence, more coins
   sharing the poller), re-check that `min_oracle_samples` still leaves margin
   — the failure mode of getting this wrong is a bot that never trades and says
   so only in the log.
4. **Live-mode populations differ.** Everything here is paper/backtest. A real
   taker changes who rests size against it; re-measure §1 after the first live
   trades.
