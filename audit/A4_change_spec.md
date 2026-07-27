# A4 — Snipe-timing forensics + exact change spec

**Scope.** (1) A code-level trace of `engine._maybe_snipe` / `strategy.evaluate_close_snipe`
answering *at what `tau` can this bot actually fire, and is firing early really worse*, measured on
**1,738 out-of-sample 1h closes (2026-05-01 → 2026-07-12)** with the bot's real fill mechanics.
(2) A minimal, line-level change spec for four changes. **Nothing is implemented here** — this
document is the instruction set for the implementing agent.

**Headline: the premise in `docs/06_live_audit.md` §5/§6 is wrong as stated, and the corrected
version is more valuable.** "Snipe only in the last 2–3s" would *cost* money. The real finding is
that the current window has **no lower bound at all** (it can fire at `tau = 0.02s`, which never
fills and in LIVE mode would send a real order into a closed market), and that its upper end
(`tau = 6`) is harmless at today's \$25 clip but is **the sole source of catastrophic single-trade
losses once the clip is raised** — the exact change docs/06 ranks as lever #1. The timing change is
therefore a **prerequisite for the cap raise**, not a standalone P&L lever.

Reproduce: `python3 scripts/a4_timing_scan.py` · `a4_timing_variants.py` · `a4_timing_final.py` ·
`a4_timing_cap.py`. Grid CSV: `audit/A4_tau_grid.csv`.

---

## PART 1 — FORENSICS

### 1a. At what `tau` can the current code actually fire?

Trace of the shipped path (`engine.py:168-266`):

| step | code | consequence |
|---|---|---|
| tick period | `_tick_loop` L169-179: `tick_secs = order_book_poll_secs = 1.0`; `wait(max(0, 1.0 - elapsed))` | period is **exactly 1.0 s** unless a tick overruns (2 REST `/book` calls ≈ 0.1–0.4 s, so it does not). Phase relative to `close_ts` is set at bot start and is effectively **uniform random**. |
| clock | `_tick` L182: `now = time.time()` sampled **once**, reused for every market | recorded `tau` is the tick's `now`, not the instant the book was read |
| gate | `_maybe_snipe` L197-200: `snipe_last = 6`; `if not (0 < tau <= snipe_last): return` | **the only bounds are `(0, 6]`. There is no lower bound.** |
| one-shot | L201-202 `snipe_done` | first qualifying second wins; the rest of the window is skipped |
| family guard | L203-207 `market.family != "1h"` → return | defensive; matches config |
| oracle staleness | L217-219: `if latest is None or (now - latest.ts) > 5: return` | `_oracle_loop` does `poll_once()` then `wait(1.0)`, so the series is ~1.0 s + RTT apart. The 5 s guard needs **~4 consecutive Binance failures** to bind. **It is not throttling anything** — consistent with docs/06 §1. |

**Answer.** Writing `r = (close_ts − T₀) mod 1 ∈ [0,1)` for the tick phase, the six evaluation
instants per close sit at

```
tau ∈ { r, r+1, r+2, r+3, r+4, r+5 }        (six ticks; matches docs/06's 1426 ÷ 6 = 237.7 closes)
```

so the bot **can fire anywhere in `(0, 6]`**, and its *last* chance is at a `tau` uniformly
distributed in `(0, 1]`. Three further effects push the effective timing later than the recorded
`tau`:

1. `now` is sampled **before** the two `get_book` REST calls, so the book fed to
   `evaluate_close_snipe` is from `now + δ_book` (δ_book ≈ 0.1–0.4 s). Ledger `tau_secs` is an
   **over**estimate of the book's true time-to-close.
2. `execute_taker_signal` (`fill_engine.py:166-169`) takes its own `signal_time = time.time()` at
   the *start of the worker thread*, then sleeps `latency_ms`. Real signal→fill gap is therefore
   **`1.5 s + δ_book + δ_queue ≈ 1.7–2.1 s`**, not 1.5 s.
3. `S_t` comes from a ~1.05–1.3 s REST poller, so it is on average ~0.6 s stale — the input-precision
   gap `audit/A2_replay.md` §3.4 already identified.

**Two latent defects fall out of this trace:**

- **D1 (real, currently harmless in PAPER; unsafe in LIVE).** With no lower `tau` bound, a close
  where nothing qualifies until the last tick fires at e.g. `tau = 0.08 s`. The order then arrives
  **~1.9 s after the market closed**. Measured: **at `tau = 1`, 46 signals produced 0 fills — 42 of
  them `empty_book`/`no_book`** (table below). In PAPER that is a wasted log line. In LIVE
  (`execution._place_live_order`) it is a real FAK order submitted into a post-close book — the
  exact adverse-selection setup that killed `settle_sweep`.
- **D2 (dormant, blocks the 5m family).** `_tick` iterates `self.markets` in **discovery order, not
  by `close_ts`**, and each in-window market performs two *blocking* REST book fetches inside the
  single tick while still using the tick's shared `now`. With only 1h enabled at most one market is
  ever in-window, so this is currently invisible. When 5m/15m are enabled (docs/06 lever 3), markets
  late in the iteration order will be evaluated hundreds of ms after their recorded `now`. Out of
  scope here — **flagged so the Chainlink work does not inherit it silently.**

