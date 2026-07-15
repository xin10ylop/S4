#!/usr/bin/env python3
"""Per-window Binance (and Chainlink where available) features.

For each window in windows_all.parquet:
- pre-open momentum: log returns over 10s/30s/1m/2m/5m/15m/1h/4h ending at open
- pre-open realized vol of 1s log returns over 1m/5m/15m/1h
- intra-window oracle distance (S_t - S_open) at fractions of life and near close
- Binance candle sign vs result (oracle basis check)

Output: data/features/binance_{family}.parquet
Usage: python scripts/build_binance_features.py 5m
"""
import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "data/data/processed/binance/klines_1s"
CL = ROOT / "data/data/processed/daily/crypto_prices"

RET_HORIZONS = {"r10s": 10, "r30s": 30, "r1m": 60, "r2m": 120, "r5m": 300,
                "r15m": 900, "r1h": 3600, "r4h": 14400}
RV_HORIZONS = {"rv1m": 60, "rv5m": 300, "rv15m": 900, "rv1h": 3600}
LIFE_FRACS = [0.25, 0.5, 0.75, 0.9]
CLOSE_OFFS = [-30, -10, -5, -2, -1, 0]


def load_series(days, source):
    frames = []
    for d in days:
        f = (BIN if source == "bin" else CL) / f"{d}.parquet"
        if not f.exists():
            continue
        if source == "bin":
            df = pd.read_parquet(f, columns=["open_time", "open", "close"])
            df["sec"] = df["open_time"] // 1_000_000
            frames.append(df[["sec", "open", "close"]].rename(
                columns={"close": "px", "open": "px_open"}))
        else:
            df = pd.read_parquet(f, columns=["timestamp_us", "price"])
            df["sec"] = df["timestamp_us"] // 1_000_000
            frames.append(df[["sec", "price"]].rename(columns={"price": "px"}))
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("sec").set_index("sec").sort_index()
    if "px_open" not in df.columns:
        df["px_open"] = df["px"]
    return df


def features_for(wins, px, source):
    """px: DataFrame indexed by unix second with px (close) and px_open columns."""
    lo, hi = int(px.index.min()), int(px.index.max())
    close_s = px["px"].reindex(range(lo, hi + 1)).ffill()
    open_s = px["px_open"].reindex(range(lo, hi + 1))
    # official candle open: open of first kline at that second; fall back to
    # previous close when the second had no kline (no trades -> open = prev close)
    open_s = open_s.fillna(close_s.shift(1))
    lp = np.log(close_s)
    out = pd.DataFrame(index=wins.index)
    w = wins["wts"].values
    dur = wins["duration"].values
    at = lambda ts: lp.reindex(ts).values  # noqa: E731

    lp_open = at(w)  # momentum anchored at last known price entering the window
    for name, h in RET_HORIZONS.items():
        out[name] = lp_open - at(w - h)
    r1 = lp.diff()
    # realized vol via rolling std of 1s returns, sampled at open
    for name, h in RV_HORIZONS.items():
        rv = r1.rolling(h, min_periods=max(10, h // 10)).std() * np.sqrt(h)
        out[name] = rv.reindex(w).values

    if source == "bin":
        # resolution semantics: candle open = open of first 1s kline of window,
        # candle close = close of last 1s kline strictly inside the window
        S_open = open_s.reindex(w).values
        S_close = close_s.reindex(w + dur - 1).values
    else:
        # chainlink semantics: price AT wts and AT wts+dur (verified vs windows table)
        S_open = close_s.reindex(w).values
        S_close = close_s.reindex(w + dur).values
    out["S_open"] = S_open
    for fr in LIFE_FRACS:
        ts = (w + (dur * fr)).astype(int)
        out[f"dist_f{int(fr*100)}"] = close_s.reindex(ts).values - S_open
    for off in CLOSE_OFFS:
        ts = w + dur + off
        tag = f"dist_c{off:+d}".replace("+", "p").replace("-", "m")
        if off == 0:
            out[tag] = S_close - S_open
        else:
            out[tag] = close_s.reindex(ts).values - S_open
    out["bin_up"] = np.where(np.isnan(out["dist_cp0"]), np.nan,
                             (out["dist_cp0"] >= 0).astype(float))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family")
    ap.add_argument("--source", default="bin", choices=["bin", "cl"])
    args = ap.parse_args()

    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    wins = wall[wall.family == args.family].reset_index(drop=True)
    days = sorted({d for d in wins.date})
    # extend day list by 1 back for pre-open lookback
    import datetime as dt
    extra = {(dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat() for d in days}
    px = load_series(sorted(set(days) | extra), args.source)
    f = features_for(wins, px, args.source)
    out = pd.concat([wins[["family", "slug", "wts", "duration", "date", "result_id",
                           "fee_rate"]], f], axis=1)
    dest = ROOT / f"data/features/{'binance' if args.source=='bin' else 'chainlink'}_{args.family}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest, index=False)
    nn = out["bin_up"].notna()
    agree = (out.loc[nn, "bin_up"] == (out.loc[nn, "result_id"] == 0)).mean()
    print(f"wrote {dest} {out.shape}; oracle-sign agreement with result: {agree:.4f} on {nn.sum()} windows")


if __name__ == "__main__":
    main()
