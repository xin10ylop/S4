"""Underlying-price oracles.

1h markets resolve on the Binance BTC/USDT 1H candle (close>=open => Up), so
Binance is both a *portable* and *authoritative* oracle for that family: it's
reachable from anywhere (via data-api.binance.vision) and matches the actual
resolution source.

Short families (5m/15m/4h) resolve on the Chainlink BTC/USD **Data Streams**
report (NOT the on-chain aggregator). That feed is now wired — see
`ChainlinkOracle` below. Provenance and every number quoted in that class are
from `audit/A1_chainlink.md`.

The two oracle classes expose the same read interface (`poll_once`, `latest`,
`price_at_or_before`, `rolling_log_return_std`, `price_at_second`) so the
engine can use either one for a family without branching on the read path.
"""
from __future__ import annotations

import collections
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from .config import Config
from .logging_setup import get_logger
from .net import get_session, ssl_context

log = get_logger("oracle")


@dataclass(frozen=True)
class PricePoint:
    ts: float  # unix seconds (float, from local receipt time)
    price: float


class BinanceOracle:
    """Maintains a 1-second-ish rolling series of BTC/USDT price via REST polling.

    Uses data-api.binance.vision, which is reachable from anywhere (unlike
    api.binance.com, which 451s in some sandboxes). This is a REST poller by
    design (simple, portable, testable here); an optional websocket client can
    be layered on later without changing the public interface.
    """

    def __init__(self, config: Config, symbol: str = "BTCUSDT", maxlen: int = 3700):
        self._base = config.binance_rest_base
        self._symbol = symbol
        self._session = get_session()
        self._series: Deque[PricePoint] = collections.deque(maxlen=maxlen)

    def fetch_price(self) -> Optional[float]:
        """One-shot fetch of the current BTC/USDT price. Returns None on error."""
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/ticker/price",
                params={"symbol": self._symbol},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data["price"])
        except Exception as exc:  # noqa: BLE001 - log and continue, oracle must not crash the bot
            log.warning("binance fetch_price failed: %s", exc)
            return None

    def poll_once(self) -> Optional[PricePoint]:
        """Fetch and append one price point to the rolling series."""
        px = self.fetch_price()
        if px is None:
            return None
        pt = PricePoint(ts=time.time(), price=px)
        self._series.append(pt)
        return pt

    def price_at_or_before(self, ts: float) -> Optional[float]:
        """Last known price at or before `ts` (backward fill), from the local series."""
        best: Optional[PricePoint] = None
        for pt in self._series:
            if pt.ts <= ts:
                if best is None or pt.ts > best.ts:
                    best = pt
        return best.price if best else None

    def latest(self) -> Optional[PricePoint]:
        return self._series[-1] if self._series else None

    def rolling_log_return_std(self, window_secs: float = 120.0) -> float:
        """Std of 1-step log returns over the trailing `window_secs`.

        Uses whatever points fall in the window (points are not guaranteed to
        be exactly 1s apart under REST polling jitter; this is an honest
        approximation of the "rolling std of 1s log-returns" spec). Returns
        NaN if there are too few points.
        """
        if len(self._series) < 3:
            return float("nan")
        now = self._series[-1].ts
        pts = [p for p in self._series if now - p.ts <= window_secs]
        if len(pts) < 3:
            return float("nan")
        rets = []
        for a, b in zip(pts, pts[1:]):
            if a.price > 0 and b.price > 0:
                rets.append(math.log(b.price / a.price))
        if len(rets) < 2:
            return float("nan")
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    def klines_1h(self, limit: int = 2) -> list:
        """Fetch recent 1H klines: [[open_time, open, high, low, close, ...], ...]."""
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={"symbol": self._symbol, "interval": "1h", "limit": limit},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance klines_1h failed: %s", exc)
            return []

    def price_at_second(self, ts: int) -> Optional[float]:
        """Authoritative Binance price at a specific unix second, via the 1s
        kline REST endpoint (works even if our local rolling series doesn't
        cover that second, e.g. bot started mid-window). Uses the kline's
        open price (price at the start of that second).
        """
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={
                    "symbol": self._symbol,
                    "interval": "1s",
                    "startTime": int(ts * 1000),
                    "limit": 1,
                },
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance price_at_second failed: %s", exc)
            return None
        if not rows:
            return None
        return float(rows[0][1])  # open price

    def hour_open_close(self, window_start_ts: int) -> Tuple[Optional[float], Optional[float]]:
        """Open/close for the 1H candle starting at window_start_ts (unix secs).

        Returns (open, close); close is None if the candle hasn't closed yet
        (candle still forming) — callers should treat that as "not resolved".
        """
        try:
            resp = self._session.get(
                f"{self._base}/api/v3/klines",
                params={
                    "symbol": self._symbol,
                    "interval": "1h",
                    "startTime": int(window_start_ts * 1000),
                    "limit": 1,
                },
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance hour_open_close failed: %s", exc)
            return None, None
        if not rows:
            return None, None
        row = rows[0]
        open_time_ms, open_px, high, low, close_px = row[0], row[1], row[2], row[3], row[4]
        close_time_ms = row[6]
        now_ms = time.time() * 1000
        candle_closed = now_ms >= close_time_ms
        return float(open_px), (float(close_px) if candle_closed else None)


# ---------------------------------------------------------------------------
# Chainlink BTC/USD Data Streams
# ---------------------------------------------------------------------------

# Chainlink mainnet BTC/USD Data Streams feed (Premium / CexPrice / v3 schema).
# Verified in audit/A1_chainlink.md by decoding a live signed report.
CHAINLINK_BTC_USD_FEED_ID = (
    "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8"
)
WEI = 10 ** 18


@dataclass(frozen=True)
class OracleSample:
    """One Chainlink Data Streams observation second.

    `wei` is the EXACT 18-decimal integer from the report — the resolution
    comparison (`settle >= strike`) must be done on these integers, never on
    the float, so a sub-wei difference can never be lost to float rounding.

    `received_at` is our local wall-clock time when the sample entered this
    process. It is what makes the "never return a print we could not yet have
    received" guarantee checkable: reads accept an `as_of` argument that
    filters on this field.

    `exact` is False for samples reconstructed from the RTDS connect snapshot,
    which carries only the float `value` (no `full_accuracy_value`). An exact
    sample for the same second always upgrades a non-exact one.
    """

    obs_sec: int
    wei: int
    received_at: float
    source: str
    exact: bool = True

    @property
    def price(self) -> float:
        return self.wei / WEI


class _WSTransport:
    """Minimal synchronous websocket transport around the `websockets` package.

    Isolated behind this tiny class purely so unit tests can inject a fake and
    exercise the full read loop with zero network. Anything with
    `send(str)` / `recv(timeout) -> str` / `close()` works.
    """

    def __init__(self, url: str, open_timeout: float = 10.0):
        from websockets.sync.client import connect as _sync_connect

        self._ws = _sync_connect(url, ssl=ssl_context(), open_timeout=open_timeout)

    def send(self, text: str) -> None:
        self._ws.send(text)

    def recv(self, timeout: float) -> str:
        return self._ws.recv(timeout=timeout)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


class ChainlinkOracle:
    """Chainlink BTC/USD **Data Streams** — the actual resolution source for
    the 5m / 15m / 4h Polymarket BTC Up/Down families.

    Transport
    ---------
    Primary is Polymarket's own public RTDS websocket, which rebroadcasts the
    signed Chainlink Data Streams BTC/USD report verbatim. No credentials, no
    contract, no key (Chainlink's own Data Streams API is commercially gated —
    it rejects unauthenticated calls with HTTP 400).

      wss://ws-live-data.polymarket.com
      topic  crypto_prices_chainlink
      filter {"symbol":"btc/usd"}

    Hot standby is the GMX oracle keeper, an independent operator that
    republishes the *signed* Data Streams report; we decode the blob and
    assert `feedId == CHAINLINK_BTC_USD_FEED_ID` before accepting anything.

    Why this is trustworthy (all measured in audit/A1_chainlink.md)
    --------------------------------------------------------------
      * RTDS `payload.full_accuracy_value` matched the signed Chainlink Data
        Streams report bit-for-bit at 18 decimals on 266/266 overlapping
        observation seconds.
      * `sign(settle - strike)` off this series reproduced Polymarket's
        on-chain `result_id` on 35,982/35,982 resolved 5m/15m/4h markets
        (100.00000%, zero disagreements, including all 254 markets that moved
        less than $0.50).
      * Binance, by contrast, gets the 5m resolution sign outright WRONG on
        4.77% of markets. That basis risk is why this class exists.

    Resolution semantics (recovered empirically, §2(b2) of A1)
    ----------------------------------------------------------
        strike = first report with observationsTimestamp >= window_start
        settle = first report with observationsTimestamp >= window_end
        Up wins iff settle >= strike        (compare the 18-dec INTEGERS)

    "First at-or-after" (backfill) beat forward-fill 99.59% vs 98.03% on
    1-second gaps. `price_at_second()` therefore implements at-or-after, which
    is the semantic analogue of BinanceOracle's "open price of that second".

    Publication lag — the thing this class must never lie about
    -----------------------------------------------------------
    The report for observation second T reaches us at T + ~1.4s (p50) /
    T + ~2.0s (p99). That lag is a hard floor set by Chainlink, not by us.
    Consequences, both enforced here:

      * `PricePoint.ts` returned by `latest()` is the **observation second**,
        not our receipt time. So `now - latest().ts` is a true measure of how
        old the underlying print is (healthy == ~1.4s), and the engine's
        existing `> 5s` staleness guard keeps working with the right meaning.
      * We only ever store a sample when it actually arrives over a wire. We
        never extrapolate, never synthesise a "current" second, and reject any
        sample stamped in the future (clock-skew / bad data guard). Reads take
        an optional `as_of` so a caller can additionally forbid samples that
        had not yet been received at a given instant.

    Degrading safely
    ----------------
    `latest()` returns **None** once the newest observation second is older
    than `max_staleness_secs` (default 4.0s = p99 publish lag 2.0s + margin).
    None means "no opinion" and every caller in this codebase skips the tick
    rather than trading. Coverage is ~96.7% of seconds even in perfect health
    (the DON does not emit every second and a second connection does not
    recover those gaps), so a missing second is normal and must not be filled
    by guessing.

    Threading
    ---------
    Ingest runs on daemon threads; all state is behind one lock. `poll_once()`
    is the interface-compatible pump the engine's oracle loop already calls and
    it lazily starts those threads, so the engine needs no special-casing.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        cfg: Optional[Dict[str, Any]] = None,
        transport_factory: Optional[Callable[[str], Any]] = None,
        http_get: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
    ):
        """`cfg` (a plain dict) overrides what is read from `config.yaml`; both
        `transport_factory` and `http_get` exist so tests can drive the real
        loops with a fake transport and no network.
        """
        raw = dict(cfg) if cfg is not None else (
            config.chainlink_cfg if config is not None else {}
        )
        self._cfg = raw
        self._clock = clock

        self.ws_url: str = raw.get("rtds_ws", "wss://ws-live-data.polymarket.com")
        self.topic: str = raw.get("topic", "crypto_prices_chainlink")
        self.symbol: str = raw.get("symbol", "btc/usd")
        self.feed_id: str = str(raw.get("feed_id", CHAINLINK_BTC_USD_FEED_ID)).lower()
        self.max_staleness_secs: float = float(raw.get("max_staleness_secs", 4.0))
        self.max_gap_secs: int = int(raw.get("max_gap_secs", 5))
        self.lookback_secs: int = int(raw.get("lookback_secs", 30))
        self._buffer_secs: int = int(raw.get("buffer_secs", 20_000))
        self._ping_interval: float = float(raw.get("ping_interval_secs", 5.0))
        self._recv_timeout: float = float(raw.get("recv_timeout_secs", 15.0))
        self._future_tolerance: float = float(raw.get("future_tolerance_secs", 2.0))

        standby = raw.get("standby") or {}
        self._standby_enabled: bool = bool(standby.get("enabled", True))
        self._standby_urls = [
            u for u in (standby.get("url"), standby.get("backup_url")) if u
        ] or [
            "https://arbitrum-api.gmxinfra.io/signed_prices/latest",
            "https://arbitrum-api.gmxinfra2.io/signed_prices/latest",
        ]
        # While RTDS is healthy the standby is only an integrity cross-check, so
        # it polls slowly. It accelerates only when RTDS has gone stale.
        self._standby_idle_secs: float = float(standby.get("idle_poll_secs", 30.0))
        self._standby_active_secs: float = float(standby.get("active_poll_secs", 0.5))

        self._transport_factory = transport_factory or (lambda url: _WSTransport(url))
        self._http_get = http_get or (lambda url, **kw: get_session().get(url, **kw))

        self._px: Dict[int, OracleSample] = {}
        self._newest_sec: Optional[int] = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list = []
        self._started = False

        # health counters (surfaced via health(), never used to fake a price)
        self.n_rtds = 0
        self.n_snapshot = 0
        self.n_standby = 0
        self.n_rejected_future = 0
        self.n_disagreements = 0
        self.n_reconnects = 0
        self.last_rx_at: float = 0.0
        self.last_error: Optional[str] = None
        self._standby_url_idx = 0
        self._last_standby_poll: float = 0.0

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        """Idempotent. Spawns the ingest threads."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._threads = [
            threading.Thread(target=self._ws_loop, daemon=True, name="chainlink-rtds"),
        ]
        if self._standby_enabled:
            self._threads.append(
                threading.Thread(target=self._standby_loop, daemon=True,
                                 name="chainlink-standby")
            )
        for t in self._threads:
            t.start()
        log.info("chainlink oracle started: %s topic=%s symbol=%s standby=%s",
                 self.ws_url, self.topic, self.symbol, self._standby_enabled)

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------------- ingest
    def ingest_sample(self, obs_sec: int, wei: int, source: str,
                      exact: bool = True, received_at: Optional[float] = None) -> bool:
        """Store one observation second. Returns True if it was stored.

        Rejects anything stamped in the future (we cannot possibly have
        received a print that has not been observed yet) and anything
        non-positive. An exact sample upgrades a snapshot-derived one for the
        same second; a genuine value disagreement between two sources is a
        loud error and the incumbent wins.
        """
        now = self._clock()
        obs_sec = int(obs_sec)
        wei = int(wei)
        if wei <= 0:
            return False
        if obs_sec > now + self._future_tolerance:
            self.n_rejected_future += 1
            log.warning("chainlink rejecting future-stamped print sec=%s now=%.2f src=%s",
                        obs_sec, now, source)
            return False
        sample = OracleSample(obs_sec=obs_sec, wei=wei,
                              received_at=received_at if received_at is not None else now,
                              source=source, exact=exact)
        with self._lock:
            prev = self._px.get(obs_sec)
            if prev is not None:
                if prev.wei == wei:
                    return False
                if prev.exact and not exact:
                    return False          # float snapshot must not clobber the exact report
                if prev.exact and exact:
                    self.n_disagreements += 1
                    log.error("chainlink DISAGREEMENT sec=%s %s=%s vs %s=%s (keeping first)",
                              obs_sec, prev.source, prev.wei, source, wei)
                    return False
                # prev was snapshot-derived and this one is exact -> upgrade
            self._px[obs_sec] = sample
            if self._newest_sec is None or obs_sec > self._newest_sec:
                self._newest_sec = obs_sec
            self.last_rx_at = now
            if source == "rtds":
                self.n_rtds += 1
            elif source == "rtds_snapshot":
                self.n_snapshot += 1
            else:
                self.n_standby += 1
            self._evict_locked()
        return True

    def _evict_locked(self) -> None:
        # amortised: only scan when we are meaningfully over budget
        if len(self._px) <= self._buffer_secs + 512 or self._newest_sec is None:
            return
        cutoff = self._newest_sec - self._buffer_secs
        for sec in [s for s in self._px if s < cutoff]:
            del self._px[sec]

    def ingest_message(self, raw: str, received_at: Optional[float] = None) -> int:
        """Parse one RTDS frame. Returns how many samples were stored.

        Two shapes are seen on this socket (both verified live):
          connect snapshot: {"payload": {"data": [{"timestamp": ms, "value": f}, ...]}}
                            — ~57s of history, float values only.
          update:           {"topic": "crypto_prices_chainlink", "type": "update",
                             "payload": {"full_accuracy_value": "<18dp int>",
                                         "symbol": "btc/usd",
                                         "timestamp": <ms>, "value": <float>}}
        Anything else (empty keepalive frames, other symbols, PONG) is ignored.
        """
        if not raw or not raw.strip() or raw.strip() in ("PONG", "PING"):
            return 0
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return 0
        if not isinstance(msg, dict):
            return 0
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return 0

        # ---- connect snapshot ------------------------------------------------
        data = payload.get("data")
        if isinstance(data, list):
            n = 0
            for row in data:
                if not isinstance(row, dict):
                    continue
                try:
                    sec = int(row["timestamp"]) // 1000
                    wei = int(round(float(row["value"]) * WEI))
                except (KeyError, TypeError, ValueError):
                    continue
                if self.ingest_sample(sec, wei, "rtds_snapshot", exact=False,
                                      received_at=received_at):
                    n += 1
            return n

        # ---- live update -----------------------------------------------------
        if payload.get("symbol") != self.symbol:
            return 0
        try:
            sec = int(payload["timestamp"]) // 1000
        except (KeyError, TypeError, ValueError):
            return 0
        fav = payload.get("full_accuracy_value")
        if fav not in (None, ""):
            try:
                wei, exact = int(fav), True
            except (TypeError, ValueError):
                return 0
        else:
            try:
                wei, exact = int(round(float(payload["value"]) * WEI)), False
            except (KeyError, TypeError, ValueError):
                return 0
        return 1 if self.ingest_sample(sec, wei, "rtds", exact=exact,
                                       received_at=received_at) else 0

    def _subscribe_frame(self) -> str:
        """Build the RTDS subscribe frame.

        The `filters` value is COMPACT JSON (no space after the colon) and that
        is load-bearing, not style: the server matches the filter string
        literally. Verified live 2026-07-27 on back-to-back connections —

            filters='{"symbol": "btc/usd"}'  -> snapshot delivered, 0 updates
            filters='{"symbol":"btc/usd"}'   -> snapshot delivered, 23 updates/25s

        `json.dumps` inserts that space by default, so a subscription built the
        obvious way silently degrades to a snapshot-only connection that then
        idles out. Keep `separators=(",", ":")`.
        """
        return json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": self.topic,
                "type": "update",
                "filters": json.dumps({"symbol": self.symbol}, separators=(",", ":")),
            }],
        }, separators=(",", ":"))

    def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = self._transport_factory(self.ws_url)
                ws.send(self._subscribe_frame())
                backoff = 1.0
                last_ping = self._clock()
                while not self._stop.is_set():
                    raw = ws.recv(self._recv_timeout)
                    self.ingest_message(raw)
                    if self._clock() - last_ping >= self._ping_interval:
                        ws.send("PING")   # documented RTDS keepalive
                        last_ping = self._clock()
            except Exception as exc:  # noqa: BLE001 - the oracle must never crash the bot
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.n_reconnects += 1
                log.warning("chainlink rtds reconnect in %.1fs (%s)", backoff, self.last_error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------- standby (GMX)
    def _standby_loop(self) -> None:
        while not self._stop.is_set():
            healthy = self.staleness() <= self.max_staleness_secs
            interval = self._standby_idle_secs if healthy else self._standby_active_secs
            if self._clock() - self._last_standby_poll >= interval:
                self._last_standby_poll = self._clock()
                try:
                    self.poll_standby_once()
                except Exception as exc:  # noqa: BLE001
                    log.debug("chainlink standby poll failed: %s", exc)
            self._stop.wait(0.2)

    def poll_standby_once(self) -> Optional[int]:
        """Fetch + decode the GMX-relayed signed Data Streams report.

        Returns the observation second stored, or None. Any report whose
        feedId is not the mainnet BTC/USD stream is discarded outright — we
        would rather have no price than a price from the wrong feed.
        """
        url = self._standby_urls[self._standby_url_idx % len(self._standby_urls)]
        try:
            resp = self._http_get(url, timeout=5)
            resp.raise_for_status()
            rows = resp.json().get("signedPrices", [])
        except Exception as exc:  # noqa: BLE001
            self._standby_url_idx += 1   # rotate to the backup host next time
            log.debug("chainlink standby fetch failed (%s): %s", url, exc)
            return None
        for row in rows:
            if row.get("tokenSymbol") != "BTC" or not row.get("blob"):
                continue
            decoded = decode_data_streams_blob(row["blob"])
            if decoded is None:
                continue
            feed_id, obs_sec, wei = decoded
            if feed_id.lower() != self.feed_id:
                continue
            self.ingest_sample(obs_sec, wei, "gmx_standby", exact=True)
            return obs_sec
        return None

    # ------------------------------------------------------------------ reads
    def _sample_at(self, sec: int, as_of: Optional[float]) -> Optional[OracleSample]:
        s = self._px.get(sec)
        if s is None:
            return None
        if as_of is not None and s.received_at > as_of:
            return None          # we had not received this print yet at `as_of`
        return s

    def poll_once(self) -> Optional[PricePoint]:
        """Interface-compatible pump for the engine's oracle loop.

        Ingest is push-based here, so this only (lazily) starts the ingest
        threads and reports the current fresh price — or None when stale, which
        is exactly what BinanceOracle.poll_once() returns on failure.
        """
        if not self._started:
            self.start()
        return self.latest()

    def latest(self, as_of: Optional[float] = None) -> Optional[PricePoint]:
        """Newest observation, or None if it is staler than
        `max_staleness_secs`. `ts` is the Chainlink observation second.

        Returning None on staleness is deliberate and is the primary safety
        property of this class: a stale oracle must produce no opinion, not an
        old opinion.
        """
        s = self.latest_raw(as_of=as_of)
        if s is None:
            return None
        ref = as_of if as_of is not None else self._clock()
        if ref - s.obs_sec > self.max_staleness_secs:
            return None
        return PricePoint(ts=float(s.obs_sec), price=s.price)

    def latest_raw(self, as_of: Optional[float] = None) -> Optional[OracleSample]:
        """Newest sample regardless of staleness — diagnostics only."""
        with self._lock:
            if self._newest_sec is None:
                return None
            if as_of is None:
                return self._px.get(self._newest_sec)
            best = None
            for s in self._px.values():
                if s.received_at <= as_of and (best is None or s.obs_sec > best.obs_sec):
                    best = s
            return best

    def staleness(self, as_of: Optional[float] = None) -> float:
        """Age in seconds of the newest print we hold. inf when we hold none.
        Healthy is ~1.4s (p50) / ~2.0s (p99)."""
        s = self.latest_raw(as_of=as_of)
        if s is None:
            return float("inf")
        ref = as_of if as_of is not None else self._clock()
        return ref - s.obs_sec

    def price_at_or_before(self, ts: float, as_of: Optional[float] = None) -> Optional[float]:
        """Last known price at or before `ts` (forward fill), bounded by
        `lookback_secs`. Same contract as BinanceOracle.price_at_or_before.

        Note: for *strike/settle* use `price_at_second`/`price_at_or_after` —
        Polymarket resolves on the first print at-or-AFTER a boundary.
        """
        start = int(ts)
        with self._lock:
            for sec in range(start, start - self.lookback_secs - 1, -1):
                s = self._sample_at(sec, as_of)
                if s is not None:
                    return s.price
        return None

    def price_at_or_after(self, ts: int, max_gap_secs: Optional[int] = None,
                          as_of: Optional[float] = None) -> Optional[float]:
        """Polymarket's rule: the FIRST print with observationsTimestamp >= ts.

        Verified 99.59% correct against `result_id` on 1-second gaps, vs
        98.03% for forward-fill (audit/A1_chainlink.md §2(b2)).
        Never searches past the newest second we hold, so this cannot invent a
        boundary price for a window that has not finished publishing yet.
        """
        s = self.sample_at_or_after(ts, max_gap_secs=max_gap_secs, as_of=as_of)
        return s.price if s is not None else None

    def sample_at_or_after(self, ts: int, max_gap_secs: Optional[int] = None,
                           as_of: Optional[float] = None) -> Optional[OracleSample]:
        gap = self.max_gap_secs if max_gap_secs is None else int(max_gap_secs)
        start = int(ts)
        with self._lock:
            newest = self._newest_sec
            if newest is None:
                return None
            for sec in range(start, start + gap + 1):
                if sec > newest:
                    return None
                s = self._sample_at(sec, as_of)
                if s is not None:
                    return s
        return None

    def price_at_second(self, ts: int) -> Optional[float]:
        """Interface-compatible with BinanceOracle.price_at_second.

        BinanceOracle answers this with a REST call for that exact second.
        Chainlink Data Streams has NO public REST backfill (verified: gamma
        /prices/crypto 404s, live-data over HTTP 426s), so this is answered
        from our own rolling buffer using Polymarket's at-or-after rule — the
        semantic analogue of "the price at that second" for this feed. Returns
        None if the second is outside the buffer, which callers must treat as
        "unknown", never as zero/stale.
        """
        return self.price_at_or_after(int(ts))

    def strike(self, window_start_ts: int) -> Optional[float]:
        return self.price_at_or_after(int(window_start_ts))

    def settle(self, close_ts: int) -> Optional[float]:
        return self.price_at_or_after(int(close_ts))

    def winner(self, window_start_ts: int, close_ts: int) -> Optional[str]:
        """Exact resolution: Up iff settle >= strike, compared as 18-decimal
        INTEGERS so no float rounding can flip a near-tie. None => we do not
        hold both boundary prints and the caller must skip.
        """
        o = self.sample_at_or_after(int(window_start_ts))
        c = self.sample_at_or_after(int(close_ts))
        if o is None or c is None:
            return None
        return "up" if c.wei >= o.wei else "down"

    def rolling_log_return_std(self, window_secs: float = 120.0) -> float:
        """Std of 1s log returns over the trailing `window_secs`, on the
        observation-second grid. NaN if there are too few points.

        CALIBRATION WARNING: this feed moves nearly every second (only 4.66% of
        consecutive prints repeat) whereas Binance 1s klines are mostly flat,
        so `strategy.close_snipe.sigma_1s_floor` (tuned for Binance) is NOT
        the right floor here and must be re-derived from Chainlink data before
        any Chainlink family is enabled. See audit/B2_impl_chainlink.md.
        """
        with self._lock:
            newest = self._newest_sec
            if newest is None:
                return float("nan")
            oldest = int(newest - window_secs)
            prices = [self._px[s].price for s in range(oldest, newest + 1) if s in self._px]
        if len(prices) < 3:
            return float("nan")
        rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
        if len(rets) < 2:
            return float("nan")
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    # ----------------------------------------------------------------- health
    def coverage(self, window_secs: int = 300) -> float:
        """Fraction of the last `window_secs` observation seconds we hold.
        ~0.967 is healthy; the missing ~3% are upstream DON gaps."""
        with self._lock:
            newest = self._newest_sec
            if newest is None:
                return 0.0
            have = sum(1 for s in range(newest - window_secs + 1, newest + 1) if s in self._px)
        return have / float(window_secs)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._px)
            newest = self._newest_sec
        st = self.staleness()
        return {
            "started": self._started,
            "samples_buffered": n,
            "newest_obs_sec": newest,
            "staleness_secs": None if st == float("inf") else round(st, 3),
            "healthy": st <= self.max_staleness_secs,
            "coverage_300s": round(self.coverage(300), 4),
            "n_rtds": self.n_rtds,
            "n_snapshot": self.n_snapshot,
            "n_standby": self.n_standby,
            "n_disagreements": self.n_disagreements,
            "n_rejected_future": self.n_rejected_future,
            "n_reconnects": self.n_reconnects,
            "last_error": self.last_error,
        }


