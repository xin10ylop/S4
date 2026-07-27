"""Behavioural tests for the three M4 risk guards.

DESIGN RULE FOR THIS FILE: every test here must FAIL if the guard it covers is
deleted from the source. A previous audit found a guard that could be removed
entirely with a green suite, so these are written against OBSERVABLE BEHAVIOUR
(fills dispatched / shares taken / positions blocked), not against the
existence of a function or a config key.

The deletion checks that were actually run are recorded in
audit/M4_risk_guards.md §5.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from polybot.config import Config
from polybot.depth import DepthTracker, max_level_shares
from polybot.engine import Engine
from polybot.execution import ExecutionRouter
from polybot.fill_engine import execute_taker_signal, fee_per_share, walk_asks
from polybot.ledger import Ledger
from polybot.oracle import BinanceOracle, PricePoint
from polybot.polymarket import BookLevel, OrderBook
from polybot.risk import (CircuitBreaker, WarmupGate, resolve_daily_limit, utc_day_str,
                          utc_midnight)
from polybot.status_server import StatusState
from polybot.strategy import WinnerDetermination

from tests.test_engine_gating import (CONFIG_YAML, FakeMarket, StubBinance, StubClob,
                                       StubExecutor, StubLedger, warm)

FEE = 0.07
EDGE_FN = lambda p: 1.0 - p - fee_per_share(p, FEE)  # noqa: E731 - settle-style edge


def _engine(tmpdir: str, **patch) -> Engine:
    """Engine on the SHIPPED config with optional deep-merged overrides."""
    with open(CONFIG_YAML) as f:
        raw = yaml.safe_load(f)
    raw["storage"] = {
        "sqlite_path": str(Path(tmpdir) / "t.db"),
        "fills_csv": str(Path(tmpdir) / "fills.csv"),
        "pnl_csv": str(Path(tmpdir) / "pnl.csv"),
    }
    raw["logging"] = dict(raw["logging"], file=str(Path(tmpdir) / "t.log"))
    raw.setdefault("risk", {})["override_path"] = str(Path(tmpdir) / "override.json")
    for path, value in patch.items():
        node = raw
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    eng = Engine(Config(raw=raw, path=CONFIG_YAML))
    eng.ledger = StubLedger()
    eng.breaker.ledger = eng.ledger
    eng.executor = StubExecutor()
    return eng


# ===========================================================================
# GUARD 1 — adverse-size filter
# ===========================================================================

class TestDepthStatistic(unittest.TestCase):
    """The statistic itself: median of recent IN-BAND ask-level sizes."""

    def test_reference_is_none_until_min_samples(self):
        t = DepthTracker(history_n=100, min_samples=5)
        for _ in range(4):
            t.observe_levels("1h", [BookLevel(0.50, 10.0)], 0.30, 0.99)
        self.assertIsNone(t.reference("1h"))
        t.observe_levels("1h", [BookLevel(0.50, 10.0)], 0.30, 0.99)
        self.assertEqual(t.reference("1h"), 10.0)

    def test_out_of_band_levels_are_not_recorded(self):
        """The whole statistic dies if 24,000-share lottery tickets at $0.01 are
        pooled with 10-share contested quotes — measured medians differ by 3
        orders of magnitude (see depth.py)."""
        t = DepthTracker(history_n=100, min_samples=3)
        t.observe_levels("1h", [BookLevel(0.01, 24_000.0), BookLevel(0.995, 50_000.0)],
                          0.30, 0.99)
        self.assertEqual(t.n_samples("1h"), 0)
        self.assertIsNone(t.reference("1h"))
        t.observe_levels("1h", [BookLevel(0.50, 10.0), BookLevel(0.60, 12.0),
                                 BookLevel(0.70, 14.0)], 0.30, 0.99)
        self.assertEqual(t.n_samples("1h"), 3)
        self.assertEqual(t.reference("1h"), 12.0)

    def test_reference_is_per_family(self):
        t = DepthTracker(history_n=100, min_samples=2)
        t.observe_levels("1h", [BookLevel(0.5, 10.0), BookLevel(0.5, 10.0)], 0.3, 0.99)
        t.observe_levels("5m", [BookLevel(0.5, 900.0), BookLevel(0.5, 900.0)], 0.3, 0.99)
        self.assertEqual(t.reference("1h"), 10.0)
        self.assertEqual(t.reference("5m"), 900.0)

    def test_history_is_bounded(self):
        t = DepthTracker(history_n=10, min_samples=1)
        for i in range(50):
            t.observe_levels("1h", [BookLevel(0.5, float(i))], 0.3, 0.99)
        self.assertEqual(t.n_samples("1h"), 10)

    def test_max_level_shares_inert_when_disabled_or_thin(self):
        t = DepthTracker(history_n=100, min_samples=5)
        for _ in range(10):
            t.observe_levels("1h", [BookLevel(0.5, 20.0)], 0.3, 0.99)
        self.assertIsNone(max_level_shares({"enabled": False, "max_size_ratio": 8}, t, "1h"))
        self.assertEqual(max_level_shares({"enabled": True, "max_size_ratio": 8}, t, "1h"),
                         160.0)
        # unknown family -> no reference -> inert
        self.assertIsNone(max_level_shares({"enabled": True, "max_size_ratio": 8}, t, "5m"))


class TestAdverseSizeWalk(unittest.TestCase):
    def test_off_by_default_takes_full_depth(self):
        """Backward compatibility: no max_level_shares == pre-M4 behaviour."""
        asks = [BookLevel(0.50, 5000.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE)
        self.assertAlmostEqual(r.total_shares, 500.0, places=6)   # cap-bound, not size-bound
        self.assertFalse(r.size_filter_bound)

    def test_cap_mode_limits_shares_taken_from_anomalous_level(self):
        asks = [BookLevel(0.50, 5000.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE, max_level_shares=100.0,
                      anomalous_mode="cap")
        self.assertAlmostEqual(r.total_shares, 100.0, places=6)
        self.assertEqual(r.n_levels_capped, 1)
        self.assertEqual(r.n_levels_skipped, 0)
        self.assertGreater(r.shares_suppressed, 0)

    def test_skip_mode_takes_nothing_from_anomalous_level(self):
        asks = [BookLevel(0.50, 5000.0), BookLevel(0.52, 40.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE, max_level_shares=100.0,
                      anomalous_mode="skip")
        self.assertEqual([f.price for f in r.fills], [0.52])
        self.assertAlmostEqual(r.total_shares, 40.0, places=6)
        self.assertEqual(r.n_levels_skipped, 1)

    def test_skip_cannot_rebaseline_the_walk_bound(self):
        """Skipping the top level must NOT let the walk chase 10c deeper — the
        max_walk_above_best anchor is the first PRICE-eligible level."""
        asks = [BookLevel(0.50, 5000.0), BookLevel(0.60, 40.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE, max_above_best=0.03,
                      max_level_shares=100.0, anomalous_mode="skip")
        self.assertEqual(r.fills, [])
        self.assertEqual(r.total_shares, 0.0)

    def test_normal_levels_are_untouched_by_the_filter(self):
        asks = [BookLevel(0.50, 50.0), BookLevel(0.51, 30.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE, max_level_shares=100.0)
        self.assertAlmostEqual(r.total_shares, 80.0, places=6)
        self.assertFalse(r.size_filter_bound)

    def test_outcome_flags_adverse_size_block(self):
        class _Clob:
            def get_book(self, token_id):
                return OrderBook(token_id=token_id, bids=[],
                                 asks=[BookLevel(0.50, 5000.0)], fetched_at=0.0)

        att = execute_taker_signal(_Clob(), "T", "up", EDGE_FN, 0.01, 0.3, 0.99, 250.0,
                                    FEE, latency_ms=0, sleep=False,
                                    max_level_shares=10.0, anomalous_mode="skip")
        self.assertFalse(att.filled)
        self.assertEqual(att.outcome, "adverse_size_blocked")


class TestAdverseSizeWiring(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def _fill_kwargs(self, eng):
        import inspect
        fn, args, kwargs = eng.executor.submissions[0]
        return inspect.signature(eng._run_fill).bind(*args, **kwargs).arguments

    def test_shipped_default_passes_no_ceiling(self):
        eng = _engine(self._tmp.name)
        eng.binance, eng.clob = StubBinance(self.now), StubClob()
        warm(eng, self.now)
        self.assertFalse(eng.config.adverse_size_cfg["enabled"])
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertIsNone(self._fill_kwargs(eng)["max_level_shares_"])

    def test_enabled_passes_ratio_times_reference(self):
        eng = _engine(self._tmp.name,
                      **{"strategy.close_snipe.adverse_size": {
                          "enabled": True, "mode": "skip", "max_size_ratio": 4.0,
                          "history_n": 200, "min_samples": 3}})
        eng.binance, eng.clob = StubBinance(self.now), StubClob()
        warm(eng, self.now)
        for _ in range(3):
            eng.depth.observe_levels("1h", [BookLevel(0.5, 25.0)], 0.30, 0.99)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        bound = self._fill_kwargs(eng)
        self.assertIsNotNone(bound["max_level_shares_"])
        self.assertEqual(bound["anomalous_mode"], "skip")
        # reference is the median of the observed in-band sizes (25), ratio 4
        self.assertAlmostEqual(bound["max_level_shares_"], 100.0, places=6)

    def test_engine_feeds_the_reference_from_books_it_already_fetched(self):
        eng = _engine(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = StubClob()   # 0.50 asks, size 5000 on both tokens
        warm(eng, self.now)
        self.assertEqual(eng.depth.n_samples("1h"), 0)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.depth.n_samples("1h"), 2)  # UP book + DOWN book

    def _live_router(self):
        with open(CONFIG_YAML) as f:
            raw = yaml.safe_load(f)
        raw["mode"] = {"paper": False}
        cfg = Config(raw=raw, path=CONFIG_YAML)

        class _Clob:
            def get_book(self, token_id):
                return OrderBook(token_id=token_id, bids=[],
                                 asks=[BookLevel(0.50, 5000.0)], fetched_at=0.0)

        return ExecutionRouter(cfg, _Clob())

    def test_live_router_forwards_the_filter_to_the_live_order_path(self):
        """A guard that only exists in the paper simulator is not a guard."""
        router = self._live_router()
        router.is_live = lambda: True
        seen = {}
        router._place_live_order = lambda *a, **kw: seen.update(kw) or "ok"
        router.place_taker_buy("T", "up", EDGE_FN, 0.01, 0.3, 0.99, 250.0, 0,
                                max_level_shares=7.0, anomalous_mode="skip")
        self.assertEqual(seen.get("max_level_shares"), 7.0)
        self.assertEqual(seen.get("anomalous_mode"), "skip")

    def test_live_order_sizing_honours_the_filter(self):
        """...and the live sizing walk actually applies it."""
        router = self._live_router()
        seen = {}
        import polybot.execution as ex
        real_walk = ex.walk_asks

        def spy(*a, **kw):
            seen.update(kw)
            return real_walk(*a, **kw)

        ex.walk_asks = spy
        router._get_live_client = lambda: object()   # create_order will fail after sizing
        try:
            router._place_live_order("T", "up", EDGE_FN, 0.01, 0.3, 0.99, 250.0, 0.03,
                                      max_level_shares=7.0, anomalous_mode="skip")
        except Exception:  # noqa: BLE001 - submitting needs a real client; we only
            pass           # care that walk_asks saw the guard arguments first
        finally:
            ex.walk_asks = real_walk
        self.assertEqual(seen.get("max_level_shares"), 7.0)
        self.assertEqual(seen.get("anomalous_mode"), "skip")


# ===========================================================================
# GUARD 2 — warmup after restart
# ===========================================================================

class _ColdOracle:
    """Oracle-shaped object with a configurable sample count."""

    def __init__(self, n, now=1_000_000.0):
        self._n = n
        self._pt = PricePoint(ts=now, price=101_000.0)

    def n_samples(self, window_secs=120.0):
        return self._n

    def latest(self):
        return self._pt

    def rolling_log_return_std(self, w):
        return 1e-4

    def hour_open_close(self, w):
        return 100_000.0, None


class TestWarmupGate(unittest.TestCase):
    CFG = {"enabled": True, "min_oracle_samples": 60, "min_uptime_secs": 120,
           "log_every_secs": 0}

    def test_blocks_on_thin_buffer(self):
        g = WarmupGate(self.CFG, started_at=0.0)
        st = g.check(_ColdOracle(59), 120.0, now=10_000.0)
        self.assertFalse(st.ready)
        self.assertEqual(st.reason, "insufficient_oracle_samples")

    def test_blocks_on_short_uptime_even_with_a_full_buffer(self):
        g = WarmupGate(self.CFG, started_at=1000.0)
        st = g.check(_ColdOracle(500), 120.0, now=1050.0)
        self.assertFalse(st.ready)
        self.assertEqual(st.reason, "insufficient_uptime")

    def test_ready_when_both_satisfied(self):
        g = WarmupGate(self.CFG, started_at=0.0)
        st = g.check(_ColdOracle(60), 120.0, now=10_000.0)
        self.assertTrue(st.ready)

    def test_oracle_without_n_samples_reads_as_cold(self):
        """Fail-safe: an oracle implementation predating this guard must NOT be
        treated as warm."""
        class _Legacy:
            pass

        g = WarmupGate(self.CFG, started_at=0.0)
        st = g.check(_Legacy(), 120.0, now=10_000.0)
        self.assertFalse(st.ready)
        self.assertEqual(st.samples, 0)

    def test_none_oracle_reads_as_cold(self):
        g = WarmupGate(self.CFG, started_at=0.0)
        self.assertFalse(g.check(None, 120.0, now=10_000.0).ready)

    def test_broken_oracle_reads_as_cold(self):
        class _Boom:
            def n_samples(self, w):
                raise RuntimeError("feed exploded")

        g = WarmupGate(self.CFG, started_at=0.0)
        self.assertFalse(g.check(_Boom(), 120.0, now=10_000.0).ready)

    def test_logs_while_warming(self):
        g = WarmupGate(self.CFG, started_at=0.0)
        st = g.check(_ColdOracle(1), 120.0, now=10_000.0)
        with self.assertLogs("polybot.risk", level="WARNING") as cm:
            g.log_progress(st, "1h", now=10_000.0)
        self.assertTrue(any("WARMING UP" in m for m in cm.output))

    def test_announces_completion_once(self):
        g = WarmupGate(self.CFG, started_at=0.0)
        st = g.check(_ColdOracle(200), 120.0, now=10_000.0)
        with self.assertLogs("polybot.risk", level="INFO") as cm:
            self.assertTrue(g.log_progress(st, "1h", now=10_000.0))
        self.assertTrue(any("WARMUP COMPLETE" in m for m in cm.output))
        self.assertFalse(g.log_progress(st, "1h", now=10_001.0))

    def test_defaults_are_on_when_config_says_nothing(self):
        with open(CONFIG_YAML) as f:
            raw = yaml.safe_load(f)
        raw["strategy"]["close_snipe"].pop("warmup", None)
        cfg = Config(raw=raw, path=CONFIG_YAML)
        self.assertTrue(cfg.warmup_cfg["enabled"])
        self.assertGreaterEqual(int(cfg.warmup_cfg["min_oracle_samples"]), 60)


class TestOracleSampleCount(unittest.TestCase):
    def _oracle(self):
        with open(CONFIG_YAML) as f:
            raw = yaml.safe_load(f)
        return BinanceOracle(Config(raw=raw, path=CONFIG_YAML))

    def test_counts_only_points_inside_the_window(self):
        o = self._oracle()
        now = time.time()
        for i in range(200):
            o._series.append(PricePoint(ts=now - i, price=100.0))
        # 1 point per second, so ~121 land inside a 120s window (the exact
        # boundary point depends on sub-ms clock drift between the two calls)
        self.assertIn(o.n_samples(120.0), (120, 121))
        self.assertIn(o.n_samples(10.0), (10, 11))
        self.assertLess(o.n_samples(10.0), o.n_samples(120.0))

    def test_dead_feed_reports_zero_not_a_full_buffer(self):
        """A poller that stopped 10 minutes ago still HOLDS 120 points. Anchored
        on wall clock (not on the newest point) that must read as 0."""
        o = self._oracle()
        now = time.time()
        for i in range(120):
            o._series.append(PricePoint(ts=now - 600 - i, price=100.0))
        self.assertEqual(o.n_samples(120.0), 0)

    def test_empty_buffer_is_zero(self):
        self.assertEqual(self._oracle().n_samples(120.0), 0)


class TestWarmupBlocksTrading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def _eng(self, n_samples=120, uptime=10_000.0):
        eng = _engine(self._tmp.name)
        eng.binance = StubBinance(self.now, n_samples=n_samples)
        eng.clob = StubClob()
        eng.warmup.started_at = self.now - uptime
        return eng

    def test_cold_buffer_blocks_the_fill(self):
        eng = self._eng(n_samples=10)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        self.assertEqual(eng.ledger.snipe_signals, [])

    def test_cold_buffer_blocks_before_any_book_fetch(self):
        eng = self._eng(n_samples=10)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.clob.calls, [])

    def test_short_uptime_blocks_the_fill(self):
        eng = self._eng(n_samples=500, uptime=5.0)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])

    def test_warm_engine_fires(self):
        eng = self._eng(n_samples=120, uptime=10_000.0)
        eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)

    def test_boundary_is_inclusive_at_the_configured_minimum(self):
        need = int(_engine(self._tmp.name).config.warmup_cfg["min_oracle_samples"])
        eng = self._eng(n_samples=need - 1)
        eng._maybe_snipe(FakeMarket(slug="a", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        eng2 = self._eng(n_samples=need)
        eng2._maybe_snipe(FakeMarket(slug="b", close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(eng2.executor.submissions), 1)


# ===========================================================================
# GUARD 3 — daily loss limit / circuit breaker
# ===========================================================================

class _BreakerLedger:
    def __init__(self, pnl=0.0, streak=0, n_resolved=0, seq=None):
        self.pnl = pnl
        self.streak = streak
        self.streak_since = {}
        self.n = n_resolved
        self.seq = seq or []

    def pnl_today(self):
        return {"net_pnl": self.pnl}

    def consecutive_losses(self, since_ts=None, limit=200):
        if since_ts is not None and since_ts in self.streak_since:
            return self.streak_since[since_ts]
        return self.streak

    def n_resolved_trades(self):
        return self.n

    def recent_trade_pnls(self, limit=50, since_ts=None):
        return self.seq


class TestDailyLimitResolution(unittest.TestCase):
    def test_percent_of_bankroll(self):
        self.assertAlmostEqual(
            resolve_daily_limit({"bankroll_usd": 1250, "max_daily_loss_pct": 8.0}), 100.0)

    def test_tighter_of_the_two_wins(self):
        cfg = {"bankroll_usd": 1250, "max_daily_loss_pct": 8.0, "max_daily_loss_usd": 40}
        self.assertAlmostEqual(resolve_daily_limit(cfg), 40.0)
        cfg["max_daily_loss_usd"] = 400
        self.assertAlmostEqual(resolve_daily_limit(cfg), 100.0)

    def test_none_when_unconfigured(self):
        self.assertIsNone(resolve_daily_limit({}))

    def test_sign_is_ignored(self):
        self.assertAlmostEqual(resolve_daily_limit({"max_daily_loss_usd": -75}), 75.0)


class TestCircuitBreaker(unittest.TestCase):
    CFG = {"daily_loss_limit": {"enabled": True, "bankroll_usd": 1250,
                                 "max_daily_loss_pct": 8.0},
           "consecutive_loss_brake": {"enabled": True, "max_consecutive_losses": 4},
           "log_every_secs": 0}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ov = Path(self._tmp.name) / "override.json"

    def _br(self, **kw):
        return CircuitBreaker(self.CFG, _BreakerLedger(**kw), override_path=self.ov)

    def test_not_tripped_on_a_normal_day(self):
        self.assertFalse(self._br(pnl=-41.47).evaluate().tripped)

    def test_trips_at_the_daily_limit(self):
        st = self._br(pnl=-100.0).evaluate()
        self.assertTrue(st.tripped)
        self.assertTrue(any("daily_loss_limit" in r for r in st.reasons))

    def test_profit_never_trips(self):
        self.assertFalse(self._br(pnl=5000.0).evaluate().tripped)

    def test_consecutive_loss_brake(self):
        self.assertFalse(self._br(streak=3).evaluate().tripped)
        st = self._br(streak=4).evaluate()
        self.assertTrue(st.tripped)
        self.assertTrue(any("consecutive_loss_brake" in r for r in st.reasons))

    def test_each_leg_can_be_disabled_independently(self):
        cfg = json.loads(json.dumps(self.CFG))
        cfg["daily_loss_limit"]["enabled"] = False
        b = CircuitBreaker(cfg, _BreakerLedger(pnl=-5000.0, streak=1), override_path=self.ov)
        self.assertFalse(b.evaluate().tripped)
        cfg2 = json.loads(json.dumps(self.CFG))
        cfg2["consecutive_loss_brake"]["enabled"] = False
        b2 = CircuitBreaker(cfg2, _BreakerLedger(pnl=0.0, streak=99), override_path=self.ov)
        self.assertFalse(b2.evaluate().tripped)

    # --- manual override ---------------------------------------------------
    def test_override_releases_the_breaker(self):
        b = self._br(pnl=-100.0)
        self.assertTrue(b.evaluate().tripped)
        b.write_override()
        st = b.evaluate()
        self.assertFalse(st.tripped)
        self.assertTrue(st.override_active)

    def test_override_is_scoped_to_one_utc_day(self):
        b = self._br(pnl=-100.0)
        b.write_override()
        rec = json.loads(self.ov.read_text())
        rec["utc_day"] = "1999-01-01"
        self.ov.write_text(json.dumps(rec))
        self.assertTrue(b.evaluate().tripped)      # yesterday's override is dead

    def test_override_rearms_after_another_full_limit(self):
        b = self._br(pnl=-100.0)
        b.write_override()
        self.assertFalse(b.evaluate().tripped)
        b.ledger.pnl = -199.0
        self.assertFalse(b.evaluate().tripped)
        b.ledger.pnl = -200.0
        self.assertTrue(b.evaluate().tripped)

    def test_override_rearms_on_a_new_losing_streak(self):
        led = _BreakerLedger(pnl=0.0, streak=4, n_resolved=10, seq=[(5000.0, "m", -1.0)])
        b = CircuitBreaker(self.CFG, led, override_path=self.ov)
        self.assertTrue(b.evaluate().tripped)
        b.write_override()
        led.streak_since[5000.0] = 0
        self.assertFalse(b.evaluate().tripped)
        led.streak_since[5000.0] = 4          # four NEW losses since the override
        self.assertTrue(b.evaluate().tripped)

    def test_corrupt_override_does_not_resume_trading(self):
        b = self._br(pnl=-100.0)
        self.ov.write_text("{not json")
        st = b.evaluate()
        self.assertTrue(st.tripped)
        # and it must not be reported as an ACTIVE override either: an
        # unreadable file is "no override", never "override granted"
        self.assertFalse(st.override_active)
        self.assertIsNone(b.read_override())

    def test_clear_override_rearms_immediately(self):
        b = self._br(pnl=-100.0)
        b.write_override()
        self.assertFalse(b.evaluate().tripped)
        self.assertTrue(b.clear_override())
        self.assertTrue(b.evaluate().tripped)


class TestUtcReset(unittest.TestCase):
    def test_utc_midnight_is_a_day_boundary(self):
        t = 1_784_251_000.0
        self.assertEqual(utc_midnight(t) % 86400, 0.0)
        self.assertLessEqual(utc_midnight(t), t)
        self.assertGreater(utc_midnight(t) + 86400, t)

    def test_day_string_changes_at_midnight(self):
        m = utc_midnight(time.time())
        self.assertNotEqual(utc_day_str(m - 1), utc_day_str(m + 1))


class TestLedgerBreakerInputs(unittest.TestCase):
    """The breaker's inputs come from the REAL ledger, so test them there."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        with open(CONFIG_YAML) as f:
            raw = yaml.safe_load(f)
        raw["storage"] = {"sqlite_path": str(Path(self._tmp.name) / "t.db"),
                          "fills_csv": str(Path(self._tmp.name) / "f.csv"),
                          "pnl_csv": str(Path(self._tmp.name) / "p.csv")}
        self.ledger = Ledger(Config(raw=raw, path=CONFIG_YAML))
        self.addCleanup(self.ledger.close)

    def _resolve(self, slug, pnl, ts):
        rows = [{"resolved_ts": ts, "market_slug": slug, "family": "1h",
                 "strategy": "close_snipe", "side": "up", "shares": 10.0,
                 "avg_price": 0.5, "payout": 1.0 if pnl > 0 else 0.0,
                 "fees_usd": 0.0, "realized_pnl": pnl, "oracle_winner": "up",
                 "gamma_winner": "up", "resolution_source": "gamma",
                 "disagreement": False}]
        self.ledger._conn.execute(
            "INSERT OR REPLACE INTO resolutions (market_slug, family, close_ts, "
            "oracle_winner, oracle_reason, gamma_winner, gamma_closed, resolved_winner, "
            "resolution_source, disagreement, resolved_ts, pnl_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, "1h", ts, "up", "r", "up", 1, "up", "gamma", 0, ts, json.dumps(rows)))
        self.ledger._conn.commit()

    def test_consecutive_losses_counts_trailing_losses_only(self):
        base = utc_midnight() + 3600
        self._resolve("m1", -10.0, base + 1)
        self._resolve("m2", +10.0, base + 2)
        self._resolve("m3", -10.0, base + 3)
        self._resolve("m4", -10.0, base + 4)
        self.assertEqual(self.ledger.consecutive_losses(), 2)

    def test_a_win_resets_the_streak(self):
        base = utc_midnight() + 3600
        self._resolve("m1", -10.0, base + 1)
        self._resolve("m2", -10.0, base + 2)
        self.assertEqual(self.ledger.consecutive_losses(), 2)
        self._resolve("m3", +1.0, base + 3)
        self.assertEqual(self.ledger.consecutive_losses(), 0)

    def test_one_row_per_resolved_market_not_per_fill(self):
        base = utc_midnight() + 3600
        self._resolve("m1", -10.0, base + 1)
        self.assertEqual(len(self.ledger.recent_trade_pnls()), 1)

    def test_streak_and_pnl_scope_to_the_current_utc_day(self):
        """Auto-reset at UTC midnight: yesterday's disaster must not gate today."""
        yday = utc_midnight() - 3600
        for i in range(6):
            self._resolve(f"y{i}", -50.0, yday - i)
        self.assertEqual(self.ledger.consecutive_losses(since_ts=utc_midnight()), 0)
        self.assertAlmostEqual(self.ledger.pnl_today()["net_pnl"], 0.0)
        self.assertEqual(self.ledger.consecutive_losses(since_ts=0), 6)

    def test_pnl_today_reflects_todays_resolutions(self):
        self._resolve("t1", -30.0, utc_midnight() + 10)
        self.assertAlmostEqual(self.ledger.pnl_today()["net_pnl"], -30.0)

    # --- end-to-end auto-reset, real ledger + real breaker -----------------
    def _real_breaker(self):
        cfg = {"daily_loss_limit": {"enabled": True, "bankroll_usd": 1250,
                                     "max_daily_loss_pct": 8.0},
               "consecutive_loss_brake": {"enabled": True, "max_consecutive_losses": 4},
               "log_every_secs": 0}
        return CircuitBreaker(cfg, self.ledger,
                              override_path=Path(self._tmp.name) / "ov.json")

    def test_breaker_auto_resets_at_utc_midnight(self):
        """Yesterday's disaster must not gate today: six losses and -$300
        BEFORE UTC midnight leave the breaker clear once the day rolls."""
        yday = utc_midnight() - 60
        for i in range(6):
            self._resolve(f"y{i}", -50.0, yday - i)
        st = self._real_breaker().evaluate()
        self.assertFalse(st.tripped, st.reasons)
        self.assertEqual(st.consecutive_losses, 0)
        self.assertAlmostEqual(st.daily_pnl, 0.0)

    def test_breaker_trips_on_todays_losses_with_the_real_ledger(self):
        base = utc_midnight() + 60
        for i in range(4):
            self._resolve(f"t{i}", -5.0, base + i)
        st = self._real_breaker().evaluate()
        self.assertTrue(st.tripped)
        self.assertEqual(st.consecutive_losses, 4)


class TestBreakerBlocksTrading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0
        self.eng = _engine(self._tmp.name)
        self.eng.binance, self.eng.clob = StubBinance(self.now), StubClob()
        warm(self.eng, self.now)

    def test_snipe_blocked_when_daily_limit_breached(self):
        self.eng.ledger.today_pnl = -100.0
        self.eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(self.eng.executor.submissions, [])
        self.assertEqual(self.eng.ledger.snipe_signals, [])

    def test_snipe_blocked_before_any_book_fetch(self):
        self.eng.ledger.today_pnl = -100.0
        self.eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(self.eng.clob.calls, [])

    def test_snipe_blocked_on_consecutive_losses(self):
        self.eng.ledger.streak = 4
        self.eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(self.eng.executor.submissions, [])

    def test_snipe_allowed_just_inside_the_limit(self):
        self.eng.ledger.today_pnl = -99.99
        self.eng.ledger.streak = 3
        self.eng._maybe_snipe(FakeMarket(slug="m", close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(self.eng.executor.submissions), 1)

    def test_settle_sweep_is_blocked_too(self):
        """A daily stop one strategy can walk around is not a daily stop."""
        eng = _engine(self._tmp.name)
        eng.binance, eng.clob = StubBinance(self.now), StubClob()
        warm(eng, self.now)
        eng.config.raw["families"]["1h"]["settle_sweep"] = True
        eng.settle_winner_cache["s1"] = WinnerDetermination("up", 1.0, 2.0, "t")
        eng.ledger.today_pnl = -100.0
        eng._maybe_settle(FakeMarket(slug="s1", close_ts=self.now - 5.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        # ... and fires once the breaker is clear
        eng.ledger.today_pnl = 0.0
        eng._maybe_settle(FakeMarket(slug="s1", close_ts=self.now - 5.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)

    def test_manual_override_lets_trading_resume(self):
        self.eng.ledger.today_pnl = -100.0
        self.eng._maybe_snipe(FakeMarket(slug="m1", close_ts=self.now + 3.0), self.now)
        self.assertEqual(self.eng.executor.submissions, [])
        self.eng.breaker.write_override()
        self.eng._maybe_snipe(FakeMarket(slug="m2", close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(self.eng.executor.submissions), 1)


class TestGuardStatusSurface(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def test_status_json_reports_blocked_state(self):
        eng = _engine(self._tmp.name)
        eng.binance = StubBinance(self.now, n_samples=0)
        eng.clob = StubClob()
        eng.status = StatusState(eng.config, eng.ledger)
        eng.ledger.today_pnl = -250.0
        eng._publish_guard_status()
        snap = eng.status.snapshot()
        self.assertIn("guards", snap)
        self.assertTrue(snap["guards"]["trading_blocked"])
        self.assertFalse(snap["guards"]["warmup"]["ready"])
        self.assertTrue(snap["guards"]["risk"]["tripped"])
        self.assertIn("depth_reference", snap["guards"])

    def test_status_json_reports_healthy_state(self):
        eng = _engine(self._tmp.name)
        eng.binance = StubBinance(self.now, n_samples=500)
        eng.clob = StubClob()
        eng.status = StatusState(eng.config, eng.ledger)
        eng.warmup.started_at = time.time() - 10_000
        eng._publish_guard_status()
        snap = eng.status.snapshot()
        self.assertFalse(snap["guards"]["trading_blocked"])
        self.assertTrue(snap["guards"]["warmup"]["ready"])

    def test_status_cli_prints_the_guard_section(self):
        import io
        import contextlib

        from polybot import main as cli

        eng = _engine(self._tmp.name)
        eng.binance = StubBinance(self.now, n_samples=0)
        eng.clob = StubClob()
        eng.status = StatusState(eng.config, eng.ledger)
        eng.ledger.today_pnl = -250.0
        eng._publish_guard_status()
        path = eng.config.status_json_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(eng.status.snapshot(), f, default=str)

        # The CLI resolves paths from the config FILE, so write the patched
        # config to disk rather than pointing it at the shipped one (which in
        # a deployed checkout is a *running* bot's status.json).
        cfg_path = Path(self._tmp.name) / "config.yaml"
        with open(cfg_path, "w") as f:
            yaml.safe_dump(eng.config.raw, f)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--config", str(cfg_path), "status"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Risk guards", out)
        self.assertIn("TRADING BLOCKED", out)
        self.assertIn("breaker:", out)
        self.assertIn("warmup:", out)
        self.assertIn("adverse-size filter", out)
        self.assertIn("python -m polybot.main resume", out)


class TestResumeCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        with open(CONFIG_YAML) as f:
            raw = yaml.safe_load(f)
        raw["storage"] = {"sqlite_path": str(Path(self._tmp.name) / "t.db"),
                          "fills_csv": str(Path(self._tmp.name) / "f.csv"),
                          "pnl_csv": str(Path(self._tmp.name) / "p.csv")}
        raw["logging"] = dict(raw["logging"], file=str(Path(self._tmp.name) / "t.log"))
        raw.setdefault("risk", {})["override_path"] = str(Path(self._tmp.name) / "ov.json")
        self.cfg_path = Path(self._tmp.name) / "config.yaml"
        with open(self.cfg_path, "w") as f:
            yaml.safe_dump(raw, f)
        self.ov = Path(self._tmp.name) / "ov.json"

    def test_resume_force_writes_a_day_scoped_override(self):
        import io
        import contextlib

        from polybot import main as cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--config", str(self.cfg_path), "resume", "--force"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.ov.exists())
        rec = json.loads(self.ov.read_text())
        self.assertEqual(rec["utc_day"], utc_day_str())

    def test_halt_removes_the_override(self):
        import io
        import contextlib

        from polybot import main as cli

        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--config", str(self.cfg_path), "resume", "--force"])
            self.assertTrue(self.ov.exists())
            cli.main(["--config", str(self.cfg_path), "halt"])
        self.assertFalse(self.ov.exists())

    def test_resume_declines_when_nothing_is_tripped(self):
        import io
        import contextlib

        from polybot import main as cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--config", str(self.cfg_path), "resume"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.ov.exists())
        self.assertIn("NOT tripped", buf.getvalue())


# ===========================================================================
# The pre-existing guards must be exactly as strong as before
# ===========================================================================

class TestNoExistingGuardWeakened(unittest.TestCase):
    def setUp(self):
        with open(CONFIG_YAML) as f:
            self.raw = yaml.safe_load(f)
        self.cfg = Config(raw=self.raw, path=CONFIG_YAML)

    def test_paper_is_still_the_default(self):
        self.assertTrue(self.raw["mode"]["paper"])
        self.assertTrue(self.cfg.paper)

    def test_family_allowlist_unchanged(self):
        self.assertEqual(self.cfg.snipe_cfg["allowed_families"], ["1h"])

    def test_fair_cap_and_sigma_floor_unchanged(self):
        self.assertEqual(float(self.cfg.snipe_cfg["fair_cap"]), 0.98)
        self.assertEqual(float(self.cfg.snipe_cfg["sigma_1s_floor"]), 8.0e-06)

    def test_walk_bound_unchanged(self):
        self.assertEqual(float(self.cfg.execution_cfg["max_walk_above_best"]), 0.03)

    def test_tau_band_and_cap_unchanged(self):
        self.assertEqual(float(self.cfg.snipe_cfg["snipe_last_secs"]), 5)
        self.assertEqual(float(self.cfg.snipe_cfg["snipe_min_tau_secs"]), 2.5)
        self.assertEqual(float(self.cfg.snipe_cfg["edge_min"]), 0.03)
        self.assertEqual(float(self.cfg.sizing_cfg["per_event_cap_usd"]), 250)
        self.assertEqual(float(self.cfg.sizing_cfg["max_open_notional"]), 1000)

    def test_walk_bound_still_enforced_with_the_filter_on(self):
        asks = [BookLevel(0.50, 10.0), BookLevel(0.70, 10.0)]
        r = walk_asks(asks, EDGE_FN, edge_min=0.01, price_min=0.3, price_max=0.99,
                      cap_usd=250.0, fee_rate=FEE, max_above_best=0.03,
                      max_level_shares=1000.0)
        self.assertEqual([f.price for f in r.fills], [0.50])

    def test_chainlink_families_still_off(self):
        for fam in ("5m", "15m", "4h"):
            self.assertFalse(self.raw["families"][fam]["close_snipe"])
            self.assertFalse(self.raw["families"][fam]["settle_sweep"])


if __name__ == "__main__":
    unittest.main()
