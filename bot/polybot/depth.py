"""Rolling observed-depth reference — the statistic behind the adverse-size filter.

WHY A RELATIVE STATISTIC AND NOT A SHARE COUNT
----------------------------------------------
"Skip levels bigger than N shares" does not survive contact with more than one
coin: a BTC hourly book quotes tens of shares near the close while a DOGE or
XRP hourly book quotes thousands for the same dollar risk. The guard therefore
compares a level against what *this family's own book* has recently been
showing.

THE STATISTIC (measured, see audit/M4_risk_guards.md §1)
--------------------------------------------------------
    size_ratio = level.size / median(recent observed ask-level sizes)

with the reference restricted to levels inside the tradeable price band
(`price_min < price < price_max`). The band restriction is not cosmetic. On
275 days of 1h quotes, the median best-ask size by price bucket is

    (0.00,0.10] 24,190    (0.30,0.50] 11.2    (0.50,0.70] 10.6
    (0.70,0.90] 19.4      (0.90,0.95] 32.3    (0.95,0.99] 37.5
    (0.99,1.00] 141.4

i.e. the ~free "lottery ticket" side of an already-decided market carries
literally a thousand times the size of a genuinely contested quote. A
reference pooled over all prices is dominated by those quotes and scores a
perfectly normal contested offer at 0.01x — the statistic becomes noise. Only
levels the strategy is actually allowed to buy are recorded.

Samples come from the books the engine already fetches inside the snipe
window; no extra REST traffic is added. That also means the reference fills
slowly (only genuinely contested closes contribute), so `reference()` returns
None until `min_samples` observations exist and the filter stays inert until
then — fail-open on the *filter*, which is correct here because the filter is
not what keeps the strategy safe (the tau band, fair_cap, the sigma floor, the
walk bound and the per-event cap are).
"""
from __future__ import annotations

import collections
import statistics
import threading
from typing import Any, Deque, Dict, Optional

from .logging_setup import get_logger

log = get_logger("depth")


class DepthTracker:
    """Per-family rolling history of observed in-band ask-level sizes.

    Thread-safe: the engine tick thread writes, the status writer reads.
    """

    def __init__(self, history_n: int = 200, min_samples: int = 30):
        self.history_n = max(1, int(history_n))
        self.min_samples = max(1, int(min_samples))
        self._lock = threading.Lock()
        self._hist: Dict[str, Deque[float]] = {}
        self._n_observed: Dict[str, int] = {}

    # ------------------------------------------------------------- writing
    def observe_levels(self, family: str, levels, price_min: float,
                       price_max: float) -> int:
        """Record every level whose price is inside the tradeable band.

        `levels` is any iterable of objects with `.price` / `.size`
        (polymarket.BookLevel). Returns how many samples were recorded.
        """
        if not levels:
            return 0
        vals = [float(lv.size) for lv in levels
                if lv is not None and lv.size and lv.size > 0
                and price_min < float(lv.price) < price_max]
        if not vals:
            return 0
        with self._lock:
            dq = self._hist.get(family)
            if dq is None:
                dq = collections.deque(maxlen=self.history_n)
                self._hist[family] = dq
            dq.extend(vals)
            self._n_observed[family] = self._n_observed.get(family, 0) + len(vals)
        return len(vals)

    def observe_book(self, family: str, book, price_min: float, price_max: float) -> int:
        """Convenience wrapper for a polymarket.OrderBook (or None)."""
        if book is None:
            return 0
        return self.observe_levels(family, book.asks, price_min, price_max)

    # ------------------------------------------------------------- reading
    def n_samples(self, family: str) -> int:
        with self._lock:
            dq = self._hist.get(family)
            return len(dq) if dq else 0

    def reference(self, family: str) -> Optional[float]:
        """Median in-band level size for this family, or None while the history
        is too thin to mean anything (the filter is then inert)."""
        with self._lock:
            dq = self._hist.get(family)
            if not dq or len(dq) < self.min_samples:
                return None
            vals = list(dq)
        med = statistics.median(vals)
        return float(med) if med > 0 else None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            fams = list(self._hist.keys())
            counts = {f: len(self._hist[f]) for f in fams}
            total = dict(self._n_observed)
        out = {}
        for f in fams:
            out[f] = {
                "samples": counts[f],
                "min_samples": self.min_samples,
                "total_observed": total.get(f, 0),
                "reference_size": self.reference(f),
            }
        return out


def max_level_shares(cfg: Optional[dict], tracker: Optional[DepthTracker],
                     family: str) -> Optional[float]:
    """Per-level share ceiling implied by the adverse-size filter, or None when
    the filter must not act (disabled, or reference not yet established).

    Kept as a free function so `engine` has exactly one place to ask "does the
    filter bind right now?" and so it is directly unit-testable.
    """
    if not cfg or not bool(cfg.get("enabled", False)) or tracker is None:
        return None
    ratio = float(cfg.get("max_size_ratio", 8.0))
    if ratio <= 0:
        return None
    ref = tracker.reference(family)
    if ref is None:
        return None
    return ratio * ref
