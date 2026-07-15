# Phase 1 — Data Audit

All data synced from the B2 vault (2,538 objects / 12.9 GB) into `data/data/processed/`,
plus Binance 1s klines backfilled from binance.vision and market metadata from Telonex.

## Inventory & coverage

| Dataset | Path | Range | Files | Gaps |
|---|---|---|---|---|
| Windows table | `windows.parquet` | 2025-10-11 → 2026-05-12 | 1 | none (47,623 markets: 25,909×5m, 20,492×15m, 1,222×4h; **no 1h rows**) |
| 5m trades/quotes/bookcurves | `daily/5m/*` | 2026-02-12 → 2026-07-07 | 92 ea | 2026-05-13 → 2026-07-05 missing (one contiguous block) |
| 15m trades/quotes/bookcurves | `daily/15m/*` | 2025-10-11 → 2026-07-07 | 216 ea | same block missing |
| 4h trades/quotes/bookcurves | `daily/4h/*` | 2025-10-15 → 2026-07-07 | ~206 ea | same block missing |
| 1h trades/quotes | `daily/1h/*` | 2025-10-11 → 2026-07-12 | 275 ea | **complete, no gaps** (no bookcurves though) |
| Chainlink BTC/USD 1s | `daily/crypto_prices/*` | 2026-04-02 → 2026-07-07 | 97 | none |
| Binance BTCUSDT 1s klines | `binance/klines_1s/*` | 2025-10-10 → 2026-01-01 (vault) | 79 | backfilled 2026-01-02 → 2026-07-13 from binance.vision |

Telonex API (key verified, works): has all families through **2026-07-14**; availability endpoint
public; download is per-market per-day. Can fill the May 13–Jul 5 gap and extend past the vault.
Telonex confirms **no `btc-updown-1h` slug family exists** — the hourly family is
`bitcoin-up-or-down-{month}-{day}-{year}-{hour}{am/pm}-et` (10,422 markets, metadata saved to
`data/telonex_btc_markets.parquet`).

## Schemas

- **quotes** (`timestamp_us, local_timestamp_us, bid_price, bid_size, ask_price, ask_size, wts[, slug]`):
  every best-bid/ask change. `wts` = window start (unix s). Prices are for **outcome_0 = "Up"** token.
- **trades** (`timestamp_us, local_timestamp_us, price, size, side, wts[, slug]`): every trade, side = taker side.
- **bookcurves** (~250 ms grid, confirmed median 250 ms): best bid/ask + cost-to-fill
  `buy/sell_avgpx_{50,200,1000,5000}` with `exhaust` flags + `bid/ask_depth_5c` (shares within 5¢ of touch).
- **crypto_prices**: Chainlink BTC/USD at 1 s grid. `timestamp_us` = oracle price time,
  `server_timestamp_us` = publish time, `local_timestamp_us` = capture time.
- **binance klines_1s**: standard OHLCV + taker-buy split, microsecond timestamps.
- **windows.parquet**: per-market metadata: `slug, wts, market_id, asset_id_0/1, result_id, status,
  settled_at_us, duration, open/close_chainlink, outcome_reconstructed, volume_shares, volume_usdc,
  family, date, fee_rate`.

## Verified semantics (all empirically checked)

- `price` in trades/quotes = **P(Up)**. Markets with `result_id=0` (Up wins) converge to ~0.994;
  `result_id=1` to ~0.006.
- **Resolution sources differ by family** (from official market descriptions):
  - 5m / 15m / 4h: **Chainlink BTC/USD data stream**, `close >= open → Up` (ties → Up).
  - 1h: **Binance BTC/USDT 1H candle** (close ≥ open → Up). Our Binance 1s klines are literally
    the resolution feed for the 1h family.
- `open_chainlink` in windows == `crypto_prices` at `wts` (48/50 spot check, rest missing rows).
- **Chainlink publish lag**: price for second T becomes publicly visible at T + ~1.24 s median
  (p5 0.83 s, p95 1.84 s, max 34 s). Local capture adds ~0.13 s.
- **Settlement delay** after window close: median 25 s (5m), 37 s (15m/4h); p95 ≈ 60–115 s.
  Trades keep printing into the settlement window.
- **Pre-open trading is real**: books go live hours before open (median first quote ~11 h before);
  4.1% of all 5m trades are pre-open; median pre-open trade at 0.50; 276/288 markets had live
  books in the final 10 s before open.
- Up win rate overall: 5m 50.4%, 15m 50.1%, 4h 49.9% — the tie rule (`>=`) confers no measurable
  aggregate edge on its own.
- Tick size: $0.01. Sizes are in shares. Volume median per market: ~$55k (5m), ~$46k (15m), ~$7.6k (4h).

## Fees (verified against official docs + windows table)

- Formula: `fee = shares × feeRate × p × (1 − p)`, **charged to takers only, per leg**.
- Makers pay **zero** and earn 20% of collected fees via daily rebates.
- feeRate history in this data: 15m/4h were **fee-free until 2026-01-04**; then 0.0624
  (Jan 5–Mar 29), 0.072 (Mar 30–May 6), 0.07 (May 7 → today). 5m launched 2026-02-12 already at 0.0624.
- Worst case (p=0.50, rate 0.07): 1.75¢/share per taker leg. At p=0.97: 0.20¢.
- Consequence: the user's manual scalp (51¢ maker in, 55¢ maker out) pays **zero fees** — fully
  consistent with their live experience.

## Anomalies / notes to chase

1. Windows table ends 2026-05-12 but daily files continue → reconstruct windows for
   May 13+ from Telonex metadata (`data/telonex_btc_markets.parquet` has result_id + timings for all).
2. 1h family has no bookcurves — quotes only (best bid/ask). Fill simulation for 1h must be
   quote-based.
3. crypto_prices (Apr 2+) and vault Binance klines (to Jan 1) originally had **no overlap**;
   backfill fixes this — oracle-vs-spot lag studies possible Apr 2 → Jul 7.
4. Chainlink max publish lag 34 s — long-lag windows may create mispricings at 5m closes.
5. 15m family has a 3-month fee-free era — great for studying what bots did before fees,
   but results from that era must not be extrapolated to the fee era.
