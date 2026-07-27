# Constraint audit — live bot `close_snipe` path (1h family)

**Audit date:** 2026-07-26 · **Bot uptime:** since 2026-07-16 18:18 UTC (~10d)
**Code audited:** `bot/polybot/{engine,strategy,fill_engine,execution,oracle,polymarket,config}.py`, `bot/config.yaml`
**Live baseline:** close_snipe 1h — 11 signals, 7 filled, 4 `book_moved_no_edge`, 214.69 sh, $134.09, +$44.97, ev/sh +$0.2095, 1426 eval ticks.

## 0. Evidence base

Local repo has **no live log/DB** (the bot runs at `/root/S4` on the deploy host per
`bot/scripts/polybot.service`; `bot/data/` here holds only the pre-relaunch archive
`archive_20260716_000254Z/`). Everything below is therefore code-derived, plus three
independent empirical checks I ran here:

1. **Archive replay** (`bot/data/archive_20260716_000254Z/{polybot.log,status.json,polybot.db}`,
   45 min of the 2026-07-15 build) — gives a real observed snipe window and a real
   discovery snapshot.
2. **Oracle/vol simulation** on `data/data/processed/binance/klines_1s/` (60 days,
   1439 hourly windows) — replays `rolling_log_return_std` + `fair_value_up` exactly.
3. **Book-band replay** on `data/data/processed/daily/1h/quotes/` (45 days, 1079
   hourly windows) forward-filled onto the six real snipe tick times.

Check 3 reproduces the live signal rate to within 0.3pp (modelled 4.91% of windows fire
vs live 11/238 = 4.62%), so the model of the gates below is calibrated, not hypothetical.

---

## A. Gates that stop a market being EVALUATED at all

| # | Gate | file:line | value | how often it binds |
|---|---|---|---|---|
| A1 | Market must be in the **current discovery snapshot** `self.markets` (wholesale-replaced dict, not `known_markets`) | `engine.py:184` (iterate), `engine.py:151` (`self.markets = found`) | 30 s refresh | **Structural single point of failure — see BUG 1.** Est. 0.1–0.5 % of closes silently skipped |
| A2 | `discover_markets` swallows every per-request HTTP error and can return `{}` without raising, wiping the snapshot | `polymarket.py:138-145, 163-165, 184-185`; `engine.py:132-137` | n/a | Whole-gamma outage → 0 markets tracked for ≥30 s. Archive min observed: 34 markets (never 0) over 86 cycles |
| A3 | Hourly slug probe window `range(-1, lookahead+1)` | `polymarket.py:222`, `config.yaml:92` (`hourly_lookahead_hours: 3`) | i = −1…3 | **Never binds.** Slug hour = window *start*, close = start+1 h, so the imminent market is `i=0` for a full hour before its close. Confirmed in archive: `…july-15-2026-2pm-et close=19:00Z` |
| A4 | Gamma `closed` filter: `get_market_by_slug` omits the param, which gamma treats as `closed=false` | `polymarket.py:125-145` (documented quirk), called at `polymarket.py:224` | implicit | Would drop the market the instant gamma flips `closed`. Archive shows `closed=False, accepting_orders=True` **9 min after** close_ts, so it does not bind pre-close |
| A5 | Family must exist in config **and** be `enabled` | `engine.py:185-187`; `config.yaml:24-54` | all 4 enabled | Never |
| A6 | `fam_cfg.close_snipe` | `engine.py:188`; `config.yaml:29` | 1h=true, others=false | Binds 100 % for 5m/15m/4h (by design — no Chainlink feed) |
| A7 | Defensive `market.family != "1h"` re-check | `engine.py:203-207` | — | Redundant with A6 |
| A8 | Snipe window `0 < tau <= snipe_last_secs` | `engine.py:197-200`; `config.yaml:58` | 6 s | Binds 3594/3600 s per window **by design**. Yields exactly 6 tick evaluations (archive: tau = 5.2, 4.2, 3.2, 2.2, 1.2, 0.2) |
| A9 | Tick cadence `wait(max(0, 1.0 − elapsed))` — period is ≥1.0 s, and `_tick` does **blocking HTTP inline** (2×`get_book`, `hour_open_close`, and `_check_resolutions`→gamma at 10 s timeout) | `engine.py:169-179`, `engine.py:192`, `engine.py:366-379` | 1.0 s | A slow tick eats eval ticks 1-for-1. Empirically bounded to ≲4 % of ticks (see BUG 4) |
| A10 | Clock: `tau = close_ts − time.time()`; `close_ts` is absolute UTC from gamma, `now` is local wall clock. **No NTP/skew guard anywhere** | `polymarket.py:52`, `engine.py:198` | — | Silent. A ±3 s host clock drift shifts the whole 6 s window; ±6 s removes it entirely. Unmonitored |

