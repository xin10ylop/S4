#!/usr/bin/env python3
"""
5m close_snipe replay harness with a BOOK-STALENESS FILTER and enforced
Chainlink publication causality.

WHY THIS EXISTS
---------------
`docs/07_scale_audit.md` §3.2 refuted the fresh-data 5m validation because 7 of
108 fills executed against order books that had been FROZEN BY A VENDOR OUTAGE
(ages 355-1975 s; 14 of the 16 stale books in the sample share one last-update
instant on 2026-07-21 ~04:07 UTC). Those 7 fills were 53.6% of total P&L. That
is not lookahead - it is *simulated liquidity that was never observed to exist
at the decision instant*. Any replay that walks "the last book snapshot before
t" without asking how old that snapshot is will manufacture the same artifact.

This harness therefore measures, for every decision and every fill, the AGE of
the book observation it is about to trade against, and can reject it.

WHAT IS PORTED (not re-derived)
-------------------------------
`evaluate_close_snipe_port`  <- bot/polybot/strategy.py:evaluate_close_snipe
`fair_value_up_port`         <- bot/polybot/strategy.py:fair_value_up
`snipe_tau_bounds_port`      <- bot/polybot/strategy.py:snipe_tau_bounds
`walk_asks_port`             <- bot/polybot/fill_engine.py:walk_asks
`fee_per_share`              <- bot/polybot/fill_engine.py:fee_per_share
Each is a line-for-line transcription and is property-tested against the real
function on randomised inputs by `scripts/fresh5m/proptest.py` (expect 0
mismatches). Do not "improve" them here - fix the bot and re-port.

The window gate ("one entry per window, first qualifying second, first
qualifying side") lives in `run_window`, mirroring engine._maybe_snipe.

CAUSALITY RULES ENFORCED
------------------------
1. Chainlink publication lag. A Chainlink report observed at second T is
   PUBLISHED at server_timestamp_us ~ T+1.1s (p50 1.119, p90 1.531, p99 2.017 -
   audit/A1_chainlink.md). A decision taken at wall-clock t may only use reports
   with server_timestamp_us <= t. `Params.causal=True` enforces exactly that and
   is the default; `causal=False` reproduces the trap (observation_ts <= t) so
   the size of the foresight it invents can be quoted. This mirrors the live
   bot, whose ChainlinkOracle.latest_raw(as_of=...) filters on `received_at`.
2. tau is measured from the OBSERVATION time of the print we actually hold to
   the close, not from wall-clock t. Holding a 1.1s-old price means 1.1s of
   uncertainty we cannot see; charging for it is the honest choice.
3. The settle print is never read before the close.
4. Books: signal is evaluated on the book at t; the FILL walks the book
   re-fetched at t + latency_ms, and must independently clear edge_min.

RESOLUTION
----------
5m settles on Chainlink Data Streams: strike = first report with observation ts
>= window_start, settle = first report with observation ts >= window_end, Up
wins iff settle >= strike (BACKFILL, not forward-fill - audit/A1_chainlink.md,
100.00000% agreement with result_id on 35,982 markets). The harness can take
the winner from the vendor's `result_id` (default) or recompute it from
Chainlink (`--winner-source chainlink`); running both and comparing the trade
tapes is the resolution cross-check (worth 0.26c/share on the 43-day sample,
i.e. the two agree).

USAGE
-----
  python3 scripts/fresh5m/replay.py control            # 43-day repo control test
  python3 scripts/fresh5m/replay.py run --source repo --days 2026-04-02,... \
        --edge-min 0.03 --max-book-age 5
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = os.environ.get("S4_ROOT", "/home/user/S4")
REPO_PROC = f"{ROOT}/data/data/processed/daily"
INF = float("inf")


# ==========================================================================
# 0. PORTED BOT PRIMITIVES  (line-for-line; property-tested in proptest.py)
# ==========================================================================

def fee_per_share(price: float, fee_rate: float) -> float:
    """PORT of fill_engine.fee_per_share."""
    return fee_rate * price * (1.0 - price)


def normal_cdf(z: float) -> float:
    """PORT of oracle.normal_cdf (math.erf, not scipy - same as the bot)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_value_up_port(S_t: float, S_open: float, sigma_1s: float,
                       tau_secs: float) -> Optional[float]:
    """PORT of strategy.fair_value_up."""
    if not (math.isfinite(S_t) and math.isfinite(S_open)
            and math.isfinite(sigma_1s) and math.isfinite(tau_secs)):
        return None
    if sigma_1s <= 0 or tau_secs <= 0 or S_open <= 0 or S_t <= 0:
        return None
    sigma = sigma_1s * math.sqrt(tau_secs)
    if sigma <= 0 or not math.isfinite(sigma):
        return None
    z = math.log(S_t / S_open) / sigma
    if not math.isfinite(z):
        return None
    return normal_cdf(z)


def snipe_tau_bounds_port(cfg: dict, latency_ms: int) -> Tuple[float, float]:
    """PORT of strategy.snipe_tau_bounds.

    tau_lo = max(snipe_min_tau_secs, latency_ms/1000 + snipe_fill_margin_secs).
    A higher `latency_ms` therefore NARROWS the tradeable band from the late
    end - which is the whole point: an order that arrives after the close
    cannot fill. This is the knob docs/07 §3.3 showed flips 5m from +6.61c to
    -2.69c when applied honestly.
    """
    tau_hi = float(cfg["snipe_last_secs"])
    tau_lo = max(
        float(cfg.get("snipe_min_tau_secs", 2.0)),
        latency_ms / 1000.0 + float(cfg.get("snipe_fill_margin_secs", 0.5)),
    )
    return tau_lo, tau_hi


@dataclass
class Level:
    price: float
    size: float


@dataclass
class SnipeSig:
    side: str
    fair: float
    ask: float
    edge: float
    tau_secs: float


