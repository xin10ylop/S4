# C2 — Staleness-filtered 5m replay harness, and its control test

**Date:** 2026-07-27 · **Scope:** build `scripts/fresh5m/replay.py`, prove it against the verifier's
published 43-day numbers, and have it ready for the fresh ~20-day fetch.
**Status: harness built, control test reproduces the verifier EXACTLY, 0 property-test mismatches.**

| deliverable | result |
|---|---|
| `scripts/fresh5m/replay.py` | built — shipped bot logic ported, staleness filter, strict causality, day-clustered stats, stress knobs |
| Control: `+8.38¢ / t=7.56` after removing stale fills | **reproduced: +8.3756¢, t=7.563** |
| Control: `71 of 1,503 fills, 14% of P&L` | **reproduced: 71 of 1,503, 14.04%** |
| Control: `+10.89¢ / t=10.33` at shipped params | **reproduced: +10.892¢, t=10.334** |
| Property test vs `bot.polybot.strategy` / `fill_engine` | **0 mismatches** on 80,000 randomised inputs |
| Second control on the fresh 5-day tape (different file format) | reproduces `docs/07` §3.2–3.3 within 0.2¢ |

Everything below is computed by an independently written harness reading raw parquet. It does **not**
reuse `data/a3/evals_all.parquet` or any A3 script.

---

## 1. What was built

`scripts/fresh5m/replay.py` — one file, two sub-commands (`control`, `run`), plus
`scripts/fresh5m/control_report.py` (the sweep driver) and `scripts/fresh5m/proptest.py`.

### 1.1 The bot logic is ported, not re-derived

| harness function | source of truth |
|---|---|
| `fair_value_up_port` | `bot/polybot/strategy.py:fair_value_up` |
| `evaluate_close_snipe_port` | `bot/polybot/strategy.py:evaluate_close_snipe` |
| `snipe_tau_bounds_port` | `bot/polybot/strategy.py:snipe_tau_bounds` |
| `walk_asks_port` | `bot/polybot/fill_engine.py:walk_asks` |
| `fee_per_share`, `normal_cdf` | `fill_engine.fee_per_share`, `oracle.normal_cdf` |
| window gate (one entry/window, first qualifying second, first qualifying side) | `engine.py:_maybe_snipe` |

`python3 scripts/fresh5m/proptest.py 20000`:

```
fair_value_up_port        vs strategy.fair_value_up        : 0 mismatches
snipe_tau_bounds_port     vs strategy.snipe_tau_bounds     : 0 mismatches
walk_asks_port            vs fill_engine.walk_asks         : 0 mismatches
evaluate_close_snipe_port vs strategy.evaluate_close_snipe : 0 mismatches
   (1,089 of 20,000 inputs produced a signal, so the positive path is exercised, not just the guards)
TOTAL MISMATCHES: 0
```

The randomised inputs deliberately include the nasty cases: NaN/inf/zero/negative prices and sigmas,
`tau <= 0`, `None` oracle inputs, empty and `None` books, zero-size levels, **unsorted ask ladders**,
`max_above_best=None`, `cap_usd=0`, and `fair_cap` at 0.98/0.99/1.0.

### 1.2 The four required additions

**(1) Book staleness filter.** Every book observation carries an age = decision instant − timestamp of
the last update the venue actually published. Two conventions are supported and both are reported,
because they are not the same experiment:

* **PRE-decision** (`Params.max_book_age_s`, the honest one): a book older than the limit is handed to
  the evaluator as `None` — exactly what the live bot sees when it cannot fetch a book. The side is
  untradeable this tick; the other side and later ticks still run, and a signal that reaches the fill
  stage against a stale book is recorded as `outcome="stale_book"` rather than silently vanishing.
* **POST-hoc** (`summarise(..., max_book_age_s=)`): drop the fill from the sample after the fact. This
  is the convention `docs/07` §3.4 used, so it is what the control test has to match.

