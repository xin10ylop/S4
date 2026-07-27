# C1 — Fresh 5m data fetch

Fetcher: `scripts/fresh5m/fetch.py` (resumable, day-granular; every stage skips a day whose
output file already exists). Summary: `scripts/fresh5m/summarize.py`.
Output root: `data/fresh5m/`.

Run stages in order:

```
python3 scripts/fresh5m/fetch.py crypto  --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py markets --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py quotes  --from 2026-06-01 --to 2026-07-26 --workers 24
python3 scripts/fresh5m/fetch.py prepass --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py books   --from 2026-06-01 --to 2026-07-26 --workers 12
python3 scripts/fresh5m/fetch.py outage  --from 2026-06-01 --to 2026-07-26 [--source books]
```

## 0. Headline

**JUNE EXISTS.** Telonex serves `quotes` and `book_snapshot_25` for every 5m market from
**2026-06-01** onward — all 16,121 markets in 2026-06-01..2026-07-26 carry non-null
`quotes_from` / `book_snapshot_25_from`. The "no 5m book data for June at all" blind spot in every
prior analysis was a gap in the *repo's local vault*, not in the vendor. The stationarity question
(Apr/May +13c..+19c vs July +0.45c..+7.4c, Welch p=0.050, transition unobserved) is now answerable
on data, not assumption.

**The A3 outage is real, and I reproduced it independently from a clean re-fetch.** On
2026-07-21 the vendor's book feed froze from **04:05:19.758Z to ~04:40Z** (~35 min). Seven
consecutive 5m windows (closes 04:10–04:40) traded against books last updated 56–2073 s earlier.
This is the *only* multi-market freeze in the 19-day target period.

**The rest of the fresh period is clean.** Pooled over 10,940 decision instants in
2026-07-08..2026-07-26, only **22 (0.201 %)** sat on a book older than 20 s, and **14 of those 22
are the single 2026-07-21 episode**. Median book age at the decision instant is **0.02–0.05 s**;
p99 is **0.6–1.5 s** on every day except 2026-07-21.

## 1. Item 1 — crypto_prices (Chainlink). COMPLETE, and it is the real resolution source

56 days fetched: **2026-06-01 .. 2026-07-26**, 4,682,510 rows → `data/fresh5m/crypto_prices/<D>.parquet`.

**Schema matches the repo's canonical store exactly** — same columns, same dtypes:

| column | dtype | meaning |
|---|---|---|
| `timestamp_us` | int64 | oracle OBSERVATION time |
| `server_timestamp_us` | int64 | **PUBLISH time** |
| `local_timestamp_us` | int64 | vendor capture time |
| `price` | float64 | BTC/USD (vendor ships it as a string; cast here) |

**`server_timestamp_us` IS present** — the causality rule is fully enforceable. It is never
earlier than `timestamp_us` on any of the 4.68 M rows.

Cross-checks:

- **Bit-identical to the repo** on all three overlap days tested (2026-06-15, 2026-07-06,
  2026-07-07): identical row counts, `max|Δserver_timestamp_us| = 0`, `max|Δprice| = 1.5e-11`
  (float64 round-trip of the vendor's decimal string). Same feed, same capture.
- **Publish lag** p50 **1.00–1.36 s**, p99 **1.73–2.21 s** — matches A1's 1.119 s / 2.017 s.
- **Resolution rule reproduces `result_id`**: applying A1's rule (strike = first report with
  observation ts ≥ window_start, settle = first with observation ts ≥ window_end, backfill, Up iff
  settle ≥ strike) to the 15,677 markets where both boundary prints exist within 5 s gives
  **99.974 % agreement** (4 disagreements / 15,677). The fetched feed is the settlement source.

The 19 genuinely new days (2026-07-08..2026-07-26) were also copied into the repo's canonical store
`data/data/processed/daily/crypto_prices/`, which now runs 2026-04-02..2026-07-26 (116 files, no gaps).
Existing files were not touched.

### Chainlink gaps — READ THIS BEFORE USING JUNE

Coverage table: `data/fresh5m/crypto_coverage.parquet`. Coverage is the ~96.9 % A1 documented on
most days, but several June days are badly degraded and **two are disqualifying**:

| day | coverage | largest hole | note |
|---|---|---|---|
| 2026-06-11 | **49.5 %** | **29,033 s (8.1 h)** | unusable |
| 2026-06-10 | **80.5 %** | **12,939 s (3.6 h)** from 15:31:38Z | unusable; see below |
| 2026-06-01 | 83.6 % | 6,573 s | 4 holes > 120 s |
| 2026-06-05 | 83.6 % | 5,045 s | 3 holes > 120 s |
| 2026-07-07 | 83.8 % | 2,908 s | 5 holes > 120 s |
| 2026-06-03 | 93.7 % | 3,401 s | |
| 2026-06-04 | 94.3 % | 2,559 s | |
| 2026-07-23 | 92.7 % | 2,811 s from 18:01:33Z | only degraded day in the target period |

