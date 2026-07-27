# B1 — Implementation of the A4 strategy/sizing change spec

**Status: implemented, tested, replay-verified against the shipped code, dry-run clean.**
All four changes in `audit/A4_change_spec.md` Part 2 are in the tree. The spec's measured
recommendation was followed exactly — `tau ∈ [2.0, 5.0]` derived from `latency_ms`, **not**
the "snipe only in the last 2–3s" rule from `docs/06_live_audit.md` §6, which A4 refuted and
which this implementation therefore does not contain.

Bot remains in **PAPER** mode. No live-mode guard was touched.

---

## 1. What changed

### (i) Timing rule — the tau band is now bounded at **both** ends

`bot/polybot/strategy.py` — new pure function after `fair_value_up`:

```python
def snipe_tau_bounds(cfg: dict, latency_ms: int) -> Tuple[float, float]:
    tau_hi = float(cfg["snipe_last_secs"])
    tau_lo = max(
        float(cfg.get("snipe_min_tau_secs", 2.0)),
        latency_ms / 1000.0 + float(cfg.get("snipe_fill_margin_secs", 0.5)),
    )
    return tau_lo, tau_hi
```

`bot/polybot/engine.py::_maybe_snipe` — the gate:

```python
# OLD — no lower bound at all: could fire at tau = 0.02s
snipe_last = float(cfg["snipe_last_secs"])
tau = market.close_ts - now
if not (0 < tau <= snipe_last):
    return

# NEW
tau_lo, tau_hi = snipe_tau_bounds(cfg, int(self.config.execution_cfg["latency_ms"]))
tau = market.close_ts - now
# tau_lo is NOT a "too late, give up" case we can log usefully — it is
# simply outside the tradeable band, same as tau > tau_hi.
if not (tau_lo <= tau <= tau_hi):
    return
```

Everything below the gate was left alone by this change: `snipe_done` one-shot, the non-1h guard,
the 5 s oracle-staleness guard, the `snipe eval` log line. `evaluate_close_snipe` stays a pure
per-tick predicate with **no** window logic, exactly as the spec's design constraint required —
which is why `tests/test_strategy.py` needed only a fixture refresh.

> Concurrency note: the parallel Chainlink workstream has since refactored the oracle-input block
> below the gate into `Engine._snipe_inputs()`, replacing the flat `market.family != "1h"` return
> with a per-family dispatch that still skips when no oracle is available. The gate itself, its
> position, and every test here are unaffected — `test_non_1h_family_still_blocked` passes against
> both shapes.

`engine.start()` now logs the active band once, plus a warning if it is empty:

```
2026-07-27T12:31:11.633Z INFO polybot.engine: close_snipe window: tau in [2.00, 5.00]s (latency_ms=1500)
```

**Why this rule and not `[1,3]`.** Two independent datasets say waiting is value-destroying:
`[1,3]` earns \$38.77 vs `[1,6]`'s \$59.09 on the live period, and `tau = 1` filled **0 of 46**
times OOS (42 on an empty book). The lower bound is a *correctness* fix — in LIVE it stops a real
FAK order being sent into a closed market (defect D1). The upper bound is the *enabling condition
for the cap raise*, and is honestly P&L-neutral at \$25.

### (ii) `edge_min` 0.05 → 0.03 — config only, no code change

Same knob still feeds both the signal test and the `walk_asks` per-level cutoff, still bounded by
`execution.max_walk_above_best: 0.03`. No second knob was added, per the spec.

### (iii) `per_event_cap_usd` \$25 → \$250, `max_open_notional` \$250 → \$1000 — config only

Coupled to (i) and shipped with it. Both the \$1,250 effective-ceiling caveat
(`_would_exceed_global_cap` is a pre-sizing gate, unchanged) and the PAPER-only framing are
recorded in the config comments and `bot/README.md`.

### (iv) `settle_sweep` OFF for 1h — config only

`families.1h.settle_sweep: true → false`. It was already off for 5m/15m/4h, so the strategy is now
dead for every configured family. The **code path was left in place**, as instructed — it is the
re-entry point if a post-close book that survives is ever observed.

