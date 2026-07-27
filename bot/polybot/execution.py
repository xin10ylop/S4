"""Order execution router: PAPER (simulated fills) vs LIVE (real orders).

Real-money orders require ALL of the following, checked in this order:
  1. config.yaml `mode.paper: false`
  2. environment variable POLYBOT_LIVE=1
  3. environment variable POLYBOT_PK (a funded wallet's private key) present
Any single guard missing => the bot stays in (or refuses to leave) paper mode.
See Config.paper for guards 1+2; guard 3 is enforced here in _get_live_client,
which is only ever called once is_live() has already confirmed 1+2.

LIVE ORDER PATH — VERIFICATION STATUS
--------------------------------------
This build's `py-clob-client` (0.34.6) is installed in the repo's Python
environment, so the call sequence below was written against the REAL,
installed package source (py_clob_client/client.py, clob_types.py,
order_builder/constants.py, and py_order_utils/model.py for signature-type
constants) — not guessed or copied from possibly-stale docs:
  - `ClobClient(host, chain_id, key, creds=None, signature_type=None, funder=None, ...)`
  - `client.create_or_derive_api_creds() -> ApiCreds`, `client.set_api_creds(creds)`
  - `OrderArgs(token_id, price, size, side, fee_rate_bps=0, nonce=0, expiration=0, taker=...)`
  - `client.create_order(order_args) -> SignedOrder`
  - `client.post_order(signed_order, orderType=OrderType.FAK)` -> POSTs to /order,
    returns `resp.json()` (a plain dict) from the CLOB REST API.
  - `BUY`/`SELL` from `py_clob_client.order_builder.constants`.
  - Signature types: EOA=0, POLY_PROXY=1 (email/magic wallet), POLY_GNOSIS_SAFE=2
    (browser wallet proxy) — from `py_order_utils.model`.

Also verified (source-inspected, same session): `client.create_order()` and
`client.create_market_order()` both internally call `__resolve_tick_size()`
and `get_neg_risk()`, which are network GETs the FIRST time a token_id is
seen (results cached — `get_tick_size` for `tick_size_ttl` seconds, default
300, per the `ClobClient(..., tick_size_ttl=300.0)` constructor arg). For a
latency-sensitive taker order this matters: `warm_cache()` below proactively
calls the public `client.get_tick_size(token_id)` / `client.get_neg_risk(token_id)`
methods (which populate the same cache dicts `create_order` reads) at market
DISCOVERY time rather than at signal time, so the time-critical order path
doesn't pay for those GETs. There is also a `MarketOrderArgs`/`create_market_order`
API (`amount` = USD notional to spend, `order_type` defaults to FOK — override
to `OrderType.FAK` for partial-fill-ok) as a simpler alternative to
`OrderArgs`/`create_order`; we do NOT use it because it has no concept of our
per-level `edge_min` cutoff (it would spend the full notional even into
unprofitable depth), whereas our own `walk_asks`-based sizing respects it.

NOT verified end-to-end: this sandbox has no funded Polygon wallet / private
key, so the actual submit -> match -> fill flow has never been exercised
against the real CLOB. In particular:
  - The **exact JSON schema of the `post_order` response** (field names for
    order id, match status, filled size) is not confirmed here — the code
    below submits the order correctly and records the raw response, but does
    NOT attempt to parse a "filled shares" figure out of it, because
    guessing that field name would be fabrication.
  - `OrderArgs.fee_rate_bps` is left at its default (0) below. The live
    gamma market payload carries `takerBaseFee`/`makerBaseFee` (observed:
    1000 on a real market) and a `feeSchedule` (`{"rate": 0.07, "exponent":
    1, "takerOnly": true}` — matching `fees.fee_rate` in config.yaml), but
    the exact relationship between those and `OrderArgs.fee_rate_bps` (does
    the server apply its own schedule regardless of what the client sends,
    or must the client echo it?) was not confirmed against a real order
    response, so we do not guess a value here.
Before this path is used for real money: (a) test on a small size manually
first, (b) inspect a real response payload and wire proper fill-quantity
parsing into `_place_live_order`, (c) resolve the `fee_rate_bps` question
above against a real fill.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config
from .fill_engine import FillAttempt, WalkResult, execute_taker_signal, walk_asks
from .logging_setup import get_logger
from .polymarket import ClobClientREST

log = get_logger("execution")


class LiveTradingDisabled(RuntimeError):
    """Raised when LIVE mode is requested but a required guard is missing."""


class ExecutionRouter:
    def __init__(self, config: Config, clob_rest: ClobClientREST):
        self.config = config
        self.clob_rest = clob_rest
        self._live_client = None

    def is_live(self) -> bool:
        return not self.config.paper

    def place_taker_buy(
        self,
        token_id: str,
        side: str,
        edge_fn: Callable[[float], float],
        edge_min: float,
        price_min: float,
        price_max: float,
        cap_usd: float,
        latency_ms: int,
        book_at_signal=None,
        max_level_shares: Optional[float] = None,
        anomalous_mode: str = "cap",
    ) -> FillAttempt:
        """Unified entry point used by the engine for both strategies.

        PAPER: waits `latency_ms`, re-fetches the live book, and walks it
        (fill_engine.execute_taker_signal) — this is the faithful simulation.
        LIVE: places one real FAK (fill-and-kill / IOC) taker order sized off
        a fresh book walk. See module docstring for verification status.

        `max_level_shares`/`anomalous_mode` are the adverse-size filter (M4
        guard 1) and are threaded into BOTH paths on purpose — a risk guard
        that only exists in the simulator is not a risk guard.
        """
        max_above_best = self.config.execution_cfg.get("max_walk_above_best", 0.03)
        if max_above_best is not None:
            max_above_best = float(max_above_best)
        if self.is_live():
            return self._place_live_order(token_id, side, edge_fn, edge_min, price_min,
                                           price_max, cap_usd, max_above_best,
                                           max_level_shares=max_level_shares,
                                           anomalous_mode=anomalous_mode)
        return execute_taker_signal(
            self.clob_rest, token_id, side, edge_fn, edge_min, price_min, price_max, cap_usd,
            self.config.fee_rate, latency_ms, book_at_signal=book_at_signal, sleep=True,
            max_above_best=max_above_best, max_level_shares=max_level_shares,
            anomalous_mode=anomalous_mode,
        )

    def _get_live_client(self):
        if self._live_client is not None:
            return self._live_client
        pk = os.environ.get("POLYBOT_PK")
        if not pk:
            raise LiveTradingDisabled(
                "LIVE mode requires POLYBOT_PK (a funded wallet's private key) in the "
                "environment. Refusing to start live trading."
            )
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:  # pragma: no cover - requirements.txt pins this dependency
            raise LiveTradingDisabled("py-clob-client is not installed") from exc

        chain_id = int(os.environ.get("POLYBOT_CHAIN_ID", "137"))  # 137 = Polygon mainnet
        signature_type = int(os.environ.get("POLYBOT_SIGNATURE_TYPE", "0"))  # 0=EOA,1=POLY_PROXY,2=POLY_GNOSIS_SAFE
        funder = os.environ.get("POLYBOT_FUNDER")  # required for signature_type 1/2 (proxy wallets)

        client = ClobClient(
            host=self.config.clob_base,
            chain_id=chain_id,
            key=pk,
            signature_type=signature_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        self._live_client = client
        log.warning(
            "LIVE trading client initialized (chain_id=%s, signature_type=%s) — "
            "REAL ORDERS CAN NOW BE PLACED", chain_id, signature_type,
        )
        return client

    def warm_cache(self, token_id: str) -> None:
        """Pre-fetch and cache tick_size/neg_risk for a token so a later
        time-critical `create_order()` call doesn't pay for those GETs (see
        module docstring). No-op in paper mode or if the live client can't be
        constructed (e.g. missing key) — this is a latency optimization, not
        a correctness requirement, so failures here are logged and swallowed.
        """
        if not self.is_live():
            return
        try:
            client = self._get_live_client()
            client.get_tick_size(token_id)
            client.get_neg_risk(token_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("warm_cache failed for %s: %s", token_id, exc)

    def _place_live_order(
        self, token_id: str, side: str, edge_fn: Callable[[float], float], edge_min: float,
        price_min: float, price_max: float, cap_usd: float,
        max_above_best: Optional[float] = 0.03,
        max_level_shares: Optional[float] = None,
        anomalous_mode: str = "cap",
    ) -> FillAttempt:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = self._get_live_client()
        signal_time = time.time()

        book = self.clob_rest.get_book(token_id)
        if book is None:
            return FillAttempt(token_id=token_id, side=side, signal_time=signal_time,
                                fill_time=time.time(), latency_ms=0, book_at_signal=None,
                                book_at_fill=None, walk=WalkResult(), edge_min=edge_min,
                                outcome="no_book")
        if not book.asks:
            return FillAttempt(token_id=token_id, side=side, signal_time=signal_time,
                                fill_time=time.time(), latency_ms=0, book_at_signal=book,
                                book_at_fill=book, walk=WalkResult(), edge_min=edge_min,
                                outcome="empty_book")

        # Size/price the same way paper does: walk the live ask ladder for all
        # profitable depth up to cap_usd. A live CLOB order takes one
        # (price, size) pair, so we submit a single FAK order at the worst
        # (highest) price still inside the profitable walk, sized to the
        # cumulative shares of that walk — FAK fills whatever is actually
        # available at submission time (which may be less, if the book moved
        # between our GET /book and the order reaching the matching engine)
        # and cancels the remainder, so over-sizing the limit is safe.
        walk = walk_asks(book.asks, edge_fn, edge_min, price_min, price_max, cap_usd,
                          self.config.fee_rate, max_above_best=max_above_best,
                          max_level_shares=max_level_shares,
                          anomalous_mode=anomalous_mode)
        if walk.total_shares <= 0:
            return FillAttempt(token_id=token_id, side=side, signal_time=signal_time,
                                fill_time=time.time(), latency_ms=0, book_at_signal=book,
                                book_at_fill=book, walk=WalkResult(), edge_min=edge_min,
                                outcome=("adverse_size_blocked" if walk.n_levels_skipped
                                         else "book_moved_no_edge"))

        limit_price = walk.fills[-1].price
        size = walk.total_shares
        order_args = OrderArgs(token_id=token_id, price=limit_price, size=size, side=BUY)
        signed_order = client.create_order(order_args)
        log.warning("LIVE ORDER SUBMIT token=%s side=%s price=%s size=%s cap_usd=%s",
                    token_id, side, limit_price, size, cap_usd)
        resp = client.post_order(signed_order, OrderType.FAK)
        log.warning("LIVE ORDER RESPONSE token=%s resp=%s", token_id, resp)

        # NOTE (see module docstring): we deliberately do NOT parse a "filled
        # shares" number out of `resp` — its schema is unverified here. We
        # record the *intended* walk as an upper bound and outcome="filled"
        # only when post_order did not raise; ledger consumers must treat
        # LIVE fills as provisional until response parsing is implemented
        # and reconciled against on-chain / portfolio balance.
        return FillAttempt(token_id=token_id, side=side, signal_time=signal_time,
                            fill_time=time.time(), latency_ms=0, book_at_signal=book,
                            book_at_fill=book, walk=walk, edge_min=edge_min, outcome="filled")
