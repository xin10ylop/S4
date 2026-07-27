# C1 — Fresh 5m data fetch

Fetcher: `scripts/fresh5m/fetch.py` (resumable, day-granular; every stage skips a day whose
output file already exists). Summary: `scripts/fresh5m/summarize.py`.
Output root: `data/fresh5m/`.

```
python3 scripts/fresh5m/fetch.py crypto  --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py markets --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py quotes  --from 2026-06-01 --to 2026-07-26 --workers 24
python3 scripts/fresh5m/fetch.py prepass --from 2026-06-01 --to 2026-07-26
python3 scripts/fresh5m/fetch.py books   --from 2026-06-01 --to 2026-07-26 --workers 12
python3 scripts/fresh5m/fetch.py outage  --from 2026-06-01 --to 2026-07-26 [--source books]
```

## 0. Headline

**56 days fetched: 2026-06-01 .. 2026-07-26.** 16,121 markets, all resolved. 32,238 of 32,242
possible quote tapes (99.99 %), 425.7 M quote rows, 6.1 GB. Book depth for the 19-day target period.

Three findings that change the decision, beyond the raw data:

1. **JUNE EXISTS.** Telonex serves `quotes` and `book_snapshot_25` for every 5m market from
   2026-06-01 onward. The "no 5m book data for June at all" blind spot was a gap in the *repo's local
   vault*, not in the vendor. **All 30 June days are now on disk.**
2. **THE LIQUIDITY REGIME BROKE ON 2026-06-27, inside that blind spot.** Top-of-book update
   intensity fell ~3.5× and never recovered: Jun 1–12 **197/s**, Jun 13–26 **141/s**, Jun 27–Jul 7
   **55/s**, Jul 8–26 **51/s** (per market-outcome tape, measured over an identical 125 s retained
   window every day, so this is apples-to-apples). The transition the stationarity question was
   asking about is no longer unobserved — it is dated, and it is a step, not a drift. §7.
3. **The A3 outage is real, and I reproduced it independently — but it is not unique, and it is not
   random.** 2026-07-21 04:05:19Z–~04:40Z froze 7 consecutive windows at 56–2073 s of book age,
   matching the verifier's "355–1975 s, one instant at ~04:07 UTC". It is one of **five** such
   multi-market freezes in 56 days, and **every freeze longer than 1400 s begins between 04:00:00
   and 04:05:20 UTC** — midnight ET, Polymarket's daily rollover. The 04h hour holds **47 % of all
   frozen markets against a 4.17 % baseline (11×)**. This is a recurring scheduled disturbance, not
   bad luck. §6.

Outside those episodes the data is clean: median book age at the decision instant is **0.02–0.05 s**
and p99 is **0.6–1.6 s** on every unaffected day. Pooled over 32,238 decision instants, only
**152 (0.471 %)** sat on a book older than 20 s — 0.201 % in the July target period, 0.672 % in June.

## 1. Item 1 — crypto_prices (Chainlink). COMPLETE, and it is the real resolution source

56 days → `data/fresh5m/crypto_prices/<D>.parquet`, 4.68 M rows.

**Schema matches the repo's canonical store exactly** — same columns, same dtypes:

| column | dtype | meaning |
|---|---|---|
| `timestamp_us` | int64 | oracle OBSERVATION time |
| `server_timestamp_us` | int64 | **PUBLISH time** |
| `local_timestamp_us` | int64 | vendor capture time |
| `price` | float64 | BTC/USD (vendor ships a decimal string; cast on write) |

**`server_timestamp_us` IS present**, so the causality rule is fully enforceable. It is never earlier
than `timestamp_us` on any of the 4.68 M rows.

Cross-checks:

- **Bit-identical to the repo** on all three overlap days tested (2026-06-15, 2026-07-06,
  2026-07-07): identical row counts, `max|Δserver_timestamp_us| = 0`, `max|Δprice| = 1.5e-11`
  (float64 round-trip of the decimal string). Same feed, same capture.