### 1b. THE KEY NUMBER — is firing early strictly worse, or is waiting a real gamble?

**Waiting is a real gamble, and the naive "just wait" recommendation is wrong.** Measured on 1,738
OOS closes, signal at `tau = k`, fill 1.5 s later against the **re-read** book, `edge_min = 0.05`,
`cap = $25`, tick phase φ = 0:

| signal tau | signals | fills | fill rate | `empty`/`no_book` | `book_moved` | P&L | ¢/share | win | t |
|---|---|---|---|---|---|---|---|---|---|
| **1** | 46 | **0** | **0%** | **42** | 4 | \$0.00 | — | — | — |
| 2 | 55 | 26 | 47% | 8 | 21 | +\$80.10 | 11.71 | 88.5% | 2.61 |
| 3 | 54 | 35 | 65% | 1 | 18 | +\$146.61 | 18.77 | 94.3% | 4.67 |
| 4 | 70 | 44 | 63% | 3 | 23 | +\$114.92 | 12.97 | 84.1% | 3.31 |
| 5 | 74 | 49 | 66% | 5 | 20 | +\$170.96 | 17.94 | 85.7% | 3.84 |
| 6 | 76 | 51 | 67% | 2 | 23 | +\$117.57 | 11.29 | 80.4% | 2.53 |
| 8 | 86 | 72 | 84% | 1 | 13 | +\$207.51 | 11.55 | 76.4% | 1.91 |
| 10 | 90 | 73 | 81% | 1 | 16 | +\$101.97 | 6.17 | 71.2% | 0.62 |
| 15 | 83 | 76 | 92% | — | — | −\$94.74 | **−4.85** | 67.1% | −0.26 |
| 30 | 120 | 110 | 92% | — | — | +\$11.09 | 0.38 | 71.8% | 0.08 |
| 60 | 143 | 129 | 90% | — | — | −\$221.76 | **−6.99** | 71.3% | −1.08 |
| 120 | 183 | 167 | 91% | — | — | −\$166.65 | −3.74 | 72.5% | −0.86 |

Two things are solid and one is not:

- **Solid — `tau ≤ 1` is mechanically dead.** 0/46. The fill lands at/after the close.
- **Solid — the edge really does decay with `tau`, but far outside the 6 s window.** It is flat-ish
  and positive across `tau = 2…8`, gone by `tau ≈ 10`, and **negative from `tau ≈ 15` onward**. This
  **confirms docs/06 §7 "don't widen the window earlier than ~5s"** — but the mechanism is that the
  window is already inside the good zone, not that seconds 5–6 are bad.
- **NOT solid — any gradient *inside* `tau = 2…6`.** 11.7 / 18.8 / 13.0 / 17.9 / 11.3 ¢ on n = 26–51
  fills is noise. docs/06 §5's "17–25¢ at −2s vs 8¢ at −10s" is **not reproducible with the bot's own
  1.5 s signal→fill gap**; that table assumed a 1 s gap and a signal book identical to the fill book
  (`scripts/backtest_1h.py:105` reads the book at `t + LAT` for *both* the signal test and the fill,
  so it structurally cannot produce a `book_moved_no_edge`).

**Survival of the opportunity — conditional on a signal existing at `tau = 6` (n = 76 closes):**

| still signalling at `tau =` | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|
| closes | 67 | 59 | 43 | **44** | 34 |
| share | 88% | 78% | 57% | **58%** | 45% |

*Any* signal still present at `tau ≤ 3`: **70%**. At `tau ≤ 2`: **61%**. At `tau ≤ 1`: **45%**.
**→ 42% of the opportunities visible at `tau = 6` have evaporated by `tau = 2`.**

**Paired P&L on exactly those 76 closes** (what waiting actually earns):

| policy | signals | fills | shares | P&L | ¢/share |
|---|---|---|---|---|---|
| fire at `tau=6` (**current**) | 76 | 51 | 1041.8 | **+\$117.57** | 11.29 |
| hold, fire at `tau=5` | 67 | 44 | 924.4 | +\$175.07 | 18.94 |
| hold, fire at `tau=3` | 43 | 31 | 715.7 | +\$138.72 | 19.38 |
| hold, fire at `tau=2` | 44 | 23 | 580.4 | **+\$52.56** | 9.06 |
| hold, fire at `tau=1` | 34 | **0** | 0 | **\$0.00** | — |

**This is the number the task asked for.** Waiting from 6 → 2 raises ¢/share on the trades you still
get, but you lose 42% of them *and* the surviving ones fill only 47% of the time, so **total dollars
fall by 55%** (\$117.57 → \$52.56). Waiting to `tau = 1` yields **zero**. A naive "just wait" rule is
strictly value-destroying. `audit/A2_replay.md` §4 reached the same verdict independently on the live
period, and my re-run of its CSV confirms it: on the 222 live-period closes, `[1,3]` earns
**\$38.77** vs `[1,6]`'s **\$59.09**.

**Tick-phase check — the single most important robustness result.** The φ = 0 numbers above are one
draw from a random variable. Averaging each policy over five tick phases (φ = 0, .2, .4, .6, .8),
`edge_min = 0.03`, `cap = $25`:

