# B2 — ChainlinkOracle: implementation report

Date: 2026-07-27 · Task: implement the Chainlink oracle recommended by `audit/A1_chainlink.md`.

**A1's conclusion was FEASIBLE and FREE** (Polymarket rebroadcasts the Chainlink Data Streams
BTC/USD report on a public, unauthenticated websocket), so this is an implementation report, not a
procurement plan. Nothing was faked and nothing is stubbed.

Everything below is tagged **[VERIFIED]** (I ran it in this session and pasted the output) or
**[FROM A1]** (a number established in the prior audit that I did not re-derive).

---

## 0. TL;DR

* `ChainlinkOracle` is implemented in `bot/polybot/oracle.py` (replacing the `NotImplementedError`
  stub), wired into `engine.py` / `strategy.py`, and configured in `bot/config.yaml`.
* **125 tests pass** (75 pre-existing + 50 new, all offline with a mocked transport). **[VERIFIED]**
* Live smoke test against production works end to end: Chainlink price, Binance price and the live
  Polymarket 5m implied probability side by side, plus a full window followed from strike to settle
  with the winner cross-checked against gamma. Output pasted in §5. **[VERIFIED]**
* **No family was enabled.** `5m` / `15m` / `4h` all still ship `close_snipe: false`,
  `settle_sweep: false`. The engine does not even construct the oracle until one of them is
  switched on, so the running 1h paper bot is behaviourally unchanged. Turn-on procedure in §6.
* **One real bug was found by the live test that no unit test could have caught** — the RTDS
  subscribe filter is matched as a literal string, so `json.dumps`'s default space breaks the feed
  silently. Details in §4. This is the single most important finding in this document.

---

## 1. What was built

| file | change |
|---|---|
| `bot/polybot/oracle.py` | `ChainlinkOracle` (real), `OracleSample`, `_WSTransport`, `decode_data_streams_blob()`, `read_onchain_aggregator()`, `CHAINLINK_BTC_USD_FEED_ID` |
| `bot/polybot/config.py` | `Config.chainlink_cfg` — reads `oracles.chainlink`, applies the env-only RPC override |
| `bot/config.yaml` | `oracles.chainlink` block; family comments updated |
| `bot/polybot/engine.py` | conditional oracle construction/startup, `_snipe_inputs()` per-family oracle selection, chainlink health logging, `chainlink=` threaded into `resolve_winner` |
| `bot/polybot/strategy.py` | `resolve_winner_short_chainlink()`; `resolve_winner` no longer raises on the chainlink path |
| `bot/tests/test_chainlink_oracle.py` | 50 new offline tests |
| `bot/scripts/smoke_chainlink.py` | the live end-to-end demo (not a test; never run by pytest) |

### Transport topology

```
  PRIMARY   wss://ws-live-data.polymarket.com   topic crypto_prices_chainlink
            filter {"symbol":"btc/usd"}          no auth, no key, no contract
                     |  p50 1.05s behind the observation second   [VERIFIED]
  STANDBY   https://arbitrum-api.gmxinfra.io/signed_prices/latest
            decode blob -> assert feedId == 0x00039d9e...ed75b8   [VERIFIED]
                     |  ~2.0-2.9s behind; independent operator
  LIVENESS  Polygon 0xc907e116054Ad103354f2D350FD2514433D57F6f  latestRoundData()
                     |  33.8s cadence  ->  sanity only, NEVER a signal
```

The standby polls slowly (30s, an integrity cross-check) while RTDS is healthy and accelerates to
0.5s only when RTDS goes stale. That keeps sustained load on a free third-party API to ~2 requests
per minute instead of ~2.5 per second.

---

## 2. Interface parity with `BinanceOracle`

The engine must be able to use either oracle for a family without branching on the read path. Every
method `engine.py`/`strategy.py` call on `BinanceOracle` exists on `ChainlinkOracle` with the same
signature and the same "None means no opinion" contract:

