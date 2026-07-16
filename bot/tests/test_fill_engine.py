import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.fill_engine import fee_per_share, walk_asks
from polybot.polymarket import BookLevel


class TestFeePerShare(unittest.TestCase):
    def test_symmetric_around_half(self):
        self.assertAlmostEqual(fee_per_share(0.5, 0.07), 0.07 * 0.25)

    def test_zero_at_bounds(self):
        self.assertAlmostEqual(fee_per_share(0.0, 0.07), 0.0)
        self.assertAlmostEqual(fee_per_share(1.0, 0.07), 0.0)

    def test_scales_with_rate(self):
        self.assertAlmostEqual(fee_per_share(0.3, 0.14), 2 * fee_per_share(0.3, 0.07))


class TestWalkAsks(unittest.TestCase):
    def setUp(self):
        # settle_sweep-style edge fn: 1 - p - fee(p)
        self.fee_rate = 0.07
        self.edge_fn = lambda p: 1.0 - p - fee_per_share(p, self.fee_rate)

    def test_stops_at_first_unprofitable_level(self):
        asks = [BookLevel(0.10, 100), BookLevel(0.50, 100), BookLevel(0.95, 100)]
        # edge_min chosen so 0.10 and 0.50 pass, 0.95 does not
        # (max_above_best=None: this test targets the edge cutoff, not the slippage bound)
        result = walk_asks(asks, self.edge_fn, edge_min=0.10, price_min=0.0, price_max=0.99,
                            cap_usd=1_000_000, fee_rate=self.fee_rate, max_above_best=None)
        prices = [f.price for f in result.fills]
        self.assertEqual(prices, [0.10, 0.50])

    def test_slippage_bound_stops_deep_walk(self):
        # default bound (3c): may not chase levels far above the best ask even
        # when the edge test would allow them (first live paper loss came from
        # a saturated fair value licensing a 10c+ deep walk)
        asks = [BookLevel(0.60, 10), BookLevel(0.62, 10), BookLevel(0.70, 100)]
        result = walk_asks(asks, self.edge_fn, edge_min=0.01, price_min=0.0, price_max=0.99,
                            cap_usd=1_000_000, fee_rate=self.fee_rate, max_above_best=0.03)
        prices = [f.price for f in result.fills]
        self.assertEqual(prices, [0.60, 0.62])

    def test_respects_notional_cap_exactly(self):
        asks = [BookLevel(0.20, 1000)]
        result = walk_asks(asks, self.edge_fn, edge_min=0.02, price_min=0.0, price_max=0.99,
                            cap_usd=25.0, fee_rate=self.fee_rate)
        self.assertAlmostEqual(result.total_cost, 25.0, places=6)
        self.assertAlmostEqual(result.total_shares, 125.0, places=6)

    def test_no_float_dust_fill_when_budget_exhausted(self):
        # A budget that divides exactly should not leave a near-zero trailing fill.
        asks = [BookLevel(0.19, 131.578947368421), BookLevel(0.20, 500)]
        result = walk_asks(asks, self.edge_fn, edge_min=0.02, price_min=0.0, price_max=0.99,
                            cap_usd=25.0, fee_rate=self.fee_rate)
        self.assertEqual(len(result.fills), 1)  # no dust second fill

    def test_never_exceeds_price_max(self):
        asks = [BookLevel(0.995, 100)]
        result = walk_asks(asks, self.edge_fn, edge_min=-1.0, price_min=0.0, price_max=0.99,
                            cap_usd=1000, fee_rate=self.fee_rate)
        self.assertEqual(result.fills, [])

    def test_empty_book_returns_empty_walk(self):
        result = walk_asks([], self.edge_fn, edge_min=0.02, price_min=0.0, price_max=0.99,
                            cap_usd=25.0, fee_rate=self.fee_rate)
        self.assertEqual(result.total_shares, 0.0)
        self.assertIsNone(result.avg_price)

    def test_self_sizes_to_all_profitable_depth_when_under_cap(self):
        asks = [BookLevel(0.05, 10), BookLevel(0.06, 10)]
        result = walk_asks(asks, self.edge_fn, edge_min=0.02, price_min=0.0, price_max=0.99,
                            cap_usd=1_000_000, fee_rate=self.fee_rate)
        self.assertAlmostEqual(result.total_shares, 20.0)


if __name__ == "__main__":
    unittest.main()
