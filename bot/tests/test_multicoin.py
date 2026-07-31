"""Behavioural tests for the M5 multi-coin wiring.

DESIGN RULE (inherited from test_risk_guards.py): every test here must FAIL if
the thing it covers is deleted from the source. These are written against
observable behaviour — which oracle was actually called, whether a fill was
dispatched, how many HTTP calls were made — not against the existence of a
function or a config key.

The safety property that matters most in this file has exactly one shape:

    A MARKET IS NEVER PRICED OFF ANOTHER COIN'S UNDERLYING.

That is the failure M1 §6 item 3 and M3 §13 item 2 both call "silent and
catastrophic": no exception, no log line, just confident signals computed from
the wrong asset. `TestNeverPricesOffTheWrongCoin` is the load-bearing class.

See docs/09_multicoin_decision.md for why only bitcoin may fill.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from polybot.config import Config
from polybot.engine import Engine
from polybot.execution import ExecutionRouter
from polybot.fill_engine import book_is_stale, execute_taker_signal, fee_per_share
from polybot.oracle import (HOURLY_COIN_FEEDS, BinanceOracle, PricePoint, coin_feed)
from polybot.polymarket import (HOURLY_COINS, BookLevel, ClobClientREST, OrderBook,
                                 _HOURLY_RE, _hourly_slug, _parse_market)

from tests.test_engine_gating import (CONFIG_YAML, FakeMarket, StubBinance, StubClob,
                                       StubExecutor, StubLedger, warm)

ET = ZoneInfo("America/New_York")


def _raw(**overrides):
    with open(CONFIG_YAML) as f:
        raw = yaml.safe_load(f)
    for k, v in overrides.items():
        raw[k] = v
    return raw


def _engine(tmpdir, coins_allowed=None, coins_shadow=None, max_book_age=None,
            discover=None):
    """Engine on the shipped config with the coin lists optionally overridden."""
    raw = _raw()
    cs = raw["strategy"]["close_snipe"]
    if coins_allowed is not None:
        cs["allowed_coins"] = list(coins_allowed)
    if coins_shadow is not None:
        cs["shadow_coins"] = list(coins_shadow)
    if max_book_age is not _SENTINEL:
        cs["max_book_age_s"] = max_book_age
    if discover is not None:
        raw["discovery"]["hourly_coins"] = list(discover)
    raw["storage"] = {"sqlite_path": str(Path(tmpdir) / "t.db"),
                      "fills_csv": str(Path(tmpdir) / "f.csv"),
                      "pnl_csv": str(Path(tmpdir) / "p.csv")}
    raw["logging"] = dict(raw["logging"], file=str(Path(tmpdir) / "t.log"))
    eng = Engine(Config(raw=raw, path=CONFIG_YAML))
    eng.ledger = StubLedger()
    eng.breaker.ledger = eng.ledger
    eng.executor = StubExecutor()
    return eng


class _Sentinel:
    pass


_SENTINEL = _Sentinel()


def _engine2(tmpdir, **kw):
    kw.setdefault("max_book_age", _SENTINEL)
    return _engine(tmpdir, **kw)


# ===========================================================================
# 1. Slug / market model
# ===========================================================================

class TestHourlySlugModel(unittest.TestCase):
    def test_all_seven_coins_are_recognised_and_the_coin_is_captured(self):
        for coin in HOURLY_COINS:
            slug = f"{coin}-up-or-down-july-15-2026-3pm-et"
            m = _HOURLY_RE.match(slug)
            self.assertIsNotNone(m, f"{coin} hourly slug not recognised")
            self.assertEqual(m.group("coin"), coin)

    def test_the_seven_are_exactly_the_seven_that_exist(self):
        """M1 §1.1 probed 43 other candidates and found none. Recognising a coin
        that does not exist is harmless; FAILING to recognise one that does
        would silently drop a whole family, so pin the set."""
        self.assertEqual(set(HOURLY_COINS),
                         {"bitcoin", "ethereum", "solana", "xrp", "dogecoin",
                          "bnb", "hype"})

    def test_unknown_coins_are_not_matched(self):
        for coin in ("litecoin", "cardano", "avax", "eth", "btc", "shib"):
            self.assertIsNone(
                _HOURLY_RE.match(f"{coin}-up-or-down-july-15-2026-3pm-et"),
                f"{coin} must NOT match — it has no hourly family")

    def test_daily_markets_are_still_excluded(self):
        """The original reason the hourly regex demands an hour+am/pm suffix."""
        for coin in HOURLY_COINS:
            self.assertIsNone(_HOURLY_RE.match(f"{coin}-up-or-down-on-july-17-2026"))

    def test_slug_builder_round_trips_for_every_coin(self):
        when = dt.datetime(2026, 7, 15, 15, 0, tzinfo=ET)
        for coin in HOURLY_COINS:
            slug = _hourly_slug(coin, when)
            m = _HOURLY_RE.match(slug)
            self.assertIsNotNone(m, slug)
            self.assertEqual(m.group("coin"), coin)

    def test_slug_builder_hour_formatting_is_unchanged_for_bitcoin(self):
        """Regression: the shipped BTC slugs must be byte-identical to before."""
        cases = [
            (dt.datetime(2026, 7, 15, 15, 0, tzinfo=ET),
             "bitcoin-up-or-down-july-15-2026-3pm-et"),
            (dt.datetime(2027, 1, 1, 0, 0, tzinfo=ET),
             "bitcoin-up-or-down-january-1-2027-12am-et"),
            (dt.datetime(2026, 7, 15, 12, 0, tzinfo=ET),
             "bitcoin-up-or-down-july-15-2026-12pm-et"),
        ]
        for when, expected in cases:
            self.assertEqual(_hourly_slug("bitcoin", when), expected)

    def test_parse_market_sets_the_coin(self):
        base = {
            "clobTokenIds": '["U","D"]',
            "endDate": "2026-07-15T20:00:00Z",
            "startDate": "2026-07-13T20:00:00Z",
            "question": "q", "conditionId": "c", "acceptingOrders": True,
        }
        for coin in HOURLY_COINS:
            m = _parse_market(dict(base, slug=f"{coin}-up-or-down-july-15-2026-3pm-et"))
            self.assertIsNotNone(m)
            self.assertEqual(m.coin, coin)
            self.assertEqual(m.family, "1h")

    def test_short_families_are_pinned_to_bitcoin(self):
        m = _parse_market({
            "slug": "btc-updown-5m-1785000000", "clobTokenIds": '["U","D"]',
            "endDate": "2026-07-15T20:05:00Z", "startDate": "2026-07-15T20:00:00Z",
        })
        self.assertIsNotNone(m)
        self.assertEqual(m.coin, "bitcoin")


# ===========================================================================
# 2. The verified feed table
# ===========================================================================

class TestCoinFeedTable(unittest.TestCase):
    def test_every_hourly_coin_has_a_feed(self):
        for coin in HOURLY_COINS:
            self.assertIn(coin, HOURLY_COIN_FEEDS, f"{coin} has no verified feed")

    def test_unknown_coin_has_no_feed(self):
        """coin_feed returning None is what makes the engine fail closed."""
        for bogus in ("litecoin", "", "BITCOIN", None):
            self.assertIsNone(coin_feed(bogus))

    def test_symbols_match_the_resolution_sources_verified_in_M1(self):
        """These exact strings come from each family's own `resolutionSource`,
        re-fetched live by the verifier on 2026-07-28 and matched verbatim."""
        self.assertEqual(HOURLY_COIN_FEEDS["bitcoin"], ("BTCUSDT", "spot"))
        self.assertEqual(HOURLY_COIN_FEEDS["ethereum"], ("ETHUSDT", "spot"))
        self.assertEqual(HOURLY_COIN_FEEDS["solana"], ("SOLUSDT", "spot"))
        self.assertEqual(HOURLY_COIN_FEEDS["xrp"], ("XRPUSDT", "spot"))
        self.assertEqual(HOURLY_COIN_FEEDS["dogecoin"], ("DOGEUSDT", "spot"))
        self.assertEqual(HOURLY_COIN_FEEDS["bnb"], ("BNBUSDT", "spot"))

    def test_hype_is_futures_not_spot(self):
        """THE trap in this table. HYPE resolves on
        https://www.binance.com/en/futures/HYPEUSDT and there is NO HYPEUSDT
        spot pair (data-api.binance.vision returns HTTP 400 "Invalid symbol").
        Wiring it to spot is the wrong-feed error class that cost this project
        3-for-3 losing fills."""
        self.assertEqual(HOURLY_COIN_FEEDS["hype"], ("HYPEUSDT", "usdm_futures"))

    def test_no_two_coins_share_a_symbol(self):
        symbols = [s for s, _ in HOURLY_COIN_FEEDS.values()]
        self.assertEqual(len(symbols), len(set(symbols)))


class TestOracleVenueRouting(unittest.TestCase):
    def _cfg(self):
        return Config(raw=_raw(), path=CONFIG_YAML)

    def test_spot_oracle_uses_the_spot_base_and_path(self):
        o = BinanceOracle(self._cfg(), symbol="ETHUSDT", venue="spot")
        self.assertEqual(o.symbol, "ETHUSDT")
        self.assertEqual(o._base, self._cfg().binance_rest_base)
        self.assertEqual(o._prefix, "/api/v3")

    def test_futures_oracle_uses_the_futures_base_and_path(self):
        o = BinanceOracle(self._cfg(), symbol="HYPEUSDT", venue="usdm_futures")
        self.assertEqual(o._base, self._cfg().binance_futures_rest_base)
        self.assertNotEqual(o._base, self._cfg().binance_rest_base)
        self.assertEqual(o._prefix, "/fapi/v1")

    def test_unknown_venue_raises_rather_than_defaulting_to_spot(self):
        with self.assertRaises(ValueError):
            BinanceOracle(self._cfg(), symbol="XUSDT", venue="perp")

    def test_price_at_second_is_unavailable_on_futures(self):
        """Binance USD-M futures has no 1s kline interval. Answering with a 1m
        open would silently answer a different question."""
        o = BinanceOracle(self._cfg(), symbol="HYPEUSDT", venue="usdm_futures")
        self.assertIsNone(o.price_at_second(1785000000))

    def test_every_configured_feed_constructs(self):
        cfg = self._cfg()
        for coin, (symbol, venue) in HOURLY_COIN_FEEDS.items():
            with self.subTest(coin=coin):
                o = BinanceOracle(cfg, symbol=symbol, venue=venue)
                self.assertEqual(o.symbol, symbol)
                self.assertEqual(o.venue, venue)


# ===========================================================================
# 3. THE load-bearing safety property
# ===========================================================================

class _NamedOracle:
    """Records that it was asked, and for what."""

    def __init__(self, name, now, s_open=100.0, s_t=101.0, sigma=1e-4, n=500):
        self.name = name
        self.calls: List[str] = []
        self._pt = PricePoint(ts=now, price=s_t)
        self._s_open = s_open
        self._sigma = sigma
        self._n = n

    def hour_open_close(self, ts):
        self.calls.append("hour_open_close")
        return self._s_open, None

    def latest(self):
        self.calls.append("latest")
        return self._pt

    def rolling_log_return_std(self, w):
        self.calls.append("sigma")
        return self._sigma

    def n_samples(self, window_secs=120.0):
        return self._n

    def poll_once(self):
        return self._pt


class TestNeverPricesOffTheWrongCoin(unittest.TestCase):
    """The single most dangerous possible defect in this expansion."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def test_bitcoin_routes_to_the_binance_attribute(self):
        eng = _engine2(self._tmp.name)
        eng.binance = StubBinance(self.now)
        self.assertIs(eng._oracle_for(FakeMarket(coin="bitcoin")), eng.binance)

    def test_a_shadow_coin_gets_its_OWN_oracle_with_its_OWN_symbol(self):
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])
        o = eng._oracle_for(FakeMarket(slug="e", coin="ethereum"))
        self.assertIsNotNone(o)
        self.assertIsNot(o, eng.binance)
        self.assertEqual(o.symbol, "ETHUSDT")

    def test_an_unconfigured_coin_gets_NO_oracle(self):
        """solana is a real family with a real feed, but it is neither allowed
        nor shadowed, so no oracle is built for it and it cannot be priced."""
        eng = _engine2(self._tmp.name, coins_allowed=["bitcoin"], coins_shadow=[])
        self.assertIsNone(eng._oracle_for(FakeMarket(slug="s", coin="solana")))

    def test_an_unknown_coin_gets_NO_oracle(self):
        eng = _engine2(self._tmp.name)
        self.assertIsNone(eng._oracle_for(FakeMarket(slug="x", coin="litecoin")))
        self.assertIsNone(eng._oracle_for(FakeMarket(slug="y", coin=None)))
        self.assertIsNone(eng._oracle_for(FakeMarket(slug="z", coin="")))

    def test_snipe_inputs_refuses_a_coin_with_no_oracle(self):
        """The whole point: NOT "falls back to BTC", but "returns nothing"."""
        eng = _engine2(self._tmp.name)
        eng.binance = _NamedOracle("btc", self.now, s_open=100_000.0, s_t=101_000.0)
        out = eng._snipe_inputs(FakeMarket(slug="s", coin="solana"), self.now,
                                 eng.config.snipe_cfg)
        self.assertIsNone(out)
        self.assertEqual(eng.binance.calls, [],
                         "BTC's oracle was consulted for a SOLANA market")

    def test_each_coin_reads_only_its_own_oracle(self):
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])
        btc = _NamedOracle("btc", self.now, s_open=100_000.0, s_t=101_000.0)
        eth = _NamedOracle("eth", self.now, s_open=3_000.0, s_t=3_030.0)
        eng.binance = btc
        eng.coin_oracles["ethereum"] = eth

        got_eth = eng._snipe_inputs(FakeMarket(slug="e", coin="ethereum"), self.now,
                                     eng.config.snipe_cfg)
        self.assertIsNotNone(got_eth)
        self.assertEqual(got_eth[0], 3_000.0)   # ETH's open, not BTC's
        self.assertEqual(got_eth[1], 3_030.0)
        self.assertEqual(btc.calls, [], "ETH market touched BTC's oracle")

        got_btc = eng._snipe_inputs(FakeMarket(slug="b", coin="bitcoin"), self.now,
                                     eng.config.snipe_cfg)
        self.assertEqual(got_btc[0], 100_000.0)

    def test_a_coin_with_no_feed_entry_is_left_without_an_oracle(self):
        """Fail-closed at construction: a config naming a coin that is not in
        the verified table must not produce a working oracle."""
        raw = _raw()
        raw["strategy"]["close_snipe"]["shadow_coins"] = ["chainlinkcoin"]
        raw["storage"] = {"sqlite_path": str(Path(self._tmp.name) / "a.db"),
                          "fills_csv": str(Path(self._tmp.name) / "a.csv"),
                          "pnl_csv": str(Path(self._tmp.name) / "b.csv")}
        raw["logging"] = dict(raw["logging"], file=str(Path(self._tmp.name) / "a.log"))
        eng = Engine(Config(raw=raw, path=CONFIG_YAML))
        self.assertNotIn("chainlinkcoin", eng.coin_oracles)
        self.assertIsNone(eng._oracle_for(FakeMarket(slug="c", coin="chainlinkcoin")))

    def test_warmup_reads_the_markets_own_oracle(self):
        """A warm BTC buffer must not make a cold ETH market tradeable."""
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])
        eng.binance = _NamedOracle("btc", self.now, n=500)
        eng.coin_oracles["ethereum"] = _NamedOracle("eth", self.now, n=0)
        warm(eng, self.now)
        eng.clob = StubClob()
        eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.ledger.snipe_signals, [])
        self.assertEqual(eng.clob.calls, [], "cold ETH oracle still fetched books")


