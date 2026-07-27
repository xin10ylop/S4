# A1 — Chainlink oracle truth-test + live feasibility

Date: 2026-07-27 · Scope: prove which price source resolves the 5m/15m/4h Polymarket BTC
Up/Down families, and determine whether we can replicate it live.

Every claim below is tagged **[VERIFIED]** (I ran it in this session and pasted the output) or
**[DOCUMENTED]** (I read it; not independently executed).

Scripts that produce every number: `/home/user/S4/scripts/a1/`
(`part1_truthtest.py`, `part1b_full.py`, `part1c_windows_all.py`, `part1d_characterise.py`,
`part1e_lagged_signal.py`, `part2e_onchain.py`, `part2e2_cadence.py`, `part2f_mirrors.py`,
`part2g_gmx_deep.py`, `part2h_polymarket_ws.py`, `part2i_pmws_vs_datastreams.py`).

---

## 0. TL;DR — the answer

1. **The captured `crypto_prices` series IS the resolution source.** `sign(close − open)` computed
   from it reproduces Polymarket's on-chain `result_id` on **35,982 / 35,982** resolved 5m/15m/4h
   markets across 97 days — **100.00000 %, zero disagreements**, including all 254 markets that moved
   less than $0.50 over the whole window. **[VERIFIED]**
   The earlier "0.74 for 5m" figure was a NaN artefact; the naive `NaN >= NaN → False` computation
   reproduces ≈0.986, not 0.74, and dropping NaNs rigorously gives exactly 1.000. **[VERIFIED]**

2. **The feed is Chainlink *Data Streams* (the low-latency off-chain DON report), not the on-chain
   aggregator.** The captured series updates on a 1-second grid and changes value 94.5 % of seconds;
   the on-chain Polygon BTC/USD aggregator updates once per **33.8 s on average**. **[VERIFIED]**

3. **We do not need Chainlink credentials.** Polymarket itself rebroadcasts the exact report on a
   public, unauthenticated WebSocket: `wss://ws-live-data.polymarket.com`, topic
   `crypto_prices_chainlink`, filter `{"symbol":"btc/usd"}`. Its `full_accuracy_value` matched the
   signed Chainlink Data Streams BTC/USD report (feed
   `0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8`) **byte-for-byte at 18
   decimals on 266/266 overlapping observation seconds**. **[VERIFIED]**

4. **Latency is ~1.4 s, and it is a hard floor set by Chainlink, not by us.** Polymarket publishes the
   report for observation-second *T* at *T + 1.37 s* (p50), *T + 2.02 s* (p99). Our network hop added
   only 0.14 s (p50) from inside this sandbox. **[VERIFIED]**

5. **Consequence for the strategy:** the `-2 s signal / -1 s fill` timing frontier is reachable, but at
   the decision instant the freshest oracle print you can hold is ~2 s old. Directional accuracy of
   `sign(S_available − S_open)` for 5m at k=1 s before close is **96.75 %** with the lagged Chainlink
   print, versus **95.96 %** using Binance with no lag — and **97.12 %** if you anchor on the Chainlink
   strike and use Binance to extrapolate across the publish lag. **[VERIFIED]**
   The decisive gain from Chainlink is **not** last-second speed — it is that the *strike* and the
   *settle print* become exact. Binance gets the resolution sign outright wrong on **4.77 %** of 5m
   markets. **[VERIFIED]**

**Recommendation: wire `ChainlinkOracle` to Polymarket RTDS (primary), with GMX's Data Streams mirror
as hot standby and the on-chain Polygon aggregator as a sanity/liveness check only.** Details in §6.

---

## 1. What the markets say they resolve on  [DOCUMENTED — repo data]

`data/telonex_btc_markets.parquet` carries Polymarket's own market metadata. `resolution_source` is
uniform per family:

| family | resolution_source | n markets |
|---|---|---|
| 5m | `https://data.chain.link/streams/btc-usd` | 55,787 |
| 15m | `https://data.chain.link/streams/btc-usd` | 26,764 |
| 4h | `https://data.chain.link/streams/btc-usd` | 1,607 |
| 1h | `https://www.binance.com/en/trade/BTC_USDT` | 10,037 |

5m/15m/4h description verbatim:

> This market will resolve to "Up" if the Bitcoin price at the end of the time range specified in the
> title is greater than or equal to the price at the beginning of that range. Otherwise, it will
> resolve to "Down".
> The resolution source for this market is information from Chainlink, specifically the BTC/USD data
> stream available at https://data.chain.link/streams/btc-usd.
> Please note that this market is about the price according to Chainlink data stream BTC/USD, not
> according to other sources or spot markets.

Note the wording: **"data stream"**, not "price feed". That distinction is the whole answer — Data
Streams ≠ the on-chain Data Feeds aggregator.

---

## 2. PART 1 — Proving it with the repo's own data

Captured series: `data/data/processed/daily/crypto_prices/*.parquet`, 97 files,
2026-04-02 → 2026-07-07. **8,123,039 unique observation seconds out of 8,380,800 in span = 96.92 %
coverage**; 42 duplicate `timestamp_us` rows (deduped, last wins). **[VERIFIED]**

### (a) Does the captured series equal `windows.parquet`'s `open_chainlink` / `close_chainlink`?

Yes — **exactly, to the last float bit**. **[VERIFIED]**

| family | windows w/ both cols | both boundary secs present in capture | open exact match | close exact match | max abs diff |
|---|---|---|---|---|---|
| 5m | 11,791 | 97.17 % | **100.000 %** | **100.000 %** | 0.0000 |
| 15m | 3,932 | 97.20 % | **100.000 %** | **100.000 %** | 0.0000 |
| 4h | 246 | 98.37 % | **100.000 %** | **100.000 %** | 0.0000 |
| ALL | 15,969 | 97.20 % | **100.000 %** | **100.000 %** | 0.0000 |