| window | fills | P&L (mean ± sd over φ) | ¢/share | win |
|---|---|---|---|---|
| `[1,6]` (current) | 69.6 | **\$164.71 ± 40.52** | 11.40 | 81.6% |
| `[2,5]` | 63.4 | \$157.13 ± 37.66 | 11.55 | 84.8% |
| `[2,4]` | 54.0 | \$167.75 ± 30.19 | 15.59 | 89.4% |
| `[1,3]` | 46.2 | \$154.51 ± 21.85 | 15.92 | 91.0% |

**At the current \$25 clip every window is the same to within a fraction of its own phase noise.**
Bootstrap (5,000 reps, φ = 0): `[2,5] − [1,6] = +\$69.52`, **95% CI [−\$27.98, +\$182.93]**. Not
significant. **Do not sell the timing change as a P&L improvement at \$25.**

### 1c. Where the timing rule actually earns its keep: the interaction with `per_event_cap_usd`

Same replay, `edge_min = 0.03`, phase-averaged over five φ:

| cap | window | fills | win | P&L (mean ± sd over φ) | ¢/share | **worst single trade** |
|---|---|---|---|---|---|---|
| \$25 | `[1,6]` | 69.6 | 81.6% | \$164.71 ± 40.52 | 11.40 | −\$26 |
| \$25 | `[2,5]` | 63.4 | 84.8% | \$157.13 ± 37.66 | 11.55 | −\$26 |
| \$100 | `[1,6]` | 69.6 | 81.6% | \$472.87 ± 76.96 | 14.12 | −\$104 |
| \$100 | `[2,5]` | 63.4 | 84.8% | \$488.76 ± 76.98 | 17.24 | −\$45 |
| **\$250** | **`[1,6]`** | 69.6 | 81.6% | **\$676.05 ± 134.26** | 14.45 | **−\$258.75** |
| **\$250** | **`[2,5]`** | 63.4 | 84.8% | **\$755.12 ± 79.91** | 19.06 | **−\$45.26** |
| \$250 | `[3,5]` | 61.8 | 84.4% | \$699.15 ± **51.93** | 19.15 | −\$45.26 |
| \$250 | `[2,4]` | 54.0 | 89.4% | \$665.94 ± 118.69 | 21.67 | −\$45.26 |
| \$500 | `[1,6]` | 69.6 | 81.6% | \$712.97 ± 239.59 | 13.22 | **−\$518** |
| \$500 | `[2,5]` | 63.4 | 84.8% | \$854.87 ± 102.03 | 19.57 | −\$45.26 |

Cap sweep at φ = 0, `edge_min = 0.03` — the current window **does not scale**:

| cap | \$25 | \$50 | \$100 | \$250 | \$500 | \$1000 |
|---|---|---|---|---|---|---|
| `[1,6]` P&L | \$136 | \$218 | \$401 | \$581 | **\$473** | **\$28** |
| `[2,5]` P&L | \$206 | \$357 | \$587 | \$832 | \$954 | \$1006 |

Worst rolling-10-trade P&L at \$250 (φ = 0): `[1,6]` **−\$232.09**, `[2,5]` **−\$16.00**,
`[2,4]` −\$12.53.

**Mechanism, not curve-fit.** At `cap = $250`, window `[1,6]`, **all 13 losing trades are `tau = 6`
(11) or `tau = 5` (2)** — none at `tau ≤ 4`. The worst is concrete and diagnosable:

```
close 2026-06-14 00:00:00Z (= Jun 13, 8pm ET)
tau=6   side=down   ask=$0.49   model edge=0.4725   500 shares / $250   LOST   −$258.75
```

The model claimed `fair_down = 0.98` while the book was quoting Down at \$0.49 **and offering 500
shares at that price**. A 47¢ "edge" with deep size is not a mispricing; it is the market telling you
your 6-second-ahead Gaussian is wrong, and it was. This is the identical shape to the window
`audit/A2_replay.md` §5 found (`2026-07-20 04:00Z`, `fair_down = 0.98`, Down collapsing to \$0.13
1.5 s later) — there the \$0.30 `price_min` saved it; here nothing did. **Six seconds of unresolved
BTC risk plus a `fair` frozen at signal time is what makes `tau = 6` the tail.** Confirming split by
depth (fills that consumed the full \$25 clip = deep book): **`tau ∈ [2,5]` → 17.33 ¢/share, 93%
win; `tau ≥ 6` → 12.78 ¢/share, 87% win.** docs/06 §4's "deep books have higher edge" is true
*pooled*, but the deep-and-early cell is the dangerous one.

**Rejected alternative (tested, do not implement):** an `edge_max` filter that skips implausibly
large edges targets the same mechanism but is strictly worse — it kills the winners too and does not
even remove the tail. Phase-averaged, `cap = $250`, `[1,6]`: flat **\$676** → `edge ≤ 0.40` \$480 →
`edge ≤ 0.25` \$274 → `edge ≤ 0.15` \$72, with the worst trade still **−\$209**. Also rejected: an
edge ramp (higher `edge_min` at high `tau`) — \$131/\$155/\$124 for bars of 0.05/0.08/0.12 vs \$136
flat.

**Live-period cross-check** (`audit/A2_replay_tau_scan.csv`, 222 closes 2026-07-16→25, *real
two-sided `book_snapshot_5` ladders*, `edge_min = 0.05`, cap \$25):

