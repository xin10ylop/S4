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
    # adverse-size filter bookkeeping (M4 guard 1). Zero unless the filter
    # actually bound on this walk, so it is free when the filter is off.
    n_levels_capped: int = 0
    n_levels_skipped: int = 0
    shares_suppressed: float = 0.0

    @property
    def size_filter_bound(self) -> bool:
        return (self.n_levels_capped + self.n_levels_skipped) > 0

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

    def truncated_to_shares(self, filled_shares: float) -> "WalkResult":
        """A copy of this walk trimmed to `filled_shares`, keeping the CHEAPEST
        levels first.

        Used by the LIVE path when a FAK order partially fills: the matching
        engine works up the ask ladder, so a partial fill got the cheap levels
        and missed the expensive tail. Truncating from the cheap end is
        therefore the faithful reconstruction — truncating from the expensive
        end would understate our average price and overstate P&L, which is the
        exact direction of error this whole change exists to remove.

        `filled_shares` above the walk total returns the walk unchanged; a
        non-positive value returns an empty walk.
        """
        if filled_shares <= 0:
            return WalkResult(n_levels_capped=self.n_levels_capped,
                              n_levels_skipped=self.n_levels_skipped,
                              shares_suppressed=self.shares_suppressed)
        if filled_shares >= self.total_shares:
            return self
        kept: List[LevelFill] = []
        remaining = filled_shares
        for f in self.fills:  # already cheapest-first (walk_asks ascends price)
            if remaining <= 0:
                break
            take = min(f.shares, remaining)
            kept.append(LevelFill(price=f.price, shares=take, fee_per_share=f.fee_per_share))
            remaining -= take
        return WalkResult(fills=kept, n_levels_capped=self.n_levels_capped,
                          n_levels_skipped=self.n_levels_skipped,
                          shares_suppressed=self.shares_suppressed)


def walk_asks(
    asks: List[BookLevel],
    edge_fn: Callable[[float], float],
    edge_min: float,
    price_min: float,
    price_max: float,
    cap_usd: float,
    fee_rate: float,
    max_above_best: Optional[float] = 0.03,
    max_level_shares: Optional[float] = None,
    anomalous_mode: str = "cap",
) -> WalkResult:
    """Walk a price-ascending ask ladder, consuming every level that clears
    the edge test, until the notional budget (`cap_usd`) is exhausted or a
    level fails the test. Asks MUST already be sorted ascending by price —
    the Polymarket /book endpoint does NOT guarantee this.

    edge_fn(price) should return the edge (e.g. fair-ask-fee, or
    1-price-fee) at that price; we stop at the first level where
    edge_fn(price) <= edge_min (levels only get worse as price rises for a
    buy, so first failure means stop, not skip-and-continue).

    `max_above_best` bounds how far above the best (first eligible) ask the
    walk may chase. The research backtests validated fills at the top of the
    book only; letting a saturated fair value license levels 20-30c deeper
    was never validated (and hurt on the first live paper trade). None
    disables the bound.

    `max_level_shares` is the ADVERSE-SIZE FILTER (M4 guard 1): the maximum
    number of shares this walk may take from any ONE level, derived by the
    caller from that family's recent typical depth (see depth.py). None (the
    default, and the shipped default) disables it entirely. When a level
    exceeds it, `anomalous_mode` decides what happens:
      "cap"  - take only `max_level_shares` from that level and keep walking
      "skip" - take nothing from that level, but keep walking to the next one
    Either way the level still counts for `best_price`, so skipping an
    anomalous top-of-book level can NEVER be used to walk deeper than
    `max_above_best` above it. Measured evidence for the default being OFF is
    in audit/M4_risk_guards.md §1.
    """
    result = WalkResult()
    remaining_usd = cap_usd
    best_price: Optional[float] = None
    filter_on = max_level_shares is not None and max_level_shares > 0
    for lvl in asks:
        if remaining_usd <= 0:
            break
        if not (price_min < lvl.price < price_max):
            break
        # `best_price` is anchored on the first PRICE-eligible level, before the
        # size filter can remove it — otherwise skipping level 0 would silently
        # re-baseline the max_above_best bound one level deeper.
        if best_price is None:
            best_price = lvl.price
        elif max_above_best is not None and lvl.price > best_price + max_above_best + 1e-9:
            break
        edge = edge_fn(lvl.price)
        if edge <= edge_min:
            break
        max_shares_by_budget = remaining_usd / lvl.price
        shares = min(lvl.size, max_shares_by_budget)
        if filter_on and lvl.size > max_level_shares:
            if anomalous_mode == "skip":
                result.n_levels_skipped += 1
                result.shares_suppressed += shares
                log.info("adverse-size filter SKIPPED level px=%.4f size=%.2f "
                         "(> max_level_shares=%.2f)", lvl.price, lvl.size,
                         max_level_shares)
                continue
            allowed = min(shares, float(max_level_shares))
            if allowed < shares:
                result.n_levels_capped += 1
                result.shares_suppressed += shares - allowed
                log.info("adverse-size filter CAPPED level px=%.4f size=%.2f -> %.2f "
                         "shares (max_level_shares=%.2f)", lvl.price, lvl.size,
                         allowed, max_level_shares)
            shares = allowed
        if shares <= 1e-9:  # guard against float dust when budget is ~exhausted
            break
        fps = fee_per_share(lvl.price, fee_rate)
        result.fills.append(LevelFill(price=lvl.price, shares=shares, fee_per_share=fps))
        remaining_usd -= shares * lvl.price
    return result