Mismatch distribution: the diff array is identically zero — mean, std, p99.9 and max are all `0.0`.
**Zero rows differ by more than $0.01.** Continuity also holds: for contiguous windows,
`open_chainlink == previous close_chainlink` in **100.00 %** of 3,930 (15m) / 245 (4h) / 11,785 (5m)
pairs. **[VERIFIED]**

**Honesty caveat — this test is circular.** I checked whether `windows.parquet`'s chainlink columns
were themselves *derived* from this capture. On the 225 rows whose open second is missing from the
capture, `open_chainlink` equals the **backfill** (next available second) of the capture in
**100.00 %** of cases (and the forward-fill in only 0.89 %). Same for the 224 close-side rows.
So `open_chainlink`/`close_chainlink` are a reconstruction from this very series, and (a) proves only
internal consistency. **The decisive test is (b), which uses `result_id` — Polymarket's actual on-chain
resolution — as ground truth.** **[VERIFIED]**

### (b) Does `sign(close − open)` from the captured series reproduce `result_id`?

**Yes: 100.00000 %, zero disagreements, on 35,982 resolved markets over 97 days.** **[VERIFIED]**

Using `data/windows_all.parquet` (independent `result_id` for every market), restricted to
2026-04-02 → 2026-07-07 and to rows where the capture has a price at **both** boundary seconds
(NaNs dropped explicitly):

| family | resolved in span | complete rows | agreement | disagreements |
|---|---|---|---|---|
| 5m | 27,924 | 26,582 (95.19 %) | **100.00000 %** | **0** |
| 15m | 9,310 | 8,846 (95.02 %) | **100.00000 %** | **0** |
| 4h | 581 | 554 (95.35 %) | **100.00000 %** | **0** |
| **ALL** | **37,815** | **35,982 (95.15 %)** | **100.00000 %** | **0** |

Robustness — agreement bucketed by total window move (this is where a merely-correlated feed would
break down):

| \|close − open\| | n | agreement |
|---|---|---|
| $0.00 – $0.50 | 254 | **100 %** |
| $0.50 – $1.00 | 245 | **100 %** |
| $1.00 – $2.00 | 549 | **100 %** |
| $2.00 – $5.00 | 1,617 | **100 %** |
| $5 – $10 | 2,648 | **100 %** |
| $10 – $25 | 6,486 | **100 %** |
| > $25 | 24,183 | **100 %** |

There were **zero exact ties** (`close == open`) in 35,982 markets, so the `>=` tie rule is untested
by this data but also irrelevant in practice (the feed carries 18 decimals).

**Where the 0.74 came from.** The naive computation that leaves NaNs in place —
`np.where(cp_close >= cp_open, 0, 1)`, where `NaN >= NaN` evaluates `False` and therefore silently
predicts "Down" — gives **98.63 %** overall (5m 98.61 %, 15m 98.65 %, 4h 99.19 %), not 0.74.
**[VERIFIED]** So the earlier 0.74 was a different bug (most likely a mis-join or a wrong
`result_id` polarity), not NaN handling. Either way it is dead: the correct number is 1.000.

**Bonus finding — 11 rows where `windows.parquet`'s own chainlink columns disagree with `result_id`.**
All 11 are rows where the capture is missing exactly one boundary second, i.e. the reconstruction's
backfill got a near-tie wrong. The captured series is right wherever it has data; the *derived
columns* are the weaker artefact. **[VERIFIED]**

### (b2) The exact resolution rule, recovered