def evaluate_close_snipe_port(
    close_ts: float,
    now_ts: float,
    S_t: Optional[float],
    S_open: Optional[float],
    sigma_1s: Optional[float],
    asks_up: Optional[Sequence[Level]],
    asks_down: Optional[Sequence[Level]],
    cfg: dict,
    fee_rate: float,
) -> Optional[SnipeSig]:
    """PORT of strategy.evaluate_close_snipe.

    Differs from the bot only in its container types: the bot takes
    Market/OrderBook objects, this takes close_ts plus the two ask ladders.
    `asks_* is None` <=> the bot's `book is None or book.best_ask is None`,
    which is exactly how a STALE book is signalled here (see run_window).
    """
    if S_t is None or S_open is None or sigma_1s is None:
        return None
    tau = close_ts - now_ts
    if tau <= 0:
        return None
    sigma_1s = max(sigma_1s, float(cfg.get("sigma_1s_floor", 8e-6)))
    fair_up = fair_value_up_port(S_t, S_open, sigma_1s, tau)
    if fair_up is None:
        return None
    fair_cap = float(cfg.get("fair_cap", 0.98))
    fair_up = min(max(fair_up, 1.0 - fair_cap), fair_cap)
    fair_down = 1.0 - fair_up

    edge_min = float(cfg["edge_min"])
    price_min = float(cfg["price_min"])
    price_max = float(cfg["price_max"])

    for side, asks, fair in (("up", asks_up, fair_up), ("down", asks_down, fair_down)):
        if asks is None or len(asks) == 0:
            continue
        ask = asks[0].price
        if not (price_min < ask < price_max):
            continue
        edge = fair - ask - fee_per_share(ask, fee_rate)
        if edge > edge_min:
            return SnipeSig(side=side, fair=fair, ask=ask, edge=edge, tau_secs=tau)
    return None


@dataclass
class LevelFill:
    price: float
    shares: float
    fee_per_share: float


@dataclass
class WalkResult:
    fills: List[LevelFill] = field(default_factory=list)

    @property
    def total_shares(self) -> float:
        return sum(f.shares for f in self.fills)

    @property
    def total_cost(self) -> float:
        return sum(f.price * f.shares for f in self.fills)

    @property
    def total_fees(self) -> float:
        return sum(f.fee_per_share * f.shares for f in self.fills)

    @property
    def avg_price(self) -> Optional[float]:
        sh = self.total_shares
        return (self.total_cost / sh) if sh > 0 else None


def walk_asks_port(
    asks: Sequence[Level],
    edge_fn: Callable[[float], float],
    edge_min: float,
    price_min: float,
    price_max: float,
    cap_usd: float,
    fee_rate: float,
    max_above_best: Optional[float] = 0.03,
) -> WalkResult:
    """PORT of fill_engine.walk_asks. Byte-for-byte control flow."""
    result = WalkResult()
    remaining_usd = cap_usd
    best_price: Optional[float] = None
    for lvl in asks:
        if remaining_usd <= 0:
            break
        if not (price_min < lvl.price < price_max):
            break
        if best_price is None:
            best_price = lvl.price
        elif max_above_best is not None and lvl.price > best_price + max_above_best + 1e-9:
            break
        edge = edge_fn(lvl.price)
        if edge <= edge_min:
            break
        max_shares_by_budget = remaining_usd / lvl.price
        shares = min(lvl.size, max_shares_by_budget)
        if shares <= 1e-9:
            break
        fps = fee_per_share(lvl.price, fee_rate)
        result.fills.append(LevelFill(price=lvl.price, shares=shares, fee_per_share=fps))
        remaining_usd -= shares * lvl.price
    return result


# ==========================================================================
# 1. PARAMETERS
# ==========================================================================

@dataclass
class Params:
    # --- shipped strategy params (bot/config.yaml strategy.close_snipe) ----
    edge_min: float = 0.03
    price_min: float = 0.30
    price_max: float = 0.99
    vol_window_secs: float = 120.0
    sigma_1s_floor: float = 8.0e-6      # 1h-calibrated; 3e-5 is the Chainlink value
    fair_cap: float = 0.98
    snipe_last_secs: float = 5.0
    snipe_min_tau_secs: float = 2.5
    snipe_fill_margin_secs: float = 0.5
    # --- execution / sizing ------------------------------------------------
    fee_rate: float = 0.07              # STRESS KNOB
    latency_ms: int = 1500              # STRESS KNOB (feeds tau_lo)
    depth_fraction: float = 1.0         # STRESS KNOB (take only X% of displayed size)
    max_walk_above_best: float = 0.03
    per_event_cap_usd: float = 25.0
    # --- staleness / causality --------------------------------------------
    max_book_age_s: float = INF         # THE point of this harness
    causal: bool = True                 # Chainlink server_timestamp_us <= t
    oracle_max_staleness_secs: float = 4.0   # bot's chainlink.max_staleness_secs
    # --- conventions (switchable so legacy frames can be reproduced) -------
    sigma_mode: str = "grid_ffill"      # "grid_ffill" (A3) | "bot" (oracle.py)
    strike_mode: str = "backfill"       # "backfill" (A1) | "exact_else_ffill" (A3)
    winner_source: str = "result_id"    # "result_id" | "chainlink"
    ladder: bool = True                 # walk all levels the tape provides
    tau_grid: Optional[Sequence[float]] = None   # override the derived band
    tick_hz: float = 1.0                # decision instants per second

    def cfg(self) -> dict:
        return dict(edge_min=self.edge_min, price_min=self.price_min,
                    price_max=self.price_max, sigma_1s_floor=self.sigma_1s_floor,
                    fair_cap=self.fair_cap, snipe_last_secs=self.snipe_last_secs,
                    snipe_min_tau_secs=self.snipe_min_tau_secs,
                    snipe_fill_margin_secs=self.snipe_fill_margin_secs)

    def taus(self) -> List[float]:
        """Decision instants as seconds-to-close, EARLIEST FIRST (the bot ticks
        forward in time, i.e. downward in tau, and takes the first signal)."""
        if self.tau_grid is not None:
            return list(self.tau_grid)
        lo, hi = snipe_tau_bounds_port(self.cfg(), self.latency_ms)
        step = 1.0 / self.tick_hz
        n = int(math.floor((hi - lo) / step + 1e-9))
        return [round(hi - i * step, 6) for i in range(n + 1)]