# ===========================================================================
# 4. Coin allowlist + shadow mode
# ===========================================================================

class TestCoinAllowlist(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def _ready(self, eng, coin):
        eng.binance = StubBinance(self.now)
        eng.coin_oracles[coin] = StubBinance(self.now, s_t=3000.6, s_open=3000.0)
        eng.clob = StubClob()
        warm(eng, self.now)
        return eng

    def test_shipped_config_allows_bitcoin_only(self):
        cfg = Config(raw=_raw(), path=CONFIG_YAML)
        self.assertEqual(cfg.allowed_coins(), ["bitcoin"])

    def test_bitcoin_still_fires(self):
        eng = self._ready(_engine2(self._tmp.name), "ethereum")
        eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)

    def test_a_coin_in_neither_list_never_signals_and_never_fetches_books(self):
        eng = self._ready(_engine2(self._tmp.name, coins_allowed=["bitcoin"],
                                    coins_shadow=[]), "solana")
        eng._maybe_snipe(FakeMarket(slug="s", coin="solana",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        self.assertEqual(eng.ledger.snipe_signals, [])
        self.assertEqual(eng.clob.calls, [], "a disallowed coin still burned REST calls")

    def test_widening_the_family_allowlist_alone_does_not_widen_coins(self):
        """M1 §6 item 4: `allowed_families` is a FAMILY allowlist. The two must
        be independent or the coin gate is decorative."""
        eng = _engine2(self._tmp.name, coins_allowed=["bitcoin"], coins_shadow=[])
        eng.config.raw["strategy"]["close_snipe"]["allowed_families"] = ["1h", "5m"]
        self._ready(eng, "bnb")
        eng._maybe_snipe(FakeMarket(slug="n", coin="bnb", close_ts=self.now + 3.0),
                          self.now)
        self.assertEqual(eng.executor.submissions, [])

    def test_every_non_bitcoin_coin_is_blocked_under_the_shipped_config(self):
        for coin in HOURLY_COINS:
            if coin == "bitcoin":
                continue
            with self.subTest(coin=coin):
                eng = self._ready(_engine2(self._tmp.name), coin)
                eng._maybe_snipe(FakeMarket(slug=f"m-{coin}", coin=coin,
                                             close_ts=self.now + 3.0), self.now)
                self.assertEqual(eng.executor.submissions, [],
                                 f"{coin} dispatched a fill under the shipped config")


class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0
        self.eng = _engine2(self._tmp.name, coins_allowed=["bitcoin"],
                            coins_shadow=["ethereum"])
        self.eng.binance = StubBinance(self.now)
        # +$0.60 on $3000 => |z| ~ 1.2 at tau=3, inside the band max_abs_z allows.
        # (Was 3030.0 — a 1% move in 3s, |z| ~ 57, which the gate now vetoes.)
        self.eng.coin_oracles["ethereum"] = StubBinance(self.now, s_t=3000.6,
                                                         s_open=3000.0)
        self.eng.clob = StubClob()
        warm(self.eng, self.now)

    def test_shadow_coin_records_a_signal(self):
        self.eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                          close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(self.eng.ledger.snipe_signals), 1,
                         "shadow mode must still produce the evidence it exists for")

    def test_the_signal_is_tagged_coin_and_shadow(self):
        """Without these fields the shadow tape is indistinguishable from a run
        of unlucky fill misses, and the whole mode is pointless."""
        self.eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                          close_ts=self.now + 3.0), self.now)
        meta = self.eng.ledger.snipe_signal_meta[0]
        self.assertEqual(meta["coin"], "ethereum")
        self.assertTrue(meta["shadow"])

    def test_a_fillable_signal_is_tagged_not_shadow(self):
        self.eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                          close_ts=self.now + 3.0), self.now)
        meta = self.eng.ledger.snipe_signal_meta[0]
        self.assertEqual(meta["coin"], "bitcoin")
        self.assertFalse(meta["shadow"])

    def test_book_age_is_recorded_on_every_signal(self):
        """The staleness statistic the verifier had to reconstruct from a vendor
        tape is now a first-class column, recorded live."""
        class _AgedClob(StubClob):
            def get_book(self_inner, token_id):
                b = super().get_book(token_id)
                import time
                b.book_ts = time.time() - 1.25
                return b

        self.eng.clob = _AgedClob()
        self.eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                          close_ts=self.now + 3.0), self.now)
        age = self.eng.ledger.snipe_signal_meta[0]["book_age_s"]
        self.assertIsNotNone(age)
        self.assertGreater(age, 0.0)

    def test_book_age_is_None_when_unmeasurable_not_zero(self):
        self.eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                          close_ts=self.now + 3.0), self.now)
        self.assertIsNone(self.eng.ledger.snipe_signal_meta[0]["book_age_s"])

    def test_shadow_coin_NEVER_dispatches_a_fill(self):
        self.eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                          close_ts=self.now + 3.0), self.now)
        self.assertEqual(self.eng.executor.submissions, [])

    def test_shadow_coin_did_fetch_books_ie_the_gate_is_after_evaluation(self):
        """If the shadow gate were placed before the book fetch we would get no
        signal and no evidence — the mode would be pointless. Pin the ordering."""
        self.eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                          close_ts=self.now + 3.0), self.now)
        self.assertEqual(sorted(self.eng.clob.calls), ["DOWN", "UP"])

    def test_bitcoin_is_unaffected_by_a_shadow_coin_being_configured(self):
        self.eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                          close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(self.eng.executor.submissions), 1)

    def test_a_coin_in_BOTH_lists_resolves_to_shadow(self):
        """A config contradiction must resolve the safe way, not the fast way."""
        eng = _engine2(self._tmp.name, coins_allowed=["bitcoin", "ethereum"],
                       coins_shadow=["ethereum"])
        eng.binance = StubBinance(self.now)
        eng.coin_oracles["ethereum"] = StubBinance(self.now, s_t=3000.6, s_open=3000.0)
        eng.clob = StubClob()
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="e", coin="ethereum",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        self.assertEqual(len(eng.ledger.snipe_signals), 1)

    def test_shipped_config_has_ethereum_in_shadow_not_allowed(self):
        cfg = Config(raw=_raw(), path=CONFIG_YAML)
        self.assertIn("ethereum", cfg.shadow_coins())
        self.assertNotIn("ethereum", cfg.allowed_coins())


class TestPerCoinCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    def test_bitcoin_keeps_the_shipped_250_clip(self):
        import inspect
        eng = _engine2(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = StubClob()
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                     close_ts=self.now + 3.0), self.now)
        fn, args, kwargs = eng.executor.submissions[0]
        bound = inspect.signature(eng._run_fill).bind(*args, **kwargs).arguments
        self.assertEqual(bound["cap_usd"], 250.0)

    def test_a_new_coin_does_not_inherit_the_bitcoin_clip(self):
        cfg = Config(raw=_raw(), path=CONFIG_YAML)
        self.assertEqual(cfg.coin_cap_usd("bitcoin"), 250.0)
        self.assertEqual(cfg.coin_cap_usd("ethereum"), 25.0)

    def test_an_unlisted_coin_falls_back_to_the_shared_cap(self):
        cfg = Config(raw=_raw(), path=CONFIG_YAML)
        self.assertEqual(cfg.coin_cap_usd("solana"),
                         float(cfg.sizing_cfg["per_event_cap_usd"]))


# ===========================================================================
# 5. Batched book fetch
# ===========================================================================

class _BatchClob:
    def __init__(self, ask=0.50, batch_answers=None, batch_raises=False):
        self.ask = ask
        self.single_calls: List[str] = []
        self.batch_calls: List[List[str]] = []
        self._batch_answers = batch_answers   # None => answer everything
        self._batch_raises = batch_raises

    def _book(self, tid):
        return OrderBook(token_id=tid, bids=[], asks=[BookLevel(self.ask, 5000.0)],
                         fetched_at=0.0)

    def get_book(self, token_id):
        self.single_calls.append(token_id)
        return self._book(token_id)

    def get_books(self, token_ids):
        self.batch_calls.append(list(token_ids))
        if self._batch_raises:
            raise RuntimeError("boom")
        answer = token_ids if self._batch_answers is None else self._batch_answers
        return {t: self._book(t) for t in answer}