Polymarket's rule is: **strike = the first Data Streams report with `observationsTimestamp >=
window_start`; settle = the first report with `observationsTimestamp >= window_end`; Up wins iff
settle >= strike.**

Evidence: on the 966 markets where our capture missed a boundary second by exactly 1 s, filling with
**backfill (next available second) agrees with `result_id` 99.59 %** of the time, versus 98.03 % for
forward-fill and 99.17 % for nearest. **[VERIFIED]**

| gap size | ffill | bfill | nearest | n |
|---|---|---|---|---|
| ≤1 s | 98.03 % | **99.59 %** | 99.17 % | 966 |
| ≤2 s | 97.80 % | **99.45 %** | 99.27 % | 1,090 |
| ≤3 s | 97.52 % | **99.06 %** | 98.80 % | 1,171 |
| ≤5 s | 97.54 % | **98.61 %** | 98.61 % | 1,219 |

Use **backfill / "first print at-or-after the boundary"** in the bot and in any future backtest.

### (c) Characterising the series — streaming feed, not an on-chain aggregator

All **[VERIFIED]** (`part1d_characterise.py`):

- **Timestamp grid**: `timestamp_us % 1e6 == 0` for **100.0000 %** of 8.12 M rows. The oracle stamps
  land on exact whole seconds.
- **Price granularity**: **95.52 %** of prints carry more than 2 decimals — sub-cent resolution.
- **Does it change every second?** Consecutive captured rows have an identical price only **4.66 %**
  of the time; **94.39 %** of all 8.12 M prices are distinct values.
- **Inter-change interval**: p50 = p75 = p90 = **1 s**; **94.500 %** of changes are exactly 1 s apart,
  99.484 % within 2 s. Only 0.013 % of intervals are ≥10 s, and 89 events ≥60 s (these coincide with
  capture outages, not feed outages — see below).
- **Per-second move**: median $0.340, mean $1.391, p90 $3.81, p99 $13.60, max $265.02.
  Median 0.048 bp.
- **Publish lag** (`server_timestamp_us − timestamp_us`): p5 0.793 s, **p50 1.119 s**, p90 1.531 s,
  p99 2.017 s. Never negative. (Mean 4.467 s is dragged by 89 outage tails; ignore the mean.)
- **Capture hop** (`local − server`): p50 0.161 s, p90 0.424 s.
- **Total oracle-time → our machine**: p50 **1.322 s**, p90 1.822 s, p99 3.098 s.
- **Gaps**: 257,761 missing seconds (3.076 %) in 95,271 runs. **85.74 % of gap runs are a single
  second**; p99 = 8 s; 27 runs exceed 600 s (real capture outages).

**Conclusion of (c): this is a streaming, sub-second-computed feed published on a 1 s grid with a
~1.1 s publish lag. It is unambiguously Chainlink Data Streams and unambiguously *not* the on-chain
aggregator** (which I measure at 33.8 s in §5).

**The ~3 % single-second gaps are upstream, not transport loss.** In a live concurrent capture I ran
Polymarket RTDS and the independent GMX/Data-Streams path side by side for 300 s: RTDS had 289/299
seconds, GMX had 266/299, and the **union was still 289** — GMX supplied **zero** of the 10 seconds
RTDS lacked. If the two paths failed independently at their observed rates, the chance of that is
~1e-10. The DON simply does not emit a report every single second. **[VERIFIED]**

### (d, extra) Why Binance is not a substitute — quantified

Same 35,982 markets, same test, but computing the sign from Binance BTC/USDT 1 s klines at the two
boundary seconds: **[VERIFIED]**

| family | Chainlink agreement | Binance agreement | Binance error rate |
|---|---|---|---|
| 5m | 100.0000 % | 95.2299 % | **4.77 %** |
| 15m | 100.0000 % | 97.1513 % | 2.85 % |
| 4h | 100.0000 % | 98.3755 % | 1.62 % |
| ALL | 100.0000 % | 95.7507 % | 4.25 % |

Level basis over 1.70 M overlapping seconds: Chainlink − Binance median **+$12.15**, **std $17.48**.
Chainlink moves every second (median \|Δ\| $0.397) while Binance 1 s klines are mostly flat
(median \|Δ\| $0.010, median 3 trades/s) but have comparable total motion (mean \|Δ\| $1.35 vs $1.39).
Peak cross-correlation of 1 s increments is at **lag −2 s** (r = 0.752): the Chainlink print stamped
*T* embeds Binance's move from ~*T−2*. **[VERIFIED]**

This is exactly the basis risk `docs/04_executability_audit.md` warned about, now with a number:
**a Binance-signalled 5m close-snipe is wrong about the resolution 1 time in 21.**

### (e, extra) Signal quality once you account for the publish lag

At wall-clock decision time `close − k`, the freshest oracle print you can *possibly* hold is stamped
`close − k − L`, with L ≈ 2 s (p50 1.37 s live, p99 2.02 s → round up). Accuracy of
`sign(S_available − S_open) == outcome`: **[VERIFIED]**

| k (s before close) | 5m | 15m | 4h |
|---|---|---|---|
| 1 | 96.75 % | 98.62 % | 99.64 % |
| 2 | 95.76 % | 98.11 % | 99.46 % |
| 3 | 94.77 % | 97.74 % | 99.28 % |
| 5 | 93.52 % | 97.22 % | 99.28 % |
| 10 | 91.45 % | 96.42 % | 99.10 % |
| 30 | 87.60 % | 94.39 % | 98.53 % |

Variant comparison for 5m (all use the **Chainlink** strike where stated): **[VERIFIED]**

| variant | k=1 | k=2 | k=3 | k=5 | k=10 |
|---|---|---|---|---|---|
| CL strike + CL print (lagged 2 s) | 96.75 % | 95.76 % | 94.77 % | 93.52 % | 91.45 % |
| CL strike + Binance level (fresh) | 95.95 % | 96.05 % | 95.75 % | 94.28 % | 91.34 % |
| **CL strike + CL lagged print + Binance drift over the lag** | **97.12 %** | **96.92 %** | **96.51 %** | **95.22 %** | **92.28 %** |
| Binance strike + Binance level | 95.96 % | 96.06 % | 95.76 % | 94.32 % | 91.39 % |

Two honest readings:
- For *direction at the last second*, the lagged Chainlink print is only ~0.8 pp better than fresh
  Binance. The big win is the **hybrid**: Chainlink for the strike and the anchor, Binance for the
  1–2 s of drift the Chainlink print hasn't published yet. Recommended.
- The *category* win is elsewhere: `settle_sweep` and post-close winner determination become exact
  instead of 95 % right, and `S_open` (the strike itself) stops carrying a $17-std basis error.

Conditional accuracy at k=2 for 5m, by distance from strike (this is the population a snipe actually
trades): **[VERIFIED]**

| \|S_avail − S_open\| | n | accuracy | cumulative share of markets |
|---|---|---|---|
| $0–2 | 941 | 68.97 % | 100 % |
| $2–5 | 1,444 | 82.76 % | 96.5 % |
| $5–10 | 2,240 | 88.35 % | 91.0 % |
| $10–20 | 3,726 | 94.74 % | 82.6 % |
| $20–40 | 5,566 | 98.08 % | 68.6 % |
| $40–80 | 6,746 | 99.67 % | 47.7 % |
| > $80 | 5,922 | **100.00 %** | 22.3 % |

22 % of 5m markets are ≥$80 from the strike 2 s before close with a *perfect* record over 5,922
observations. That is the shape of the "+14 c/share at ~52 trades/day" prize.

---

## 3. PART 2(d) — Chainlink Data Streams, direct

**What it is** **[DOCUMENTED]** — Chainlink's low-latency product. A Decentralised Oracle Network
computes the price off-chain and signs a **report**; consumers pull the report over REST/WebSocket
and (optionally) verify it on-chain. Unlike Data Feeds, there is no on-chain write per update, so the
cadence is sub-second rather than heartbeat-driven.

**Feed ID (mainnet BTC/USD, Premium/CexPrice, v3 schema)** — **[VERIFIED]**, extracted from
`docs.chain.link/data-streams/crypto-streams` and independently confirmed by decoding a live signed
report (§5, GMX):

```
0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8
  path=btc-usd-premium-prod  status=live  decimals=18  schema=Premium
  attributeType=CexPrice  sourceChain=42161 (Arbitrum)
  clicProductName=BTC/USD-RefPrice-DS-Premium-Global-003