## B. Gates that stop a SIGNAL firing

| # | Gate | file:line | value | how often it binds |
|---|---|---|---|---|
| B1 | `snipe_done` — one entry per window, set **before** the fill is attempted | `engine.py:201-202`, `engine.py:241` | — | **See BUG 2.** Cost 4 of 11 signals a retry |
| B2 | `S_open` unavailable (`hour_open_close` HTTP fail → `(None, None)`) → `return` with **no log line** | `engine.py:209-215`; `oracle.py:153-182` (timeout 5) | — | Rare; costs 1 silent tick each, retried next tick |
| B3 | **Oracle staleness** `latest is None or (now − latest.ts) > 5` → silent `return` | `engine.py:217-219` | 5 s | **See BUG 3.** Empirically ~never |
| B4 | sigma NaN or ≤0 (`<3` points in window, or `<2` returns) → silent `return` | `engine.py:221-223`; `oracle.py:97-108` | — | Only in the first ~3 s after a process restart |
| B5 | `sigma_1s_floor` clamp | `strategy.py:84`; `config.yaml:63` | 8e-6 | Measured median simulated `sigma_hat` = 3.3e-5 → floor binds in ~0 % of windows. **Dead knob at this level** |
| B6 | `fair_cap` clamp of `fair_up` into [0.02, 0.98] | `strategy.py:92-93`; `config.yaml:64` | 0.98 | **Binds ~96 % of ticks** (8301/8634 in the 60-day sim). This is the single most consequential parameter |
| B7 | Book missing (`get_book` returned `None`, e.g. 404/timeout) or has no asks | `strategy.py:101-102`; `polymarket.py:278-286` | — | Archive had 12 book-fetch 404s in 45 min. Near close the **winning** token's asks are frequently gone entirely (85 % of ticks in the 45-day replay) |
| B8 | `price_min < ask < price_max` (`continue`, correctly tries the other side) | `strategy.py:104`; `config.yaml:60-61` | (0.30, 0.99) | Winner ask ≤0.30 at some tick in **1.39 %** of windows; net opportunity cost (cheap winner ask *and* no band ask) = **0.83 %** of windows (9/1079) |
| B9 | `edge = fair − ask − fee(ask) > edge_min` | `strategy.py:106-107`; `config.yaml:59` | 0.05 | **The binding gate.** With `fair` pinned at 0.98 this is exactly `ask < 0.92515`. Combined with B8 the firing band is **ask ∈ (0.30, 0.92515)** |
| B10 | Side order: `up` is tested first and returns immediately | `strategy.py:100-113` | — | If both sides qualify (very rare) the bot can take the lower-edge side |

### B — the shape of the binding constraint (45-day book replay, 1079 windows)

```
winner-token ask ABSENT for the whole last 6 s ............ 84.89 %
winner ask present but ALL >= 0.92515 (edge < 0.05) .......  9.36 %
winner ask <= 0.30 at some tick (blocked by price_min) .....  1.39 %
winner ask INSIDE the firing band (0.30, 0.92515) ..........  4.91 %  <- signals
loser  ask inside the firing band ..........................  3.06 %  <- adverse-selection exposure
```
Live: 11 signals / ~238 evaluated windows = **4.62 %**. Model 4.91 %. Match.

**Conclusion: the throughput limit is book structure, not tuning.** In ~95 % of hourly
closes the winning token simply has no offer between 30¢ and 92.5¢ in the final six
seconds — either nothing is offered at all, or it is offered at 97¢+. Loosening
`edge_min` or `price_min` cannot manufacture liquidity that is not there; only raising
`fair_cap` widens the band, and that trades directly against tail protection.

