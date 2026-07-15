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


if __name__ == "__main__":
    unittest.main()