**2026-06-10 is a publish-time trap.** 7,273 of its reports have a publish lag > 10 s, max
**7,923 s (2.2 h)** — the vendor backfilled hours-late observations. Anything keying off
`timestamp_us` will silently import ~2 h of foresight on that day. Enforcing
`server_timestamp_us <= t` handles it correctly, which is exactly why that rule is not optional.

Per-day evaluability (both resolution boundaries present) is in `markets.parquet.evaluable`.
Target period 2026-07-08..2026-07-26: **97.9–99.7 %**. June: 06-11 51 %, 06-01 83 %, 06-05 83 %,
06-10 84 %, rest ≥ 94 %.

## 2. Item 2 — 5m market metadata. COMPLETE

`data/fresh5m/markets.parquet` — **16,121 markets**, 56 days, **100 % `status == resolved`**,
`result_id` split 8,018 Up / 8,103 Down. Carries `asset_id_0` (Up) / `asset_id_1` (Down),
`result_id`, `market_id`, `close_s`, `window_start_s`, plus the `evaluable` / `pred_up` columns from
the resolution-rule check above.

Verified: `slug == btc-updown-5m-<window_start>` and `end_date_us/1e6 - 300 == window_start` on
**all 16,121 rows, 0 mismatches**. Day counts are 288/day except 2026-06-17 (283), 2026-06-20 (287),
2026-07-12 (287) — those windows do not exist upstream.

**Date key**: files are keyed by `d = date(close_s - 1)`, so day *D* holds the 288 windows closing in
`(D 00:00, D+1 00:00]`. Closes and midnight are both on 5-minute boundaries, so a 5m window can never
straddle a UTC date — the whole window is always inside its file.

## 3. Item 3 — quotes. COMPLETE for the target period

`data/fresh5m/quotes/<D>.parquet`. Columns: `close_s`, `oid` (0 = Up), `timestamp_us`,
`local_timestamp_us`, `bid_price`, `bid_size`, `ask_price`, `ask_size` (prices/sizes float32).

Coverage is **576/576 tapes on every day** (288 markets × 2 outcomes). The single exception in the
whole run is **2026-07-12**, where market `btc-updown-5m-1783888500` 404s on both outcomes
(286 of 287 markets). No other fetch failure occurred anywhere.

**Tape is trimmed to `[close − 120 s, close + 5 s]`, plus the single last update strictly before
that cut.** The untrimmed in-window tape runs ~65 top-of-book updates/s — 11.4 M rows and 174 MB
*per day*, i.e. ~9.7 GB for 56 days, which does not fit in the 15 GB free on this box. 120 s is 24×
the widest decision band (τ ≤ 5 s) and still spans the two-book re-check. **Retaining the last
pre-cut update means book age at the decision instant is still exact even when the book was frozen
for far longer than the retained span** — that is what let the 2073 s freeze below be measured at
all. 96.5 % of the raw rows were already inside the 300 s window, so the trim costs almost no
in-window resolution.

## 4. Item 4 — book_snapshot_25 for candidate windows

`data/fresh5m/books/<D>.parquet`, 25 levels each side, trimmed to `[close − 45 s, close + 2 s]`
plus the last pre-cut row. Candidate list: `data/fresh5m/candidates.parquet`.

Pre-pass (`prepass`) keeps a window if, at any τ ∈ {2,3,4,5} s, some outcome's last ask is in
(0.30, 0.99) and `fair − ask − fee > 0` — i.e. **edge_min = 0, strictly more generous than the
shipped 0.01**. `fair` comes from Chainlink under the causal rule (§6). Because σ is the one
genuinely free parameter of `fair`, the pre-pass **sweeps σ ∈ {0.5σ̂, σ̂, 2σ̂}** and keeps the window
if *any* of them clears — so the book set is a superset of what a differently-calibrated replay
would select, not a hostage to my calibration.

It also fetches **12 randomly chosen REJECTED windows per day as a control**
(`candidates.is_control == True`), so the replay can measure what the prefilter discarded instead of
taking it on trust.

Yield ≈ 30–45 true candidate windows/day out of 288 (~11–16 %), + 12 controls.

## 5. Item 5 — Binance 1s klines. ALREADY COMPLETE, nothing fetched

`data/data/processed/binance/klines_1s/` already runs **2025-10-10 .. 2026-07-26**, 290 files,
**no missing day** anywhere in 2026-04-02..2026-07-26. Schema verified as
`open_time`/`close_time` int64 **microseconds**, 86,400 rows/day. No work needed.

## 6. BOOK FRESHNESS AND THE OUTAGE SCAN

