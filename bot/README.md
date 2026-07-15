# polybot — Polymarket BTC Up/Down trading bot

Paper-trading bot (with a gated, real-order LIVE path) for Polymarket's BTC
Up/Down markets (5m/15m/1h/4h families). Built from the strategy research in
`../docs/` (`03_final_report.md`, `04_executability_audit.md`,
`05_clob_api_spec.md`) — read those for *why* these two strategies, and what
their live-executability actually looks like.

## Strategies

- **close_snipe** (primary; default ON for the `1h` family only): in the last
  `snipe_last_secs` (default 6) before a window closes, compares the live
  Binance underlying price to a normal-CDF fair value and takes a mispriced
  ask if the edge clears `edge_min` after fees. 1h uses Binance directly
  (Binance *is* the market's resolution source, so there's no basis risk).
  5m/15m/4h resolve on Chainlink and stay OFF until a Chainlink oracle is
  wired (see `polybot/oracle.py::ChainlinkOracle` — a documented, honest
  stub, not a silent Binance substitute).
- **settle_sweep** (measurement mode; default ON for all families): after a
  window closes, sweeps the known winner's cheap asks while the market
  microstructure catches up. Per the executability audit this is largely an
  HFT race in practice (winner-side asks are cheap and present only a small
  fraction of the time, for well under a second) — this build runs it to
  *measure* real paper-fill capture rate, not because real money should
  follow it yet.

Both strategies self-size: they walk the live ask ladder, buying every level
that still clears the edge threshold, up to `per_event_cap_usd`.

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
  seconds (`status.write_interval_secs` in config.yaml).

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
python3 -m unittest discover -s tests -v
```

These are deterministic, offline unit tests (no network) covering the fee/
fill-walk math, the fair-value formula, ledger PnL arithmetic, slug parsing
(including a live-verified gamma quirk, see below), and the paper/live
execution guards. They complement, not replace, testing against live data —
see "What was actually tested" below.

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
   log closely.
3. Only then scale `per_event_cap_usd` / `max_open_notional` up, and only
   after paper-mode fill/resolution data supports it.

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
    engine.py                  # orchestration: discovery/oracle/tick loops, thread pool
    ws_client.py               # optional, off-by-default WS market-data client
    main.py                    # CLI: run / status / markets
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