### (iii, addendum) settle_sweep no longer inherits the close_snipe clip

The spec's *recommended, optional* 4-line decoupling was taken rather than the `# NOTE:` fallback,
because the failure mode is silent: re-enabling `settle_sweep` under the old code would have handed
it a \$250 clip on zero evidence.

```python
cap_usd = float(self.config.settle_cfg.get(
    "cap_usd", self.config.sizing_cfg["per_event_cap_usd"]))
```

with `strategy.settle_sweep.cap_usd: 25` in config. The `.get(...)` fallback keeps an old config
working.

### Backward compatibility

Every new key is read with `cfg.get(key, default)` carrying the spec's default. A `config.yaml`
predating this change loads and runs: missing `snipe_min_tau_secs`/`snipe_fill_margin_secs` yields
`[2.0, snipe_last_secs]`; missing `settle_sweep.cap_usd` falls back to `per_event_cap_usd`. This is
covered by `test_defaults_when_new_keys_absent`.

---

## 2. Safety properties — all preserved, and now test-pinned

| property | status | pinned by |
|---|---|---|
| 3 live guards (`mode.paper` + `POLYBOT_LIVE=1` + `POLYBOT_PK`) | untouched | `test_config.py` (3 pre-existing tests) + `test_safety_invariants_still_hold` |
| `fair_cap: 0.98` | untouched | `test_safety_invariants_still_hold` |
| `sigma_1s_floor: 8e-06` | untouched | `test_safety_invariants_still_hold` |
| `max_walk_above_best: 0.03` (walk slippage bound) | untouched | `test_safety_invariants_still_hold` |
| per-event cap flows from config to the fill worker | raised, not bypassed | `test_cap_passed_to_fill_is_config_value` |
| global `max_open_notional` gate | raised, logic unchanged | `test_global_cap_still_blocks_fills` |
| `price_min/price_max` band | untouched | `test_strategy.py` |
| oracle-staleness skip | untouched | `test_stale_oracle_skips` |
| non-1h `close_snipe` defensive guard | untouched | `test_non_1h_family_still_blocked` |

The change is **net risk-reducing on the tail** despite the 10× clip: worst single trade
−\$258.75 → −\$45.26, worst 10-trade run −\$232 → −\$16 (measured, §4).

---

## 3. Tests

`cd /home/user/S4/bot && python3 -m pytest tests/ -q` → **all pass, 0 failures**.

- Baseline before this work: **54 passed**.
- Immediately after this change: **75 passed** (54 + 21 added here). This is the number attributable
  to B1.
- Final count at time of writing: **132 passed** — the tree is shared with the concurrent
  Chainlink-oracle workstream, which is still adding tests, so the total is a moving number. Every
  re-run during this work (75 → 124 → 132) was green.
- The four files this change touched contribute **37 passing tests** (`test_snipe_window.py` 6,
  `test_engine_gating.py` 13, plus `test_config.py` and `test_strategy.py`).

**New file `bot/tests/test_snipe_window.py` (6 tests)** — pure, no Engine, no network:

| test | asserts |
|---|---|
| `test_bounds_from_config` | shipped keys + 1500 ms → exactly `(2.0, 5.0)` |
| `test_floor_tracks_latency` | 3000 ms → `tau_lo = 3.5` (latency dominates) |
| `test_floor_never_below_min` | 250 ms and 0 ms → `tau_lo = 2.0` (floor dominates; the dead `tau=1` bucket stays excluded even if latency is tuned down) |
| `test_defaults_when_new_keys_absent` | old config → `(2.0, 5.0)`; legacy `snipe_last_secs: 6` → `(2.0, 6.0)` |
| `test_fill_lands_before_close` | **D1 regression guard.** Property over 7 latencies × the whole band: `tau − latency ≥ snipe_fill_margin_secs` at every admissible tau |
| `test_band_is_non_empty` | `tau_lo < tau_hi`; and a misconfiguration (`snipe_last_secs: 2`, 3000 ms) is *detectable* as `tau_lo ≥ tau_hi` rather than silently never firing |

