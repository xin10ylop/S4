"""Main orchestration: discovery, oracle polling, signal evaluation, dispatch,
resolution — tied together on a set of background threads.

Design: a handful of lightweight threads (oracle poller, discovery poller,
1Hz decision tick, status writer, HTTP server) sharing a few thread-safe
collections (Ledger already locks internally; StatusState locks internally;
`self.markets` is swapped atomically so readers never see a half-built dict).
Latency-bound fill attempts (`execution.place_taker_buy`) run on a
ThreadPoolExecutor so the 1Hz tick loop is never blocked waiting for a
simulated 1.5s reaction latency.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Set

from .config import Config
from .execution import ExecutionRouter
from .fill_engine import FillAttempt
from .ledger import Ledger
from .logging_setup import get_logger
from .oracle import BinanceOracle, ChainlinkOracle
from .polymarket import ClobClientREST, GammaClient, Market, discover_markets
from .status_server import StatusState, make_server, write_status_loop
from .strategy import (evaluate_close_snipe, resolve_winner, settle_sweep_target,
                       snipe_tau_bounds)

log = get_logger("engine")


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.gamma = GammaClient(config)
        self.clob = ClobClientREST(config)
        self.binance = BinanceOracle(config)
        # The Chainlink oracle is only constructed when a family that resolves
        # on it actually has a strategy enabled. With the shipped config
        # (5m/15m/4h all off) this stays None and the running 1h bot behaves
        # exactly as before — wiring the feed and enabling trading are
        # deliberately separate steps. See audit/B2_impl_chainlink.md.
        self._chainlink_raw_cfg = config.chainlink_cfg
        self.chainlink: Optional[ChainlinkOracle] = None
        if self._chainlink_needed():
            try:
                self.chainlink = ChainlinkOracle(config)
            except Exception:  # noqa: BLE001
                log.exception("failed to construct ChainlinkOracle; chainlink families "
                              "will skip every tick")
        self.ledger = Ledger(config)
        self.status = StatusState(config, self.ledger)
        self.router = ExecutionRouter(config, self.clob)
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fill")

        self.known_markets: Dict[str, Market] = {}   # grows monotonically, used for scheduling/resolution
        self.markets: Dict[str, Market] = {}          # most recent discovery snapshot

        self.snipe_done: Set[str] = set()
        self.window_open_cache: Dict[str, float] = {}
        self.settle_winner_cache: Dict[str, object] = {}
        self.in_flight_settle: Set[str] = set()
        self.last_resolution_check: Dict[str, float] = {}
        self.settle_done: Set[str] = set()
        # consecutive empty/no-book settle attempts per market: once the book
        # has been bulk-cancelled post-close it does not repopulate (verified
        # live — see docs/05_clob_api_spec.md), so stop hammering after a few
        # consecutive empties instead of retrying for the whole window.
        self.settle_empty_streak: Dict[str, int] = {}

        self._stop = threading.Event()
        self._threads: list = []
        self._http_server = None

    def _chainlink_needed(self) -> bool:
        """True iff some enabled family declares `oracle: chainlink` AND has a
        strategy switched on for it."""
        for fam in self.config.families().values():
            if (fam.enabled and fam.oracle == "chainlink"
                    and (fam.close_snipe or fam.settle_sweep)):
                return True
        return False

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        log.info("starting engine: mode=%s", "PAPER" if self.config.paper else "LIVE")
        if not self.config.paper:
            log.warning("LIVE MODE IS ARMED — real orders may be placed")
        _tl, _th = snipe_tau_bounds(self.config.snipe_cfg,
                                    int(self.config.execution_cfg["latency_ms"]))
        log.info("close_snipe window: tau in [%.2f, %.2f]s (latency_ms=%s)",
                 _tl, _th, self.config.execution_cfg["latency_ms"])
        if _tl >= _th:
            log.warning("close_snipe window is EMPTY (tau_lo=%.2f >= tau_hi=%.2f) — the bot "
                        "will never fire a snipe. Check strategy.close_snipe.snipe_last_secs "
                        "vs execution.latency_ms.", _tl, _th)

        self._http_server = make_server(self.config.status_json_path, self.config.http_port)
        http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True,
                                        name="http")
        http_thread.start()
        self._threads.append(http_thread)
        log.info("status HTTP server listening on :%d (/status, /health)", self.config.http_port)

        status_thread = threading.Thread(
            target=write_status_loop,
            args=(self.status, self.config.status_json_path,
                  float(self.config.status_cfg.get("write_interval_secs", 3)), self._stop),
            daemon=True, name="status-writer",
        )
        status_thread.start()
        self._threads.append(status_thread)

        oracle_thread = threading.Thread(target=self._oracle_loop, daemon=True, name="oracle")
        oracle_thread.start()
        self._threads.append(oracle_thread)

        if self.chainlink is not None:
            # Push-based (websocket); start() spawns its own ingest threads.
            self.chainlink.start()
            log.info("chainlink oracle enabled for families: %s",
                     [f.name for f in self.config.families().values()
                      if f.oracle == "chainlink" and f.enabled
                      and (f.close_snipe or f.settle_sweep)])

        discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True,
                                             name="discovery")
        discovery_thread.start()
        self._threads.append(discovery_thread)

        # run one synchronous discovery pass before the tick loop starts so
        # `run()` for a short time still has markets to show/act on.
        self._do_discovery()

    def stop(self) -> None:
        log.info("stopping engine")
        self._stop.set()
        if self._http_server:
            self._http_server.shutdown()
        self.executor.shutdown(wait=False)
        self.ledger.close()

    def run_forever(self) -> None:
        self.start()
        try:
            self._tick_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # --------------------------------------------------------------- oracle
    def _oracle_loop(self) -> None:
        last_cl_log = 0.0
        while not self._stop.is_set():
            pt = self.binance.poll_once()
            if pt:
                self.status.set_oracle(pt.price, pt.ts)
            if self.chainlink is not None and time.time() - last_cl_log >= 60.0:
                last_cl_log = time.time()
                h = self.chainlink.health()
                log.info("chainlink health: %s", h)
                if not h["healthy"]:
                    self.status.add_event("oracle_stale",
                                          f"chainlink staleness={h['staleness_secs']}s "
                                          f"reconnects={h['n_reconnects']}")
            self._stop.wait(1.0)

    # ------------------------------------------------------------ discovery
    def _discovery_loop(self) -> None:
        poll_secs = float(self.config.discovery_cfg.get("poll_secs", 30))
        while not self._stop.is_set():
            self._stop.wait(poll_secs)
            if self._stop.is_set():
                break
            self._do_discovery()

    def _do_discovery(self) -> None:
        try:
            found = discover_markets(self.gamma, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("discovery failed: %s", exc)
            return
        new_slugs = [s for s in found if s not in self.known_markets]
        for s in new_slugs:
            m = found[s]
            log.info("discovered market: %s family=%s close=%s", s, m.family,
                      m.end_date.isoformat())
            self.status.add_event("discover", f"discovered {m.family} market {s}",
                                   family=m.family, slug=s)
            # Latency optimization for LIVE mode only (no-op in paper): warm
            # the py-clob-client tick_size/neg_risk cache now, well before
            # any signal, rather than at time-critical order-submission time.
            self.router.warm_cache(m.up_token_id)
            self.router.warm_cache(m.down_token_id)
        self.known_markets.update(found)
        self.markets = found
        self.status.last_discovery_ts = time.time()
        self._update_status_tracked()
        log.info("discovery: %d markets tracked (%d new)", len(found), len(new_slugs))

    def _update_status_tracked(self) -> None:
        now = time.time()
        out = {}
        for slug, m in self.markets.items():
            out[slug] = {
                "slug": slug, "family": m.family, "close_ts": m.close_ts,
                "close_in_secs": round(m.close_ts - now, 1),
                "accepting_orders": m.accepting_orders, "closed": m.closed,
            }
        self.status.set_tracked_markets(out)

    # ------------------------------------------------------------- main tick
    def _tick_loop(self) -> None:
        tick_secs = float(self.config.execution_cfg.get("order_book_poll_secs", 1.0))
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("tick failed")
            self._update_status_tracked()
            self.status.set_open_positions(self.ledger.all_open_positions())
            elapsed = time.time() - t0
            self._stop.wait(max(0.0, tick_secs - elapsed))

    def _tick(self) -> None:
        now = time.time()
        families = self.config.families()
        for slug, market in list(self.markets.items()):
            fam_cfg = families.get(market.family)
            if fam_cfg is None or not fam_cfg.enabled:
                continue
            if fam_cfg.close_snipe:
                self._maybe_snipe(market, now)
            if fam_cfg.settle_sweep:
                self._maybe_settle(market, now)
        self._check_resolutions(now)

    # ------------------------------------------------- oracle input selection
    def _snipe_inputs(self, market: Market, now: float, cfg: dict):
        """(S_open, S_t, sigma_1s) for this family's oracle, or None to skip.

        Each family reads from the oracle that actually resolves it: 1h from
        the Binance 1H candle, 5m/15m/4h from the Chainlink BTC/USD Data
        Stream. Returning None means "no trustworthy inputs this tick" and the
        caller skips — we never substitute one feed for the other, because
        Binance disagrees with Chainlink on the 5m resolution sign 4.77% of
        the time (audit/A1_chainlink.md §2d).
        """
        if market.family == "1h":
            S_open = self.window_open_cache.get(market.slug)
            if S_open is None:
                open_px, _ = self.binance.hour_open_close(market.window_start_ts)
                if open_px is None:
                    return None
                S_open = open_px
                self.window_open_cache[market.slug] = S_open
            latest = self.binance.latest()
            if latest is None or (now - latest.ts) > 5:
                return None  # oracle stale, skip this tick rather than trade on old data
            sigma_1s = self.binance.rolling_log_return_std(float(cfg["vol_window_secs"]))
            if sigma_1s != sigma_1s or sigma_1s <= 0:  # NaN check
                return None
            return S_open, latest.price, sigma_1s

        # ---- Chainlink families (5m / 15m / 4h) ----------------------------
        if self.chainlink is None:
            # Defensive: config should not enable close_snipe for a chainlink
            # family without the oracle, but never guess a price if it does.
            log.warning("close_snipe enabled for %s but no chainlink oracle is running",
                        market.family)
            return None

        # The strike is the FIRST print at-or-after the window open — exact,
        # and fixed for the life of the window, so cache it once resolved.
        S_open = self.window_open_cache.get(market.slug)
        if S_open is None:
            S_open = self.chainlink.strike(market.window_start_ts)
            if S_open is None:
                return None
            self.window_open_cache[market.slug] = S_open

        latest = self.chainlink.latest()   # None when staler than max_staleness_secs
        if latest is None or (now - latest.ts) > 5:
            return None
        S_t = latest.price
        if bool(self._chainlink_raw_cfg.get("hybrid_binance_drift", False)):
            # A1 §2e: Chainlink anchor + Binance drift across the ~1.4s publish
            # lag measured 97.12% vs 96.75% sign accuracy at k=1s on 5m. Purely
            # additive — if Binance is unavailable we fall back to the pure
            # (lagged) Chainlink print rather than skipping.
            b_now = self.binance.latest()
            b_then = self.binance.price_at_or_before(latest.ts)
            if b_now is not None and b_then is not None:
                S_t = latest.price + (b_now.price - b_then)
        sigma_1s = self.chainlink.rolling_log_return_std(float(cfg["vol_window_secs"]))
        if sigma_1s != sigma_1s or sigma_1s <= 0:  # NaN check
            return None
        return S_open, S_t, sigma_1s

    # --------------------------------------------------------- close_snipe
    def _maybe_snipe(self, market: Market, now: float) -> None:
        cfg = self.config.snipe_cfg
        tau_lo, tau_hi = snipe_tau_bounds(cfg, int(self.config.execution_cfg["latency_ms"]))
        tau = market.close_ts - now
        # tau_lo is NOT a "too late, give up" case we can log usefully — it is
        # simply outside the tradeable band, same as tau > tau_hi.
        if not (tau_lo <= tau <= tau_hi):
            return
        if market.slug in self.snipe_done:
            return

        inputs = self._snipe_inputs(market, now, cfg)
        if inputs is None:
            return
        S_open, S_t, sigma_1s = inputs

        book_up = self.clob.get_book(market.up_token_id)
        book_down = self.clob.get_book(market.down_token_id)

        sig = evaluate_close_snipe(market, now, S_t, S_open, sigma_1s, book_up, book_down,
                                    cfg, self.config.fee_rate)
        # Always log evaluation ticks inside the snipe window (max snipe_last
        # lines per window) — "evaluated, no edge" must be distinguishable
        # from "never ran" when auditing a live close.
        ask_u = book_up.best_ask.price if (book_up and book_up.best_ask) else None
        ask_d = book_down.best_ask.price if (book_down and book_down.best_ask) else None
        log.info("snipe eval %s tau=%.1fs S=%.2f S_open=%.2f sig1s=%.2e askU=%s askD=%s -> %s",
                 market.slug, tau, S_t, S_open, sigma_1s, ask_u, ask_d,
                 "SIGNAL" if sig else "no_edge")
        if sig is None:
            return

        self.snipe_done.add(market.slug)
        signal_id = self.ledger.record_snipe_signal(sig)
        log.info("SIGNAL close_snipe %s side=%s fair=%.4f ask=%.4f edge=%.4f tau=%.1fs",
                  market.slug, sig.side, sig.fair, sig.ask, sig.edge, sig.tau_secs)
        self.status.add_event("signal", f"close_snipe {market.slug} side={sig.side} "
                                         f"edge={sig.edge:.3f}", family=market.family)

        if self._would_exceed_global_cap():
            log.warning("skipping fill for %s: max_open_notional would be exceeded", market.slug)
            return

        book_at_signal = book_up if sig.side == "up" else book_down
        cap_usd = float(self.config.sizing_cfg["per_event_cap_usd"])
        latency_ms = int(self.config.execution_cfg["latency_ms"])
        price_min = float(cfg["price_min"])
        price_max = float(cfg["price_max"])
        edge_min = float(cfg["edge_min"])
        fair = sig.fair

        def edge_fn(p: float, _fair=fair) -> float:
            from .fill_engine import fee_per_share
            return _fair - p - fee_per_share(p, self.config.fee_rate)

        self.executor.submit(self._run_fill, "close_snipe", market, sig.side, sig.token_id,
                              edge_fn, edge_min, price_min, price_max, cap_usd, latency_ms,
                              signal_id, book_at_signal)

    # -------------------------------------------------------- settle_sweep
    def _maybe_settle(self, market: Market, now: float) -> None:
        cfg = self.config.settle_cfg
        delay = float(cfg["settle_delay_secs"])
        window = float(cfg["settle_window_secs"])
        t_since_close = now - market.close_ts
        if not (delay <= t_since_close <= window):
            return
        if market.slug in self.settle_done:
            return
        if market.slug in self.in_flight_settle:
            return
        # early stop: post-close book was observed empty repeatedly => it has
        # been bulk-cancelled and will not repopulate; stop re-attempting.
        if (t_since_close > 10
                and self.settle_empty_streak.get(market.slug, 0)
                >= int(cfg.get("empty_streak_stop", 5))):
            self.settle_done.add(market.slug)
            log.info("settle early-stop for %s: book empty %d consecutive attempts",
                     market.slug, self.settle_empty_streak.get(market.slug, 0))
            return

        wd = self.settle_winner_cache.get(market.slug)
        if wd is None:
            distance_guard = float(cfg["distance_guard_usd"])
            wd = resolve_winner(market, self.binance, distance_guard,
                                chainlink=self.chainlink)
            log.info("settle winner determination %s: %s (%s) open=%s close=%s",
                      market.slug, wd.winner, wd.reason, wd.s_open, wd.s_close)
            # Only cache a definitive winner or a PERMANENT ambiguity (the
            # underlying open/close prices are fixed historical values, so
            # "ambiguous within distance guard" will never change on retry).
            # A None winner from "data not yet available" is transient
            # (oracle indexing lag) and must be retried, not cached.
            if wd.winner is not None or wd.reason == "ambiguous_within_distance_guard":
                self.settle_winner_cache[market.slug] = wd
        if wd.winner is None:
            return  # ambiguous / unresolved this tick; will retry next tick until window closes

        # settle_sweep gets its OWN clip. `sizing.per_event_cap_usd` was raised to
        # $250 for close_snipe on close_snipe evidence only (audit/A4_change_spec.md
        # Part 2 iii); settle_sweep has never filled and must not silently inherit
        # a 10x clip if it is ever re-enabled. Falls back to the shared cap when
        # `strategy.settle_sweep.cap_usd` is absent (backward compatible).
        cap_usd = float(self.config.settle_cfg.get(
            "cap_usd", self.config.sizing_cfg["per_event_cap_usd"]))
        invested = self.ledger.strategy_cost_for_market(market.slug, "settle_sweep")
        remaining = cap_usd - invested
        if remaining <= 0.5:  # not worth another attempt
            self.settle_done.add(market.slug)
            return
        if self._would_exceed_global_cap():
            return

        token_id, edge_fn, edge_min, price_max = settle_sweep_target(market, wd.winner, cfg,
                                                                       self.config.fee_rate)
        signal_id = self.ledger.record_settle_signal(market.family, market.slug, wd.winner,
                                                       token_id, wd.reason)
        self.status.add_event("signal", f"settle_sweep {market.slug} winner={wd.winner}",
                               family=market.family)
        self.in_flight_settle.add(market.slug)
        latency_ms = int(self.config.execution_cfg["latency_ms"])
        self.executor.submit(self._run_fill, "settle_sweep", market, wd.winner, token_id,
                              edge_fn, edge_min, 0.0, price_max, remaining, latency_ms,
                              signal_id, None, self.in_flight_settle)

    def _would_exceed_global_cap(self) -> bool:
        max_notional = float(self.config.sizing_cfg["max_open_notional"])
        return self.ledger.total_open_notional() >= max_notional

    # ------------------------------------------------------------- fill worker
    def _run_fill(self, strategy: str, market: Market, side: str, token_id: str, edge_fn,
                  edge_min: float, price_min: float, price_max: float, cap_usd: float,
                  latency_ms: int, signal_id: Optional[int], book_at_signal,
                  in_flight_set: Optional[Set[str]] = None) -> None:
        try:
            attempt: FillAttempt = self.router.place_taker_buy(
                token_id, side, edge_fn, edge_min, price_min, price_max, cap_usd, latency_ms,
                book_at_signal=book_at_signal,
            )
            self.ledger.record_fill(strategy=strategy, family=market.family,
                                     market_slug=market.slug, signal_id=signal_id,
                                     attempt=attempt, edge_min=edge_min)
            if attempt.filled:
                self.status.add_event(
                    "fill", f"{strategy} {market.slug} side={side} shares="
                            f"{attempt.walk.total_shares:.2f} avg_px={attempt.walk.avg_price:.4f}",
                    family=market.family)
            else:
                self.status.add_event("fill_miss", f"{strategy} {market.slug} side={side} "
                                                     f"outcome={attempt.outcome}",
                                       family=market.family)
            if strategy == "settle_sweep":
                if attempt.outcome in ("empty_book", "no_book"):
                    self.settle_empty_streak[market.slug] = (
                        self.settle_empty_streak.get(market.slug, 0) + 1)
                else:
                    self.settle_empty_streak[market.slug] = 0
        except Exception:  # noqa: BLE001
            log.exception("fill worker failed for %s/%s", strategy, market.slug)
        finally:
            if in_flight_set is not None:
                in_flight_set.discard(market.slug)

    # -------------------------------------------------------------- resolution
    def _check_resolutions(self, now: float) -> None:
        poll_secs = float(self.config.resolution_cfg["gamma_poll_secs"])
        timeout_secs = float(self.config.resolution_cfg["resolution_timeout_mins"]) * 60
        for slug in self.ledger.unresolved_markets():
            market = self.known_markets.get(slug)
            if market is None:
                continue
            if now < market.close_ts:
                continue
            last_check = self.last_resolution_check.get(slug, 0.0)
            if now - last_check < poll_secs:
                continue
            self.last_resolution_check[slug] = now
            self._attempt_resolution(market, now, timeout_secs)

    def _attempt_resolution(self, market: Market, now: float, timeout_secs: float) -> None:
        raw = self.gamma.get_market_by_slug(market.slug, closed=True)
        gamma_winner = None
        gamma_closed = False
        if raw and raw.get("closed"):
            gamma_closed = True
            try:
                prices = json.loads(raw.get("outcomePrices", "[]"))
                if len(prices) == 2:
                    gamma_winner = "up" if float(prices[0]) >= float(prices[1]) else "down"
            except Exception:  # noqa: BLE001
                log.warning("could not parse outcomePrices for %s: %r", market.slug,
                            raw.get("outcomePrices"))

        distance_guard = float(self.config.settle_cfg["distance_guard_usd"])
        wd = self.settle_winner_cache.get(market.slug) or resolve_winner(
            market, self.binance, distance_guard, chainlink=self.chainlink)

        if gamma_closed and gamma_winner is not None:
            self.ledger.resolve_market(market.slug, market.family, market.close_ts, wd,
                                        gamma_winner, True, "gamma")
        elif now - market.close_ts > timeout_secs:
            if wd.winner is not None:
                log.warning("gamma unresolved after %ss for %s, falling back to oracle winner=%s",
                            timeout_secs, market.slug, wd.winner)
                self.ledger.resolve_market(market.slug, market.family, market.close_ts, wd,
                                            gamma_winner, gamma_closed, "oracle_fallback")
            else:
                log.warning("resolution timeout for %s and oracle winner is also ambiguous; "
                            "will keep retrying", market.slug)