| window | signals | fills | shares | P&L | ¢/share |
|---|---|---|---|---|---|
| `[1,6]` (what ran) | 13 | 8 | 144.7 | +\$59.09 | 40.85 |
| **`[2,5]`** | 11 | 8 | 200.9 | **+\$73.38** | 36.53 |
| `[2,4]` | 10 | 7 | 150.2 | +\$45.96 | 30.59 |
| `[1,3]` | 10 | 5 | 131.0 | +\$38.77 | 29.60 |

Independent dataset, independent book source, same direction: `[2,5]` ≥ `[1,6]` (+24%), `[1,3]` ≪
`[1,6]`. This **confirms A2's rejection of "snipe only in the last 2–3s"** while showing A2's implied
"keep `tau = 6`" does not survive a raised cap.

### 1c (answer). RECOMMENDED RULE

> **Fire on the first qualifying tick with `tau ∈ [2.0 s, 5.0 s]`**, where the lower bound is
> derived as `max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs)` so it tracks
> `latency_ms` automatically. Keep "first qualifying second" — do **not** wait for a better one.

Justification, in the order the evidence supports it:

1. **Lower bound `tau ≥ 2.0` — near-certain, mechanical.** 0/46 fills at `tau = 1`; 42/46 land on an
   empty book. It closes defect **D1** (a live order sent after the close). This is a correctness fix
   and would be right even if it changed no P&L.
2. **Upper bound `tau ≤ 5.0` — the enabling condition for the cap raise.** At \$25 it is P&L-neutral
   (CI straddles zero — state this honestly). At \$250 it converts a −\$259 worst trade and a
   −\$232 worst 10-trade run into −\$45 and −\$16, cuts phase-variance by 40%, and *raises* mean P&L
   (\$755 vs \$676). Both independent datasets agree on the sign. Cost: −10% signals, −5% fills.
3. **Do not narrow further.** `[2,4]` and `[1,3]` have the best ¢/share and win rate but fewer fills
   and lower total dollars, and `[1,3]` is clearly worse on the live period (\$38.77 vs \$59.09).
   `[3,5]` is the lowest-variance alternative (± \$51.93) and is an acceptable fallback if the
   implementer prefers stability over mean.

**Honesty flag for whoever reads the P&L afterwards:** the vault's 1h tape is **top-of-book only**,
so the fill model consumes one level. The live bot walks the full ladder up to
`max_walk_above_best = 0.03`. The \$250 P&L figures are therefore a **lower bound on size and, more
importantly, a lower bound on the tail** — a real −\$259 trade could have been worse. This is an
argument for staging the cap raise (see §2.iii).

---

## PART 2 — CHANGE SPEC

Four changes. `(i)` and `(iii)` are coupled and **must ship together or not at all** — raising the
cap without the timing fix is the `[1,6] @ $500` cell (worst trade −\$518, P&L falling).

### (i) Timing rule — `bot/polybot/strategy.py` + `bot/polybot/engine.py` + `bot/config.yaml`

**Design constraint:** keep the `tau` gate in `engine.py`, and put the *bound computation* in
`strategy.py` as a pure function. `evaluate_close_snipe` must stay a pure per-tick predicate with no
window logic (this is why `bot/tests/test_strategy.py` needs no changes).

**(i-a) NEW function in `bot/polybot/strategy.py`**, immediately after `fair_value_up`
(i.e. after line 42), before `@dataclass class SnipeSignal`:

```python
def snipe_tau_bounds(cfg: dict, latency_ms: int) -> Tuple[float, float]:
    """(tau_lo, tau_hi): the seconds-to-close band in which close_snipe may fire.

    tau_hi (`snipe_last_secs`) bounds how EARLY we trade. `fair` is computed at
    signal time and frozen for the fill, so an early signal carries stale-fair
    risk that grows with the per-trade clip. Measured on 1,738 OOS 1h closes
    (audit/A4_change_spec.md Part 1c): at per_event_cap_usd=$250 every losing
    trade in the old (0, 6] window came from tau >= 5, worst single trade
    -$258.75; restricting to [2, 5] moves that to -$45.26 while RAISING mean
    P&L. At the old $25 clip the two are statistically indistinguishable.

    tau_lo bounds how LATE. An order that arrives at or after the close can
    never fill (the book is bulk-cancelled) and, in LIVE mode, would be a real
    FAK order sent into a closed market. Measured: tau=1 signals filled 0 times
    out of 46, 42 of them on an empty book. It is therefore derived from the
    real signal->fill gap, not hardcoded, so it tracks `latency_ms`.
    """
    tau_hi = float(cfg["snipe_last_secs"])
    tau_lo = max(
        float(cfg.get("snipe_min_tau_secs", 2.0)),
        latency_ms / 1000.0 + float(cfg.get("snipe_fill_margin_secs", 0.5)),
    )
    return tau_lo, tau_hi
```

`Tuple` is already imported in `strategy.py` (line 11). No new imports.

**(i-b) `bot/polybot/engine.py` line 28** — extend the existing import:

```python
# OLD
from .strategy import evaluate_close_snipe, resolve_winner, settle_sweep_target
# NEW
from .strategy import (evaluate_close_snipe, resolve_winner, settle_sweep_target,
                       snipe_tau_bounds)
```

