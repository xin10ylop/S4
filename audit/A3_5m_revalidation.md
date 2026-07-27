# A3 — Re-validation of the 5m `close_snipe` opportunity

**Question.** `docs/03_final_report.md` #3 claims 5m close-snipe on a *visible* Chainlink signal is
worth **+14.0¢/share at ~52 trades/day OOS**. Before spending engineering effort on a live Chainlink
feed, does that edge still exist — under the **current** bot parameters, under an honest treatment of
the Chainlink publication lag, and on data that did not exist when the strategy was designed?

**Answer: yes, but smaller and slower than advertised.** On five days of freshly-pulled vendor data
(2026-07-21 → 07-25, *all* 1,440 windows, none of it available when the strategy was written) the
strategy makes **+7.4¢/share at 21.6 trades/day, 74.1 % win rate**. That is roughly **half** the
per-share edge and **40 %** of the frequency of the original claim. It is still positive on 4 of
5 days and survives a day-block bootstrap
(P(EV>0) = 0.994), but with 5 days of data it is **not** statistically decisive on its own
(t_iid = 1.84; day-level t = 2.12, p = 0.10).

The single most valuable secondary finding: **Telonex sells the Chainlink series.** Channel
`crypto_prices`, `asset_id="btcusd"`, coverage through 2026-07-26, **bit-identical** to the repo's
`daily/crypto_prices/` vault. Full out-of-sample validation was therefore possible, and the "we have
no Chainlink feed" blocker is a *live-feed* problem only, not a data problem.

Scripts: `scripts/a3/{replay5m,run_repo,analyse,decompose,fetch_5m_books,run_fresh}.py`.
Data written: `data/a3/` (eval frames, fresh book snapshots, fresh `crypto_prices`, `publish_lag.csv`).

---

## 0. Headline comparison

| | docs/03 claim | **Part 1** repo data, orig OOS window | **Part 1** repo data, all 43 d | **Part 2 — FRESH** 2026-07-21…25 |
|---|---|---|---|---|
| period | Apr 16–May 12 + Jul 6/7 | Apr 16–May 12 + Jul 6/7 | Apr 2–May 12 + Jul 6/7 | **Jul 21–25** |
| days / windows | 29 | 29 / 8,337 | 43 / 12,367 | **5 / 1,440 (100 %)** |
| trades/day | **52** | 32.7 | 35.0 | **21.6** |
| EV/share | **+14.0¢** | +15.5¢ | +9.4¢ | **+7.4¢** |
| win rate | 80 % | 78.6 % | 74.1 % | **74.1 %** |
| t (per-trade) | — | 11.85 | 8.79 | **1.84** |
| t (per-day mean) | — | 6.09 | 4.82 | **2.12 (p = 0.10)** |

All Part-1/Part-2 numbers use the **current** bot config: `fair_cap 0.98`, `sigma_1s_floor 8e-6`,
`edge_min 0.05`, `price∈(0.30,0.99)`, snipe at τ ∈ {6,5,4,3,2} s (first qualifying second, one entry
per window), **1.5 s latency with a re-fetched fill book that must independently clear `edge_min`**,
3 ¢ walk bound, `per_event_cap_usd 25`, fee `0.07·p·(1−p)`, and a 4 s oracle-staleness guard.

---

## 1. The publication lag — measured, not assumed

`crypto_prices` carries three clocks. `timestamp_us` is the **observation second** the Chainlink
report refers to (and what Polymarket settles on); `server_timestamp_us` is when the report was
**published**; `local_timestamp_us` is when the capture machine received it.

**A live bot at wall-clock `t` can only use the newest print with `server_timestamp_us ≤ t`.**
Everything below obeys that rule. The uncertainty horizon `τ_eff` runs from that print's *observation*
second to the close second — i.e. the staleness is added to the horizon, not ignored.

Pooled over every print in every day used (3.65 M repo prints, 0.75 M fresh prints):

| lag (s) | p5 | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| publish (`server − timestamp`), 43 repo days Apr 2 – Jul 7 | 0.807 | **1.137** | 1.555 | 2.051 | 10.3 |
| publish, 9 fresh days Jul 18 – 26 | 0.777 | **1.170** | 1.648 | 2.073 | 78.9 |
| to our capture machine (`local − timestamp`), repo days | 0.948 | 1.323 | 1.803 | 2.469 | 2370 |
| to our capture machine, fresh days | 1.000 | 1.412 | 1.896 | 2.325 | 79.2 |

