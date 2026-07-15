"""status.json writer + tiny stdlib HTTP server for /status and /health."""
from __future__ import annotations

import collections
import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .config import Config
from .ledger import Ledger
from .logging_setup import get_logger

log = get_logger("status")


class StatusState:
    """Shared, thread-safe mutable state the engine updates and the status
    writer/HTTP server read from."""

    def __init__(self, config: Config, ledger: Ledger):
        self.config = config
        self.ledger = ledger
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._tracked_markets: Dict[str, dict] = {}
        self._open_positions: List[dict] = []
        self._events: Deque[dict] = collections.deque(
            maxlen=int(config.status_cfg.get("recent_events_keep", 200))
        )
        self.last_discovery_ts: Optional[float] = None
        self.last_oracle_price: Optional[float] = None
        self.last_oracle_ts: Optional[float] = None

    def set_tracked_markets(self, markets: Dict[str, dict]) -> None:
        with self._lock:
            self._tracked_markets = markets

    def set_open_positions(self, positions: List[dict]) -> None:
        with self._lock:
            self._open_positions = positions

    def set_oracle(self, price: float, ts: float) -> None:
        with self._lock:
            self.last_oracle_price = price
            self.last_oracle_ts = ts

    def add_event(self, kind: str, message: str, **extra: Any) -> None:
        with self._lock:
            self._events.append({
                "ts": time.time(),
                "kind": kind,
                "message": message,
                **extra,
            })

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            tracked = list(self._tracked_markets.values())
            positions = list(self._open_positions)
            events = list(self._events)
            last_oracle_price = self.last_oracle_price
            last_oracle_ts = self.last_oracle_ts
        now = time.time()
        metrics = self.ledger.metrics()
        pnl_today = self.ledger.pnl_today()
        return {
            "generated_at": dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat(),
            "uptime_secs": round(now - self.started_at, 1),
            "mode": "LIVE" if not self.config.paper else "PAPER",
            "oracle": {
                "last_price": last_oracle_price,
                "last_update_secs_ago": (round(now - last_oracle_ts, 1)
                                          if last_oracle_ts else None),
            },
            "markets_tracked": tracked,
            "n_markets_tracked": len(tracked),
            "open_positions": positions,
            "n_open_positions": len(positions),
            "pnl": {
                "today": pnl_today,
                "cumulative_net": metrics["net_pnl"],
                "cumulative_gross": metrics["gross_pnl"],
                "fees_paid": metrics["fees_paid"],
            },
            "metrics": metrics,
            "recent_events": list(reversed(events))[:50],
        }


def write_status_loop(state: StatusState, path: Path, interval_secs: float,
                       stop_event: threading.Event) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        try:
            snap = state.snapshot()
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(snap, f, indent=2, default=str)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 - status writer must never crash the bot
            log.warning("status write failed: %s", exc)
        stop_event.wait(interval_secs)


class _Handler(BaseHTTPRequestHandler):
    status_path: Path = None  # set by make_server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path.rstrip("/") in ("", "/health"):
            self._respond(200, {"ok": True})
            return
        if self.path.rstrip("/") == "/status":
            try:
                with open(self.status_path) as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())
            except FileNotFoundError:
                self._respond(503, {"error": "status not yet written"})
            return
        self._respond(404, {"error": "not found"})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(status_path: Path, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"status_path": status_path})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    return server