- **Publish lag** p50 **1.00–1.36 s**, p99 **1.73–2.21 s** — matches A1's 1.119 s / 2.017 s.
- **The resolution rule reproduces `result_id`.** Applying A1's rule (strike = first report with
  observation ts ≥ window_start; settle = first with observation ts ≥ window_end; backfill; Up iff
  settle ≥ strike) to the 15,677 markets where both boundary prints exist within 5 s gives
  **99.974 % agreement** (4 disagreements). The fetched feed is the settlement source.

The 19 genuinely new days (2026-07-08..2026-07-26) were also copied into the repo's canonical store
`data/data/processed/daily/crypto_prices/`, which now runs 2026-04-02..2026-07-26 (116 files, no
gaps). Existing files were not modified.

### Chainlink gaps — READ THIS BEFORE USING JUNE

Coverage table: `data/fresh5m/crypto_coverage.parquet`. Most days sit at the ~96.9 % A1 documented,
but several June days are degraded and two are disqualifying:

| day | coverage | largest hole | note |
|---|---|---|---|
| 2026-06-11 | **49.5 %** | **29,033 s (8.1 h)** | unusable |
| 2026-06-10 | **80.5 %** | **12,939 s (3.6 h)** from 15:31:38Z | unusable; publish-lag trap, below |
| 2026-06-01 | 83.6 % | 6,573 s | 4 holes > 120 s |
| 2026-06-05 | 83.6 % | 5,045 s | 3 holes > 120 s |
| 2026-07-07 | 83.8 % | 2,908 s | 5 holes > 120 s |
| 2026-06-03 | 93.7 % | 3,401 s | |
| 2026-06-04 | 94.3 % | 2,559 s | |
| 2026-07-23 | 92.7 % | 2,811 s from 18:01:33Z | worst day in the target period |
| 2026-07-24 | **94.1 %** | 387 s | **added on re-verification — also below 95 %, also in the target period** |

**2026-06-10 is the worst publish-time trap.** 7,273 of its reports have a publish lag > 10 s, max
**7,923 s (2.2 h)** — the vendor backfilled hours-late observations. Anything keying off
`timestamp_us` silently imports up to ~2 h of foresight on that day. Enforcing
`server_timestamp_us <= t` handles it correctly. That is why the rule is not optional.

**Correction (re-verification): the trap is NOT confined to 2026-06-10.** 18 of 56 days carry
reports with publish lag > 10 s, and **9 days exceed 60 s**: 06-03 (121 s), 06-08 (63 s),
06-09 (216 s), **06-10 (7,924 s)**, 06-19 (205 s), 06-20 (191 s), **06-21 (874 s)**, 06-29 (364 s),
and **07-24 (79 s) — inside the target period**. Four target-period days (07-11, 07-12, 07-22,
07-24) carry lag > 10 s reports. 2026-06-21 in particular (874 s / 14.6 min trap, 860 s coverage
hole) was not previously flagged at all. None of this is a data defect — `server_timestamp_us` is
present and correct on every row — but it means the causality rule is load-bearing on the *fresh
July days too*, not just on one bad June day.

Per-day evaluability (both resolution boundaries present) is in `markets.parquet.evaluable`:
target period **97.9–99.7 %**; June 06-11 51 %, 06-01 83 %, 06-05 83 %, 06-10 84 %, rest ≥ 94 %.

## 2. Item 2 — 5m market metadata. COMPLETE

`data/fresh5m/markets.parquet` — **16,121 markets, 56 days, 100 % `status == resolved`**,
`result_id` split 8,018 Up / 8,103 Down. Carries `asset_id_0` (Up) / `asset_id_1` (Down),
`result_id`, `market_id`, `close_s`, `window_start_s`, plus `evaluable` / `pred_up` from the
resolution check above.

Verified: `slug == btc-updown-5m-<window_start>` and `end_date_us/1e6 − 300 == window_start` on **all
16,121 rows, 0 mismatches**. 288 markets/day except 2026-06-17 (283), 2026-06-20 (287),
2026-07-12 (287) — those windows do not exist upstream.

**Date key**: files are keyed by `d = date(close_s − 1)`, so day *D* holds the 288 windows closing in
`(D 00:00, D+1 00:00]`. Closes and midnight are both on 5-minute boundaries, so a 5m window can never
straddle a UTC date — the whole window is always inside its own file.