## C. Gates that stop a FILL after a signal

| # | Gate | file:line | value | how often it binds |
|---|---|---|---|---|
| C1 | Global open-notional cap, checked **after** `snipe_done` is set | `engine.py:248-250`, `engine.py:327-329`; `config.yaml:76` | $250 | Never so far ($134 cumulative, ≤$25 concurrent). But when it does bind it **burns the window permanently** (B1 already fired) and records a signal with no fill row |
| C2 | 1500 ms simulated latency, then the book is **re-fetched** and walked against the *new* book | `fill_engine.py:166-171`; `execution.py:118-122`; `config.yaml:79` | 1500 ms | This is the honest core of the sim. Directly responsible for the 4 `book_moved_no_edge` (36 % of signals) |
| C3 | `no_book` — re-fetch returned `None` | `fill_engine.py:172-177` | — | Occasional |
| C4 | `empty_book` — asks all pulled during the 1.5 s | `fill_engine.py:178-183` | — | Dominant failure mode for settle_sweep (1235/1235); plausible for some of the 4 snipe misses |
| C5 | `book_moved_no_edge` — walk returned 0 shares | `fill_engine.py:184-186` | — | **4/11 = 36.4 % of live signals** |
| C6 | **`price_min` `break` in the walk** — a best ask *below* `price_min` aborts the entire ladder | `fill_engine.py:102` | 0.30 | **See BUG 6a.** Verified: book [0.25×50, 0.55×200, 0.56×200] → **0 shares**; same book without the 0.25 level → 45.45 sh / $25.00 |

## D. Gates that cap SIZE below `per_event_cap_usd = 25`

Observed: 7 fills, $134.09 → **$19.16/fill = 76.6 % of cap**; 23.4 % of intended notional
never filled. Ranked causes:

| # | Cap | file:line | value | effect |
|---|---|---|---|---|
| D1 | Level depth: `shares = min(lvl.size, remaining/price)` | `fill_engine.py:111-112` | book | Primary, legitimate |
| D2 | `max_walk_above_best` — walk stops at `best + 0.03` | `fill_engine.py:106-107`; `execution.py:112-114`; `config.yaml:82` | 0.03 | Verified: 0.55×10 then 0.60×500 → **10 sh / $5.50** of a $25 cap. A thin top level truncates the fill hard |
| D3 | Per-level `edge <= edge_min` → `break` (not skip) | `fill_engine.py:108-110` | 0.05 | At fair=0.98 the walk stops at the first level ≥0.92515 regardless of remaining budget |
| D4 | `price_min` break (D=C6) | `fill_engine.py:102` | 0.30 | **Silent total loss of the fill**, not a partial cap |
| D5 | `price_max` break | `fill_engine.py:102` | 0.99 | Never — D3 always fires first at fair≤0.98 |
| D6 | Float-dust guard `shares <= 1e-9` | `fill_engine.py:113-114` | — | Never materially |
| D7 | Global cap C1 | `engine.py:248` | $250 | Never yet |
| D8 | **Fees are not charged against `cap_usd`** | `fill_engine.py:117` (`remaining_usd -= shares*price`) | — | Over-spends the cap: verified $25.00 cost + $0.70 fees = **$25.70 outlay**. Direction: fills slightly *larger* than intended |
| D9 | **`market.order_min_size` and `market.tick_size` are parsed but never enforced** | `polymarket.py:104-105` (parsed), no consumer in `fill_engine.py`/`execution.py` | 5 sh default | Paper accepts a 0.5-share fill that the live CLOB would reject. Paper results are **optimistic** vs live by this amount |

No Kelly/edge-proportional sizing exists — `cap_usd` is a flat $25 for every signal
regardless of edge size (`engine.py:253`).

---

## E. Critical bug hunt — findings

### BUG 1 — Can a 1h market about to close be MISSING from `self.markets`? **YES.**

Three compounding facts:

