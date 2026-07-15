import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.oracle import PricePoint, normal_cdf


class FakeSeriesOracle:
    """Exercises BinanceOracle's pure rolling-stat logic without network calls
    by directly populating the internal deque (same structure the real class uses)."""

    def __init__(self, points):
        from polybot.oracle import BinanceOracle
        # Build without hitting the network: bypass __init__'s session creation
        # by constructing a real instance (network only happens on fetch calls).
        self.oracle = BinanceOracle.__new__(BinanceOracle)
        import collections
        self.oracle._series = collections.deque(points, maxlen=3700)

    def std(self, window=120.0):
        return self.oracle.rolling_log_return_std(window)

    def price_at_or_before(self, ts):
        return self.oracle.price_at_or_before(ts)


class TestNormalCdf(unittest.TestCase):
    def test_zero_is_half(self):
        self.assertAlmostEqual(normal_cdf(0.0), 0.5)

    def test_symmetric(self):
        self.assertAlmostEqual(normal_cdf(1.0) + normal_cdf(-1.0), 1.0)

    def test_matches_known_quantiles(self):
        self.assertAlmostEqual(normal_cdf(1.6448536269514722), 0.95, places=6)
        self.assertAlmostEqual(normal_cdf(-1.6448536269514722), 0.05, places=6)


class TestRollingLogReturnStd(unittest.TestCase):
    def test_too_few_points_is_nan(self):
        f = FakeSeriesOracle([PricePoint(0, 100), PricePoint(1, 101)])
        self.assertTrue(math.isnan(f.std()))

    def test_constant_price_has_zero_std(self):
        pts = [PricePoint(float(i), 100.0) for i in range(10)]
        f = FakeSeriesOracle(pts)
        self.assertAlmostEqual(f.std(), 0.0)

    def test_only_uses_window(self):
        # Old points far outside the window must not affect the stat.
        old = [PricePoint(float(i), 100.0 + i * 50) for i in range(5)]  # huge jumps, far in past
        recent = [PricePoint(1000.0 + i, 100.0) for i in range(10)]      # flat, recent
        f = FakeSeriesOracle(old + recent)
        self.assertAlmostEqual(f.std(window=120.0), 0.0)

    def test_nonzero_for_varying_recent_prices(self):
        pts = [PricePoint(float(i), 100.0 * (1.001 ** i)) for i in range(20)]
        f = FakeSeriesOracle(pts)
        std = f.std()
        self.assertFalse(math.isnan(std))
        self.assertGreater(std, 0.0)


class TestPriceAtOrBefore(unittest.TestCase):
    def test_returns_most_recent_at_or_before(self):
        pts = [PricePoint(0, 100), PricePoint(5, 105), PricePoint(10, 110)]
        f = FakeSeriesOracle(pts)
        self.assertEqual(f.price_at_or_before(7), 105)
        self.assertEqual(f.price_at_or_before(10), 110)
        self.assertEqual(f.price_at_or_before(100), 110)

    def test_returns_none_if_before_all_points(self):
        pts = [PricePoint(10, 100)]
        f = FakeSeriesOracle(pts)
        self.assertIsNone(f.price_at_or_before(5))


if __name__ == "__main__":
    unittest.main()