# ==========================================================================
# 2. CHAINLINK FEED  (publication-lag aware)
# ==========================================================================

class ChainlinkFeed:
    """Chainlink BTC/USD Data Streams series with an honest 'what did we know
    at wall-clock t' view.

      timestamp_us         observation time  -> what Polymarket settles on
      server_timestamp_us  publication time  -> when we could first know it
    """

    def __init__(self, df: pd.DataFrame, vol_window_secs: float = 120.0,
                 sigma_mode: str = "grid_ffill"):
        df = df.dropna(subset=["price"]).drop_duplicates("timestamp_us")
        df = df.sort_values("server_timestamp_us")
        self.pub = df.server_timestamp_us.to_numpy(np.int64)
        self.obs = df.timestamp_us.to_numpy(np.int64)
        self.px = df.price.to_numpy(float)
        o = np.argsort(self.obs, kind="stable")
        self.obs_sorted = self.obs[o]
        self.px_by_obs = self.px[o]
        self.pub_by_obs = self.pub[o]
        self.sec_sorted = self.obs_sorted // 1_000_000
        self.vol_window_secs = float(vol_window_secs)
        self.sigma_mode = sigma_mode
        self._build_sigma()

    # ---------------- boundary prints (strike / settle) -------------------
    def _first_at_or_after(self, sec: np.ndarray, max_gap: int = 5) -> np.ndarray:
        """A1 rule: FIRST print with observation second >= sec (backfill).
        `max_gap` mirrors oracles.chainlink.max_gap_secs = 5."""
        sec = np.asarray(sec, np.int64)
        i = np.searchsorted(self.obs_sorted, sec * 1_000_000, side="left")
        out = np.full(len(sec), np.nan)
        ok = i < len(self.obs_sorted)
        j = np.clip(i, 0, len(self.obs_sorted) - 1)
        within = ok & (self.sec_sorted[j] - sec <= max_gap)
        out[within] = self.px_by_obs[j[within]]
        return out

    def _exact(self, sec: np.ndarray) -> np.ndarray:
        sec = np.asarray(sec, np.int64)
        i = np.searchsorted(self.obs_sorted, sec * 1_000_000)
        out = np.full(len(sec), np.nan)
        j = np.clip(i, 0, len(self.obs_sorted) - 1)
        hit = (i < len(self.obs_sorted)) & (self.obs_sorted[j] == sec * 1_000_000)
        out[hit] = self.px_by_obs[j[hit]]
        return out

    def _at_or_before(self, sec: np.ndarray) -> np.ndarray:
        sec = np.asarray(sec, np.int64)
        i = np.searchsorted(self.obs_sorted, sec * 1_000_000, side="right") - 1
        out = np.full(len(sec), np.nan)
        ok = i >= 0
        out[ok] = self.px_by_obs[i[ok]]
        return out

    def strike(self, wts: np.ndarray, mode: str = "backfill") -> np.ndarray:
        if mode == "backfill":
            return self._first_at_or_after(wts)
        # A3 legacy: exact second, else the newest print at-or-before it
        s = self._exact(wts)
        miss = ~np.isfinite(s)
        if miss.any():
            s[miss] = self._at_or_before(np.asarray(wts)[miss])
        return s

    def settle(self, close_s: np.ndarray, mode: str = "backfill") -> np.ndarray:
        return self.strike(close_s, mode=mode)

    # ---------------- live view -------------------------------------------
    def latest_at(self, t_us: np.ndarray, causal: bool = True):
        """The newest print the bot could hold at wall-clock t_us.

        causal=True  -> newest with server_timestamp_us <= t   (TRUTH)
        causal=False -> newest with timestamp_us       <= t   (THE TRAP: this
                        hands the model a price it will not learn for ~1.1s)

        Returns (price, obs_ts_us, pub_ts_us); NaN/-1 where nothing is held.
        """
        t_us = np.asarray(t_us, np.int64)
        if causal:
            i = np.searchsorted(self.pub, t_us, side="right") - 1
            src_px, src_obs, src_pub = self.px, self.obs, self.pub
        else:
            i = np.searchsorted(self.obs_sorted, t_us, side="right") - 1
            src_px, src_obs, src_pub = self.px_by_obs, self.obs_sorted, self.pub_by_obs
        px = np.full(len(t_us), np.nan)
        obs = np.full(len(t_us), -1, np.int64)
        pub = np.full(len(t_us), -1, np.int64)
        ok = i >= 0
        px[ok] = src_px[i[ok]]
        obs[ok] = src_obs[i[ok]]
        pub[ok] = src_pub[i[ok]]
        return px, obs, pub

    def causality_violations(self, t_us: np.ndarray) -> Dict[str, int]:
        """ASSERTION SUPPORT. Counts decisions where the observation-indexed
        view and the publication-indexed view disagree, i.e. where using
        `timestamp_us <= t` would hand us a report not yet published."""
        _, obs_c, _ = self.latest_at(t_us, causal=True)
        _, obs_n, pub_n = self.latest_at(t_us, causal=False)
        held = (obs_c >= 0) & (obs_n >= 0)
        return dict(
            n=int(held.sum()),
            future_report_used=int(((pub_n > np.asarray(t_us, np.int64)) & held).sum()),
            newer_obs_than_causal=int(((obs_n > obs_c) & held).sum()),
            mean_foresight_s=float(np.mean((obs_n[held] - obs_c[held]) / 1e6)) if held.any() else 0.0,
        )

    # ---------------- realised volatility ---------------------------------
    def _build_sigma(self):
        """sigma_1s(T) for every observation second T we hold.

        "grid_ffill" (A3/replay5m convention): 1s grid, forward-filled log
            price, rolling std of the 1s diffs over `vol_window_secs`
            (min_periods=30). Missing seconds contribute a ZERO return.
        "bot" (bot/polybot/oracle.py:ChainlinkOracle.rolling_log_return_std):
            only the seconds actually held in [T-window, T] are used, and the
            log return between two prints straddling a gap is counted as if it
            were a 1s return. This is what the LIVE bot computes; it runs
            slightly hotter than the ffill grid because gaps are not damped.
        """
        sec = self.sec_sorted
        lp_all = np.log(self.px_by_obs)
        # collapse duplicate seconds to the last print in that second
        s_ser = pd.Series(lp_all, index=sec).groupby(level=0).last()
        secs_u = s_ser.index.to_numpy(np.int64)
        lp_u = s_ser.to_numpy(float)
        if self.sigma_mode == "grid_ffill":
            lo, hi = int(secs_u[0]), int(secs_u[-1])
            grid = np.arange(lo, hi + 1)
            lp = pd.Series(lp_u, index=secs_u).reindex(grid).ffill()
            r = lp.diff()
            w = int(self.vol_window_secs)
            s = r.rolling(w, min_periods=30).std(ddof=1)
            self._sig_sec = grid
            self._sig_val = s.to_numpy()
        elif self.sigma_mode == "bot":
            r = np.diff(lp_u)                       # return between consecutive HELD prints
            n = len(r)
            c1 = np.concatenate([[0.0], np.cumsum(r)])
            c2 = np.concatenate([[0.0], np.cumsum(r * r)])
            hi_idx = np.arange(len(secs_u))          # print index of T
            lo_idx = np.searchsorted(secs_u, secs_u - int(self.vol_window_secs), side="left")
            a, b = lo_idx, hi_idx                    # returns r[a:b]
            cnt = (b - a).astype(float)
            s1 = c1[b] - c1[a]
            s2 = c2[b] - c2[a]
            with np.errstate(invalid="ignore", divide="ignore"):
                var = (s2 - s1 * s1 / np.maximum(cnt, 1)) / np.maximum(cnt - 1, 1)
                val = np.sqrt(np.maximum(var, 0.0))
            val[cnt < 2] = np.nan
            _ = n
            self._sig_sec = secs_u
            self._sig_val = val
        else:
            raise ValueError(f"unknown sigma_mode {self.sigma_mode}")

    def sigma_at_obs(self, obs_us: np.ndarray) -> np.ndarray:
        sec = np.asarray(obs_us, np.int64) // 1_000_000
        i = np.searchsorted(self._sig_sec, sec)
        out = np.full(len(sec), np.nan)
        j = np.clip(i, 0, len(self._sig_sec) - 1)
        hit = (i < len(self._sig_sec)) & (self._sig_sec[j] == sec)
        out[hit] = self._sig_val[j[hit]]
        return out


