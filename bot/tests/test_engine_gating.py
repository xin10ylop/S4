"""Engine-level gating tests for the close_snipe timing band and sizing.

These exercise `Engine._maybe_snipe` / `Engine._tick` as *wired* (config ->
snipe_tau_bounds -> gate -> executor.submit), with every network-touching
collaborator replaced by a stub. Nothing here opens a socket.

Guards audit/A4_change_spec.md changes (i) window, (iii) cap, (iv) settle off.
"""
import inspect
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from polybot.config import Config
from polybot.engine import Engine
from polybot.oracle import PricePoint
from polybot.polymarket import BookLevel, OrderBook

CONFIG_YAML = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class FakeMarket:
    slug: str = "bitcoin-up-or-down-july-15-2026-3pm-et"
    family: str = "1h"
    # M5: every Market carries its coin, and the engine routes the oracle and
    # the fill permission off it. Default bitcoin so these pre-M5 tests keep
    # testing what they were written to test.
    coin: str = "bitcoin"
    up_token_id: str = "UP"
    down_token_id: str = "DOWN"
    close_ts: float = 1_000_000.0
    accepting_orders: bool = True
    closed: bool = False

    @property
    def window_start_ts(self) -> int:
        return int(self.close_ts - 3600)


class StubBinance:
    """Deterministic oracle: price above the window open with modest vol, so
    fair_up is confidently (but not absurdly) UP and a 0.50 ask is a clear
    mispricing.

    The default used to be s_t=101_000 against s_open=100_000 — a 1% BTC move
    with 3 seconds left, which at sigma=1e-4 is |z| ~ 57. That is a ~57-sigma
    event, and it is precisely the shape `max_abs_z` now vetoes (see
    docs/10_realmoney_audit.md §4: |z|>5 fills won 57% against a 77%
    break-even). Every gating test written against it was therefore asserting
    on a signal the shipped bot must refuse to take. The default is now +$20
    on $100k => |z| ~ 1.2 at tau=3, inside the band that actually carries the
    edge. Tests that specifically want the vetoed regime pass s_t explicitly.
    """

    def __init__(self, now: float, s_t: float = 100_020.0, s_open: float = 100_000.0,
                 staleness: float = 0.0, sigma: float = 1e-4, n_samples: int = 120):
        self._pt = PricePoint(ts=now - staleness, price=s_t)
        self._s_open = s_open
        self._sigma = sigma
        self._n = n_samples

    def hour_open_close(self, window_start_ts):
        return self._s_open, None

    def latest(self):
        return self._pt

    def rolling_log_return_std(self, window_secs):
        return self._sigma

    def n_samples(self, window_secs=120.0):
        # M4 guard 2 reads this; the default here is a fully warm buffer so the
        # pre-existing gating tests keep testing what they were written to test.
        return self._n


class StubClob:
    def __init__(self, ask_up: Optional[float] = 0.50, ask_down: Optional[float] = 0.50):
        self._asks = {"UP": ask_up, "DOWN": ask_down}
        self.calls: List[str] = []

    def get_book(self, token_id):
        self.calls.append(token_id)
        px = self._asks.get(token_id)
        if px is None:
            return None
        return OrderBook(token_id=token_id, bids=[],
                         asks=[BookLevel(px, 5000.0)], fetched_at=0.0)