def decode_data_streams_blob(blob: str) -> Optional[Tuple[str, int, int]]:
    """Decode a GMX-relayed Chainlink Data Streams v3 report.

    Outer envelope is `abi.encode(bytes32[3], bytes, bytes32[], bytes32[], bytes32)`;
    the inner `bytes` is the v3 report, nine static 32-byte words:
      feedId(bytes32) validFrom(uint32) observationsTimestamp(uint32)
      nativeFee(uint192) linkFee(uint192) expiresAt(uint32)
      price(int192) bid(int192) ask(int192)

    Returns `(feed_id_hex, observations_timestamp, price_wei)` or None if the
    blob is malformed. Hand-rolled on purpose: every field here is a single
    static word, so this needs no web3/eth-abi dependency in the bot.
    """
    try:
        raw = bytes.fromhex(blob[2:] if blob.startswith("0x") else blob)
        if len(raw) < 7 * 32:
            return None
        report_off = int.from_bytes(raw[3 * 32:4 * 32], "big")
        report_len = int.from_bytes(raw[report_off:report_off + 32], "big")
        rep = raw[report_off + 32:report_off + 32 + report_len]
        if len(rep) < 9 * 32:
            return None
        feed_id = "0x" + rep[0:32].hex()
        obs_sec = int.from_bytes(rep[2 * 32:3 * 32], "big")
        price = int.from_bytes(rep[6 * 32 + 8:7 * 32], "big")   # int192, right-aligned
        if price >= 1 << 191:
            price -= 1 << 192
        if price <= 0 or obs_sec <= 0:
            return None
        return feed_id, obs_sec, price
    except Exception:  # noqa: BLE001
        return None


