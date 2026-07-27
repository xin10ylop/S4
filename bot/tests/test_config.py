import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.config import load_config


class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        os.environ.pop("POLYBOT_LIVE", None)

    def tearDown(self):
        os.environ.pop("POLYBOT_LIVE", None)

    def test_loads_real_config_yaml(self):
        c = load_config()
        self.assertTrue(c.paper)
        self.assertIn("1h", c.families())
        self.assertEqual(c.families()["1h"].close_snipe, True)
        self.assertEqual(c.families()["5m"].close_snipe, False)  # off by default (no Chainlink)
        self.assertAlmostEqual(c.fee_rate, 0.07)

    def test_a4_shipped_values_are_pinned(self):
        """Regression pins for audit/A4_change_spec.md so a later edit cannot
        silently undo the measured configuration."""
        c = load_config()
        self.assertEqual(c.families()["1h"].settle_sweep, False)   # (iv) 1235 signals, 0 fills
        self.assertAlmostEqual(c.snipe_cfg["edge_min"], 0.03)      # (ii)
        self.assertEqual(c.snipe_cfg["snipe_last_secs"], 5)        # (i)
        self.assertAlmostEqual(c.snipe_cfg["snipe_min_tau_secs"], 2.0)
        self.assertAlmostEqual(c.snipe_cfg["snipe_fill_margin_secs"], 0.5)
        self.assertEqual(c.sizing_cfg["per_event_cap_usd"], 250)   # (iii)
        self.assertEqual(c.sizing_cfg["max_open_notional"], 1000)
        # settle_sweep must not inherit the raised close_snipe clip
        self.assertEqual(c.settle_cfg["cap_usd"], 25)

    def test_safety_invariants_still_hold(self):
        """The cap raise must not have loosened any of the guards."""
        c = load_config()
        self.assertTrue(c.paper)                                    # PAPER is still the default
        self.assertAlmostEqual(c.snipe_cfg["fair_cap"], 0.98)       # certainty cap intact
        self.assertGreater(float(c.snipe_cfg["sigma_1s_floor"]), 0)  # sigma floor intact
        self.assertAlmostEqual(c.execution_cfg["max_walk_above_best"], 0.03)  # walk bound intact
        self.assertGreater(float(c.sizing_cfg["max_open_notional"]),
                           float(c.sizing_cfg["per_event_cap_usd"]))

    def test_paper_true_by_default_regardless_of_env(self):
        os.environ["POLYBOT_LIVE"] = "1"
        c = load_config()  # config.yaml has mode.paper: true
        self.assertTrue(c.paper)  # config says paper -> stays paper even if env says live

    def test_live_requires_both_config_and_env(self):
        import yaml
        from polybot.config import Config
        with open(Path(__file__).resolve().parent.parent / "config.yaml") as f:
            raw = yaml.safe_load(f)
        raw["mode"]["paper"] = False

        os.environ.pop("POLYBOT_LIVE", None)
        c1 = Config(raw=raw, path=Path("x"))
        self.assertTrue(c1.paper)  # config says live, but env doesn't confirm -> stays paper

        os.environ["POLYBOT_LIVE"] = "1"
        c2 = Config(raw=raw, path=Path("x"))
        self.assertFalse(c2.paper)  # both agree -> live


if __name__ == "__main__":
    unittest.main()