Per-day detail in `data/a3/publish_lag.csv`. The lag is **drifting upward**: daily p50 was 1.01–1.03 s
on Jul 18–20 and 1.32–1.36 s on Jul 23–26 (vs 1.14 s median across April–May). Stream coverage is
83.8–99.1 % of seconds (mean 97.9 %) — those are gaps in the stream itself, not gaps in our capture,
which is why the code takes the newest *available* print rather than assuming one exists every second.

**Consequence at the decision second.** With a ~1.2 s publish lag on a 1 s grid, the newest print a
bot holds at integer second `t` is stamped `t−2` **69 %** of the time (repo period) / **77 %** (fresh
period), `t−1` 28 % / 18 %, `t−3` 2 % / 3.5 %. **0.7 %** (repo) and **1.5 %** (fresh) of decision
seconds are staler than the 4 s guard recommended in `audit/A1_chainlink.md` and are skipped. The
lag drift between the two periods is visible directly in that shift from `t−1` toward `t−2`.

### Getting this wrong invents edge that is not there

Same fresh data, same everything, only the information set changes (edge_min 0.05):

| information set at the decision second | trades/day | EV/share | win | t |
|---|---|---|---|---|
| **A — honest: newest print PUBLISHED by t** | 21.6 | **+7.42¢** | 74.1 % | 1.84 |
| B — the print *stamped* t (ignores the ~1.2 s publish lag) | 21.2 | +8.56¢ | 76.4 % | 2.15 |
| C — the **settle** print itself (pure lookahead) | 31.8 | **+26.78¢** | **100.0 %** | **18.17** |

Ignoring the publish lag overstates the edge by ~15 %. Using the settle print manufactures a
flawless, enormous, entirely fictitious strategy. Neither shortcut was taken anywhere in this audit.

### The strike/settle convention was verified against ground truth, not guessed

`data/data/processed/windows.parquet` carries `open_chainlink`/`close_chainlink` for 11,791 5m
windows in the Chainlink coverage period. Testing candidate rules against them:

| candidate rule | exact match |
|---|---|
| strike = print stamped at `wts − 1` | 6.7 % |
| **strike = print stamped exactly at `wts`** | **98.6 %** |
| strike = print stamped at `wts + 1` | 6.8 % |
| **settle = print stamped exactly at `wts + 300`** | **98.6 %** |

The 1.4 % residual is missing seconds in the stream, not a wrong rule. The strike is ~300 s old at
decision time, so it carries no lag risk; the settle print is never consulted before the close.

---

## 2. Part 1 — repo data, current parameters

Usable days = intersection of `daily/5m/{quotes,bookcurves}` and `daily/crypto_prices`:
**2026-04-02 → 05-12 and 2026-07-06 → 07-07 (43 days, 12,367 windows).** The vendor gap
May 13 – Jul 5 means **the repo simply has no 5m book data for June**; "the latest available window"
in the vault is Jul 6–7, exactly the two days the original OOS already used. This is why Part 2 exists.

### 2.1 By period (edge_min 0.05)

| period | days | windows | signals/day | trades/day | EV/share | win | t_iid | day-mean | t_day | PnL @$25 |
|---|---|---|---|---|---|---|---|---|---|---|
| Apr 02 – Apr 15 | 14 | 4,030 | 61.9 | 39.6 | **−1.03¢** | 66.2 % | −0.58 | −0.57¢ | −0.48 | −$415 |
| Apr 16 – Apr 30 | 15 | 4,318 | 65.0 | 36.3 | **+13.06¢** | 77.8 % | 7.59 | +13.51¢ | 5.45 | +$2,332 |
| May 01 – May 12 | 12 | 3,449 | 64.8 | 32.3 | **+19.66¢** | 80.2 % | 9.62 | +14.00¢ | 3.80 | +$3,951 |
| **Jul 06 – Jul 07** | 2 | 570 | **21.0** | **8.0** | **+0.45¢** | 68.8 % | 0.04 | −0.86¢ | −0.16 | +$28 |
| orig OOS (Apr16–May12+Jul6/7) | 29 | 8,337 | 61.9 | 32.7 | +15.54¢ | 78.6 % | 11.85 | +12.72¢ | 6.09 | +$6,311 |
| all 43 days | 43 | 12,367 | 61.9 | 35.0 | +9.44¢ | 74.1 % | 8.79 | +8.39¢ | 4.82 | +$5,896 |

