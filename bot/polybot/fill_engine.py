"""Paper fill simulation.

The central honesty requirement: a signal fired against the book seen at
decision-time t is NOT filled against that book. We simulate reaction latency
(`latency_ms`), then RE-FETCH the live order book and fill against THAT book.
This is what actually captures the central risk of both strategies — the book
moving or the profitable depth vanishing between signal and order arrival.

Both close_snipe and settle_sweep reduce to the same primitive: walk a sorted
ask ladder, taking every level that clears an edge test, up to a notional
budget. The two strategies differ only in what `edge_fn` computes (fair-value
edge vs 1-minus-price edge) and how the target token/timing is chosen — that
logic lives in strategy.py. This module is intentionally strategy-agnostic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import Config
from .logging_setup import get_logger
from .polymarket import BookLevel, ClobClientREST, OrderBook

log = get_logger("fill_engine")


def fee_per_share(price: float, fee_rate: float) -> float:
    """Taker fee in $ per share at a given price. Maker fee is 0 (not modeled; we're takers)."""
    return fee_rate * price * (1.0 - price)


@dataclass
class LevelFill:
    price: float
    shares: float
    fee_per_share: float

    @property
    def cost(self) -> float:
        return self.price * self.shares

    @property
    def fee(self) -> float:
        return self.fee_per_share * self.shares


@dataclass
class WalkResult:
    fills: List[LevelFill] = field(default_factory=list)

    @property
    def total_shares(self) -> float:
        return sum(f.shares for f in self.fills)

    @property
    def total_cost(self) -> float:
        return sum(f.cost for f in self.fills)

    @property
    def total_fees(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def avg_price(self) -> Optional[float]:
        sh = self.total_shares
        return (self.total_cost / sh) if sh > 0 else None


def walk_asks(
    asks: List[BookLevel],
    edge_fn: Callable[[float], float],
    edge_min: float,
    price_min: float,
    price_max: float,
    cap_usd: float,
    fee_rate: float,
) -> WalkResult:
    """Walk a price-ascending ask ladder, consuming every level that clears
    the edge test, until the notional budget (`cap_usd`) is exhausted or a
    level fails the test. Asks MUST already be sorted ascending by price —
    the Polymarket /book endpoint does NOT guarantee this.

    edge_fn(price) should return the edge (e.g. fair-ask-fee, or
    1-price-fee) at that price; we stop at the first level where
    edge_fn(price) <= edge_min (levels only get worse as price rises for a
    buy, so first failure means stop, not skip-and-continue).
    """
    result = WalkResult()
    remaining_usd = cap_usd
    for lvl in asks:
        if remaining_usd <= 0:
            break
        if not (price_min < lvl.price < price_max):
            break
        edge = edge_fn(lvl.price)
        if edge <= edge_min:
            break
        max_shares_by_budget = remaining_usd / lvl.price
        shares = min(lvl.size, max_shares_by_budget)
        if shares <= 1e-9:  # guard against float dust when budget is ~exhausted
            break
        fps = fee_per_share(lvl.price, fee_rate)
        result.fills.append(LevelFill(price=lvl.price, shares=shares, fee_per_share=fps))
        remaining_usd -= shares * lvl.price
    return result


@dataclass
class FillAttempt:
    """Record of one attempt to fill a signal — successful or not.

    This is the key executability record: "signal fired but book moved / no
    profitable size left" must be captured, not silently dropped.
    """
    token_id: str
    side: str
    signal_time: float
    fill_time: float
    latency_ms: int
    book_at_signal: Optional[OrderBook]
    book_at_fill: Optional[OrderBook]
    walk: WalkResult
    edge_min: float
    outcome: str  # "filled", "no_book", "book_moved_no_edge", "empty_book"

    @property
    def filled(self) -> bool:
        return self.walk.total_shares > 0


def execute_taker_signal(
    clob: ClobClientREST,
    token_id: str,
    side: str,
    edge_fn: Callable[[float], float],
    edge_min: float,
    price_min: float,
    price_max: float,
    cap_usd: float,
    fee_rate: float,
    latency_ms: int,
    book_at_signal: Optional[OrderBook] = None,
    sleep: bool = True,
) -> FillAttempt:
    """Simulate the full signal -> latency -> re-fetch -> fill pipeline for one attempt.

    `sleep=True` performs a real time.sleep(latency_ms/1000) — intended to be
    called from a worker thread so the main loop is not blocked. Tests may
    pass sleep=False to skip the wait (book_at_fill would then equal a
    fresh fetch taken immediately).
    """
    signal_time = time.time()
    if sleep and latency_ms > 0:
        time.sleep(latency_ms / 1000.0)
    fill_time = time.time()

    book = clob.get_book(token_id)
    if book is None:
        return FillAttempt(
            token_id=token_id, side=side, signal_time=signal_time, fill_time=fill_time,
            latency_ms=latency_ms, book_at_signal=book_at_signal, book_at_fill=None,
            walk=WalkResult(), edge_min=edge_min, outcome="no_book",
        )
    if not book.asks:
        return FillAttempt(
            token_id=token_id, side=side, signal_time=signal_time, fill_time=fill_time,
            latency_ms=latency_ms, book_at_signal=book_at_signal, book_at_fill=book,
            walk=WalkResult(), edge_min=edge_min, outcome="empty_book",
        )
    walk = walk_asks(book.asks, edge_fn, edge_min, price_min, price_max, cap_usd, fee_rate)
    outcome = "filled" if walk.total_shares > 0 else "book_moved_no_edge"
    return FillAttempt(
        token_id=token_id, side=side, signal_time=signal_time, fill_time=fill_time,
        latency_ms=latency_ms, book_at_signal=book_at_signal, book_at_fill=book,
        walk=walk, edge_min=edge_min, outcome=outcome,
    )