class TestBatchedBookFetch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.eng = _engine2(self._tmp.name)

    def test_one_batch_call_replaces_the_two_sequential_gets(self):
        self.eng.clob = _BatchClob()
        books = self.eng._fetch_books(["UP", "DOWN"])
        self.assertEqual(self.eng.clob.batch_calls, [["UP", "DOWN"]])
        self.assertEqual(self.eng.clob.single_calls, [])
        self.assertEqual(sorted(books), ["DOWN", "UP"])

    def test_partial_batch_response_falls_back_per_missing_token(self):
        """A token missing from the batch must NOT read as an empty book."""
        self.eng.clob = _BatchClob(batch_answers=["UP"])
        books = self.eng._fetch_books(["UP", "DOWN"])
        self.assertEqual(self.eng.clob.single_calls, ["DOWN"])
        self.assertIsNotNone(books["DOWN"])

    def test_batch_transport_failure_falls_back_completely(self):
        self.eng.clob = _BatchClob(batch_answers=[])
        books = self.eng._fetch_books(["UP", "DOWN"])
        self.assertEqual(sorted(self.eng.clob.single_calls), ["DOWN", "UP"])
        self.assertIsNotNone(books["UP"])

    def test_batch_raising_does_not_kill_the_tick(self):
        self.eng.clob = _BatchClob(batch_raises=True)
        books = self.eng._fetch_books(["UP", "DOWN"])
        self.assertEqual(sorted(self.eng.clob.single_calls), ["DOWN", "UP"])
        self.assertIsNotNone(books["UP"])

    def test_a_client_without_the_batch_method_still_works(self):
        """Backward compatibility with any ClobClient that predates M5."""
        self.eng.clob = StubClob()
        books = self.eng._fetch_books(["UP", "DOWN"])
        self.assertEqual(sorted(self.eng.clob.calls), ["DOWN", "UP"])
        self.assertIsNotNone(books["UP"])

    def test_empty_token_list_makes_no_calls(self):
        self.eng.clob = _BatchClob()
        self.assertEqual(self.eng._fetch_books([]), {})
        self.assertEqual(self.eng.clob.batch_calls, [])
        self.assertEqual(self.eng.clob.single_calls, [])

    def test_snipe_uses_the_batch_path(self):
        self.eng.binance = StubBinance(1_000_000.0)
        self.eng.clob = _BatchClob()
        warm(self.eng, 1_000_000.0)
        self.eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                          close_ts=1_000_003.0), 1_000_000.0)
        self.assertEqual(len(self.eng.clob.batch_calls), 1)
        self.assertEqual(self.eng.clob.single_calls, [])