**New file `bot/tests/test_engine_gating.py` (13 tests)** — the gate *as wired*. `engine.py` had no
test file at all before this; `_maybe_snipe` was 100% untested. Engine is built against the real
`bot/config.yaml` with `storage`/`logging` redirected to a temp dir, then `binance`/`clob`/`ledger`/
`executor` are replaced with stubs — **no socket is opened**.

| test | asserts |
|---|---|
| `test_no_fire_above_window` | `tau = 5.6` → no submit, no signal recorded, **and no book fetched** (gated before the REST calls) |
| `test_no_fire_below_window` | `tau = 1.9` → nothing. *The bug this change exists to fix.* |
| `test_no_fire_at_legacy_tau_values` | every tau the old `(0, 6]` gate admitted but the new one must not — `0.02, 0.5, 1.0, 1.5, 5.5, 6.0` — is silent |
| `test_fires_inside_window` | `tau = 3.0` + mispriced stub book → exactly one submit, slug in `snipe_done` |
| `test_fires_at_band_edges` | `tau = 2.0` and `tau = 5.0` both fire (bounds inclusive) |
| `test_one_entry_per_window` | two in-band ticks on one slug → exactly one submit (**does not double-fire**) |
| `test_stale_oracle_skips` | oracle 6 s old → no fire |
| `test_non_1h_family_still_blocked` | 5m market at `tau = 3` → no fire |
| `test_window_tracks_latency_ms` | `latency_ms = 3000` → `tau = 3.0` stops firing, `tau = 4.0` still fires |
| `test_cap_passed_to_fill_is_config_value` | the `cap_usd` **and** `edge_min` reaching `_run_fill` equal the config values, and `cap_usd == 250.0` — guards against a hardcoded 25 creeping back. Bound by `inspect.signature`, so it survives argument reordering |
| `test_global_cap_still_blocks_fills` | over `max_open_notional` → signal recorded, **no fill dispatched** |
| `test_settle_sweep_off_for_1h` | `engine._tick()` over a post-close 1h market never reaches `_maybe_settle` — guards (iv) at the *wiring* level, not just the YAML |
| `test_settle_sweep_has_its_own_cap` | with settle_sweep force-enabled, the clip reaching `_run_fill` is \$25, strictly less than `per_event_cap_usd` |

**Extended `bot/tests/test_config.py` (+2 tests)** — `test_a4_shipped_values_are_pinned` pins all
eight changed/new config values; `test_safety_invariants_still_hold` pins the guards above.

**Refreshed `bot/tests/test_strategy.py`** — `CFG` now mirrors shipped values
(`snipe_last_secs: 5`, `snipe_min_tau_secs: 2.0`, `snipe_fill_margin_secs: 0.5`, `edge_min: 0.03`).
Its `tau = 3` cases sit inside the new band and its assertions hold at 0.03 (the fires-case edge is
0.1997; the no-fire cases are ≤ 0). No test broke.

### The new tests were verified to actually fail against the old code

Temporarily restoring the old `if not (0 < tau <= 6)` gate produces
**`4 failed, 71 passed`** — `test_no_fire_above_window`, `test_no_fire_below_window`,
`test_no_fire_at_legacy_tau_values`, `test_window_tracks_latency_ms`. The gate is genuinely guarded,
not vacuously green.

---

## 4. Replay verification — the *shipped code*, not the analysis

`scripts/a4_*.py` hardcode their candidate windows. To verify the **implementation**, the replay was
re-run reading `bot/config.yaml` and calling the shipped `strategy.snipe_tau_bounds()` to derive the
band, on the same 1,738 OOS closes (2026-05-01 → 2026-07-12), phase-averaged over φ ∈ {0, .2, .4, .6, .8}:

