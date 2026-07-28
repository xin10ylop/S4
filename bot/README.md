# polybot — Polymarket BTC Up/Down trading bot

Paper-trading bot (with a gated, real-order LIVE path) for Polymarket's BTC
Up/Down markets (5m/15m/1h/4h families). Built from the strategy research in
`../docs/` (`03_final_report.md`, `04_executability_audit.md`,
`05_clob_api_spec.md`) — read those for *why* these two strategies, and what
their live-executability actually looks like.

## Strategies

- **close_snipe** (primary; default ON for the `1h` family only): in a bounded
  band before a window closes — `tau in [tau_lo, snipe_last_secs]`, default
  **[2.5s, 5.0s]** (`snipe_min_tau_secs: 2.5` binds over
  `latency_ms/1000 + snipe_fill_margin_secs = 2.0`), where
  `tau_lo = max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs)`
  — compares the live Binance underlying price to a normal-CDF fair value and
  takes a mispriced ask if the edge clears `edge_min` after fees. The band is
  bounded at **both** ends (`audit/A4_change_spec.md`): below `tau_lo` the
  order arrives at/after the close and cannot fill (measured 0 fills in 46
  attempts at `tau=1`, 42 of them on an empty book — in LIVE that is a real
  FAK order into a closed market); above `snipe_last_secs` the `fair` frozen
  at signal time carries too much unresolved BTC risk for the raised
  `per_event_cap_usd` (every large loss at a $250 clip came from `tau >= 5`).
  1h uses Binance directly
  (Binance *is* the market's resolution source, so there's no basis risk).
  5m/15m/4h resolve on Chainlink and stay OFF until a Chainlink oracle is
  wired (see `polybot/oracle.py::ChainlinkOracle` — a documented, honest
  stub, not a silent Binance substitute).
  > **Operator note — coverage arithmetic.** Audits that counted `snipe eval`
  > log lines used to divide by 6 (one eval tick per second in the old `(0, 6]`
  > window) to get "closes evaluated". With the `[2.5, 5.0]` band at a 1 Hz
  > tick the divisor is now **3–4**. A ~45% drop in eval lines after this
  > change is the expected, intended effect — **not** a throttle.

- **settle_sweep** (measurement mode; **OFF for every family**): after a
  window closes, sweeps the known winner's cheap asks while the market
  microstructure catches up. Per the executability audit this is largely an
  HFT race in practice (winner-side asks are cheap and present only a small
  fraction of the time, for well under a second). The measurement is now in:
  10 days live on 1h produced **1,235 signals and 0 fills** (every attempt
  `empty_book` — the post-close book is bulk-cancelled and does not
  repopulate), so it is disabled for 1h as well as for the Chainlink
  families. The code path is retained, not deleted: it is the re-entry point
  if a book that survives the close is ever observed.

Both strategies self-size: they walk the live ask ladder, buying every level
that still clears the edge threshold, up to a per-event clip —
`sizing.per_event_cap_usd` (default **$250**, PAPER) for close_snipe and
`strategy.settle_sweep.cap_usd` (default $25) for settle_sweep, which is kept
separate so a re-enabled settle_sweep cannot inherit the close_snipe clip.

## Risk guards (M4)

Five things bound risk here. Two are old (`per_event_cap_usd` per trade,
`max_open_notional` across open positions); three were added in M4 and are
documented with their measurements in `../audit/M4_risk_guards.md`. All are
config-driven and backward compatible — a `config.yaml` written before M4
still loads, and the two guards that matter default **ON** even if the file
says nothing about them.

| Guard | Config | Default | Blocks |
|---|---|---|---|
| 1. Adverse-size filter | `strategy.close_snipe.adverse_size` | **OFF** (measured) | caps/skips an ask level whose size is anomalous vs the family's recent typical depth |
| 2. Warmup after restart | `strategy.close_snipe.warmup` | **ON** | any close_snipe until the vol buffer and process uptime are both warm |
| 3. Daily loss limit + consecutive-loss brake | `risk:` | **ON** | opening *any* new position after a bad day |

**1. Adverse-size filter — implemented, tested, and OFF by default.** The
statistic is `level.size / median(recent in-band ask-level sizes for this
family)`; only levels inside `(price_min, price_max)` feed the reference,
because a book that has already decided quotes ~24,000 shares at $0.01 and ~11
shares at $0.50 and pooling them makes the statistic meaningless. The default
is OFF **because the measurement did not support the hypothesis**: replaying shipped
close_snipe over 6,523 1h closes, levels larger than 5x the family median went
44-for-44 at +38.1c/share while normal levels went 175-for-189 (92.6%) at
+22.4c/share — at every threshold from 3x up, all 14 losing trades came from
*normal*-sized offers. Enabling it at 5x would have discarded 69% of realized
PnL (skip mode) to prevent a loss that never happened. The evidence is not
unanimous and §1.2b of the audit records the dissent in full: of three
reference definitions tested, the market's own trailing-300s depth gives
−10.6c/share (p=0.035, n=19) at its loosest cut — but those trades are still
+15.6c/share profitable (a dilution, not adverse selection), it is 1 cell of
15, and it decays to p=0.90 as the threshold tightens. The mechanism it guards (an informed seller) is real
in principle — it is what poisoned `settle_sweep` 3-for-3 — but there the
seller knew the Chainlink print and we were reading Binance; in close_snipe
the information is the public BTC price, so a big resting offer is a stale
market-maker quote, not a sniper. **Re-run the measurement per coin before
enabling it for a new family.**