```
Testnet equivalent: `0x00037da06d56d083fe599397a4769a042d63aa73dc4ef57709d31e9971a5b439`
(`btc-usd-ref-price-plus-sepolia-prod-v03`, Arbitrum Sepolia).

**Endpoints** **[DOCUMENTED]**:

| | mainnet | testnet |
|---|---|---|
| REST | `https://api.dataengine.chain.link` | `https://api.testnet-dataengine.chain.link` |
| WS | `wss://ws.dataengine.chain.link` | `wss://ws.testnet-dataengine.chain.link` |

REST paths: `GET /api/v1/reports?feedID=&timestamp=` (report **at** a timestamp — this is the exact
call that reconstructs a strike/settle), `/api/v1/reports/latest?feedID=`,
`/api/v1/reports/bulk?feedIDs=&timestamp=`, `/api/v1/reports/page?feedID=&startTimestamp=&limit=`.
WS path: `/api/v1/ws?feedIDs=<comma-separated>`.

**Report v3 payload** (confirmed by decoding a live report, §5): `feedId (bytes32)`,
`validFromTimestamp (uint32)`, `observationsTimestamp (uint32)`, `nativeFee`, `linkFee`,
`expiresAt`, `price (int192, 18 dp)`, `bid (int192)`, `ask (int192)`. **[VERIFIED]**

**Access requires credentials — verified, not assumed.** Unauthenticated calls are rejected:
**[VERIFIED]**

```
$ curl "https://api.dataengine.chain.link/api/v1/reports/latest?feedID=0x00039d9e...ed75b8"
HTTP 400
{"error":"Key: 'Headers.UserId' Error:Field validation for 'UserId' failed on the 'required' tag
         Key: 'Headers.Timestamp' ... Key: 'Headers.HmacSignature' ..."}
```
Auth is HMAC-SHA256 over the request with a UUID `Authorization` header, an
`X-Authorization-Timestamp` (ms, ±5 s tolerance) and `X-Authorization-Signature-SHA256`.
**[DOCUMENTED]**

**Cost / eligibility, honestly** **[DOCUMENTED]**: Data Streams is gated. Chainlink's docs say you must
"contact us" for mainnet access; billing is **subscription-based** (pay-per-verification is
deprecated) and **no public price list exists**. Chainlink publicly solicits "algo traders & market
makers" for direct API access with onboarding support, so we are the intended customer profile — but
expect a sales conversation, a contract, and an unknown (likely four-to-five-figure annual) fee.
There is no self-serve signup.

**Verdict on (d): technically ideal, commercially gated, and — given §5 — unnecessary.**

---

## 4. PART 2(e) — The on-chain aggregator. I actually called it.

`pip install web3` → **web3 7.16.0**. **[VERIFIED]**

### Live read, Polygon (2026-07-27 ~11:00 UTC)

```
RPC   https://polygon-bor-rpc.publicnode.com     (https://polygon-rpc.com refused; publicnode worked)
proxy 0xc907e116054Ad103354f2D350FD2514433D57F6f
description "BTC / USD"   decimals 8   version 6
inner aggregator 0x014497a2AEF847C7021b17BFF70A68221D22AA63
block 90963306
latestRoundData():
  roundId          55340232221132376148
  answer           6527799000000  ->  $65,277.99000000
  startedAt        1785149949
  updatedAt        1785149953
  answeredInRound  55340232221132376148
  age of answer    3.3 s
```
**[VERIFIED]**

### Live read, Ethereum mainnet

```
RPC   https://ethereum-rpc.publicnode.com        (https://eth.llamarpc.com refused)
proxy 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c
description "BTC / USD"   decimals 8   version 6
inner aggregator 0x4a3411ac2948B33c69666B35cc6d055B27Ea84f1
answer 6519367465659 -> $65,193.67465659   updatedAt 1785146483
age of answer  3,475.5 s  (58 minutes stale)
```
**[VERIFIED]**

### Against Binance at the same instant

Binance BTCUSDT `$65,333.34` (data-api.binance.vision). **[VERIFIED]**

| source | price | diff vs Binance | answer age |
|---|---|---|---|
| Polygon aggregator | $65,277.99 | −$55.35 (−8.5 bp) | 3 s |
| Ethereum aggregator | $65,193.67 | −$139.67 (−21.4 bp) | 3,475 s |

### Cadence — measured, not assumed

Walked back **220 consecutive rounds** on the Polygon proxy via `getRoundData()`, spanning 2.06 h,
plus a 60 s live poll: **[VERIFIED]**

| metric | value |
|---|---|
| interval p0 / p10 / p50 / p90 / p99 / p100 | 21 s / 33 s / **33 s** / 35 s / 44 s / 45 s |
| mean interval | **33.8 s** (≈106.5 updates/hour) |
| intervals ≤ 2 s | **0.00 %** |
| \|Δanswer\| between rounds | median $5.38, mean $7.82, max $58.31 |
| \|Δ%\| between rounds | median 0.0082 %, p90 0.0303 %, max 0.0894 % |
| answers with sub-cent digits | 74.55 % |
| rounds observed in a 60 s live poll | 2 |
| `eth_call` RTT over a free public RPC | median **211 ms**, p90 239 ms, max 361 ms |

### Does it match the captured series' behaviour?

**No — and this is the point.**

| property | captured `crypto_prices` | Polygon on-chain aggregator |
|---|---|---|
| update interval | 1 s (94.5 % of changes) | **33.8 s mean, never < 21 s** |
| timestamp grid | exact whole seconds | irregular block timestamps |
| repeats between updates | 4.66 % | would be ~97 % if resampled to 1 s |
| decimals | 18 (sub-cent, 95.5 % of prints) | 8, sub-cent 74.6 % |
| answer age when polled | ~1.1 s | 3–35 s |

The on-chain aggregator is a **downstream, throttled write of the same DON price**. Sub-cent
precision confirms shared lineage; 33 s cadence confirms it cannot be the 1 s series and cannot be
the resolution source. A 5m market is 300 s long — resolving it off a 33 s feed would mean the
"close" print is on average 17 s stale, which would have produced visible disagreements in §2(b).
There were none. **[VERIFIED]**

