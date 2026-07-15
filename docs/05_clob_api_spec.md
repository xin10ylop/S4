# Polymarket CLOB API — Live-Verified Integration Spec (2026-07-15)

Verified live against gamma-api.polymarket.com and clob.polymarket.com, including a 4-minute
observation of a real 5m BTC market crossing its close.

## Discovery
GET https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=endDate&ascending=true&end_date_min=<ISO-now>
Filter slugs by: btc-updown-5m- / btc-updown-15m- / btc-updown-4h- / bitcoin-up-or-down-
- end_date_min is REQUIRED (without it, thousands of stale never-closed markets surface).
- Do NOT sort by startDate: markets are pre-created ~24h ahead of their window.
- Direct: /markets?slug=<slug>  and /events?slug=<slug>.

## Timeline (scheduling)
- Use Gamma endDate / events[].endDate for the window CLOSE (second-precise). Also eventStartTime/events[].startTime for open.
- Do NOT use CLOB end_date_iso (date-only, midnight) or game_start_time (sports only).
- Slug <unix_ts> for short families = window START.
- 4h markets sometimes carry events[].eventMetadata.priceToBeat = exact opening reference price (not always present).

## Token mapping
clobTokenIds[0]=Up (outcome_0), [1]=Down. Order always matches outcomes array.

## Order book
GET https://clob.polymarket.com/book?token_id=<id>
GOTCHA: bids sorted ASCENDING (best bid = bids[-1]); asks sorted DESCENDING (best ask = asks[-1]).
Batch: POST /books  body [{"token_id":...}]
No-auth helpers: /price?token_id=&side=, /midpoint, /spread, /tick-size, /neg-risk, /fee-rate, /time.

## WebSocket (no auth)
wss://ws-subscriptions-clob.polymarket.com/ws/market
subscribe: {"assets_ids":[<id>,...],"type":"market"}
Heartbeat: send text "PING" every ~10s idle -> "PONG".
Events: book (snapshot), price_change (batched, includes best_bid/best_ask), last_trade_price, market_resolved.
EMPTY-BOOK SIGNAL: price_change with best_bid:"0"/best_ask:"1" (or empty book snapshot) = liquidity gone.
This fires MINUTES before REST closed/accepting_orders flags flip. Use it, not the REST flags.

## Settlement mechanics (empirically observed on a live 5m close)
- Book reprices toward 0.01/0.99 within a handful of seconds of close (sometimes before, from the live feed).
- Real fills continue to ~T+26s; accepting_orders=true, closed=false persist for MINUTES (misleading).
- ~T+90-100s: bulk-cancel wipes the entire book; nothing left to sweep.
- => Settlement "buy winner cheap" is a single-digit-second race. Confirms docs/04_executability_audit.md.

## Orders (py-clob-client 0.34.6)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON  # 137
client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=POLYGON,
                    signature_type=1, funder=PROXY_WALLET_ADDR)  # sigtype 1 = POLY_PROXY (web magic wallet)
creds = client.create_or_derive_api_creds(); client.set_api_creds(creds)  # L2
# Sweep resting asks as taker IOC -> use FAK (partial fills; FOK is all-or-nothing):
mo = MarketOrderArgs(token_id=TOKEN_ID, amount=25.0, side=BUY)  # $ on BUY
signed = client.create_market_order(mo)
resp = client.post_order(signed, orderType=OrderType.FAK)
# Auth: L0 none (books/prices), L1 private-key sign (create order, derive key), L2 HMAC (post/cancel/get orders).
# LATENCY: create_*_order() always fetches tick_size + neg_risk + fee_rate (3 GETs). Pre-warm these caches
#   at market discovery so the time-critical order doesn't block. tick_size caches 300s; others indefinitely.

## Fees
Gamma feeSchedule: {"exponent":1,"rate":0.07,"takerOnly":true,"rebateRate":0.2} => taker-only, maker 0, rate 0.07.
Per-market base_fee=1000 (bps-like) auto-attached by SDK; exact base_fee<->0.07 relation unconfirmed —
verify the exact formula before real money. Model used in research: taker = 0.07*p*(1-p) per share.