class StubLedger:
    def __init__(self):
        self.snipe_signals: List[object] = []
        self.snipe_signal_meta: List[dict] = []
        self.settle_signals: List[tuple] = []
        self.open_notional = 0.0
        self.today_pnl = 0.0
        self.streak = 0

    def record_snipe_signal(self, sig, extra_meta=None):
        # M5: the engine attaches {coin, shadow, book_age_s}; capture it so
        # tests can assert on the shadow tape's contents, not just its size.
        self.snipe_signals.append(sig)
        self.snipe_signal_meta.append(dict(extra_meta or {}))
        return len(self.snipe_signals)

    def record_settle_signal(self, *a, **kw):
        self.settle_signals.append((a, kw))
        return len(self.settle_signals)

    def total_open_notional(self):
        return self.open_notional

    def strategy_cost_for_market(self, *_):
        return 0.0

    def unresolved_markets(self):
        return []

    def all_open_positions(self):
        return []

    # --- M4 guard 3 (circuit breaker) inputs -------------------------------
    def __init_risk__(self):
        pass

    def pnl_today(self):
        return {"n": 0, "gross_pnl": 0.0, "fees_paid": 0.0, "net_pnl": self.today_pnl}

    def consecutive_losses(self, since_ts=None, limit=200):
        return self.streak

    def n_resolved_trades(self):
        return 0

    def recent_trade_pnls(self, limit=50, since_ts=None):
        return []

    def metrics(self):
        return {"n_resolved_positions": 0, "n_fill_attempts": 0, "attempts_by_outcome": {},
                "n_filled": 0, "win_rate": None, "gross_pnl": 0.0, "fees_paid": 0.0,
                "net_pnl": 0.0, "resolution_disagreements": 0, "per_strategy_family": {}}


@dataclass
class StubExecutor:
    """Captures submit() instead of running the fill worker."""
    submissions: List[tuple] = field(default_factory=list)

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))
        return None

    def shutdown(self, wait=True):
        pass


def _make_engine(tmpdir: str, **raw_overrides) -> Engine:
    """Engine against the SHIPPED config.yaml, with storage/logging redirected
    to a temp dir so tests never touch bot/data/."""
    with open(CONFIG_YAML) as f:
        raw = yaml.safe_load(f)
    raw["storage"] = {
        "sqlite_path": str(Path(tmpdir) / "t.db"),
        "fills_csv": str(Path(tmpdir) / "fills.csv"),
        "pnl_csv": str(Path(tmpdir) / "pnl.csv"),
    }
    raw["logging"] = dict(raw["logging"], file=str(Path(tmpdir) / "t.log"))
    for k, v in raw_overrides.items():
        raw[k] = v
    eng = Engine(Config(raw=raw, path=CONFIG_YAML))
    eng.ledger = StubLedger()
    eng.breaker.ledger = eng.ledger      # breaker reads the stub, not the temp sqlite
    eng.executor = StubExecutor()
    return eng


def warm(engine, now: float) -> None:
    """Satisfy M4 guard 2 (warmup) so a test can exercise something else.

    Tests run on a synthetic clock (`now = 1_000_000.0`), so the real
    `started_at` set in Engine.__init__ would read as negative uptime. Every
    test that expects a fill must call this — which is the point: the guard is
    on by default and cannot be bypassed by accident.
    """
    engine.warmup.started_at = now - 10_000.0


class _EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0
        self.engine = _make_engine(self._tmp.name)
        self.engine.binance = StubBinance(self.now)
        self.engine.clob = StubClob()
        warm(self.engine, self.now)

    def _market_at_tau(self, tau: float, slug: str = "m1") -> FakeMarket:
        return FakeMarket(slug=slug, close_ts=self.now + tau)

    @property
    def submissions(self):
        return self.engine.executor.submissions