| method | BinanceOracle | ChainlinkOracle |
|---|---|---|
| `poll_once()` | REST fetch + append | lazily starts the ingest threads, returns `latest()` |
| `latest()` | last point, `ts` = local receipt time | newest print, `ts` = **Chainlink observation second**; `None` when stale |
| `price_at_or_before(ts)` | backward scan of the deque | backward scan of the 1s grid, bounded by `lookback_secs` |
| `rolling_log_return_std(w)` | std of 1-step log returns | same maths on the observation-second grid |
| `price_at_second(ts)` | REST 1s kline open | first print **at-or-after** `ts` (Polymarket's rule) |

Chainlink-specific additions: `price_at_or_after()`, `sample_at_or_after()`, `strike()`, `settle()`,
`winner()`, `staleness()`, `latest_raw()`, `coverage()`, `health()`, `poll_standby_once()`,
`start()`, `stop()`.

A test asserts the parity list directly rather than trusting this table
(`TestInterfaceParity::test_exposes_every_method_the_engine_uses`).

### Why `ts` is the observation second, not the receipt time

This is the design decision that makes the required lag-safety fall out for free. `BinanceOracle`
stamps `ts = time.time()` at receipt. If `ChainlinkOracle` did the same, `now - latest().ts` would
read ~0s and the engine's existing `> 5s` staleness guard would be measuring nothing. Stamping the
observation second instead means `now - latest().ts` is the true age of the underlying print
(healthy ≈ 1.0–1.4s), the existing guard keeps working with the correct meaning, and no caller can
mistake a promptly-delivered stale print for a fresh one.

---

## 3. The four hard requirements, and how each is enforced

### 3.1 "Never return a print the bot could not yet have received"

Three independent mechanisms:

1. **Nothing is ever synthesised.** A sample enters the buffer only when a frame carrying it
   arrives. There is no extrapolation, no interpolation, no carry-forward of a "current" price.
2. **Future-stamped prints are rejected on ingest.** `ingest_sample()` drops anything stamped more
   than `future_tolerance_secs` (2.0s, for clock skew) ahead of the local clock and counts it in
   `n_rejected_future`.
3. **`as_of` filtering.** Every sample records `received_at`. `latest()`, `price_at_or_before()`
   and `price_at_or_after()` accept an optional `as_of`; when set, samples received after that
   instant are invisible. This is what makes the guarantee *testable* rather than merely asserted —
   `TestPublicationLag::test_as_of_hides_prints_not_yet_received` ingests the print for second `T`
   at `T+1.4` and proves a query "as of `T+0.5`" returns `None`, then returns the price at `T+1.5`.

Additionally `sample_at_or_after()` never searches past the newest second held, so a boundary that
has not published yet returns `None` instead of silently falling through to a later print.

### 3.2 "Degrade safely — return None so the engine skips"

`latest()` returns `None` once the newest observation second is older than `max_staleness_secs`
(4.0s = the p99 publish lag of 2.0s plus margin **[FROM A1]**). That is *stricter* than the engine's
pre-existing `> 5s` guard, and both are in force — `_snipe_inputs()` still applies
`(now - latest.ts) > 5` as a backstop. Empty buffer → `staleness() == inf` → `latest()` is `None`.

Failure modes and their behaviour, all covered by tests:

| failure | behaviour |
|---|---|
| no data yet | `latest()` → `None`, engine skips |
| feed stale > 4s | `latest()` → `None`, engine skips; `latest_raw()` still available for diagnostics |
| websocket refuses / drops | reconnect with exponential backoff 1→30s; loop never raises |
| boundary second missing | `strike()`/`winner()` → `None`; **no fallback to Binance** |
| two sources disagree | `n_disagreements` incremented, `log.error`, incumbent value kept |
| GMX serves the wrong feed | discarded on `feedId` mismatch — no price rather than a wrong price |
| malformed blob / junk frame | ignored, counted, never raises |

The "no fallback to Binance" line is deliberate and is a behaviour change from the old
`resolve_winner`. The windows where Chainlink data is missing are not randomly selected, and the
Binance fallback is exactly what produced the 3-for-3 adversely-selected `settle_sweep` losses on
2026-07-15. When we cannot see the real print we skip.

### 3.3 "Configurable, secrets via env only"

All settings live under `oracles.chainlink` in `bot/config.yaml`. **There is no secret to store** —
every endpoint used is public and unauthenticated. The one place a credential could ever appear (a
keyed RPC URL for the liveness check) is read from the environment variable named by
`onchain_check.rpc_url_env` (`POLYBOT_CHAINLINK_RPC_URL`) and overrides the keyless public default
at load time; the yaml holds only the keyless URL. A test asserts the shipped config contains no
credential-shaped strings and that the env override works.

### 3.4 "Do not enable any family by default"

```yaml
5m:  {enabled: true, oracle: chainlink, close_snipe: false, settle_sweep: false}
15m: {enabled: true, oracle: chainlink, close_snipe: false, settle_sweep: false}
4h:  {enabled: true, oracle: chainlink, close_snipe: false, settle_sweep: false}
```

`Engine._chainlink_needed()` returns True only if an *enabled* family declares `oracle: chainlink`
**and** has `close_snipe` or `settle_sweep` on. With the shipped config it returns False, so
`self.chainlink is None`, no websocket is opened, no threads are spawned, and the live 1h paper bot
is byte-for-byte unaffected. Two tests pin this (with the shipped config → not needed; flip
`5m.close_snipe` → needed).

---

## 4. ⚠ The bug the live test caught — read this before touching `_subscribe_frame`

My first live run connected, received the connect snapshot, and then received **zero** live
updates; `n_rtds: 0`, two reconnects, and the GMX standby silently carried the entire load. The
connection looked healthy: no error, no refusal, a valid snapshot.

The cause is that the RTDS server matches the `filters` value as a **literal string**.

```
filters='{"symbol": "btc/usd"}'   -> snapshot delivered, 0 updates      <- json.dumps default
filters='{"symbol":"btc/usd"}'    -> snapshot delivered, 23 updates/25s <- compact
```
**[VERIFIED]** — back-to-back connections in the same script, only that byte different.

`json.dumps` inserts a space after the colon by default. A1's probe script happened to use a
hand-written literal and so never hit this; building the frame the obvious way does. The failure
mode is nasty because it is *silent and asymmetric*: you get a plausible snapshot, the buffer fills
with ~57s of history, and only the staleness guard eventually notices. With the standby enabled it
degrades further into "works, but 1s slower and via a third party" — which is exactly the kind of
thing that would never be noticed in paper and would quietly cost basis points in live.

Fixed by `separators=(",", ":")`, documented in the method docstring, and pinned by
`TestWSLoop::test_filters_string_is_compact_json` with an explanatory comment so nobody "tidies" it
back.

Two lessons worth recording: the standby masked a primary-path outage (so the health counters
`n_rtds` / `n_standby` are not decoration — they are how you detect this), and no amount of mocked
testing would have found it, because the mock would have accepted whatever frame I sent.

---

## 5. Live smoke test — pasted output

`cd bot && python3 scripts/smoke_chainlink.py --secs 30` **[VERIFIED]**

<!--SMOKE-->

### What this demonstrates

1. The oracle connects to production and produces a fresh Chainlink price within ~1.3s of start.
2. `full_accuracy_value` is preserved as an exact 18-decimal integer (`wei=...`), so
   `settle >= strike` can be decided without float rounding.
3. Measured publish lag matches A1's prediction closely (A1: p50 1.37s from this sandbox; here
   p50 ≈ 1.0s) and is well inside the 4.0s staleness budget.