class TestBookPayloadParsing(unittest.TestCase):
    def test_timestamp_and_tick_size_are_parsed(self):
        import time
        now_ms = int(time.time() * 1000)
        b = OrderBook.from_raw("T", {"bids": [], "asks": [],
                                      "timestamp": str(now_ms), "tick_size": "0.001"})
        self.assertAlmostEqual(b.book_ts, now_ms / 1000.0, places=3)
        self.assertEqual(b.tick_size, 0.001)
        self.assertLess(abs(b.age_secs()), 5.0)

    def test_missing_timestamp_yields_unknown_age_not_zero(self):
        b = OrderBook.from_raw("T", {"bids": [], "asks": []})
        self.assertIsNone(b.book_ts)
        self.assertIsNone(b.age_secs())

    def test_implausible_timestamps_are_rejected(self):
        """A seconds-vs-milliseconds unit change must read as UNKNOWN, not as a
        book that is 56 years old (which would silently halt all trading)."""
        import time
        for bad in ("0", "-5", str(int(time.time())), "not-a-number", ""):
            with self.subTest(bad=bad):
                b = OrderBook.from_raw("T", {"bids": [], "asks": [], "timestamp": bad})
                self.assertIsNone(b.book_ts)

    def test_asks_are_sorted_ascending_regardless_of_payload_order(self):
        """Pre-existing invariant, re-pinned because from_raw changed."""
        b = OrderBook.from_raw("T", {"bids": [], "asks": [
            {"price": "0.70", "size": "1"}, {"price": "0.50", "size": "2"}]})
        self.assertEqual([lvl.price for lvl in b.asks], [0.50, 0.70])


