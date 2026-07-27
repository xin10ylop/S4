#!/usr/bin/env python3
"""M4 guard 1 measurement: does an unusually LARGE cheap offer near the 1h close
predict a worse outcome than a normal-sized one?

Replays close_snipe at the SHIPPED parameters (bot/config.yaml) over every 1h
window we hold quotes for, and records — for each simulated fill — the size of
the ask level we took together with a scale-free "how big is this offer
relative to what this market normally shows" statistic. Outcome (win/lose,
PnL per share) is then compared across buckets of that statistic.

Shipped parameters replicated here (do not "improve" them — the point is to
measure the bot we actually run):
  tau band [2.5, 5.0]s  -> integer signal seconds tau in {3,4,5}
  edge_min 0.03, price in (0.30, 0.99), vol_window 120s, sigma_1s_floor 8e-6
  fair = Phi(ln(S_t/S_open)/(sigma_1s*sqrt(tau))) clipped to [0.02, 0.98]
  one entry per window, first side that clears
  latency 1500ms -> the fill is evaluated against the book at signal_sec + 2
  (the next full second at or after signal+1.5s), with `fair` FROZEN at signal.

Data: data/data/processed/daily/1h/quotes (top-of-book, 1 row per book update)
      data/data/processed/binance/klines_1s
      data/windows_all.parquet (result_id per window)

Only the Up token is quoted in this dataset, so the Down side is derived the
standard way (px_down = 1 - bid_up, size_down = bid_size_up).

Output: data/c2/m4_depth_fills.parquet  (one row per simulated fill)
        data/c2/m4_depth_evals.parquet  (one row per window we evaluated)
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / "data/data/processed"
OUT = ROOT / "data/c2"

FEE = 0.07
EDGE_MIN = 0.03
PRICE_MIN, PRICE_MAX = 0.30, 0.99
VOL_WINDOW = 120
SIGMA_FLOOR = 8e-6
FAIR_CAP = 0.98
TAUS = (5, 4, 3)          # integer seconds inside the shipped [2.5, 5.0] band
FILL_LAG = 2              # latency_ms 1500 -> next full second at/after +1.5s
CAP_USD = 250.0

# Depth-reference emulation: the live bot only sees the book inside the snipe
# window, so its "recent typical depth" can only be built from the top-of-book
# it observes at those seconds. We emulate exactly that: sample best-ask size
# at the seconds the bot would have fetched (tau in TAUS plus the fill second),
# and keep a rolling deque of the last N such observations for the family.
#
# CRITICAL: depth is violently price-dependent on this book. Measured on the
# last 60 days of 1h quotes, median best-ask size by price bucket is
#   (0,0.1]=24,190   (0.3,0.5]=11.2   (0.5,0.7]=10.6   (0.7,0.9]=19.4
#   (0.9,0.95]=32.3  (0.95,0.99]=37.5 (0.99,1.0]=141.4
# i.e. the ~free "lottery ticket" side of a decided market carries THOUSANDS of
# times the size of a genuinely contested quote. Pooling all observations makes
# the reference meaningless (a normal contested offer scores 0.01x). The
# reference is therefore built ONLY from levels inside the tradeable price band
# — the same band the strategy is allowed to buy in — which is what the shipped
# guard does too.
DEPTH_HISTORY_N = 200
DEPTH_MIN_SAMPLES = 30
DEPTH_SAMPLE_TAUS = (5, 4, 3, 2, 1)


def fee(p):
    return FEE * p * (1.0 - p)


def load_quotes(day: str):
    f = P / f"daily/1h/quotes/{day}.parquet"
    if not f.exists():
        return None
    q = pd.read_parquet(f, columns=["timestamp_us", "bid_price", "bid_size",
                                     "ask_price", "ask_size", "wts"])
    q = q.dropna(subset=["wts"]).astype({"wts": "int64"})
    q["sec"] = q.timestamp_us // 1_000_000
    g = (q.groupby(["wts", "sec"])
           .agg(bid=("bid_price", "last"), ask=("ask_price", "last"),
                bs=("bid_size", "last"), az=("ask_size", "last"))
           .reset_index())
    return g


_KLINE_CACHE: dict = {}


def _kline_day(day: str):
    if day in _KLINE_CACHE:
        return _KLINE_CACHE[day]
    f = P / f"binance/klines_1s/{day}.parquet"
    out = None
    if f.exists():
        k = pd.read_parquet(f, columns=["open_time", "open", "close"])
        k["sec"] = k.open_time // 1_000_000
        out = k[["sec", "open", "close"]]
    if len(_KLINE_CACHE) > 4:
        _KLINE_CACHE.clear()
    _KLINE_CACHE[day] = out
    return out


def load_klines(day: str):
    frames = []
    for dd in [(pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), day,
               (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")]:
        kd = _kline_day(dd)
        if kd is not None:
            frames.append(kd)
    if not frames:
        return None, None
    k = pd.concat(frames).drop_duplicates("sec").set_index("sec").sort_index()
    full = range(int(k.index.min()), int(k.index.max()) + 1)
    kc = k["close"].reindex(full).ffill()
    ko = k["open"].reindex(full)
    ko = ko.fillna(kc.shift(1))
    return kc, ko


def run(days_limit=None, edge_min=EDGE_MIN, taus=TAUS, suffix=""):
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    w1 = wall[wall.family == "1h"].copy()
    days = sorted({f.stem for f in (P / "daily/1h/quotes").glob("*.parquet")})
    if days_limit:
        days = days[:days_limit]

    # rolling depth history, per (family, side) and pooled
    hist = {"up": collections.deque(maxlen=DEPTH_HISTORY_N),
            "down": collections.deque(maxlen=DEPTH_HISTORY_N),
            "pooled": collections.deque(maxlen=DEPTH_HISTORY_N)}

    fills, evals = [], []
    for di, day in enumerate(days):
        wins = w1[w1.date == day]
        if wins.empty:
            continue
        g = load_quotes(day)
        if g is None or g.empty:
            continue
        kc, ko = load_klines(day)
        if kc is None:
            continue
        for _, wrow in wins.sort_values("wts").iterrows():
            wts = int(wrow.wts)
            rid = int(wrow.result_id)
            close_s = wts + 3600
            gw = g[g.wts == wts]
            if gw.empty:
                continue
            lo = close_s - 400
            grid = pd.DataFrame({"sec": np.arange(lo, close_s)})
            grid = pd.merge_asof(grid, gw.drop(columns=["wts"]).sort_values("sec"),
                                 on="sec", direction="backward").set_index("sec")

            S_open = ko.reindex([wts]).values[0]
            if not np.isfinite(S_open):
                continue
            seg = kc.reindex(range(close_s - VOL_WINDOW - 10, close_s + 1))
            r1 = np.log(seg).diff()

            # --- depth samples this window would have contributed (bot's view).
            # Only levels inside the tradeable price band are recorded — see the
            # DEPTH_HISTORY_N comment for why pooling all prices is meaningless.
            win_samples_up, win_samples_dn = [], []
            for tt in DEPTH_SAMPLE_TAUS:
                if (close_s - tt) not in grid.index:
                    continue
                row = grid.loc[close_s - tt]
                if (np.isfinite(row.az) and row.az > 0 and np.isfinite(row.ask)
                        and PRICE_MIN < row.ask < PRICE_MAX):
                    win_samples_up.append(float(row.az))
                pxd = (1.0 - row.bid) if np.isfinite(row.bid) else np.nan
                if (np.isfinite(row.bs) and row.bs > 0 and np.isfinite(pxd)
                        and PRICE_MIN < pxd < PRICE_MAX):
                    win_samples_dn.append(float(row.bs))

            # reference computed from history BEFORE this window (no lookahead)
            def ref(key):
                h = hist[key]
                return float(np.median(h)) if len(h) >= DEPTH_MIN_SAMPLES else float("nan")

            ref_up, ref_dn, ref_pool = ref("up"), ref("down"), ref("pooled")

            # in-window reference: this market's own trailing depth, [-300,-10]s
            seg_g = grid.loc[max(lo, close_s - 300):close_s - 10]
            own_up = seg_g.az[np.isfinite(seg_g.az) & (seg_g.az > 0)]
            own_dn = seg_g.bs[np.isfinite(seg_g.bs) & (seg_g.bs > 0)]
            own_ref_up = float(own_up.median()) if len(own_up) >= 30 else float("nan")
            own_ref_dn = float(own_dn.median()) if len(own_dn) >= 30 else float("nan")

            signal = None
            for tau in taus:
                t = close_s - tau
                if t not in grid.index:
                    continue
                S_t = kc.reindex([t]).values[0]
                if not np.isfinite(S_t):
                    continue
                sd = r1.loc[:t].tail(VOL_WINDOW).std()
                if not np.isfinite(sd):
                    continue
                sigma_1s = max(float(sd), SIGMA_FLOOR)
                sigma = sigma_1s * np.sqrt(tau)
                z = np.log(S_t / S_open) / sigma
                fair_up = float(norm.cdf(z))
                fair_up = min(max(fair_up, 1.0 - FAIR_CAP), FAIR_CAP)
                row = grid.loc[t]
                for side in ("up", "down"):
                    if side == "up":
                        px = row.ask
                        sz = row.az
                        fv = fair_up
                    else:
                        px = (1.0 - row.bid) if np.isfinite(row.bid) else np.nan
                        sz = row.bs
                        fv = 1.0 - fair_up
                    if not (np.isfinite(px) and PRICE_MIN < px < PRICE_MAX):
                        continue
                    if fv - px - fee(px) > edge_min:
                        signal = dict(tau=tau, t=t, side=side, fair=fv,
                                      sig_px=float(px),
                                      sig_size=float(sz) if np.isfinite(sz) else np.nan,
                                      sigma_1s=sigma_1s, s_t=float(S_t),
                                      s_open=float(S_open))
                        break
                if signal:
                    break

            evals.append(dict(day=day, wts=wts, close_s=close_s, result_id=rid,
                              signalled=signal is not None,
                              ref_up=ref_up, ref_dn=ref_dn, ref_pool=ref_pool))

            if signal is not None:
                tf = signal["t"] + FILL_LAG
                if tf in grid.index:
                    rowf = grid.loc[tf]
                    if signal["side"] == "up":
                        pxf, szf = rowf.ask, rowf.az
                        rf, own_rf = ref_up, own_ref_up
                    else:
                        pxf = (1.0 - rowf.bid) if np.isfinite(rowf.bid) else np.nan
                        szf = rowf.bs
                        rf, own_rf = ref_dn, own_ref_dn
                    edge_f = (signal["fair"] - pxf - fee(pxf)
                              if np.isfinite(pxf) else np.nan)
                    filled = bool(np.isfinite(pxf) and PRICE_MIN < pxf < PRICE_MAX
                                  and edge_f > edge_min and np.isfinite(szf) and szf > 0)
                    won = (rid == 0) if signal["side"] == "up" else (rid == 1)
                    shares = min(float(szf), CAP_USD / float(pxf)) if filled else 0.0
                    fills.append(dict(
                        day=day, wts=wts, close_s=close_s, side=signal["side"],
                        tau=signal["tau"], fair=signal["fair"],
                        sig_px=signal["sig_px"], sig_size=signal["sig_size"],
                        fill_px=float(pxf) if np.isfinite(pxf) else np.nan,
                        fill_size=float(szf) if np.isfinite(szf) else np.nan,
                        edge_fill=float(edge_f) if np.isfinite(edge_f) else np.nan,
                        filled=filled, won=bool(won),
                        pnl_per_share=((1.0 if won else 0.0) - float(pxf) - fee(float(pxf)))
                                      if filled else np.nan,
                        shares=shares,
                        depth_ref_family=rf, depth_ref_own=own_rf,
                        depth_ref_pooled=ref_pool,
                        sigma_1s=signal["sigma_1s"], result_id=rid))

            # append this window's depth samples AFTER using the reference
            for v in win_samples_up:
                hist["up"].append(v)
                hist["pooled"].append(v)
            for v in win_samples_dn:
                hist["down"].append(v)
                hist["pooled"].append(v)

        if di % 25 == 0:
            print(f"  ... {day}  windows={len(evals)} fills={len(fills)}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    fdf = pd.DataFrame(fills)
    edf = pd.DataFrame(evals)
    fdf.to_parquet(OUT / f"m4_depth_fills{suffix}.parquet")
    edf.to_parquet(OUT / f"m4_depth_evals{suffix}.parquet")
    print(f"\nwindows evaluated: {len(edf)}  signals: {int(edf.signalled.sum())}  "
          f"fill rows: {len(fdf)}  actually filled: {int(fdf.filled.sum()) if len(fdf) else 0}")
    return fdf, edf


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--edge-min", type=float, default=EDGE_MIN)
    ap.add_argument("--taus", type=str, default="5,4,3")
    ap.add_argument("--suffix", type=str, default="")
    a = ap.parse_args()
    run(a.days, a.edge_min, tuple(int(x) for x in a.taus.split(",")), a.suffix)