**(i-c) `bot/polybot/engine.py` lines 195-200** — the gate:

```python
# OLD
    def _maybe_snipe(self, market: Market, now: float) -> None:
        cfg = self.config.snipe_cfg
        snipe_last = float(cfg["snipe_last_secs"])
        tau = market.close_ts - now
        if not (0 < tau <= snipe_last):
            return

# NEW
    def _maybe_snipe(self, market: Market, now: float) -> None:
        cfg = self.config.snipe_cfg
        tau_lo, tau_hi = snipe_tau_bounds(cfg, int(self.config.execution_cfg["latency_ms"]))
        tau = market.close_ts - now
        # tau_lo is NOT a "too late, give up" case we can log usefully — it is
        # simply outside the tradeable band, same as tau > tau_hi.
        if not (tau_lo <= tau <= tau_hi):
            return
```

Everything below (the `snipe_done` check, family guard, oracle staleness guard, the
`log.info("snipe eval ...")` line) is **unchanged**. The eval-log line already prints `tau`, so the
existing "N eval ticks ÷ window = closes evaluated" audit arithmetic in docs/06 §1 still works — but
the divisor changes from 6 to **3–4** (ticks landing in a 3.0 s band at a 1 Hz cadence). Note this in
the operator README so the next audit does not mis-read coverage as a 45% drop.

**(i-d) `bot/polybot/engine.py`, `start()`** — add one line after L65 (`log.info("starting
engine…")`) so the operator can see the active band without reading config:

```python
        _tl, _th = snipe_tau_bounds(self.config.snipe_cfg,
                                    int(self.config.execution_cfg["latency_ms"]))
        log.info("close_snipe window: tau in [%.2f, %.2f]s (latency_ms=%s)",
                 _tl, _th, self.config.execution_cfg["latency_ms"])
```

**(i-e) `bot/config.yaml` lines 57-59** — under `strategy.close_snipe`:

```yaml
# OLD
  close_snipe:
    snipe_last_secs: 6       # fire in the last N seconds before close
    edge_min: 0.05            # fair - ask - fee(ask) must exceed this

# NEW
  close_snipe:
    # Snipe window is tau in [tau_lo, snipe_last_secs], where
    #   tau_lo = max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs).
    # Measured on 1,738 OOS 1h closes — see audit/A4_change_spec.md Part 1.
    snipe_last_secs: 5           # was 6. Upper tau bound: how EARLY we may fire.
                                 # tau=6 signals carry 6s of unresolved BTC risk against a
                                 # `fair` frozen at signal time; at a $250 clip every large
                                 # loss came from there (worst single trade -$258.75).
    snipe_min_tau_secs: 2.0      # NEW. Floor on the lower tau bound.
    snipe_fill_margin_secs: 0.5  # NEW. Required slack between order arrival and the close.
                                 # tau=1 signals filled 0/46 times (42 hit an empty book);
                                 # in LIVE that is a real order sent into a closed market.
    edge_min: 0.03               # was 0.05 — see (ii)
```

With `latency_ms: 1500` this yields `tau ∈ [2.0, 5.0]`. **If `execution.latency_ms` is ever lowered,
the floor auto-follows down to `snipe_min_tau_secs = 2.0` and no further** — deliberate, because the
measured `tau = 1` bucket is dead even at 1000 ms in half the phase draws.

---

### (ii) `edge_min` 0.05 → 0.03 — `bot/config.yaml` only

Included in the (i-e) block above. **No code change.**

Measured (1,738 OOS closes, φ = 0, `lat = 1500`, `cap = $25`):

| window | `edge_min` | signals | fills | P&L | ¢/share | t |
|---|---|---|---|---|---|---|
| `[1,6]` | 0.05 | 100 | 60 | \$143.49 | 12.13 | 2.72 |
| `[1,6]` | 0.03 | 111 | 70 | \$136.03 | 9.54 | 2.50 |
| **`[2,5]`** | 0.05 | 92 | 54 | \$185.12 | 16.83 | 3.89 |
| **`[2,5]`** | **0.03** | 100 | 66 | **\$205.55** | 15.41 | **4.31** |
| `[2,5]` | 0.02 | 108 | 68 | \$200.87 | 15.13 | 4.27 |

**+22% fills, +11% P&L, higher t-stat in the recommended window; 0.02 adds nothing.** Direction
matches docs/06 §5.2 and A2 §5. Confirmed at `cap = $250` too (\$831.72 at 0.03 vs \$777.88 at 0.05).

**Coupling the implementer must know:** `edge_min` is used **twice** — in
`strategy.evaluate_close_snipe` (the signal test) *and*, via the `edge_fn` closure in
`engine._maybe_snipe` L257/L264, in `fill_engine.walk_asks` (the per-level fill cutoff). Lowering it
also lets the walk go 2¢ deeper into the ladder. That is intended and consistent, and
`execution.max_walk_above_best = 0.03` still bounds it — **do not add a second knob.**

---

### (iii) `per_event_cap_usd` \$25 → \$250, `max_open_notional` \$250 → \$1000

**`bot/config.yaml` lines 74-76:**