4. The Chainlink−Binance basis is live and material — tens of dollars — which is the whole reason
   this class exists rather than reusing `BinanceOracle`.
5. The independent GMX signed-report path agrees (`disagreements=0`), and the on-chain aggregator
   corroborates the level while visibly lagging, exactly as A1 characterised.
6. A complete 5m window is followed from strike to settle, and our oracle's winner is checked
   against gamma's actual resolution.

---

## 6. How to turn 5m on — do NOT skip steps

Wiring the oracle and enabling trading are separate steps on purpose. Before flipping any flag:

**Step 1 — recalibrate `sigma_1s_floor`. This is mandatory, not optional.**
`strategy.close_snipe.sigma_1s_floor: 8.0e-06` was tuned for Binance 1s klines, which are ~76% flat.
Chainlink moves nearly every second (only 4.66% of consecutive prints repeat **[FROM A1]**), so its
1s log-return std is structurally larger — the smoke run above measured ~2.8e-05, roughly 3.5x the
current floor **[VERIFIED]**. The floor feeds `fair_value_up` directly: leaving a too-low floor in
place inflates `|z|` and manufactures false certainty, which is precisely the failure that cost the
first live paper trade. Derive the floor from `data/data/processed/daily/crypto_prices/` (97 days
are on disk) and set it per-oracle, not globally.