def read_onchain_aggregator(rpc_url: str, feed_address: str,
                            http_post: Optional[Callable[..., Any]] = None,
                            timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """LIVENESS / SANITY CHECK ONLY — never a signal source.

    Raw `eth_call` of `latestRoundData()` (selector 0xfeaf968c) on a Chainlink
    on-chain aggregator, via plain JSON-RPC so the bot needs no web3
    dependency. The Polygon BTC/USD aggregator updates every **33.8s on
    average and never faster than 21s** (measured over 220 consecutive rounds),
    so it CANNOT be the 1s resolution series and must never be used to price a
    5m window. Its only job is to catch "our stream has silently gone wrong".
    """
    post = http_post or (lambda url, **kw: get_session().post(url, **kw))
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": feed_address, "data": "0xfeaf968c"}, "latest"]}
    try:
        resp = post(rpc_url, json=body, timeout=timeout)
        resp.raise_for_status()
        result = resp.json().get("result")
        if not result or len(result) < 2 + 5 * 64:
            return None
        raw = bytes.fromhex(result[2:])
        answer = int.from_bytes(raw[32:64], "big")
        if answer >= 1 << 255:
            answer -= 1 << 256
        updated_at = int.from_bytes(raw[96:128], "big")
    except Exception as exc:  # noqa: BLE001
        log.debug("onchain aggregator read failed: %s", exc)
        return None
    return {
        "answer_raw": answer,
        "price": answer / 1e8,          # aggregator decimals = 8
        "updated_at": updated_at,
        "age_secs": time.time() - updated_at,
    }


def normal_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (avoids a hard scipy dependency at import time)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
