# M1 — Multi-coin hourly reconnaissance

**Date:** 2026-07-27 (UTC) · **Scope:** enumerate every Polymarket hourly Up/Down family,
verify each one's resolution source from its own metadata, measure liquidity and quote
staleness per coin, and land the historical data needed for the M2 backtest.

---

## 0. Bottom line

**The hourly family runs on exactly SEVEN coins, and all seven resolve on the same rule
as BTC: the Binance `<COIN>/USDT` 1H candle, `close >= open => Up`.**

The proven `close_snipe` edge is therefore portable in principle, and the binding
constraint — frequency — is relieved by **7x in closes/day** (24/day → 168/day).

Three findings change the implementation plan:

1. **HYPE resolves on Binance USD‑M FUTURES, not spot.** Its `resolutionSource` is
   `https://www.binance.com/en/futures/HYPEUSDT` and **there is no HYPE/USDT spot pair on
   Binance at all** (`data-api.binance.vision` returns HTTP 400 for `HYPEUSDT` spot). Wiring
   HYPE to a spot or Hyperliquid-native feed is exactly the class of error that already cost
   this project 3-for-3 losing fills. See §2.
2. **Settle reconciliation is 100.000% for all seven coins over 5,873 hours** (839 per coin,
   35 days) once the candle anchor is taken from *traded* prints. My first pass got 3 hours
   wrong; the cause was a data-representation trap that would have silently corrupted the M2
   backtest. See §2.3 — this is the most important correction in this document.
3. **Liquidity spans a 570x range and quote staleness tracks it almost perfectly.** The
   hypothesis that thinner coins carry staler quotes is **confirmed and quantified**: median
   book age runs 1.0 s on ETH/BTC to 12.7 s on HYPE, while capacity inside the bot's own
   3c slippage bound falls from ~$390 to ~$0.25 per close. See §3.

**Recommended order of expansion: ETH, SOL, XRP first** (real books, real depth, meaningful
staleness), **DOGE and BNB second** (thin but tradeable), **HYPE last and only with a verified
futures feed** (see §6 — its live data path is *not* reachable from this sandbox and is
unverified).

---

## 1. Family enumeration

Method: `gamma-api.polymarket.com/markets?closed=false&limit=100&order=endDate&ascending=true&end_date_min=<now>`,
paginated to gamma's hard offset ceiling (offset+limit must be < 3000 — beyond that gamma
returns **HTTP 422**, which is why a naive `limit=1000` sweep silently truncates), cross-checked
against direct slug construction for 43 candidate coin names over the next two ET hours.

### 1.1 Hourly families — the complete list

Slug pattern is identical for all seven, and **names the hour the candle OPENS, in ET wall
clock** (not the close):

```
<coin>-up-or-down-<month>-<day>-<year>-<hour><am|pm>-et
e.g. ethereum-up-or-down-july-27-2026-5pm-et   opens 21:00Z, closes 22:00Z
```

| coin prefix | display | Binance symbol | venue |
|---|---|---|---|
| `bitcoin` | Bitcoin | BTCUSDT | spot |
| `ethereum` | Ethereum | ETHUSDT | spot |
| `solana` | Solana | SOLUSDT | spot |
| `xrp` | XRP | XRPUSDT | spot |
| `dogecoin` | Dogecoin | DOGEUSDT | spot |
| `bnb` | BNB | BNBUSDT | spot |
| `hype` | HYPE | HYPEUSDT | **USD‑M futures** |

Each coin keeps exactly **4 open hourly markets** at a time (a rolling ~4 h lookahead), created
about 2 days ahead of the close (`createdAt` 2026‑07‑25 for a 2026‑07‑27 close).

### 1.2 Coins that do NOT have an hourly family