class TestBatchClientRequestShape(unittest.TestCase):
    """The batch client is the one piece that talks to a live endpoint, so pin
    the request/response contract verified live on 2026-07-28."""

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    class _Sess:
        def __init__(self, payload):
            self.payload = payload
            self.posted = []

        def post(self, url, json=None, timeout=None):
            self.posted.append((url, json))
            return TestBatchClientRequestShape._Resp(self.payload)

    def _client(self, payload):
        c = ClobClientREST(Config(raw=_raw(), path=CONFIG_YAML))
        c.session = self._Sess(payload)
        return c

    def test_posts_a_list_of_token_id_objects_and_keys_on_asset_id(self):
        import time
        ts = str(int(time.time() * 1000))
        c = self._client([
            {"asset_id": "A", "bids": [], "asks": [{"price": "0.4", "size": "10"}],
             "timestamp": ts, "tick_size": "0.001"},
            {"asset_id": "B", "bids": [], "asks": [], "timestamp": ts},
        ])
        out = c.get_books(["A", "B"])
        url, body = c.session.posted[0]
        self.assertTrue(url.endswith("/books"))
        self.assertEqual(body, [{"token_id": "A"}, {"token_id": "B"}])
        self.assertEqual(sorted(out), ["A", "B"])
        self.assertEqual(out["A"].asks[0].price, 0.4)

    def test_a_token_the_server_omits_is_absent_not_empty(self):
        c = self._client([{"asset_id": "A", "bids": [], "asks": []}])
        out = c.get_books(["A", "B"])
        self.assertIn("A", out)
        self.assertNotIn("B", out)


# ===========================================================================
# 6. Stale-book guard (M5 guard 4)
# ===========================================================================

def _book(ask=0.50, age=None, size=5000.0):
    import time
    return OrderBook(token_id="T", bids=[], asks=[BookLevel(ask, size)],
                     fetched_at=time.time(),
                     book_ts=(None if age is None else time.time() - age))


class TestStaleBookPredicate(unittest.TestCase):
    def test_off_when_no_limit_configured(self):
        self.assertFalse(book_is_stale(_book(age=999.0), None))
        self.assertFalse(book_is_stale(_book(age=999.0), 0))

    def test_fresh_book_passes(self):
        self.assertFalse(book_is_stale(_book(age=1.0), 5.0))

    def test_stale_book_is_caught(self):
        self.assertTrue(book_is_stale(_book(age=9.0), 5.0))

    def test_boundary_is_inclusive_of_the_limit(self):
        self.assertFalse(book_is_stale(_book(age=4.99), 5.0))
        self.assertTrue(book_is_stale(_book(age=5.01), 5.0))

    def test_unknown_age_fails_OPEN(self):
        """Deliberate: the age comes from the CLOB's own `timestamp` field, so
        treating "field absent" as "too stale" would let an upstream schema
        change silently stop ALL trading. Documented in engine._book_too_stale."""
        self.assertFalse(book_is_stale(_book(age=None), 5.0))

    def test_missing_book_is_not_stale(self):
        self.assertFalse(book_is_stale(None, 5.0))

    def test_a_negative_age_from_clock_skew_is_never_stale(self):
        """Observed live 2026-07-28: this host's clock reads ~1.05s BEHIND the
        CLOB, so books report impossible negative ages. A negative age must
        clamp to 0, never wrap into a comparison that blocks trading."""
        self.assertFalse(book_is_stale(_book(age=-1.05), 5.0))
        self.assertFalse(book_is_stale(_book(age=-600.0), 5.0))

    def test_clock_skew_is_reported_once(self):
        import polybot.fill_engine as fe
        fe._skew_warned = False
        with self.assertLogs("polybot.fill_engine", level="WARNING") as cm:
            book_is_stale(_book(age=-1.05), 5.0)
        self.assertTrue(any("CLOCK SKEW" in m for m in cm.output))
        fe._skew_warned = False


