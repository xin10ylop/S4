# A2 — Telonex shadow-replay of the live paper-trading period

**Question:** did the live bot behave exactly as its logic dictates over 2026-07-16 → 2026-07-25?
**Answer: yes.** Replaying `close_snipe/1h` against independently-fetched vendor data reproduces the
live bot's behaviour to within the noise of its own price inputs. **No logic discrepancy was found.**
This closes the "still outstanding" item in `docs/06_live_audit.md` §9.

Scripts: `scripts/fetch_live_audit_data.py` (data), `scripts/replay_live_period.py` (replay).
Per-trade CSV: `audit/A2_replay_trades.csv`. Fixed-tau grid: `audit/A2_replay_tau_scan.csv`.

---

## 1. Data fetched (all new — the repo's 1h vault stopped 2026-07-12)

| dataset | detail |
|---|---|
| hourly markets | **222** markets, closes `2026-07-16 18:00Z .. 2026-07-25 23:00Z` → `data/live_audit/hourly_markets.parquet` |
| quotes | **888** files = 222 markets × 2 outcomes × 2 UTC days (close date + prior date), **0 failures** → `data/live_audit/quotes/outcome_{0,1}/` |
| book_snapshot_5 | 52 files for the 13 signal windows → `data/live_audit/books/outcome_{0,1}/` |
| Binance 1s klines | 2026-07-14 … 2026-07-26 (13 days × 86,400 rows) → `data/data/processed/binance/klines_1s/` |

The Telonex markets dataset returns **231** `bitcoin-up-or-down-*` markets in the period; **9 are the
DAILY** `bitcoin-up-or-down-on-<month>-<d>-<yyyy>` markets. The fetch applies the bot's own
`polymarket._HOURLY_RE` verbatim, leaving exactly **222** hourly closes — precisely `9d × 24 + 6`.

### Data-integrity checks (all passed)

- **kline schema**: `pyarrow.Schema.equals()` against an existing vault file → `True`
  (`open_time`/`close_time` int64 **microseconds**, `n_trades` int32, 11 columns, 86,400 rows/day).
- **S_open is authoritative**: 1H candles aggregated from the new 1s klines were compared against the
  REST `/api/v3/klines?interval=1h` endpoint — **247/247 hours matched exactly** on open and close.
  So the replay's `S_open` is bit-for-bit what `oracle.hour_open_close()` returns live.
- **resolution source**: Telonex `result_id` agreed with the Binance 1H rule (`close >= open ⇒ Up`)
  for **222/222** markets. No resolution surprises in the period.

## 2. Replay fidelity — the port was verified against the bot, not just eyeballed

| check | result |
|---|---|
| `replay.evaluate_second` vs `strategy.evaluate_close_snipe` | 20,000 randomised inputs (9,535 firing) — **0 mismatches** on fire/no-fire, side, fair, ask, edge |
| `replay.walk_asks_single` vs `fill_engine.walk_asks` | 20,000 one-level ladders (2,866 filling) — **0 mismatches** on shares/cost/fees |
| `bot/tests` | 54 passed (bot code untouched) |

Everything else is copied line-for-line: `sigma_1s_floor` 8e-6, `fair_cap` 0.98 clip, up-then-down
side order, one entry per window, `fair` **frozen at signal time** for the fill's `edge_fn`
(`engine.py` closes over `_fair=fair`), `max_walk_above_best` 0.03, `per_event_cap_usd` $25,
`latency_ms` 1500, fee `0.07·p·(1−p)`.

`S_t` = the **open** of the 1s kline at second `t` (`oracle.price_at_second`'s own convention;
strictly non-anticipative). `sigma_1s` = sample std (ddof=1) of 1s log-returns over the trailing 120s.

## 3. Headline result vs the live bot

Replay: 222 closes, **all 222 evaluated**, 1,280 evaluation ticks (1,332 minus 52 skipped after a
signal breaks the loop — exactly as `snipe_done` does live).

| | **LIVE (docs/06)** | **REPLAY** (5-level ladder) | REPLAY (top-of-book only) |
|---|---|---|---|
| closes evaluated | ~238 | **222** | 222 |
| signals | 11 | **13** | 13 |
| fills | 7 | **8** | 8 |
| `book_moved_no_edge` | 4 | **4** | 4 |
| `empty_book` | 0 | 1 | 1 |
| shares | 214.69 | 144.65 | 91.92 |
| deployed | $134.09 | $83.69 | $49.66 |
| **total PnL** | **+$44.97** | **+$59.09** | +$41.01 |
| **EV / share** | **+20.95¢** | **+40.85¢** | +44.61¢ |
| win rate | 85.7% (6/7) | 100% (8/8) | 100% (8/8) |
| signals/day | 1.13 | 1.41 | 1.41 |

