#!/usr/bin/env python3
"""Strict tape-level backtest of the two 1h strategies.

A) close-snipe: in [close-K, close-2]s, fair = Phi(z) from *visible* Binance 1s;
   buy the side whose (fair - ask - fee) > edge_min. Fill against the book one
   second AFTER the signal second (latency >= 1s). One entry per window/side.
   Hold to resolution.
B) settlement: in [close+2, close+SETTLE_MAX]s, outcome already determined by
   Binance candle; buy winner at ask while (1 - ask - fee) > edge_min. Fill one
   second after signal. Cap shares per window.

Fees: taker 0.07 * p * (1-p). Sizing: min(ask_size, dollars/price).
Outputs per-trade CSVs under results/.
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
SNIPE_FROM = -6  # last seconds only: tau small, model risk minimal
SETTLE_MAX = 300
DOLLARS = 10.0


def fee(p):
    return FEE * p * (1 - p)


def load_day(day, wall):
    wins = wall[(wall.family == "1h") & (wall.date == day)]
    if wins.empty:
        return None
    f = P / f"daily/1h/quotes/{day}.parquet"
    if not f.exists():
        return None
    q = pd.read_parquet(f).dropna(subset=["wts"]).astype({"wts": "int64"})
    q["sec"] = q.timestamp_us // 1_000_000
    g = q.groupby(["wts", "sec"]).agg(bid=("bid_price", "last"), ask=("ask_price", "last"),
                                      bs=("bid_size", "last"), az=("ask_size", "last")).reset_index()
    frames = []
    for dd in [day, (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
               (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")]:
        ff = P / f"binance/klines_1s/{dd}.parquet"
        if ff.exists():
            k = pd.read_parquet(ff, columns=["open_time", "open", "close"])
            k["sec"] = k.open_time // 1_000_000
            frames.append(k[["sec", "open", "close"]])
    k = pd.concat(frames).drop_duplicates("sec").set_index("sec").sort_index()
    kc = k["close"].reindex(range(int(k.index.min()), int(k.index.max()) + 1)).ffill()
    ko = k["open"].reindex(range(int(k.index.min()), int(k.index.max()) + 1))
    ko = ko.fillna(kc.shift(1))
    return wins, g, kc, ko


def run_day(day, wall):
    loaded = load_day(day, wall)
    if loaded is None:
        return [], []
    wins, g, kc, ko = loaded
    trades_a, trades_b = [], []
    for _, wrow in wins.iterrows():
        wts, dur, rid = int(wrow.wts), int(wrow.duration), int(wrow.result_id)
        close_s = wts + dur
        gw = g[g.wts == wts]
        if gw.empty:
            continue
        # full-second book grid across snipe + settle horizon
        grid = pd.DataFrame({"sec": np.arange(close_s + SNIPE_FROM - 5, close_s + SETTLE_MAX)})
        grid = pd.merge_asof(grid, gw.drop(columns=["wts"]).sort_values("sec"),
                             on="sec", direction="backward")
        grid = grid.set_index("sec")
        S_open = ko.reindex([wts]).values[0]
        if np.isnan(S_open):
            continue
        # realized vol of 1s returns over 120s before signal, per second
        win_lo = close_s + SNIPE_FROM - 130
        seg = kc.reindex(range(win_lo, close_s + 1))
        r1 = np.log(seg).diff()

        # --- A) close-snipe
        done_a = False
        for t in range(close_s + SNIPE_FROM, close_s - 1):
            if done_a:
                break
            S_t = kc.reindex([t]).values[0]
            tau = close_s - t
            sig = r1.loc[:t].tail(120).std() * np.sqrt(tau)
            if not np.isfinite(sig) or sig <= 0 or not np.isfinite(S_t):
                continue
            z = np.log(S_t / S_open) / sig
            fair = norm.cdf(z)
            row = grid.loc[t + 1] if t + 1 in grid.index else None  # 1s latency fill
            if row is None:
                continue
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

        # --- B) settlement buy of known winner
        # iterate over actual quote EVENTS post-close; each standing book state
        # can be consumed at most once (we take it, it's gone)
        S_close = kc.reindex([close_s - 1]).values[0]
        if not np.isfinite(S_close):
            continue
        win_up = S_close >= S_open  # binance candle rule
        won_side_matches = (rid == 0) == win_up  # sanity (should be ~always true)
        bought = 0.0
        # standing book at close+2 (hittable once) + subsequent fresh events
        seed = grid.loc[close_s + 2:close_s + 2]
        ev_rows = gw[(gw.sec > close_s + 2) & (gw.sec <= close_s + SETTLE_MAX)].copy()
        if win_up:
            chg = (ev_rows.ask.ne(ev_rows.ask.shift()) | ev_rows.az.ne(ev_rows.az.shift()))
        else:
            chg = (ev_rows.bid.ne(ev_rows.bid.shift()) | ev_rows.bs.ne(ev_rows.bs.shift()))
        ev_rows = pd.concat([seed.reset_index().rename(columns={"index": "sec"}),
                             ev_rows[chg.fillna(True)]], ignore_index=True)
        for _, row in ev_rows.iterrows():
            if bought >= 500:
                break
            px = row.ask if win_up else (1 - row.bid if pd.notna(row.bid) else np.nan)
            szq = row.az if win_up else row.bs
            if not (np.isfinite(px) and 0.02 < px < 0.995):
                continue
            if 1 - px - fee(px) > EDGE_MIN_B:
                shares = float(min(szq, 500 - bought))
                if shares <= 0:
                    continue
                pnl = (1.0 if won_side_matches else 0.0) - px - fee(px)
                trades_b.append((day, wts, int(row.sec) - close_s,
                                 "up" if win_up else "down", px, shares, pnl))
                bought += shares
    return trades_a, trades_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2025-10-11")
    ap.add_argument("--to-date", default="2026-04-30")
    ap.add_argument("--tag", default="train")
    args = ap.parse_args()
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    days = sorted(Path(f).stem for f in glob.glob(str(P / "daily/1h/quotes/*.parquet")))
    days = [d for d in days if args.from_date <= d <= args.to_date]
    A, B = [], []
    for i, d in enumerate(days):
        a, b = run_day(d, wall)
        A += a
        B += b
        if (i + 1) % 25 == 0:
            print(f"{i+1}/{len(days)} days, A={len(A)} B={len(B)}", flush=True)
    ta = pd.DataFrame(A, columns=["date", "wts", "t_rel", "side", "px", "shares", "fair", "pnl_share"])
    tb = pd.DataFrame(B, columns=["date", "wts", "t_rel", "side", "px", "shares", "pnl_share"])
    ta.to_csv(ROOT / f"results/bt_1h_snipe_{args.tag}.csv", index=False)
    tb.to_csv(ROOT / f"results/bt_1h_settle_{args.tag}.csv", index=False)
    for name, t in [("SNIPE", ta), ("SETTLE", tb)]:
        if len(t) == 0:
            print(name, "no trades")
            continue
        ev = t.pnl_share.mean() * 100
        wr = (t.pnl_share > 0).mean()
        tpd = len(t) / len(days)
        print(f"{name}: n={len(t)} ({tpd:.2f}/day) EV={ev:.2f}c/share wr={wr:.3f} "
              f"med_px={t.px.median():.3f} med_shares={t.shares.median():.0f}")


if __name__ == "__main__":
    main()
