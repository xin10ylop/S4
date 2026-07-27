"""ChainlinkOracle unit tests — fully offline.

Every test drives the real ingest/read code through a FAKE websocket transport
and a FAKE http getter. Nothing here touches the network; the live end-to-end
proof lives in bot/scripts/smoke_chainlink.py and audit/B2_impl_chainlink.md.
"""
import json
import math
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.oracle import (CHAINLINK_BTC_USD_FEED_ID, WEI, ChainlinkOracle,
                            decode_data_streams_blob, read_onchain_aggregator)

BASE = 1_785_000_000  # fixed observation second used across tests


def cfg(**over):
    """Config with the standby off unless a test opts in, so background REST
    polling can never interfere with a deterministic assertion."""
    base = {
        "rtds_ws": "wss://example.invalid",
        "topic": "crypto_prices_chainlink",
        "symbol": "btc/usd",
        "feed_id": CHAINLINK_BTC_USD_FEED_ID,
        "max_staleness_secs": 4.0,
        "max_gap_secs": 5,
        "lookback_secs": 30,
        "ping_interval_secs": 5.0,
        "standby": {"enabled": False},
    }
    base.update(over)
    return base


def update_frame(sec, wei, symbol="btc/usd"):
    return json.dumps({
        "topic": "crypto_prices_chainlink", "type": "update",
        "payload": {"full_accuracy_value": str(wei), "symbol": symbol,
                    "timestamp": sec * 1000, "value": wei / WEI},
    })


def snapshot_frame(rows):
    return json.dumps({"payload": {"data": [
        {"timestamp": s * 1000, "value": v} for s, v in rows]}})


class FakeClock:
    def __init__(self, t=float(BASE)):
        self.t = t

    def __call__(self):
        return self.t