Direct slug probes returned nothing for: `hyperliquid` (the prefix is `hype`), `cardano`/`ada`,
`litecoin`/`ltc`, `chainlink`/`link`, `avalanche`/`avax`, `sui`, `toncoin`, `tron`, `pepe`,
`shiba-inu`, `polkadot`, `aptos`, `near`, `stellar`, `monero`, `uniswap`, `aave`, `ondo`,
`worldcoin`, `pump`, `trump`, `gold`, `silver`, `sp500`, `nasdaq`, and the short-form aliases
`btc`/`eth`/`sol`/`doge`. **Seven is the whole universe** as of this date.

### 1.3 The other duration families (context, not this run's target)

The same seven coins also run the short-duration families, under a *different* slug shape
carrying a unix window-start rather than an ET date:

```
<short>-updown-<5m|15m|4h>-<window_start_unix>     e.g. sol-updown-5m-1785186300
short prefixes: btc, eth, sol, xrp, doge, bnb, hype
```

Open-market counts in a 30 h window: 5m ×383, 15m ×130, 4h ×7 (one per coin), 1h ×28 (four per
coin). **These resolve on Chainlink Data Streams, not Binance** — do not mix them with the
hourly family. The multi-coin 5m/15m expansion is a separate, larger opportunity that this
document does not evaluate.

---

## 2. Resolution source — VERIFIED PER FAMILY (safety-critical)

### 2.1 Quoted from each market's own `description`

All seven descriptions are the same template with the pair substituted. Verbatim, from
`gamma-api.polymarket.com/markets?slug=<coin>-up-or-down-july-27-2026-5pm-et`:

> This market will resolve to "Up" if the close price is greater than or equal to the open price
> for the **`<PAIR>` 1 hour candle** that begins on the time and date specified in the title.
> Otherwise, this market will resolve to "Down".
>
> The resolution source for this market is information from Binance, specifically the `<PAIR>`
> pair (`<URL>`). The close « C » and open « O » displayed at the top of the graph for the
> relevant "1H" candle will be used once the data for that candle is finalized.
>
> Please note that this market is about the price according to Binance `<PAIR>`, not according
> to other exchanges or trading pairs.

| coin | `<PAIR>` | `resolutionSource` (verbatim) |
|---|---|---|
| bitcoin | BTC/USDT | `https://www.binance.com/en/trade/BTC_USDT` |
| ethereum | ETH/USDT | `https://www.binance.com/en/trade/ETH_USDT` |
| solana | SOL/USDT | `https://www.binance.com/en/trade/SOL_USDT` |
| xrp | XRP/USDT | `https://www.binance.com/en/trade/XRP_USDT` |
| dogecoin | DOGE/USDT | `https://www.binance.com/en/trade/DOGE_USDT` |
| bnb | BNB/USDT | `https://www.binance.com/en/trade/BNB_USDT` |
| **hype** | **HYPE/USDT** | **`https://www.binance.com/en/futures/HYPEUSDT`** ← futures |

The HYPE description says "the HYPE/USDT pair (https://www.binance.com/en/futures/HYPEUSDT)" —
the pair name reads like spot but the URL is the **perpetual futures** contract. Binance has no
HYPE/USDT spot market (`GET data-api.binance.vision/api/v3/klines?symbol=HYPEUSDT` → **HTTP 400**),
so the futures reading is the only consistent one, and it reconciles at 100% (§2.3).

Confirmed stable over history: the same single `resolution_source` string appears on **all 22,713**
hourly markets in the Telonex catalogue (3,244–3,245 per coin, back to **2026‑03‑15**). No coin
has ever switched feed.

`resolvedBy` is the same UMA adapter for all seven
(`0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7`), `umaBond` 250, `customLiveness` 600 s.

### 2.2 Market mechanics (recorded per request)

Uniform across all seven coins — checked on 672 markets (7 coins × 96 consecutive hours):

| property | value |
|---|---|
| outcomes | `["Up", "Down"]` — **672/672 markets, ordering never varies** |
| `clobTokenIds` | `[<Up token>, <Down token>]`, index-aligned to `outcomes`; **token0 = Up** |
| `orderMinSize` | 5 shares (all coins, all markets) |
| `orderPriceMinTickSize` | **0.01 on a freshly-opened market, 0.001 once live/resolved** |
| `negRisk` | false |
| `takerBaseFee` / `makerBaseFee` | 1000 / 1000 — **identical for all seven**, so the fee model is coin-invariant and the shipped `0.07·p·(1−p)` taker model carries over unchanged |
| `rewardsMinSize`, `rewardsMaxSpread` | present, per-market |