Every saved quote and book row keeps the vendor's own `timestamp_us` (the exchange/last-update
instant) next to `local_timestamp_us` (vendor capture). Book age at a decision instant `t` is
`t − timestamp_us` of the last row with `timestamp_us <= t`. **Nothing is forward-filled at fetch
time and nothing is dropped for staleness** — staleness is recorded, and what to do about it is the
replay's decision, not the fetcher's.

Causality is enforced in the pre-pass and must be enforced in the replay: `S_t` is taken from the
last Chainlink report with **`server_timestamp_us <= t`**, never `timestamp_us <= t`. I did this;
`data/fresh5m/candidates.parquet` was produced under that rule. σ̂ likewise uses only the trailing
180 s of *published* prints.

### Outage scan

An outage episode is a maximal run of **consecutive** 5m closes in which at least one outcome's book
was frozen ≥ 20 s at its own τ = 3 s decision instant. (Grouping on an *identical microsecond*
last-update instant — the A3 write-up's phrasing — under-counts: the two outcomes of one market share
an instant, but neighbouring markets each froze on their own last tick seconds apart. What identifies
a vendor outage is that last-update instants all predate a common cutoff while decision instants
march on.) Output: `data/fresh5m/outage_clusters_quotes.parquet`, `age_by_day_quotes.parquet`.

**Every outage found in 2026-07-08..2026-07-26:**

| day | first frozen update (UTC) | markets | obs | age range |
|---|---|---|---|---|
| **2026-07-21** | **04:05:19.758** | **7** | **14** | **56.5 – 2073.1 s** |
| 2026-07-10 | 04:06:31.589 | 1 | 2 | 205.4 s |
| 2026-07-08 | 21:16:56.460 | 1 | 2 | 180.5 s |
| 2026-07-16 | 04:02:56.194 | 1 | 2 | 120.8 s |
| 2026-07-24 | 04:03:34.635 | 1 | 2 | 82.4 s |

The 2026-07-21 episode in full — this is the A3 contamination, re-derived from scratch:

| window close (UTC) | last book update (UTC) | age at τ=3 s | ask Up / Down |
|---|---|---|---|
| 04:10 | 04:09:00.454 | 56.5 s | 0.93 / 0.09 |
| 04:15 | 04:09:00.255 | 356.7 s | 0.43 / 0.58 |
| 04:20 | 04:07:49.499 | 727.5 s | 0.49 / 0.52 |
| 04:25 | 04:09:00.007 | 957.0 s | 0.51 / 0.50 |
| 04:30 | 04:05:19.758 | 1477.2 s | 0.51 / 0.50 |
| 04:35 | 04:05:21.885 | 1775.1 s | 0.51 / 0.50 |
| 04:40 | 04:05:23.872 | 2073.1 s | 0.51 / 0.50 |

The ages (356–2073 s) and the ~04:07Z anchor line up with the verifier's "355–1975 s, one
last-update instant at 2026-07-21 ~04:07 UTC". **The verifier was right.** The last four windows
never received a single book update during their own 5-minute window; their books sat pinned at
0.51/0.50 while BTC moved. Any fill simulated there is against liquidity that was never observed to
exist.

Note the four single-market blips cluster at **04:02–04:07 UTC**, as does the big one — there is a
recurring daily disturbance in that minute band. Worth a staleness guard in any live 5m deployment,
not just a backtest filter.

**Recommendation for the replay:** drop any fill whose book age at the decision instant exceeds a
hard threshold (20 s is generous; the clean p99 is 1.5 s). At 20 s this discards 0.201 % of decision
instants in the target period — a negligible cost, and it removes exactly the failure mode that
inflated A3 by 53.6 % of its P&L.

## 7. What is NOT here / caveats

- **Quote tape is trimmed to 120 s pre-close.** Anything needing the full 5-minute window tape (e.g.
  modelling intra-window market drift from the open) must re-fetch. Disk, not vendor, was the
  binding constraint. The `books` trim is tighter still (45 s).
- **book_snapshot_25 is candidate-only by design.** Windows the pre-pass rejected have quotes but no
  depth. The 12 controls/day are the check on that.
- **June 10 and June 11 Chainlink coverage is 80 % / 49 % with multi-hour holes** — exclude them or
  the June leg of the stationarity test will be measuring vendor downtime. June 1, 3, 4, 5 and
  July 7 are partially degraded.
- **2026-07-27 is deliberately excluded** — only 55 of 288 windows had resolved at fetch time.
- The 4 resolution disagreements (`btc-updown-5m-` 1781229000, 1781740500, 1782481500, 1783450800)
  are boundary-second edge cases; at 4/15,677 they do not move anything, but do not treat the rule
  as exact to the last basis point.
- `trades` and `book_snapshot_full` were not fetched (not needed for the close-snipe replay).