## 3. Item 3 — quotes. COMPLETE, all 56 days

`data/fresh5m/quotes/<D>.parquet`. Columns: `close_s`, `oid` (0 = Up), `timestamp_us`,
`local_timestamp_us`, `bid_price`, `bid_size`, `ask_price`, `ask_size` (prices/sizes float32).
**425,743,161 rows / 32,238 tapes / 6.12 GB.**

Coverage is **576/576 tapes on every day** bar two. In the whole 32,242-request run exactly **two
markets 404'd** on both outcomes: `btc-updown-5m-1783888500` (2026-07-12) and
`btc-updown-5m-1782098400` (2026-06-22). No other failure of any kind.

**Tape is trimmed to `[close − 120 s, close + 5 s]`, plus the single last update strictly before that
cut.** The untrimmed in-window tape was 11.4 M rows / 174 MB *per day* in July and up to 38 M rows /
305 MB in June — ~15 GB for 56 days, against 15 GB free on this box. 120 s is 24× the widest decision
band (τ ≤ 5 s) and still spans the two-book re-check. 96.5 % of raw rows were already inside the
300 s window, so the trim costs almost no in-window resolution.

**Retaining the last pre-cut update means book age at the decision instant is exact even when the
book was frozen far longer than the retained span** — that is what let the 2,394 s freeze in §6 be
measured at all. Nothing is forward-filled and nothing is dropped for staleness.

## 4. Item 4 — book_snapshot_25 for candidate windows. COMPLETE for the target period

`data/fresh5m/books/<D>.parquet`, 25 levels per side, trimmed to `[close − 45 s, close + 2 s]` plus
the last pre-cut row. **19 days (2026-07-08..2026-07-26), 1,343 windows, 29.9 M rows, 1.03 GB, zero
fetch failures.** Candidate list: `data/fresh5m/candidates.parquet`.

Selection (`prepass`) keeps a window if, at any τ ∈ {2,3,4,5} s, some outcome's last ask is in
(0.30, 0.99) and `fair − ask − fee > 0` — **edge_min = 0, strictly more generous than the shipped
0.01**. `fair` comes from Chainlink under the causal rule (§5). Because σ is the one genuinely free
parameter of `fair`, the pre-pass **sweeps σ ∈ {0.5σ̂, σ̂, 2σ̂}** and keeps the window if any clears,
so the book set is a superset of what a differently-calibrated replay would pick rather than a
hostage to my calibration.

It also fetches **12 randomly chosen REJECTED windows per day as a control**
(`candidates.is_control == True`), so the replay can measure what the prefilter discarded instead of
taking it on trust.

**The prepass is complete for all 56 days**: `candidates.parquet` holds **3,986 distinct windows —
3,314 true candidates + 672 controls**, ~71/day in June and ~71/day in July (of 288, i.e. ~25 %).
**2,968 clear the shipped `edge_min = 0.01`** — comfortably more than the ~20 honest fills/day the
strategy would actually take, so candidate supply is not the binding constraint on the replay.

Book *downloads* were complete for 2026-07-08..2026-07-26 and still running for June + Jul 1–7 at
write time. The stage is resumable and idempotent — `fetch.py books --from 2026-06-01 --to
2026-07-07` picks up wherever it stopped; check `ls data/fresh5m/books | wc -l` for the current
count. **June quotes are complete regardless**, and quotes alone support the top-of-book replay and
the whole of §6 and §7.

## 5. Item 5 — Binance 1s klines. ALREADY COMPLETE, nothing fetched

`data/data/processed/binance/klines_1s/` already runs **2025-10-10 .. 2026-07-26**, 290 files, **no
missing day** in 2026-04-02..2026-07-26. Schema verified: `open_time`/`close_time` int64
**microseconds**, 86,400 rows/day. No work needed.

## 6. BOOK FRESHNESS AND THE OUTAGE SCAN