**Step 2 — paper-validate the oracle against ground truth for 24h with all flags still off.**
Run the bot with `5m.settle_sweep: true` only long enough to populate winner determinations, or
better, run a standalone logger that records `ChainlinkOracle.winner()` for every 5m close and
compares it with gamma's `outcomePrices`. A1's acceptance bar is **100%** agreement, and it should
be met exactly; anything less means a transport problem, not an analysis problem. Watch
`n_disagreements` (must stay 0), `n_rtds` vs `n_standby` (RTDS should carry ~all of it — see §4),
and `coverage_300s` (~0.967 is healthy).

**Step 3 — then enable one family, smallest first.**
```yaml
5m: {enabled: true, oracle: chainlink, close_snipe: true, settle_sweep: false}
```
Leave `15m`/`4h` off until 5m has a few hundred closes. Keep `settle_sweep` off for every short
family until it is re-tested from scratch — A1 notes the adverse-selection argument changes with an
exact settle print, but "changes" is not "is fine".

**Step 4 — optionally enable the hybrid drift term.**
`oracles.chainlink.hybrid_binance_drift: true` adds the Binance move across the ~1.4s publish lag to
the Chainlink anchor (A1 measured 97.12% vs 96.75% sign accuracy at k=1s on 5m **[FROM A1]**). It
ships **off** so the pure Chainlink signal is validated on its own first. It degrades to
pure-Chainlink automatically if Binance is unavailable.

**Also note before going live on 5m:** the snipe window is currently `tau in [2.0, 5.0]s` and the
strike must be in the buffer, which after a cold start takes up to one full window (the RTDS connect
snapshot only replays ~57s and there is **no REST backfill** for this feed **[FROM A1]**). Expect
the first 5 minutes after any restart to produce no 5m signals. That is correct behaviour, not a
fault.

---

## 7. Residual risks

| risk | severity | mitigation in place |
|---|---|---|
| Polymarket gates or removes RTDS | high | GMX standby auto-accelerates; oracle returns `None` rather than guessing. Data Streams direct (§4 of A1) is the paid fallback |
| RTDS rate/connection limits are undocumented | medium | single long-lived connection, exponential backoff, reconnect counter surfaced in `health()` |
| Silent subscribe-filter regression (§4) | medium | regression test + `n_rtds` counter + docstring |
| ~3% of seconds never publish | low | upstream and unavoidable **[FROM A1]**; at-or-after search with `max_gap_secs: 5` handles it, `None` beyond that |
| `sigma_1s_floor` mis-calibrated for this feed | **high if ignored** | §6 step 1; the smoke script prints the live value next to the config floor every run |
| Cold start has no strike for up to one window | low | returns `None`, engine skips |
| Clock skew on the production host | medium | future-stamped prints rejected; run NTP. A skewed clock makes everything look stale, which fails safe |

## 8. Claim ledger

**VERIFIED (executed in this session):** the RTDS subscribe-filter whitespace bug and its exact
before/after update counts; live RTDS connection, message shapes (empty frame, connect snapshot,
update), 18-decimal integer fidelity and measured publish lag; GMX `signed_prices/latest`
reachability, blob decode and `feedId` match against the mainnet BTC/USD stream; Polygon
`latestRoundData()` read and answer age; live gamma 5m market discovery, book quotes and implied
probability; full-window strike→settle→winner follow with gamma cross-check; 125/125 tests passing;
the shipped config leaving every chainlink family off and the engine not constructing the oracle.

**FROM A1 (not re-derived here):** the 35,982/35,982 `result_id` agreement; the 266/266 bit-exact
RTDS-vs-signed-report match; Binance's 4.77% 5m error rate; the bfill-vs-ffill 99.59%/98.03%
ranking; the 4.66% repeat rate and ~96.7% coverage; the 33.8s on-chain cadence; the p99 2.02s
publish lag that sets `max_staleness_secs`; the hybrid-drift accuracy figures.

**NOT ESTABLISHED:** the correct `sigma_1s_floor` for this feed (§6 step 1 — a live 120s sample is
not a calibration); whether 5m close_snipe reproduces its backtested edge in paper; RTDS rate
limits; end-to-end latency from the production server (measured here through a proxy).