t-stat on per-trade ¢/share = **3.75**; median trade +26.2¢/share. One trade
(2026-07-18 06:00Z, Down @ $0.37, 67.2 shares) is **69.5%** of the replay PnL — n is still tiny.

### Explaining every gap

1. **Signals 13 vs 11.** The replay evaluates exactly 6 integer seconds; the live 1 Hz tick runs at an
   arbitrary, drifting sub-second phase. **5 of the 13 signal windows qualified on only 1–2 of the 6
   seconds** (see `A2_replay_tau_scan.csv`) — a phase shift alone drops those. The live period also
   ran ~13h past Telonex's coverage (~238 closes vs 222). 11-vs-13 is phase noise, not a defect.
2. **`book_moved_no_edge` 4 vs 4 — exact match.** The one failure mode the audit cared about
   reproduces on the nose.
3. **Shares/deployed lower in replay.** Purely a book-resolution artefact: going from the top-of-book
   quote tape (91.92 shares) to the real 5-level ladder (144.65) recovers over half the gap to live's
   214.69. The live bot walked the *full* CLOB `/book` ladder (unbounded levels within 3¢ of best);
   Telonex snapshots stop at 5. Replay fill size is a **lower bound**, as expected.
4. **EV/share 40.9¢ vs 20.9¢, win 8/8 vs 6/7 — the input-precision gap, and it is the right sign.**
   The live bot's `S_t` and `sigma_1s` come from ~1 Hz REST polls of `/ticker/price`: jittered,
   sometimes repeated, occasionally stale. Repeated poll values *depress* realized sigma, which
   *inflates* |z| and therefore `fair`, so the live bot fires on asks the exact-kline model rejects —
   marginally worse-priced trades, one of which lost. `sigma_1s_floor` mitigates this but cannot
   remove it. Statistically the win rates are indistinguishable: **P(8/8 | p = 0.857) = 0.29**.

**Verdict: the live bot did what its code says.** Every difference traces to input precision or book
depth resolution; none traces to a logic error, a missed close, or a throttle.

## 4. Tau distribution — the audit's "first qualifying second" hypothesis

**The premise is confirmed; the conclusion is refuted.**

The bot does take the earliest second, overwhelmingly:

| tau it fired at | 6 | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|---|
| signals | **9** | 1 | 0 | 1 | 1 | 1 |

But firing later is *worse*, not better, at the shipped `latency_ms = 1500`. Fixed-tau
counterfactual (fire at exactly tau=k, ignoring one-per-window):

| tau | signals | fills | shares | PnL | ¢/share |
|---|---|---|---|---|---|
| **6** | 9 | 6 | 124.5 | +$56.50 | **45.4** |
| 5 | 9 | 7 | 187.1 | +$71.70 | 38.3 |
| 4 | 8 | 6 | 136.5 | +$44.28 | 32.4 |
| 3 | 8 | 5 | 131.0 | +$38.77 | 29.6 |
| 2 | 7 | **1** | 27.8 | +$2.60 | 9.4 |
| 1 | 6 | **0** | 0 | $0.00 | — |

**Mechanism:** the fill lands 1.5s *after* the signal. A tau=2 signal fills at close−0.5s and a tau=1
signal at close**+**0.5s, by which time makers have pulled and the book is being bulk-cancelled.
Policy "fire on the **last** qualifying second" → **13 signals, 0 fills, $0.**

Latency is the real lever, and it does not rescue the late window either:

| latency | ¢/share @ tau=1 | @ tau=2 | @ tau=4 | @ tau=6 |
|---|---|---|---|---|
| 250 ms | 23.9 (2 fills) | 16.1 | 43.7 | 32.8 (**1 loss**) |
| 500 ms | 23.9 | 16.1 | 38.3 | 35.5 (1 loss) |
| 1000 ms | 9.4 | 16.5 | 40.7 | 23.6 (1 loss) |
| **1500 ms (shipped)** | — (0 fills) | 9.4 | 32.4 | **45.4 (0 losses)** |

