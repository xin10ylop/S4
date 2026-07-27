"""Unit tests for strategy.snipe_tau_bounds — the close_snipe timing band.

Pure: no Engine, no config file, no network. See audit/A4_change_spec.md Part 1
for the measurements behind the [2.0, 5.0] default.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.strategy import snipe_tau_bounds

SHIPPED = {"snipe_last_secs": 5, "snipe_min_tau_secs": 2.0, "snipe_fill_margin_secs": 0.5}


class TestSnipeTauBounds(unittest.TestCase):
    def test_bounds_from_config(self):
        self.assertEqual(snipe_tau_bounds(SHIPPED, 1500), (2.0, 5.0))

    def test_floor_tracks_latency(self):
        # 3000ms latency + 0.5s margin = 3.5s, which dominates the 2.0 floor.
        lo, hi = snipe_tau_bounds(SHIPPED, 3000)
        self.assertAlmostEqual(lo, 3.5)
        self.assertAlmostEqual(hi, 5.0)

    def test_floor_never_below_min(self):
        # Even with near-zero latency the measured-dead tau<2 bucket stays excluded.
        lo, _ = snipe_tau_bounds(SHIPPED, 250)
        self.assertAlmostEqual(lo, 2.0)
        lo0, _ = snipe_tau_bounds(SHIPPED, 0)
        self.assertAlmostEqual(lo0, 2.0)

    def test_defaults_when_new_keys_absent(self):
        # An old config.yaml (pre-A4) must not crash the bot.
        self.assertEqual(snipe_tau_bounds({"snipe_last_secs": 5}, 1500), (2.0, 5.0))
        # ...and the legacy snipe_last_secs: 6 still yields a sane band.
        self.assertEqual(snipe_tau_bounds({"snipe_last_secs": 6}, 1500), (2.0, 6.0))

    def test_fill_lands_before_close(self):
        """D1 regression guard: no tau in the band may put the fill at/after close.

        For every tau the gate admits, tau - latency must leave at least
        snipe_fill_margin_secs of slack before the close. Fails if anyone
        reintroduces a firing point at tau < latency.
        """
        margin = SHIPPED["snipe_fill_margin_secs"]
        for latency_ms in (0, 250, 800, 1000, 1500, 2000, 3000):
            lo, hi = snipe_tau_bounds(SHIPPED, latency_ms)
            tau = lo
            while tau <= hi + 1e-9:
                self.assertGreaterEqual(round(tau - latency_ms / 1000.0, 9), margin,
                                        f"tau={tau} latency_ms={latency_ms} fills too late")
                tau += 0.1

    def test_band_is_non_empty(self):
        lo, hi = snipe_tau_bounds(SHIPPED, 1500)
        self.assertLess(lo, hi)
        # A misconfiguration must be *detectable* (lo >= hi) rather than a band
        # that silently never fires without any way to notice.
        bad_lo, bad_hi = snipe_tau_bounds({"snipe_last_secs": 2}, 3000)
        self.assertGreaterEqual(bad_lo, bad_hi)


if __name__ == "__main__":
    unittest.main()