**2. Warmup after restart.** `sigma_1s` is the std of 1s log returns over the
trailing 120s of oracle polls; a fresh process has an almost empty buffer and
`rolling_log_return_std` will answer from as few as two returns. A too-small
sigma inflates `|z|` and pushes `fair` toward `fair_cap` — the mechanism
behind this project's first −$25 loss. Trading is blocked until
`min_oracle_samples: 60` samples exist inside the vol window **and**
`min_uptime_secs: 120` of wall clock have passed, logged every 15s while
warming. Measured: signals that exist *only* because sigma was cold are
25.5% of the tape at n=3 and earn +9.2c/share against the warm tape's
+25.0c/share (−15.8c, day-clustered p<0.001); at n=60 that contamination is
3.3%. The lockout costs ~0.0012 trades per restart.

**3. Daily loss limit / circuit breaker.** Stops opening new positions once
realized PnL since UTC midnight breaches
`max_daily_loss_pct` × `bankroll_usd` (or `max_daily_loss_usd`, whichever is
**tighter**), or once `max_consecutive_losses` resolved trades in a row have
lost. Resets automatically at UTC midnight (the daily figure is computed from
UTC midnight — there is no cron and no state to clear). Applies to
close_snipe *and* settle_sweep. Calibration: over 119 trading days the worst
day was −$41.47, no day was worse than −$50, and the longest losing streak was
3 — so the shipped $100/day and 4-in-a-row would never have fired on the
observed tape. This is a breaker for genuine breakage, not a variance
throttle.

```bash
python -m polybot.main status     # shows a "Risk guards (M4)" section
python -m polybot.main resume     # manual override: release the breaker for this UTC day
python -m polybot.main halt       # cancel an override (re-arm immediately)
```

The override is **not** an off switch: it is scoped to one UTC day and records
the loss level at which it was granted, so the breaker re-arms if the day
loses another full limit or a new losing streak forms. A forgotten override
cannot silently disable the guard. `status.json` carries
`guards.trading_blocked` — that single boolean is what an alerting rule should
watch, because "alive but deliberately not trading" otherwise looks exactly
like "alive with no signals".

### Verifying the guards are real

`python3 scripts/m4/mutation_check.py` copies `bot/` to a temp dir, deletes or
neuters each guard in turn (26 mutations), and requires the suite to go red
for every one. A previous audit of this project found a guard that could be
removed entirely with a green suite; this is the standing check that it cannot
happen again. Current result: **26/26 killed**. It has caught three real
defects so far — see `../audit/M4_risk_guards.md` §5.1.

## Paper fill engine — the honesty guarantee

A signal is never filled against the book that triggered it. The engine
waits `latency_ms` (default 1500ms, simulating reaction latency), RE-FETCHES
the live order book, and only then walks it for fills. This is what makes the
paper numbers trustworthy: it captures the real risk that the book moves or
the profitable depth vanishes between signal and order arrival. Every
attempt — filled or not — is recorded, including `empty_book` /
`book_moved_no_edge` outcomes, because "signal fired but nothing was there to
buy" is itself the key executability metric.

## Quick start (paper, local)

```bash
cd bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python -m polybot.main markets     # list currently-live BTC markets (no state needed)
python -m polybot.main run         # main loop; Ctrl-C to stop
python -m polybot.main status      # in another shell, once it's running
```

Requires Python 3.11. No secrets/env vars are needed for paper mode.

If you're behind a proxy that needs a custom CA bundle (e.g. this repo's dev
sandbox), set `POLYBOT_CA_BUNDLE=/path/to/bundle.crt`; on a normal server,
leave it unset and the system trust store is used. See `polybot/net.py`.

### HTTP status endpoint

While `run` is active: `curl localhost:8899/status | python3 -m json.tool` and
`curl localhost:8899/health`. Port comes from `POLYBOT_PORT` (default 8899).

### Data / logs

- `bot/data/polybot.db` — SQLite, source of truth (`signals`, `fills`,
  `resolutions` tables).