1. `self.markets = found` (`engine.py:151`) **replaces** the snapshot wholesale every
   30 s. The monotonic `known_markets` dict exists (`engine.py:44`) but `_tick`
   iterates `self.markets` (`engine.py:184`) — so anything missing from one discovery
   pass is invisible to the snipe path until the next pass.
2. The imminent hourly market reaches `found` **only** via the deterministic slug probe
   (`polymarket.py:218-228`). A single `get_market_by_slug` call. That function swallows
   *all* exceptions and returns `None` (`polymarket.py:138-145`) — a timeout, a 5xx, or
   one dropped TLS handshake removes the market from the snapshot with only a
   `WARNING gamma slug lookup failed` line and no strategy-level alarm.
3. **The broad `/events` fallback does not cover it.** It scans `closed=false` ordered
   by `createdAt` **descending** (`polymarket.py:248-250`), and hourly markets are listed
   ~2 days ahead of their close. Proven from the archive snapshot: the seven tracked 1h
   markets were the five slug-constructed ones (`july-15` 2pm–6pm ET) **plus
   `july-17-2026-2pm-et` and `july-17-2026-3pm-et`** — i.e. the fallback surfaced only
   markets ~2 days in the future, never the one closing in seconds.

Also, `discover_markets` can legitimately return `{}` with no exception raised (every
inner error is swallowed), and `_do_discovery`'s `try/except` (`engine.py:135-137`) only
protects against a raise — so a gamma outage assigns an empty dict and **silently idles
every strategy** for ≥30 s.

**Impact:** when it happens, the snipe is skipped with *zero* `snipe eval` log lines —
indistinguishable from "no market existed". Estimated 0.1–0.5 % of closes.

**Fixes:** (a) `if not found: log.error(...); return` — never assign an empty snapshot;
(b) iterate `known_markets` filtered to `close_ts > now − grace` in `_tick` instead of
`self.markets`; (c) merge rather than replace: `self.markets = {**self.markets, **found}`
pruned by `close_ts`; (d) emit an explicit `ERROR` if no market with `0 < tau < 60`
exists when one is arithmetically due.

### BUG 2 — Does `snipe_done` block a legitimate second opportunity? **YES.**

`self.snipe_done.add(market.slug)` runs at `engine.py:241` — **before** the global-cap
check (`:248`) and **before** the fill worker is even submitted (`:264`). The fill's
outcome never feeds back. Consequences:

- A signal at tau=6 lands its fill at tau≈4.5 (1.5 s latency). If it returns
  `book_moved_no_edge`, **four more eval ticks (tau 4,3,2,1) exist and are all skipped.**
  This applies to **4 of 11 live signals (36 %)**.
- A signal blocked by C1 (global cap) burns the window with no fill row at all.
- `snipe_done` is never cleared and is in-memory only, so a process restart mid-window
  can re-enter a window already entered.

**Fix:** only mark `snipe_done` on `attempt.filled` (inside `_run_fill`), and use a
separate `in_flight_snipe` set (mirroring `in_flight_settle`, `engine.py:50`) to prevent
concurrent duplicate attempts within one window.

### BUG 3 — Is the oracle-staleness check tripping? **NO (essentially never).**

- Poll interval: `poll_once()` then `self._stop.wait(1.0)` (`engine.py:116-121`) → real
  period = RTT + 1.0 s ≈ **1.06–1.30 s** (my 60-day sim measured 101.7 points landing in
  a 120 s window vs 120 at ideal 1 s → effective Δ ≈ 1.18 s).
- Gate needs a **>5 s** gap, i.e. ~4 consecutive lost polls. `fetch_price` uses
  `timeout=5` (`oracle.py:60`); one full-timeout hang alone produces a ~6.2 s gap and
  would cost 1–2 ticks — that is the realistic trip mechanism, not rate limiting
  (`ticker/price` is weight 1, ~50 req/min vs a 1200/min budget).
- Archive rate: 2 `binance fetch_price failed` in 2700 s (0.07 %), and both were instant
  proxy-refusals, not timeouts.
- **Hard empirical bound:** BUG 4's tick reconciliation shows ≥237.7 of ~240 windows were
  fully evaluated, leaving ≤55 ticks total (<4 % over 10 days) for *all* silent gates
  combined (B2+B3+B4). The staleness gate is not a material loss channel.