⚠️ **Tick size must be read per market at order time, not assumed.** In the live CLOB books
sampled at one close, `tick_size` was 0.001 for BTC/ETH/SOL/XRP/DOGE but **0.01 for BNB and
HYPE** at the same instant. Gamma and the CLOB can also disagree for the same market. The
authoritative value is the `tick_size` field on the CLOB `/book` response.

Full token IDs for the last 96 h of every coin are in `data/multicoin/gamma_volume.parquet`
(`token0`, `token1`); for the full 22,713-market history see `data/multicoin/markets.parquet`
(`asset_id_0`, `asset_id_1`).

### 2.3 Independent settle reconciliation — 100.000%, and the trap that nearly hid it

Rather than trust the description, the settle was **re-derived from each coin's own Binance feed**
and compared against Telonex's recorded `result_id`, over 35 days (2026‑06‑22 … 2026‑07‑26):

| coin | symbol | hours checked | exact ties | settle agreement | verdict |
|---|---|---|---|---|---|
| bitcoin | BTCUSDT | 839 | 1 | **100.000%** | OK |
| ethereum | ETHUSDT | 839 | 1 | **100.000%** | OK |
| solana | SOLUSDT | 839 | 10 | **100.000%** | OK |
| xrp | XRPUSDT | 839 | 10 | **100.000%** | OK |
| dogecoin | DOGEUSDT | 839 | 7 | **100.000%** | OK |
| bnb | BNBUSDT | 839 | 2 | **100.000%** | OK |
| hype | HYPEUSDT (futures) | 839 | 0 | **100.000%** | OK |

5,873 hours, zero disagreements. This also confirms the documented tie rule empirically:
`close == open` resolves **Up**, and ties are real (31 of 5,873 = 0.53%, concentrated in the
low-priced coins where the tick is coarse relative to hourly volatility).

**The trap.** The first pass reported 99.88% for SOL, XRP and DOGE — one "mismatch" each. All
three looked like ties (`open == close` at tick precision) that Polymarket had resolved *Down*,
which would have contradicted the stated `>=` rule. Fetching the **actual Binance 1H candle** for
those three hours showed Polymarket was right and my reconstruction was wrong:

| market | my reconstructed open | true Binance 1H open | true close | true direction |
|---|---|---|---|---|
| `solana-up-or-down-july-10-2026-12pm-et` | 78.14 | **78.15** | 78.14 | Down ✓ |
| `xrp-up-or-down-july-17-2026-5pm-et` | 1.0885 | **1.0886** | 1.0885 | Down ✓ |
| `dogecoin-up-or-down-july-1-2026-4pm-et` | 0.07283 | **0.07284** | 0.07283 | Down ✓ |

Cause: **Binance's 1s-kline archive back-fills seconds that had no trades**, carrying the
previous close forward at `volume = 0`. Binance's 1H candle open is the first *traded* price in
`[open, close)`. When an hour opens on a silent second, the bar sitting at `open_s` holds the
carried price, not the hour's first print — off by one tick, and on a near-tie that flips the
settle. Filtering to `volume > 0` before taking the anchors moved every coin to exactly 100%.

Consequences, both of which bind on M2:
- **Any multi-coin backtest must take candle anchors from `volume > 0` bars only.** This is
  implemented in `scripts/multicoin/fetch.py::stage_verify` and
  `scripts/multicoin/analyze_quotes.py::load_underlying`.
- The error rate scales with silence, so it hurts the thin coins most — exactly the coins this
  expansion is chasing. HYPE trades in only **63,580 of 86,400 seconds/day (73.6%)**.
- The live bot is **not** exposed to this: `oracle.py` calls Binance's `/klines?interval=1h`
  and reads the exchange's own candle, so it never reconstructs anchors. Only the backtest path
  had the defect.