**(2) Strict Chainlink causality.** `Params.causal=True` (default) selects the newest report with
`server_timestamp_us <= t`. `causal=False` reproduces the trap (`timestamp_us <= t`).
`ChainlinkFeed.causality_violations()` is the explicit assertion: it counts, over the actual decision
grid, how many decisions the non-causal view would answer with a report that had not been published
yet. **I ran this and I state the result: over the 61,835 decisions of the 43-day control,
61,128 (98.86%) of them would use an unpublished report under the non-causal rule, with a mean of
1.725 s of invented foresight.** Section 4 prices it. `tau` is also measured from the *observation*
time of the print we hold to the close, so the ~1.1 s we cannot see is charged as risk, not ignored.

**(3) Day-level statistics.** `summarise()` returns `t_trade` (per-fill) and `t_day`
(mean of daily mean ¢/share ÷ SE across days, n_days−1 df) side by side. The day-clustered number is
the pre-committed one and it is consistently **~half** the per-trade number — fills inside a day share
one BTC path and one book regime, so treating 1,503 fills as 1,503 independent draws roughly doubles
the apparent significance.

**(4) Stress knobs.** `fee_rate`, `depth_fraction`, and `latency_ms` — where `latency_ms` feeds
`tau_lo = max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs)` through the ported
`snipe_tau_bounds`, so raising latency correctly narrows the band from the late end instead of
silently letting orders land after the close. Also exposed: `sigma_1s_floor`, `per_event_cap_usd`,
`edge_min`, `vol_window_secs`, `fair_cap`, `max_walk_above_best`, `tau_grid`, `tick_hz`.

### 1.3 Conventions made switchable (so legacy frames can be reproduced, then departed from)

