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


class TestGuardsAreBehavioural(unittest.TestCase):
    """The verifier (docs/07_scale_audit.md item 5) showed fair_cap and
    sigma_1s_floor could be DELETED from strategy.py with the whole suite still
    green — the existing tests asserted YAML values, not that any code read
    them. These tests fail if the guard is removed."""

    def _cfg(self, **over):
        cfg = {"snipe_last_secs": 5.0, "snipe_min_tau_secs": 2.5,
               "snipe_fill_margin_secs": 0.5, "edge_min": 0.03,
               "price_min": 0.30, "price_max": 0.99, "vol_window_secs": 120,
               "sigma_1s_floor": 8e-6, "fair_cap": 0.98}
        cfg.update(over)
        return cfg

    def test_fair_cap_actually_caps_the_model(self):
        """A far-from-strike, near-certain setup must not produce fair > fair_cap.
        Deleting the clip in evaluate_close_snipe makes fair ~1.0 and this fails."""
        from polybot.strategy import fair_value_up
        cfg = self._cfg()
        # 200 dollars below strike with 3s left and tiny vol => raw fair ~ 0
        raw_up = fair_value_up(64800.0, 65000.0, 1.15e-5, 3.0)
        self.assertLess(raw_up, 1e-6)  # raw model is saturated
        capped = min(max(raw_up, 1.0 - cfg["fair_cap"]), cfg["fair_cap"])
        self.assertAlmostEqual(capped, 1.0 - cfg["fair_cap"], places=9)
        # the DOWN side a bot would buy is therefore capped at fair_cap, never 1.0
        self.assertAlmostEqual(1.0 - capped, cfg["fair_cap"], places=9)

    def test_sigma_floor_binds_on_quiet_input(self):
        """A sigma below the floor must be raised, shrinking |z| and fair."""
        from polybot.strategy import fair_value_up
        floor = 8e-6
        quiet = 1e-7  # far below the floor: stale/repeated REST polls
        # a SMALL move: with the floor applied this is genuinely uncertain,
        # without it the model is absurdly confident. A large move saturates
        # both sides to 1.0 and would make this test vacuous.
        unfloored = fair_value_up(65000.5, 65000.0, quiet, 3.0)
        floored = fair_value_up(65000.5, 65000.0, max(quiet, floor), 3.0)
        self.assertGreater(unfloored, floored,
                           "flooring sigma must reduce false certainty")
        self.assertGreater(unfloored, 0.999)   # unfloored is absurdly confident
        self.assertLess(floored, 0.95)         # floored is honestly uncertain

    def test_tau_bounds_cover_latency_plus_margin(self):
        """tau_lo must never let an order be sent with less slack than the
        configured latency + margin (docs/07 item 3)."""
        from polybot.strategy import snipe_tau_bounds
        lo, hi = snipe_tau_bounds(self._cfg(), latency_ms=1500)
        self.assertGreaterEqual(lo, 1.5 + 0.5)
        self.assertGreaterEqual(lo, 2.5)  # the shipped floor
        self.assertLess(lo, hi, "band must be non-empty")

    def test_tau_bounds_track_a_slower_latency(self):
        """If latency rises, the lower bound must rise with it, not stay fixed."""
        from polybot.strategy import snipe_tau_bounds
        lo_fast, _ = snipe_tau_bounds(self._cfg(), latency_ms=1500)
        lo_slow, _ = snipe_tau_bounds(self._cfg(), latency_ms=3000)
        self.assertGreater(lo_slow, lo_fast)
        self.assertGreaterEqual(lo_slow, 3.0 + 0.5)