**Real weakness (unmeasured, worth fixing):** `latest.ts` is **local receipt time**
(`oracle.py:73`), not the exchange timestamp. A feed serving *stale prices quickly*
(cached CDN response from `data-api.binance.vision`) passes this check unharmed. Compare
against a server-side timestamp instead.

### BUG 4 — Does 1426 eval ticks reconcile? **YES — coverage is ~100 %, nothing material is missing.**

Ticks per close are **exactly 6** when the loop period is 1.0 s, confirmed by the archive
(tau = 5.2, 4.2, 3.2, 2.2, 1.2, 0.2). 6 is a hard ceiling: the wait is
`max(0, 1.0 − elapsed)` (`engine.py:179`), so the period is always ≥1.0 s and the window
`0 < tau ≤ 6` is 6 s long.

```
windows implied by tick count      = 1426 / 6                = 237.7
signals suppress the rest of their window (snipe_done, BUG 2):
  11 signals x [0..5] lost ticks   = 0..55 lost ticks
=> windows actually evaluated      = (1426 .. 1481)/6        = 237.7 .. 246.8
elapsed hourly closes since 07-16 18:18Z, as a function of snapshot time:
   07-26 00:00Z -> 222   06:00Z -> 228   12:00Z -> 234
   15:00Z -> 237        18:00Z -> 240   23:00Z -> 245
```

Because ≥237.7 windows must have been evaluated, the snapshot cannot predate
**2026-07-26 ~16:00 UTC**, and at that time 237–240 closes had elapsed. **Evaluated ≈
elapsed.** There is no missing-tick population to explain — the ~9.7-day figure in the
brief is slightly low; the true elapsed is ≈9.95 days.

Two caveats on the number itself:
- `cmd_pnl` counts `snipe eval` only in the **current** `polybot.log`, ignoring the 5
  rotated backups (`main.py:158-168` vs `logging_setup.py:42-46`, `max_bytes: 10 MiB`).
  Estimated volume under the current config is ~4 MiB/10 d, so rotation has probably not
  occurred — but the counter is silently wrong the moment it does.
- Gates B2/B3/B4 return **before** the eval log line (`engine.py:235`), so silent skips
  are invisible in this metric. Move the log line above those gates.

### BUG 5 — Does `rolling_log_return_std` produce a biased sigma? **YES, but upward (+9.5 %) — the duplicate-price fear is wrong. The real defect is the sqrt-time scaling.**

Measured on 60 days of 1s klines, replaying the estimator exactly:

- **Duplicate prices are not a poll artefact — they are intrinsic.** 46.1 % of
  *consecutive true 1 s closes* are already identical (9.7 % of 1 s bars have zero
  trades). Poll jitter adds very little on top.
- **Net bias on `sigma_hat` is +9.5 %, i.e. conservative:**
  `median(sigma_hat / true sigma_1s) = 1.095`, IQR [1.089, 1.115]. This is exactly
  `sqrt(Δ) = sqrt(1.18) = 1.086` — the estimator returns the std of returns over a
  **1.18 s** sampling interval and the model then treats it as a per-**second** sigma.
  Interval inflation dominates duplicate deflation.