---

## 3. Liquidity and staleness per coin

### 3.1 Traded volume per market — a 570x spread

From gamma, median `volumeNum` over the last 96 consecutive hourly markets per coin
(`data/multicoin/gamma_volume.parquet`):

| coin | median vol/market | p25 | p75 | total over 96 h |
|---|---|---|---|---|
| bitcoin | **$25,531** | $19,661 | $32,809 | $2,681,451 |
| ethereum | **$5,849** | $4,172 | $8,661 | $710,485 |
| solana | **$1,973** | $1,507 | $2,543 | $245,623 |
| xrp | **$928** | $644 | $1,459 | $134,439 |
| dogecoin | **$382** | $285 | $515 | $41,941 |
| bnb | **$321** | $195 | $483 | $34,134 |
| hype | **$45** | $32 | $68 | $6,466 |

### 3.2 The staleness hypothesis — CONFIRMED

Live CLOB books were polled for all seven coins simultaneously (1 Hz inside the last 150 s), so
every coin is measured by the same clock in the same request and the comparison is exact.

First, a mechanical check that the measurement means anything: **the `timestamp` field on a CLOB
`/books` response is a genuine last-update stamp, not a response stamp.** Across the whole
capture it never once moved while the book `hash` was unchanged (0 exceptions in 2,282 polls).
So "book age" is real, and the fraction of polls at which the hash changes is a direct proxy for
how many bots are competing on that book.

| coin | quote update rate | median book age | p90 book age | p99 book age | median volume/market |
|---|---|---|---|---|---|
| ethereum | **93.2%** | 0.98 s | 1.20 s | 5.4 s | $5,849 |
| bitcoin | **90.7%** | 0.98 s | 1.67 s | 4.5 s | $25,531 |
| xrp | **72.2%** | 1.51 s | 3.34 s | 5.5 s | $928 |
| solana | **66.0%** | 1.61 s | 4.10 s | 8.2 s | $1,973 |
| dogecoin | **59.9%** | 1.62 s | 4.72 s | 7.3 s | $382 |
| bnb | **37.7%** | 2.89 s | 15.45 s | 26.2 s | $321 |
| hype | **19.1%** | **12.66 s** | **82.81 s** | **96.6 s** | $45 |

*(Book age carries a constant clock offset between this box and the CLOB; it is common-mode
across coins and is calibrated out by setting the freshest observed book to 0. Raw and
calibrated values are both printed by `scripts/multicoin/analyze_books.py`.)*

The ladder is **monotone in volume** and spans an order of magnitude in staleness. BNB's quotes
sit unrefreshed for 15 s at p90; HYPE's for 83 s. Against a 1H candle that is an enormous amount
of underlying movement a resting ask has not priced.

### 3.3 The other side of the hypothesis — capacity

Depth the bot could actually lift in one walk (asks from best up to best+0.03, the rule in
`strategy.py`), median over the sampled close, at τ ∈ [2,5] s before the close:

| coin | median capacity within 3c | p25 | p75 | median ask levels | median best-ask notional |
|---|---|---|---|---|---|
| bitcoin | **$387** | $386 | $387 | 12.0 | $27 |
| ethereum | **$310** | $89 | $5,666 | 11.5 | $59 |
| solana | **$259** | $259 | $259 | 8.0 | $24 |
| xrp | **$251** | $251 | $251 | 4.0 | $25 |
| dogecoin | **$22** | $21 | $22 | 4.0 | $3 |
| bnb | **$16** | $15 | $16 | 3.5 | $11 |
| hype | **$0.25** | $0.25 | $0.25 | 2.0 | $0.25 |

**Both sides of the hypothesis hold, and they trade off sharply.** ETH/SOL/XRP carry roughly
BTC-class capacity ($250–390 vs the $250 per-event cap) with meaningfully staler quotes than
BTC — that is the sweet spot. DOGE and BNB are real but hold only ~$16–22 per close, so they
contribute frequency, not size. **HYPE is effectively uninvestable at the close**: 2 ask levels
and $0.25 of capacity, i.e. its 83 s stale quotes cannot be lifted for a meaningful amount.
Its staleness is not an opportunity; it is the *absence of a market*.