Two things stand out. First, **the edge is not stationary**: it is absent in the first two weeks of
April (which was the tail of the original *training* split), peaks in late April/early May, and is
much weaker by July. Second, **opportunity frequency collapses**: ~62 signals/day through May,
21/day on Jul 6–7, 40/day on the fresh July window.

### 2.2 edge_min sensitivity

| edge_min | orig-OOS trades/day | orig-OOS EV/share | all-43d trades/day | all-43d EV/share |
|---|---|---|---|---|
| 0.05 | 32.7 | +15.54¢ | 35.0 | +9.44¢ |
| 0.03 | 36.2 (+11 %) | +14.10¢ (−9 %) | 38.9 | +8.71¢ |
| 0.02 | 39.9 (+22 %) | +12.75¢ (−18 %) | 42.9 | +7.91¢ |

Same shape as the 1h finding: loosening `edge_min` buys frequency at a modest cost in per-share EV,
and total PnL is roughly flat (orig-OOS: $6,311 / $6,307 / $6,131 at the $25 cap).

### 2.3 Why 33 trades/day and not 52 — the claim *is* reproducible

Reproducing the 2026 backtest's *conventions* on the *same* OOS window recovers its numbers almost
exactly, which validates this replay engine against `docs/03`:

| variant (orig OOS window, edge_min 0.05) | trades/day | EV/share | win | t |
|---|---|---|---|---|
| **A — current bot** (cap 0.98, σ floor, publish lag, 2-book exec) | 32.7 | **+15.54¢** | 78.6 % | 11.85 |
| B — A but zero publication lag | 33.0 | +17.41¢ | 80.8 % | 13.80 |
| C — A but no `fair_cap`, no σ floor | 34.7 | +14.66¢ | 79.3 % | 11.72 |
| D — A but *one-book* execution (decide and fill on the same t+1.5 s book) | 49.1 | +13.94¢ | 76.1 % | 12.79 |
| **E — C + D = the 2026 research setup** | **51.8** | **+13.32¢** | 77.2 % | 12.82 |
| *docs/03 #3, as published* | *52* | *+14.0¢* | *80 %* | — |

So the 52 → 33 trades/day gap is **almost entirely the honest two-book execution**. The 2026 backtest
evaluated the edge *on the already-lagged book and filled there instantly*, which removes
book-movement risk by construction. Reinstating it (a signal must still clear `edge_min` on a book
re-fetched 1.5 s later) kills **~33 %** of signals. Across all 43 days: 2,661 signals →
1,503 `filled` (56.5 %), 906 `book_moved_no_edge` (34.0 %), 252 `empty_book` (9.5 %).

