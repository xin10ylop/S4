import math
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.polymarket import BookLevel, OrderBook
from polybot.strategy import evaluate_close_snipe, fair_value_up


@dataclass
class FakeMarket:
    slug: str = "bitcoin-up-or-down-july-15-2026-3pm-et"
    family: str = "1h"
    up_token_id: str = "UP"
    down_token_id: str = "DOWN"
    close_ts: float = 1_000_000.0


# Mirrors the shipped bot/config.yaml (audit/A4_change_spec.md). Note that
# evaluate_close_snipe never reads the window keys — the tau gate lives in
# engine._maybe_snipe (see tests/test_snipe_window.py, tests/test_engine_gating.py).
CFG = {"snipe_last_secs": 5, "snipe_min_tau_secs": 2.0, "snipe_fill_margin_secs": 0.5,
       "edge_min": 0.03, "price_min": 0.30, "price_max": 0.99, "vol_window_secs": 120}


class TestFairValueUp(unittest.TestCase):
    def test_equal_price_is_half(self):
        self.assertAlmostEqual(fair_value_up(100.0, 100.0, 0.001, 10), 0.5)

    def test_above_open_favors_up(self):
        v = fair_value_up(101.0, 100.0, 0.001, 10)
        self.assertGreater(v, 0.5)

    def test_below_open_favors_down(self):
        v = fair_value_up(99.0, 100.0, 0.001, 10)
        self.assertLess(v, 0.5)

    def test_symmetric(self):
        up = fair_value_up(101.0, 100.0, 0.001, 10)
        down = fair_value_up(99.0, 100.0, 0.001, 10)
        # ln(101/100) != -ln(99/100) exactly, but close_ish; check complementary structure instead
        self.assertAlmostEqual(up + fair_value_up(100.0 * 100.0 / 101.0, 100.0, 0.001, 10), 1.0,
                                places=6)

    def test_none_on_non_positive_sigma(self):
        self.assertIsNone(fair_value_up(101, 100, 0.0, 10))
        self.assertIsNone(fair_value_up(101, 100, -0.001, 10))

    def test_none_on_non_positive_tau(self):
        self.assertIsNone(fair_value_up(101, 100, 0.001, 0))
        self.assertIsNone(fair_value_up(101, 100, 0.001, -5))

    def test_none_on_nan_inputs(self):
        self.assertIsNone(fair_value_up(float("nan"), 100, 0.001, 10))
        self.assertIsNone(fair_value_up(101, float("nan"), 0.001, 10))


def _book(token_id, ask_price, ask_size=100.0):
    return OrderBook(token_id=token_id, bids=[], asks=[BookLevel(ask_price, ask_size)],
                      fetched_at=0.0)


class TestEvaluateCloseSnipe(unittest.TestCase):
    def test_fires_on_clear_mispriced_up(self):
        market = FakeMarket(close_ts=1000.0)
        now = 997.0  # tau = 3s, within snipe_last_secs=6
        # S_t well above S_open with modest vol -> fair_up close to 1
        book_up = _book("UP", 0.50)   # way underpriced vs fair ~1.0
        book_down = _book("DOWN", 0.50)
        sig = evaluate_close_snipe(market, now, 101.0, 100.0, 0.01, book_up, book_down, CFG, 0.07)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "up")
        self.assertGreater(sig.edge, CFG["edge_min"])

    def test_no_fire_when_price_already_fair(self):
        market = FakeMarket(close_ts=1000.0)
        now = 997.0
        # fair_up ~ 0.5 (S_t == S_open); ask priced right at ~0.5 -> no edge
        book_up = _book("UP", 0.50)
        book_down = _book("DOWN", 0.50)
        sig = evaluate_close_snipe(market, now, 100.0, 100.0, 0.01, book_up, book_down, CFG, 0.07)
        self.assertIsNone(sig)

    def test_no_fire_outside_price_band(self):
        market = FakeMarket(close_ts=1000.0)
        now = 997.0
        # fair_up ~1.0 but ask is already >= price_max (0.99) -> filtered out
        book_up = _book("UP", 0.995)
        book_down = _book("DOWN", 0.01)
        sig = evaluate_close_snipe(market, now, 101.0, 100.0, 0.01, book_up, book_down, CFG, 0.07)
        self.assertIsNone(sig)

    def test_no_fire_when_tau_non_positive(self):
        market = FakeMarket(close_ts=1000.0)
        sig = evaluate_close_snipe(market, 1000.0, 101.0, 100.0, 0.01, _book("UP", 0.5),
                                    _book("DOWN", 0.5), CFG, 0.07)
        self.assertIsNone(sig)

    def test_no_fire_on_missing_inputs(self):
        market = FakeMarket(close_ts=1000.0)
        sig = evaluate_close_snipe(market, 997.0, None, 100.0, 0.01, _book("UP", 0.5),
                                    _book("DOWN", 0.5), CFG, 0.07)
        self.assertIsNone(sig)

    def test_no_fire_when_no_books(self):
        market = FakeMarket(close_ts=1000.0)
        sig = evaluate_close_snipe(market, 997.0, 101.0, 100.0, 0.01, None, None, CFG, 0.07)
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
