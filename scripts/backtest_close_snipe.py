#!/usr/bin/env python3
"""Strict close-snipe + settlement backtest for 5m/15m/4h (Chainlink families).

Signal: real-time Binance 1s (visible). Books: bookcurves 1s grid.
Outcome: actual result_id. Entries in the last SNIPE_LAST seconds; settlement
buys of the (Chainlink-determined) winner from close+2s, once the Chainlink
close print would be visible (~1.3s publish lag -> we use +2s).

Settlement winner is decided by the *actual* result (no basis error) but only
tradeable when our Chainlink feed (crypto_prices) covers the day; before Apr 2
we fall back to Binance sign and eat the basis risk as real losses.
"""
import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data/data/processed"
sys.path.insert(0, str(ROOT))

FEE = 0.07
EDGE_MIN_A = 0.05
EDGE_MIN_B = 0.02
SNIPE_LAST = 6
SETTLE_MAX = 120
DOLLARS = 10.0
CAP_B = 500
USE_CL_SIGNAL = False


def fee(p):
    return FEE * p * (1 - p)


def load_books(fam, day):
    f = P / f"daily/{fam}/bookcurves/{day}.parquet"
    if not f.exists():
        return None
    b = pd.read_parquet(f, columns=["timestamp_us", "bid_p0", "ask_p0", "bid_s0", "ask_s0", "wts"])
    b = b.dropna(subset=["wts"]).astype({"wts": "int64"})
    b["sec"] = b.timestamp_us // 1_000_000
    return b.groupby(["wts", "sec"]).agg(bid=("bid_p0", "last"), ask=("ask_p0", "last"),
                                         bs=("bid_s0", "last"), az=("ask_s0", "last")).reset_index()


