# Live Paper-Trading Audit (2026-07-26, after ~10 days deployed)

Live results audited: `close_snipe/1h` — 11 signals, 7 fills, 4 `book_moved_no_edge`,
214.69 shares, $134.09 deployed, **+$44.97 realized, 85.7% win rate, +20.95¢/share**.
`settle_sweep/1h` — 1,235 signals, 1,235 `empty_book`, zero fills.

> Scope note: a 10-agent audit workflow (Telonex shadow-replay of the live period + Chainlink
> feed scoping) **failed — all agents hit the account session limit** and returned nothing.
> The two highest-value analyses below were run directly on local data instead. The Telonex
> live-period replay and the Chainlink feasibility study remain **not done**.

> **CORRECTION (2026-07-27, `audit/A4_change_spec.md` — read that before acting on §5/§6).**
> The timing claim in §5.1 / §6 lever 2 was **re-measured on 1,738 OOS closes using the bot's
> own 1.5 s signal→fill gap and re-read fill book, and it does not hold**:
> - The "−2s → −1s" cell assumed a 1 s gap and read the *same* book for the signal and the
>   fill, so it could not produce a `book_moved_no_edge`. At the shipped 1.5 s gap that cell is
>   unreachable: **`tau = 1` signals filled 0 times out of 46**, 42 of them on an empty book.
> - "Snipe only in the last 2–3s" is **refuted on both datasets**: `[1,3]` earns $38.77 vs
>   `[1,6]`'s $59.09 on the live period, and has the lowest total P&L of any candidate OOS.
> - There is **no significant gradient inside `tau = 2…6`** (11.7 / 18.8 / 13.0 / 17.9 / 11.3 ¢
>   on n = 26–51 fills). What *is* confirmed, and more strongly than stated here, is the decay
>   **outside** ~10 s: −4.85¢ at `tau = 15`, −6.99¢ at `tau = 60`.
> - The cap table in §4 is monotone to $1,000 only **after** the window is narrowed. In the
>   shipped `(0, 6]` window P&L peaks near $250 and collapses ($473 at $500, $28 at $1,000).
>
> **Shipped instead:** bound the window at *both* ends, `tau ∈ [2.0, 5.0]`. It is P&L-neutral at
> the $25 clip (bootstrap CI straddles zero — stated honestly) and is the **precondition for
> lever 1**: at $250 it turns a −$258.75 worst trade and a −$232 worst 10-trade run into −$45.26
> and −$16 while *raising* mean P&L ($755 vs $676).

## 1. Is anything stopping the bot? **No.**

| check | result |
|---|---|
| eval ticks / 6 per close | 1,426 ÷ 6 = **237.7 closes evaluated** |
| hourly closes available in 9.7 days | ~233 |
| coverage | **~100% — not one close was skipped** |
| signals | 11 / 238 closes = **1.13/day** (backtest predicted **1.25/day**) |
| fills | 7 / 11 signals = 64%; the 4 misses are the post-loss safety fix declining after the book moved |

No gate, bug, or throttle is suppressing activity. The bot is firing exactly as designed.

## 2. Is the edge real? Better than backtest — but the sample is still small

| | Backtest OOS | Live paper |
|---|---|---|
| EV/share | 9.82¢ | **20.95¢** |
| Win rate | 81.3% | 85.7% (6/7) |
| Signals/day | 1.25 | 1.13 |

Return on capital actually deployed: **+33.5% in 10 days** ($44.97 on $134.09).

Honest statistics at n=7: P(≥6 wins | no edge) = **0.0625** — suggestive, not significant.
95% CI on the win rate = **[0.50, 0.98]**. Encouraging; not yet proof.

## 3. Why the P&L is small — three small numbers multiplied

**$4.64/day = 0.72 fills/day × 30.7 shares/fill × $0.2095 EV/share**

The edge (third factor) is excellent. The other two are small **by our own configuration**:

- **Frequency** is capped by trading only the 1h family: ~1 tradeable close per hour. Parameter
  tuning cannot fix this (see §5) — only more market families can.
- **Size** is capped at `per_event_cap_usd = $25`, and the book often gave less than that.

## 4. MEASURED: how much size the book actually supports

At each of 87 OOS signal moments, the real top-of-book depth on the side we would buy
(1-second-latency fill, single taker lifting displayed size):

| available notional | $ |
|---|---|
| median | **11.40** |
| mean | 46.80 |
| p75 / p90 | 50.70 / 109.80 |
| max | 765.60 |

Raising the per-trade cap, holding everything else fixed, over the same 87 OOS trades:

| cap | avg actually filled | % of trades filling the full clip | total OOS P&L |
|---|---|---|---|
| **$25 (current)** | $13.50 | 34% | **$157.78** |
| $50 | $21.10 | 25% | $258.42 |
| $100 | $30.10 | 11% | $511.23 |
| **$250** | $39.10 | 3% | **$831.67** |
| $500 | $43.80 | 1% | $1,050.26 |
| $1000 | $46.80 | 0% | $1,169.30 |

**$25 → $250 is a ~5.3× P&L increase** while average fill only rises 2.9× — because the
deep-book moments are also the *higher-edge* moments:

