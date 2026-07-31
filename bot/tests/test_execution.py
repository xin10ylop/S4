import os
import sys
import time
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


class TestPlaceLiveOrder(unittest.TestCase):
    """End-to-end exercise of `_place_live_order` — the ONLY code path that
    spends real money, and the one an independent audit flagged as having no
    test at all. The py-clob-client is stubbed; everything else is real.

    These are the tests that kill the three mutations which survived the first
    run of scripts/m4/mutation_check.py: partial fills not truncated, a
    zero-match FAK recorded as a fill, and the exchange minimum not enforced.
    """

    def setUp(self):
        import yaml
        from polybot.config import Config
        from polybot.polymarket import BookLevel, OrderBook
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        raw["mode"] = {"paper": False}
        self.book_levels = [BookLevel(0.50, 400.0), BookLevel(0.52, 400.0)]

        class _Clob:
            def get_book(_self, token_id):
                return OrderBook(token_id=token_id, bids=[],
                                 asks=list(self.book_levels), fetched_at=time.time())

        self.router = ExecutionRouter(Config(raw=raw, path=cfg_path), _Clob())
        self.posted = []

        class _FakeClient:
            def create_order(_self, order_args):
                return {"signed": True, "size": order_args.size, "price": order_args.price}

            def post_order(_self, signed, order_type):
                self.posted.append(signed)
                return self.response

        self.router._get_live_client = lambda: _FakeClient()
        self.response = {"success": True}

    def _run(self, order_min_size=5.0, cap_usd=100.0):
        # edge_fn generous enough that both levels are profitable
        return self.router._place_live_order(
            "TOK", "up", lambda p: 0.95 - p, 0.01, 0.30, 0.99, cap_usd, 0.03,
            order_min_size=order_min_size)

    def test_full_fill_records_the_full_walk(self):
        self.response = {"success": True, "sizeMatched": 200.0}
        att = self._run()
        self.assertEqual(att.outcome, "filled")
        self.assertAlmostEqual(att.walk.total_shares, 200.0)
        self.assertEqual(len(self.posted), 1, "exactly one order must be submitted")

    def test_partial_fill_is_truncated_to_what_actually_matched(self):
        """The mutation `walk = walk.truncated_to_shares(filled)` -> pass must
        fail here. Without it the ledger books 200 shares we do not own."""
        self.response = {"success": True, "sizeMatched": 60.0}
        att = self._run()
        self.assertEqual(att.outcome, "filled")
        self.assertAlmostEqual(att.walk.total_shares, 60.0,
                               msg="a partial fill must not book the intended size")
        # 60 shares all came from the 0.50 level, so cost is 60*0.50, not a
        # blend that includes the 0.52 level we never reached.
        self.assertAlmostEqual(att.walk.total_cost, 30.0, places=6)
        self.assertAlmostEqual(att.walk.avg_price, 0.50, places=9)

    def test_zero_match_is_not_recorded_as_a_fill(self):
        """The mutation `elif filled <= 0:` -> `elif False:` must fail here.
        A FAK that matched nothing booking a full winning position is the
        single worst failure mode in the live path."""
        self.response = {"success": True, "sizeMatched": 0.0}
        att = self._run()
        self.assertNotEqual(att.outcome, "filled")
        self.assertEqual(att.outcome, "book_moved_no_edge")
        self.assertEqual(att.walk.total_shares, 0.0)

    def test_rejected_order_is_zero_shares_not_a_fill(self):
        self.response = {"success": False, "errorMsg": "not enough balance"}
        att = self._run()
        self.assertEqual(att.outcome, "book_moved_no_edge")
        self.assertEqual(att.walk.total_shares, 0.0)

    def test_unknown_response_schema_is_flagged_not_assumed(self):
        self.response = {"orderID": "0xabc"}          # no recognised size field
        att = self._run()
        self.assertEqual(att.outcome, "filled_unverified",
                         "an unparseable response must be flagged for reconciliation, "
                         "never silently treated as a confirmed fill")

    def test_below_minimum_size_is_refused_not_submitted(self):
        """The mutation `if size < order_min_size:` -> `if False:` must fail
        here. Submitting a sub-minimum order gets it rejected by the CLOB, and
        the engine's fill worker swallows that exception — so it would show up
        as silence, not as an error."""
        from polybot.polymarket import BookLevel
        self.book_levels = [BookLevel(0.50, 2.0)]     # 2 shares < the 5-share minimum
        att = self._run(order_min_size=5.0)
        self.assertEqual(att.outcome, "below_min_size")
        self.assertEqual(self.posted, [], "no order may reach the CLOB below its minimum")

    def test_size_at_exactly_the_minimum_is_allowed(self):
        from polybot.polymarket import BookLevel
        self.book_levels = [BookLevel(0.50, 5.0)]
        self.response = {"success": True, "sizeMatched": 5.0}
        att = self._run(order_min_size=5.0)
        self.assertEqual(att.outcome, "filled")
        self.assertEqual(len(self.posted), 1)

    def test_stale_book_is_refused_before_any_order(self):
        from polybot.polymarket import BookLevel, OrderBook

        class _StaleClob:
            def get_book(_self, token_id):
                return OrderBook(token_id=token_id, bids=[], asks=[BookLevel(0.50, 400.0)],
                                 fetched_at=time.time(), book_ts=time.time() - 600.0)

        self.router.clob_rest = _StaleClob()
        att = self.router._place_live_order("TOK", "up", lambda p: 0.95 - p, 0.01, 0.30,
                                             0.99, 100.0, 0.03, max_book_age_s=5.0)
        self.assertEqual(att.outcome, "stale_book")
        self.assertEqual(self.posted, [])


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