```
SHIPPED config -> snipe_tau_bounds = [2.0, 5.0]  edge_min=0.03  cap=$250.0  latency_ms=1500
windows=1740 with-tape=1738

=== cap = $25 ===
policy                            fills   win%          P&L mean±sd    c/sh     worst
OLD (0,6] @ shipped edge/cap       69.6   81.6     164.71 ±  40.52   11.40    -26.22
SHIPPED [2,5]                      63.4   84.8     157.13 ±  37.66   11.55    -26.09

=== cap = $250 ===
policy                            fills   win%          P&L mean±sd    c/sh     worst
OLD (0,6] @ shipped edge/cap       69.6   81.6     676.05 ± 134.26   14.45   -258.75
SHIPPED [2,5]                      63.4   84.8     755.12 ±  79.91   19.06    -45.26
```

The `edge_min` change (ii) was re-checked the same way, inside the window the code actually derives
(φ = 0, lat = 1500 ms, cap \$25):

```
shipped window [2,5]  (phase 0, lat=1500, cap=$25)
 edge_min  signals  fills       P&L    c/sh      t
     0.05       88     54    185.12   16.83   3.89
     0.03      100     66    205.55   15.41   4.31
     0.02      108     68    200.87   15.13   4.27
```

Matches A4 Part 2 (ii) on fills, P&L, ¢/share and t-stat (0.03 is the peak on both P&L and t; 0.02
adds nothing). The only discrepancy anywhere in the reproduction is the `edge_min = 0.05` signal
count, 88 here vs 92 in A4 — a signal-counting nuance, not a P&L one: fills (54), P&L (\$185.12),
¢/share (16.83) and t (3.89) all agree exactly.

The window comparison reproduces A4 Part 1c **to the cent** (\$164.71 ± 40.52 / \$157.13 ± 37.66 at \$25;
\$676.05 ± 134.26 / \$755.12 ± 79.91 at \$250; worst −\$258.75 → −\$45.26). Phase-variance falls 40%.
It also reproduces the honest part: **at \$25 the window change is not an improvement** — it is
−\$7.58 mean, well inside its own ±\$40 phase noise. The window earns its keep only as the
precondition for the cap.

The spec's Part 3d command was also run as written
(`scripts/a4_timing_scan.py --edge-min 0.03 --cap-usd 250`), and lands on its predicted value:

```
window  signals  fills      win   shares  notional      pnl  c_per_share   t_stat  pnl_per_day
 [2,5]      100     66    87.88  3849.51   2761.99   831.72        21.61     4.31        11.49   <- shipped
 [1,6]      111     70    81.43  5152.78   3600.46   580.85        11.27     2.50         8.02   <- old
 [3,5]       96     64    87.50  3432.21   2436.65   743.94        21.68     4.11        10.27
 [2,4]       89     57    85.96  3402.70   2474.37   680.38        20.00     3.58         9.40
 [1,3]       83     50    94.00  3050.84   2289.50   617.68        20.25     4.93         8.53
```

Spec expectation: "\[2,5] ~ \$832" → measured **\$831.72**. `[2,5]` is the best window on total
dollars *and* on ¢/share-adjusted t-stat among the wide candidates, and the shipped choice is not
the product of picking a lucky cell: every window whose lower bound is ≥ 2 and upper bound ≤ 5 lands
in the 20–22 ¢/share band, while every window that includes `tau = 6` collapses to ~10–11 ¢/share.

---

## 5. Dry sanity run

`cd /home/user/S4/bot && timeout 90 python3 -m polybot.main run` — clean start, discovery, no
exceptions (0 lines matching `traceback|exception|error` across the run):

```
2026-07-27T12:31:11.632Z INFO polybot.engine: starting engine: mode=PAPER
2026-07-27T12:31:11.633Z INFO polybot.engine: close_snipe window: tau in [2.00, 5.00]s (latency_ms=1500)
2026-07-27T12:31:11.633Z INFO polybot.engine: status HTTP server listening on :8899 (/status, /health)
2026-07-27T12:31:13.557Z INFO polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-8am-et family=1h close=2026-07-27T13:00:00+00:00
2026-07-27T12:31:13.557Z INFO polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-9am-et family=1h close=2026-07-27T14:00:00+00:00
...
2026-07-27T12:31:13.560Z INFO polybot.engine: discovery: 44 markets tracked (44 new)
2026-07-27T12:31:43.220Z INFO polybot.engine: discovery: 44 markets tracked (0 new)
2026-07-27T12:32:14.880Z INFO polybot.engine: stopping engine
```

