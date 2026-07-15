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