**Verdict on (e): usable only as a slow, free liveness/sanity check. Never as a signal.**

---

## 5. PART 2(f) — Other real-time sources. Two work, both free.

### 5.1 ★ Polymarket RTDS — the winner

`wss://ws-live-data.polymarket.com`, topic **`crypto_prices_chainlink`**, filter
`{"symbol":"btc/usd"}`. Public, **no authentication**. Officially documented by Polymarket at
`docs.polymarket.com/market-data/websocket/rtds`. **[DOCUMENTED]**

Subscribe frame **[VERIFIED — this exact frame worked]**:
```json
{"action":"subscribe","subscriptions":[
  {"topic":"crypto_prices_chainlink","type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}
```
Chainlink symbols: `btc/usd`, `eth/usd`, `sol/usd`, `xrp/usd`. A sibling topic `crypto_prices` carries
Binance (`btcusdt`, …) — that is the 1h family's source, so **one socket can feed all four families**.
Docs say send the text frame `PING` every 5 s. **[DOCUMENTED]**

Live message **[VERIFIED]**:
```json
{"connection_id":"gVXk0y-M8WeIKEhvdA==",
 "payload":{"full_accuracy_value":"65245091651786417500000",
            "symbol":"btc/usd","timestamp":1785150685000,"value":65245.09165178642},
 "timestamp":1785150686566,"topic":"crypto_prices_chainlink","type":"update"}
```

**This maps 1:1 onto the repo's captured schema** — and that is the final piece of the provenance
chain:

| capture column | RTDS field |
|---|---|
| `timestamp_us` | `payload.timestamp` (ms → whole seconds) |
| `server_timestamp_us` | outer `timestamp` (ms, server publish time) |
| `local_timestamp_us` | local receipt time |
| `price` | `payload.value` ( = `full_accuracy_value` / 1e18 ) |

Measured over a **300 s live capture** **[VERIFIED]**:

| metric | live RTDS (2026-07-27) | historical capture (Apr–Jul) |
|---|---|---|
| per-second coverage | **96.67 %** | 96.92 % |
| inter-update == 1 s | 96.54 % (100 % ≤ 2 s) | 94.50 % (99.48 % ≤ 2 s) |
| consecutive identical prices | 4.15 % | 4.66 % |
| sub-cent prices | 97.24 % | 95.52 % |
| median \|Δ\| per second | $0.130 | $0.340 (quieter tape today) |
| publish lag p50 | **1.373 s** | 1.119 s |
| publish lag p99 | **2.016 s** | 2.017 s |
| network hop to us p50 | 0.137 s | 0.161 s |
| end-to-end p50 / p90 | **1.527 s** / 1.927 s | 1.322 s / 1.822 s |

`full_accuracy_value / 1e18 == value` on **290/290** messages — the WS carries the raw 18-decimal
Data Streams answer, so comparisons can be done as **exact integers with no float ties**.

**On connect, RTDS replays a snapshot**: 51 rows covering 57 s of history. Useful for warm start and
for filling a short reconnect gap — but **not enough to recover a 5m strike** (300 s back), so the bot
must keep its own rolling buffer.

**No REST backfill found.** `gamma-api.polymarket.com/prices/crypto` → 404;
`live-data.polymarket.com/...` → Cloudflare error page; `ws-live-data.polymarket.com` over HTTP →
426 Upgrade Required. **[VERIFIED]** Treat RTDS as stream-only.

### 5.2 ★ GMX oracle keeper — a free public mirror of the signed Data Streams report

GMX V2 consumes Chainlink Data Streams and republishes the **signed reports** on a public,
unauthenticated API. **[VERIFIED]**

```
https://arbitrum-api.gmxinfra.io/signed_prices/latest    (backup: arbitrum-api.gmxinfra2.io)
https://arbitrum-api.gmxinfra.io/prices/tickers
https://arbitrum-api.gmxinfra.io/prices/candles?tokenSymbol=BTC&period=1m|5m&limit=5000
```

Decoding the BTC entry's `blob` (outer `abi.decode(bytes32[3],bytes,bytes32[],bytes32[],bytes32)`,
then the v3 report schema) yields: **[VERIFIED]**
```
configDigest 0x00094baebfda9b87680d8e59aa20a3e565126640ee7caeab3cd965e5568b17ee   signers=6
feedId       0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8   <-- BTC/USD mainnet
validFrom=1785150321  observations=1785150322  (age 1 s)
price 65,229.19721323   bid 65,229.16451986   ask 65,230.74313109
```

Characteristics over a 120 s poll at 0.35 s: whole-second `observationsTimestamp` 100 %; sub-cent
prices 100 %; **end-to-end latency p50 2.08 s** (p5 1.61 s, p90 2.70 s) — ~0.5 s worse than RTDS
because of the extra keeper hop. `prices/candles` gives OHLC from the same feed back ~3.5 days (1m)
and ~17 days (5m) — a small but real backfill/audit source.

### 5.3 ⭐ THE DECISIVE CROSS-CHECK

I captured Polymarket RTDS and the GMX-relayed **signed Chainlink report** concurrently for 300 s and
compared them at matching observation seconds: **[VERIFIED]**

```
observation seconds present in BOTH: 266
EXACT integer match of payload.full_accuracy_value vs Data Streams report price: 266/266 = 100.00%
|difference|: max $0.0000000000   median $0.0000000000

  sec=1785150765  pm_full=65211802547675000000000  ds_raw=65211802547675000000000
  sec=1785150766  pm_full=65211804894000000000000  ds_raw=65211804894000000000000
  sec=1785150768  pm_full=65212142000000000000000  ds_raw=65212142000000000000000
  sec=1785150770  pm_full=65212031154431880000000  ds_raw=65212031154431880000000
  sec=1785150771  pm_full=65212110133034600000000  ds_raw=65212110133034600000000

latency to us: polymarket-ws p50 = 1.529 s   vs   gmx-poll p50 = 1.957 s
```