def load_chainlink(days: Sequence[str], vol_window_secs: float = 120.0,
                   sigma_mode: str = "grid_ffill",
                   dirs: Sequence[str] = ()) -> Optional[ChainlinkFeed]:
    """Load crypto_prices for `days`. Searches the repo vault first, then any
    extra directories given (so a fresh fetch can be dropped in unchanged)."""
    search = [f"{REPO_PROC}/crypto_prices/{{d}}.parquet",
              f"{ROOT}/data/a3/crypto_prices/polymarket_crypto_prices_{{d}}_btcusd.parquet"]
    for extra in dirs:
        search += [f"{extra}/{{d}}.parquet",
                   f"{extra}/polymarket_crypto_prices_{{d}}_btcusd.parquet",
                   f"{extra}/crypto_prices/{{d}}.parquet"]
    frames = []
    cols = ["timestamp_us", "server_timestamp_us", "price"]
    for d in days:
        for pat in search:
            f = pat.format(d=d)
            if os.path.exists(f):
                x = pd.read_parquet(f, columns=cols)
                x["price"] = x.price.astype(float)
                frames.append(x)
                break
    if not frames:
        return None
    return ChainlinkFeed(pd.concat(frames, ignore_index=True),
                         vol_window_secs=vol_window_secs, sigma_mode=sigma_mode)


# ==========================================================================
# 3. BOOK TAPES
# ==========================================================================

class BookTape:
    """An event-driven book tape for ONE token in ONE window.

    `at(t_us)` returns (levels, age_seconds) for the last snapshot published
    at or before t. `age` is the whole point of this class: it is how long ago
    the venue last told us anything about this book. A 1975-second age means
    the tape is frozen and the "liquidity" in it was never observed to exist.
    """

    __slots__ = ("ts", "prices", "sizes")

    def __init__(self, ts: np.ndarray, prices: np.ndarray, sizes: np.ndarray):
        self.ts = ts              # (n,)   int64 us
        self.prices = prices      # (n, L) float
        self.sizes = sizes        # (n, L) float

    def at(self, t_us: int) -> Tuple[Optional[List[Level]], float]:
        i = int(np.searchsorted(self.ts, t_us, side="right")) - 1
        if i < 0:
            return None, INF
        age = (t_us - self.ts[i]) / 1e6
        p, s = self.prices[i], self.sizes[i]
        # Truncate at the first level the vendor did not report - do NOT skip
        # past it, a hole in the ladder is not depth. A finite price with a
        # missing size is kept at size 0 so the walk stops there rather than
        # inventing liquidity (and so the signal still sees the quote, exactly
        # as the bot's OrderBook.best_ask would).
        lv: List[Level] = []
        for pp, ss in zip(p, s):
            if not np.isfinite(pp):
                break
            lv.append(Level(float(pp), float(ss) if np.isfinite(ss) and ss > 0 else 0.0))
        if not lv:
            return None, age
        return lv, age