Every saved quote and book row keeps the vendor's own `timestamp_us` (the exchange/last-update
instant) next to `local_timestamp_us` (vendor capture). Book age at decision instant `t` is
`t − timestamp_us` of the last row with `timestamp_us <= t`. **Nothing is forward-filled at fetch
time and nothing is dropped for staleness** — staleness is recorded; what to do about it is the
replay's call, not the fetcher's.

**Causality: `S_t` is taken from the last Chainlink report with `server_timestamp_us <= t`, never
`timestamp_us <= t`. I enforced this**; `candidates.parquet` was produced under that rule, and σ̂
uses only the trailing 180 s of *published* prints.

An outage episode is a maximal run of **consecutive** 5m closes in which at least one outcome's book
was frozen ≥ 20 s at its own τ = 3 s decision instant. (Grouping on an *identical microsecond*
last-update instant — the A3 write-up's phrasing — under-counts: the two outcomes of one market share
an instant, but neighbouring markets each froze on their own last tick seconds apart. What identifies
a vendor outage is that last-update instants all predate a common cutoff while decision instants
march on.) Outputs: `outage_clusters_quotes.parquet`, `age_by_day_quotes.parquet`.

**25 episodes in 56 days. All multi-market ones (≥ 2 markets):**

| day | first frozen update (UTC) | markets | obs | age range |
|---|---|---|---|---|
| **2026-07-01** | **04:05:00.081** | **7** | 14 | **311.8 – 2394.7 s** |
| **2026-06-24** | **04:02:59.095** | **6** | 12 | **316.6 – 2217.9 s** |
| **2026-07-21** | **04:05:19.758** | **7** | 14 | **56.5 – 2073.1 s** ← the A3 one |
| **2026-06-10** | **04:00:00.128** | **5** | 10 | **285.1 – 1796.9 s** |
| **2026-06-19** | **04:05:10.130** | **5** | 10 | **240.4 – 1481.2 s** |
| 2026-06-19 | 04:57:12.553 | 3 | 6 | 75.9 – 764.4 s |
| 2026-06-03 | 12:14:36.114 | 9 | 18 | 20.9 – 320.2 s |
| 2026-06-05 | 12:23:07.012 | 5 | 10 | 110.0 – 297.0 s |
| 2026-06-02 | 14:18:49.581 | 3 | 6 | 63.3 – 165.1 s |
| 2026-06-03 | 15:18:17.529 | 5 | 10 | 23.5 – 149.7 s |
| 2026-06-02 | 13:14:23.823 | 2 | 4 | 33.2 – 99.6 s |
| 2026-06-02 | 10:28:19.886 | 3 | 6 | 79.1 – 97.1 s |
| 2026-06-02 | 12:28:29.232 | 3 | 6 | 47.8 – 87.8 s |
| 2026-06-02 | 13:39:09.160 | 2 | 4 | 47.8 – 59.7 s |

Plus 11 isolated single-market blips (61–206 s), 5 of them also in the 04h hour.

**The 2026-07-21 episode in full — the A3 contamination, re-derived from scratch:**

| window close (UTC) | last book update (UTC) | age at τ=3 s | ask Up / Down |
|---|---|---|---|
| 04:10 | 04:09:00.454 | 56.5 s | 0.93 / 0.09 |
| 04:15 | 04:09:00.255 | 356.7 s | 0.43 / 0.58 |
| 04:20 | 04:07:49.499 | 727.5 s | 0.49 / 0.52 |
| 04:25 | 04:09:00.007 | 957.0 s | 0.51 / 0.50 |
| 04:30 | 04:05:19.758 | 1477.2 s | 0.51 / 0.50 |
| 04:35 | 04:05:21.885 | 1775.1 s | 0.51 / 0.50 |
| 04:40 | 04:05:23.872 | 2073.1 s | 0.51 / 0.50 |

Ages (356–2073 s) and the ~04:07Z anchor line up with the verifier's "355–1975 s, one last-update
instant at 2026-07-21 ~04:07 UTC". **The verifier was right.** The last four windows never received
a single book update during their own 5-minute window; their books sat pinned at 0.51/0.50 while BTC
moved. Any fill simulated there is against liquidity never observed to exist.

**Confirmed independently on the `book_snapshot_25` channel** (`fetch.py outage --source books`,
`outage_clusters_books.parquet`), a different vendor channel from the quote tape above:

