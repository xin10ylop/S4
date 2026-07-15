"""Underlying-price oracles.

1h markets resolve on the Binance BTC/USDT 1H candle (close>=open => Up), so
Binance is both a *portable* and *authoritative* oracle for that family: it's
reachable from anywhere (via data-api.binance.vision) and matches the actual
resolution source.

Short families (5m/15m/4h) resolve on a Chainlink BTC/USD print. We do not
have a wired Chainlink feed in this build (see ChainlinkOracle below — a
documented, honest stub). Per the strategy spec, close_snipe for those
families therefore stays OFF until a Chainlink oracle is configured; we do NOT
substitute Binance as a signal source for short-family snipe (basis risk).
settle_sweep for short families is allowed to use the Binance price only as a
*distance-guarded* winner check (see strategy.py), which is a materially
different, safer use than using it as a fair-value input.
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from .config import Config
from .logging_setup import get_logger
from .net import get_session

log = get_logger("oracle")


@dataclass(frozen=True)
class PricePoint:
    ts: float  # unix seconds (float, from local receipt time)
    price: float


class BinanceOracle:
    """Maintains a 1-second-ish rolling series of BTC/USDT price via REST polling.

    Uses data-api.binance.vision, which is reachable from anywhere (unlike
    api.binance.com, which 451s in some sandboxes). This is a REST poller by
    design (simple, portable, testable here); an optional websocket client can
    be layered on later without changing the public interface.
    """

    def __init__(self, config: Config, symbol: str = "BTCUSDT", maxlen: int = 3700):
        self._base = config.binance_rest_base
        self._symbol = symbol
        self._session = get_session()
        self._series: Deque[PricePoint] = collections.deque(maxlen=maxlen)

    def fetch_price(self) -> Optional[float]:
        """One-shot fetch of the current BTC/USDT price. Returns None on error."""
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/ticker/price",
                params={"symbol": self._symbol},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data["price"])
        except Exception as exc:  # noqa: BLE001 - log and continue, oracle must not crash the bot
            log.warning("binance fetch_price failed: %s", exc)
            return None

    def poll_once(self) -> Optional[PricePoint]:
        """Fetch and append one price point to the rolling series."""
        px = self.fetch_price()
        if px is None:
            return None
        pt = PricePoint(ts=time.time(), price=px)
        self._series.append(pt)
        return pt

    def price_at_or_before(self, ts: float) -> Optional[float]:
        """Last known price at or before `ts` (backward fill), from the local series."""
        best: Optional[PricePoint] = None
        for pt in self._series:
            if pt.ts <= ts:
                if best is None or pt.ts > best.ts:
                    best = pt
        return best.price if best else None

    def latest(self) -> Optional[PricePoint]:
        return self._series[-1] if self._series else None

    def rolling_log_return_std(self, window_secs: float = 120.0) -> float:
        """Std of 1-step log returns over the trailing `window_secs`.

        Uses whatever points fall in the window (points are not guaranteed to
        be exactly 1s apart under REST polling jitter; this is an honest
        approximation of the "rolling std of 1s log-returns" spec). Returns
        NaN if there are too few points.
        """
        if len(self._series) < 3:
            return float("nan")
        now = self._series[-1].ts
        pts = [p for p in self._series if now - p.ts <= window_secs]
        if len(pts) < 3:
            return float("nan")
        rets = []
        for a, b in zip(pts, pts[1:]):
            if a.price > 0 and b.price > 0:
                rets.append(math.log(b.price / a.price))
        if len(rets) < 2:
            return float("nan")
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    def klines_1h(self, limit: int = 2) -> list:
        """Fetch recent 1H klines: [[open_time, open, high, low, close, ...], ...]."""
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={"symbol": self._symbol, "interval": "1h", "limit": limit},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance klines_1h failed: %s", exc)
            return []

    def price_at_second(self, ts: int) -> Optional[float]:
        """Authoritative Binance price at a specific unix second, via the 1s
        kline REST endpoint (works even if our local rolling series doesn't
        cover that second, e.g. bot started mid-window). Uses the kline's
        open price (price at the start of that second).
        """
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={
                    "symbol": self._symbol,
                    "interval": "1s",
                    "startTime": int(ts * 1000),
                    "limit": 1,
                },
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance price_at_second failed: %s", exc)
            return None
        if not rows:
            return None
        return float(rows[0][1])  # open price

    def hour_open_close(self, window_start_ts: int) -> Tuple[Optional[float], Optional[float]]:
        """Open/close for the 1H candle starting at window_start_ts (unix secs).

        Returns (open, close); close is None if the candle hasn't closed yet
        (candle still forming) — callers should treat that as "not resolved".
        """
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={
                    "symbol": self._symbol,
                    "interval": "1h",
                    "startTime": int(window_start_ts * 1000),
                    "limit": 1,
                },
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance hour_open_close failed: %s", exc)
            return None, None
        if not rows:
            return None, None
        row = rows[0]
        open_time_ms, open_px, high, low, close_px = row[0], row[1], row[2], row[3], row[4]
        close_time_ms = row[6]
        now_ms = time.time() * 1000
        candle_closed = now_ms >= close_time_ms
        return float(open_px), (float(close_px) if candle_closed else None)


class ChainlinkOracle:
    """Chainlink BTC/USD oracle — NOT wired in this build.

    Short-family (5m/15m/4h) markets resolve on a Chainlink print, which is a
    different feed from Binance and can diverge by several dollars around
    fast moves (the research in docs/04_executability_audit.md measured this
    basis risk and it kills the short-family close-snipe edge if you use
    Binance as a stand-in signal). Wiring a real Chainlink read requires
    either:
      - an on-chain read of the relevant Chainlink aggregator (e.g. via a
        Polygon RPC + the aggregator's `latestRoundData()`), or
      - a data vendor that mirrors Chainlink prints in real time.

    Until one of those is configured (set `chainlink_rpc_url` /
    `chainlink_feed_address` — not present in config.yaml by default), this
    class raises on use so short-family close_snipe stays honestly OFF rather
    than silently degrading to a biased signal. settle_sweep for short
    families does NOT depend on this class; it uses BinanceOracle with a
    distance guard instead (see strategy.py `resolve_winner`).
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "ChainlinkOracle is not wired in this build. Configure a Chainlink "
            "BTC/USD read (on-chain aggregator or data vendor) before enabling "
            "close_snipe for 5m/15m/4h. See class docstring."
        )


def normal_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (avoids a hard scipy dependency at import time)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