### 3.4 Historical top-of-book depth and the frequency screen

From the fetched historical quote tape, snapshotted at the last quote at or before the decision
instant. `frac in band` is the fraction of closes where some side's best ask lay inside the
strategy's `[0.30, 0.99]` price band — **this is the quantity the whole expansion is about.**

<!--LIQ_TABLE-->

### 3.5 Per-coin edge quality: how determined is the outcome at the decision instant?

`close_snipe` is not a directional bet — it buys an ask that is cheap relative to an outcome the
underlying has *already largely decided*. So the ceiling on win rate is simply: at τ seconds
before the close, does `sign(S_t − S_open)` already equal the settle? Measured over the same 839
hours per coin:

| coin | τ=3 s | τ=5 s | τ=10 s | τ=30 s | median hourly \|return\| |
|---|---|---|---|---|---|
| bitcoin | **99.88%** | 99.88% | 99.17% | 98.33% | 18.9 bp |
| ethereum | **99.64%** | 99.28% | 98.57% | 97.97% | 23.0 bp |
| hype | **99.52%** | 99.52% | 99.40% | 97.97% | 39.0 bp |
| xrp | **99.40%** | 98.93% | 98.57% | 98.45% | 23.0 bp |
| dogecoin | **99.40%** | 99.17% | 99.17% | 97.74% | 27.3 bp |
| solana | **99.28%** | 99.17% | 98.81% | 97.85% | 26.4 bp |
| **bnb** | **98.57%** | **96.78%** | 95.95% | 93.80% | 17.9 bp |

**BNB is a clear outlier and should be treated as the riskiest coin, not the mid-tier one its
volume suggests.** At τ=5 s its sign still flips 3.2% of the time — 5x BTC's rate. The cause is
visible in the last column: BNB has the *lowest* hourly volatility (17.9 bp), so its close sits
near the open more often and the sign is decided later. Low volatility is not safety here; it is
precisely what keeps the outcome undecided into the final seconds, and a 3.2% flip rate is large
against a 3c edge threshold on a ~0.9-priced ask.

Ranking by edge quality at τ=3 s: **BTC > ETH > HYPE > XRP ≈ DOGE > SOL >> BNB.**

### 3.6 Cross-coin correlation — the "zero self-competition" premise needs splitting in two

Hourly log returns over the same 839 hours are **highly correlated (Pearson 0.67–0.90)**:

| | BTC | ETH | SOL | XRP | DOGE | BNB | HYPE |
|---|---|---|---|---|---|---|---|
| **BTC** | 1.00 | 0.90 | 0.83 | 0.85 | 0.83 | 0.86 | 0.70 |
| **ETH** | 0.90 | 1.00 | 0.84 | 0.85 | 0.85 | 0.85 | 0.70 |
| **HYPE** | 0.70 | 0.70 | 0.71 | 0.68 | 0.67 | 0.67 | 1.00 |

**All seven coins settle the same direction in 44.9% of hours**; sign agreement with BTC runs
72–84%. The premise "zero self-competition" is **true for order flow and false for risk**:

- ✅ **Order books are genuinely separate.** Seven distinct CLOB books, no queue competition, and
  the batch fetch costs no extra latency (§6, item 3). Frequency really does multiply.
- ❌ **Outcomes are not independent.** Firing on 5 coins in one hour is closer to one 5x-levered
  crypto-beta position than to 5 diversified bets.

The mitigating factor is §3.5: at τ=3 s the outcome is ≥98.6% determined, so most of the
correlated *return* variance is already resolved before entry and does not translate into
correlated PnL. What remains correlated is the **tail**: a sharp market-wide move inside the
final seconds leaves every coin's resting ask stale in the same direction at the same instant,
and all concurrent fills lose together. Sizing must be set against that joint tail, not against
7 independent draws. **Quantifying it is M2's job.**