`snipe eval` lines only appear inside the band, i.e. 2–5 s before an hourly close, so a second run
was timed across the 13:00:00 UTC close to exercise that path against live books and a live oracle:

```
2026-07-27T12:59:05.675Z INFO polybot.engine: starting engine: mode=PAPER
2026-07-27T12:59:05.675Z INFO polybot.engine: close_snipe window: tau in [2.00, 5.00]s (latency_ms=1500)
2026-07-27T12:59:05.676Z INFO polybot.engine: status HTTP server listening on :8899 (/status, /health)
...
2026-07-27T12:59:07.610Z INFO polybot.engine: discovery: 41 markets tracked (41 new)
2026-07-27T12:59:37.113Z INFO polybot.engine: discovery: 41 markets tracked (0 new)
2026-07-27T12:59:56.589Z INFO polybot.engine: snipe eval bitcoin-up-or-down-july-27-2026-8am-et tau=4.4s S=65077.87 S_open=65100.79 sig1s=1.34e-05 askU=0.01 askD=None -> no_edge
2026-07-27T12:59:56.946Z INFO polybot.engine: snipe eval bitcoin-up-or-down-july-27-2026-8am-et tau=3.4s S=65077.86 S_open=65100.79 sig1s=1.33e-05 askU=0.01 askD=None -> no_edge
2026-07-27T12:59:57.971Z INFO polybot.engine: snipe eval bitcoin-up-or-down-july-27-2026-8am-et tau=2.4s S=65077.86 S_open=65100.79 sig1s=1.33e-05 askU=0.01 askD=None -> no_edge
2026-07-27T13:00:08.516Z INFO polybot.engine: discovered market: bitcoin-up-or-down-july-27-2026-12pm-et family=1h close=2026-07-27T17:00:00+00:00
2026-07-27T13:00:08.517Z INFO polybot.engine: discovery: 44 markets tracked (3 new)
2026-07-27T13:00:35.472Z INFO polybot.engine: stopping engine
```

**This is the change working end-to-end against live books and a live oracle.** Read the tau column:

- **Exactly 3 eval ticks — `tau = 4.4, 3.4, 2.4` — all inside `[2.0, 5.0]`.** The tick phase for this
  close was ≈ 0.4 s, so the old `(0, 6]` gate would have produced **6** ticks here, at
  `tau = 5.4, 4.4, 3.4, 2.4, 1.4, 0.4`. **Both new bounds fired**: nothing at `tau = 5.4` (the early
  end that produced every large loss at a \$250 clip) and nothing at `tau = 1.4` or `0.4` (the end
  that fills 0/46 and in LIVE would be an order into a closed market — defect D1, now closed).
- The **÷3–4 divisor** predicted for the coverage arithmetic is observed directly: 3 lines, 1 close.
- `-> no_edge` is correct, not a miss: `askU = 0.01` is below `price_min = 0.30`, so the price band
  filtered it. `askD = None` (no down-side book at all) was handled without error.
- 0 exceptions, 0 tracebacks. Discovery ran throughout (41 → 44 markets), the new hour's market was
  picked up 8 s after the close, and the engine shut down cleanly.
- No `settle_sweep` activity after the close — change (iv) confirmed live.

**Note for the next coverage audit:** `docs/06_live_audit.md` §1 divided `snipe eval` line counts by
**6**. With a 3.0 s band at a 1 Hz tick the divisor is now **3–4**. A ~45% drop in eval lines is the
intended effect of this change, **not** a throttle. Flagged in `bot/README.md`.

---

## 6. Files changed