```yaml
# OLD
sizing:
  per_event_cap_usd: 25       # paper default; also used as live default until scaled up
  max_open_notional: 250      # global cap across all concurrently open paper/live positions

# NEW
sizing:
  # $25 -> $250 measured at ~5x P&L on 1,738 OOS closes (docs/06 sec.4; independently
  # reproduced in audit/A4_change_spec.md Part 1c). REQUIRES the [2.0, 5.0] snipe window
  # from change (i): in the old (0, 6] window the same cap produces a -$258.75 worst trade
  # and P&L that PEAKS at $250 then falls ($473 at $500, $28 at $1000).
  per_event_cap_usd: 250
  # `_would_exceed_global_cap` is a "stop opening new positions" gate evaluated BEFORE the
  # new trade is sized (engine.py:327-329 uses `>=` on current open notional), so the true
  # ceiling is max_open_notional + per_event_cap_usd = $1250. 1h positions resolve within
  # ~1-2 min of close via the gamma poll, so real concurrency is ~1; $1000 is ~4 clips of
  # headroom and leaves room for a second family later.
  max_open_notional: 1000
```

**No code change.** Two things the implementer must be told:

- **The cap is not a hard ceiling.** `engine._would_exceed_global_cap` (L327-329) tests
  `total_open_notional() >= max_open_notional` *before* the fill is sized, so one more full clip can
  always open on top. Effective ceiling = \$1250. Tightening that to a true ceiling is a separate
  change — **do not fold it in here.**
- **`per_event_cap_usd` is shared with `settle_sweep`** (`engine._maybe_settle` L306). Change (iv)
  turns `settle_sweep` off for 1h and it is already off for 5m/15m/4h, so nothing reaches that line
  today — but re-enabling `settle_sweep` for any family would silently hand it a \$250 clip.
  **Recommended, 4 lines, optional:** add `strategy.settle_sweep.cap_usd: 25` to config and change
  L306 to
  `cap_usd = float(self.config.settle_cfg.get("cap_usd", self.config.sizing_cfg["per_event_cap_usd"]))`.
  If the implementer declines, add a `# NOTE:` comment at L306 instead. Do not leave it silent.

**Sizing reality check** (top-of-book notional available at the fill instant, `edge_min = 0.03`,
`tau ∈ [2,5]`, n = 191): median **\$17.70**, mean \$56.41, p90 \$106.92, max \$2,409.
Only **38.2%** of fill moments offer ≥\$25, **10.5%** offer ≥\$100, **4.7%** offer ≥\$250 — i.e. the
raise binds on ~1 trade in 20, and those few carry most of the incremental P&L *and* all of the tail.
Independently reproduces docs/06 §4 (median \$11.40 there, top-of-book, different window).

**Recommended staging (advice, not a blocker).** `docs/06_live_audit.md` §8 sets the go/no-go at
"~20+ resolved trades, start live at \$25–50". The bot has 7. Shipping \$250 in **PAPER** is the
right call — it is exactly how you collect the tail evidence the top-of-book model cannot give you.
**Do not carry \$250 into LIVE on the strength of this document.** If the operator wants belt and
braces, ship \$100 first (\$488.76 ± 76.98 phase-averaged, worst trade −\$45) and step to \$250 after
20 resolved trades.

---

### (iv) `settle_sweep` OFF for 1h — `bot/config.yaml` line 30

```yaml
# OLD
families:
  1h:
    enabled: true
    duration_secs: 3600
    oracle: binance          # 1h resolves on Binance BTC/USDT 1H candle
    close_snipe: true        # PRIMARY strategy — default ON for 1h only
    settle_sweep: true

# NEW
families:
  1h:
    enabled: true
    duration_secs: 3600
    oracle: binance          # 1h resolves on Binance BTC/USDT 1H candle
    close_snipe: true        # PRIMARY strategy — default ON for 1h only
    # OFF as of A4: 10 days live produced 1,235 signals and 1,235 empty_book —
    # ZERO fills. The post-close book is bulk-cancelled and does not repopulate
    # (docs/05_clob_api_spec.md), so this is pure logging + ~5 wasted REST book
    # fetches per hourly close. `empty_streak_stop: 5` was already capping it at
    # 5 attempts/market (1,235 / 238 closes = 5.2), i.e. the guard worked and the
    # strategy still has no edge to guard. Do NOT re-enable without evidence the
    # book survives the close.
    settle_sweep: false
```

**No code change.** Downstream effects the implementer should verify, not fix:

- `_maybe_settle` is never called for 1h → `settle_winner_cache` is never warmed for 1h.
  `_attempt_resolution` (L396) already handles this:
  `wd = self.settle_winner_cache.get(slug) or resolve_winner(...)`. **Net effect is fewer Binance
  calls, not more** (it previously called `resolve_winner` on every settle tick too).
- `settle_empty_streak` / `in_flight_settle` / `settle_done` stay empty for 1h. Harmless.
- The `settle_sweep` code path is now **dead for every configured family**. Leave it in — it is the
  re-entry point once a real Chainlink oracle lands. Do not delete.

---

## PART 3 — TESTS

### 3a. Existing tests: **none break.** `cd bot && python3 -m pytest tests/ -q` → 54 passed (verified against the current tree).