class TestSnipeWindowGate(_EngineTestBase):
    def test_no_fire_above_window(self):
        # tau_hi = snipe_last_secs = 5
        self.engine._maybe_snipe(self._market_at_tau(5.6), self.now)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.engine.ledger.snipe_signals, [])
        self.assertEqual(self.engine.clob.calls, [])  # gated before any book fetch

    def test_no_fire_below_window(self):
        # THE bug this change exists to fix: tau=1.9 fills at/after the close.
        self.engine._maybe_snipe(self._market_at_tau(1.9), self.now)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.engine.ledger.snipe_signals, [])
        self.assertEqual(self.engine.clob.calls, [])

    def test_no_fire_at_legacy_tau_values(self):
        # The old gate was (0, 6]; every tau it admitted that the new one does
        # not must now be silent.
        for tau in (0.02, 0.5, 1.0, 1.5, 2.0, 5.5, 6.0):
            self.engine.snipe_done.clear()
            self.engine._maybe_snipe(self._market_at_tau(tau), self.now)
        self.assertEqual(self.submissions, [])

    def test_fires_inside_window(self):
        m = self._market_at_tau(3.0)
        self.engine._maybe_snipe(m, self.now)
        self.assertEqual(len(self.submissions), 1)
        self.assertIn(m.slug, self.engine.snipe_done)
        self.assertEqual(len(self.engine.ledger.snipe_signals), 1)

    def test_fires_at_band_edges(self):
        # tau_lo is 2.5 (snipe_min_tau_secs raised from 2.0 in docs/07 item 3 so
        # the bound also covers the two blocking /book fetches).
        for i, tau in enumerate((2.5, 5.0)):
            m = self._market_at_tau(tau, slug=f"edge{i}")
            self.engine._maybe_snipe(m, self.now)
        self.assertEqual(len(self.submissions), 2)

    def test_one_entry_per_window(self):
        m4 = self._market_at_tau(4.0, slug="same")
        m3 = self._market_at_tau(3.0, slug="same")
        # same slug, two ticks inside the band
        self.engine._maybe_snipe(m4, self.now)
        self.engine._maybe_snipe(m3, self.now + 1.0)
        self.assertEqual(len(self.submissions), 1)

    def test_stale_oracle_skips(self):
        self.engine.binance = StubBinance(self.now, staleness=6.0)
        self.engine._maybe_snipe(self._market_at_tau(3.0), self.now)
        self.assertEqual(self.submissions, [])

    def test_non_1h_family_still_blocked(self):
        m = FakeMarket(slug="btc-updown-5m-1", family="5m", close_ts=self.now + 3.0)
        self.engine._maybe_snipe(m, self.now)
        self.assertEqual(self.submissions, [])

    def test_window_tracks_latency_ms(self):
        """Raising latency_ms lifts the floor, so a tau that used to fire stops."""
        eng = _make_engine(self._tmp.name)
        eng.config.raw["execution"]["latency_ms"] = 3000  # floor -> 3.5s
        eng.binance = StubBinance(self.now)
        eng.clob = StubClob()
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="a", close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        eng._maybe_snipe(FakeMarket(slug="b", close_ts=self.now + 4.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)


class TestSizingAndSettleWiring(_EngineTestBase):
    def _bound_fill_args(self):
        fn, args, kwargs = self.submissions[0]
        return inspect.signature(self.engine._run_fill).bind(*args, **kwargs).arguments

    def test_cap_passed_to_fill_is_config_value(self):
        self.engine._maybe_snipe(self._market_at_tau(3.0), self.now)
        bound = self._bound_fill_args()
        self.assertEqual(bound["cap_usd"],
                         float(self.engine.config.sizing_cfg["per_event_cap_usd"]))
        self.assertEqual(bound["cap_usd"], 250.0)  # (iii) shipped value
        self.assertEqual(bound["edge_min"],
                         float(self.engine.config.snipe_cfg["edge_min"]))
        self.assertEqual(bound["strategy"], "close_snipe")

    def test_global_cap_still_blocks_fills(self):
        self.engine.ledger.open_notional = 10_000.0
        self.engine._maybe_snipe(self._market_at_tau(3.0), self.now)
        # the signal is still recorded, but no fill is dispatched
        self.assertEqual(len(self.engine.ledger.snipe_signals), 1)
        self.assertEqual(self.submissions, [])

    def test_settle_sweep_off_for_1h(self):
        """(iv) at the wiring level: a post-close 1h market never reaches
        _maybe_settle when _tick runs against the shipped config."""
        called = []
        self.engine._maybe_settle = lambda m, n: called.append(m.slug)
        post_close = FakeMarket(slug="closed1h", close_ts=self.now - 10.0)
        self.engine.markets = {post_close.slug: post_close}
        self.engine._tick()
        self.assertEqual(called, [])
        self.assertFalse(self.engine.config.families()["1h"].settle_sweep)

    def test_settle_sweep_has_its_own_cap(self):
        """If settle_sweep is ever re-enabled it must NOT inherit the $250
        close_snipe clip (engine reads strategy.settle_sweep.cap_usd)."""
        eng = _make_engine(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = StubClob()
        warm(eng, self.now)
        eng.config.raw["families"]["1h"]["settle_sweep"] = True
        eng.settle_winner_cache["s1"] = type("W", (), {"winner": "up", "reason": "t",
                                                       "s_open": 1.0, "s_close": 2.0})()
        eng._maybe_settle(FakeMarket(slug="s1", close_ts=self.now - 5.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)
        bound = inspect.signature(eng._run_fill).bind(
            *eng.executor.submissions[0][1], **eng.executor.submissions[0][2]).arguments
        self.assertEqual(bound["cap_usd"], 25.0)
        self.assertLess(bound["cap_usd"], float(eng.config.sizing_cfg["per_event_cap_usd"]))


class TestOrphanedPositionRecovery(_EngineTestBase):
    """docs/10_realmoney_audit.md §7. `known_markets` is populated ONLY by
    discovery, and discovery asks gamma with `closed` omitted — which gamma
    treats as closed=false. So a position filled just before a restart, whose
    window closed while the bot was down, is never rediscovered. Before the fix
    `_check_resolutions` skipped it forever: its cost stayed in
    total_open_notional() eating the global cap, and its realised P&L never
    reached the daily breaker or the consecutive-loss brake.
    """

    RAW = {
        "slug": "bitcoin-up-or-down-july-15-2026-3pm-et",
        "question": "Bitcoin Up or Down?",
        "conditionId": "0xcond",
        "clobTokenIds": '["UP", "DOWN"]',
        "endDate": "2026-07-15T19:00:00Z",
        "startDate": "2026-07-13T19:00:00Z",
        "closed": True, "active": False, "acceptingOrders": False,
        "outcomePrices": '["1", "0"]',
    }

    def setUp(self):
        super().setUp()
        self.slug = self.RAW["slug"]
        self.engine.ledger.unresolved = [self.slug]
        self.engine.ledger.unresolved_markets = lambda: self.engine.ledger.unresolved
        self.engine.known_markets.pop(self.slug, None)
        self.gamma_calls = []

        def fake_get(slug, closed=None):
            self.gamma_calls.append((slug, closed))
            return dict(self.RAW) if closed is True else None

        self.engine.gamma.get_market_by_slug = fake_get
        self.resolved = []
        self.engine._attempt_resolution = lambda m, now, t: self.resolved.append(m.slug)

    def test_orphan_is_recovered_and_resolved(self):
        now = 1_784_142_000.0 + 3600.0   # comfortably past the window close
        self.engine._check_resolutions(now)
        self.assertIn(self.slug, self.engine.known_markets,
                      "orphaned market must be rebuilt into known_markets")
        self.assertEqual(self.resolved, [self.slug],
                         "recovered orphan must actually be resolved, not just cached")
        self.assertIn((self.slug, True), self.gamma_calls,
                      "recovery MUST query gamma with closed=True — closed=None returns "
                      "an empty list for resolved markets, which is the whole bug")

    def test_recovery_happens_once_not_every_tick(self):
        now = 1_784_142_000.0 + 3600.0
        self.engine._check_resolutions(now)
        n_after_first = len(self.gamma_calls)
        self.engine._check_resolutions(now + 0.5)
        self.assertEqual(len(self.gamma_calls), n_after_first,
                         "a recovered market is cached; do not re-fetch it every tick")

    def test_unrecoverable_slug_is_rate_limited(self):
        """A slug gamma cannot return must not spin one request per tick."""
        self.engine.gamma.get_market_by_slug = lambda slug, closed=None: (
            self.gamma_calls.append((slug, closed)) or None)
        now = 1_784_142_000.0 + 3600.0
        for i in range(50):
            self.engine._check_resolutions(now + i * 0.1)   # 5 s of ticks
        poll = float(self.engine.config.resolution_cfg["gamma_poll_secs"])
        self.assertLessEqual(len(self.gamma_calls), 5.0 / poll + 2,
                             f"expected rate-limiting to ~1 call per {poll}s, "
                             f"got {len(self.gamma_calls)} in 5s")

    def test_gamma_failure_does_not_crash_the_resolution_loop(self):
        def boom(slug, closed=None):
            raise RuntimeError("gamma 503")
        self.engine.gamma.get_market_by_slug = boom
        self.engine._check_resolutions(1_784_142_000.0 + 3600.0)   # must not raise
        self.assertEqual(self.resolved, [])


if __name__ == "__main__":
    unittest.main()