- `bot/data/fills.csv`, `bot/data/pnl.csv` — append-only CSV mirrors.
- `bot/data/polybot.log` — rotating log file (also mirrored to stdout).
- `bot/data/status.json` — machine-readable snapshot, rewritten every few
  seconds (`status.write_interval_secs` in config.yaml). Includes a `guards`
  block (warmup / circuit breaker / depth reference) and the single
  `guards.trading_blocked` boolean.
- `bot/data/risk_override.json` — written by `python -m polybot.main resume`;
  UTC-day-scoped, removed by `halt`. Absent = no override.

All of `bot/data/` is git-ignored.

## Configuration

Everything tunable lives in `bot/config.yaml`: per-family enable flags,
per-strategy edges/windows/caps, latency, fee rate, polling intervals,
storage paths. It's plain YAML, safe to commit — **no secrets belong there**.
Secrets are environment variables only (see `.env.example`); the bot never
logs them.

## Running the test suite

```bash
cd bot
python3 -m pytest tests -q          # 212 tests
python3 -m unittest discover -s tests -v
```

These are deterministic, offline unit tests (no network) covering the fee/
fill-walk math, the fair-value formula, ledger PnL arithmetic, slug parsing
(including a live-verified gamma quirk, see below), the paper/live execution
guards, and the three M4 risk guards. They complement, not replace, testing
against live data — see "What was actually tested" below.

Tests that expect a fill must explicitly warm the engine
(`tests.test_engine_gating.warm`) — the warmup guard is on by default and
cannot be bypassed by accident, which is why it shows up in unrelated tests.

## Live-verified quirks worth knowing before you touch this code

- **gamma's `startDate` for hourly markets is NOT the pricing-window open.**
  It's roughly the market-listing time (observed ~2 days before `endDate`).
  The true window start for every family is always `endDate - duration`
  (cross-checked against the slug-embedded timestamp for short families).
  See `polymarket.py::_parse_market`.
- **`GET /markets?slug=X` implicitly filters to `closed=false` when the
  `closed` param is omitted.** A market that has already resolved returns an
  empty list unless you pass `closed=true` explicitly. Discovery leaves
  `closed` unset; the resolution poller passes `closed=True`. See
  `polymarket.py::GammaClient.get_market_by_slug`.
- **`/book`'s bids/asks are not guaranteed sorted** (observed both orderings
  live). Always sort yourself — `polymarket.py::OrderBook.from_raw` does.
- **Markets stay `accepting_orders=true, closed=false` for minutes past their
  actual close.** This is the whole premise of `settle_sweep` — but per the
  executability audit, the *cheap* asks on the winning side vanish in well
  under a second, so don't expect settle_sweep paper fills to be common; see
  the empty-book results from the live run in the build report.

## Flipping to LIVE (real money)

Three independent guards must all agree, or the bot stays in paper mode
(`polybot/execution.py`, `polybot/config.py::Config.paper`):

1. `bot/config.yaml`: `mode.paper: false`
2. Environment: `POLYBOT_LIVE=1`
3. Environment: `POLYBOT_PK=<funded wallet private key>`

Also set `POLYBOT_SIGNATURE_TYPE` (0=EOA, 1=email/magic proxy wallet,
2=browser-wallet proxy — 0 unless you know you're using a proxy wallet) and
`POLYBOT_FUNDER` (the proxy wallet address, required for signature_type 1/2).
Put these in `bot/.env` (chmod 600, git-ignored — copy `.env.example`).

**The live order path is implemented against the real, installed
`py-clob-client` API** (`ClobClient`, `OrderArgs`, `create_order`,
`post_order(..., OrderType.FAK)`) — verified by inspecting the installed
package's source, not guessed. It has **not** been exercised end-to-end
against a funded account (none available in this build environment). Before
it ever touches real money:

1. Read the verification-status note at the top of `polybot/execution.py` —
   it lists exactly what's confirmed vs. still unverified (notably: the
   `post_order` response schema, and the `fee_rate_bps` semantics).
2. Test with a tiny `per_event_cap_usd` first, watching `/status` and the
   log closely. **The shipped `per_event_cap_usd: 250` is a PAPER setting**
   chosen to collect tail evidence that the top-of-book backtest cannot
   provide; `docs/06_live_audit.md` §8 puts the live go/no-go at ~20 resolved
   trades and a $25–50 starting clip. Turn it down before arming LIVE.
3. Only then scale `per_event_cap_usd` / `max_open_notional` up, and only
   after paper-mode fill/resolution data supports it. Note that
   `max_open_notional` is a *stop-opening-new-positions* gate checked before
   the new fill is sized, so the true ceiling is
   `max_open_notional + per_event_cap_usd` ($1,250 at the shipped values).