| file | why it survives |
|---|---|
| `tests/test_strategy.py` | `CFG` carries `snipe_last_secs: 6` but `evaluate_close_snipe` **never reads it** — the window gate lives in `engine.py`, and the spec keeps it there. Its `now = 997.0` / `close = 1000.0` cases are `tau = 3`, inside the new band anyway. |
| `tests/test_config.py` | asserts only `1h.close_snipe`, `5m.close_snipe`, `fee_rate`, `paper`. None change. |
| `tests/test_ledger.py` | uses a **local** config dict (L22-24), not `bot/config.yaml`. |
| `tests/test_fill_engine.py`, `test_execution.py`, `test_oracle.py`, `test_polymarket.py` | untouched code. |

### 3b. Existing tests that must be **extended** (they pass today but stop guarding the change)

1. **`tests/test_config.py::test_loads_real_config_yaml`** — add regression assertions pinning the
   four new/changed config values, so a future edit cannot silently undo this work:
   ```python
   self.assertEqual(c.families()["1h"].settle_sweep, False)   # (iv)
   self.assertAlmostEqual(c.snipe_cfg["edge_min"], 0.03)      # (ii)
   self.assertEqual(c.snipe_cfg["snipe_last_secs"], 5)        # (i)
   self.assertAlmostEqual(c.snipe_cfg["snipe_min_tau_secs"], 2.0)
   self.assertEqual(c.sizing_cfg["per_event_cap_usd"], 250)   # (iii)
   self.assertEqual(c.sizing_cfg["max_open_notional"], 1000)
   ```
2. **`tests/test_strategy.py`** — change `CFG` to
   `{"snipe_last_secs": 5, "snipe_min_tau_secs": 2.0, "edge_min": 0.03, ...}` so the fixture mirrors
   shipped config. The four `now = 997.0` cases (`tau = 3`) still sit inside the band and their
   assertions hold at `edge_min = 0.03` (the fires-case edge is 0.1997, the no-fire cases are ≤0).
   Cosmetic but prevents a stale fixture drifting from reality.

### 3c. NEW tests — the real gap: **`engine.py` has no test file at all.**

`_maybe_snipe` and `_maybe_settle` are 100% untested today, and (i) modifies exactly `_maybe_snipe`.

**NEW FILE `bot/tests/test_snipe_window.py`** — pure, no Engine construction, no network:

| # | test | assertion |
|---|---|---|
| 1 | `test_bounds_from_config` | `snipe_tau_bounds({"snipe_last_secs":5,"snipe_min_tau_secs":2.0,"snipe_fill_margin_secs":0.5}, 1500) == (2.0, 5.0)` |
| 2 | `test_floor_tracks_latency` | `latency_ms=3000` → `tau_lo == 3.5` (latency dominates the 2.0 floor) |
| 3 | `test_floor_never_below_min` | `latency_ms=250` → `tau_lo == 2.0` (the 2.0 floor dominates) — the measured `tau=1` bucket stays excluded even if latency is tuned down |
| 4 | `test_defaults_when_new_keys_absent` | `snipe_tau_bounds({"snipe_last_secs":5}, 1500) == (2.0, 5.0)` — an old config.yaml must not crash the bot |
| 5 | `test_fill_lands_before_close` | property: for every `tau` in the band, `tau - latency_ms/1000 >= snipe_fill_margin_secs`. **This is the D1 regression guard** — it fails if anyone reintroduces a `tau < latency` firing point |
| 6 | `test_band_is_non_empty` | `tau_lo < tau_hi`; a config with `snipe_last_secs: 2` and `latency_ms: 3000` must be detectable as a misconfiguration rather than silently never firing |

**NEW FILE `bot/tests/test_engine_gating.py`** — the gate as wired, using a minimal fake so no
network is touched. Construct `Engine` against a temp-dir config (pattern: copy `bot/config.yaml`,
override `storage.*` to `tmp_path`), then monkeypatch `self.binance`, `self.clob`, `self.ledger`,
and `self.executor` with stubs and call `engine._maybe_snipe(fake_market, now)` directly:

| # | test | assertion |
|---|---|---|
| 7 | `test_no_fire_above_window` | `tau = 5.6` → `executor.submit` **not** called, `record_snipe_signal` not called |
| 8 | `test_no_fire_below_window` | `tau = 1.9` → not called. **The bug this whole change exists to fix.** |
| 9 | `test_fires_inside_window` | `tau = 3.0` with a mispriced stub book → `executor.submit` called exactly once, `market.slug in engine.snipe_done` |
| 10 | `test_fires_at_band_edges` | `tau = 2.0` and `tau = 5.0` both fire (bounds inclusive, matching `tau_lo <= tau <= tau_hi`) |
| 11 | `test_one_entry_per_window` | two calls at `tau = 4.0` and `tau = 3.0` → exactly one `submit` (`snipe_done` still honoured) |
| 12 | `test_stale_oracle_skips` | `binance.latest().ts = now - 6` → no fire (the existing L218 guard, now covered) |
| 13 | `test_settle_sweep_off_for_1h` | with shipped config, `engine._tick()` over a post-close 1h market never calls `_maybe_settle` (guards change (iv) at the wiring level, not just the YAML) |
| 14 | `test_cap_passed_to_fill_is_config_value` | the `cap_usd` argument reaching `_run_fill` equals `sizing.per_event_cap_usd` — guards (iii) against a hardcoded 25 creeping back |