class FakeWS:
    """Replays a scripted list of frames, then blocks/raises like a real socket."""

    def __init__(self, frames, raise_after=True):
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.raise_after = raise_after
        self.recv_calls = 0

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout):
        self.recv_calls += 1
        if self.frames:
            return self.frames.pop(0)
        if self.raise_after:
            raise ConnectionError("fake socket exhausted")
        return ""

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# message parsing
# ---------------------------------------------------------------------------
class TestIngestMessage(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.o = ChainlinkOracle(cfg=cfg(), clock=self.clock)

    def test_update_uses_exact_18_decimal_integer(self):
        wei = 65211802547675000000000
        self.assertEqual(self.o.ingest_message(update_frame(BASE, wei)), 1)
        s = self.o.latest_raw()
        self.assertEqual(s.wei, wei)          # exact integer preserved
        self.assertTrue(s.exact)
        self.assertEqual(s.obs_sec, BASE)

    def test_ts_is_the_observation_second_not_receipt_time(self):
        self.clock.t = BASE + 1.4          # publish lag
        self.o.ingest_message(update_frame(BASE, 65000 * WEI))
        pt = self.o.latest()
        self.assertEqual(pt.ts, float(BASE))   # NOT BASE + 1.4
        self.assertAlmostEqual(pt.price, 65000.0)

    def test_snapshot_is_ingested_but_marked_inexact(self):
        n = self.o.ingest_message(snapshot_frame(
            [(BASE - 2, 65001.5), (BASE - 1, 65002.25), (BASE, 65003.0)]))
        self.assertEqual(n, 3)
        self.assertFalse(self.o.latest_raw().exact)
        self.assertAlmostEqual(self.o.price_at_or_before(BASE - 1), 65002.25)

    def test_exact_update_upgrades_snapshot_value_for_same_second(self):
        self.o.ingest_message(snapshot_frame([(BASE, 65003.0)]))
        exact_wei = 65003000000000000000001      # differs in the last wei
        self.o.ingest_message(update_frame(BASE, exact_wei))
        s = self.o.latest_raw()
        self.assertEqual(s.wei, exact_wei)
        self.assertTrue(s.exact)
        self.assertEqual(self.o.n_disagreements, 0)   # an upgrade is not a disagreement

    def test_snapshot_never_clobbers_an_exact_value(self):
        exact_wei = 65003000000000000000001
        self.o.ingest_message(update_frame(BASE, exact_wei))
        self.o.ingest_message(snapshot_frame([(BASE, 65003.0)]))
        self.assertEqual(self.o.latest_raw().wei, exact_wei)
        self.assertEqual(self.o.n_disagreements, 0)

    def test_two_exact_sources_disagreeing_is_loud_and_first_wins(self):
        self.o.ingest_sample(BASE, 65003 * WEI, "rtds")
        self.o.ingest_sample(BASE, 65009 * WEI, "gmx_standby")
        self.assertEqual(self.o.latest_raw().wei, 65003 * WEI)
        self.assertEqual(self.o.n_disagreements, 1)

    def test_other_symbols_and_junk_are_ignored(self):
        self.assertEqual(self.o.ingest_message(update_frame(BASE, WEI, symbol="eth/usd")), 0)
        for junk in ("", "   ", "PONG", "not json", "[]", "null",
                     json.dumps({"payload": None}), json.dumps({"no_payload": 1})):
            self.assertEqual(self.o.ingest_message(junk), 0)
        self.assertIsNone(self.o.latest())


# ---------------------------------------------------------------------------
# publication-lag / no-peeking guarantees
# ---------------------------------------------------------------------------
class TestPublicationLag(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.o = ChainlinkOracle(cfg=cfg(), clock=self.clock)

    def test_future_stamped_print_is_rejected(self):
        self.clock.t = float(BASE)
        # A print stamped 10s ahead of our clock cannot have been observed yet.
        self.assertFalse(self.o.ingest_sample(BASE + 10, 65000 * WEI, "rtds"))
        self.assertEqual(self.o.n_rejected_future, 1)
        self.assertIsNone(self.o.latest())

    def test_small_clock_skew_is_tolerated(self):
        self.clock.t = float(BASE)
        self.assertTrue(self.o.ingest_sample(BASE + 1, 65000 * WEI, "rtds"))

    def test_as_of_hides_prints_not_yet_received(self):
        # print for second BASE physically arrives at BASE+1.4 (publish lag)
        self.clock.t = BASE + 1.4
        self.o.ingest_message(update_frame(BASE, 65000 * WEI))
        # A caller asking "what did I hold at BASE+0.5?" must get nothing.
        self.assertIsNone(self.o.latest(as_of=BASE + 0.5))
        self.assertIsNone(self.o.price_at_or_before(BASE, as_of=BASE + 0.5))
        self.assertIsNone(self.o.price_at_or_after(BASE, as_of=BASE + 0.5))
        # ...and the same query after the print landed must return it.
        self.assertIsNotNone(self.o.latest(as_of=BASE + 1.5))

    def test_at_or_after_never_searches_past_newest_second(self):
        self.o.ingest_sample(BASE, 65000 * WEI, "rtds")
        # boundary is 2s in the future relative to our newest print: even
        # though it is inside max_gap_secs, we must not answer.
        self.assertIsNone(self.o.price_at_or_after(BASE + 2))


# ---------------------------------------------------------------------------
# staleness / degradation
# ---------------------------------------------------------------------------
class TestStaleness(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.o = ChainlinkOracle(cfg=cfg(max_staleness_secs=4.0), clock=self.clock)

    def test_empty_oracle_returns_none_and_infinite_staleness(self):
        self.assertIsNone(self.o.latest())
        self.assertEqual(self.o.staleness(), float("inf"))
        self.assertFalse(self.o.health()["healthy"])

    def test_healthy_lag_still_returns_a_price(self):
        self.clock.t = BASE + 1.4                 # p50 publish lag
        self.o.ingest_message(update_frame(BASE, 65000 * WEI))
        self.assertIsNotNone(self.o.latest())
        self.assertTrue(self.o.health()["healthy"])

    def test_stale_feed_returns_none(self):
        self.clock.t = BASE + 1.0
        self.o.ingest_message(update_frame(BASE, 65000 * WEI))
        self.assertIsNotNone(self.o.latest())
        self.clock.t = BASE + 4.5                 # feed died 4.5s ago
        self.assertIsNone(self.o.latest())
        self.assertFalse(self.o.health()["healthy"])
        # the raw sample is still inspectable for diagnostics
        self.assertIsNotNone(self.o.latest_raw())
        self.assertAlmostEqual(self.o.staleness(), 4.5)

    def test_poll_once_returns_none_when_stale(self):
        o = ChainlinkOracle(cfg=cfg(), clock=self.clock,
                            transport_factory=lambda url: FakeWS([]))
        o._started = True                          # don't spawn threads
        self.clock.t = BASE + 1.0
        o.ingest_message(update_frame(BASE, 65000 * WEI))
        self.assertIsNotNone(o.poll_once())
        self.clock.t = BASE + 30.0
        self.assertIsNone(o.poll_once())


# ---------------------------------------------------------------------------
# strike / settle / winner — Polymarket's verified resolution rule
# ---------------------------------------------------------------------------
class TestResolutionRule(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(t=float(BASE + 1000))
        self.o = ChainlinkOracle(cfg=cfg(), clock=self.clock)

    def test_strike_is_first_print_at_or_after_boundary(self):
        # second BASE is missing (a normal ~3% upstream gap)
        self.o.ingest_sample(BASE - 1, 64990 * WEI, "rtds")
        self.o.ingest_sample(BASE + 1, 65010 * WEI, "rtds")
        # backfill, not forward-fill: 99.59% vs 98.03% correct (A1 §2b2)
        self.assertAlmostEqual(self.o.strike(BASE), 65010.0)
        self.assertAlmostEqual(self.o.price_at_or_before(BASE), 64990.0)

    def test_gap_larger_than_max_gap_returns_none(self):
        self.o.ingest_sample(BASE + 6, 65010 * WEI, "rtds")
        self.assertIsNone(self.o.strike(BASE))     # max_gap_secs = 5

    def test_winner_compares_integers_not_floats(self):
        # Two prices whose float64 representations are identical but whose
        # 18-decimal integers differ by one wei. A float comparison would call
        # this a tie; the integer comparison must call it Up.
        open_wei = 65000 * WEI
        close_wei = open_wei + 1
        self.assertEqual(float(open_wei) / WEI, float(close_wei) / WEI)  # float-identical
        self.o.ingest_sample(BASE, open_wei, "rtds")
        self.o.ingest_sample(BASE + 300, close_wei, "rtds")
        self.assertEqual(self.o.winner(BASE, BASE + 300), "up")

    def test_winner_ties_resolve_up(self):
        self.o.ingest_sample(BASE, 65000 * WEI, "rtds")
        self.o.ingest_sample(BASE + 300, 65000 * WEI, "rtds")
        self.assertEqual(self.o.winner(BASE, BASE + 300), "up")   # close >= open

    def test_winner_down(self):
        self.o.ingest_sample(BASE, 65000 * WEI, "rtds")
        self.o.ingest_sample(BASE + 300, 64999 * WEI, "rtds")
        self.assertEqual(self.o.winner(BASE, BASE + 300), "down")

    def test_winner_none_when_a_boundary_is_missing(self):
        self.o.ingest_sample(BASE, 65000 * WEI, "rtds")
        self.assertIsNone(self.o.winner(BASE, BASE + 300))

    def test_price_at_second_uses_at_or_after(self):
        self.o.ingest_sample(BASE + 1, 65010 * WEI, "rtds")
        self.assertAlmostEqual(self.o.price_at_second(BASE), 65010.0)
        self.assertIsNone(self.o.price_at_second(BASE - 100))


# ---------------------------------------------------------------------------
# interface parity with BinanceOracle
# ---------------------------------------------------------------------------
class TestInterfaceParity(unittest.TestCase):
    def test_exposes_every_method_the_engine_uses(self):
        from polybot.oracle import BinanceOracle
        required = ["poll_once", "latest", "price_at_or_before",
                    "rolling_log_return_std", "price_at_second"]
        for name in required:
            self.assertTrue(callable(getattr(BinanceOracle, name, None)), name)
            self.assertTrue(callable(getattr(ChainlinkOracle, name, None)), name)

    def test_latest_returns_the_same_PricePoint_type(self):
        from polybot.oracle import PricePoint
        clock = FakeClock(t=float(BASE))
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        o.ingest_sample(BASE, 65000 * WEI, "rtds")
        self.assertIsInstance(o.latest(), PricePoint)

    def test_rolling_log_return_std(self):
        clock = FakeClock(t=float(BASE + 200))
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        self.assertTrue(math.isnan(o.rolling_log_return_std()))   # empty
        for i in range(10):
            o.ingest_sample(BASE + i, 65000 * WEI, "rtds")
        self.assertAlmostEqual(o.rolling_log_return_std(120.0), 0.0)  # flat
        o2 = ChainlinkOracle(cfg=cfg(), clock=clock)
        for i in range(30):
            o2.ingest_sample(BASE + i, int(65000 * (1.0001 ** i) * WEI), "rtds")
        std = o2.rolling_log_return_std(120.0)
        self.assertFalse(math.isnan(std))
        self.assertGreaterEqual(std, 0.0)

    def test_rolling_std_only_uses_the_window(self):
        clock = FakeClock(t=float(BASE + 1000))
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        for i in range(10):                       # ancient, wild
            o.ingest_sample(BASE + i, int((65000 + i * 500) * WEI), "rtds")
        for i in range(10):                       # recent, flat
            o.ingest_sample(BASE + 900 + i, 65000 * WEI, "rtds")
        self.assertAlmostEqual(o.rolling_log_return_std(120.0), 0.0)


# ---------------------------------------------------------------------------
# the websocket loop itself, driven by a fake transport
# ---------------------------------------------------------------------------
class TestWSLoop(unittest.TestCase):
    def test_subscribe_frame_matches_the_verified_rtds_contract(self):
        o = ChainlinkOracle(cfg=cfg())
        frame = json.loads(o._subscribe_frame())
        self.assertEqual(frame["action"], "subscribe")
        sub = frame["subscriptions"][0]
        self.assertEqual(sub["topic"], "crypto_prices_chainlink")
        self.assertEqual(sub["type"], "update")
        # filters must be a JSON *string*, not a nested object
        self.assertIsInstance(sub["filters"], str)
        self.assertEqual(json.loads(sub["filters"]), {"symbol": "btc/usd"})

    def test_filters_string_is_compact_json(self):
        """REGRESSION GUARD — this one byte decides whether the feed works.

        The RTDS server matches `filters` as a literal string. Verified live:
        '{"symbol": "btc/usd"}' (json.dumps default, with a space) gets you the
        connect snapshot and then ZERO updates forever, which looks like a
        healthy connection until the staleness guard trips. The compact form
        streams normally. Do not "tidy" this back to a plain json.dumps.
        """
        o = ChainlinkOracle(cfg=cfg())
        filters = json.loads(o._subscribe_frame())["subscriptions"][0]["filters"]
        self.assertEqual(filters, '{"symbol":"btc/usd"}')
        self.assertNotIn(" ", filters)

    def test_loop_subscribes_pings_and_ingests_then_reconnects(self):
        clock = FakeClock(t=float(BASE))
        frames = ["", snapshot_frame([(BASE - 2, 64999.0)]),
                  update_frame(BASE - 1, 65000 * WEI),
                  update_frame(BASE, 65001 * WEI)]
        sockets = []

        def factory(url):
            self.assertEqual(url, "wss://example.invalid")
            ws = FakeWS(list(frames))
            sockets.append(ws)
            return ws

        o = ChainlinkOracle(cfg=cfg(), clock=clock, transport_factory=factory)
        # stop after the first socket dies so the loop terminates
        original = o.ingest_message

        def counting(raw, received_at=None):
            n = original(raw, received_at)
            if o.latest_raw() and o.latest_raw().obs_sec == BASE:
                o._stop.set()
            return n

        o.ingest_message = counting
        o._ws_loop()

        self.assertGreaterEqual(len(sockets), 1)
        first = sockets[0]
        self.assertEqual(json.loads(first.sent[0])["action"], "subscribe")
        self.assertTrue(first.closed)
        self.assertAlmostEqual(o.latest(as_of=BASE).price, 65001.0)
        self.assertEqual(o.n_rtds, 2)
        self.assertEqual(o.n_snapshot, 1)

    def test_loop_survives_a_transport_that_always_fails(self):
        o = ChainlinkOracle(cfg=cfg(), clock=FakeClock())
        attempts = {"n": 0}

        def boom(url):
            attempts["n"] += 1
            if attempts["n"] >= 3:
                o._stop.set()
            raise OSError("connection refused")

        o._transport_factory = boom
        o._ws_loop()             # must return, not raise
        self.assertGreaterEqual(o.n_reconnects, 3)
        self.assertIsNone(o.latest())          # and still no opinion
        self.assertIn("OSError", o.last_error)

    def test_ping_is_sent_on_the_documented_cadence(self):
        clock = FakeClock(t=float(BASE))
        ws = FakeWS([update_frame(BASE, 65000 * WEI)] * 3, raise_after=True)
        o = ChainlinkOracle(cfg=cfg(ping_interval_secs=5.0), clock=clock,
                            transport_factory=lambda url: ws)
        original = o.ingest_message
        seen = {"n": 0}

        def advancing(raw, received_at=None):
            clock.t += 6.0            # each frame takes >ping_interval
            seen["n"] += 1
            if seen["n"] >= 3:
                o._stop.set()
            return original(raw, received_at)

        o.ingest_message = advancing
        o._ws_loop()
        self.assertEqual(json.loads(ws.sent[0])["action"], "subscribe")
        self.assertIn("PING", ws.sent[1:])


# ---------------------------------------------------------------------------
# GMX standby: signed Data Streams report decoding
# ---------------------------------------------------------------------------
def build_blob(feed_id_hex, obs_sec, price_wei):
    """Construct an abi.encode(bytes32[3], bytes, bytes32[], bytes32[], bytes32)
    envelope wrapping a v3 report — the exact shape GMX serves."""
    def w(i):
        return i.to_bytes(32, "big")

    def sw(i):                     # signed, right-aligned in 32 bytes
        return (i & ((1 << 256) - 1)).to_bytes(32, "big")

    report = (bytes.fromhex(feed_id_hex[2:]) + w(obs_sec - 1) + w(obs_sec)
              + w(10) + w(11) + w(obs_sec + 100) + sw(price_wei)
              + sw(price_wei - 5) + sw(price_wei + 5))
    head = w(1) + w(2) + w(3)      # bytes32[3]
    head += w(7 * 32)              # offset -> report
    head += w(7 * 32 + 32 + len(report))          # offset -> bytes32[] rs
    head += w(7 * 32 + 32 + len(report) + 32)     # offset -> bytes32[] ss
    head += w(0)                   # rawVs
    tail = w(len(report)) + report + w(0) + w(0)
    return "0x" + (head + tail).hex()


class TestStandbyDecode(unittest.TestCase):
    def test_decodes_a_well_formed_report(self):
        blob = build_blob(CHAINLINK_BTC_USD_FEED_ID, BASE, 65123456789012345678901)
        out = decode_data_streams_blob(blob)
        self.assertIsNotNone(out)
        feed, obs, price = out
        self.assertEqual(feed, CHAINLINK_BTC_USD_FEED_ID)
        self.assertEqual(obs, BASE)
        self.assertEqual(price, 65123456789012345678901)

    def test_malformed_blobs_return_none_not_exceptions(self):
        for bad in ("", "0x", "0xzz", "0x" + "00" * 10, "not hex at all"):
            self.assertIsNone(decode_data_streams_blob(bad))

    def test_standby_accepts_the_right_feed(self):
        blob = build_blob(CHAINLINK_BTC_USD_FEED_ID, BASE, 65000 * WEI)
        payload = {"signedPrices": [
            {"tokenSymbol": "ETH", "blob": build_blob("0x" + "11" * 32, BASE, WEI)},
            {"tokenSymbol": "BTC", "blob": blob},
        ]}
        o = ChainlinkOracle(cfg=cfg(standby={"enabled": True}),
                            clock=FakeClock(t=float(BASE + 2)),
                            http_get=lambda url, **kw: FakeResponse(payload))
        self.assertEqual(o.poll_standby_once(), BASE)
        self.assertEqual(o.latest_raw().source, "gmx_standby")
        self.assertEqual(o.n_standby, 1)

    def test_standby_rejects_a_wrong_feed_id(self):
        payload = {"signedPrices": [
            {"tokenSymbol": "BTC", "blob": build_blob("0x" + "22" * 32, BASE, 65000 * WEI)}]}
        o = ChainlinkOracle(cfg=cfg(standby={"enabled": True}),
                            clock=FakeClock(t=float(BASE + 2)),
                            http_get=lambda url, **kw: FakeResponse(payload))
        self.assertIsNone(o.poll_standby_once())
        self.assertIsNone(o.latest())          # wrong feed => no price at all

    def test_standby_http_failure_rotates_host_and_returns_none(self):
        def boom(url, **kw):
            raise OSError("dns")
        o = ChainlinkOracle(cfg=cfg(standby={"enabled": True}), http_get=boom)
        self.assertIsNone(o.poll_standby_once())
        self.assertEqual(o._standby_url_idx, 1)

    def test_standby_and_rtds_agreeing_is_not_a_disagreement(self):
        blob = build_blob(CHAINLINK_BTC_USD_FEED_ID, BASE, 65000 * WEI)
        o = ChainlinkOracle(
            cfg=cfg(standby={"enabled": True}), clock=FakeClock(t=float(BASE + 2)),
            http_get=lambda url, **kw: FakeResponse(
                {"signedPrices": [{"tokenSymbol": "BTC", "blob": blob}]}))
        o.ingest_message(update_frame(BASE, 65000 * WEI))
        o.poll_standby_once()
        self.assertEqual(o.n_disagreements, 0)


# ---------------------------------------------------------------------------
# on-chain aggregator (liveness check only)
# ---------------------------------------------------------------------------
class TestOnchainAggregator(unittest.TestCase):
    def test_decodes_latest_round_data(self):
        answer = 6527799000000                  # $65,277.99 at 8 decimals
        updated_at = int(time.time()) - 3
        words = [55340232221132376148, answer, updated_at - 4, updated_at,
                 55340232221132376148]
        result = "0x" + "".join(w.to_bytes(32, "big").hex() for w in words)
        out = read_onchain_aggregator(
            "https://rpc.invalid", "0xc907",
            http_post=lambda url, **kw: FakeResponse({"result": result}))
        self.assertAlmostEqual(out["price"], 65277.99, places=6)
        self.assertEqual(out["updated_at"], updated_at)
        self.assertLess(out["age_secs"], 10)

    def test_bad_rpc_returns_none(self):
        self.assertIsNone(read_onchain_aggregator(
            "https://rpc.invalid", "0x0",
            http_post=lambda url, **kw: FakeResponse({"error": "boom"})))

        def boom(url, **kw):
            raise OSError("refused")
        self.assertIsNone(read_onchain_aggregator("https://rpc.invalid", "0x0",
                                                  http_post=boom))


# ---------------------------------------------------------------------------
# buffer / concurrency hygiene
# ---------------------------------------------------------------------------
class TestBuffer(unittest.TestCase):
    def test_buffer_evicts_old_seconds(self):
        clock = FakeClock(t=float(BASE + 5000))
        o = ChainlinkOracle(cfg=cfg(buffer_secs=100), clock=clock)
        for i in range(2000):
            o.ingest_sample(BASE + i, (65000 + i) * WEI, "rtds")
        self.assertLessEqual(len(o._px), 100 + 512 + 1)
        self.assertIsNotNone(o.latest_raw())
        self.assertEqual(o.latest_raw().obs_sec, BASE + 1999)

    def test_concurrent_ingest_and_read_is_consistent(self):
        clock = FakeClock(t=float(BASE + 10_000))
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        errors = []

        def writer(lo):
            try:
                for i in range(lo, lo + 500):
                    o.ingest_sample(BASE + i, (65000 + i) * WEI, "rtds")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(500):
                    o.latest()
                    o.price_at_or_before(BASE + 250)
                    o.rolling_log_return_std(120.0)
                    o.health()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=writer, args=(0,)),
              threading.Thread(target=writer, args=(500,)),
              threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(o._px), 1000)

    def test_coverage_reports_the_gap_rate(self):
        clock = FakeClock(t=float(BASE + 400))
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        for i in range(300):
            if i % 10 != 0:                    # drop 10% of seconds
                o.ingest_sample(BASE + i, 65000 * WEI, "rtds")
        self.assertAlmostEqual(o.coverage(300), 0.9, places=2)


# ---------------------------------------------------------------------------
# config plumbing + safety defaults
# ---------------------------------------------------------------------------
class TestConfigWiring(unittest.TestCase):
    def setUp(self):
        from polybot.config import load_config
        self.config = load_config()

    def test_shipped_config_has_no_secrets_and_no_enabled_chainlink_family(self):
        cl = self.config.chainlink_cfg
        self.assertTrue(cl["rtds_ws"].startswith("wss://"))
        self.assertEqual(cl["feed_id"].lower(), CHAINLINK_BTC_USD_FEED_ID)
        blob = json.dumps(self.config.raw).lower()
        for banned in ("private_key", "api_secret", "passphrase", "hmac", "0x" + "0" * 60):
            self.assertNotIn(banned, blob)
        for name, fam in self.config.families().items():
            if fam.oracle == "chainlink":
                self.assertFalse(fam.close_snipe, f"{name} close_snipe must ship OFF")
                self.assertFalse(fam.settle_sweep, f"{name} settle_sweep must ship OFF")

    def test_engine_does_not_build_the_oracle_with_shipped_flags(self):
        from polybot.engine import Engine
        needed = Engine._chainlink_needed(type("E", (), {"config": self.config})())
        self.assertFalse(needed)

    def test_engine_builds_the_oracle_once_a_family_is_switched_on(self):
        from polybot.config import Config
        from polybot.engine import Engine
        raw = json.loads(json.dumps(self.config.raw))
        raw["families"]["5m"]["close_snipe"] = True
        cfg2 = Config(raw=raw, path=self.config.path)
        self.assertTrue(Engine._chainlink_needed(type("E", (), {"config": cfg2})()))

    def test_secret_rpc_url_comes_from_env_not_yaml(self):
        import os
        from polybot.config import load_config
        var = self.config.chainlink_cfg["onchain_check"]["rpc_url_env"]
        self.assertNotIn("://user:", self.config.chainlink_cfg["onchain_check"]["rpc_url"])
        os.environ[var] = "https://secret.example/xyzkey"
        try:
            self.assertEqual(load_config().chainlink_cfg["onchain_check"]["rpc_url"],
                             "https://secret.example/xyzkey")
        finally:
            del os.environ[var]

    def test_constructing_from_a_config_object_works(self):
        o = ChainlinkOracle(self.config)
        self.assertEqual(o.feed_id, CHAINLINK_BTC_USD_FEED_ID)
        self.assertEqual(o.topic, "crypto_prices_chainlink")
        self.assertIsNone(o.latest())      # nothing ingested, no opinion


# ---------------------------------------------------------------------------
# strategy-level winner determination through the chainlink path
# ---------------------------------------------------------------------------
class TestStrategyResolveWinner(unittest.TestCase):
    def _market(self, family="5m", start=BASE, close=BASE + 300):
        import datetime as dt
        from polybot.polymarket import Market
        return Market(
            slug=f"btc-updown-{family}-{start}", family=family, question="", condition_id="",
            up_token_id="U", down_token_id="D",
            start_date=dt.datetime.fromtimestamp(start, tz=dt.timezone.utc),
            end_date=dt.datetime.fromtimestamp(close, tz=dt.timezone.utc),
            accepting_orders=True, closed=False, active=True, enable_order_book=True,
            order_min_size=5, tick_size=0.01, raw={})

    def test_chainlink_path_is_used_and_no_longer_raises(self):
        from polybot.strategy import resolve_winner
        o = ChainlinkOracle(cfg=cfg(), clock=FakeClock(t=float(BASE + 400)))
        o.ingest_sample(BASE, 65000 * WEI, "rtds")
        o.ingest_sample(BASE + 300, 65000 * WEI + 1, "rtds")
        wd = resolve_winner(self._market(), binance=None, distance_guard_usd=3.0, chainlink=o)
        self.assertEqual(wd.winner, "up")
        self.assertEqual(wd.reason, "chainlink_data_streams")

    def test_missing_boundary_skips_rather_than_falling_back_to_binance(self):
        from polybot.strategy import resolve_winner
        o = ChainlinkOracle(cfg=cfg(), clock=FakeClock(t=float(BASE + 400)))
        o.ingest_sample(BASE, 65000 * WEI, "rtds")     # no close print
        wd = resolve_winner(self._market(), binance=None, distance_guard_usd=3.0, chainlink=o)
        self.assertIsNone(wd.winner)
        self.assertEqual(wd.reason, "chainlink_boundary_unavailable")

    def test_1h_still_uses_binance_even_when_chainlink_is_present(self):
        from polybot.strategy import resolve_winner

        class FakeBinance:
            def hour_open_close(self, ts):
                return 65000.0, 65010.0

        o = ChainlinkOracle(cfg=cfg())
        wd = resolve_winner(self._market(family="1h", close=BASE + 3600),
                            binance=FakeBinance(), distance_guard_usd=3.0, chainlink=o)
        self.assertEqual(wd.winner, "up")
        self.assertEqual(wd.reason, "binance_1h_candle")


# ---------------------------------------------------------------------------
# engine-level oracle selection (Engine._snipe_inputs)
# ---------------------------------------------------------------------------
class TestEngineSnipeInputs(unittest.TestCase):
    """The engine must read each family from the oracle that actually resolves
    it, and must skip rather than substitute when that oracle has no opinion."""

    def setUp(self):
        import tempfile
        import yaml
        from polybot.config import Config
        from polybot.engine import Engine

        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        raw["storage"] = {"sqlite_path": str(Path(self._tmp.name) / "t.db"),
                          "fills_csv": str(Path(self._tmp.name) / "f.csv"),
                          "pnl_csv": str(Path(self._tmp.name) / "p.csv")}
        raw["logging"] = dict(raw["logging"], file=str(Path(self._tmp.name) / "t.log"))
        raw["families"]["5m"]["close_snipe"] = True     # exercise the path
        self.now = float(BASE + 250)
        self.engine = Engine(Config(raw=raw, path=cfg_path))
        self.snipe_cfg = self.engine.config.snipe_cfg

    def _market(self, family="5m", start=BASE, close=BASE + 300):
        import datetime as dt
        from polybot.polymarket import Market
        return Market(
            slug=f"btc-updown-{family}-{start}", family=family, question="", condition_id="",
            up_token_id="U", down_token_id="D",
            start_date=dt.datetime.fromtimestamp(start, tz=dt.timezone.utc),
            end_date=dt.datetime.fromtimestamp(close, tz=dt.timezone.utc),
            accepting_orders=True, closed=False, active=True, enable_order_book=True,
            order_min_size=5, tick_size=0.01, raw={})

    def _oracle(self, clock):
        o = ChainlinkOracle(cfg=cfg(), clock=clock)
        o._started = True                # never spawn threads in a test
        return o

    def test_engine_constructs_the_oracle_when_5m_is_on(self):
        self.assertIsNotNone(self.engine.chainlink)

    def test_returns_inputs_from_chainlink_for_a_5m_market(self):
        clock = FakeClock(t=self.now)
        o = self._oracle(clock)
        for i in range(0, 249):
            o.ingest_sample(BASE + i, int((65000 + i * 0.5) * WEI), "rtds")
        self.engine.chainlink = o
        out = self.engine._snipe_inputs(self._market(), self.now, self.snipe_cfg)
        self.assertIsNotNone(out)
        S_open, S_t, sigma = out
        self.assertAlmostEqual(S_open, 65000.0)          # strike = print at window open
        self.assertAlmostEqual(S_t, 65000 + 248 * 0.5)   # newest print
        self.assertGreater(sigma, 0.0)

    def test_skips_when_chainlink_is_stale(self):
        clock = FakeClock(t=self.now)
        o = self._oracle(clock)
        for i in range(0, 200):                       # newest print is 50s old
            o.ingest_sample(BASE + i, 65000 * WEI, "rtds")
        self.engine.chainlink = o
        self.assertIsNone(self.engine._snipe_inputs(self._market(), self.now, self.snipe_cfg))

    def test_skips_when_the_strike_is_missing(self):
        clock = FakeClock(t=self.now)
        o = self._oracle(clock)
        for i in range(100, 249):                     # buffer starts after the window open
            o.ingest_sample(BASE + i, 65000 * WEI, "rtds")
        self.engine.chainlink = o
        self.assertIsNone(self.engine._snipe_inputs(self._market(), self.now, self.snipe_cfg))

    def test_skips_when_no_chainlink_oracle_exists(self):
        self.engine.chainlink = None
        self.assertIsNone(self.engine._snipe_inputs(self._market(), self.now, self.snipe_cfg))

    def test_never_substitutes_binance_for_a_chainlink_family(self):
        """Even with a perfectly healthy Binance oracle, a dead Chainlink feed
        must produce no signal — Binance is wrong about the 5m resolution sign
        4.77% of the time (A1 §2d)."""
        from polybot.oracle import PricePoint

        class HealthyBinance:
            def latest(self):
                return PricePoint(ts=self.now_ref, price=65000.0)

            def hour_open_close(self, ts):
                return 64000.0, None

            def rolling_log_return_std(self, w):
                return 1e-4

            def price_at_or_before(self, ts):
                return 65000.0

        hb = HealthyBinance()
        hb.now_ref = self.now
        self.engine.binance = hb
        self.engine.chainlink = self._oracle(FakeClock(t=self.now))   # empty buffer
        self.assertIsNone(self.engine._snipe_inputs(self._market(), self.now, self.snipe_cfg))

    def test_1h_still_reads_binance(self):
        from polybot.oracle import PricePoint

        class StubBinance:
            def hour_open_close(self, ts):
                return 64000.0, None

            def latest(self_inner):
                return PricePoint(ts=self.now, price=65000.0)

            def rolling_log_return_std(self, w):
                return 1e-4

        self.engine.binance = StubBinance()
        out = self.engine._snipe_inputs(self._market(family="1h", close=BASE + 3600),
                                        self.now, self.snipe_cfg)
        self.assertIsNotNone(out)
        self.assertEqual(out[0], 64000.0)
        self.assertEqual(out[1], 65000.0)


if __name__ == "__main__":
    unittest.main()