@dataclass
class Window:
    day: str
    wts: int
    close_s: int
    result_id: Optional[int]
    up: BookTape
    dn: BookTape


# ------------------------------ repo adapter ------------------------------
_BC_COLS = ["timestamp_us", "bid_p0", "bid_s0", "ask_p0", "ask_s0", "wts"]
_Q_COLS = ["timestamp_us", "bid_price", "bid_size", "ask_price", "ask_size", "wts"]


def load_repo_day(day: str, book_source: str = "bookcurves") -> List[Window]:
    """Repo vault: data/data/processed/daily/5m/{bookcurves,quotes}/DAY.parquet.

    The vault stores ONE token's book per window (the Up token). The Down-token
    ask is reconstructed as `1 - bid_up`, which A3 §3.2 verified against the
    real Down ladder on 5,017/5,017 fresh comparisons, exactly. Both synthetic
    sides therefore share the Up tape's timestamps and hence its AGE - which is
    correct: one vendor stream, one staleness.
    """
    if book_source == "bookcurves":
        f = f"{REPO_PROC}/5m/bookcurves/{day}.parquet"
        cols, ren = _BC_COLS, dict(bid_p0="bid", bid_s0="bidsz", ask_p0="ask", ask_s0="asksz")
    else:
        f = f"{REPO_PROC}/5m/quotes/{day}.parquet"
        cols, ren = _Q_COLS, dict(bid_price="bid", bid_size="bidsz",
                                  ask_price="ask", ask_size="asksz")
    if not os.path.exists(f):
        return []
    b = pd.read_parquet(f, columns=cols).rename(columns=ren)
    b = b.dropna(subset=["wts"]).sort_values("timestamp_us")
    w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    w = w[(w.family == "5m") & (w.date == day)][["wts", "duration", "result_id"]]
    w = w.drop_duplicates("wts").sort_values("wts")
    if w.empty:
        return []
    out: List[Window] = []
    grp = {int(k): v for k, v in b.groupby("wts", sort=False)}
    for _, r in w.iterrows():
        wts, dur = int(r.wts), int(r.duration)
        g = grp.get(wts)
        if g is None or g.empty:
            continue
        ts = g.timestamp_us.to_numpy(np.int64)
        ask = g.ask.to_numpy(float)[:, None]
        asz = g.asksz.to_numpy(float)[:, None]
        bid = g.bid.to_numpy(float)[:, None]
        bsz = g.bidsz.to_numpy(float)[:, None]
        out.append(Window(day=day, wts=wts, close_s=wts + dur,
                          result_id=(int(r.result_id) if pd.notna(r.result_id) else None),
                          up=BookTape(ts, ask, asz),
                          dn=BookTape(ts, 1.0 - bid, bsz)))
    return out


# ----------------------------- fresh adapter ------------------------------
def load_fresh_day(day: str, books_dir: str) -> List[Window]:
    """Fresh capture. Two accepted layouts, auto-detected:

    (a) TAPE  - `<books_dir>/<day>.parquet` with one row per book update:
        close_s, oid (0=Up, 1=Down), timestamp_us,
        ask_price_0..N, ask_size_0..N  [, bid_price_0, bid_size_0]
        This is the layout to prefer: it preserves real update times, so the
        staleness filter measures true venue silence.

    (b) SNAPS - `<books_dir>/snaps.parquet` in the audit/A3 layout:
        close_s, oid, rel (seconds relative to close), book_age,
        ask_price_0..4, ask_size_0..4. `rel` is the sample grid, `book_age`
        is the vendor-reported age of the snapshot, so the tape timestamp is
        reconstructed as close_s + rel - book_age.
    """
    tape_f = f"{books_dir}/{day}.parquet"
    snaps_f = f"{books_dir}/snaps.parquet"
    mk = None
    for cand in (f"{books_dir}/_markets.parquet", f"{books_dir}/markets.parquet"):
        if os.path.exists(cand):
            mk = pd.read_parquet(cand)
            break
    res: Dict[int, Optional[int]] = {}
    if mk is not None:
        mk["result_id"] = pd.to_numeric(mk.result_id, errors="coerce")
        res = {int(c): (int(v) if pd.notna(v) else None)
               for c, v in zip(mk.close_s, mk.result_id)}

    if os.path.exists(tape_f):
        df = pd.read_parquet(tape_f)
        df = df.sort_values("timestamp_us")
    elif os.path.exists(snaps_f):
        df = pd.read_parquet(snaps_f)
        df = df[df.d == day].copy()
        if df.empty:
            return []
        df["timestamp_us"] = ((df.close_s + df.rel - df.book_age) * 1e6).astype(np.int64)
        df = df.sort_values("timestamp_us")
    else:
        return []

    nlev = 0
    while f"ask_price_{nlev}" in df.columns:
        nlev += 1
    out: List[Window] = []
    for cs, g in df.groupby("close_s", sort=True):
        tapes = {}
        for oid in (0, 1):
            h = g[g.oid == oid]
            if h.empty:
                tapes[oid] = BookTape(np.zeros(0, np.int64), np.zeros((0, 1)), np.zeros((0, 1)))
                continue
            h = h.drop_duplicates("timestamp_us", keep="last")
            ts = h.timestamp_us.to_numpy(np.int64)
            pr = np.column_stack([h[f"ask_price_{L}"].to_numpy(float) for L in range(nlev)])
            sz = np.column_stack([h[f"ask_size_{L}"].to_numpy(float) for L in range(nlev)])
            keep = np.isfinite(pr).any(axis=1)
            tapes[oid] = BookTape(ts[keep], pr[keep], sz[keep])
        out.append(Window(day=day, wts=int(cs) - 300, close_s=int(cs),
                          result_id=res.get(int(cs)), up=tapes[0], dn=tapes[1]))
    return out