Note the direction of the other two effects: `fair_cap` + σ floor **raise** EV/share (they suppress
the model's worst over-confident trades), and the publication lag costs about **1.9¢/share** and 1 %
of trades. The current, tighter parameters are not the problem.

### 2.4 Timing frontier (fixed-τ counterfactual, orig OOS, edge_min 0.05)

| fire at τ = | 6 s | 5 s | 4 s | 3 s | 2 s |
|---|---|---|---|---|---|
| trades/day | 25.0 | 22.9 | 19.6 | 16.2 | 11.6 |
| EV/share | +15.1¢ | +16.0¢ | **+21.2¢** | +20.7¢ | +20.9¢ |
| win | 78.5 % | 80.5 % | 86.4 % | 87.2 % | 87.8 % |
| t | 10.1 | 10.6 | **14.7** | 13.2 | 11.9 |

**5m behaves the opposite way to 1h.** In `docs/06_live_audit.md` the 1h book bulk-cancels before the
close, so firing late means zero fills. On 5m the book is still there: at τ=2 s (fill at close−0.5 s)
the fill rate is 49 % and EV/share is *higher*. The bot's "first qualifying second" policy therefore
takes the **weakest** end of the frontier here too — but on 5m the fix is cheap, because waiting for
τ≈4 s still leaves ~20 trades/day. The fresh window agrees qualitatively (τ=3 s is the best bucket at
+15.1¢) though n is small.

---

## 3. Part 2 — FRESH Telonex data, genuinely out of sample

### 3.1 Telonex *does* sell the Chainlink series

`get_availability()` lists no `crypto_prices` channel for a market slug, which is why it looked
unavailable. It is a **single-asset** channel, not a prediction-market channel:

```
GET /v1/downloads/polymarket/crypto_prices/<YYYY-MM-DD>?asset_id=btcusd
# SDK: download(exchange="polymarket", channel="crypto_prices", asset_id="btcusd", ...)
```

Coverage runs through **2026-07-26** (i.e. Telonex's usual "through ~2 days ago"). Columns
`timestamp_us, server_timestamp_us, local_timestamp_us, exchange, asset_id, symbol, source, price`
with `source = "chainlink"`; the repo's processed vault is the same rows with the string columns
dropped and `price` cast to double.

**Integrity check on the overlap day 2026-07-06:** raw Telonex vs the repo vault — 84,737 rows each,
inner join on `timestamp_us` = 84,737 rows, `max|Δprice| = 0.0`, `server_timestamp_us` identical
100 %, `local_timestamp_us` identical 100 %. **Bit-identical.** The repo's Chainlink capture and
Telonex's `crypto_prices` are the same product.

### 3.2 What was pulled

| dataset | detail |
|---|---|
| markets | fresh `get_markets_dataframe(exchange="polymarket")` → 2,289,494 rows, **59,287** `btc-updown-5m-*`, closes through 2026-07-28 |
| resolved 5m markets Jul 21–25 | **1,440** = 288 × 5, i.e. **100 % of windows**, all with `book_snapshot_5` coverage |
| `book_snapshot_5` | **2,880 files** (1,440 markets × 2 outcomes), **0 failures**, snapshotted onto a 0.5 s grid over [close−12 s, close+2 s] → `data/a3/books5/snaps.parquet` |
| `crypto_prices` | 2026-07-18 → 07-26 → `data/a3/crypto_prices/` |

Nothing here overlaps the design sample (Feb–May 2026) or the repo's 5m vault.

### 3.3 Engine cross-validation

Fetching the fresh pipeline's inputs for **Jul 6–7** — an *independent* download path (Telonex
`book_snapshot_5`, real Down-token ladder, 5 levels) versus the repo's vault (`bookcurves`, Up token
only, synthetic Down) — reproduces the trade set exactly:

| edge_min | repo pipeline signals / fills / win / EV-per-share | fresh pipeline signals / fills / win / EV-per-share |
|---|---|---|
| 0.05 | 42 / 16 / 68.8 % / +0.4534¢ | 42 / 16 / 68.8 % / +0.45¢ |
| 0.03 | 50 / 18 / 72.2 % / +1.078¢ | 50 / 18 / 72.2 % / +1.08¢ |
| 0.02 | 54 / 21 / 66.7 % / −6.669¢ | 54 / 21 / 66.7 % / −6.67¢ |

Two independent data paths, identical answers. Also settled: **the Down token's best ask equals
`1 − (Up token's best bid)` in 5,017 / 5,017 comparisons, exactly.** The repo's synthetic-Down
convention is not an approximation at the top of book — it is the identity Polymarket's CLOB
enforces. (Deeper levels are *not* mirrored, so the real Down ladder still adds information for
capacity.)

### 3.4 Fresh results (2026-07-21 → 07-25, 1,440 windows, 5 days)

| edge_min | signals/day | trades/day | EV/share | median/share | win | t_iid | day-mean | t_day (4 df) | day-block 95 % CI | P(EV>0) |
|---|---|---|---|---|---|---|---|---|---|---|
| **0.05** | 40.0 | **21.6** | **+7.42¢** | +13.63¢ | 74.1 % | 1.84 | +8.00¢ | 2.12 (p = 0.10) | [+1.4¢, +14.6¢] | 0.994 |
| 0.03 | 45.8 | 25.4 | +6.05¢ | +11.85¢ | 75.6 % | 1.66 | +6.91¢ | 3.23 (p = 0.032) | [+3.3¢, +10.4¢] | 1.000 |
| 0.02 | 52.4 | 27.8 | +5.89¢ | +9.72¢ | 77.0 % | 1.77 | +6.47¢ | 3.90 (p = 0.017) | [+3.5¢, +9.3¢] | 1.000 |

Daily (edge_min 0.05): Jul 21 +9.9¢ (22 fills), Jul 22 **−1.6¢** (12), Jul 23 +20.4¢ (12),
Jul 24 +2.2¢ (30), Jul 25 +9.2¢ (32). Four of five days positive.
Side split: Down +11.2¢ (n=57, 79 % win), Up +3.2¢ (n=51, 69 % win).

