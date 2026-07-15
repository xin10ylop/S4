"""Signal generation for close_snipe and settle_sweep.

Pure functions with no network I/O of their own (oracle/book state is passed
in) so they're directly unit-testable. engine.py wires these to live data on
a schedule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .fill_engine import fee_per_share
from .logging_setup import get_logger
from .oracle import BinanceOracle, normal_cdf
from .polymarket import Market, OrderBook

log = get_logger("strategy")

Side = str  # "up" | "down"


# --------------------------------------------------------------------------
# A) close_snipe
# --------------------------------------------------------------------------

def fair_value_up(S_t: float, S_open: float, sigma_1s: float, tau_secs: float) -> Optional[float]:
    """P(candle close >= open) under a local driftless-diffusion approximation:
      sigma = sigma_1s * sqrt(tau); z = ln(S_t / S_open) / sigma; fair_up = Phi(z).
    Returns None (=> caller must skip) if any input is non-finite or sigma<=0.
    """
    if not (math.isfinite(S_t) and math.isfinite(S_open) and math.isfinite(sigma_1s) and math.isfinite(tau_secs)):
        return None
    if sigma_1s <= 0 or tau_secs <= 0 or S_open <= 0 or S_t <= 0:
        return None
    sigma = sigma_1s * math.sqrt(tau_secs)
    if sigma <= 0 or not math.isfinite(sigma):
        return None
    z = math.log(S_t / S_open) / sigma
    if not math.isfinite(z):
        return None
    return normal_cdf(z)


@dataclass
class SnipeSignal:
    market_slug: str
    family: str
    side: Side
    token_id: str
    fair: float
    ask: float
    edge: float
    tau_secs: float  # seconds remaining to close at decision time
    s_t: float
    s_open: float
    sigma_1s: float


def evaluate_close_snipe(
    market: Market,
    now_ts: float,
    S_t: Optional[float],
    S_open: Optional[float],
    sigma_1s: Optional[float],
    book_up: Optional[OrderBook],
    book_down: Optional[OrderBook],
    cfg: dict,
    fee_rate: float,
) -> Optional[SnipeSignal]:
    """One decision-time check. Intended to be called once per second during
    the last `snipe_last_secs` seconds before close. Returns the first side
    that clears edge_min (ask below fair by more than edge_min after fees),
    or None. Caller enforces "one entry per window".
    """
    if S_t is None or S_open is None or sigma_1s is None:
        return None
    close_ts = market.close_ts
    tau = close_ts - now_ts
    if tau <= 0:
        return None
    fair_up = fair_value_up(S_t, S_open, sigma_1s, tau)
    if fair_up is None:
        return None
    fair_down = 1.0 - fair_up

    edge_min = float(cfg["edge_min"])
    price_min = float(cfg["price_min"])
    price_max = float(cfg["price_max"])

    for side, book, fair in (("up", book_up, fair_up), ("down", book_down, fair_down)):
        if book is None or book.best_ask is None:
            continue
        ask = book.best_ask.price
        if not (price_min < ask < price_max):
            continue
        edge = fair - ask - fee_per_share(ask, fee_rate)
        if edge > edge_min:
            token_id = market.up_token_id if side == "up" else market.down_token_id
            return SnipeSignal(
                market_slug=market.slug, family=market.family, side=side, token_id=token_id,
                fair=fair, ask=ask, edge=edge, tau_secs=tau, s_t=S_t, s_open=S_open,
                sigma_1s=sigma_1s,
            )
    return None


# --------------------------------------------------------------------------
# B) settle_sweep
# --------------------------------------------------------------------------

@dataclass
class WinnerDetermination:
    winner: Optional[Side]  # None => unresolved or ambiguous; caller must skip
    s_open: Optional[float]
    s_close: Optional[float]
    reason: str


def resolve_winner_1h(market: Market, binance: BinanceOracle) -> WinnerDetermination:
    """1h family resolves on the Binance BTC/USDT 1H candle: close>=open => Up.
    This is the market's actual resolution source, not a proxy — no basis risk.
    """
    open_px, close_px = binance.hour_open_close(market.window_start_ts)
    if open_px is None or close_px is None:
        return WinnerDetermination(None, open_px, close_px, "candle_not_closed_or_unavailable")
    winner = "up" if close_px >= open_px else "down"
    return WinnerDetermination(winner, open_px, close_px, "binance_1h_candle")


def resolve_winner_short_binance_proxy(
    market: Market, binance: BinanceOracle, distance_guard_usd: float
) -> WinnerDetermination:
    """Short families (5m/15m/4h) truly resolve on a Chainlink BTC/USD print,
    which we do not have wired (see oracle.ChainlinkOracle). We use Binance as
    a PROXY here, but only trust it when |close-open| exceeds
    `distance_guard_usd` — Binance vs Chainlink can diverge by several
    dollars right around a close (see docs/04_executability_audit.md), and an
    ambiguous small move is exactly where that basis risk bites. When the
    guard trips we return winner=None and the caller must skip the window
    entirely rather than risk buying the loser.
    """
    S_open = binance.price_at_second(market.window_start_ts)
    S_close = binance.price_at_second(market.close_ts)
    if S_open is None or S_close is None:
        return WinnerDetermination(None, S_open, S_close, "binance_proxy_unavailable")
    if abs(S_close - S_open) <= distance_guard_usd:
        return WinnerDetermination(None, S_open, S_close, "ambiguous_within_distance_guard")
    winner = "up" if S_close >= S_open else "down"
    return WinnerDetermination(winner, S_open, S_close, "binance_proxy_guarded")


def resolve_winner(
    market: Market, binance: BinanceOracle, distance_guard_usd: float, chainlink=None
) -> WinnerDetermination:
    if market.family == "1h":
        return resolve_winner_1h(market, binance)
    if chainlink is not None:
        raise NotImplementedError("Chainlink path not wired; see oracle.ChainlinkOracle docstring")
    return resolve_winner_short_binance_proxy(market, binance, distance_guard_usd)


def settle_sweep_target(
    market: Market, winner: Side, cfg: dict, fee_rate: float
) -> Tuple[str, Callable[[float], float], float, float]:
    """Returns (token_id, edge_fn, edge_min, price_max) for sweeping the
    winning token's asks. edge_fn(p) = 1 - p - fee(p); never touches the
    losing token (payout is always 0 there).
    """
    token_id = market.up_token_id if winner == "up" else market.down_token_id
    edge_min = float(cfg["settle_edge_min"])
    price_max = float(cfg["price_max"])

    def edge_fn(p: float) -> float:
        return 1.0 - p - fee_per_share(p, fee_rate)

    return token_id, edge_fn, edge_min, price_max