**Polymarket's public WebSocket carries the Chainlink Data Streams BTC/USD report verbatim, to all 18
decimals, with lower latency than the GMX mirror and no credentials.**

Chain of proof, closed:
`market description cites data.chain.link/streams/btc-usd` →
`GMX blob decodes to feedId 0x00039d9e… = the live mainnet BTC/USD Data Stream` →
`Polymarket RTDS value == that report's price, 266/266 exact` →
`RTDS schema == the repo's crypto_prices schema, same cadence/lag/coverage` →
`crypto_prices sign(close−open) == result_id, 35,982/35,982`.

### 5.4 Rejected alternatives

| source | status | why not |
|---|---|---|
| **Pyth Hermes** (`hermes.pyth.network`, BTC/USD `0xe62df6c8…`) | **[VERIFIED]** reachable, free, no auth, ~400 ms cadence, age 2 s when polled; price $65,223.20 ± $17.63 conf | A *different oracle*. Same class of basis risk as Binance. Only useful as a third-party liveness cross-check. |
| **Chainlink on-chain aggregator** (Polygon/Ethereum) | **[VERIFIED]** working, free | 33.8 s cadence. Sanity check only. |
| **Chainlink SVR / other on-chain variants** | **[DOCUMENTED]** | Same on-chain write cadence problem. |
| **Chainlink Data Streams direct** | **[VERIFIED]** endpoint alive, rejects unauthenticated | Needs a commercial contract; provides nothing RTDS doesn't. |
| **Binance as a stand-in** | **[VERIFIED]** | Wrong resolution sign on 4.77 % of 5m markets. Already burned this project once (3/3 settle_sweep fills lost, `config.yaml` comment 2026-07-15). |
| **Tardis/Kaiko/Amberdata-style vendors** | not pursued | Would cost money to replicate a stream we can get for free and would still be a mirror of the same RTDS/Data Streams source. |

---

## 6. RANKED RECOMMENDATION

### #1 — Polymarket RTDS WebSocket (PRIMARY). Do this.

- **Endpoint** `wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`,
  filter `{"symbol":"btc/usd"}`. No auth, no key, no contract.
- **Expected latency**: report for second *T* is in our process at **T + ~1.4 s (p50), T + ~2.0 s
  (p99)** on a well-placed server. (Measured 1.53 s p50 from inside this sandbox *through an HTTPS
  proxy*; the proxy hop was 0.14 s, so a colocated server should see ~1.4 s.)
- **Fidelity**: bit-exact to the resolution source. Compare `int(full_accuracy_value)` — never floats.
- **Coverage**: ~96.7 % of seconds; the missing ~3 % are upstream and unavoidable.
- **Why it beats everything else**: it is *the same server that resolves the market*. Even if
  Chainlink changed feeds tomorrow, this endpoint would follow.
- **Risks to hold in mind**: undocumented rate/connection limits; Polymarket could gate it; it is a
  single point of failure. Mitigate with #2 as hot standby and a hard staleness guard.

### #2 — GMX oracle keeper (HOT STANDBY)

- `https://arbitrum-api.gmxinfra.io/signed_prices/latest` (+ `gmxinfra2.io`), poll every 0.3–0.5 s,
  decode the blob, assert `feedId == 0x00039d9e…`.
- **Expected latency** ~2.0 s p50 — ~0.5 s worse, but it is a *cryptographically signed* copy of the
  same report, from a completely independent operator. Perfect failover and perfect audit trail.
- Use it to (a) fill in if RTDS drops, (b) continuously verify RTDS hasn't drifted.

### #3 — On-chain Polygon aggregator (LIVENESS / SANITY ONLY)

- `0xc907e116054Ad103354f2D350FD2514433D57F6f` on `https://polygon-bor-rpc.publicnode.com`,
  `latestRoundData()`, 211 ms RTT.
- Poll once a minute. Alert if it diverges from the streamed price by more than, say, 0.3 % for
  more than 2 minutes — that means our stream is stale/wrong.
- **Never** use it for a signal or for settlement. 33.8 s cadence.

### #4 — Chainlink Data Streams direct (ONLY IF #1 IS WITHDRAWN)

- Start the commercial conversation only if Polymarket closes RTDS. Then use
  `wss://ws.dataengine.chain.link/api/v1/ws?feedIDs=0x00039d9e…` with HMAC auth, and
  `GET /api/v1/reports?feedID=&timestamp=` for exact strike/settle reconstruction — the one genuine
  capability RTDS lacks.

### Strategy-side changes this unlocks

1. **Set `oracle: chainlink` live and flip `close_snipe: true` for 5m/15m/4h.** The gate in
   `engine.py:_maybe_snipe` (`if market.family != "1h": return`) must be replaced by an
   *oracle-available* check, not a family check.
2. **Use the hybrid signal**: `S_open` = Chainlink strike (exact); `S_t` = last Chainlink print
   **plus** the Binance move since that print's second. Measured +0.4 pp accuracy at k=1 and
   +1.7 pp at k=3 over pure-lagged-Chainlink. Both feeds already come off the same RTDS socket.
3. **Backfill rule everywhere**: strike/settle = *first print with `observationsTimestamp >=
   boundary`*. 99.59 % correct on 1-second gaps vs 98.03 % for forward-fill.
4. **Staleness guard**: refuse to trade if the newest print is older than ~4 s (p99 publish lag
   2.0 s + margin). The existing 5 s Binance guard is roughly right; make it explicit and per-oracle.
5. **`settle_sweep` for short families can be reconsidered** — it was killed because the Binance
   winner-check was adversely selected. With an exact Chainlink settle print the adverse selection
   argument changes completely. Re-test before re-enabling; do not just flip the flag.