`sigma_mode` (`grid_ffill` = A3's forward-filled 1 s grid | `bot` = `oracle.py`'s "only the seconds we
actually hold"), `strike_mode` (`backfill` = the A1 rule | `exact_else_ffill` = A3 legacy),
`winner_source` (`result_id` | `chainlink`), `ladder` (walk all levels | top-of-book only).
Section 5 shows all four are worth ≤0.26 ¢/share, i.e. the control result is not an artifact of any of
them.

---

## 2. Control test — the verifier's numbers reproduce exactly

Repo vault, `daily/5m/bookcurves` + `daily/crypto_prices` + `windows_all.parquet`,
**2026-04-02 → 05-12 and 2026-07-06 → 07-07 = 43 days, 12,367 windows.**
A3 conventions (τ ∈ {6,5,4,3,2}, `edge_min` 0.05, top-of-book fill, $25 clip).

| variant | signals | fills | trades/day | ¢/share | win | t (trade) | t (day) | published target |
|---|---|---|---|---|---|---|---|---|
| A3 baseline, no staleness filter | 2,661 | 1,503 | 34.95 | **+9.4353** | 74.05% | 8.790 | 4.824 | 1,503 fills, +9.44¢, t 8.79, t_day 4.82 ✓ |
| **+ post-hoc drop book age > 30 s** | 2,661 | **1,432** | 33.30 | **+8.3756** | 73.04% | **7.563** | 4.168 | **+8.38¢, t=7.56, 71/1,503, 14% of P&L ✓** |
| **shipped params** (`edge_min` 0.03, τ∈[2,5]) | 2,616 | **1,405** | 32.67 | **+10.8918** | 78.36% | **10.335** | 5.755 | **+10.89¢, t=10.33 ✓** |
| shipped params + post-hoc drop > 30 s | 2,616 | 1,329 | 30.91 | +9.7708 | 77.35% | 8.938 | 5.211 | — |
| shipped as the bot **actually** runs it (τ∈[2.5,5] → {5,4,3}) | 2,510 | 1,369 | 31.84 | +10.8445 | 78.45% | 10.151 | 5.764 | — |

**Stale-fill count: 71 of 1,503, carrying 14.04% of P&L. The published figure is 71 of 1,503 and 14%.
Exact match, including the fill count, the P&L fraction, and both t-statistics to 3 significant
figures.** No tolerance was needed.

### 2.1 The threshold the verifier used was 30 s, not 5 s — and it matters

`docs/07` never states the staleness threshold behind "+8.38¢". Scanning it out: **only 30 s produces
71 fills and 14% of P&L.** At 2 s it is 120 fills / 20.0%, at 5 s 113 / 18.9%, at 10 s 103 / 17.1%,
at 60 s 35 / 7.3%. The recommendation in `docs/07` §6 is a **≤5 s** filter, which is a materially
*stricter* test than the one the +8.38¢ number was computed under. Quote the right pair.

### 2.2 The +10.89¢ shipped figure is BEFORE the staleness correction

The doc's sentence "survives the same correction: +8.38¢ … at the shipped parameters it gives +10.89¢"
reads as though both are corrected. They are not: **+10.89¢ is the uncorrected shipped run.** Corrected
at 30 s it is **+9.77¢ (t=8.94)**; corrected at 5 s it is **+9.33¢ (t=8.34)**. Anyone quoting "+10.89¢
after the outage fix" is overstating by ~1.5¢.

---

## 3. Book-staleness sensitivity (the deliverable)

43 days, shipped parameters (`edge_min` 0.03, τ∈[2,5]); PRE = rejected at decision time, POST = the
verifier's after-the-fact removal. `pnl/day` at the $25 clip.

| max book age | PRE ¢/share | PRE t_trade | PRE t_day | PRE fills/day | POST ¢/share | POST t_trade | POST t_day | POST fills dropped | POST % of P&L |
|---|---|---|---|---|---|---|---|---|---|
| **2 s** | +9.32 | 8.32 | 4.89 | 29.77 | +9.24 | 8.25 | 4.90 | 127 | 19.5% |
| **5 s** | +9.37 | 8.38 | 4.91 | 29.84 | +9.33 | 8.34 | 4.91 | 123 | 19.0% |
| **10 s** | +9.53 | 8.57 | 4.91 | 30.12 | +9.53 | 8.57 | 4.91 | 110 | 16.8% |
| **30 s** | +9.77 | 8.94 | 5.21 | 30.91 | +9.77 | 8.94 | 5.21 | 76 | 13.8% |
| **60 s** | +10.26 | 9.56 | 5.47 | 31.84 | +10.26 | 9.56 | 5.47 | 36 | 7.5% |
| **unlimited** | +10.89 | 10.33 | 5.76 | 32.67 | +10.89 | 10.33 | 5.76 | — | — |

Same sweep on the A3 convention (`edge_min` 0.05, τ∈{6..2}): 2 s +7.95¢ / t_day 3.66 · 5 s +8.04 / 3.70
· 10 s +8.12 / 3.65 · 30 s +8.38 / 4.17 · 60 s +8.87 / 4.57 · unlimited +9.44 / 4.82.

**Read this as: on the 43-day sample, the staleness filter costs about 1.5 ¢/share and 3 fills/day,
and the edge survives every threshold at t_day ≈ 4.9–5.8.** PRE and POST agree to within 0.1 ¢, which
is itself a check — if a stale book at signal time were routinely rescued by a fresh one 1.5 s later
the two would diverge, and they do not (`stale_book`-blocked signals: 0–9 across the whole 43 days).

### 3.1 What the stale fills actually are

| | fresh fills (age ≤ 30 s) | stale fills (age > 30 s) |
|---|---|---|
| n | 1,432 | 71 |
| win rate | 73.0% | **94.4%** |
| ¢/share | +8.38 | **+30.81** |
| age > 300 s subset | — | 7 fills, **100% win, +48.68¢/share** |

The age distribution of fills is violently bimodal: **median 6 ms, p90 60 ms, p95 27.7 s, max 1,047 s.**
There is no continuum — a book is either live-streaming or frozen.

Two diagnostics I ran that `docs/07` did not:

1. **It is not a `bookcurves` sampling artifact.** Re-measuring the same 45 stale decisions against the
   independent `daily/5m/quotes` tape: **0 of 45** are contradicted (median age 53 s on bookcurves vs
   69 s on quotes; on 2026-04-20 the quotes tape has no in-window rows at all). The venue really was
   silent. (`data/c2/table6_tape_crosscheck.csv`)
2. **The repo sample's staleness is mostly market-specific, not one big outage.** Counting book updates
   *anywhere* in the day's 5m capture during each stale window's silence: only **11 of 71** coincide
   with a total capture blackout; the other 60 have 5–343 updates in other markets. This differs from
   the fresh 5-day sample, where 14 of 16 stale books shared one instant (2026-07-21 ~04:07). Two
   sub-patterns are visible: a systematic per-window truncation on 2026-04-20 (14 consecutive windows
   each frozen at exactly close−59 s) and genuine thin-market quiescence elsewhere.

   **This does not make the fills safe.** Whatever the cause, the recorded ask is a price the market
   left minutes ago; 94% win and +31¢/share is the signature of filling against a quote that was not
   there. Reject them. But it does mean a `≤5 s` filter on 5m is partly rejecting *real* thin-book
   moments, so it is a conservative bound, not a pure contamination fix.

---

## 4. The causality trap, priced

43 days, A3 convention, identical in every respect except the oracle read rule.

| | signals | fills | ¢/share | win | t_trade | t_day | $/day @ $25 |
|---|---|---|---|---|---|---|---|
| **CAUSAL** `server_timestamp_us <= t` (truth, and what the bot does) | 2,661 | 1,503 | **+9.44** | 74.1% | 8.79 | 4.82 | $137 |
| **NON-CAUSAL** `timestamp_us <= t` (the trap) | 2,784 | 1,526 | **+11.33** | 76.5% | 10.92 | 6.08 | $161 |

**The trap is worth +1.89 ¢/share — 20% of the entire measured edge — and it inflates t_day from 4.82
to 6.08.** It fires on 98.86% of decisions (mean 1.725 s of foresight). It is not exotic: reading
`timestamp_us <= t` is the obvious thing to write, and it is wrong by more than the fee. Any 5m number
produced without this filter should be treated as inflated by ~2 ¢/share until proven otherwise.

Independent confirmation: A3 §2.3 measured the publication lag at "about 1.9 ¢/share" by a different
route. Two implementations, same answer.

---

## 5. Conventions and stress knobs (43 days, A3 baseline)

| variant | fills | trades/day | ¢/share | t_trade | t_day | $/day @ $25 |
|---|---|---|---|---|---|---|
| baseline | 1,503 | 34.95 | +9.44 | 8.79 | 4.82 | $137 |
| σ via `oracle.py` convention (held prints, no ffill) | 1,499 | 34.86 | +9.42 | 8.76 | 4.80 | $137 |
| strike = A1 backfill (first print at-or-after `wts`) | 1,499 | 34.86 | +9.38 | 8.71 | 4.60 | $137 |
| winner recomputed from Chainlink instead of `result_id` | 1,486 | 34.56 | +9.17 | 8.48 | 4.45 | $133 |
| `sigma_1s_floor` 8e-6 → 3e-5 | 1,336 | 31.07 | +9.75 | 8.35 | 4.19 | $129 |
| fee 0.07 → 0.10 | 1,491 | 34.67 | +9.10 | 8.45 | 4.79 | $133 |
| `depth_fraction` 50% | 1,503 | 34.95 | +9.44 | 8.79 | 4.82 | $126 |
| latency 3.0 s, τ grid unchanged (**not executable**) | 1,087 | 25.28 | +12.54 | 10.43 | 7.00 | $129 |
| latency 3.0 s **with the bot's `tau_lo = lat+0.5` rule** | 768 | 17.86 | +14.43 | 10.69 | 6.15 | $103 |
| **ALL: fee .10 + 50% depth + latency 3.0 s (band) + age ≤ 5 s** | 646 | 15.02 | **+11.51** | 7.66 | **4.88** | **$69** |

Notes that matter:

* **Every convention knob is worth ≤0.26 ¢/share.** The control result does not depend on any of them.
* `depth_fraction` correctly leaves ¢/share unchanged in top-of-book mode (it can only shrink size,
  not move the average price) and correctly *does* move it on a real ladder — on the fresh 5-level
  tape at a $250 clip it takes shares from 19,518 to 14,681 and ¢/share from 7.21 to 7.11.
* **Higher latency *raises* ¢/share on the 43-day repo sample (+9.44 → +14.43) while halving fill
  count.** On the fresh 5-day sample the same knob was catastrophic (−2.69¢). That is a real
  regime difference between the two tapes, not a harness difference — the same code produces both.
* **The full stress stack leaves the 43-day sample at +11.5 ¢/share, t_day 4.88, $69/day at a $25
  clip.** The fresh 5-day sample under the same stack was +0.77¢, t 0.15. The two samples disagree,
  which is precisely why the 20-day fetch exists.

---

## 6. Second control: the fresh-tape adapter

The harness must also read the *fresh* capture format (5-level ladders for both tokens, per-snapshot
`book_age`) before the fetch agent's data lands. Run against the existing Jul 21–25 capture
(`data/a3/books5/`), which `docs/07` §3.2–3.3 characterised:

| variant | fills | trades/day | ¢/share | win | t_trade | t_day | published |
|---|---|---|---|---|---|---|---|
| A3 fresh headline (`edge_min` 0.05, τ{6..2}) | 111 | 22.2 | **+7.51** | 74.8% | 1.91 | 2.13 | 108 fills, 21.6/d, +7.42¢, 74.1%, t 1.84 |
| + drop book age > 30 s | 104 | 20.8 | **+4.89** | 73.1% | 1.21 | 1.24 | 20.2/d, +4.72¢, 72.3%, t 1.13 |
| + drop book age > 5 s | 103 | 20.6 | +4.48 | 72.8% | 1.10 | 1.03 | +4.30¢, t 1.02 |
| shipped params (`edge_min` 0.03, τ∈[2,5]) | 117 | 23.4 | **+3.83** | 73.5% | 1.03 | 1.43 | 22.8/d, +3.66¢, t 0.96 |
| shipped + drop > 30 s | 110 | 22.0 | **+1.12** | 71.8% | 0.29 | 0.24 | +0.87¢, t 0.22 |

**And the key diagnostic reproduces on the nose: 7 stale fills (age > 30 s), 100% win,
+46.40 ¢/share, 52.3% of total P&L** — against the published "7 of 108, all 7 won at ~+46¢/share,
53.6% of total P&L".

Residual gap: 111 fills vs the published 108 (+2.8%), everything else within 0.2 ¢/share. The cause is
the snapshot-timestamp reconstruction (`close_s + rel − book_age`) versus A3's direct `rel`-index
lookup; it moves three marginal fills and no conclusion. If the fetch agent writes a real update tape
(schema in §7) this reconstruction disappears entirely.

Causality audit on that tape: 7,054 of 7,200 decisions (97.97%) would use an unpublished report under
the non-causal rule; mean foresight 1.803 s.

---

## 7. Ready for the fresh fetch — what `data/fresh5m/` must contain

```
python3 scripts/fresh5m/replay.py run --source fresh \
    --books-dir data/fresh5m/books --chainlink-dir data/fresh5m/crypto_prices \
    --days 2026-07-08,... --edge-min 0.03 --max-book-age 5 --cap-usd 250 --out data/c2/fresh.parquet
```

**Preferred book layout (a): one parquet per day, one row per book update.**
`close_s`, `oid` (0 = Up, 1 = Down), `timestamp_us`, `ask_price_0..N`, `ask_size_0..N`
(+ `bid_price_0`, `bid_size_0`). Real update timestamps are what make the staleness filter measure
true venue silence — please capture them rather than a fixed sampling grid.

**Accepted fallback (b):** `snaps.parquet` in the A3 layout (`close_s`, `oid`, `rel`, `book_age`,
`ask_price_0..4`, `ask_size_0..4`, `d`); the tape timestamp is reconstructed as
`close_s + rel − book_age`. Plus `_markets.parquet` with `close_s`, `result_id`, `slug`.

**Chainlink:** `timestamp_us`, `server_timestamp_us`, `price` — `server_timestamp_us` is
**not optional**; without it §4 shows the result is inflated by ~1.9 ¢/share. Fetch one extra day
before the first replay day so the 120 s vol window is warm at midnight.

**Pre-committed reporting for the fresh run**, on the strength of what is above:
shipped parameters (`edge_min` 0.03, τ from `tau_lo = max(2.5, latency/1000+0.5)` to 5.0),
`--max-book-age 5`, strict causality, and **`t_day` as the decision statistic**, with the full stress
stack as the secondary. Report `+X¢ (t_day)` — never the per-trade t alone; it runs ~2× hot.

---

## 8. Caveats

1. **The 43-day control uses a synthetic Down book** (`ask_down = 1 − bid_up`) because the repo vault
   stores one token per window. A3 §3.2 verified that identity on 5,017/5,017 fresh comparisons, and
   the fresh-tape control in §6 uses real two-sided ladders, so this is checked — but it is a
   reconstruction, not a measurement.
2. **The control is a top-of-book fill.** The repo vault has one price level; the fresh tape has five.
   Per-share EV is unaffected (a one-level walk fills at the best ask by construction), $ P&L is a
   lower bound.
3. **Decision instants are integer seconds.** The live bot ticks at ~1 Hz on a drifting sub-second
   phase. `Params.tick_hz` can densify the grid; the control uses integer seconds because that is what
   the published numbers used.
4. **`sigma_1s_floor` recalibration is still open, and 3e-5 is the wrong shape of number.** Measured
   over 1.39 M seconds sampled across the repo's `crypto_prices`, Chainlink `sigma_1s` (bot
   convention) runs p05 1.44e-05, p25 2.44e-05, **p50 3.52e-05**, p90 8.02e-05. So 3.0e-5 is
   approximately the *median*, not a floor — setting the floor there would bind on **38.7%** of
   decisions, versus 0.6% for the shipped 8e-6. A floor is meant to catch the quiet tail; a low
   percentile (~1.4e-05) is the defensible choice. The harness exposes `--sigma-floor`; at 3e-5 on the
   43-day sample it costs 11% of trades for +0.3 ¢/share. **Do not adopt 3e-5 without deciding whether
   you want a floor or a recalibrated typical value — they are not the same object.**
5. Two published figures in `docs/07` §3.4 need the corrections in §2.1 and §2.2 before reuse.

---

## 9. Files

| path | what |
|---|---|
| `scripts/fresh5m/replay.py` | the harness |
| `scripts/fresh5m/proptest.py` | port verification vs the live bot code |
| `scripts/fresh5m/control_report.py` | the sweep driver that produced every table here |
| `data/c2/table1_control.csv` | §2 control test |
| `data/c2/table2_staleness_a3.csv`, `table2b_staleness_shipped.csv` | §3 staleness sweeps |
| `data/c2/table3_causality.csv` | §4 |
| `data/c2/table4_conventions_stress.csv` | §5 |
| `data/c2/table5_stale_fills.csv` | the 71 stale fills, itemised |
| `data/c2/table6_tape_crosscheck.csv` | bookcurves vs quotes staleness cross-check |
| `data/c2/table7_fresh_adapter.csv` | §6 fresh-tape control |
| `data/c2/trades_*.parquet` | per-variant trade tapes (19 variants × 43 days) |
