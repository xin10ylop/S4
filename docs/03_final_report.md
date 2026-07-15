# Final Report — Polymarket BTC Up/Down Strategy Research

All results below are **out-of-sample** (OOS): parameters frozen on train, reported on untouched
test windows. Fees: verified current schedule — taker `0.07 × p × (1−p)` per share per leg,
**maker $0** (+20% rebate program). Fills: strict tape-level simulation — taker fills against the
recorded standing book 1–3s after the signal; maker fills require price to trade *through* the
limit. Latency ≥1s everywhere (robustness shown to 3s).

Splits: 5m train Feb 12–Apr 15 → test Apr 16–Jul 7 · 15m train Oct 11–Mar 31 → test Apr 1–Jul 7 ·
1h train Oct 11–Apr 30 → test May 1–Jul 12 · 4h train Oct 15–Mar 31 → test Apr 1–Jul 7.
(5m/15m/4h dailies have a vendor gap May 13–Jul 5; OOS covers Apr–May 12 + Jul 6–7. 1h has no gap.)

## The two structural discoveries everything rests on

1. **Resolution feeds are visible and per-family different.** The 1h family
   (`bitcoin-up-or-down-*`) resolves on the **Binance BTC/USDT 1H candle** — reproducible from
   public data with **100.0000% accuracy** (6,531/6,531 windows). The 5m/15m/4h families resolve
   on the **Chainlink BTC/USD stream**, whose print for second T becomes publicly visible at
   T+~1.3s. Chainlink is *not* lagged Binance — it is a multi-exchange aggregate with a wandering
   ~$19-RMSE basis vs Binance; the best predictor of the settle print is the last visible
   Chainlink print (RMSE $4.5 at 2s). Any strategy near the close must be built on the correct feed.

2. **Markets keep trading after close, and almost nobody watches.** Trading continues into the
   settlement window (median resolution 25–37s after close on short families; minutes-to-hours on
   1h). The Polymarket UI effectively buries expired markets, stranding GTC limit orders on the
   book — while the outcome is already publicly determined. On the 1h family the post-close tape
   shows essentially **zero competing takers**; on 15m a handful; on 5m an active bot pack
   (~$15k/day of winner-side fills at avg 0.84).

---

## Ranked strategies (all OOS, real fees, strict fills)

### #1 — Settlement sweep (all four families)
**Logic**: at close+2s the winner is knowable from the visible feed (Binance candle for 1h;
Chainlink print for 5m/15m/4h). Buy the winning side as taker wherever the standing book offers it
below 0.98 (edge > 2¢ after fee); sweep new quotes for up to 2 minutes. No model, no side-picking —
the outcome is already determined.

| family | trades/day | EV ¢/share | win rate | median entry | $/day at full available size |
|---|---|---|---|---|---|
| 5m | 8.3 | +30.4 | 98.8% | 0.73 | ~$430 (but contested — see risks) |
| 15m | 1.0 | +36.4 | 100% | 0.55 | ~$65 |
| 1h | 0.16 | +17.2 | 100% | 0.84 | ~$3–50/event, uncontested |
| 4h | 0.07 | +28.7 | 100% | 0.78 | small |

- Capacity: median $28–40 profit per event (p90 ≈ $170); the constraint is stranded-liquidity
  depth, not signal.
- Latency-insensitive (standing orders sit for seconds-to-minutes). wr losses: ~1% of events
  resolve against even a >$2 oracle cushion (resolution-print anomalies) — the EV shown includes them.
- $100 bankroll at $5-clips compounding, OOS replay (5m leg alone): **$100 → ~$7,700 in 29
  trading days, max drawdown 0.3%** — growth caps once clip size exceeds available discounts.
- The 5m leg is a race against existing bots (they demonstrably leave money behind — the sim
  consumes what *remained* on the book after their recorded fills — but a live entrant splits the
  pool). 15m/1h/4h legs are essentially uncontested in the data.