# ==========================================================================
# 4. THE REPLAY
# ==========================================================================

def run_window(w: Window, cl: ChainlinkFeed, p: Params) -> Optional[dict]:
    """One 5m window. Mirrors engine._maybe_snipe: tick from the early end of
    the band toward the close, one entry per window, first qualifying side.

    STALENESS. A book whose last update is older than `max_book_age_s` is
    passed to the evaluator as `None`, i.e. exactly as the live bot sees a
    book it could not fetch. Consequences, deliberately:
      * at SIGNAL time that side simply cannot be traded this tick (the other
        side and later ticks are still live);
      * at FILL time there is no fill and the attempt is recorded with
        outcome `stale_book`, so it shows up as a lost opportunity rather
        than silently disappearing.
    """
    cfg = p.cfg()
    close_s = w.close_s
    taus = p.taus()
    lat_us = int(p.latency_ms * 1000)

    # strike is fixed for the life of the window (bot caches it the same way)
    S_open = float(cl.strike(np.array([w.wts]), mode=p.strike_mode)[0])
    if not math.isfinite(S_open):
        return None

    # winner
    if p.winner_source == "chainlink":
        st = cl.strike(np.array([w.wts]), mode="backfill")[0]
        se = cl.settle(np.array([close_s]), mode="backfill")[0]
        if not (math.isfinite(st) and math.isfinite(se)):
            return None
        up_won = bool(se >= st)
    else:
        if w.result_id is None:
            return None
        up_won = (w.result_id == 0)     # vendor convention: result_id 0 => Up

    t_arr = np.array([close_s - t for t in taus], dtype=float)
    t_us_arr = np.rint(t_arr * 1e6).astype(np.int64)
    px, obs_us, pub_us = cl.latest_at(t_us_arr, causal=p.causal)
    sig_arr = cl.sigma_at_obs(obs_us)

    for n, tau_nom in enumerate(taus):
        t_us = int(t_us_arr[n])
        if obs_us[n] < 0 or not math.isfinite(px[n]):
            continue
        obs_lag = (t_us - obs_us[n]) / 1e6
        if obs_lag > p.oracle_max_staleness_secs:
            continue                     # bot: latest() returns None -> skip tick
        sigma = sig_arr[n]
        if not math.isfinite(sigma) or sigma <= 0:
            continue
        # tau runs from the OBSERVATION time of the print we hold to the close
        tau_eff = (close_s * 1e6 - obs_us[n]) / 1e6

        lv_up, age_up = w.up.at(t_us)
        lv_dn, age_dn = w.dn.at(t_us)
        stale_up = age_up > p.max_book_age_s
        stale_dn = age_dn > p.max_book_age_s
        a_up = None if (stale_up or lv_up is None) else lv_up
        a_dn = None if (stale_dn or lv_dn is None) else lv_dn

        sig = evaluate_close_snipe_port(
            close_ts=close_s, now_ts=close_s - tau_eff,
            S_t=float(px[n]), S_open=S_open, sigma_1s=float(sigma),
            asks_up=a_up, asks_down=a_dn, cfg=cfg, fee_rate=p.fee_rate)
        if sig is None:
            continue

        # ---- SIGNAL FIRES: one per window ---------------------------------
        tape = w.up if sig.side == "up" else w.dn
        age_sig = age_up if sig.side == "up" else age_dn
        fill_lv, age_fil = tape.at(t_us + lat_us)
        fair = sig.fair

        def edge_fn(q: float, _f=fair) -> float:
            return _f - q - fee_per_share(q, p.fee_rate)

        rec = dict(day=w.day, wts=w.wts, close_s=close_s, tau=tau_nom,
                   tau_eff=tau_eff, side=sig.side, fair=fair, sig_ask=sig.ask,
                   edge_sig=sig.edge, S_open=S_open, S_t=float(px[n]),
                   sigma=float(sigma), obs_lag=obs_lag,
                   pub_lag=(pub_us[n] - obs_us[n]) / 1e6,
                   book_age_sig=age_sig, book_age_fil=age_fil,
                   up_won=up_won, won=(up_won if sig.side == "up" else (not up_won)),
                   shares=0.0, avg_price=np.nan, pnl_per_share=np.nan, pnl=0.0,
                   n_levels=0, outcome="filled")

        if age_fil > p.max_book_age_s:
            rec["outcome"] = "stale_book"
            return rec
        if fill_lv is None:
            rec["outcome"] = "empty_book"
            return rec
        ladder = fill_lv if p.ladder else fill_lv[:1]
        if p.depth_fraction != 1.0:
            ladder = [Level(l.price, l.size * p.depth_fraction) for l in ladder]
        walk = walk_asks_port(ladder, edge_fn, p.edge_min, p.price_min, p.price_max,
                              p.per_event_cap_usd, p.fee_rate,
                              max_above_best=p.max_walk_above_best)
        if walk.total_shares <= 0:
            rec["outcome"] = "book_moved_no_edge"
            return rec
        avg = walk.avg_price
        ps = (1.0 if rec["won"] else 0.0) - avg - fee_per_share(avg, p.fee_rate)
        rec.update(shares=walk.total_shares, avg_price=avg, pnl_per_share=ps,
                   pnl=ps * walk.total_shares, n_levels=len(walk.fills))
        return rec
    return None