# Clock skew beyond this magnitude is reported, once per process, as an
# operational fault. See book_is_stale for why it matters.
_SKEW_WARN_SECS = 0.5
_skew_warned = False


def book_is_stale(book: Optional[OrderBook], max_book_age_s: Optional[float],
                  now: Optional[float] = None) -> bool:
    """True iff the CLOB's own last-update stamp for `book` is older than the
    bound. Shared by the paper and live order paths so they cannot diverge.

    Fails OPEN (returns False) when the guard is off, the book is missing, or
    the payload carried no timestamp — an upstream schema change must not
    silently halt all trading. See engine._book_too_stale for the full argument.

    CLOCK SKEW. `age = local_now - clob_timestamp`, so it inherits any offset
    between this host's clock and the CLOB's. Observed live on 2026-07-28: ages
    of **-1.05 s**, i.e. this sandbox's clock is at least a second behind the
    exchange. Two consequences, and only one of them is dangerous:

      * clock BEHIND (negative ages): the guard under-blocks by the offset.
        Safe direction, and it is what was actually observed.
      * clock AHEAD: every age inflates by the offset, and a host drifting a few
        seconds ahead would make EVERY book look stale and silently halt all
        trading — the same class of failure as the warmup deadlock M4 found.

    A negative age is physically impossible, so it is reported, and clamped to 0
    for the comparison. Be clear about what that clamp is worth: for any
    positive `max_book_age_s` it is a **no-op** — a negative age never exceeds a
    positive bound either way — and the mutation checker correctly flagged a
    mutation removing it as unkillable. It stays because it makes the intent
    explicit and holds if the comparison is ever changed; it is not a guard.
    **The behaviour that protects anything here is the warning**, which is what
    the checker mutates. And the warning cannot fix a clock running AHEAD —
    nothing read from a single timestamp can — so **the production host must run
    NTP**, and this is the line that says so out loud.
    """
    if book is None or max_book_age_s is None or max_book_age_s <= 0:
        return False
    age = book.age_secs(now)
    if age is None:
        return False
    if age < -_SKEW_WARN_SECS:
        global _skew_warned
        if not _skew_warned:
            _skew_warned = True
            log.warning(
                "CLOCK SKEW: book %s reports an age of %.2fs, which is impossible — this "
                "host's clock is at least %.2fs BEHIND the CLOB. The stale-book guard is "
                "under-blocking by roughly that much. Harmless in this direction, but a "
                "clock running AHEAD would make every book look stale and halt trading: "
                "run NTP on the production host.", book.token_id, age, -age)
    return max(0.0, age) > float(max_book_age_s)


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
    # "filled" | "no_book" | "empty_book" | "book_moved_no_edge"
    # | "adverse_size_blocked" (M4 guard 1 declined every profitable level)
    # | "stale_book"           (M5 guard: the CLOB had not updated this book
    #                           within max_book_age_s, so we refuse to lift it)
    outcome: str

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
    max_above_best: Optional[float] = 0.03,
    max_level_shares: Optional[float] = None,
    anomalous_mode: str = "cap",
    max_book_age_s: Optional[float] = None,
) -> FillAttempt:
    """Simulate the full signal -> latency -> re-fetch -> fill pipeline for one attempt.

    `sleep=True` performs a real time.sleep(latency_ms/1000) — intended to be
    called from a worker thread so the main loop is not blocked. Tests may
    pass sleep=False to skip the wait (book_at_fill would then equal a
    fresh fetch taken immediately).

    `max_book_age_s` (M5) rejects a FILL-TIME book the CLOB has not updated
    recently. This is the position the measurement actually indicts: the
    verifier found HYPE drew 93.6% of its P&L and BNB 46.5% of its negative
    P&L from fills against books older than 5 s, while BTC's oldest fill book
    in 66 fills was 2.2 s. None disables it.
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
    if book_is_stale(book, max_book_age_s, fill_time):
        log.warning("stale-book guard: refusing to fill %s against a book %.1fs old "
                    "(max_book_age_s=%s)", token_id, book.age_secs(fill_time),
                    max_book_age_s)
        return FillAttempt(
            token_id=token_id, side=side, signal_time=signal_time, fill_time=fill_time,
            latency_ms=latency_ms, book_at_signal=book_at_signal, book_at_fill=book,
            walk=WalkResult(), edge_min=edge_min, outcome="stale_book",
        )
    walk = walk_asks(book.asks, edge_fn, edge_min, price_min, price_max, cap_usd, fee_rate,
                     max_above_best=max_above_best, max_level_shares=max_level_shares,
                     anomalous_mode=anomalous_mode)
    if walk.total_shares > 0:
        outcome = "filled"
    elif walk.n_levels_skipped > 0:
        # distinct from book_moved_no_edge: the edge WAS there, our own guard
        # declined it. Must be visible in the ledger or the guard is invisible.
        outcome = "adverse_size_blocked"
    else:
        outcome = "book_moved_no_edge"
    return FillAttempt(
        token_id=token_id, side=side, signal_time=signal_time, fill_time=fill_time,
        latency_ms=latency_ms, book_at_signal=book_at_signal, book_at_fill=book,
        walk=walk, edge_min=edge_min, outcome=outcome,
    )
