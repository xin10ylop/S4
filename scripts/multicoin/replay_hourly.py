#!/usr/bin/env python3
"""M3: close_snipe replay for the Polymarket HOURLY Up/Down families, all coins.

This is an ADAPTER around the validated harness `scripts/fresh5m/replay.py`.
Every piece of bot logic is IMPORTED from there, not re-derived:

    fee_per_share, normal_cdf, fair_value_up_port, snipe_tau_bounds_port,
    evaluate_close_snipe_port, walk_asks_port, Level, BookTape, _t

so the property-test result in audit/C2_harness.md (`proptest.py`, 0 mismatches
vs bot.polybot.strategy / fill_engine on 80,000 randomised inputs) covers this
script too.  What is NEW here, and only here, is the *data layer*:

  1. UNDERLYING = Binance 1s klines, per coin, per that coin's own resolution
     symbol (audit/M1_multicoin_recon.md §2).  NOT Chainlink.  The hourly
     family settles on the Binance <COIN>/USDT 1H candle.
  2. ANCHORS FROM TRADED PRINTS ONLY.  Binance's 1s archive back-fills silent
     seconds by carrying the previous close at volume=0.  M1 §2.3 showed that
     taking the bar sitting at open_s returns the carried price, not the hour's
     first trade, and flips the settle on near-ties.  Every anchor here is
     taken from `volume > 0` bars.
  3. CAUSAL S_t.  A 1s bar covering second s is only complete at s+1, so at a
     decision instant t the newest price the bot could hold is the close of the
     last volume>0 bar whose bar END is <= t (minus an optional feed latency).
     `tau` is then measured from that print's observation instant to the close,
     exactly as replay.py charges Chainlink's publication lag as risk rather
     than ignoring it.
  4. TWO-BOOK FILL.  Signal at t against the book as of t; fill against the
     book RE-FETCHED at t + latency_ms.  Never the signal book.
  5. BOOK STALENESS.  age = t - the venue's own last-update stamp
     (`timestamp_us` on the Telonex quote tape; M1 §3.2 verified this field
     never moves while the book hash is unchanged, i.e. it is a real
     last-update stamp).  A book older than `max_book_age_s` is handed to the
     evaluator as None -- what the live bot sees when it has no book.
  6. DAY-CLUSTERED t-stats, best-day / best-3-day removal, train/test split.

The hourly quote tape is TOP OF BOOK ONLY (one ask level per side).  Per-share
EV is unaffected by that (a one-level walk fills at the best ask by
construction); $ P&L is a LOWER BOUND.  See §capacity in the report.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts")

# ---- IMPORTED BOT LOGIC (property-tested in scripts/fresh5m/proptest.py) ----
from fresh5m.replay import (  # noqa: E402
    INF,
    Level,
    BookTape,
    fee_per_share,
    evaluate_close_snipe_port,
    snipe_tau_bounds_port,
    walk_asks_port,
    _t,
)

DATA = f"{ROOT}/data/multicoin"

COINS: Dict[str, Tuple[str, str]] = {
    "bitcoin": ("BTCUSDT", "spot"),
    "ethereum": ("ETHUSDT", "spot"),
    "solana": ("SOLUSDT", "spot"),
    "xrp": ("XRPUSDT", "spot"),
    "dogecoin": ("DOGEUSDT", "spot"),
    "bnb": ("BNBUSDT", "spot"),
    "hype": ("HYPEUSDT", "futures"),
}


# ==========================================================================
# PARAMETERS  (defaults = bot/config.yaml, shipped)
# ==========================================================================

@dataclass
class HParams:
    # --- shipped strategy params -------------------------------------------
    edge_min: float = 0.03
    price_min: float = 0.30
    price_max: float = 0.99
    vol_window_secs: float = 120.0
    sigma_1s_floor: float = 8.0e-6
    fair_cap: float = 0.98
    snipe_last_secs: float = 5.0
    snipe_min_tau_secs: float = 2.5
    snipe_fill_margin_secs: float = 0.5
    # --- execution / sizing -------------------------------------------------
    fee_rate: float = 0.07
    latency_ms: int = 1500
    depth_fraction: float = 1.0
    max_walk_above_best: float = 0.03
    per_event_cap_usd: float = 250.0        # bot/config.yaml sizing
    # --- staleness / causality ---------------------------------------------
    max_book_age_s: float = INF
    feed_lag_ms: int = 0                    # extra underlying-feed latency (stress)
    max_underlying_age_s: float = INF       # gate on last-trade age (stress)
    tau_from_print: bool = True             # charge print staleness into tau
    # --- conventions --------------------------------------------------------
    sigma_min_periods: int = 30
    # --- CANDIDATE GUARDS (not shipped; quantified on TRAIN only) ----------
    z_min: float = 0.0                      # skip ticks the model is unsure about
    z_max: float = INF                      # skip the over-confident regime
    winner_source: str = "result_id"        # "result_id" | "binance"
    tick_hz: float = 1.0
    tau_grid: Optional[Sequence[float]] = None

    def cfg(self) -> dict:
        return dict(edge_min=self.edge_min, price_min=self.price_min,
                    price_max=self.price_max, sigma_1s_floor=self.sigma_1s_floor,
                    fair_cap=self.fair_cap, snipe_last_secs=self.snipe_last_secs,
                    snipe_min_tau_secs=self.snipe_min_tau_secs,
                    snipe_fill_margin_secs=self.snipe_fill_margin_secs)

    def taus(self) -> List[float]:
        """Decision instants (seconds-to-close), EARLIEST FIRST -- the bot ticks
        forward in time and takes the first qualifying signal."""
        if self.tau_grid is not None:
            return list(self.tau_grid)
        lo, hi = snipe_tau_bounds_port(self.cfg(), self.latency_ms)
        step = 1.0 / self.tick_hz
        n = int(math.floor((hi - lo) / step + 1e-9))
        return [round(hi - i * step, 6) for i in range(n + 1)]


# ==========================================================================
# UNDERLYING FEED
# ==========================================================================

class BinanceFeed:
    """Binance 1s klines for ONE symbol, with an honest "what did the bot hold
    at wall-clock t" view.

    Grid convention.  The bot polls `/api/v3/ticker/price` at ~1 Hz and feeds
    `rolling_log_return_std` with whatever it got, INCLUDING repeats when no
    trade happened (`oracle.py:BinanceOracle.rolling_log_return_std`).  The
    faithful offline analogue is therefore a forward-filled 1-second grid of
    last-trade price -- silent seconds contribute a zero return, which is
    precisely the "quiet polling underestimates vol" effect that
    `sigma_1s_floor` exists to catch.
    """

    def __init__(self, k: pd.DataFrame, vol_window_secs: float = 120.0,
                 min_periods: int = 30):
        k = k.sort_values("ts_ms")
        traded = k[k.volume > 0]
        if traded.empty:
            raise ValueError("no traded bars")
        self.t_sec = (traded.ts_ms.to_numpy(np.int64) // 1000)     # bar START second
        self.t_open = traded.open.to_numpy(float)                  # first trade in that second
        self.t_close = traded.close.to_numpy(float)                # last  trade in that second
        # ---- 1s ffilled grid of last-trade price (bot's polled series) -----
        lo, hi = int(self.t_sec[0]), int(self.t_sec[-1])
        grid = np.arange(lo, hi + 1, dtype=np.int64)
        s = pd.Series(self.t_close, index=self.t_sec)
        s = s.groupby(level=0).last().reindex(grid).ffill()
        lp = np.log(s.to_numpy(float))
        r = pd.Series(lp).diff()
        w = int(vol_window_secs)
        self.sig_sec = grid
        self.sig_val = r.rolling(w, min_periods=min_periods).std(ddof=1).to_numpy()

    # ---------------- anchors ------------------------------------------------
    def hour_open(self, open_s: int, close_s: int) -> Optional[float]:
        """Binance 1H candle open = FIRST TRADED price in [open_s, close_s)."""
        i = int(np.searchsorted(self.t_sec, open_s, "left"))
        if i >= len(self.t_sec) or self.t_sec[i] >= close_s:
            return None
        return float(self.t_open[i])

    def hour_close(self, open_s: int, close_s: int) -> Optional[float]:
        """Binance 1H candle close = LAST TRADED price in [open_s, close_s)."""
        j = int(np.searchsorted(self.t_sec, close_s, "left")) - 1
        if j < 0 or self.t_sec[j] < open_s:
            return None
        return float(self.t_close[j])

    # ---------------- causal live view --------------------------------------
    def held_at(self, t_us: int, feed_lag_us: int = 0) -> Tuple[Optional[float], Optional[int]]:
        """Newest price the bot could hold at wall-clock t_us.

        A 1s bar covering second s is only COMPLETE at s+1, so the last trade
        we can be sure has already printed is the close of the newest volume>0
        bar with (s + 1) * 1e6 <= t_us - feed_lag.  Returns (price, obs_us)
        where obs_us is the END of that bar, i.e. the latest instant the trade
        could have occurred -- the conservative observation stamp.
        """
        cut = (t_us - feed_lag_us) // 1_000_000            # seconds
        j = int(np.searchsorted(self.t_sec, cut - 1, "right")) - 1
        if j < 0:
            return None, None
        return float(self.t_close[j]), int((self.t_sec[j] + 1) * 1_000_000)

    def sigma_at(self, t_us: int) -> float:
        """sigma_1s the bot would have computed at t: rolling std over the
        vol window ending at the last FULLY COMPLETE second before t."""
        sec = (t_us // 1_000_000) - 1
        i = int(np.searchsorted(self.sig_sec, sec, "right")) - 1
        if i < 0:
            return float("nan")
        return float(self.sig_val[i])


def load_binance(symbol: str, days: Sequence[str]) -> Optional[pd.DataFrame]:
    fr = []
    for d in days:
        p = f"{DATA}/binance/{symbol}/{d}.parquet"
        if os.path.exists(p):
            fr.append(pd.read_parquet(p))
    if not fr:
        return None
    return pd.concat(fr, ignore_index=True)


# ==========================================================================
# BOOK TAPE  (Telonex hourly quotes: TOP OF BOOK, both outcomes)
# ==========================================================================

@dataclass
class HWindow:
    coin: str
    day: str
    open_s: int
    close_s: int
    result_id: Optional[int]
    up: BookTape
    dn: BookTape


def _empty_tape() -> BookTape:
    return BookTape(np.zeros(0, np.int64), np.zeros((0, 1)), np.zeros((0, 1)))


def load_coin_windows(coin: str, days: Optional[Sequence[str]] = None) -> List[HWindow]:
    files = sorted(glob.glob(f"{DATA}/quotes/{coin}/*.parquet"))
    if not files:
        return []
    q = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    # DETERMINISTIC ORDER.  The tape carries duplicate `timestamp_us` values
    # (several book updates published inside the same microsecond stamp), so an
    # unstable sort makes "the last update at or before t" ambiguous and two
    # correct implementations can pick different books.  Break the tie on the
    # vendor's own capture instant, stably.
    q = q.sort_values(["timestamp_us", "local_timestamp_us"], kind="mergesort")
    mk = pd.read_parquet(f"{DATA}/markets.parquet")
    mk = mk[(mk.coin == coin) & (mk.status == "resolved")]
    meta = {int(r.close_s): (int(r.open_s), int(r.result_id))
            for r in mk.itertuples() if str(r.result_id) in ("0", "1")}

    out: List[HWindow] = []
    for close_s, g in q.groupby("close_s", sort=True):
        close_s = int(close_s)
        if close_s not in meta:
            continue
        open_s, rid = meta[close_s]
        day = pd.Timestamp(close_s, unit="s", tz="UTC").strftime("%Y-%m-%d")
        if days is not None and day not in days:
            continue
        tapes = {}
        for oid in (0, 1):
            gg = g[g.oid == oid]
            if gg.empty:
                tapes[oid] = _empty_tape()
                continue
            # PRICE PRECISION.  The tape stores prices as float32, so a quote of
            # exactly 0.30 comes back as 0.30000001192092896 in float64 and
            # sneaks past the bot's STRICT `price_min < ask` guard.  The live
            # bot parses JSON decimals into float64, where 0.3 is 0.3 and the
            # guard rejects it.  Three of 176 fills in the 81-day sample were
            # admitted by that artifact alone and they carried -$194.7 of P&L.
            # Snap back to the venue's decimal grid (tick is 0.001 or 0.01,
            # M1 section 2.2) before anything compares against a threshold.
            tapes[oid] = BookTape(
                gg.timestamp_us.to_numpy(np.int64),
                np.round(gg.ask_price.to_numpy(float), 4)[:, None],
                gg.ask_size.to_numpy(float)[:, None])
        out.append(HWindow(coin=coin, day=day, open_s=open_s, close_s=close_s,
                           result_id=rid, up=tapes[0], dn=tapes[1]))
    return out


# ==========================================================================
# THE REPLAY
# ==========================================================================

def run_window(w: HWindow, feed: BinanceFeed, p: HParams) -> Optional[dict]:
    """Mirrors engine._maybe_snipe: tick from the early end of the tau band
    toward the close, ONE entry per window, first qualifying side."""
    cfg = p.cfg()
    close_s = w.close_s
    lat_us = int(p.latency_ms * 1000)
    feed_lag_us = int(p.feed_lag_ms * 1000)

    S_open = feed.hour_open(w.open_s, close_s)
    if S_open is None or not math.isfinite(S_open) or S_open <= 0:
        return None

    if p.winner_source == "binance":
        c = feed.hour_close(w.open_s, close_s)
        if c is None:
            return None
        up_won = bool(c >= S_open)
    else:
        if w.result_id is None:
            return None
        up_won = (w.result_id == 0)          # markets.parquet: outcome_0 == "Up"

    for tau_nom in p.taus():
        t_us = int(round((close_s - tau_nom) * 1e6))
        S_t, obs_us = feed.held_at(t_us, feed_lag_us)
        if S_t is None or not math.isfinite(S_t) or S_t <= 0:
            continue
        und_age = (t_us - obs_us) / 1e6
        if und_age > p.max_underlying_age_s:
            continue
        sigma = feed.sigma_at(t_us)
        if not math.isfinite(sigma) or sigma <= 0:
            continue
        tau_eff = ((close_s * 1e6) - obs_us) / 1e6 if p.tau_from_print else tau_nom
        if tau_eff <= 0:
            continue

        if p.z_min > 0.0 or math.isfinite(p.z_max):
            zz = abs(math.log(S_t / S_open)) / (max(sigma, p.sigma_1s_floor)
                                                * math.sqrt(tau_eff))
            if zz < p.z_min or zz > p.z_max:
                continue

        lv_up, age_up = w.up.at(t_us)
        lv_dn, age_dn = w.dn.at(t_us)
        a_up = None if (age_up > p.max_book_age_s or lv_up is None) else lv_up
        a_dn = None if (age_dn > p.max_book_age_s or lv_dn is None) else lv_dn

        sig = evaluate_close_snipe_port(
            close_ts=close_s, now_ts=close_s - tau_eff,
            S_t=float(S_t), S_open=float(S_open), sigma_1s=float(sigma),
            asks_up=a_up, asks_down=a_dn, cfg=cfg, fee_rate=p.fee_rate)
        if sig is None:
            continue

        # ---- SIGNAL FIRES ---------------------------------------------------
        tape = w.up if sig.side == "up" else w.dn
        age_sig = age_up if sig.side == "up" else age_dn
        sig_lv = lv_up if sig.side == "up" else lv_dn
        fill_lv, age_fil = tape.at(t_us + lat_us)     # RE-FETCH after latency
        fair = sig.fair

        def edge_fn(q: float, _f=fair) -> float:
            return _f - q - fee_per_share(q, p.fee_rate)

        sig_notional = (sig_lv[0].price * sig_lv[0].size) if sig_lv else np.nan
        rec = dict(coin=w.coin, day=w.day, close_s=close_s, tau=tau_nom,
                   tau_eff=tau_eff, side=sig.side, fair=fair, sig_ask=sig.ask,
                   edge_sig=sig.edge, S_open=float(S_open), S_t=float(S_t),
                   sigma=float(sigma), und_age=und_age,
                   z=math.log(S_t / S_open) / (max(sigma, p.sigma_1s_floor) * math.sqrt(tau_eff)),
                   ret_bp=1e4 * math.log(S_t / S_open),
                   sig_notional=sig_notional,
                   book_age_sig=age_sig, book_age_fil=age_fil,
                   up_won=up_won, won=(up_won if sig.side == "up" else (not up_won)),
                   shares=0.0, avg_price=np.nan, fill_ask=np.nan,
                   fill_notional=np.nan, pnl_per_share=np.nan, pnl=0.0,
                   n_levels=0, outcome="filled")

        if age_fil > p.max_book_age_s:
            rec["outcome"] = "stale_book"
            return rec
        if fill_lv is None:
            rec["outcome"] = "empty_book"
            return rec
        rec["fill_ask"] = fill_lv[0].price
        rec["fill_notional"] = fill_lv[0].price * fill_lv[0].size
        ladder = fill_lv
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


class CoinTape:
    """Loaded, reusable tape for one coin (windows + underlying feed)."""

    def __init__(self, coin: str, vol_window_secs: float = 120.0,
                 min_periods: int = 30, verbose: bool = True):
        self.coin = coin
        sym, _ = COINS[coin]
        self.symbol = sym
        bdays = sorted(os.path.basename(f)[:-8]
                       for f in glob.glob(f"{DATA}/binance/{sym}/*.parquet"))
        k = load_binance(sym, bdays)
        if k is None:
            raise ValueError(f"no binance data for {sym}")
        self.feed = BinanceFeed(k, vol_window_secs, min_periods)
        self.b_lo = int(k.ts_ms.min() // 1000)
        self.b_hi = int(k.ts_ms.max() // 1000)
        allw = load_coin_windows(coin)
        # keep only closes whose whole hour + vol window is inside the underlying
        self.windows = [w for w in allw
                        if w.open_s >= self.b_lo and w.close_s <= self.b_hi + 1]
        self.days = sorted({w.day for w in self.windows})
        if verbose:
            print(f"  {coin}: {len(self.windows)} closes over {len(self.days)} days "
                  f"({self.days[0] if self.days else '-'} .. "
                  f"{self.days[-1] if self.days else '-'})", flush=True)

    def run(self, p: HParams, days: Optional[Sequence[str]] = None) -> Tuple[pd.DataFrame, dict]:
        ws = self.windows if days is None else [w for w in self.windows if w.day in set(days)]
        recs = [r for w in ws if (r := run_window(w, self.feed, p)) is not None]
        dd = sorted({w.day for w in ws})
        return pd.DataFrame(recs), dict(windows=len(ws), days=len(dd), day_list=dd)


# ==========================================================================
# STATISTICS
# ==========================================================================

def summarise(tr: pd.DataFrame, n_days: int, n_windows: int, label: str = "",
              post_max_book_age_s: float = INF, cap_usd: float = 250.0) -> dict:
    out = dict(label=label, days=n_days, closes=n_windows)
    if tr is None or tr.empty:
        return {**out, "signals": 0, "fills": 0, "ev_share": np.nan,
                "trades_day": 0.0, "t_trade": np.nan, "t_day": np.nan,
                "win": np.nan, "pnl_day": 0.0, "book_moved": 0,
                "empty_book": 0, "stale_book_blocked": 0, "no_fill": 0}
    f = tr[tr.outcome == "filled"].copy()
    n_all, pnl_all = len(f), f.pnl.sum()
    if math.isfinite(post_max_book_age_s):
        stale = ((f.book_age_sig > post_max_book_age_s)
                 | (f.book_age_fil > post_max_book_age_s))
        out["stale_fills_removed"] = int(stale.sum())
        out["stale_pnl_frac"] = float(f.pnl[stale].sum() / pnl_all) if pnl_all else np.nan
        f = f[~stale]
    ps = f.pnl_per_share.to_numpy(float)
    daily_ps = f.groupby("day").pnl_per_share.mean().to_numpy(float)
    daily_pnl = f.groupby("day").pnl.sum().reindex(
        pd.Index(sorted(set(tr.day)))).fillna(0.0) if len(f) else pd.Series(dtype=float)
    out.update(
        signals=len(tr), fills=len(f), fills_before=n_all,
        trades_day=len(f) / n_days if n_days else np.nan,
        signals_day=len(tr) / n_days if n_days else np.nan,
        ev_share=float(ps.mean()) if len(ps) else np.nan,
        sd_share=float(ps.std(ddof=1)) if len(ps) > 1 else np.nan,
        median_share=float(np.median(ps)) if len(ps) else np.nan,
        win=float(f.won.mean()) if len(f) else np.nan,
        t_trade=_t(ps), t_day=_t(daily_ps), n_day_clusters=len(daily_ps),
        pnl=float(f.pnl.sum()), shares=float(f.shares.sum()),
        deployed=float((f.avg_price * f.shares).sum()),
        pnl_day=float(f.pnl.sum() / n_days) if n_days else np.nan,
        shares_per_fill=float(f.shares.mean()) if len(f) else np.nan,
        notional_per_fill=float((f.avg_price * f.shares).mean()) if len(f) else np.nan,
        no_fill=int((tr.outcome != "filled").sum()),
        stale_book_blocked=int((tr.outcome == "stale_book").sum()),
        book_moved=int((tr.outcome == "book_moved_no_edge").sum()),
        empty_book=int((tr.outcome == "empty_book").sum()),
        up_frac=float((f.side == "up").mean()) if len(f) else np.nan,
        cap_usd=cap_usd,
    )
    # 95% CI on EV/share, day-clustered
    if len(daily_ps) > 1:
        se = daily_ps.std(ddof=1) / math.sqrt(len(daily_ps))
        out["ev_ci_lo"] = float(daily_ps.mean() - 1.96 * se)
        out["ev_ci_hi"] = float(daily_ps.mean() + 1.96 * se)
    # best-day / best-3-day removal (on total P&L by day)
    if len(f):
        dp = f.groupby("day").pnl.sum().sort_values(ascending=False)
        dps = f.groupby("day")
        for k, key in ((1, "drop1"), (3, "drop3")):
            bad = set(dp.index[:k])
            g = f[~f.day.isin(bad)]
            nd = max(n_days - len(bad), 1)
            out[f"ev_{key}"] = float(g.pnl_per_share.mean()) if len(g) else np.nan
            out[f"pnl_day_{key}"] = float(g.pnl.sum() / nd)
            out[f"t_day_{key}"] = _t(g.groupby("day").pnl_per_share.mean().to_numpy(float)) \
                if len(g) else np.nan
        _ = dps
    return out


def fmt_table(rows: List[dict], cols: Sequence[str]) -> str:
    d = pd.DataFrame(rows)
    keep = [c for c in cols if c in d.columns]
    return d[keep].to_string(index=False, float_format=lambda v: f"{v:,.4f}")


# ==========================================================================
# CLI
# ==========================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--max-book-age", type=float, default=INF)
    ap.add_argument("--cap-usd", type=float, default=250.0)
    ap.add_argument("--edge-min", type=float, default=0.03)
    ap.add_argument("--max-walk-above-best", type=float, default=0.03,
                    help="cents above best ask the fill walk may climb; the measured\n                          binding constraint on P&L (docs/11_deep_fills.md)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    p = HParams(max_book_age_s=a.max_book_age, per_event_cap_usd=a.cap_usd,
                edge_min=a.edge_min, max_walk_above_best=a.max_walk_above_best)
    rows, trs = [], []
    for coin in [c for c in a.coins.split(",") if c in COINS]:
        tape = CoinTape(coin)
        tr, meta = tape.run(p)
        if not tr.empty:
            tr["coin"] = coin
            trs.append(tr)
        rows.append({**summarise(tr, meta["days"], meta["windows"], coin,
                                 cap_usd=a.cap_usd), "coin": coin})
    print(fmt_table(rows, ["coin", "days", "closes", "signals", "fills", "book_moved",
                           "empty_book", "ev_share", "win", "trades_day",
                           "t_trade", "t_day", "pnl_day"]))
    if a.out and trs:
        pd.concat(trs, ignore_index=True).to_parquet(a.out)
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
