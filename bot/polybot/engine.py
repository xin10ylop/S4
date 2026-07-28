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
from .depth import DepthTracker, max_level_shares
from .execution import ExecutionRouter
from .fill_engine import FillAttempt
from .ledger import Ledger
from .logging_setup import get_logger
from .oracle import BinanceOracle, ChainlinkOracle, coin_feed
from .polymarket import ClobClientREST, GammaClient, Market, OrderBook, discover_markets
from .risk import CircuitBreaker, WarmupGate
from .status_server import StatusState, make_server, write_status_loop
from .strategy import (evaluate_close_snipe, resolve_winner, settle_sweep_target,
                       snipe_tau_bounds)

log = get_logger("engine")


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.gamma = GammaClient(config)
        self.clob = ClobClientREST(config)
        # `self.binance` is BTC/USDT spot: the shipped, validated, only
        # profitable oracle. It stays a first-class attribute (not just a dict
        # entry) because everything about bitcoin must keep working byte-for-
        # byte after the multi-coin refactor.
        self.binance = BinanceOracle(config)
        # M5: one oracle per NON-bitcoin coin we are allowed to price, built
        # from the verified `coin -> (symbol, venue)` table in oracle.py. A coin
        # with no entry here gets NO oracle and therefore never trades; see
        # `_oracle_for`, which returns None rather than substituting BTC.
        self.coin_oracles: Dict[str, BinanceOracle] = {}
        self._build_coin_oracles()
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

        # ---- M4 risk guards -------------------------------------------------
        # 1) adverse-size filter: rolling per-family depth reference. The
        #    tracker is always built (so the statistic is observable in
        #    status.json even with the filter off); whether it BINDS is decided
        #    by depth.max_level_shares from strategy.close_snipe.adverse_size.
        self._adverse_cfg = dict(self.config.adverse_size_cfg)
        self.depth = DepthTracker(
            history_n=int(self._adverse_cfg.get("history_n", 200)),
            min_samples=int(self._adverse_cfg.get("min_samples", 30)),
        )
        # 2) warmup after restart: `started_at` is process start, so a deploy /
        #    watchdog restart resets it — which is exactly the point.
        self.warmup = WarmupGate(self.config.warmup_cfg)
        # 3) daily loss limit + consecutive-loss brake
        self.breaker = CircuitBreaker(self.config.risk_cfg, self.ledger,
                                       override_path=self.config.risk_override_path)

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
        self._snipe_family_warned: Set[str] = set()
        self._snipe_coin_warned: Set[str] = set()
        self._breaker_blocked_slugs: Set[str] = set()
        self._shadow_logged: Set[str] = set()

        self._stop = threading.Event()
        self._threads: list = []
        self._http_server = None

    # ------------------------------------------------------- per-coin oracles
    def _priced_coins(self) -> list:
        """Coins whose underlying we must poll: everything we may fill plus
        everything we shadow-evaluate. Discovery breadth alone does NOT create
        an oracle — a discovered-but-unpriced coin simply never produces a
        signal."""
        seen, out = set(), []
        for c in list(self.config.allowed_coins()) + list(self.config.shadow_coins()):
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _build_coin_oracles(self) -> None:
        """Construct one BinanceOracle per priced non-bitcoin coin. FAILS CLOSED:
        a coin missing from oracle.HOURLY_COIN_FEEDS, or whose oracle cannot be
        constructed, is left with no oracle and can never produce a signal."""
        for coin in self._priced_coins():
            if coin == "bitcoin":
                continue  # served by self.binance
            feed = coin_feed(coin)
            if feed is None:
                log.error("coin %r is configured for pricing but has NO verified feed in "
                          "oracle.HOURLY_COIN_FEEDS — it will never trade. This is the "
                          "fail-closed path; add a verified entry or remove the coin.", coin)
                continue
            symbol, venue = feed
            try:
                self.coin_oracles[coin] = BinanceOracle(self.config, symbol=symbol,
                                                         venue=venue)
            except Exception:  # noqa: BLE001
                log.exception("failed to construct oracle for %s (%s@%s); it will never "
                              "trade", coin, symbol, venue)

    def _binance_oracles(self) -> Dict[str, BinanceOracle]:
        """coin -> oracle for every Binance-backed feed the engine polls."""
        out: Dict[str, BinanceOracle] = {"bitcoin": self.binance}
        out.update(self.coin_oracles)
        return out

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
        # M4 guards: announce the configured state once, loudly, at boot. A
        # guard nobody can see in the log is a guard nobody can verify.
        log.info("M4 guard 1 adverse_size: enabled=%s mode=%s max_size_ratio=%s "
                 "history_n=%s min_samples=%s",
                 self._adverse_cfg.get("enabled"), self._adverse_cfg.get("mode"),
                 self._adverse_cfg.get("max_size_ratio"),
                 self._adverse_cfg.get("history_n"), self._adverse_cfg.get("min_samples"))
        log.info("M4 guard 2 warmup: enabled=%s min_oracle_samples=%d min_uptime_secs=%.0f "
                 "(vol_window=%.0fs)", self.warmup.enabled, self.warmup.min_samples,
                 self.warmup.min_uptime_secs, float(self.config.snipe_cfg["vol_window_secs"]))
        # M5: the coin wiring is safety-critical, so it is announced explicitly
        # — which coins are discovered, which may fill, which are shadow-only,
        # and the exact symbol/venue each one is priced off.
        log.info("M5 coins: discover=%s allowed(fill)=%s shadow(no fill)=%s",
                 self.config.hourly_coins(), self.config.allowed_coins(),
                 self.config.shadow_coins())
        for coin, oracle in self._binance_oracles().items():
            log.info("M5 oracle: coin=%-9s symbol=%-9s venue=%-13s cap_usd=%.0f may_fill=%s",
                     coin, oracle.symbol, oracle.venue, self.config.coin_cap_usd(coin),
                     coin in self.config.allowed_coins()
                     and coin not in self.config.shadow_coins())
        for coin in self._priced_coins():
            if coin != "bitcoin" and coin not in self.coin_oracles:
                log.error("M5 oracle MISSING for coin=%s — it is configured for pricing but "
                          "has no feed; it can never signal. FAIL-CLOSED.", coin)
        log.info("M5 guard 4 stale-book: max_book_age_s=%s",
                 self.config.max_book_age_s if self.config.max_book_age_s else "OFF")
        log.info("M4 guard 3 circuit breaker: daily_limit=%s consecutive_loss_brake=%s "
                 "override_path=%s",
                 (f"${self.breaker.daily_limit:.2f}"
                  if (self.breaker.daily_enabled and self.breaker.daily_limit) else "OFF"),
                 (self.breaker.max_streak if self.breaker.streak_enabled else "OFF"),
                 self.config.risk_override_path)
        _boot = self.breaker.evaluate()
        if _boot.tripped:
            log.error("CIRCUIT BREAKER IS ALREADY TRIPPED AT BOOT: %s — the bot will run "
                      "and track markets but will not open positions until UTC midnight or "
                      "`python -m polybot.main resume`.", "; ".join(_boot.reasons))

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
    def _poll_binance_oracles(self, pool: Optional[ThreadPoolExecutor]) -> None:
        """Poll every coin oracle for one sample.

        CONCURRENTLY, and this matters more than it looks. M4 §open-item-3
        measured the single-oracle poll rate at 0.642-0.825 polls/s and flagged
        that the warmup gate (60 samples in a 120 s window) has only ~22%
        headroom at the slow end. Polling N coins SEQUENTIALLY would multiply
        the period by N and deadlock the warmup gate at N >= 2 — the bot would
        sit "warming up" forever and silently never trade, which M4 identified
        as a worse failure than the one being guarded. Fanning out keeps the
        period at ~max(latency) instead of ~sum(latency).
        """
        oracles = self._binance_oracles()

        def _one(item):
            coin, oracle = item
            try:
                return coin, oracle.poll_once()
            except Exception:  # noqa: BLE001 - one bad feed must not stop the others
                log.exception("oracle poll failed for %s", coin)
                return coin, None

        if pool is not None and len(oracles) > 1:
            results = list(pool.map(_one, list(oracles.items())))
        else:
            results = [_one(it) for it in oracles.items()]
        for coin, pt in results:
            # status.oracle is the BITCOIN price: it is what the shipped
            # strategy trades and what every existing dashboard/alert reads.
            if coin == "bitcoin" and pt:
                self.status.set_oracle(pt.price, pt.ts)

    def _oracle_loop(self) -> None:
        last_cl_log = 0.0
        n_oracles = max(1, len(self._binance_oracles()))
        pool = (ThreadPoolExecutor(max_workers=n_oracles, thread_name_prefix="oracle")
                if n_oracles > 1 else None)
        # Rate-COMPENSATING sleep: the old loop slept a flat 1.0 s AFTER the
        # fetch, so the true period was 1.0 s + latency (the measured
        # 0.642-0.825 polls/s). Subtracting the elapsed time targets a real 1 Hz
        # and lifts warmup headroom from ~22% to ~100% at the same threshold.
        # Floor of 0.2 s so a pathological feed cannot become a hot loop.
        target = 1.0
        try:
            while not self._stop.is_set():
                t0 = time.time()
                self._poll_binance_oracles(pool)
                if self.chainlink is not None and time.time() - last_cl_log >= 60.0:
                    last_cl_log = time.time()
                    h = self.chainlink.health()
                    log.info("chainlink health: %s", h)
                    if not h["healthy"]:
                        self.status.add_event("oracle_stale",
                                              f"chainlink staleness={h['staleness_secs']}s "
                                              f"reconnects={h['n_reconnects']}")
                self._stop.wait(max(0.2, target - (time.time() - t0)))
        finally:
            if pool is not None:
                pool.shutdown(wait=False)

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
            self._publish_guard_status()
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
    def _oracle_for(self, market: Market):
        """The oracle that actually feeds this market's sigma_1s, or None.

        FAILS CLOSED BY CONSTRUCTION. Returning None means "no trustworthy
        price for this market"; WarmupGate then reads 0 samples and refuses to
        trade, and `_snipe_inputs` skips. There is deliberately no default and
        no fallback branch: a coin priced off another coin's underlying would
        raise no error, produce confident-looking signals, and lose — the exact
        silent-catastrophe path M1 §6 item 3 and M3 §13 item 2 both list as
        blocking for any second coin.
        """
        if market.family != "1h":
            return self.chainlink
        coin = getattr(market, "coin", None)
        if coin == "bitcoin":
            # Read through the attribute, not the map, so `self.binance` stays
            # the single authoritative BTC oracle.
            return self.binance
        if not coin:
            return None
        return self.coin_oracles.get(coin)

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
            # Route strictly by coin. `_oracle_for` returns None for any coin
            # without a verified feed, and we skip rather than guess.
            oracle = self._oracle_for(market)
            if oracle is None:
                log.warning("no oracle for %s (coin=%r) — refusing to price it",
                            market.slug, getattr(market, "coin", None))
                return None
            S_open = self.window_open_cache.get(market.slug)
            if S_open is None:
                open_px, _ = oracle.hour_open_close(market.window_start_ts)
                if open_px is None:
                    return None
                S_open = open_px
                self.window_open_cache[market.slug] = S_open
            latest = oracle.latest()
            if latest is None or (now - latest.ts) > 5:
                return None  # oracle stale, skip this tick rather than trade on old data
            sigma_1s = oracle.rolling_log_return_std(float(cfg["vol_window_secs"]))
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

    # ------------------------------------------------------------ book access
    def _fetch_books(self, token_ids: list) -> Dict[str, OrderBook]:
        """Fetch several books in ONE round trip, falling back per token.

        Measured live 2026-07-28 (data/multicoin/book_latency.csv): a batched
        `POST /books` costs the same as a single `GET /book` regardless of how
        many tokens are asked for (155 ms for 2, 157 ms for 14), while 14
        sequential GETs cost 2,241 ms — longer than the entire 2.5-5.0 s snipe
        band. Two sequential GETs (what this used to do) cost ~308 ms; one batch
        costs ~155 ms, so the margin between the gate and the close grows by
        ~0.15 s and every additional coin is free.

        Falls back to per-token `GET /book` for anything the batch did not
        answer, including a total transport failure and a client that has no
        batch method at all. A missing book must never be silently read as an
        empty one.
        """
        if not token_ids:
            return {}
        out: Dict[str, OrderBook] = {}
        batch = getattr(self.clob, "get_books", None)
        if callable(batch) and len(token_ids) > 1:
            try:
                out = dict(batch(token_ids) or {})
            except Exception:  # noqa: BLE001 - never let the batch path kill a tick
                log.exception("batched book fetch raised; falling back to per-token GET")
                out = {}
        for tid in token_ids:
            if out.get(tid) is None:
                out[tid] = self.clob.get_book(tid)
        return out

    def _book_too_stale(self, book: Optional[OrderBook],
                        now: Optional[float] = None) -> Optional[float]:
        """Book age in seconds if it breaches `max_book_age_s`, else None.

        WHY THIS EXISTS. The shipped backtest ran with no staleness filter at
        all. The verifier measured what that costs per coin: BTC's worst fill
        book was 2.2 s old (median 0.30 s) so the guard would never have bound
        on the only coin that trades — but HYPE drew 93.6% of its P&L from
        fills against books over 5 s old and BNB 46.5% of its (negative) P&L,
        and applying a <= 5 s filter lifted the pooled non-BTC result from
        +2.01c to +3.70c per share. So this is a no-op on what is validated and
        a real guard on anything new. M1 §3.2 ranks p90 book age BTC 1.9 s /
        ETH 1.93 s / XRP 4.37 s / DOGE 4.99 s / SOL 6.97 s / BNB 9.14 s /
        HYPE 87.71 s, which is the same ordering as their measured edge.

        FAILS OPEN on an unmeasurable book (`book_ts is None`), and that choice
        is deliberate rather than lazy: the age comes from the CLOB's own
        `timestamp` field, so treating "field absent" as "too stale" would let
        an upstream schema change silently stop all trading. The absence is
        logged once per occurrence instead.
        """
        limit = self.config.max_book_age_s
        if limit is None or book is None:
            return None
        age = book.age_secs(now)
        if age is None:
            log.warning("book %s carries no CLOB timestamp — age guard cannot apply "
                        "(failing OPEN)", book.token_id)
            return None
        return age if age > limit else None

    # --------------------------------------------------------- close_snipe
    def _maybe_snipe(self, market: Market, now: float) -> None:
        cfg = self.config.snipe_cfg
        # SAFETY ALLOWLIST (restores the guard deleted in the tau-band refactor;
        # see docs/07_scale_audit.md item 4). Without this, flipping one YAML
        # boolean arms 5m/15m/4h at the $250 clip on 1h-calibrated edge_min and
        # window, which the verifier showed loses money on 5m (-$58/day at $250).
        # The families here have been validated for close_snipe; adding one is a
        # deliberate act that must follow the go/no-go in docs/07 sec. 6.
        allowed = cfg.get("allowed_families", ["1h"])
        if market.family not in allowed:
            if market.slug not in self._snipe_family_warned:
                self._snipe_family_warned.add(market.slug)
                log.warning("close_snipe is enabled for family %s in config but %s is NOT in "
                            "strategy.close_snipe.allowed_families=%s — refusing to trade it. "
                            "See docs/07_scale_audit.md before adding it.",
                            market.family, market.family, allowed)
            return

        # ---- M5 COIN ALLOWLIST (second, independent of the family allowlist).
        # `allowed_families` is a FAMILY allowlist; on its own, widening
        # _HOURLY_RE to seven coins would silently widen what trades (M1 §6
        # item 4). A coin must be in `allowed_coins` (fills) or `shadow_coins`
        # (evaluate + log, never fill) to get any further.
        coin = getattr(market, "coin", None)
        may_fill = coin in self.config.allowed_coins()
        is_shadow = coin in self.config.shadow_coins()
        if not (may_fill or is_shadow):
            if market.slug not in self._snipe_coin_warned:
                self._snipe_coin_warned.add(market.slug)
                log.warning("close_snipe: coin %r (%s) is NOT in "
                            "strategy.close_snipe.allowed_coins=%s nor shadow_coins=%s — "
                            "refusing to trade it. See docs/09_multicoin_decision.md.",
                            coin, market.slug, self.config.allowed_coins(),
                            self.config.shadow_coins())
            return
        # A shadow coin that also happens to be allowed is resolved to SHADOW by
        # Config.shadow_coins(); re-assert it here so the safe reading is local
        # and cannot be lost by a future edit to either list.
        if is_shadow:
            may_fill = False

        tau_lo, tau_hi = snipe_tau_bounds(cfg, int(self.config.execution_cfg["latency_ms"]))
        tau = market.close_ts - now
        # tau_lo is NOT a "too late, give up" case we can log usefully — it is
        # simply outside the tradeable band, same as tau > tau_hi.
        if not (tau_lo <= tau <= tau_hi):
            return
        if market.slug in self.snipe_done:
            return

        # ---- M4 guard 2: WARMUP. Checked BEFORE any book fetch so a
        # just-restarted process also stops burning REST calls it cannot act
        # on. `sigma_1s` from a thin buffer reads too small, which inflates
        # |z| and therefore `fair` — see risk.WarmupGate.
        wu = self.warmup.check(self._oracle_for(market), float(cfg["vol_window_secs"]), now)
        self.warmup.log_progress(wu, market.family, now)
        if not wu.ready:
            return

        # ---- M4 guard 3: CIRCUIT BREAKER. Also checked before the book
        # fetches; re-checked just before the fill is dispatched, because a
        # resolution can land in between.
        # Deliberately evaluated on the WALL clock, not the tick's `now`: the
        # breaker reasons about UTC days and realized PnL, which are wall-clock
        # facts. Passing a market-derived timestamp here would make the
        # UTC-day-scoped override compare against the wrong day.
        br = self.breaker.allow_new_position()
        if br.tripped:
            if market.slug not in self._breaker_blocked_slugs:
                self._breaker_blocked_slugs.add(market.slug)
                self.status.add_event("risk_block",
                                       f"close_snipe {market.slug} blocked: "
                                       f"{'; '.join(br.reasons)}", family=market.family)
            return

        inputs = self._snipe_inputs(market, now, cfg)
        if inputs is None:
            return
        S_open, S_t, sigma_1s = inputs

        books = self._fetch_books([market.up_token_id, market.down_token_id])
        book_up = books.get(market.up_token_id)
        book_down = books.get(market.down_token_id)
        # Feed the depth reference from books we already had to fetch — the
        # adverse-size statistic costs no extra REST traffic.
        self.depth.observe_book(market.family, book_up, float(cfg["price_min"]),
                                 float(cfg["price_max"]))
        self.depth.observe_book(market.family, book_down, float(cfg["price_min"]),
                                 float(cfg["price_max"]))

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
        log.info("SIGNAL close_snipe %s coin=%s side=%s fair=%.4f ask=%.4f edge=%.4f tau=%.1fs",
                  market.slug, coin, sig.side, sig.fair, sig.ask, sig.edge, sig.tau_secs)
        self.status.add_event("signal", f"close_snipe {market.slug} side={sig.side} "
                                         f"edge={sig.edge:.3f}", family=market.family)

        # ---- M5 SHADOW GATE. The signal above is real and is now in the
        # ledger; what a shadow coin never gets is a fill. Placed AFTER the
        # signal record on purpose: the whole point of shadow mode is to
        # accumulate live out-of-sample evidence for a coin whose backtest did
        # not survive stress (ethereum: +9.94c shipped, +0.49c at a 3 s round
        # trip, +3.73c at a 300 ms feed lag — verifier §7). Zero risk, real data.
        if not may_fill:
            self.status.add_event("shadow_signal",
                                   f"close_snipe {market.slug} coin={coin} side={sig.side} "
                                   f"edge={sig.edge:.3f} — SHADOW, no fill dispatched",
                                   family=market.family)
            if coin not in self._shadow_logged:
                self._shadow_logged.add(coin)
                log.warning("coin %s is in shadow_coins: signals are recorded but NO fill "
                            "will ever be dispatched for it.", coin)
            log.info("SHADOW (no fill) %s coin=%s edge=%.4f", market.slug, coin, sig.edge)
            return

        book_at_signal = book_up if sig.side == "up" else book_down

        # ---- M5 BOOK-AGE GUARD at the decision point. See `_book_too_stale`.
        stale_age = self._book_too_stale(book_at_signal)
        if stale_age is not None:
            log.warning("skipping fill for %s: signal book is %.1fs stale (> %ss)",
                        market.slug, stale_age, self.config.max_book_age_s)
            self.status.add_event("stale_book",
                                   f"close_snipe {market.slug} signal book {stale_age:.1f}s "
                                   f"stale — no fill", family=market.family)
            return

        if self._would_exceed_global_cap():
            log.warning("skipping fill for %s: max_open_notional would be exceeded", market.slug)
            return
        # re-check the breaker: a resolution may have landed since the gate above
        br = self.breaker.allow_new_position()
        if br.tripped:
            log.error("skipping fill for %s: circuit breaker tripped (%s)",
                      market.slug, "; ".join(br.reasons))
            self.status.add_event("risk_block", f"close_snipe {market.slug} fill blocked: "
                                                 f"{'; '.join(br.reasons)}",
                                   family=market.family)
            return

        # Per-coin clip. M3 §6: median fillable notional per signal is $6-12 on
        # EVERY coin including BTC, so the cap is a tail-loss control, not an
        # upside control — a new coin starts small by construction.
        cap_usd = self.config.coin_cap_usd(coin)
        latency_ms = int(self.config.execution_cfg["latency_ms"])
        price_min = float(cfg["price_min"])
        price_max = float(cfg["price_max"])
        edge_min = float(cfg["edge_min"])
        fair = sig.fair
        # M4 guard 1: per-level share ceiling, None when the filter is off or
        # the depth reference is not yet established.
        mls = max_level_shares(self._adverse_cfg, self.depth, market.family)
        mode = str(self._adverse_cfg.get("mode", "cap"))

        def edge_fn(p: float, _fair=fair) -> float:
            from .fill_engine import fee_per_share
            return _fair - p - fee_per_share(p, self.config.fee_rate)

        self.executor.submit(self._run_fill, "close_snipe", market, sig.side, sig.token_id,
                              edge_fn, edge_min, price_min, price_max, cap_usd, latency_ms,
                              signal_id, book_at_signal, None, mls, mode)

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
        # M4 guard 3 applies to every strategy that OPENS a position, not just
        # close_snipe — a daily stop that one strategy can walk around is not a
        # daily stop. Wall clock, same reason as in _maybe_snipe.
        br = self.breaker.allow_new_position()
        if br.tripped:
            if market.slug not in self._breaker_blocked_slugs:
                self._breaker_blocked_slugs.add(market.slug)
                self.status.add_event("risk_block", f"settle_sweep {market.slug} blocked: "
                                                     f"{'; '.join(br.reasons)}",
                                       family=market.family)
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

    # ----------------------------------------------------------- guard status
    def _publish_guard_status(self) -> None:
        """Push warmup / breaker / depth state into status.json every tick.

        Wrapped in a try: status reporting must never be able to stop trading.
        """
        try:
            window = float(self.config.snipe_cfg["vol_window_secs"])
            wu = self.warmup.check(self.binance, window)
            # Log warmup progress from the TICK loop, not only from a gated
            # snipe: after a restart the next 1h close can be ~an hour away, and
            # "silently not trading" must never look like "nothing to trade".
            self.warmup.log_progress(wu, "startup")
            wud = wu.as_dict()
            wud["oracle"] = "binance"
            # Warmup is per-oracle, so it must be REPORTED per-oracle. A shared
            # poller that starves one coin's buffer is invisible in a single
            # aggregate number, and "silently not trading" is the failure mode
            # this whole guard exists to make loud.
            per_coin = {}
            for coin, oracle in self._binance_oracles().items():
                st = self.warmup.check(oracle, window)
                feed = coin_feed(coin)
                per_coin[coin] = {
                    "symbol": feed[0] if feed else None,
                    "venue": feed[1] if feed else None,
                    "oracle_samples": st.samples,
                    "ready": st.ready,
                    "may_fill": coin in self.config.allowed_coins()
                                and coin not in self.config.shadow_coins(),
                    "shadow": coin in self.config.shadow_coins(),
                    "cap_usd": self.config.coin_cap_usd(coin),
                }
            wud["coins"] = per_coin
            risk = self.breaker.evaluate().as_dict()
            if not risk["tripped"] and self._breaker_blocked_slugs:
                # breaker released (UTC rollover or manual resume): allow the
                # "blocked" event to fire again if it trips a second time
                self._breaker_blocked_slugs.clear()
            depth = {
                "enabled": bool(self._adverse_cfg.get("enabled", False)),
                "mode": self._adverse_cfg.get("mode", "cap"),
                "max_size_ratio": self._adverse_cfg.get("max_size_ratio"),
                "families": self.depth.snapshot(),
            }
            self.status.set_guards(wud, risk, depth)
        except Exception:  # noqa: BLE001
            log.exception("guard status publish failed")

    def _would_exceed_global_cap(self) -> bool:
        max_notional = float(self.config.sizing_cfg["max_open_notional"])
        return self.ledger.total_open_notional() >= max_notional

    # ------------------------------------------------------------- fill worker
    def _run_fill(self, strategy: str, market: Market, side: str, token_id: str, edge_fn,
                  edge_min: float, price_min: float, price_max: float, cap_usd: float,
                  latency_ms: int, signal_id: Optional[int], book_at_signal,
                  in_flight_set: Optional[Set[str]] = None,
                  max_level_shares_: Optional[float] = None,
                  anomalous_mode: str = "cap") -> None:
        try:
            attempt: FillAttempt = self.router.place_taker_buy(
                token_id, side, edge_fn, edge_min, price_min, price_max, cap_usd, latency_ms,
                book_at_signal=book_at_signal, max_level_shares=max_level_shares_,
                anomalous_mode=anomalous_mode,
            )
            # keep the depth reference fed with the fill-time book too
            self.depth.observe_book(market.family, attempt.book_at_fill, price_min, price_max)
            extra_meta = None
            if max_level_shares_ is not None or attempt.walk.size_filter_bound:
                extra_meta = {
                    "adverse_size_max_level_shares": max_level_shares_,
                    "adverse_size_mode": anomalous_mode,
                    "adverse_size_levels_capped": attempt.walk.n_levels_capped,
                    "adverse_size_levels_skipped": attempt.walk.n_levels_skipped,
                    "adverse_size_shares_suppressed": round(
                        attempt.walk.shares_suppressed, 4),
                    "depth_reference": self.depth.reference(market.family),
                }
            self.ledger.record_fill(strategy=strategy, family=market.family,
                                     market_slug=market.slug, signal_id=signal_id,
                                     attempt=attempt, edge_min=edge_min,
                                     extra_meta=extra_meta)
            if attempt.walk.size_filter_bound:
                self.status.add_event(
                    "adverse_size", f"{strategy} {market.slug} capped={attempt.walk.n_levels_capped} "
                                    f"skipped={attempt.walk.n_levels_skipped} "
                                    f"shares_suppressed={attempt.walk.shares_suppressed:.2f}",
                    family=market.family)
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