def run_days(days: Sequence[str], p: Params, source: str = "repo",
             books_dir: Optional[str] = None, book_source: str = "bookcurves",
             cl_dirs: Sequence[str] = (), verbose: bool = True):
    """Returns (trades DataFrame, meta dict)."""
    recs, meta = [], dict(windows=0, days=0, causality=[])
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_chainlink([prev, d], vol_window_secs=p.vol_window_secs,
                            sigma_mode=p.sigma_mode, dirs=cl_dirs)
        if cl is None:
            if verbose:
                print(f"  {d}: no chainlink", flush=True)
            continue
        wins = (load_repo_day(d, book_source) if source == "repo"
                else load_fresh_day(d, books_dir or f"{ROOT}/data/fresh5m/books"))
        if not wins:
            if verbose:
                print(f"  {d}: no books", flush=True)
            continue
        # causality audit on this day's decision grid
        probe = np.array([int((w.close_s - t) * 1e6) for w in wins for t in p.taus()],
                         dtype=np.int64)
        meta["causality"].append(cl.causality_violations(probe))
        n0 = len(recs)
        for w in wins:
            r = run_window(w, cl, p)
            if r is not None:
                recs.append(r)
        meta["windows"] += len(wins)
        meta["days"] += 1
        if verbose:
            print(f"  {d}: {len(wins)} windows, {len(recs)-n0} signals", flush=True)
    tr = pd.DataFrame(recs)
    return tr, meta


def run_days_multi(days: Sequence[str], param_sets: Dict[str, Params],
                   source: str = "repo", books_dir: Optional[str] = None,
                   book_source: str = "bookcurves", cl_dirs: Sequence[str] = (),
                   verbose: bool = True):
    """Run several parameter sets over the same tape, loading each day once.

    The book tapes are the expensive part (~0.5M rows/day), so a sensitivity
    sweep that reloads them per variant costs hours for nothing.
    """
    recs: Dict[str, list] = {k: [] for k in param_sets}
    meta = dict(windows=0, days=0, causality=[])
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        feeds: Dict[Tuple[str, float], Optional[ChainlinkFeed]] = {}
        for p in param_sets.values():
            key = (p.sigma_mode, p.vol_window_secs)
            if key not in feeds:
                feeds[key] = load_chainlink([prev, d], vol_window_secs=p.vol_window_secs,
                                            sigma_mode=p.sigma_mode, dirs=cl_dirs)
        if all(v is None for v in feeds.values()):
            if verbose:
                print(f"  {d}: no chainlink", flush=True)
            continue
        wins = (load_repo_day(d, book_source) if source == "repo"
                else load_fresh_day(d, books_dir or f"{ROOT}/data/fresh5m/books"))
        if not wins:
            if verbose:
                print(f"  {d}: no books", flush=True)
            continue
        ref = next(v for v in feeds.values() if v is not None)
        probe = np.array([int((w.close_s - t) * 1e6) for w in wins
                          for t in next(iter(param_sets.values())).taus()], dtype=np.int64)
        meta["causality"].append(ref.causality_violations(probe))
        for name, p in param_sets.items():
            cl = feeds[(p.sigma_mode, p.vol_window_secs)]
            if cl is None:
                continue
            for w in wins:
                r = run_window(w, cl, p)
                if r is not None:
                    recs[name].append(r)
        meta["windows"] += len(wins)
        meta["days"] += 1
        if verbose:
            print(f"  {d}: {len(wins)} windows", flush=True)
    return {k: pd.DataFrame(v) for k, v in recs.items()}, meta


# ==========================================================================
# 5. STATISTICS
# ==========================================================================

def agg_causality(rows: Sequence[dict]) -> dict:
    """Pool the per-day causality audits. `mean_foresight_s` is n-weighted, so
    it reads as "seconds of unpublished future the non-causal view hands you"."""
    if not rows:
        return {}
    d = pd.DataFrame(rows)
    n = float(d.n.sum())
    return dict(decisions=int(n),
                trap_would_use_unpublished_report=int(d.future_report_used.sum()),
                trap_rate=round(float(d.future_report_used.sum()) / n, 4) if n else np.nan,
                mean_foresight_s=round(float((d.mean_foresight_s * d.n).sum() / n), 3) if n else np.nan)


def _t(x: np.ndarray) -> float:
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def summarise(tr: pd.DataFrame, n_days: int, n_windows: int, label: str = "",
              max_book_age_s: float = INF) -> dict:
    """EV/share, per-trade t, and DAY-CLUSTERED t.

    The day-clustered t is `mean(daily mean pnl/share) / SE(daily means)` with
    n_days-1 df. It is the pre-committed statistic: fills inside one day share
    the same BTC path and the same book regime, so the per-trade t (which
    treats 1,500 fills as 1,500 independent draws) is badly overstated.

    `max_book_age_s < inf` applies the POST-HOC removal convention used by
    docs/07 §3.4 - the fill is dropped from the sample after the fact rather
    than at decision time. Use it to reproduce published numbers; use
    Params.max_book_age_s for the honest pre-decision rejection.
    """
    out = dict(label=label, days=n_days, windows=n_windows)
    if tr is None or tr.empty:
        return {**out, "signals": 0, "fills": 0, "ev_share": np.nan}
    f = tr[tr.outcome == "filled"].copy()
    n_all, pnl_all = len(f), f.pnl.sum()
    if math.isfinite(max_book_age_s):
        stale = (f.book_age_sig > max_book_age_s) | (f.book_age_fil > max_book_age_s)
        out["stale_fills_removed"] = int(stale.sum())
        out["stale_pnl_frac"] = float(f.pnl[stale].sum() / pnl_all) if pnl_all else np.nan
        f = f[~stale]
    ps = f.pnl_per_share.to_numpy(float)
    daily = f.groupby("day").pnl_per_share.mean().to_numpy(float)
    out.update(
        signals=len(tr), fills=len(f), fills_before=n_all,
        trades_day=len(f) / n_days if n_days else np.nan,
        signals_day=len(tr) / n_days if n_days else np.nan,
        ev_share=float(ps.mean()) if len(ps) else np.nan,
        median_share=float(np.median(ps)) if len(ps) else np.nan,
        win=float(f.won.mean()) if len(f) else np.nan,
        t_trade=_t(ps),
        t_day=_t(daily),
        n_day_clusters=len(daily),
        pnl=float(f.pnl.sum()), shares=float(f.shares.sum()),
        deployed=float((f.avg_price * f.shares).sum()),
        pnl_day=float(f.pnl.sum() / n_days) if n_days else np.nan,
        no_fill=int((tr.outcome != "filled").sum()),
        stale_book_blocked=int((tr.outcome == "stale_book").sum()),
        book_moved=int((tr.outcome == "book_moved_no_edge").sum()),
        empty_book=int((tr.outcome == "empty_book").sum()),
        up_frac=float((f.side == "up").mean()) if len(f) else np.nan,
    )
    return out