| day | first frozen update (UTC) | markets | age range |
|---|---|---|---|
| 2026-07-21 | **04:07:06.907** | 6 | **356.6 – 1923.0 s** |
| 2026-07-10 | 04:06:31.589 | 1 | 205.4 s |
| 2026-07-08 | 21:16:56.460 | 1 | 180.5 s |
| 2026-07-24 | 04:03:34.648 | 1 | 82.4 s |

356.6–1923.0 s at 04:07:06Z reproduces the verifier's "355–1975 s at ~04:07 UTC" to within a few
seconds, from an independent re-fetch. The depth channel and the top-of-book channel froze together,
which is what a vendor/exchange-side outage looks like and rules out a capture artefact in one
channel. (6 markets not 7 because books are only fetched for candidate windows.) The book channel is
otherwise even cleaner than quotes: median age 0.00 s, p99 0.1–0.2 s on unaffected days.

### The 04:00 UTC rollover is a systematic hazard, not an accident

| | episodes | frozen markets | worst age |
|---|---|---|---|
| 04:00–04:59 UTC | 9 | **36** | 2394.7 s |
| all other 23 hours | 16 | 40 | 320.2 s |

36 of 76 frozen markets (**47 %**) fall in one hour that carries **4.17 %** of closes — an **11×**
concentration — and **every episode exceeding 1400 s starts between 04:00:00 and 04:05:20 UTC**.
That is midnight ET, Polymarket's daily rollover.

*Counting convention (clarified on re-verification):* those 36 are markets whose **freeze began** in
the 04h hour. Counting instead by the market's own **close hour** gives **33 of 76 (43.4 %)** — the
other 3 froze at ~04:0x but closed after 05:00, because the freezes run up to 40 minutes. Both
counts are correct under their own definition and both are a 10–11× concentration; the conclusion is
unchanged.

**Consequences.** For the backtest: drop any fill whose book age at the decision instant exceeds a
hard threshold (20 s is generous against a clean p99 of 1.6 s); that discards 0.471 % of decision
instants over 56 days and removes exactly the failure mode that supplied 53.6 % of A3's P&L. For any
live 5m deployment: a book-age guard is **mandatory, not optional**, and the 04:00–05:00 UTC hour
deserves a hard skip — a live bot without one would have sent real orders into a book frozen for 40
minutes on five separate days in this sample.

## 7. The liquidity regime broke on 2026-06-27

Mean top-of-book updates/s per market-outcome tape, measured over the identical 125 s retained window
on every day (`coverage_summary.parquet`):

| period | updates/s |
|---|---|
| Jun 1 – Jun 12 | **196.8** |
| Jun 13 – Jun 26 | **141.3** |
| Jun 27 – Jul 7 | **55.1** |
| Jul 8 – Jul 26 | **51.1** |

Day-level, the break is sharp and dated: 2026-06-26 111.7/s → 06-27 79.1 → 06-28 59.0 → 06-29 50.7,
and it never returns above ~68/s in the following 29 days.

This lands **squarely inside the June window that no prior analysis could see**, and it is the same
place the edge is claimed to have changed (Apr/May +13c…+19c vs July +0.45c…+7.4c, Welch p = 0.050).
It is a step change in market microstructure, not a slow drift, which is consistent with a
participant entering or leaving rather than with noise. I am reporting the measurement, not the
causal claim — the replay now has the data to test whether the 5m edge steps down at the same date.
Note the direction is not obvious in advance: *fewer* top-of-book updates could mean less competition
(better for us) or a thinner, more adversarial book (worse). Depth from `books/` for June is what
settles it.

## 8. INDEPENDENT RE-VERIFICATION (second pass, recomputed from the parquet files)

Everything in §§1–7 was re-derived from the saved files by a separate script that does **not** call
`fetch.py`'s own coverage or outage code, so a bug in the fetcher could not confirm itself. Result:
**every headline number reproduces exactly.**

