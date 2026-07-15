import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.config import Config
from polybot.fill_engine import FillAttempt, LevelFill, WalkResult
from polybot.ledger import Ledger
from polybot.strategy import SnipeSignal, WinnerDetermination


def _make_config(tmpdir: Path) -> Config:
    raw = {
        "mode": {"paper": True},
        "endpoints": {"gamma_base": "x", "clob_base": "x", "clob_ws": "x",
                      "binance_rest_base": "x", "binance_ws_base": "x"},
        "fees": {"fee_rate": 0.07},
        "families": {"1h": {"enabled": True, "duration_secs": 3600, "oracle": "binance",
                             "close_snipe": True, "settle_sweep": True}},
        "strategy": {"close_snipe": {}, "settle_sweep": {}},
        "sizing": {"per_event_cap_usd": 25, "max_open_notional": 250},
        "execution": {"latency_ms": 1500, "order_book_poll_secs": 1.0, "use_websocket": False},
        "resolution": {"gamma_poll_secs": 15, "resolution_timeout_mins": 30},
        "discovery": {"poll_secs": 30, "hourly_lookahead_hours": 3, "events_limit": 200,
                      "markets_limit": 1000},
        "logging": {"level": "INFO", "file": str(tmpdir / "polybot.log"), "max_bytes": 1000,
                    "backup_count": 1},
        "status": {"write_interval_secs": 3, "http_port_env": "POLYBOT_PORT",
                   "http_port_default": 8899, "recent_events_keep": 200},
        "storage": {"sqlite_path": str(tmpdir / "polybot.db"), "fills_csv": str(tmpdir / "fills.csv"),
                    "pnl_csv": str(tmpdir / "pnl.csv")},
    }
    return Config(raw=raw, path=tmpdir / "config.yaml")


class TestLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = _make_config(Path(self._tmp.name))
        self.ledger = Ledger(self.config)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _fill_attempt(self, price=0.6, shares=40.0, outcome="filled"):
        walk = WalkResult(fills=[LevelFill(price=price, shares=shares,
                                            fee_per_share=0.07 * price * (1 - price))]
                           if outcome == "filled" else [])
        return FillAttempt(token_id="TOK", side="up", signal_time=time.time() - 1.5,
                            fill_time=time.time(), latency_ms=1500, book_at_signal=None,
                            book_at_fill=None, walk=walk, edge_min=0.05, outcome=outcome)

    def test_signal_and_fill_roundtrip(self):
        sig = SnipeSignal(market_slug="slug1", family="1h", side="up", token_id="TOK",
                           fair=0.7, ask=0.6, edge=0.08, tau_secs=4.0, s_t=65000, s_open=64950,
                           sigma_1s=0.0001)
        sig_id = self.ledger.record_snipe_signal(sig)
        self.assertIsInstance(sig_id, int)
        attempt = self._fill_attempt()
        fid = self.ledger.record_fill(strategy="close_snipe", family="1h", market_slug="slug1",
                                       signal_id=sig_id, attempt=attempt, edge_min=0.05)
        self.assertIsInstance(fid, int)
        positions = self.ledger.open_position_summary("slug1")
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0]["shares"], 40.0)
        self.assertAlmostEqual(positions[0]["avg_price"], 0.6)

    def test_resolve_market_computes_correct_pnl_for_winner(self):
        attempt = self._fill_attempt(price=0.6, shares=40.0)
        self.ledger.record_fill(strategy="close_snipe", family="1h", market_slug="slug2",
                                 signal_id=None, attempt=attempt, edge_min=0.05)
        wd = WinnerDetermination(winner="up", s_open=100, s_close=101, reason="test")
        res = self.ledger.resolve_market("slug2", "1h", time.time(), wd, gamma_winner="up",
                                          gamma_closed=True, resolution_source="gamma")
        self.assertEqual(res["resolved_winner"], "up")
        row = res["pnl_rows"][0]
        expected_fees = 40.0 * 0.07 * 0.6 * 0.4
        expected_pnl = 40.0 * (1.0 - 0.6) - expected_fees
        self.assertAlmostEqual(row["realized_pnl"], expected_pnl, places=6)
        self.assertFalse(row["disagreement"])

    def test_resolve_market_computes_correct_pnl_for_loser(self):
        attempt = self._fill_attempt(price=0.6, shares=40.0)
        self.ledger.record_fill(strategy="close_snipe", family="1h", market_slug="slug3",
                                 signal_id=None, attempt=attempt, edge_min=0.05)
        wd = WinnerDetermination(winner="down", s_open=100, s_close=99, reason="test")
        res = self.ledger.resolve_market("slug3", "1h", time.time(), wd, gamma_winner="down",
                                          gamma_closed=True, resolution_source="gamma")
        row = res["pnl_rows"][0]
        expected_fees = 40.0 * 0.07 * 0.6 * 0.4
        expected_pnl = 40.0 * (0.0 - 0.6) - expected_fees
        self.assertAlmostEqual(row["realized_pnl"], expected_pnl, places=6)

    def test_flags_disagreement_between_oracle_and_gamma(self):
        attempt = self._fill_attempt(price=0.5, shares=10.0)
        self.ledger.record_fill(strategy="settle_sweep", family="5m", market_slug="slug4",
                                 signal_id=None, attempt=attempt, edge_min=0.02)
        wd = WinnerDetermination(winner="up", s_open=100, s_close=101, reason="test")
        res = self.ledger.resolve_market("slug4", "5m", time.time(), wd, gamma_winner="down",
                                          gamma_closed=True, resolution_source="gamma")
        self.assertTrue(res["disagreement"])

    def test_empty_book_attempts_are_recorded_but_not_positions(self):
        attempt = self._fill_attempt(outcome="empty_book")
        self.ledger.record_fill(strategy="settle_sweep", family="5m", market_slug="slug5",
                                 signal_id=None, attempt=attempt, edge_min=0.02)
        self.assertEqual(self.ledger.open_position_summary("slug5"), [])
        self.assertEqual(self.ledger.unresolved_markets(), [])
        metrics = self.ledger.metrics()
        self.assertEqual(metrics["attempts_by_outcome"].get("empty_book"), 1)

    def test_total_open_notional_aggregates_across_markets(self):
        a = self._fill_attempt(price=0.5, shares=10.0)   # cost 5.0
        b = self._fill_attempt(price=0.25, shares=8.0)   # cost 2.0
        self.ledger.record_fill(strategy="close_snipe", family="1h", market_slug="m1",
                                 signal_id=None, attempt=a, edge_min=0.05)
        self.ledger.record_fill(strategy="close_snipe", family="1h", market_slug="m2",
                                 signal_id=None, attempt=b, edge_min=0.05)
        self.assertAlmostEqual(self.ledger.total_open_notional(), 7.0, places=6)


if __name__ == "__main__":
    unittest.main()