6. **`sigma_1s` should be recomputed from the Chainlink series, not Binance.** Chainlink's 1 s
   log-return std is structurally different (it moves every second; Binance klines are 76 % flat), so
   the existing `sigma_1s_floor: 8e-6` will need re-calibration.

---

## 7. Code sketch — `bot/polybot/oracle.py::ChainlinkOracle`

Drop-in replacement for the current `NotImplementedError` stub. Keeps the same shape as
`BinanceOracle` (`latest()`, `price_at_or_before()`, `rolling_log_return_std()`) so `engine.py` and
`strategy.py` need no interface change, and adds `price_at_or_after()` for the strike/settle rule.

```python
class ChainlinkOracle:
    """Chainlink BTC/USD Data Streams — the actual resolution source for 5m/15m/4h.

    Transport: Polymarket's public RTDS websocket, which rebroadcasts the signed
    Chainlink Data Streams BTC/USD report (feedId 0x00039d9e4539...ed75b8) verbatim.
    Verified 2026-07-27: payload.full_accuracy_value matched the signed report's
    18-decimal price on 266/266 overlapping observation seconds (audit/A1_chainlink.md).

    Semantics (verified on 35,982 resolved markets, 100.00000% agreement with result_id):
      strike  = first report with observationsTimestamp >= window_start
      settle  = first report with observationsTimestamp >= window_end
      Up wins iff settle >= strike        (compare the 18-dec INTEGERS, never floats)

    Publish lag: p50 1.37 s, p99 2.02 s from observation second to our process.
    Coverage: ~96.7 % of seconds; the ~3 % single-second gaps are upstream (the DON
    does not emit every second) and are NOT recoverable from a second connection.
    """

    WS_URL   = "wss://ws-live-data.polymarket.com"
    TOPIC    = "crypto_prices_chainlink"
    SYMBOL   = "btc/usd"
    FEED_ID  = "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
    GMX_URL  = "https://arbitrum-api.gmxinfra.io/signed_prices/latest"

    def __init__(self, config, maxlen: int = 20_000):   # 20k s ≈ 5.5 h > one 4h window
        self._cfg = config
        self._px: "collections.OrderedDict[int, int]" = collections.OrderedDict()  # sec -> wei price
        self._lock = threading.Lock()
        self._maxlen = maxlen
        self._last_rx = 0.0
        self._stop = threading.Event()
        self._threads = []

    # ---------------------------------------------------------------- ingest
    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._ws_loop,  daemon=True, name="chainlink-ws"),
            threading.Thread(target=self._gmx_loop, daemon=True, name="chainlink-gmx"),
        ]
        for t in self._threads:
            t.start()

    def _put(self, sec: int, wei: int, source: str) -> None:
        with self._lock:
            prev = self._px.get(sec)
            if prev is not None:
                if prev != wei:                      # must never happen; loud if it does
                    log.error("chainlink DISAGREEMENT sec=%s existing=%s %s=%s", sec, prev, source, wei)
                return
            self._px[sec] = wei
            self._last_rx = time.time()
            while len(self._px) > self._maxlen:
                self._px.popitem(last=False)

    def _ws_loop(self) -> None:
        """Primary: Polymarket RTDS. Reconnects with backoff; the connect snapshot
        replays ~57 s of history, which covers a short reconnect gap."""
        sub = {"action": "subscribe", "subscriptions": [
            {"topic": self.TOPIC, "type": "update",
             "filters": json.dumps({"symbol": self.SYMBOL})}]}
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with websocket_connect(self.WS_URL, ssl=ssl_context(self._cfg)) as ws:
                    ws.send(json.dumps(sub))
                    last_ping = time.time()
                    backoff = 1.0
                    while not self._stop.is_set():
                        msg = ws.recv(timeout=10)
                        if time.time() - last_ping > 5:      # docs: PING text frame every 5 s
                            ws.send("PING"); last_ping = time.time()
                        if not msg or not msg.strip():
                            continue
                        j = json.loads(msg)
                        p = j.get("payload") or {}
                        if "data" in p:                       # connect snapshot (list of {timestamp,value})
                            for r in p["data"]:
                                self._put(r["timestamp"] // 1000,
                                          int(round(float(r["value"]) * 1e18)), "snapshot")
                            continue
                        if p.get("symbol") != self.SYMBOL:
                            continue
                        # full_accuracy_value is the EXACT 18-decimal Data Streams answer.
                        wei = int(p["full_accuracy_value"]) if p.get("full_accuracy_value") \
                              else int(round(float(p["value"]) * 1e18))
                        self._put(p["timestamp"] // 1000, wei, "rtds")
            except Exception as exc:
                log.warning("chainlink ws reconnect (%s)", exc)
                self._stop.wait(backoff); backoff = min(backoff * 2, 30.0)

    def _gmx_loop(self) -> None:
        """Hot standby: GMX republishes the SIGNED Data Streams report. ~0.5 s slower
        than RTDS, fully independent operator. Also our continuous integrity check."""
        while not self._stop.is_set():
            try:
                for x in get_session().get(self.GMX_URL, timeout=5).json()["signedPrices"]:
                    if x["tokenSymbol"] != "BTC" or not x.get("blob"):
                        continue
                    _, report, *_ = abi_decode(
                        ["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"],
                        bytes.fromhex(x["blob"][2:]))
                    fid, _vf, obs, _nf, _lf, _exp, price, _bid, _ask = abi_decode(
                        ["bytes32", "uint32", "uint32", "uint192", "uint192",
                         "uint32", "int192", "int192", "int192"], report)
                    if "0x" + fid.hex() != self.FEED_ID:
                        continue
                    self._put(int(obs), int(price), "gmx")
            except Exception as exc:
                log.debug("chainlink gmx poll failed: %s", exc)
            self._stop.wait(0.4)

    # ----------------------------------------------------------------- reads
    def price_at_or_after(self, ts: int, max_wait_secs: int = 5) -> Optional[float]:
        """Polymarket's rule: FIRST print with observationsTimestamp >= ts.
        Verified 99.59 % correct vs 98.03 % for forward-fill on 1-second gaps."""
        with self._lock:
            for t in range(int(ts), int(ts) + max_wait_secs + 1):
                if t in self._px:
                    return self._px[t] / 1e18
        return None

    def strike(self, window_start_ts: int) -> Optional[float]:
        return self.price_at_or_after(window_start_ts)

    def settle(self, close_ts: int) -> Optional[float]:
        return self.price_at_or_after(close_ts)

    def winner(self, window_start_ts: int, close_ts: int) -> Optional[str]:
        """Exact resolution. Integer comparison — no float ties."""
        with self._lock:
            o = c = None
            for t in range(window_start_ts, window_start_ts + 6):
                if t in self._px: o = self._px[t]; break
            for t in range(close_ts, close_ts + 6):
                if t in self._px: c = self._px[t]; break
        if o is None or c is None:
            return None
        return "up" if c >= o else "down"

    def latest(self) -> Optional[PricePoint]:
        with self._lock:
            if not self._px:
                return None
            sec, wei = next(reversed(self._px.items()))
        return PricePoint(ts=float(sec), price=wei / 1e18)

    def price_at_or_before(self, ts: float) -> Optional[float]:
        with self._lock:
            for t in range(int(ts), int(ts) - 30, -1):
                if t in self._px:
                    return self._px[t] / 1e18
        return None

    def staleness(self) -> float:
        """Seconds between the newest observation second we hold and wall clock.
        Healthy is ~1.4 s (p50) / 2.0 s (p99). Callers MUST skip if > ~4 s."""
        lp = self.latest()
        return float("inf") if lp is None else time.time() - lp.ts

    def rolling_log_return_std(self, window_secs: float = 120.0) -> float:
        """1 s log-return std over the trailing window. NOTE: recalibrate
        strategy.sigma_1s_floor for this feed — Chainlink moves every second
        (4.7 % repeats) whereas Binance 1 s klines are ~76 % flat."""
        ...  # same maths as BinanceOracle, over self._px

    def stop(self) -> None:
        self._stop.set()
```