| file | change |
|---|---|
| `bot/polybot/strategy.py` | **+1 function** `snipe_tau_bounds(cfg, latency_ms)` (26 lines incl. docstring) after `fair_value_up`. No other change; `evaluate_close_snipe` untouched. |
| `bot/polybot/engine.py` | import `snipe_tau_bounds`; `_maybe_snipe` gate `0 < tau <= snipe_last` → `tau_lo <= tau <= tau_hi`; `start()` logs the band + warns on an empty band; `_maybe_settle` reads `settle_cfg.get("cap_usd", …)` instead of `per_event_cap_usd`. |
| `bot/config.yaml` | `1h.settle_sweep: true→false`; `snipe_last_secs: 6→5`; **new** `snipe_min_tau_secs: 2.0`, `snipe_fill_margin_secs: 0.5`; `edge_min: 0.05→0.03`; **new** `settle_sweep.cap_usd: 25`; `per_event_cap_usd: 25→250`; `max_open_notional: 250→1000`. Each with the measurement that justifies it in a comment. |
| `bot/tests/test_snipe_window.py` | **new**, 6 tests |
| `bot/tests/test_engine_gating.py` | **new**, 13 tests (first test coverage of `engine.py`) |
| `bot/tests/test_config.py` | +2 tests (8 config pins + 5 safety pins) |
| `bot/tests/test_strategy.py` | `CFG` fixture synced to shipped values |
| `bot/README.md` | close_snipe described as a two-sided band; the ÷6 → ÷3–4 operator note; settle_sweep marked OFF everywhere with the 1,235/0 evidence; separate settle clip documented; \$250-is-PAPER and the \$1,250 effective-ceiling caveat added to the live-mode checklist |
| `docs/06_live_audit.md` | correction banner at the top; §5.1 struck through and superseded; §6 lever 2 rewritten as the two-sided band with its honest "P&L-neutral at \$25" framing; §7 first bullet strengthened |
| `audit/A2_replay.md` | §4 confirmed, plus the two refinements: `[2,5]` beats `[1,6]` on A2's own data (\$73.38 vs \$59.09), and A2's implied "keep `tau=6`" holds only at the \$25 clip |

Diffstat against the pre-A4 baseline (`d1da7cf`) for these files: **716 insertions, 54 deletions**.
Note that `engine.py`, `strategy.py` and `config.yaml` are shared with the concurrent Chainlink
workstream, so their line counts in that diffstat are not all B1's. B1's own footprint in production
code is small and surgical: **~15 lines in `engine.py`** (import, 4-line gate, 2 startup log
statements, 2-line settle-cap lookup), **~31 lines in `strategy.py`** (one new function plus a
docstring correction), and **~35 lines of `config.yaml`**, most of which are the comments recording
why each number is what it is.

Not committed — the orchestrator commits.

---

## 7. Things deliberately **not** done

- **`edge_max` / edge-ramp filters.** A4 tested and rejected both; they kill winners and do not
  remove the tail.
- **Turning `max_open_notional` into a true hard ceiling.** Real, but a separate change the spec
  explicitly says not to fold in. The \$1,250 effective ceiling is documented instead.
- **Deleting the `settle_sweep` code path.** Config-disabled only, per the spec.
- **Defect D2** (`_tick` iterates markets in discovery order, not by `close_ts`, with blocking REST
  book fetches inside one tick sharing a single `now`). Invisible today because at most one 1h
  market is ever in-window. It becomes real the moment 5m/15m are enabled — **the Chainlink
  workstream must fix this before enabling those families, or their recorded `tau` will be hundreds
  of ms optimistic.** Out of scope here, flagged so it is not inherited silently.
- **\$250 in LIVE.** Shipped in PAPER only. `docs/06_live_audit.md` §8 sets the go/no-go at ~20
  resolved trades and a \$25–50 starting clip; the bot has 7. The \$250 paper run is precisely how
  the tail evidence the top-of-book replay cannot provide gets collected.

## 8. Expected effect on the paper bot

Phase-averaged over the 1,738 OOS closes, all four changes together: signals ~111 → ~100, fills
~70 → ~63, **P&L \$164.71 → \$755.12**, ¢/share 11.40 → 19.06, win 81.6% → 84.8%, worst single trade
−\$258.75 → −\$45.26, worst 10-trade run −\$232 → −\$16. Frequency stays ~1 trade/day on 1h — nothing
in this change moves that, and nothing can; only more families do.