# ==========================================================================
# 6. CONTROL TEST + CLI
# ==========================================================================
CONTROL_DAYS = ([f"2026-04-{d:02d}" for d in range(2, 31)]
                + [f"2026-05-{d:02d}" for d in range(1, 13)]
                + ["2026-07-06", "2026-07-07"])


def cmd_control(args):
    """Reproduce docs/07 §3.4 on the repo's own 43 days.

    Targets: +8.38c/share t=7.56 after removing stale-book fills (71 of 1,503),
    14% of P&L; +10.89c t=10.33 at the shipped parameters.

    The published frame used A3's conventions, which this run reproduces
    exactly and then departs from one knob at a time:
      tau grid {6,5,4,3,2}s, edge_min 0.05, sigma via the ffill 1s grid,
      strike = exact-second else forward-fill, top-of-book fill only.
    """
    base = Params(edge_min=0.05, tau_grid=[6, 5, 4, 3, 2],
                  sigma_mode="grid_ffill", strike_mode="exact_else_ffill",
                  ladder=False, per_event_cap_usd=25.0, max_book_age_s=INF)
    tr, meta = run_days(CONTROL_DAYS, base, source="repo", verbose=args.verbose)
    tr.to_parquet(f"{ROOT}/data/c2/control_trades.parquet", index=False)
    nd, nw = meta["days"], meta["windows"]
    rows = [summarise(tr, nd, nw, "A3 baseline (em=0.05, tau 6-2), no filter"),
            summarise(tr, nd, nw, "  + post-hoc removal of book age > 30s", 30.0)]

    ship = replace(base, edge_min=0.03, tau_grid=[5, 4, 3, 2])
    tr2, m2 = run_days(CONTROL_DAYS, ship, source="repo", verbose=False)
    tr2.to_parquet(f"{ROOT}/data/c2/control_trades_shipped.parquet", index=False)
    rows += [summarise(tr2, m2["days"], m2["windows"], "shipped params (em=0.03, tau [2,5])"),
             summarise(tr2, m2["days"], m2["windows"], "  + post-hoc removal of book age > 30s", 30.0)]
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250, "display.max_columns", 60)
    print(df[["label", "days", "windows", "signals", "fills", "trades_day", "ev_share",
              "win", "t_trade", "t_day", "stale_fills_removed", "stale_pnl_frac"]].to_string(index=False))
    print("\ncausality audit over the decision grid:", agg_causality(meta["causality"]))
    df.to_csv(f"{ROOT}/data/c2/control_summary.csv", index=False)


def cmd_run(args):
    p = Params(
        edge_min=args.edge_min, sigma_1s_floor=args.sigma_floor,
        fee_rate=args.fee_rate, latency_ms=args.latency_ms,
        depth_fraction=args.depth_fraction, per_event_cap_usd=args.cap_usd,
        max_book_age_s=(INF if args.max_book_age < 0 else args.max_book_age),
        causal=not args.noncausal, sigma_mode=args.sigma_mode,
        strike_mode=args.strike_mode, winner_source=args.winner_source,
        tau_grid=([float(x) for x in args.tau_grid.split(",")] if args.tau_grid else None),
        ladder=not args.top_of_book,
    )
    days = args.days.split(",")
    tr, meta = run_days(days, p, source=args.source, books_dir=args.books_dir,
                        book_source=args.book_source,
                        cl_dirs=(args.chainlink_dir,) if args.chainlink_dir else ())
    if args.out:
        tr.to_parquet(args.out, index=False)
    print(pd.DataFrame([summarise(tr, meta["days"], meta["windows"], args.label)]).to_string(index=False))
    print("causality:", agg_causality(meta["causality"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("control", help="reproduce the verifier's 43-day numbers")
    c.add_argument("--verbose", action="store_true")
    c.set_defaults(func=cmd_control)

    r = sub.add_parser("run", help="replay arbitrary days")
    r.add_argument("--days", required=True)
    r.add_argument("--source", default="repo", choices=["repo", "fresh"])
    r.add_argument("--books-dir", default=None)
    r.add_argument("--chainlink-dir", default=None)
    r.add_argument("--book-source", default="bookcurves", choices=["bookcurves", "quotes"])
    r.add_argument("--edge-min", type=float, default=0.03)
    r.add_argument("--sigma-floor", type=float, default=8.0e-6)
    r.add_argument("--fee-rate", type=float, default=0.07)
    r.add_argument("--latency-ms", type=int, default=1500)
    r.add_argument("--depth-fraction", type=float, default=1.0)
    r.add_argument("--cap-usd", type=float, default=25.0)
    r.add_argument("--max-book-age", type=float, default=-1, help="seconds; <0 = unlimited")
    r.add_argument("--noncausal", action="store_true", help="THE TRAP: use obs_ts<=t")
    r.add_argument("--sigma-mode", default="grid_ffill", choices=["grid_ffill", "bot"])
    r.add_argument("--strike-mode", default="backfill", choices=["backfill", "exact_else_ffill"])
    r.add_argument("--winner-source", default="result_id", choices=["result_id", "chainlink"])
    r.add_argument("--tau-grid", default=None)
    r.add_argument("--top-of-book", action="store_true")
    r.add_argument("--out", default=None)
    r.add_argument("--label", default="run")
    r.set_defaults(func=cmd_run)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