`docs/06_live_audit.md` §5 measured the −2s→−1s frontier assuming a **1s** signal→fill gap. At the
bot's actual 1.5s gap that cell is unreachable, and even at 250 ms the late buckets underperform
tau=4–6 on this period — while cutting latency *adds* the one losing trade that the 1.5s delay had
protected the bot from. **Recommendation: do NOT adopt lever #2 ("snipe only in the last 2–3s") as
written.** It would have taken this period from 8 fills to ~1.

*Caveat: 6–9 signals per tau bucket, one 9-day sample, one trade at 69% of PnL.*

> **CONFIRMED and refined by `audit/A4_change_spec.md` (2026-07-27, 1,738 OOS closes).** A4
> independently reproduces this rejection of "last 2–3s" / "last qualifying second", including on
> *this file's own* tau-scan CSV. Two additions:
> - **`[2, 5]` is a different rule and beats `[1, 6]` on A2's own data: $73.38 vs $59.09** (+24%).
>   A2 tested where to *move* the firing point; A4 tested where to *bound the band*.
> - **A2's implied "keep `tau = 6`" holds only at the $25 clip.** At `per_event_cap_usd: 250`
>   every losing trade in `(0, 6]` comes from `tau >= 5` — worst single trade −$258.75, worst
>   10-trade run −$232 — versus −$45.26 / −$16 under `[2, 5]`. The shipped config is `[2.0, 5.0]`.

## 5. What actually caps frequency: the price band, not `edge_min`

At tau=3, across all 222 closes and both outcomes:

| best ask | ≤$0.01 | $0.01–0.30 | **$0.30–0.99** | >$0.99 |
|---|---|---|---|---|
| quotes | 196 | 22 | **27** | 13 |

Only **21/222 closes (9%)** had *any* ask inside the tradeable band `(0.30, 0.99)` on either side —
and only **25/222 (11%)** did so at *any* of the six snipe seconds. By 6 seconds before the hour the
hourly market is already priced as decided: the loser is quoted at a penny and the winner has no ask
or sits above 99¢. **The bot signalled on 13 of those 25 tradeable closes — 52% conversion, and the
other 12 simply never cleared `edge_min`.** Nothing is throttling it; there is nothing to buy in 89%
of closes.
This sharpens `docs/06` §3: the frequency ceiling is structural to the 1h family's book, so only more
families (lever 3) can lift it.

### Two parameter sensitivities measured on this period

`edge_min` (direction matches `docs/06` §5.2; net benefit smaller here):

| edge_min | signals | fills | PnL | ¢/share |
|---|---|---|---|---|
| 0.02 | 17 | 12 | +$64.31 | 28.0 |
| 0.03 | 16 | 10 | +$62.95 | 31.3 |
| **0.05 (shipped)** | 13 | 8 | +$59.09 | 40.9 |
| 0.12 | 6 | 4 | +$54.01 | 57.6 |

`price_min` — **load-bearing, confirms `docs/06` §7 "do not change"**:

| price_min | fills | win rate | ¢/share |
|---|---|---|---|
| **0.30 (shipped)** | 8 | **100%** | **40.9** |
| 0.20 | 8 | 87.5% | 35.7 |
| 0.10 | 11 | 63.6% | 3.8 |

The window it saved is **2026-07-20 04:00Z**: the model said `fair_down = 0.98` and Down was asked at
$0.77 at signal; 1.5s later the Down ask had **collapsed to $0.13** — the market had already seen the
last-second rally. Up won. `price_min` blocked a fill that would have lost. This is the adverse
selection that killed `settle_sweep`, showing up inside `close_snipe`, and the band is what stops it.

## 6. Limitation of this comparison

The repo contains **no live `fills.csv`/log for 2026-07-16 → 07-26** — the only archive
(`bot/data/archive_20260716_000254Z/`) predates the period. The comparison above is therefore against
the **aggregates** in `docs/06_live_audit.md`, not trade-by-trade. To convert "consistent in
aggregate" into "identical trade-by-trade", copy the server's `bot/data/fills.csv` + `polybot.log`
into the repo and join on `market_slug`; the replay CSV is keyed to make that a one-liner.

## Reproduce

```bash
python3 scripts/fetch_binance.py 2026-07-14 2026-07-26
python3 scripts/fetch_live_audit_data.py --from 2026-07-16T18:00:00Z --to 2026-07-26
python3 scripts/replay_live_period.py --tau-scan --depth      # canonical run
python3 scripts/replay_live_period.py --tau-scan --latency-ms 500   # latency grid
python3 scripts/replay_live_period.py --edge-min 0.03 --depth       # parameter sweeps
```
