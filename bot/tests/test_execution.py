import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.config import load_config
from polybot.execution import ExecutionRouter, LiveTradingDisabled
from polybot.polymarket import ClobClientREST


class TestExecutionRouterGuards(unittest.TestCase):
    def setUp(self):
        os.environ.pop("POLYBOT_LIVE", None)
        os.environ.pop("POLYBOT_PK", None)

    def tearDown(self):
        os.environ.pop("POLYBOT_LIVE", None)
        os.environ.pop("POLYBOT_PK", None)

    def _router(self):
        config = load_config()
        return ExecutionRouter(config, ClobClientREST(config))

    def test_default_config_is_paper(self):
        router = self._router()
        self.assertFalse(router.is_live())

    def test_env_alone_does_not_arm_live(self):
        os.environ["POLYBOT_LIVE"] = "1"
        router = self._router()
        # config.yaml still has mode.paper: true -> must stay paper
        self.assertFalse(router.is_live())

    def test_live_client_refuses_without_private_key(self):
        import yaml
        from polybot.config import Config
        with open(Path(__file__).resolve().parent.parent / "config.yaml") as f:
            raw = yaml.safe_load(f)
        raw["mode"]["paper"] = False
        os.environ["POLYBOT_LIVE"] = "1"
        config = Config(raw=raw, path=Path("x"))
        router = ExecutionRouter(config, ClobClientREST(config))
        self.assertTrue(router.is_live())
        with self.assertRaises(LiveTradingDisabled):
            router._get_live_client()

    def test_warm_cache_is_noop_in_paper_mode(self):
        router = self._router()
        # Must not raise / must not attempt to construct a live client.
        with mock.patch.object(router, "_get_live_client") as m:
            router.warm_cache("some-token-id")
            m.assert_not_called()


class TestParseFilledSize(unittest.TestCase):
    """docs/10_realmoney_audit.md §2. The FAK response tells us how much
    actually matched; recording the INTENDED walk instead makes every risk
    control run on a position we may not own."""

    def test_unknown_schema_returns_none_not_a_guess(self):
        from polybot.execution import _parse_filled_size
        self.assertIsNone(_parse_filled_size({"orderID": "0xabc", "status": "matched"}))
        self.assertIsNone(_parse_filled_size(None))
        self.assertIsNone(_parse_filled_size("ok"))

    def test_explicit_failure_is_zero_shares(self):
        from polybot.execution import _parse_filled_size
        self.assertEqual(_parse_filled_size({"success": False, "errorMsg": "not enough balance"}),
                         0.0)

    def test_recognised_size_fields(self):
        from polybot.execution import _parse_filled_size
        for key in ("makingAmount", "sizeMatched", "matched_size", "filledSize"):
            self.assertEqual(_parse_filled_size({"success": True, key: "12.5"}), 12.5,
                             f"{key} should be recognised")

    def test_nested_payload(self):
        from polybot.execution import _parse_filled_size
        self.assertEqual(_parse_filled_size({"success": True, "order": {"sizeMatched": 7.0}}), 7.0)

    def test_unparseable_value_does_not_crash_or_lie(self):
        from polybot.execution import _parse_filled_size
        self.assertIsNone(_parse_filled_size({"sizeMatched": "n/a"}))


class TestWalkTruncation(unittest.TestCase):
    """A partial FAK fill got the CHEAP levels. Truncating from the expensive
    end would understate our average price and overstate P&L."""

    def _walk(self):
        from polybot.fill_engine import LevelFill, WalkResult
        return WalkResult(fills=[
            LevelFill(price=0.70, shares=10.0, fee_per_share=0.0147),
            LevelFill(price=0.72, shares=10.0, fee_per_share=0.0141),
            LevelFill(price=0.74, shares=10.0, fee_per_share=0.0135),
        ])

    def test_keeps_cheapest_levels_first(self):
        t = self._walk().truncated_to_shares(15.0)
        self.assertAlmostEqual(t.total_shares, 15.0)
        # 10 @ 0.70 + 5 @ 0.72 => avg 0.70667, NOT 0.73 (which is what keeping
        # the expensive end would give)
        self.assertAlmostEqual(t.avg_price, (10 * 0.70 + 5 * 0.72) / 15.0, places=9)

    def test_truncation_never_flatters_the_average_price(self):
        full = self._walk()
        for filled in (1.0, 5.0, 12.0, 19.9, 25.0):
            t = full.truncated_to_shares(filled)
            self.assertLessEqual(t.avg_price, full.avg_price + 1e-12,
                                 "a partial fill can only be CHEAPER on average, never dearer")

    def test_zero_and_overfill_edges(self):
        full = self._walk()
        self.assertEqual(full.truncated_to_shares(0.0).total_shares, 0.0)
        self.assertEqual(full.truncated_to_shares(-3.0).total_shares, 0.0)
        self.assertIs(full.truncated_to_shares(30.0), full)
        self.assertIs(full.truncated_to_shares(999.0), full)

    def test_fees_scale_with_the_truncated_size(self):
        t = self._walk().truncated_to_shares(10.0)
        self.assertAlmostEqual(t.total_fees, 10.0 * 0.0147, places=9)


if __name__ == "__main__":
    unittest.main()