| claim | doc | recomputed | match |
|---|---|---|---|
| quote tapes | 32,238 | **32,238** | ✅ |
| quote rows | 425,743,161 | **425,743,161** | ✅ |
| frozen (age ≥ 20 s) decision instants | 152 (0.471 %) | **152 (0.471 %)** | ✅ |
| distinct frozen markets | 76 | **76** | ✅ |
| markets / all resolved | 16,121 / 100 % | **16,121 / 100 %** | ✅ |
| liquidity regime, 4 periods | 196.8 / 141.3 / 55.1 / 51.1 per s | **196.8 / 141.3 / 55.1 / 51.1** | ✅ |
| break day-by-day 06-25→06-30 | 111.7 → 79.1 → 59.0 → 50.7 | **113.6 / 111.7 / 79.1 / 59.0 / 50.7 / 53.6** | ✅ |
| Chainlink resolution rule vs `result_id` | 99.974 % | **99.98 % (15,619 / 15,622)** | ✅ |
| crypto schema + `server_timestamp_us` | present, never < observe | **present, 0 negative lags in 4.64 M rows** | ✅ |

The **2026-07-21 episode reproduces row for row** (56.5 / 356.7 / 727.5 / 957.0 / 1477.2 / 1775.1 /
2073.1 s, anchored at 04:05:19.758Z–04:09:00Z). This is now the *third* independent derivation of
that outage — the original verifier, the first C1 pass, and this one. **It is settled fact.**

Post-break liquidity never recovers: max **67.9 updates/s** on any day after 2026-06-28, against a
Jun 1–26 mean of 166.9/s — a **3.23× step down**.

### The publish-lag rule is worth ~$1.35 of foresight per decision, measured

The causality rule was asserted in §6 but never priced. At each market's τ = 3 s decision instant I
took `S_causal` (last report with `server_timestamp_us ≤ t`, legal) and `S_peek` (last report with
`timestamp_us ≤ t`, illegal) and differenced them over all 16,121 markets:

| | all 56 days | target 07-08..07-26 |
|---|---|---|
| instants where the two differ | **96.5 %** | **97.8 %** |
| mean \|S_peek − S_causal\| | **$2.34** | **$1.35** |
| max | $420 (06-10) | **$64** |

**Keying the signal off observation time changes the BTC price used on ~97 % of decisions**, by
$1.35 on a typical fresh-July decision. On a 5-minute at-the-money binary that is not a rounding
error — it is ~1.1 s of future price movement handed to the strategy for free, and it is exactly the
mechanism A1 warned would "manufacture an edge that does not exist." Any replay that reads
`timestamp_us` instead of `server_timestamp_us` should be assumed contaminated until re-run.

## 9. What is NOT here / caveats

- **Quote tape is trimmed to 120 s pre-close** (books to 45 s). Anything needing the full 5-minute
  tape — e.g. modelling intra-window drift from the window open — must re-fetch. Disk, not the
  vendor, was the binding constraint.
- **book_snapshot_25 is candidate-only by design, and complete only for 2026-07-08..2026-07-26.**
  Rejected windows have quotes but no depth; the 12 controls/day are the check on that. June +
  Jul 1–7 books were still downloading at write time — resume with
  `fetch.py books --from 2026-06-01 --to 2026-07-07`.
- **June 10 and June 11 Chainlink coverage is 80 % / 49 % with multi-hour holes** — exclude them or
  the June leg of the stationarity test measures vendor downtime. June 1, 3, 4, 5 and July 7 are
  partially degraded. Note June 10 and June 19 also carry book freezes, so June's bad days are bad
  in two independent ways.
- **2026-07-27 is deliberately excluded** — only 55 of 288 windows had resolved at fetch time, so
  2026-07-26 is the most recent complete day.
- The 4 resolution disagreements (`btc-updown-5m-` 1781229000, 1781740500, 1782481500, 1783450800)
  are boundary-second edge cases. At 4/15,677 they move nothing, but the rule is not exact to the
  last basis point.
- `trades` and `book_snapshot_full` were not fetched (not needed for the close-snipe replay).
- The 5m family remains DISABLED behind `strategy.close_snipe.allowed_families = ["1h"]`. Nothing in
  this run changes that; this run only supplies the data to decide it.