### #2 — 15m close-snipe
**Logic**: in the last 6s before close, fair value = Φ(distance/σ√τ) from real-time Binance;
buy either side as taker when fair − ask − fee > 5¢. One entry per window, hold to resolution.
- OOS: **+6.7¢/share, 10.9 trades/day, wr 74%** (at 2s latency: +8.1¢ — robust). Positive every
  month including July (+17.6¢).
- Median entry 0.65, median clip 12 shares ($8). Max loss −94¢/share (favorite reversal) —
  size accordingly. $100 @ $5-clips OOS replay: → ~$560 but 61% max DD under fixed-fraction
  sizing; halve clip size for sane DD.

### #3 — 5m close-snipe (visible-Chainlink signal)
Same logic on 5m, signal = last *visible* Chainlink print (2s stale), NOT Binance (Binance-signal
version is −0.3¢ = dead; the $19 basis kills it).
- OOS: **+14.0¢/share, 52 trades/day, wr 80%**, robust at 2s latency (+13.9¢).
- Honest caveat: EV concentrates in high-vol weeks (w17: +20.5¢ on n=676); the quietest recent
  days (Jul 6–7) gave only +1.2¢. This edge breathes with volatility and with competition. Deploy
  small, measure, scale only if live EV tracks.
- $100 @ $5-clips OOS replay: → ~$3,300, max DD 37%.

### #4 — 1h close-snipe
Same last-6s logic on the hourly family (Binance IS the oracle — zero model risk on the feed).
- OOS: **+9.8¢/share, 1.25 trades/day, wr 81%**; July alone +24¢ (n=9). Robust to 3s latency.
- Small but the cleanest feed-edge on the board; shares the infrastructure with #1/#2.

### #5 — Cross-family dominance arbitrage (5m × 15m)
**Logic**: the last 5m window of each 15m window shares its close. Strike ordering makes one
contract strictly dominate the other; when the dominated contract trades *richer* (gap > 3.5¢),
buy the dominant pair (payoff floor $1, sometimes $2 when the close lands between strikes).
- OOS: **+17.8¢/pair-share, 10.5 opportunities/day, wr 54%** (fat left tail when one leg's fill
  decays — needs two-leg execution with an abort rule; median violation lives 3s).

### #6 — Pre-open maker fade (the user's strategy family, validated & tuned)
**Logic**: place maker bids at ≤0.45 on BOTH sides pre-open (crowd pushes pre-open prices away
from fair ≈ 0.50 with no strike information); limit-sell filled side at 0.58; abort at bid
~10s after open if unfilled. Maker legs pay zero fee.
- OOS: **+0.67¢/share, ~59 fills/day** (t≈2.7). Small per-share but high frequency, near-zero
  capital, and the only entry that *never* crosses the spread. Best X found: 0.45 (0.47+ decays;
  0.49 ≈ breakeven — consistent with the user's live experience being real but thin).
- The "which side" question: unpredictable (every side-model failed — see ruled-out list); the
  both-sides fade with a fast abort IS the answer the data supports.

## Suggested portfolio at $100 bankroll, $5 clips
Run #1 across all four families + #2/#4 snipes from one process (same feeds: Binance 1s WS,
Chainlink stream WS, CLOB quotes WS). OOS-replay of that stack compounds $100 well past $1k within
a month at sub-40% DD — but treat projections as capacity-bounded: the settlement sweep alone
realistically supports **$400–700/day at full size** in the current regime, of which the
uncontested 15m/1h/4h slice is ~$70–120/day.

## Execution requirements
- Feeds: Binance 1s (public WS), Chainlink BTC/USD data-stream (public, ~1.3s publish lag),
  Polymarket CLOB market+user WS.
- Latency budget: 1–3s is sufficient (edges verified at 2–3s). No colocation needed.
- Order types: taker IOC for #1–#5; GTC post-only for #6. Fee rate 0.07 verified from the
  windows table + official docs; maker legs are free.