| depth quartile | n | EV/share | median depth |
|---|---|---|---|
| thin | 23 | 5.33¢ | $1.85 |
| 2 | 21 | 10.52¢ | $5.83 |
| 3 | 21 | 5.69¢ | $22.22 |
| **deep** | 22 | **17.41¢** | $90.10 |

Size is **not** adversely selected here — the opposite. Beyond ~$250 returns flatten (only 1–3%
of trades can absorb it), so **$250 is the sensible ceiling** for this family.

*Caveats*: top-of-book only (1h has no depth snapshots in the vault), assumes we lift all
displayed size as the only taker, and the quartile split is n≈22 per bucket.

## 5. MEASURED: parameter frontier (OOS, 1,740 closes, May 1 – Jul 12)

| signal → fill timing | edge_min | trades/day | EV/share | t-stat |
|---|---|---|---|---|
| **−2s → −1s** | 0.02 | 0.68 | 17.41¢ | **5.8** |
| **−2s → −1s** | 0.03 | 0.64 | 18.23¢ | **5.8** |
| −2s → −1s | 0.05 | 0.51 | 18.81¢ | 5.0 |
| −2s → −1s | 0.12 | 0.33 | 25.14¢ | 5.1 |
| −5s → −2s | 0.03 | 0.66 | 14.05¢ | 3.6 |
| −10s → −5s | 0.02 | 1.03 | 8.36¢ | 2.2 |
| −30s → −15s | 0.05 | 0.99 | **−7.00¢** | −1.4 |

Two clear results:

1. ~~**Later is dramatically better.** Sniping at −2s/−1s yields 17–25¢/share at t≈5–6; at
   −30s/−15s the edge is *negative*. The current bot fires on the **first** qualifying second
   inside a 6-second window, so it can take the weaker early end of that range.~~
   **SUPERSEDED — see the correction banner at the top.** Half right: the *decay outside ~10s*
   is confirmed and is stronger than stated. But the −2s/−1s cell assumed a 1 s signal→fill gap
   and a signal book identical to the fill book; at the shipped 1.5 s gap `tau = 1` fills 0/46,
   and there is no significant gradient inside `tau = 2…6`. The correct rule is a **two-sided**
   band `tau ∈ [2.0, 5.0]`, which is what ships.
2. **Lowering `edge_min` to 0.02–0.03 is a modest net win**: +26% frequency for −7% EV
   (best expected ¢/day at −2s→−1s, edge_min 0.02–0.03).
3. **Frequency cannot exceed ~1 trade/day on 1h at any parameter setting.** Confirmed across
   the whole grid.

## 6. Ranked levers

| # | lever | effect | work | risk |
|---|---|---|---|---|
| 1 | `per_event_cap_usd` $25 → $250 | ~**5×** P&L (measured, §4) | one config line | more capital at risk per trade; 85% win rate means real drawdowns |
| 2 | ~~Snipe only in the last **2–3s**~~ → **bound the window at both ends, `tau ∈ [2.0, 5.0]`**, and `edge_min` → 0.03 | timing: **P&L-neutral at $25** (CI straddles zero) — it is the *precondition for lever 1*, not a standalone lever. `edge_min` 0.03: +22% fills, +11% P&L | small code/config change | −10% signals, −5% fills; "last 2–3s" as originally written is **value-destroying** (see correction banner) |
| 3 | **Wire a Chainlink feed → unlock 5m/15m** | 5m backtested at **+14¢/share, ~52 trades/day** — a **~70× frequency** increase | real work: Data Streams or on-chain aggregator; **not yet scoped** (workflow died) | basis risk already burned us once (the 3 poisoned fills); must be a true Chainlink read, never a Binance proxy |
| 4 | Retire `settle_sweep` on 1h | none (0 fills in 1,235 tries) | config | none — it is pure logging now |

**Combined estimate for levers 1+2 on the 1h family: roughly $16/day** vs today's $4.64/day.
Meaningful money requires lever 3.

## 7. Do NOT change

- Don't widen the snipe window earlier than ~5s before close — the −30s bucket is **negative EV**.
  **Strengthened by A4 and now shipped as `snipe_last_secs: 5`:** it also means *don't sit at 6s*.
  And don't narrow the *late* end either — there is a hard floor at
  `tau_lo = max(2.0, latency_ms/1000 + 0.5)`, because an order that arrives at/after the close
  cannot fill (0/46) and in LIVE would be a real FAK order into a closed market.
- Don't re-enable `settle_sweep` on 5m/15m/4h without a real Chainlink feed (that combination
  produced 3-for-3 losing fills; see the adverse-selection finding in `bot/README.md`).
- Don't remove `fair_cap` / the 3¢ walk bound — they exist because of the −$25 tail loss.

## 8. Go/no-go framework for real money

- **Need ~20+ resolved trades** (currently 7). At ~0.7/day on 1h that is ~3 more weeks — or days
  if lever 3 lands.
- **Threshold**: EV/share holding above ~+8¢ with a t-stat > 2 across the full sample.
- **Starting size if it passes**: $25–50/trade, i.e. well below the measured $250 ceiling.
- **Falsifier**: win rate drifting below ~70%, or EV/share turning negative over any rolling
  20-trade window → stop and re-audit.

## 9. Still outstanding (blocked by the session limit)

- Telonex shadow-replay of the live Jul 16–26 window to confirm live fills match the logic exactly.
- Chainlink Data Streams vs on-chain aggregator feasibility study (lever 3).