def load_binance(day):
    frames = []
    for dd in [(pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), day,
               (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")]:
        ff = P / f"binance/klines_1s/{dd}.parquet"
        if ff.exists():
            k = pd.read_parquet(ff, columns=["open_time", "close"])
            k["sec"] = k.open_time // 1_000_000
            frames.append(k[["sec", "close"]])
    if not frames:
        return None
    k = pd.concat(frames).drop_duplicates("sec").set_index("sec")["close"].sort_index()
    return k.reindex(range(int(k.index.min()), int(k.index.max()) + 1)).ffill()


def load_chainlink(day):
    f = P / f"daily/crypto_prices/{day}.parquet"
    if not f.exists():
        return None
    c = pd.read_parquet(f, columns=["timestamp_us", "price"])
    c["sec"] = c.timestamp_us // 1_000_000
    s = c.drop_duplicates("sec").set_index("sec")["price"].sort_index()
    return s.reindex(range(int(s.index.min()), int(s.index.max()) + 1)).ffill()


def run_day(fam, day, wall):
    wins = wall[(wall.family == fam) & (wall.date == day)]
    if wins.empty:
        return [], []
    g = load_books(fam, day)
    kc = load_binance(day)
    if g is None or kc is None:
        return [], []
    cl = load_chainlink(day)
    trades_a, trades_b = [], []
    for _, wrow in wins.iterrows():
        wts, dur, rid = int(wrow.wts), int(wrow.duration), int(wrow.result_id)
        close_s = wts + dur
        gw = g[g.wts == wts]
        if gw.empty:
            continue
        grid = pd.DataFrame({"sec": np.arange(close_s - SNIPE_LAST - 5, close_s + SETTLE_MAX)})
        grid = pd.merge_asof(grid, gw.drop(columns=["wts"]).sort_values("sec"),
                             on="sec", direction="backward").set_index("sec")
        # strike: chainlink at open when available, else binance
        S_open = cl.reindex([wts]).values[0] if cl is not None else np.nan
        if not np.isfinite(S_open):
            S_open = kc.reindex([wts]).values[0]
        if not np.isfinite(S_open):
            continue
        sig_src = cl if (USE_CL_SIGNAL and cl is not None) else kc
        sig_lag = 2 if (USE_CL_SIGNAL and cl is not None) else 0
        seg = sig_src.reindex(range(close_s - SNIPE_LAST - 130 - sig_lag, close_s + 1))
        r1 = np.log(seg).diff()

        # --- A) close snipe, last SNIPE_LAST seconds, first passage
        done_a = False
        for t in range(close_s - SNIPE_LAST, close_s - 1):
            if done_a:
                break
            # visible price: chainlink print for t-2 (publish lag) or binance at t
            S_t = sig_src.reindex([t - sig_lag]).values[0]
            tau = close_s - t + sig_lag  # uncertainty horizon includes signal staleness
            sig = r1.loc[:t - sig_lag].tail(120).std() * np.sqrt(tau)
            if not np.isfinite(sig) or sig <= 0 or not np.isfinite(S_t):
                continue
            z = np.log(S_t / S_open) / sig
            fair = norm.cdf(z)
            if t + 1 not in grid.index:
                continue
            row = grid.loc[t + 1]
            for side in ("up", "down"):
                px = row.ask if side == "up" else (1 - row.bid if pd.notna(row.bid) else np.nan)
                szq = row.az if side == "up" else row.bs
                fv = fair if side == "up" else 1 - fair
                if not (np.isfinite(px) and 0.30 < px < 0.99):
                    continue
                if fv - px - fee(px) > EDGE_MIN_A:
                    shares = min(szq, DOLLARS / px)
                    won = (rid == 0) if side == "up" else (rid == 1)
                    pnl = (1.0 if won else 0.0) - px - fee(px)
                    trades_a.append((day, wts, t - close_s, side, px, shares, fv, pnl))
                    done_a = True
                    break

        # --- B) settlement: winner per visible feed at +2s
        if cl is not None:
            S_close = cl.reindex([close_s]).values[0]
        else:
            S_close = kc.reindex([close_s - 1]).values[0]
        if not np.isfinite(S_close):
            continue
        win_up = S_close >= S_open
        won_matches = (rid == 0) == win_up
        bought = 0.0
        seed = grid.loc[close_s + 2:close_s + 2]
        ev_rows = gw[(gw.sec > close_s + 2) & (gw.sec <= close_s + SETTLE_MAX)].copy()
        if win_up:
            chg = (ev_rows.ask.ne(ev_rows.ask.shift()) | ev_rows.az.ne(ev_rows.az.shift()))
        else:
            chg = (ev_rows.bid.ne(ev_rows.bid.shift()) | ev_rows.bs.ne(ev_rows.bs.shift()))
        ev_rows = pd.concat([seed.reset_index().rename(columns={"index": "sec"}),
                             ev_rows[chg.fillna(True)]], ignore_index=True)
        for _, row in ev_rows.iterrows():
            if bought >= CAP_B:
                break
            px = row.ask if win_up else (1 - row.bid if pd.notna(row.bid) else np.nan)
            szq = row.az if win_up else row.bs
            if not (np.isfinite(px) and 0.02 < px < 0.995):
                continue
            if 1 - px - fee(px) > EDGE_MIN_B:
                shares = float(min(szq, CAP_B - bought))
                if shares <= 0:
                    continue
                pnl = (1.0 if won_matches else 0.0) - px - fee(px)
                trades_b.append((day, wts, int(row.sec) - close_s,
                                 "up" if win_up else "down", px, shares, pnl))
                bought += shares
    return trades_a, trades_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family")
    ap.add_argument("--from-date", required=True)
    ap.add_argument("--to-date", required=True)
    ap.add_argument("--tag", default="train")
    ap.add_argument("--cl-signal", action="store_true")
    args = ap.parse_args()
    global USE_CL_SIGNAL
    USE_CL_SIGNAL = args.cl_signal
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    days = sorted(Path(f).stem for f in glob.glob(str(P / f"daily/{args.family}/bookcurves/*.parquet")))
    days = [d for d in days if args.from_date <= d <= args.to_date]
    A, B = [], []
    for i, d in enumerate(days):
        a, b = run_day(args.family, d, wall)
        A += a
        B += b
        if (i + 1) % 20 == 0:
            print(f"{i+1}/{len(days)} days, A={len(A)} B={len(B)}", flush=True)
    ta = pd.DataFrame(A, columns=["date", "wts", "t_rel", "side", "px", "shares", "fair", "pnl_share"])
    tb = pd.DataFrame(B, columns=["date", "wts", "t_rel", "side", "px", "shares", "pnl_share"])
    ta.to_csv(ROOT / f"results/bt_{args.family}_snipe_{args.tag}.csv", index=False)
    tb.to_csv(ROOT / f"results/bt_{args.family}_settle_{args.tag}.csv", index=False)
    for name, t in [("SNIPE", ta), ("SETTLE", tb)]:
        if len(t) == 0:
            print(name, "no trades")
            continue
        print(f"{name}: n={len(t)} ({len(t)/len(days):.2f}/day) EV={t.pnl_share.mean()*100:.2f}c/share "
              f"wr={(t.pnl_share>0).mean():.3f} med_px={t.px.median():.3f} med_sh={t.shares.median():.0f}")


if __name__ == "__main__":
    main()