## Risks & honesty
- **Regime dependence**: snipe/settle frequency tracks volatility (Jan 2026 was 5–10× July).
  Both 1h legs decayed from Dec/Jan peaks (+33¢) to ~+8–10¢ by May–Jul — competition is arriving.
- **Resolution tail**: ~1% of settlement buys lose against a >$2 oracle cushion (print anomalies).
  Included in all EVs; cap per-event exposure.
- **The 5m settlement race**: recorded rivals take ~$15k/day there; our sim only consumes what
  they left, but a live entrant may face faster rivals for the same leftovers.
- **Fee changes**: Polymarket has moved fees 0 → 6.24% → 7.2% → 7% in 7 months. A doubling of
  the taker rate would cut ~1.5–2¢/share from the taker legs — #1 survives easily, #2–#4 compress.
- **Data gap**: May 13–Jul 5 dailies missing for 5m/15m/4h (vendor gap); OOS conclusions for those
  families rest on Apr 16–May 12 + Jul 6–7 (+ the gap-free 1h family May–Jul). Telonex (key
  verified) can backfill the gap and extend forward for live re-validation.

## Everything tested and ruled out (full log: `results/results_table.csv`)

| Hypothesis | Verdict | Why |
|---|---|---|
| H1–H8 ML/momentum/streak/imbalance side-prediction pre-open | FAIL | AUC ≈ 0.52 all families; BTC direction unpredictable at these horizons |
| H4 streak→price (gambler's fallacy) | REAL BUT THIN | crowd prices reversal after streaks (up to −5¢ on 15m open) yet outcomes partly justify it; residual ≤1¢ |
| H8 time-of-day bias | FAIL | ≤2.5¢, not robust across eras |
| H9–H11 skip filters on the fade | MINOR | vol/spread filters shift EV <0.5¢ |
| H12 mispricing surface | MAPPED | feeds #2–#4; favorite-longshot bias visible on mids everywhere |
| H14 first-seconds-after-open fade | FAIL | lf0 surface biases < spread+fee |
| H16 favorite-buy (taker) | FAIL | −9¢ on 4h: bias < spread+fee |
| H16 favorite-bid (maker) | FAIL | adverse selection: fills arrive exactly when favorites collapse |
| H19 both-sides pre-open set-buy 0.49/0.49 | FAIL | −0.7 to −1.9¢; one-sided adverse fills dominate; joint fill only 11–20% |
| H21 4h two-sided market-making ±2¢ | FAIL | −3¢/pair: −10¢ adverse selection per one-sided fill vs +4¢ capture |
| H22 4h directional momentum | FAIL | same AUC ≈ 0.52 |
| H26 spike overreaction fade | INVERTED, THEN FAIL | market *under*-reacts (+2.6¢ continuation on mids) but spread+fee eats the follow |
| H27 thin-book push fade | WEAK | true reversion +0.9¢ thin books; below capture threshold |
| H28 flow-following | FAIL (honest kill) | initial +13–16¢ was lookahead leakage; corrected ΔAUC ≈ 0.000 |
| 5m close-snipe on Binance signal | FAIL | −0.3¢: the $19 oracle basis poisons close calls; visible-Chainlink variant is #3 |
| Early snipe (20–30s before close) | FAIL | model overconfidence at long τ; flipped negative in Mar/Apr |

## Reproduction
`scripts/sync_data.py` → `scripts/fetch_binance.py` → `scripts/build_windows_all.py` →
`scripts/build_snapshots.py`, `build_binance_features.py`, `scan_events.py` →
`scripts/backtest_1h.py`, `backtest_close_snipe.py` (env `TLX_LAT` for latency sensitivity) →
`results/*.csv`. Full audit in `docs/01_data_audit.md`, hypothesis battery in `docs/02_hypotheses.md`.