### 3.7 Frequency screen

The **indicative signals/day** column applies the shipped fair value
`Phi(ln(S_t/S_open) / (sigma_1s·sqrt(tau)))` with `edge_min 0.03`, swept over
τ ∈ {2.5,3,3.5,4,4.5,5} s, counting a close as a signal if any side qualifies at any τ.
It is a **frequency screen, not a backtest** — no fill simulation, no re-fetch latency, no fee.
Its calibration is good: it reproduces BTC at **0.93 signals/day against the 0.9 trades/day
measured live over 10 days**, which is the reason to take the other coins' numbers seriously as
an order-of-magnitude estimate. PnL per trade is M2's job and is **not** established here.

---

## 4. Data landed

All under `data/multicoin/` (nothing written to `data/fresh5m/`, which another workflow owns).

| dataset | path | contents |
|---|---|---|
| market catalogue | `markets.parquet` | 22,713 hourly markets, 7 coins, **2026‑03‑15 → 2026‑07‑29**, with `result_id`, `asset_id_0/1`, `close_s`, `open_s`, `resolution_source`. Every window verified to land on an exact hour boundary (0 exceptions). |
| raw vendor catalogue | `telonex_hourly_markets_raw.parquet` | unfiltered Telonex rows incl. per-channel availability ranges |
| quote tape | `quotes/<coin>/<YYYY-MM-DD>.parquet` | top of book, **both outcomes**, `[close−120 s, close+5 s]`, plus the last update before the cut so staleness stays exact. Columns: `timestamp_us` (book's own last-update instant), `local_timestamp_us` (vendor capture instant), `bid/ask_price`, `bid/ask_size`, `close_s`, `oid`. |
| underlying | `binance/<SYMBOL>/<YYYY-MM-DD>.parquet` | 1 s OHLCV. Spot 1s klines for the six spot pairs; **HYPE rebuilt from USD‑M futures aggTrades** (Binance publishes no 1s klines for futures). 35 days × 7 symbols, complete. |
| live books | `books/books_*.jsonl` + `meta_*.json` | full CLOB book both sides, all 7 coins, 1 Hz near the close, with `book_ts`, `hash`, `tick_size`, `min_order_size`, `rtt` |
| per-market volume | `gamma_volume.parquet` | 672 markets (7 coins × 96 h): volume, liquidity, spread, tick, both token IDs, fee schedule |
| settle audit | `verify_settle.csv` | the §2.3 reconciliation table |
| liquidity summaries | `liquidity_by_coin.csv`, `quote_summary_by_coin.csv` | §3 tables |

**Scripts** (all resumable; every stage skips work whose output already exists):

- `scripts/multicoin/fetch.py` — `markets` / `quotes` / `binance` / `verify` stages
- `scripts/multicoin/fetch_volume.py` — per-market volume via deterministic slug lookup
- `scripts/multicoin/sample_books.py` + `sample_loop.sh` — live CLOB book sampler across closes
- `scripts/multicoin/analyze_books.py` — depth / spread / staleness / update-rate
- `scripts/multicoin/analyze_quotes.py` — historical depth, staleness, band occupancy, frequency screen
- `scripts/multicoin/gamma.py` — shared gamma/CLOB helpers

---

## 5. Summary table

<!--SUMMARY_TABLE-->

---

## 6. Problems, risks and what is NOT yet established

### Blocking for HYPE
1. **No reachable live futures feed from this sandbox.** `fapi.binance.com` and `dapi.binance.com`
   return **HTTP 451** here (same geo-block as `api.binance.com`); `fapi1/fapi2.binance.com`
   redirect to the marketing site. `data-api.binance.vision` is **spot-only** — it has no futures
   path. Historical HYPE was obtained from `data.binance.vision` bulk futures aggTrades, which is
   fine for backtest but is **not a live feed**. Before HYPE can be enabled, `fapi.binance.com`
   must be confirmed reachable *from the production server* and a futures client written
   (different base URL and path prefix from spot).
2. **HYPE is not worth that work yet.** $45 median volume, 2 ask levels, $0.25 of liftable
   capacity at the close. Recommend explicitly deferring it.

### Blocking for the multi-coin expansion generally

3. **The bot's book-fetch path does not scale to 7 coins and will blow the decision window.**
   `engine.py:337-338` fetches the two books with two sequential `GET /book` calls, and
   `polymarket.py:278` is the only book accessor in the codebase. Measured against the live CLOB
   just now, on the 7 markets closing at 22:00Z:

   | method | tokens | median latency |
   |---|---|---|
   | `GET /book` | 1 | 417 ms |
   | `GET /book` sequential | 14 (7 coins × 2) | **5,492 ms** |
   | **`POST /books` batched** | **14** | **344 ms** |

   Seven coins done the current way costs **~5.5 s — longer than the entire τ ∈ [2.5, 5] s
   window**, so the strategy would simply never fill. Batched `POST /books` fetches all 14 books
   in **less time than one single-book GET** (344 ms vs 417 ms), i.e. **the multi-coin expansion
   is latency-free if and only if the bot switches to the batch endpoint.** This is the single
   highest-value implementation change and it must land before any coin is enabled. It also
   lowers the `tau_lo` floor rather than raising it, since `latency/1000 + 0.5` is computed from
   this number.

4. **The bot is hardcoded to BTC in three places** and cannot trade any other coin today:
   - `polymarket.py:21` — `_HOURLY_RE = ^bitcoin-up-or-down-...` matches only BTC.
   - `polymarket.py:110` — `_hourly_slug()` emits a literal `bitcoin-` prefix.
   - `oracle.py:49` — `BinanceOracle(symbol="BTCUSDT")`; the engine holds a single
     `self.binance` and `engine.py:254` routes every `1h` market to it. A second coin would be
     **priced off BTC's underlying** — a silent, catastrophic failure with no error raised.

   The minimum safe change: add a `coin` field to `Market`, build one oracle per symbol, key
   them by coin, and make `_snipe_inputs` fail closed if a market's coin has no oracle.
5. **`strategy.close_snipe.allowed_families` is a family allowlist, not a coin allowlist.**
   Adding coins to the `1h` family silently widens what the existing allowlist permits. A
   **coin allowlist must be added alongside it** before any regex is widened, or the safety
   property the allowlist was introduced to provide is lost.
6. **Per-coin position caps are not designed.** The `$250` `per_event_cap_usd` is BTC-calibrated;
   §3.3 shows DOGE/BNB hold ~$16–22 and HYPE ~$0.25 near the close. A flat cap will walk the
   book to the slippage bound on thin coins.

### Not established by this run
7. **No PnL claim for any non-BTC coin.** §3.4 measures *frequency*, not edge. The live BTC
   result was +21.5c/share; whether that survives on thinner books — where the stale quote may
   be stale precisely because it is about to be adversely selected — is **exactly what M2 must
   test**. The 5m `settle_sweep` autopsy in `bot/config.yaml` is the cautionary precedent: the
   only cheap asks left standing were the ones the market knew were losers.
8. **Concurrency under load is unmeasured.** The seven closes are simultaneous
   (all on the hour), so a multi-coin bot faces up to 7 concurrent decisions in the same 2.5–5 s
   window against one REST budget. Book re-fetch latency under that load is unmeasured and
   directly determines the `tau_lo` floor.
9. **Live book depth rests on few closes.** §3.2/§3.3 come from a small number of simultaneous
   closes; the historical tape in §3.4 is the statistically solid measurement. Treat the live
   numbers as corroboration, not as the primary estimate.

### Operational notes
10. **gamma pagination hard-caps at offset+limit < 3000** (HTTP 422 beyond). Any sweep that
    assumes `limit=1000` works will silently truncate. Resolved markets additionally require
    `closed=true`, or a slug lookup returns an empty list rather than the market.
11. **Telonex `to_date` is exclusive** (confirmed, unchanged).
