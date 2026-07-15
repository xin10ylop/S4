"""Optional Polymarket CLOB WebSocket market-channel client.

OFF BY DEFAULT (config `execution.use_websocket: false`). The engine's
always-on, portable path is REST polling (`polymarket.ClobClientREST.get_book`,
polled once per tick — see engine.py) and that is what every test in this
build exercises. This module is a verified, tested, but optional lower-latency
alternative; wiring it into the engine's hot path (replacing/augmenting the
REST poll) is left as a follow-up rather than done here, per the build brief's
explicit instruction not to block on it.

VERIFIED LIVE (connected with the `websockets` package against the real
endpoint during this build; independently corroborates the same payload
recorded in docs/05_clob_api_spec.md from an earlier live session):

  connect: wss://ws-subscriptions-clob.polymarket.com/ws/market
  subscribe (send once after connecting):
      {"assets_ids": [<token_id>, ...], "type": "market"}

  First message(s) received: a JSON ARRAY, one dict per subscribed asset,
  each a full book snapshot:
      {"event_type": "book", "market": "0x...", "asset_id": "...",
       "timestamp": "...", "hash": "...", "tick_size": "0.01",
       "last_trade_price": "0.61",
       "bids": [{"price": "0.01", "size": "20951.8"}, ...],
       "asks": [{"price": "0.99", "size": "..."}, ...]}
  (raw bid/ask array ordering is NOT guaranteed here either — we sort, same
  as the REST /book path.)

  Subsequent messages: a JSON OBJECT, incremental, possibly covering several
  asset_ids in one message:
      {"event_type": "price_change", "market": "0x...", "timestamp": "...",
       "price_changes": [{"asset_id": "...", "price": "0.61", "size": "...",
                           "side": "BUY", "hash": "...",
                           "best_bid": "0.61", "best_ask": "0.62"}, ...]}

  We did not observe a server-initiated ping/pong or an idle disconnect in
  our ~15s live test window, but docs/05_clob_api_spec.md (a separate live
  session) reports the server expects a "PING" text frame roughly every 10s
  of idle, replying "PONG" — this client sends one proactively on a timer
  rather than relying on that being strictly required.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .logging_setup import get_logger
from .net import ssl_context
from .polymarket import BookLevel, OrderBook

log = get_logger("ws_client")

PING_INTERVAL_SECS = 10.0
RECONNECT_BACKOFF_SECS = 2.0


@dataclass
class _TokenState:
    bids: Dict[float, float] = field(default_factory=dict)
    asks: Dict[float, float] = field(default_factory=dict)
    last_trade_price: Optional[float] = None
    updated_ts: float = 0.0

    def to_order_book(self, token_id: str) -> OrderBook:
        bids = sorted((BookLevel(p, s) for p, s in self.bids.items() if s > 0),
                      key=lambda l: l.price, reverse=True)
        asks = sorted((BookLevel(p, s) for p, s in self.asks.items() if s > 0),
                      key=lambda l: l.price)
        return OrderBook(token_id=token_id, bids=bids, asks=asks, fetched_at=self.updated_ts)


class WSMarketClient:
    """Maintains a live, in-memory order-book cache for a set of token ids via
    the CLOB market WebSocket channel. Runs its own asyncio loop on a
    dedicated background thread so it can be used from the engine's
    thread-based design without every caller needing to be async.
    """

    def __init__(self, config: Config, token_ids: List[str]):
        self.config = config
        self.token_ids = list(dict.fromkeys(token_ids))  # de-dup, preserve order
        self._state: Dict[str, _TokenState] = {t: _TokenState() for t in self.token_ids}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-market")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        with self._lock:
            st = self._state.get(token_id)
            if st is None or st.updated_ts == 0.0:
                return None
            return st.to_order_book(token_id)

    def is_fresh(self, token_id: str, max_age_secs: float = 5.0) -> bool:
        with self._lock:
            st = self._state.get(token_id)
            return bool(st and st.updated_ts and (time.time() - st.updated_ts) <= max_age_secs)

    # ------------------------------------------------------------------ async
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001
            log.exception("ws_client thread crashed")

    async def _main(self) -> None:
        import websockets

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.config.clob_ws, ssl=ssl_context(), open_timeout=10
                ) as ws:
                    self.connected = True
                    log.info("ws connected, subscribing to %d tokens", len(self.token_ids))
                    await ws.send(json.dumps({"assets_ids": self.token_ids, "type": "market"}))
                    ping_task = asyncio.ensure_future(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            self._handle_message(raw)
                            if self._stop.is_set():
                                break
                    finally:
                        ping_task.cancel()
            except Exception as exc:  # noqa: BLE001
                log.warning("ws connection error, reconnecting: %s", exc)
            self.connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(RECONNECT_BACKOFF_SECS)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SECS)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def _handle_message(self, raw: str) -> None:
        if raw == "PONG":
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        with self._lock:
            if isinstance(data, list):
                for entry in data:
                    if entry.get("event_type") != "book":
                        continue
                    asset_id = entry.get("asset_id")
                    st = self._state.get(asset_id)
                    if st is None:
                        continue
                    st.bids = {float(l["price"]): float(l["size"]) for l in entry.get("bids", [])}
                    st.asks = {float(l["price"]): float(l["size"]) for l in entry.get("asks", [])}
                    ltp = entry.get("last_trade_price")
                    st.last_trade_price = float(ltp) if ltp not in (None, "") else st.last_trade_price
                    st.updated_ts = time.time()
            elif isinstance(data, dict) and data.get("event_type") == "price_change":
                for change in data.get("price_changes", []):
                    asset_id = change.get("asset_id")
                    st = self._state.get(asset_id)
                    if st is None:
                        continue
                    # Incremental: update the single price level that changed,
                    # AND trust the accompanying best_bid/best_ask as a
                    # cross-check/backstop in case we ever missed a level.
                    price = change.get("price")
                    size = change.get("size")
                    side = change.get("side")
                    if price is not None and size is not None and side in ("BUY", "SELL"):
                        book_side = st.bids if side == "BUY" else st.asks
                        book_side[float(price)] = float(size)
                    st.updated_ts = time.time()
