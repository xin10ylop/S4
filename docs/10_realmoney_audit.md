# 10 — Real-Money Readiness Audit

**Date:** 2026-07-31 · **Trigger:** owner is preparing to fund a live wallet.
**Live sample under audit:** 4 signals · 3 fills · 539.05 shares · $406.10 cost · **+$126.07** ·
win_rate 1.000 · EV/share **$0.2339** · 306 eval ticks.
**Backtest under audit:** `data/multicoin/m3/trades_shipped.parquet`, bitcoin 1h, shipped params,
66 fills over 79 calendar days (2026-05-08 → 07-25).

---

## 0. Verdict

**DO NOT FUND YET.** Two independent audits (this one and a second by a different model with no
shared context, §6b) reached the same verdict for overlapping reasons. The **edge survives every
attack** either of us ran. What is not ready is everything around it: the live order path has never
executed once, the wallet path does not exist, and the live sample is n=3.

| | |
|---|---|
| Is the edge real? | **Yes, on 66 backtest fills.** t_day 2.25, win 87.9%, +$6.46/day at the $250 cap. |
| Does the live n=3 confirm it? | **No — n=3 confirms nothing.** 95% CI on the win rate is [0.292, 1.000]. §1. |
| Is the live sample even auditable? | **No.** The tape is not in this repo — `polybot.db` is empty, `fills.csv` is header-only, `git ls-files bot/data` is empty. §6b.1. |
| Confirmed on live fills | **The fee model.** Realised 1.30¢/share vs `0.07·p·(1−p)` = 1.30¢ at p = 0.7534. §1. |
| Fixed in this pass | \|z\| gate · live fill-quantity parsing · partial-fill truncation · exchange-minimum enforcement · orphaned-position recovery. §6c. |
| Still blocking | Rejected orders are swallowed; no approval/balance/**redemption** code; `post_order` schema, `fee_rate_bps` and real fill size all unverified; live fills ran **4× fatter** than the depth model (p = 0.0092). §3, §6b, §8. |
| Max bet per trade | **$250** (captures 97.9% of available P&L). $400 captures 100%. Above $400: **zero**. §5. |
| Does it compound? | **Barely, and only up to ~$1,000 of bankroll.** Above that the order book binds on 92–100% of trades and $/day is frozen at ~$6.6. §6. |
| First live clip | **$25**, not the shipped $250 — which would go live at 10× the repo's own recommendation. §6b.6. |

---

## 1. The live sample: what 3/3 actually proves

Nothing. Stated precisely:

| statistic | value |
|---|---|
| wins | 3 / 3 |
| Clopper-Pearson 95% CI on the win rate | **[0.292, 1.000]** |
| one-sided 95% lower bound | **0.368** |
| P(3/3 \| true win rate = 0.50) | **0.125** — not significant at any conventional level |
| P(3/3 \| true win rate = 0.9615, the backtest figure) | 0.89 |
| P(≥1 loss in 3 \| true win rate = 0.90) | **0.271** |

A fair coin produces 3/3 one time in eight. The result is *consistent* with the backtest and would
have been consistent with a coin flip. There was a 27% chance of a red trade in those three and it
did not land — that is luck, not confirmation.

**The +$126 headline is likewise a small-sample artifact.** Realised EV/share was **$0.2339** against
a backtest expectation of **$0.1332** — running 1.76× hot, exactly what 3/3 with no losers looks
like. The 5th-percentile 30-trade outcome at this clip is **+$57**, the median **+$221**.

**One thing the live sample does confirm, and it matters:** the fee model. Realised
`1 − 0.7534 − fee = 0.2339` implies a fee of **1.30¢/share**; the config formula
`0.07 × p × (1−p)` at p = 0.7534 gives **1.30¢**. The fee schedule is right to four decimals on
real fills. That was worth checking and it passed.

**Against the pre-committed go/no-go bar** (≥30 resolved trades, EV/share > +8¢, t > 2, win > 70%):
3 of 30 trades. **10% of the way there.** The bar has not moved.

---

## 2. Blocking defect 1 — the live ledger over-reports (`execution.py:250-258`)

```python
# NOTE (see module docstring): we deliberately do NOT parse a "filled
# shares" number out of `resp` — its schema is unverified here. We
# record the *intended* walk as an upper bound and outcome="filled"
# only when post_order did not raise
```

The order is a **FAK** (fill-and-kill). FAK fills whatever is available at match time and cancels
the rest. So the three live-mode failure modes are:

1. **Partial fill** — book thinned between our `GET /book` and the match. Ledger says 351 shares;
   you own 40. Every downstream number (P&L, win rate, the circuit breaker's daily-loss counter,
   `max_open_notional`) is computed off a position you do not have.
2. **Zero fill** — `post_order` returns 200 with an empty match. Ledger records a full winning
   trade that never existed.
3. **Worse average price** — the ledger assumes the walk's price ladder; the match engine may
   have filled only the top level.

All three inflate reported P&L and all three are silent. The circuit breaker cannot protect a
position it is mis-measuring.

**This is not a hypothetical.** The whole point of the live audit is that this path has *never run
once*. Also unverified, per the same module's docstring (`execution.py:44-63`): the `post_order`
response schema, and whether `OrderArgs.fee_rate_bps` (shipped at the default `0`) must echo the
market's `takerBaseFee` or is ignored server-side. If the client must echo it and sends 0, orders
may be rejected — or may fill at a fee we did not model.

**Fix before funding:** place one real order at the smallest legal size (5 shares, ~$4), log the raw
`resp`, wire real fill-quantity parsing, and reconcile against the on-chain balance. This is a
~$4 experiment that closes a defect no amount of paper trading can close.

---

## 3. Blocking defect 2 — live fills are 4× fatter than the depth model

| | backtest (5-level snapshots, $250 cap) | live (3 fills) |
|---|---|---|
| mean notional / fill | **$33.32** | **$135.37** |
| median notional / fill | $10.51 | — |
| mean shares / fill | 44.6 | **179.7** |
| max notional observed | $250.00 (cap) | $250.00 (cap) |
| fraction of fills hitting the $250 cap | **1.5%** | **33%** (1 of 3) |

Bootstrapping 3 draws from the backtest fill-size distribution 200,000 times:
**P(mean ≥ $135.37) = 0.0092.** The live fills are significantly larger than the model says they
should be.

There is a **benign explanation already on record** (`docs/07_scale_audit.md` §1): the live bot walks
the *full* CLOB ladder while the vendor snapshots stop at 5 levels, so the backtest is a known lower
bound. But that effect was previously measured at **1.5×** (214.69 live shares vs 144.65 replay),
not 4×. The gap between 1.5× and 4× is unexplained.

The two readings are not equivalent:

- **If the depth is real**, every backtest capacity number in this repo is a floor, the max bet in §5
  scales up ~4×, and $/day is understated.
- **If the paper fill engine is over-filling**, then the headline P&L is inflated by roughly the same
  factor and the strategy is a ~$1.6/day strategy, not a $6.5/day one.

n = 3 cannot distinguish these. **This is the single highest-value thing to measure next**, and the
$4 live probe in §2 measures it directly: submit a FAK sized off the walk and compare filled shares
to intended shares.

To pull the evidence off the server (I have no SSH from this container):

```bash
ssh root@165.227.83.238 "cd /opt/polybot && \
  python3 -c \"import csv,sys; r=list(csv.DictReader(open('data/fills.csv'))); \
  [print({k:v for k,v in x.items() if k in ('ts','slug','side','fair','z','sigma','ask','shares','cost','outcome')}) for x in r]\""
```

The `z` and `fair` columns on those 3 fills are what §4 needs to know whether the live wins were in
the good band or lucky ones in the bad band.

---

## 4. The free improvement: gate `|z| ≤ 5`

Bitcoin, shipped params, 66 fills, $250 cap, 79 calendar days:

| bucket | n | win | EV/share | total P&L |
|---|---|---|---|---|
| \|z\| ≤ 5 | 52 | **96.15%** | **+20.38¢** | +$512.87 |
| \|z\| > 5 | 14 | **57.14%** | **−12.92¢** | −$2.63 |

**Break-even win rate at the live average ask of 0.7534 is 76.64%.** The `|z| > 5` bucket runs at
**57.14%** — that is not a marginal bucket, it is 19 points below water.

Gate comparison at the $250 cap:

| gate | n | win | total P&L | $/day | worst single trade |
|---|---|---|---|---|---|
| **SHIPPED (no gate)** | 66 | 87.88% | $510.24 | $6.46 | **−$45.26** |
| **\|z\| ≤ 5** | 52 | **96.15%** | **$512.87** | **$6.49** | **−$25.47** |
| \|z\| ∈ [1, 5] | 45 | 97.78% | $492.54 | $6.23 | −$15.87 |
| fair < 0.98 (drop all saturated) | 28 | 96.43% | $169.14 | $2.14 | −$25.47 |

**The `|z| ≤ 5` gate is free.** It does not cost $0.03/day — it *gains* $0.03/day, which is noise;
the honest claim is **it costs nothing and removes 14 trades that deployed $487 of notional for a
net −$2.63 while producing the worst loss in the sample.** 30-trade bootstrap:

| | median | 5th pct | P(net loss) | median max DD | 5th-pct max DD |
|---|---|---|---|---|---|
| SHIPPED | +$221 | +$57 | 0.9% | −$25 | −$71 |
| **\|z\| ≤ 5** | **+$282** | **+$117** | **0.0%** | **−$16** | **−$41** |

**Why this is a mechanism, not a data-mined threshold.** `fair` is clipped at `fair_cap = 0.98`.
That clip binds for every `|z|` above ≈2.05 — so the model reports *the identical fair value* for
|z| = 2.1 and |z| = 50. Past the clip the model carries **no discriminating information at all**;
the apparent "edge" at |z| = 50 is an artifact of the clip meeting a cheap ask. And a cheap ask at
|z| = 50 is the market telling you your sigma is wrong or your price feed is stale — classic adverse
selection. One live example from the sample (`trades_shipped.parquet` row 2): |z| = 6.53,
fair pinned at 0.98, ask **0.47**, apparent edge **0.49**, result **−$45.26**.

**The honest caveat, preserved from `audit/M3_multicoin_backtest.md` §4.3:** the Fisher p = 0.0055
for this split is **split-dependent** (p = 0.81 on the neighbouring cut), and the TRAIN-selected
band failed out of sample in §7.1. I am **not** claiming this effect is statistically established.
I am claiming the gate is worth shipping because it is **P&L-neutral in the measured sample and
mechanically motivated** — you do not need statistical proof to stop taking trades that cost you
nothing to skip.

**Rejected alternative:** gating on `fair < 0.98` (drop all saturated fills) tests at Fisher
p = 0.1245 and destroys **two thirds of the P&L** ($6.46 → $2.14/day). Saturation alone is not the
problem — saturation *at extreme |z|* is. Within the saturated fills, |z| ≤ 5 wins 95.8% (+15.6¢)
and |z| > 5 wins 57.1% (−12.9¢). Do not ship the saturation gate.

---

## 5. Max bet per trade

Three independent constraints. The binding one is the order book.

### 5.1 Book depth — the hard ceiling

Cap sweep, bitcoin, 66 fills (per-share P&L is cap-independent, so this is exact):

| cap | notional deployed | total P&L | $/day | return on notional | % of fills where the cap binds |
|---|---|---|---|---|---|
| $10 | $482 | $109.59 | $2.61 | 22.7% | 51.5% |
| $25 | $855 | $170.30 | $2.16 | 19.9% | 28.8% |
| $50 | $1,269 | $261.74 | $3.31 | 20.6% | 21.2% |
| $100 | $1,736 | $421.27 | $5.33 | 24.3% | 9.1% |
| **$250** | **$2,199** | **$510.24** | **$6.46** | **23.2%** | **1.5%** |
| $400 | $2,234 | $521.39 | $6.60 | 23.3% | 0.0% |
| $1,000 | $2,234 | $521.39 | $6.60 | 23.3% | 0.0% |
| $10,000 | $2,234 | $521.39 | $6.60 | 23.3% | 0.0% |

**$250 captures 97.9% of every dollar the book will give you. $400 captures 100%. Above $400 the
extra capital does literally nothing** — the largest single fillable notional in 66 fills was
$285.29. Return on deployed notional is *flat* at ~23% from $10 to $400, so there is no measurable
price-impact decay inside that range; the ladder within `max_walk_above_best = 0.03` is simply
shallow.

### 5.2 Kelly — the risk ceiling

Per share at the live average ask of 0.7534: cost $0.7664, win pays $0.2336, lose costs $0.7664 —
a **3.28 : 1 loss-to-win ratio**, so this is a high-win-rate negative-skew bet where being wrong
about the win rate is expensive.

| assumed win rate | source | full Kelly | quarter Kelly |
|---|---|---|---|
| 96.15% | \|z\| ≤ 5 point estimate (n = 52) | 83.5% | 20.9% |
| **86.79%** | **Clopper-Pearson 95% lower bound on 50/52** | **44.3%** | **11.1%** |
| 87.88% | all BTC fills (n = 66) | 48.2% | 12.1% |
| 80.00% | stress | 14.4% | 3.6% |
| 76.64% | break-even | 0% | 0% |

**Use quarter Kelly on the 95% lower bound: 11.1% of bankroll.** Full Kelly here is a fantasy —
it assumes the win rate is known, and it is estimated from 52 trades.

### 5.3 The answer

**Max bet per trade = min( 11.1% of bankroll , what the book gives inside a 3¢ walk , $250 ).**

| bankroll | quarter-Kelly cap | effective avg fill | $/day | % of trades limited by the BOOK, not your bankroll |
|---|---|---|---|---|
| $100 | $11 | $8.01 | $1.68 | 51.9% |
| $250 | $28 | $13.44 | $2.51 | 75.0% |
| $500 | $56 | $19.72 | $3.84 | 80.8% |
| $1,000 | $111 | $27.24 | $5.73 | 92.3% |
| **$2,250** | **$250** | **$32.91** | **$6.49** | **98.1%** |
| $5,000 | $555 | $33.60 | $6.63 | 100.0% |
| $10,000 | $1,110 | $33.60 | $6.63 | **100.0%** |

**$250 is the correct shipped cap and it is reached at a bankroll of ~$2,250.** Beyond $2,250,
additional capital earns exactly $0.

**Caveat that could move this 4×:** all of §5.1 is measured on 5-level vendor snapshots. If §3
resolves in favour of "live depth is genuinely deeper", the ceiling moves to roughly $1,000–1,600
and the bankroll ceiling to ~$9,000–14,000. **Do not size on that hope until §3 is closed.**

---

## 6. Does it compound?

**Structurally, almost no — and the reason is §5.**

Compounding requires that your position size be limited by your *capital*. Here it is limited by the
*order book*, and it is limited by the order book **from the very first dollar**: at a $100 bankroll
under quarter-Kelly, the book — not your bankroll — is already the binding constraint on **51.9%** of
trades. By $1,000 it is 92.3%. By $3,000 it is 100%.

`$/day` as a function of bankroll: $1.68 → $2.51 → $3.84 → $5.73 → **$6.63, and then flat forever.**

Return on capital collapses accordingly: **65%/month at $100 → 18.7% at $1,000 → 9.0% at $2,250 →
2.0% at $10,000.**

Fixed-fraction simulation of the brief's original path (5% of bankroll, start $100, 200 trades ≈ 9
months at 0.66 fills/day, resampling the shipped BTC fills):

| config | median | 5th pct | 95th pct | P(end < $100) |
|---|---|---|---|---|
| SHIPPED (no gate) | $449 | $310 | $619 | 0.0% |
| **with \|z\| ≤ 5 gate** | **$689** | **$519** | **$894** | **0.0%** |

That is a genuine 4.5× (6.9× gated) — but read it correctly: it is large *because the starting
bankroll is far below the book ceiling*. The same simulation started at $2,500 returns roughly a flat
$6.6/day, or ~8%/month and falling.

**Note where the `|z| ≤ 5` gate pays off most.** In flat-cap terms it is worth $0.03/day (§4 —
nothing). In *compounding* terms it is worth **+53% on the 9-month median** ($449 → $689). The
reason: while the bankroll is the binding constraint, what compounds is **return on notional**, and
the gate lifts that from 23.2% to 30.0%. Dollar-neutral changes are not return-neutral once you
compound. This is the strongest argument for shipping the gate.

**Practical recommendation:** wire compounding **only over the $100 → ~$2,000 ramp**, as
`per_event_cap_usd = min(0.111 × bankroll, 250)`, re-evaluated weekly rather than per trade (per-trade
re-sizing adds path dependence for no gain when the book binds 50–98% of the time). Above ~$2,250,
**freeze the cap at $250 and withdraw the profit** — leaving it in the account does nothing.

`src/bankroll.py` exists and implements fixed-fraction projection but is **not wired into the bot**;
the bot reads the flat `sizing.per_event_cap_usd` from `bot/config.yaml:358`. Given the above, that
is close to the right design already — the only change worth making is the ramp.

**Honest limit on all of §6:** the bootstrap can only resample regimes it has seen. 66 fills over 79
days contains no adverse regime — no exchange outage, no oracle divergence, no period where the
counterparty flow got smarter. `P(net loss) = 0.0%` over 30 trades is a statement about this sample,
not about the world.

---

## 6b. Independent second audit (different model, no shared context)

A second auditor was run against the same repo with no access to my analysis and instructions to
find reasons the strategy loses real money. It reached the **same verdict — DO NOT FUND YET** — and
independently confirmed the two blockers above. It also found **five defects neither the prior work
nor my pass had caught**, four of which are now fixed (§6c).

**Where it corrected me.** I wrote in an earlier draft that an average fill price of $0.7534 against
a realised EV/share of $0.2339 might indicate fair-value saturation. That inference is wrong:
$0.2339 is *realised* P&L on three winners and equals `1 − avg_price − fee` by construction, so it
carries **zero** information about model fair. Entry prices bound fair only weakly
(fair ≥ ask + fee + edge_min ⇒ fair ≥ ~0.79). Whether the live trades were saturated is answerable
only from the production `signals` rows. The saturation question is real (§4) but the price
arithmetic does not speak to it.

**Where it was right and I had missed it entirely:**

1. **The live tape is not in this repo.** `bot/data/polybot.db` has 0 rows in `signals`/`fills`/
   `resolutions`; `fills.csv` and `pnl.csv` are header-only; `polybot.log` covers 07-27→07-28 with
   3 eval lines and no fills. The only archived tape holds 115 **settle_sweep** attempts — the dead
   strategy's record survived and both winning close_snipe tapes did not. `git ls-files bot/data`
   is empty: the ledger has never been committed. **Every live number in this audit is therefore
   unverifiable from the repo**, mine included. The pasted figures are internally consistent
   (§1 fee check) but consistency is not provenance.
2. **Orphaned positions on restart** — a genuine live-money hole. Fixed, §6c.
3. **`order_min_size` (5) and `tick_size` parsed but never enforced** — the paper walk books fills
   of 2.4 shares that the CLOB rejects. Fixed, §6c.
4. **Rejected orders are swallowed.** `_run_fill` catches the exception and logs; **no fill row is
   written at all**. A live bot with no USDC allowance signals-and-errors forever and the ledger
   reads "no signals converted", not "orders rejected".
5. **No approval / balance / redemption code anywhere.** Winning CTF shares are never redeemed, so
   USDC drains while the bot "wins". Not fixed — it needs a funded wallet to build against.
6. **The shipped `per_event_cap_usd: 250` is a live-mode trap.** Flipping `mode.paper` today would
   start real trading at **10× the repo's own recommended first clip** ($25–50). Nothing in code
   enforces that the $250 was justified on *paper* evidence.
7. **ETH shadow evaluation shares BTC's tick.** Hourly markets for both coins close at the same
   instant, so the shadow coin's blocking book fetch (~155 ms) sits inside BTC's snipe tick, eating
   the very tau margin `snipe_min_tau_secs: 2.5` exists to protect.
8. **Status server binds `0.0.0.0` unauthenticated** on a root-run box.

**Where I disagree with it, on measurement.** It proposed *size-capping* saturated-fair signals
rather than vetoing high-|z| ones, on the grounds that the z-band rule failed walk-forward. I tested
both on the 66 BTC fills:

| policy | P&L | $/day | worst trade | return on notional | 30-tr 5th pct | 5th-pct max DD |
|---|---|---|---|---|---|---|
| SHIPPED flat $250 | $510.24 | $6.46 | −$45.26 | 23.2% | +$57 | −$71 |
| **hard veto \|z\|>5** | **$512.87** | **$6.49** | **−$25.47** | **30.0%** | **+$117** | **−$37** |
| size-cap $50 when \|z\|>5 | $491.12 | $6.22 | −$45.26 | 24.4% | +$53 | −$70 |
| size-cap $25 when \|z\|>5 | $495.67 | $6.27 | −$25.91 | 26.0% | +$61 | −$51 |
| size-cap $50 when fair pinned | $316.22 | $4.00 | −$45.26 | 19.9% | +$23 | −$71 |

**The hard veto dominates every size-cap variant on every column.** Size-capping only helps when the
cap sits *below* the trade's actual notional, and the worst trade (|z| = 6.53, $43.67 notional) is
untouched by a $50 cap. Its concern — that the z-band rule failed walk-forward — is why the config
comment ships the honest caveat rather than a significance claim, and why the gate is justified on
being *free* rather than on being *proven*.

---

## 6c. What was fixed in this pass

| # | defect | fix | tests |
|---|---|---|---|
| 1 | No \|z\| gate; \|z\|>5 traded at full $250 clip | `max_abs_z: 5.0`, enforced in `evaluate_close_snipe` after the sigma floor, two-sided, `null` disables. New `snipe_z()` shares the exact quantity `fair_value_up` feeds the CDF. | 8 behavioural + 5 mutations |
| 2 | Live path recorded the **intended** walk as `filled` | `_parse_filled_size()` searches known field names and returns **None** rather than guessing; unknown ⇒ `outcome="filled_unverified"` + `log.error`; zero match ⇒ `book_moved_no_edge`; partial ⇒ `walk.truncated_to_shares()` | 10 behavioural + 5 mutations |
| 3 | Partial fills would have kept the full walk's price ladder | `truncated_to_shares()` keeps the **cheapest** levels — the direction a real FAK fills — so a partial can only lower the average price, never flatter it | property test over 5 fill sizes |
| 4 | `order_min_size` parsed, never enforced | live path refuses sub-minimum orders with `outcome="below_min_size"` instead of submitting one the CLOB rejects | 1 mutation |
| 5 | Orphaned positions never resolved after a restart | `_recover_orphaned_market()` re-fetches with `closed=True`, caches into `known_markets`, rate-limited by `gamma_poll_secs`, fails soft on gamma errors | 4 behavioural + 3 mutations |

**A test-fixture defect surfaced by the gate, worth recording.** `StubBinance` defaulted to
`s_t=101_000` against `s_open=100_000` — a **1% BTC move with 3 seconds left**, which at
`sigma=1e-4` is **|z| ≈ 57**. Six gating tests and three shadow-mode tests were asserting on a
signal shape the shipped bot must now refuse. The fixture, not the gate, was wrong: it has been
moved to +$20 on $100k (|z| ≈ 1.2), inside the band that actually carries the edge. This is a
reminder that the old tests would have passed no matter how bad the high-|z| behaviour got.

Suite: **307 → 328 passing.** Mutation checker: 54 → 69 checks.

---

## 7. What I could not verify in this session

| item | status | how to close |
|---|---|---|
| **The live sample itself** | **UNVERIFIED — the tape is not in this repo at all** (§6b.1) | commit the production `polybot.db` + logs |
| The 3 live fills' `z` / `fair` / `sigma` | **UNVERIFIED** — no SSH from this container | the `fills.csv` command in §3 |
| `post_order` response schema | **UNVERIFIED** | one $4 live order |
| `fee_rate_bps` semantics | **UNVERIFIED** | same order's response |
| Actual filled vs intended shares under FAK | **UNVERIFIED** | same order, reconciled on-chain |
| Whether live depth is truly 4× the snapshot model | **UNVERIFIED** | ~20 live fills |
| Wallet allowance / approval flow for USDC + CTF | **NOT TESTED** | manual, before the first order |

---

## 8. Ordered plan to real money

**Done in this pass** (§6c): `|z| ≤ 5` gate, live fill-quantity parsing with partial-fill
truncation, exchange-minimum enforcement, orphaned-position recovery.

**Still blocking, in order of dollars at risk:**

1. **Commit the production tape.** `polybot.db` + logs. Until then no live claim in this repo —
   including the +$126 — is verifiable, and the |z| question about the 3 wins cannot be answered.
   One query answers it:
   ```sql
   SELECT market_slug, fair, ask,
          json_extract(meta_json,'$.s_t')       AS s_t,
          json_extract(meta_json,'$.s_open')    AS s_open,
          json_extract(meta_json,'$.sigma_1s')  AS sigma,
          json_extract(meta_json,'$.tau_secs')  AS tau
   FROM signals;   -- then z = ln(s_t/s_open) / (sigma*sqrt(tau))
   ```
   If any of the three wins was |z| > 5, treat +$126 as luck from the bucket that runs −12.9¢/share.
2. **Record rejected orders.** `_run_fill` currently swallows the exception (§6b.4). A live bot with
   no USDC allowance would look identical to a quiet market. Make rejection a ledger outcome.
3. **Build the wallet path** — USDC approval, CTF approval, balance check at startup, and
   **redemption of winning shares** (§6b.5). None of it exists. Without redemption the account
   drains while the bot wins.
4. **One $4 live order** (5 shares). Capture the raw response. This single order resolves: the
   `post_order` schema (and lets the guessed field list in `_FILLED_SIZE_KEYS` be replaced with the
   real name), `fee_rate_bps`, and intended-vs-actual fill size — which is the §3 question.
5. **Set `per_event_cap_usd: 25` before flipping `mode.paper`** (§6b.6). The shipped $250 was
   justified on paper evidence only; it must not be the number that goes live first.
6. **Run live at $25 until 30 resolved trades.** At 0.66 fills/day that is **~45 days**.
   Bar: EV/share > +8¢, t_day > 2, win rate > 70%.
7. **Only then** step to $100, and to $250 after another 30.
8. **Never exceed $250** until §3 is resolved in favour of deeper live books.

Items 1–3 cost nothing but time. Item 4 costs $4. There is no reason to pay real dollars to
shortcut item 6 — the paper bot is collecting that evidence for free right now.

Expected honest outcome if everything holds: **~$6.50/day, ~$195/month, from ~$2,250 of working
capital.** That is an excellent *return on capital* and a small *absolute* number. Both facts are
true and the second one is the one that gets forgotten.