- **The scaling is the real problem.** `fair_value_up` uses
  `sigma_total = sigma_1s * sqrt(tau)` (`strategy.py:36`). Against realized moves:

  | tau | median predicted sd | realized sd | ratio | std(z) | P(&#124;z&#124;>3) |
  |---|---|---|---|---|---|
  | 6 s | 7.83e-5 | 2.111e-4 | **2.70×** | 2.95 | 13.1 % |
  | 3 s | 5.69e-5 | 1.669e-4 | **2.93×** | 3.03 | 11.5 % |
  | 1 s | 3.31e-5 | 1.345e-4 | **4.06×** | 4.00 | 17.8 % |

  A trailing-120 s Gaussian understates the last-seconds-before-the-hour move by
  **2.7–4.1×** (top-of-hour settlement flow + vol clustering + fat tails). Pre-cap the
  model is wildly overconfident: `P(|z|>3)` is 11–18 % where a Gaussian says 0.27 %.

- **`fair_cap = 0.98` is what rescues it, and it over-rescues.** 96 % of ticks clip to a
  cap. At the cap the model is *under*confident: empirical `P(up | fair_up = 0.98) =
  0.9965` (n = 3997) vs the modelled 0.98 — a 0.0165 systematic understatement. Same on
  the low side (`P(up | fair_up = 0.02) = 0.0060` vs 0.02).

**Net:** the +9.5 % sigma bias and the 2.7–4× scaling error push in opposite directions,
and `fair_cap` truncates the residual. The live +$0.2095/share is consistent with the cap
being conservative. **But the safety comes entirely from `fair_cap`, not from the model.**
Raising `fair_cap` (the obvious lever for more signals — it directly widens the firing
band from `ask<0.92515` toward `ask<0.945` at 0.99) removes the only thing standing
between the bot and a 2.7–4× underestimated tail. Do not raise it without first
re-fitting sigma. The principled fix is to divide by `sqrt(Δ)` (measure the actual mean
poll interval) **and** replace `sqrt(tau)` scaling with an empirically-fitted
last-6-seconds volatility term.

### BUG 6 — Silent sub-$25 caps other than book depth? **YES — four.**

- **6a (real bug, causes total misses):** `walk_asks` uses `break`, not `continue`, on
  the price-bounds test (`fill_engine.py:102`). Because asks ascend, a best ask **below**
  `price_min` aborts the whole ladder. Verified: `[0.25×50, 0.55×200, 0.56×200]` at
  fair 0.98 → **0 shares / $0**; drop the 0.25 level → 45.45 sh / $25.00. A *price
  improvement* between signal and fill is therefore recorded as `book_moved_no_edge`.
  Note `strategy.py:104` correctly uses `continue` for the same test — the two are
  inconsistent. This is a plausible contributor to the 4 live misses.
- **6b:** `max_walk_above_best = 0.03` (`fill_engine.py:106`, `config.yaml:82`) —
  verified truncation to $5.50 of a $25 cap on a thin top level.
- **6c:** per-level `edge <= edge_min` → `break` (`fill_engine.py:108-110`) caps the walk
  at `ask < 0.92515` regardless of remaining budget.
- **6d (opposite direction):** fees are excluded from the budget decrement
  (`fill_engine.py:117`) → true outlay $25.70 on a $25 cap.
- Plus **D9**: `order_min_size` (default 5 shares) is parsed at `polymarket.py:104` and
  never enforced, so paper can book fills the live CLOB would reject.

---

## F. Ranked recommendations

1. **Never assign an empty/partial discovery snapshot** (`engine.py:151`) and drive
   `_tick` off `known_markets`. Add an ERROR when a due close has no tracked market. *(BUG 1 — silent total misses)*
2. **Move `snipe_done` into the fill worker, gated on `attempt.filled`**, with an
   `in_flight_snipe` set for de-dup. *(BUG 2 — recovers up to 4 retries per 11 signals)*
3. **Change `break` → `continue` on the price-bounds test in `walk_asks`**
   (`fill_engine.py:102`), or clamp instead of aborting. *(BUG 6a)*
4. **Move the `snipe eval` log line above the B2/B3/B4 gates** so silent skips become
   observable, and count rotated logs in `cmd_pnl`. *(BUG 4 observability)*
5. **Fix the sigma units** (`sigma_hat / sqrt(mean_poll_interval)`) and re-fit the
   tau-scaling before touching `fair_cap`. *(BUG 5)*
6. Enforce `order_min_size` in `walk_asks`, and charge fees against `cap_usd`. *(D8/D9)*
7. Add a host clock-skew check against gamma/Binance server time. *(A10)*
8. Use exchange timestamps, not local receipt time, for the staleness gate. *(BUG 3)*
9. Do **not** expect more throughput from loosening `edge_min`/`price_min` — 95 % of
   closes have no winner offer in the band at all. The only real levers on signal count
   are `fair_cap` (dangerous, see BUG 5), a longer `snipe_last_secs`, or a websocket book
   feed to cut the 1500 ms latency that costs 36 % of signals.