**Target: 54 → ~72 passing.** Tests 5, 8 and 13 are the ones that must exist; the rest are cheap.

### 3d. Not a test, but do it: **replay the change before deploying**

```bash
python3 scripts/a4_timing_scan.py --edge-min 0.03 --cap-usd 250     # expect [2,5] ~ $832, worst -$45
python3 scripts/replay_live_period.py --tau-scan --depth --edge-min 0.03   # live-period cross-check
```

---

## PART 4 — DOCS THAT ARE NOW WRONG

The implementer should correct these in the same commit; leaving them creates a contradictory record.

| doc | statement | correction |
|---|---|---|
| `docs/06_live_audit.md` §5.1 | "Later is dramatically better. −2s/−1s yields 17–25¢, −30s/−15s is negative" | Half right. The **decay outside ~10 s is confirmed and stronger than stated** (−4.85¢ at `tau=15`, −6.99¢ at `tau=60`). But the −2s/−1s cell **assumed a 1 s signal→fill gap**; at the shipped 1.5 s it is unreachable (`tau=1` fills 0/46), and there is no significant gradient inside `tau = 2…6`. |
| `docs/06_live_audit.md` §6 lever 2 | "Snipe only in the last **2–3s** → ~2× EV/share" | **Refuted on both datasets.** `[1,3]` earns \$38.77 vs `[1,6]`'s \$59.09 on the live period and has the *lowest* total P&L of any candidate window OOS. Replace with: "*bound the window at both ends to `[2.0, 5.0]`; it is P&L-neutral at \$25 and is the precondition for lever 1*". |
| `docs/06_live_audit.md` §4 | cap table monotone to \$1000 (\$1,169) | Only true once the window is narrowed. In the **shipped** window P&L peaks near \$250 and collapses (\$473 at \$500, \$28 at \$1000). |
| `docs/06_live_audit.md` §7 | "Don't widen the snipe window earlier than ~5s" | **Strengthen and keep** — it is the best-supported claim in that section, and it now also means *don't sit at 6s*. |
| `audit/A2_replay.md` §4 | "Recommendation: do NOT adopt lever #2 as written" | **Confirmed** for "last 2–3s" / "last qualifying second". Add that `[2,5]` is a different rule and **beats `[1,6]` on A2's own data** (\$73.38 vs \$59.09), and that A2's `tau=6`-is-best conclusion holds only at the \$25 clip. |
| `bot/README.md` | operator arithmetic "eval ticks ÷ 6 = closes evaluated" | Divisor becomes **3–4** after (i). Flag so the next coverage audit is not misread as a 45% throttle. |

---

## PART 5 — SUMMARY OF EVERY EDIT

| # | file | location | change |
|---|---|---|---|
| 1 | `bot/polybot/strategy.py` | after L42 | **add** `snipe_tau_bounds(cfg, latency_ms)` |
| 2 | `bot/polybot/engine.py` | L28 | add `snipe_tau_bounds` to the `.strategy` import |
| 3 | `bot/polybot/engine.py` | L195-200 | replace `0 < tau <= snipe_last` with `tau_lo <= tau <= tau_hi` |
| 4 | `bot/polybot/engine.py` | `start()`, after L65 | log the active band once at startup |
| 5 | `bot/config.yaml` | L58 | `snipe_last_secs: 6` → `5` |
| 6 | `bot/config.yaml` | after L58 | **new** `snipe_min_tau_secs: 2.0`, `snipe_fill_margin_secs: 0.5` |
| 7 | `bot/config.yaml` | L59 | `edge_min: 0.05` → `0.03` |
| 8 | `bot/config.yaml` | L75 | `per_event_cap_usd: 25` → `250` |
| 9 | `bot/config.yaml` | L76 | `max_open_notional: 250` → `1000` |
| 10 | `bot/config.yaml` | L30 | `1h.settle_sweep: true` → `false` |
| 11 | `bot/polybot/engine.py` | L306 | *optional* — decouple settle sizing from `per_event_cap_usd`, or add a `# NOTE:` |
| 12 | `bot/tests/test_config.py` | `test_loads_real_config_yaml` | add 6 regression assertions |
| 13 | `bot/tests/test_strategy.py` | L22-23 | sync `CFG` with shipped values |
| 14 | `bot/tests/test_snipe_window.py` | **new** | tests 1–6 |
| 15 | `bot/tests/test_engine_gating.py` | **new** | tests 7–14 |
| 16 | `docs/06_live_audit.md`, `audit/A2_replay.md`, `bot/README.md` | see Part 4 | correct the timing claims |

**Expected effect on the paper bot** (1,738 OOS closes, phase-averaged, all four changes together):
signals ~111 → ~100/72 days, fills ~70 → ~63, **P&L \$164.71 → \$755.12** (driven by the cap, enabled
by the window), ¢/share 11.40 → 19.06, win 81.6% → 84.8%, **worst single trade −\$258.75 → −\$45.26**,
worst 10-trade run −\$232 → −\$16. Frequency is unchanged at ~1 trade/day — **only lever 3
(Chainlink → 5m/15m) moves that**, exactly as docs/06 §5.3 concluded.