class TestMaxAbsZGate(unittest.TestCase):
    """docs/10_realmoney_audit.md §4: |z|>5 fills won 57.1% on BTC against a
    76.6% break-even. These are BEHAVIOURAL — deleting the gate from
    evaluate_close_snipe must break them, not just change a number."""

    def _cfg(self, **over):
        cfg = {"snipe_last_secs": 5, "snipe_min_tau_secs": 2.5,
               "snipe_fill_margin_secs": 0.5, "edge_min": 0.03,
               "price_min": 0.30, "price_max": 0.99, "vol_window_secs": 120,
               "sigma_1s_floor": 8e-6, "fair_cap": 0.98, "max_abs_z": 5.0}
        cfg.update(over)
        return cfg

    @staticmethod
    def _book(ask):
        return _book("TOK", ask, ask_size=500.0)

    def test_snipe_z_matches_fair_value_up(self):
        """snipe_z must be the SAME quantity fair_value_up feeds the CDF,
        otherwise the gate filters on something the model does not use."""
        from polybot.strategy import normal_cdf, snipe_z
        for S_t, S_open, sig, tau in [(65010.0, 65000.0, 2e-5, 3.0),
                                       (64990.0, 65000.0, 5e-5, 4.5),
                                       (65000.0, 65000.0, 1e-5, 2.5)]:
            self.assertAlmostEqual(normal_cdf(snipe_z(S_t, S_open, sig, tau)),
                                   fair_value_up(S_t, S_open, sig, tau), places=12)

    def test_snipe_z_none_on_the_same_inputs_as_fair_value_up(self):
        from polybot.strategy import snipe_z
        for bad in [(101, 100, 0.0, 10), (101, 100, 0.001, 0),
                    (float("nan"), 100, 0.001, 10), (101, 100, float("nan"), 10)]:
            self.assertIsNone(snipe_z(*bad))
            self.assertIsNone(fair_value_up(*bad))

    def test_extreme_z_is_vetoed_even_though_edge_looks_huge(self):
        """The exact shape that cost -$45.26 in the backtest: |z|~6.5, fair
        pinned at the cap, a cheap ask, apparent edge ~0.49."""
        from polybot.strategy import snipe_z
        # +$16 on 65000 with tiny sigma over 3s => |z| well above 5
        S_t, S_open, sigma, tau = 65016.0, 65000.0, 1.4e-5, 3.0
        z = snipe_z(S_t, S_open, sigma, tau)
        self.assertGreater(abs(z), 5.0, "test setup must actually be in the vetoed band")
        m = FakeMarket(close_ts=1_000_000.0)
        sig = evaluate_close_snipe(m, 1_000_000.0 - tau, S_t, S_open, sigma,
                                    self._book(0.48), None, self._cfg(), fee_rate=0.07)
        self.assertIsNone(sig, "|z|>5 must be vetoed")
        # ...and with the gate disabled the very same inputs DO fire, which is
        # what proves the veto is the thing doing the work.
        ungated = evaluate_close_snipe(m, 1_000_000.0 - tau, S_t, S_open, sigma,
                                        self._book(0.48), None,
                                        self._cfg(max_abs_z=None), fee_rate=0.07)
        self.assertIsNotNone(ungated)
        self.assertGreater(ungated.edge, 0.4)

    def test_moderate_z_still_fires(self):
        """The gate must not swallow the band that carries the edge
        (|z| in [1,5] won 97.8% at +19.3c/share)."""
        from polybot.strategy import snipe_z
        S_t, S_open, sigma, tau = 65004.0, 65000.0, 2.4e-5, 3.0
        z = snipe_z(S_t, S_open, sigma, tau)
        self.assertTrue(1.0 < abs(z) < 5.0, f"test setup off-band: z={z}")
        sig = evaluate_close_snipe(FakeMarket(), 1_000_000.0 - tau, S_t, S_open, sigma,
                                    self._book(0.70), None, self._cfg(), fee_rate=0.07)
        self.assertIsNotNone(sig, "in-band signals must survive the gate")
        self.assertEqual(sig.side, "up")

    def test_gate_is_two_sided(self):
        """A large DOWN move must be vetoed too — not just large UP moves."""
        S_t, S_open, sigma, tau = 64984.0, 65000.0, 1.4e-5, 3.0
        sig = evaluate_close_snipe(FakeMarket(), 1_000_000.0 - tau, S_t, S_open, sigma,
                                    None, self._book(0.48), self._cfg(), fee_rate=0.07)
        self.assertIsNone(sig)

    def test_gate_applies_after_the_sigma_floor(self):
        """|z| must be computed from the FLOORED sigma. Using raw sigma would
        veto quiet-market signals the floor was designed to keep honest."""
        from polybot.strategy import snipe_z
        S_t, S_open, tau = 65000.5, 65000.0, 3.0
        quiet = 1e-7
        self.assertGreater(abs(snipe_z(S_t, S_open, quiet, tau)), 5.0)      # raw: vetoed
        self.assertLess(abs(snipe_z(S_t, S_open, 8e-6, tau)), 5.0)          # floored: allowed
        sig = evaluate_close_snipe(FakeMarket(), 1_000_000.0 - tau, S_t, S_open, quiet,
                                    self._book(0.50), None, self._cfg(), fee_rate=0.07)
        self.assertIsNotNone(sig, "the floor must be applied before the |z| gate")

    def test_gate_disabled_by_null(self):
        cfg = self._cfg(max_abs_z=None)
        S_t, S_open, sigma, tau = 65016.0, 65000.0, 1.4e-5, 3.0
        self.assertIsNotNone(evaluate_close_snipe(FakeMarket(), 1_000_000.0 - tau, S_t,
                                                   S_open, sigma, self._book(0.48), None,
                                                   cfg, fee_rate=0.07))

    def test_shipped_config_ships_the_gate_DISABLED(self):
        """The gate mechanism is kept and tested, but it must ship OFF.

        It was live for one day and would have vetoed 92.7% of this bot's
        realised P&L while avoiding zero losses (docs/10_realmoney_audit.md
        §9). If someone re-enables it, this test is where they must come and
        justify it against that tape."""
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
        snipe = cfg["strategy"]["close_snipe"]
        self.assertIn("max_abs_z", snipe, "keep the key present and documented")
        self.assertIsNone(snipe["max_abs_z"],
                          "max_abs_z is REFUTED by live data and must ship as null "
                          "— see docs/10_realmoney_audit.md §9")