class TestStaleBookBlocksFills(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_000_000.0

    class _AgedClob:
        def __init__(self, age):
            self.age = age
            self.calls: List[str] = []

        def get_book(self, token_id):
            self.calls.append(token_id)
            b = _book()
            if self.age is not None:
                import time
                b.book_ts = time.time() - self.age
            return b

    def test_shipped_config_has_the_guard_armed_at_5s(self):
        self.assertEqual(Config(raw=_raw(), path=CONFIG_YAML).max_book_age_s, 5.0)

    def test_engine_refuses_to_dispatch_on_a_stale_signal_book(self):
        eng = _engine2(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = self._AgedClob(age=30.0)
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(eng.executor.submissions, [])
        # ...but the signal IS recorded, so the miss is auditable.
        self.assertEqual(len(eng.ledger.snipe_signals), 1)

    def test_engine_dispatches_on_a_fresh_signal_book(self):
        eng = _engine2(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = self._AgedClob(age=0.5)
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)

    def test_a_book_with_no_timestamp_still_dispatches(self):
        eng = _engine2(self._tmp.name)
        eng.binance = StubBinance(self.now)
        eng.clob = self._AgedClob(age=None)
        warm(eng, self.now)
        eng._maybe_snipe(FakeMarket(slug="b", coin="bitcoin",
                                     close_ts=self.now + 3.0), self.now)
        self.assertEqual(len(eng.executor.submissions), 1)

    def test_fill_path_rejects_a_stale_fill_time_book(self):
        """The position the measurement actually indicts: HYPE drew 93.6% of its
        P&L from fills against books over 5s old."""
        clob = self._AgedClob(age=30.0)
        att = execute_taker_signal(
            clob, "T", "up", lambda p: 1.0 - p - fee_per_share(p, 0.07), 0.01,
            0.30, 0.99, 250.0, 0.07, latency_ms=0, sleep=False, max_book_age_s=5.0)
        self.assertEqual(att.outcome, "stale_book")
        self.assertFalse(att.filled)

    def test_fill_path_fills_a_fresh_book(self):
        clob = self._AgedClob(age=0.5)
        att = execute_taker_signal(
            clob, "T", "up", lambda p: 1.0 - p - fee_per_share(p, 0.07), 0.01,
            0.30, 0.99, 250.0, 0.07, latency_ms=0, sleep=False, max_book_age_s=5.0)
        self.assertEqual(att.outcome, "filled")

    def test_guard_off_fills_even_a_very_stale_book(self):
        """Backward compatibility: the pre-M5 behaviour is exactly max_book_age_s
        absent, and it must be unchanged."""
        clob = self._AgedClob(age=300.0)
        att = execute_taker_signal(
            clob, "T", "up", lambda p: 1.0 - p - fee_per_share(p, 0.07), 0.01,
            0.30, 0.99, 250.0, 0.07, latency_ms=0, sleep=False, max_book_age_s=None)
        self.assertEqual(att.outcome, "filled")

    def test_the_guard_reaches_the_LIVE_order_path_too(self):
        """A guard that only exists in the simulator is not a guard. Mirrors
        test_risk_guards.test_live_order_sizing_honours_the_filter."""
        raw = _raw()
        raw["mode"]["paper"] = False
        import os
        old = os.environ.get("POLYBOT_LIVE")
        os.environ["POLYBOT_LIVE"] = "1"
        try:
            cfg = Config(raw=raw, path=CONFIG_YAML)
            self.assertFalse(cfg.paper, "test setup failed: still in paper mode")
            router = ExecutionRouter(cfg, self._AgedClob(age=30.0))
            router._live_client = object()   # must never be used
            att = router.place_taker_buy(
                "T", "up", lambda p: 1.0 - p - fee_per_share(p, 0.07), 0.01,
                0.30, 0.99, 250.0, latency_ms=0)
            self.assertEqual(att.outcome, "stale_book")
        finally:
            if old is None:
                os.environ.pop("POLYBOT_LIVE", None)
            else:
                os.environ["POLYBOT_LIVE"] = old


# ===========================================================================
# 7. Oracle polling under multiple coins
# ===========================================================================

class TestMultiOraclePolling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_every_priced_coin_is_polled_each_cycle(self):
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])
        polled = []

        class _P:
            def __init__(self, name):
                self.name = name

            def poll_once(self_inner):
                polled.append(self_inner.name)
                return PricePoint(ts=0.0, price=1.0)

        eng.binance = _P("bitcoin")
        eng.coin_oracles["ethereum"] = _P("ethereum")
        eng._poll_binance_oracles(None)
        self.assertEqual(sorted(polled), ["bitcoin", "ethereum"])

    def test_one_broken_feed_does_not_stop_the_others(self):
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])
        polled = []

        class _Bad:
            def poll_once(self):
                raise RuntimeError("feed down")

        class _Good:
            def poll_once(self):
                polled.append("ok")
                return PricePoint(ts=0.0, price=1.0)

        eng.binance = _Bad()
        eng.coin_oracles["ethereum"] = _Good()
        eng._poll_binance_oracles(None)   # must not raise
        self.assertEqual(polled, ["ok"])

    def test_status_oracle_still_reports_bitcoin(self):
        eng = _engine2(self._tmp.name, coins_shadow=["ethereum"])

        class _P:
            def __init__(self, px):
                self.px = px

            def poll_once(self):
                return PricePoint(ts=123.0, price=self.px)

        eng.binance = _P(100_000.0)
        eng.coin_oracles["ethereum"] = _P(3_000.0)
        eng._poll_binance_oracles(None)
        self.assertEqual(eng.status.last_oracle_price, 100_000.0,
                         "status.oracle must stay the BITCOIN price — every "
                         "dashboard and alert reads it")

    def test_priced_coins_is_allowed_plus_shadow_not_discovery(self):
        eng = _engine2(self._tmp.name, coins_allowed=["bitcoin"],
                       coins_shadow=["ethereum"],
                       discover=["bitcoin", "ethereum", "solana", "bnb"])
        self.assertEqual(sorted(eng._priced_coins()), ["bitcoin", "ethereum"])
        self.assertNotIn("solana", eng.coin_oracles)
        self.assertNotIn("bnb", eng.coin_oracles)


# ===========================================================================
# 8. Backward compatibility — a pre-M5 config.yaml
# ===========================================================================

class TestPreM5ConfigIsSafe(unittest.TestCase):
    """A config.yaml written before this change must load and behave EXACTLY
    like the shipped bitcoin-only bot, with the new guard simply off."""

    def setUp(self):
        raw = _raw()
        cs = raw["strategy"]["close_snipe"]
        for k in ("allowed_coins", "shadow_coins", "max_book_age_s"):
            cs.pop(k, None)
        raw["discovery"].pop("hourly_coins", None)
        raw["sizing"].pop("per_coin_cap_usd", None)
        raw["endpoints"].pop("binance_futures_rest_base", None)
        self.cfg = Config(raw=raw, path=CONFIG_YAML)

    def test_defaults_to_bitcoin_only_everywhere(self):
        self.assertEqual(self.cfg.allowed_coins(), ["bitcoin"])
        self.assertEqual(self.cfg.shadow_coins(), [])
        self.assertEqual(self.cfg.hourly_coins(), ["bitcoin"])

    def test_stale_book_guard_defaults_off(self):
        self.assertIsNone(self.cfg.max_book_age_s)

    def test_caps_fall_back_to_the_shared_one(self):
        self.assertEqual(self.cfg.coin_cap_usd("bitcoin"), 250.0)
        self.assertEqual(self.cfg.coin_cap_usd("ethereum"), 250.0)

    def test_futures_base_has_a_value_and_is_not_the_spot_base(self):
        self.assertTrue(self.cfg.binance_futures_rest_base)
        self.assertNotEqual(self.cfg.binance_futures_rest_base,
                            self.cfg.binance_rest_base)

    def test_a_bad_max_book_age_value_reads_as_OFF_not_as_zero(self):
        raw = _raw()
        raw["strategy"]["close_snipe"]["max_book_age_s"] = "soon"
        self.assertIsNone(Config(raw=raw, path=CONFIG_YAML).max_book_age_s)


