# Phase 2 — Hypothesis Battery (written before any testing)

Ground rules: every hypothesis gets tested on the TRAIN split only; survivors go to untouched
TEST data. All results (pass/fail) land in `results/results_table.csv`. Fees: maker = 0,
taker = `0.07 × p × (1−p)` per share (current era), applied per leg.

## Data splits (fixed now, before testing)

| Family | Train | Test (untouched until validation) |
|---|---|---|
| 5m | 2026-02-12 → 2026-04-15 | 2026-04-16 → 2026-05-12, plus 2026-07-06/07 |
| 15m | 2025-10-11 → 2026-03-31 | 2026-04-01 → 2026-05-12, plus 2026-07-06/07 |
| 1h | 2025-10-11 → 2026-04-30 | 2026-05-01 → 2026-07-12 |
| 4h | 2025-10-15 → 2026-03-31 | 2026-04-01 → 2026-07-07 |

Fee-free-era (pre-2026-01-05) 15m results never extrapolate to the fee era.

## A. Fair-value machinery (prerequisite, not a hypothesis)

The contract is a digital option: P(Up) = P(close ≥ open) given current oracle distance
d = S_t − S_open, time remaining τ, and short-horizon vol σ. Fair ≈ Φ(d / (σ√τ)).
Build this from Chainlink 1s (5m/15m/4h) and Binance 1s (1h). Everything below tests
deviations between market price and this fair value, or predictability of the resolution
itself.

## B. Hypotheses

### Area 1 — Which side to buy (open / pre-open)
- H1: Binance momentum (returns over last 1/5/15/60 min before open) predicts resolution
  better than 50% — buy that side pre-open at ≤ fair.
- H2: Pre-open order-book imbalance (bid vs ask depth on both tokens) predicts resolution.
- H3: Pre-open signed taker flow predicts resolution.
- H4: Previous-window outcome streaks bias the next resolution (momentum or reversal).
- H5: The running 15m/1h/4h market states (their current P vs their strike) predict the 5m
  resolution (shared close logic).
- H6: Distance from current spot to the *previous* window's close (gap behavior) predicts resolution.
- H7: ML classifier (logistic + GBM) on all of H1–H6 features beats any single rule
  out-of-fold; use it to pick side + skip.
- H8: Time-of-day / day-of-week seasonality in resolution (e.g. US-hours drift) is exploitable.

### Area 2 — When to skip
- H9: Edge concentrates in low-vol regimes (distance decides early); skip high-vol windows.
- H10: Edge concentrates when pre-open price deviates from 0.50 (fade the herd) — skip when book is fair.
- H11: Spread/depth filters (skip when spread > 2c or depth < X) improve realized EV materially.

### Area 3 — Windows of market life (map first, then trade)
- H12: Build the full mispricing surface: E[outcome − mid] binned by (time-in-window ×
  z-score of distance × family). Any bucket with |bias| > costs is a strategy.
- H13: Pre-open prices far from 0.50 revert (no strike exists yet → fair ≈ 0.50 + ε).
  Fade via maker orders both sides.
- H14: First-seconds-after-open: price jumps toward early spot moves overshoot; fade.
- H15: Mid-life flow-driven deviations (price moved ≥ 2c with no matching spot move)
  mean-revert; maker fade with model-fair anchor.
- H16: Last-minute favorites are undervalued (favorite-longshot bias): buying the leader at
  0.85–0.97 in the final 30–60 s beats the price systematically after taker fees.
- H17: Last-5-seconds sniping: fair from spot vs stale quotes still leaves +EV after the 7%
  taker fee in some price buckets (quantify which the fee killed).
- H18: Settlement-window free money: after close, before resolution (~25–40 s), the winner
  (known via Chainlink/Binance print ~1.3 s after close) is still buyable < 0.99.
- H19: Buy-both-sides pre-open at 0.49/0.49 (set costs < $1); measure joint-fill probability
  and adverse selection of one-sided fills.

### Area 4 — Longer families from scratch
- H20: Full independent search on 1h (Binance-resolved, complete data): repeat H1–H18 machinery.
- H21: 4h thin-book market-making: two-sided maker quotes ±x around model fair with inventory
  caps captures spread (median $7.6k volume, less bot competition).
- H22: 4h directional: multi-hour Binance momentum/vol features predict resolution (real drift
  horizon, unlike 5m noise).

### Area 5 — Cross-market & oracle
- H23: Chainlink is a lagged transform of Binance spot (estimate lag/EMA); predicting the
  oracle print seconds ahead upgrades every close-time strategy (esp. H17).
- H24: Cross-family disagreement: when 5m/15m/1h/4h markets sharing the same close imply
  inconsistent probabilities, the outlier converges (relative value).
- H25: Oracle print variance: Chainlink publish lag has fat tails (max 34 s) — long-lag
  moments create stale-strike or stale-settle mispricings.

### Area 6 — Game theory & microstructure
- H26: Overreaction to sharp candles: after a k-sigma 10–30 s Binance move, P(Up) overshoots
  Δfair and reverts; fade as maker.
- H27: Thin-book pushes: when depth-within-5c is small, small flow moves price several cents
  off fair; resting maker orders systematically harvest the reversion.
- H28: Signed taker-flow imbalance intra-window predicts resolution beyond price (informed
  flow exists — follow it rather than fade when book is deep).
- H29: Fee-era natural experiment (15m): after 2026-01-05 the latency-arb bots retreated —
  late-window favorite mispricing widened; quantify pre/post and exploit what they left.

### Area 7 — What the data hands us
- H30: Anything anomalous from the audit: contiguous data gap edges, the 34 s oracle-lag
  tail, settlement trades printing at extreme prices, markets with missing books, duplicate
  timestamps — chase each anomaly for exploitable structure.

## Execution constraints applied to every backtest

- Maker entries: fill only if the quote tape shows trades crossing our resting price
  (conservative: price must trade *through* our limit, not just touch).
- Taker entries: walk the bookcurve cost-to-fill, not top-of-book.
- Latency: signals computed at t use only data with `local_timestamp_us ≤ t`; orders land
  ≥ 250 ms after signal.
- Sizing: $5–10 per trade baseline; capacity measured from bookcurves (max size at ≤1c slippage).
