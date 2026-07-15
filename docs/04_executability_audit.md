# Executability Re-Audit (live-data verified, 2026-07-15)

Purpose: before any real money, re-attack the two candidate strategies with *live* Polymarket
data and *full-resolution* history, focusing on fill realism and latency — the two things that
turn a backtest edge into a mirage.

## What I verified against the LIVE API

- Live BTC markets exist for every family right now (`bitcoin-up-or-down-…-et` hourly;
  `btc-updown-{5m,15m,4h}-<ts>` short). New short markets are minted continuously.
- Order books are live and deep on running markets (e.g. the 1pm-ET hourly: 23 bids / 76 asks,
  tick 0.01, min order 5 shares, `enableOrderBook=true`).
- **Markets keep `accepting_orders=true, closed=false` after the window close.** Confirmed on a
  5m market **+27s past its close** — orders were still accepted. The core "trade after the
  outcome is known" premise holds.
- Token mapping: `clobTokenIds[0]=Up (outcome_0)`, `[1]=Down`. tick 0.01, min 5.

## The fill-realism attack that changes the ranking

Measured on full-resolution historical quotes (every best-bid/ask change), post-close winner-side
asks priced below 0.98 (the settlement-sweep target):

| family | % of closes with a cheap winner ask | median persistence | p90 persistence |
|---|---|---|---|
| 1h | **2%** | 0.2 s | 0.2 s |
| 5m | **8%** | 0.4 s | 1.5 s |

**Implication**: the settlement-sweep opportunity is (a) rare and (b) gone in a fraction of a
second. At a realistic 1–3 s reaction latency you would miss almost all of it — the fills my
1-second-granularity backtest "captured" were, in real life, taken by faster/colocated bots. The
+30¢/share settlement EV is therefore **not achievable without HFT co-location**. This corrects the
first report, which wrongly called settlement "latency-insensitive" (the *market* stays open for
minutes; the *cheap asks* do not).

Live snapshot corroboration: a 5m market at +27s post-close showed the winning token's asks
**empty** (holders correctly refuse to dump the winner) while the losing token had 983k shares
resting at 0.01 (worthless dumps — a trap the bot must never buy).

## Revised ranking by *executability* (not theoretical edge)

1. **1h close-snipe** — trades the deep, liquid running book in the final seconds; edge is a stale
   quote vs the fast Binance underlying (Binance *is* the 1h oracle → reachable, no basis risk).
   Verified to hold at 2–3 s latency (+8–24¢/share OOS). **This is the first real-money candidate.**
2. **15m/5m close-snipe (Chainlink signal)** — same mechanism, but needs a live Chainlink BTC/USD
   feed (Binance-signal version is dead due to the ~$19 basis). Higher volume; deploy once the
   Chainlink feed is wired and paper-validated.
3. **Settlement sweep (all families)** — theoretically cleanest (outcome known) but an HFT race in
   practice. Keep it in the bot in **measurement-only** paper mode to quantify real capture; do not
   commit real money unless paper shows a capture rate and latency profile that actually works.

## Self-sizing / max-fill (answer to "can it always buy the max it can and fill")

Both strategies are implemented to **walk the live book and take all size that remains profitable**:
- Sort the target side's asks by price; consume each level while `1 − price − fee(price) > min_edge`
  (settlement) or `fair − price − fee(price) > min_edge` (snipe); stop at the first unprofitable
  level or the configured per-event notional cap.
- This auto-adapts to whatever depth is present: tiny book → tiny fill; deep book → large fill.
  It never crosses into negative-EV levels, so it "always buys the max it can *profitably* fill".

## Scalability / max bet (from book depth)

- **Close-snipe (running book)**: the last-seconds book is deep. Observed per-side depth to a 1¢
  slippage band routinely spans hundreds–thousands of shares; the practical per-trade cap is set by
  how much you're willing to hold at ~80% win rate, not by liquidity. Sensible live cap: start
  $10–25/clip, scale to a few hundred $/event as paper confirms fills.
- **Settlement sweep (stranded book)**: winner-side depth below 0.98, when present, is large
  (median ~4,500–5,000 shares ≈ $4k on short families) BUT present only 2–8% of the time and for
  <0.5 s — so realized capacity is bounded by latency and competition, not by depth.
- **Hard limits**: min order 5 shares, tick 1¢. Taker fee `0.07·p·(1−p)`/share; maker 0.
- **Overall max sustainable size** is competition-bounded: you are splitting stranded/stale
  liquidity with other bots. Paper trading measures your realized share; scale to it, don't assume
  the whole book is yours.

## Live-execution requirements confirmed
- Feeds: Binance 1s (server-side; geo-blocked in this sandbox but fine on the DO box), Polymarket
  CLOB REST `/book` + WSS market channel, gamma market discovery. Optional Chainlink for short snipes.
- Order path: `py-clob-client`, taker IOC/FAK for sweeping, L2 API-key auth over an L1-signed key.
- Latency budget: 1–3 s works for close-snipe; sub-second needed for settlement (hence paper-measure).

## Bottom line
Paper-trade the **1h close-snipe** as the lead real-money candidate, and run settlement + short
families alongside in measurement mode. Let live paper fills — not backtest EV — decide what gets
real money. The bot below is built to make exactly that decision with faithful, latency-aware fills.