# ===========================================================================
# 9. Nothing pre-existing was weakened
# ===========================================================================

class TestM5WeakensNothing(unittest.TestCase):
    def setUp(self):
        self.raw = _raw()
        self.cfg = Config(raw=self.raw, path=CONFIG_YAML)

    def test_paper_mode_family_allowlist_and_bounds_unchanged(self):
        self.assertTrue(self.cfg.paper)
        self.assertEqual(self.cfg.snipe_cfg["allowed_families"], ["1h"])
        self.assertEqual(float(self.cfg.snipe_cfg["fair_cap"]), 0.98)
        self.assertEqual(float(self.cfg.snipe_cfg["sigma_1s_floor"]), 8.0e-06)
        self.assertEqual(float(self.cfg.execution_cfg["max_walk_above_best"]), 0.03)
        self.assertEqual(float(self.cfg.snipe_cfg["snipe_min_tau_secs"]), 2.5)
        self.assertEqual(float(self.cfg.snipe_cfg["snipe_last_secs"]), 5)
        self.assertEqual(float(self.cfg.snipe_cfg["edge_min"]), 0.03)

    def test_the_three_M4_guards_are_unchanged(self):
        self.assertFalse(self.cfg.adverse_size_cfg["enabled"])
        self.assertTrue(self.cfg.warmup_cfg["enabled"])
        self.assertEqual(int(self.cfg.warmup_cfg["min_oracle_samples"]), 60)
        self.assertEqual(float(self.cfg.warmup_cfg["min_uptime_secs"]), 120)
        self.assertTrue(self.cfg.risk_cfg["daily_loss_limit"]["enabled"])
        self.assertTrue(self.cfg.risk_cfg["consecutive_loss_brake"]["enabled"])
        self.assertEqual(int(self.cfg.risk_cfg["consecutive_loss_brake"]
                             ["max_consecutive_losses"]), 4)

    def test_chainlink_families_are_still_off(self):
        for fam in ("5m", "15m", "4h"):
            self.assertFalse(self.raw["families"][fam]["close_snipe"])
            self.assertFalse(self.raw["families"][fam]["settle_sweep"])

    def test_discovery_scan_respects_hourly_coins(self):
        """REGRESSION, found in the live dry run of 2026-07-28.

        `discover_markets` has two sources: deterministic slug construction
        (which loops over `hourly_coins`) and a broad `/events` scan (which
        pattern-matches). Widening `_HOURLY_RE` to seven coins made the SECOND
        source ingest every coin regardless of the config, and the live dry run
        duly tracked bnb, hype, solana, xrp and dogecoin markets. Nothing unsafe
        happened — the coin allowlist refused all of them, loudly — but
        discovery breadth must come from one place.
        """
        from polybot.polymarket import discover_markets

        class _Gamma:
            """Slug lookups miss; the events page offers all seven coins."""

            def get_market_by_slug(self, slug, closed=None):
                return None

            def get_events_page(self, closed, limit, offset, order, ascending):
                if offset:
                    return []
                return [{"markets": [{
                    "slug": f"{c}-up-or-down-july-15-2026-3pm-et",
                    "clobTokenIds": '["U","D"]',
                    "endDate": "2026-07-15T20:00:00Z",
                    "startDate": "2026-07-13T20:00:00Z",
                } for c in HOURLY_COINS]}]

        raw = _raw()
        raw["discovery"]["hourly_coins"] = ["bitcoin", "ethereum"]
        found = discover_markets(_Gamma(), Config(raw=raw, path=CONFIG_YAML))
        self.assertEqual(sorted(m.coin for m in found.values()),
                         ["bitcoin", "ethereum"])

    def test_discovery_scan_still_finds_the_short_btc_families(self):
        """The coin filter must not accidentally drop btc-updown-* slugs, which
        carry no coin token at all."""
        from polybot.polymarket import discover_markets

        class _Gamma:
            def get_market_by_slug(self, slug, closed=None):
                return None

            def get_events_page(self, closed, limit, offset, order, ascending):
                if offset:
                    return []
                return [{"markets": [{
                    "slug": "btc-updown-5m-1785000000",
                    "clobTokenIds": '["U","D"]',
                    "endDate": "2026-07-15T20:05:00Z",
                    "startDate": "2026-07-15T20:00:00Z",
                }]}]

        found = discover_markets(_Gamma(), Config(raw=_raw(), path=CONFIG_YAML))
        self.assertEqual([m.family for m in found.values()], ["5m"])

    def test_discovery_list_never_exceeds_what_can_be_priced(self):
        """A coin discovered but not priced is only wasted REST calls, but it
        also means an operator widened one list and forgot the other."""
        priced = set(self.cfg.allowed_coins()) | set(self.cfg.shadow_coins())
        self.assertEqual(set(self.cfg.hourly_coins()), priced)


if __name__ == "__main__":
    unittest.main()