**Decay is real but only marginally significant.** Apr 16 – May 12 (+15.80¢, n=933) vs
Jul 21–25 (+7.42¢, n=108): Welch t = 1.98, **p = 0.050**.

### 3.5 Latency sensitivity (fresh data, edge_min 0.05)

The signal set is fixed at 200 (the signal is evaluated on the book at `t`); only the fill changes.

| simulated latency | 0.5 s | 1.0 s | **1.5 s (shipped)** | 2.0 s | 3.0 s |
|---|---|---|---|---|---|
| fills (trades/day) | 148 (29.6) | 121 (24.2) | **108 (21.6)** | 93 (18.6) | 71 (14.2) |
| EV/share | +5.74¢ | +7.09¢ | **+7.42¢** | +7.49¢ | +5.18¢ |
| win | 72.3 % | 73.6 % | 74.1 % | 74.2 % | 71.8 % |
| PnL @ $25 (5 d) | $211 | $273 | **$294** | $337 | $113 |

The edge is flat across 1–2 s and only degrades at 3 s, matching `docs/03`'s "robust at 2 s latency"
claim. Slower is *not* uniformly worse: the surviving signals are better ones, so EV/share rises
slightly even as fill count falls — right up until 3 s, where the book is gone.

---

## 4. Capacity

### 4.1 Fresh data — real 5-level ladders, both outcomes

Per **filled** signal: walk the fill-time ladder taking every level within 3 ¢ of the best ask that
still clears `edge_min = 0.05`, with an unlimited budget.

| | value |
|---|---|
| median fillable notional | **$83** |
| mean / p75 / p90 / p95 / max | $262 / $400 / $595 / $924 / $3,433 |
| fills supporting ≥ $25 / $50 / $100 / $250 / $500 / $1,000 | 81 / 64 / 49 / 32 / 20 / 5 (of 108) |
| ladder exhausted at level 5 | **0 %** — every figure above is a **lower bound** |
| total fillable | $28,274 over 5 days = **$5,655/day** (vs $540/day at the $25 cap) |

**Deep-book moments carry more edge, not less** (same conclusion as the 1h audit):

| capacity quartile | ≤$25 | $25–83 | $83–400 | **$400–3,433** |
|---|---|---|---|---|
| EV/share | +7.5¢ | +2.5¢ | +5.6¢ | **+14.0¢** |

Replaying the fresh 5 days at different `per_event_cap_usd`:

| cap | shares | PnL (5 d) | per day | EV/share |
|---|---|---|---|---|
| **$25** (current) | 3,908 | $294 | $59 | 7.5¢ |
| $100 | 11,037 | $1,094 | $219 | 9.9¢ |
| $250 | 19,340 | $2,221 | **$444** | 11.5¢ |
| $1,000 | 35,177 | $4,878 | $976 | 13.9¢ |
| $5,000 | 41,651 | $6,649 | $1,330 | 16.0¢ |

EV/share *rises* with the cap because the larger clips are concentrated in the deep-book, high-edge
signals. **Caveat:** this is a replay against a standing book snapshot. It does not model market
impact, queue priority, or the fact that sweeping $1,000 in the last 3 seconds is itself information.
Treat $250 as the defensible number and anything above it as untested.

### 4.2 Repo bookcurves (Up token only, orig OOS window, n = 491 up-side fills)

Median **$200**, p75 $1,000, p90 $1,000, mean $726; 82 % of fills support ≥ $25, 56 % ≥ $100,
27 % ≥ $500. Total fillable $356,300 vs $12,275 actually deployed at the $25 cap (**29×**). Edge by
depth quartile: +11.4¢ / +9.9¢ / +27.2¢ / **+36.8¢**. Consistent with the fresh measurement, and
larger — again the April/May regime looks better than July.

---

## 5. What this means

1. **The 5m edge is real and still present, but it has roughly halved.** +15.5¢/share on the original
   OOS window → +7.4¢/share on fresh July data, with frequency down from ~33 to ~22 trades/day.
   The decay test is p = 0.05 — suggestive, not settled.
2. **The original +14¢ / 52-trades claim is reproducible but was measured under a more permissive
   execution convention.** The 52 trades/day never survives a 1.5 s re-fetch; ~1/3 of signals die
   because the book moves. Plan on ~22 trades/day, not 52.