Nothing in this repo will place a real order unless you deliberately set all
three guards above. If you want to double check: `python -m polybot.main run`
in paper mode with `mode.paper: false` set anyway will still refuse to place
live orders unless `POLYBOT_LIVE=1` and `POLYBOT_PK` are set, and will raise
a clear `LiveTradingDisabled` error rather than silently paper-trading if
you *do* set `POLYBOT_LIVE=1` without a key.

## Optional: WebSocket market data

`polybot/ws_client.py` is a live-verified but **off-by-default**
(`execution.use_websocket: false`) alternative to REST book polling — it
subscribes to `wss://ws-subscriptions-clob.polymarket.com/ws/market` and
maintains an in-memory book cache from the `book`/`price_change` events. It
was independently connected and validated during this build (see the module
docstring for the exact verified message schema). It is not wired into the
engine's decision loop; REST polling (`order_book_poll_secs`, default 1s) is
the tested, always-on default path. Wiring the WS client in as a
lower-latency source for `close_snipe`'s book fetch is a natural follow-up.

## Deploying to a server

```bash
# On a fresh Ubuntu box, as root:
POLYBOT_REPO_URL=<your-git-remote> bash bot/scripts/deploy_server.sh
```

This clones (or pulls) the repo to `/root/S4`, creates a venv, installs
`requirements.txt`, and installs/enables/starts the `polybot` systemd unit
(`bot/scripts/polybot.service`). Re-run it any time to redeploy the latest
code (`git pull` + restart). It never touches secrets — create
`/root/S4/bot/.env` yourself (chmod 600) before flipping to live mode.

Service management once deployed:
```bash
systemctl status polybot
journalctl -u polybot -f
systemctl restart polybot
```

## File tree

```
bot/
  config.yaml               # all tunables; safe to commit, no secrets
  requirements.txt
  .env.example               # copy to .env (git-ignored) for LIVE-mode secrets
  README.md
  polybot/
    __init__.py
    config.py                # config.yaml + env loading
    net.py                   # shared requests.Session + ssl.SSLContext (CA bundle handling)
    logging_setup.py         # stdout + rotating file logging
    oracle.py                # BinanceOracle (data-api.binance.vision); ChainlinkOracle stub
    polymarket.py             # gamma discovery + CLOB REST book/midpoint client
    strategy.py               # close_snipe / settle_sweep signal math (pure functions)
    fill_engine.py             # book-walking + latency-aware paper fill simulation
    execution.py               # PAPER/LIVE order router (gated real-order path)
    ledger.py                  # SQLite + CSV persistence, resolution, PnL/metrics
    status_server.py           # status.json writer + stdlib HTTP server (/status, /health)
    depth.py                   # M4 guard 1: rolling per-family depth reference statistic
    risk.py                    # M4 guards 2+3: WarmupGate, CircuitBreaker
    engine.py                  # orchestration: discovery/oracle/tick loops, thread pool
    ws_client.py               # optional, off-by-default WS market-data client
    main.py                    # CLI: run / status / markets / pnl / resume / halt
  scripts/
    polybot.service            # systemd unit
    deploy_server.sh           # clone/pull, venv, install service, start
  tests/                       # offline, deterministic unit tests (no network)
  data/                        # runtime output (git-ignored): db, csv, log, status.json
```

## Paper-trading finding #1 (2026-07-15, first 4.5h deployed)

358 settle-sweep attempts across all families: 355 `empty_book`, **3 fills — all
three on 5m markets, all three `resolution_disagreements`, all three lost**
(paper −$80). The Binance-proxy winner call was wrong exactly and only on the
fills the book allowed: the cheap "winner" asks still standing post-close were
left by traders watching the true oracle (Chainlink) who knew the Binance call
was wrong. Textbook adverse selection — the distance guard cannot fix a
selection effect. Consequence: `settle_sweep` is now **disabled for 5m/15m/4h
in config.yaml** until a real Chainlink oracle is wired; it remains ON for 1h
(Binance IS that family's resolution source — zero basis risk; its 4.5h of
attempts show only clean `empty_book` outcomes, no poisoned fills).

## Known TODOs

- **Chainlink oracle not wired** (`oracle.py::ChainlinkOracle`) — 5m/15m/4h
  `close_snipe` stays off until this exists; don't substitute Binance as a
  signal source for it (basis risk, see the executability audit).
- **LIVE `post_order` response parsing** — schema unverified without a real
  account; see `execution.py` module docstring.
- **`fee_rate_bps` on live orders** — left at 0 (SDK default); relationship
  to the market's `takerBaseFee`/gamma `feeSchedule` unconfirmed against a
  real fill. See `execution.py`.
- **WS client not wired into the engine's decision loop** — exists,
  live-verified, off by default; see above.
- Settlement-sweep capacity should be judged from accumulated paper data
  (`pnl.csv` filtered to `strategy=settle_sweep`), not assumed from the
  research doc's numbers — that's the whole point of running it in paper
  first.