**Engine wiring** (`engine.py`), replacing the family gate:

```python
# was:  if market.family != "1h": return
if market.family == "1h":
    S_open = self.binance.hour_open_close(market.window_start_ts)[0]
    S_t    = self.binance.latest().price
    sigma  = self.binance.rolling_log_return_std(cfg["vol_window_secs"])
else:
    if self.chainlink is None or self.chainlink.staleness() > 4.0:
        return                                   # no oracle / stale -> do not trade
    S_open = self.chainlink.strike(market.window_start_ts)
    if S_open is None:
        return
    cl_pt  = self.chainlink.latest()
    # hybrid: Chainlink anchor + Binance drift over the ~1.4 s publish lag.
    # measured 97.12 % vs 96.75 % sign accuracy at k=1 s (5m). Degrade gracefully.
    drift  = 0.0
    b_now, b_then = self.binance.latest(), self.binance.price_at_or_before(cl_pt.ts)
    if b_now and b_then:
        drift = b_now.price - b_then
    S_t    = cl_pt.price + drift
    sigma  = self.chainlink.rolling_log_return_std(cfg["vol_window_secs"])
```

**config.yaml**:
```yaml
endpoints:
  polymarket_rtds_ws: "wss://ws-live-data.polymarket.com"
  gmx_signed_prices:  "https://arbitrum-api.gmxinfra.io/signed_prices/latest"
  chainlink_rpc_url:  "https://polygon-bor-rpc.publicnode.com"      # sanity check only
  chainlink_feed_address: "0xc907e116054Ad103354f2D350FD2514433D57F6f"
families:
  5m:  {enabled: true, oracle: chainlink, close_snipe: true,  settle_sweep: false}
  15m: {enabled: true, oracle: chainlink, close_snipe: true,  settle_sweep: false}
  4h:  {enabled: true, oracle: chainlink, close_snipe: true,  settle_sweep: false}
strategy:
  close_snipe:
    max_oracle_staleness_secs: 4.0     # p99 publish lag 2.0 s + margin
```

Ship it in PAPER first and confirm against `result_id` for a day: `ChainlinkOracle.winner()` should
match Polymarket's resolution on **100 %** of closes. If it does not, something is wrong with the
transport, not with this analysis.

---

## 8. Claim ledger

**VERIFIED (executed in this session):** 100.000 % exact match of the capture to
`open/close_chainlink`; the circularity finding (backfill reconstruction); 100.00000 % sign agreement
with `result_id` on 35,982 markets and every near-tie bucket; the NaN-trap reproduction (98.63 %);
Binance's 4.77 % 5m error rate and the $17.48-std basis; the full cadence/lag/gap characterisation of
the capture; the gap-fill rule ranking; the lagged-signal accuracy tables; live `latestRoundData()` on
Polygon and Ethereum with prices, `updatedAt`, ages and RPC latency; 220-round Polygon cadence
(33.8 s mean); Data Streams REST rejecting unauthenticated calls (HTTP 400); the mainnet BTC/USD feed
ID and its appearance inside a live signed report; GMX blob decode, cadence and 2.08 s latency;
Polymarket RTDS subscribe/payload/cadence/latency/coverage; **266/266 exact integer match between
Polymarket RTDS and the signed Chainlink Data Streams report**; the upstream-gap proof; absence of a
Polymarket REST backfill; Pyth Hermes reachability.

**DOCUMENTED (read, not executed):** Data Streams endpoint hostnames and REST paths; the HMAC auth
header scheme; subscription-based billing with no public price list and "contact us" gating;
Polymarket RTDS topic list, symbol list, filter syntax and the 5 s `PING` requirement; the market
descriptions and `resolution_source` values (from the repo's own Telonex metadata).

**NOT ESTABLISHED:** the actual dollar cost of a Data Streams subscription; RTDS rate/connection
limits; whether Polymarket will keep RTDS public; end-to-end latency from the production server
(measured here through a proxy — expect ~0.1–0.2 s better).