3. **5m is still an order of magnitude more scalable than 1h.** Median fillable notional per signal is
   $83–200 versus $11 on 1h, and the deep-book signals carry the most edge. At `per_event_cap_usd`
   $250 the fresh window is worth **~$444/day** on paper — against ~$4.50/day realised on the live
   1h leg. Even at half the modelled edge, that is the difference between a rounding error and a
   business.
4. **The publication lag is a tax, not a wall.** Honest handling costs ~1.9¢/share and ~1 % of
   trades. A live feed must nonetheless carry the 4 s staleness guard (1.5 % of decision seconds are
   staler than that) and must never key off `timestamp_us`.
5. **The blocker is genuinely a live feed, not data.** Telonex sells the identical historical series;
   `audit/A1_chainlink.md` §"Endpoint" already documents the live source
   (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`, filter `{"symbol":"btc/usd"}`).
6. **Two cheap parameter changes are indicated by this data**, both consistent with the 1h audit:
   - **Fire at τ≈4 s instead of the first qualifying second.** On the repo OOS window the shipped
     first-qualifying policy yields 32.7 trades/day at +15.5¢ (t = 11.9); a fixed τ=4 s policy yields
     19.6 trades/day at **+21.2¢** (t = 14.7) — ~6¢/share more for ~40 % fewer trades, and total PnL
     at the $25 cap only falls from $6,311 to $4,673 (which the higher cap in the next bullet more
     than recovers). Unlike 1h, waiting does **not** cost the fill.
   - **Raise `per_event_cap_usd` well above $25.** On fresh data $25 → $250 turns $59/day into
     $444/day *and* raises EV/share from 7.5¢ to 11.5¢.

### Caveats a reader should hold onto

- **5 fresh days is 5 days.** n = 108 fills; t_iid = 1.84. The fresh result is directionally
  reassuring, not proof. The obvious next step is 20–30 days of `book_snapshot_5` (a few hours of
  fetching at the rate measured here: 2,880 files in 760–860 s across two independent runs, 0 failures).
- Fills are simulated against a **static** book snapshot at t+1.5 s. No impact, no queue, no
  competition from the "active bot pack" `docs/03` flagged on 5m.
- Fee is fixed at `0.07·p·(1−p)`. That is exact for Jun–Jul 2026 and slightly optimistic for April
  (the vault records 0.072 there).
- The decision second is an exact integer second; the live bot ticks at a drifting sub-second phase.
  `audit/A2_replay.md` established that this is noise, not bias.
- The repo has **no 5m book data for June**, so the transition from the strong Apr/May regime to the
  weaker July regime is unobserved.
- `bot/tests`: 54 passed. No bot code was modified by this audit.

---

## Appendix — reproducing this

```bash
cd /home/user/S4
# Part 1 (repo vault): build the per-second eval frame, then the tables
python3 scripts/a3/run_repo.py "$(cat data/a3/days.txt)" all      # -> data/a3/evals_all.parquet
python3 scripts/a3/decompose.py                                   # §2.3 variant table

# Part 2 (fresh vendor data)
python3 scripts/a3/fetch_markets.py                               # -> data/a3/telonex_5m_markets.parquet
python3 scripts/a3/fetch_5m_books.py 2026-07-21 2026-07-25        # 2,880 book_snapshot_5 files -> snaps.parquet
python3 -c "from telonex import download; download(api_key=KEY, exchange='polymarket', \
  channel='crypto_prices', asset_id='btcusd', from_date='2026-07-18', to_date='2026-07-27', \
  download_dir='data/a3/crypto_prices')"
python3 scripts/a3/run_fresh.py 2026-07-21,2026-07-22,2026-07-23,2026-07-24,2026-07-25
```

| artefact | what it is |
|---|---|
| `data/a3/evals_all.parquet` | 61,835 per-second decisions, repo vault, 43 days |
| `data/a3/evals_fresh.parquet` | 7,200 per-second decisions, fresh vendor data, 5 days |
| `data/a3/books5/snaps.parquet` | 83,520 book snapshots (1,440 markets × 2 outcomes × 29 grid points) |
| `data/a3/crypto_prices/` | fresh Chainlink BTC/USD, 2026-07-06/07 and 07-18 → 07-26 |
| `data/a3/publish_lag.csv` | per-day publish-lag percentiles and stream coverage, 52 days |
