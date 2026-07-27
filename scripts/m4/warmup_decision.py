#!/usr/bin/env python3
"""M4 guard 2, part (c) in full: compare the trades a COLD sigma manufactures
against the trades the warm (120-sample) sigma produces, with day-clustered
significance, and price the cost of the warmup lockout in missed trades.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FEE, EDGE_MIN = 0.07, 0.03
PRICE_MIN, PRICE_MAX = 0.30, 0.99
NS = (3, 5, 10, 20, 30, 45, 60, 90, 120)
RNG = np.random.default_rng(777)


def fee(p):
    return FEE * p * (1 - p)


def signal_for(fair_up, ask, bid, fill_ask, fill_bid):
    for side in ("up", "down"):
        px = ask if side == "up" else (1 - bid if np.isfinite(bid) else np.nan)
        fv = fair_up if side == "up" else 1 - fair_up
        if not (np.isfinite(px) and PRICE_MIN < px < PRICE_MAX):
            continue
        if fv - px - fee(px) <= EDGE_MIN:
            continue
        fpx = fill_ask if side == "up" else (1 - fill_bid if np.isfinite(fill_bid) else np.nan)
        if not (np.isfinite(fpx) and PRICE_MIN < fpx < PRICE_MAX):
            return None
        if fv - fpx - fee(fpx) <= EDGE_MIN:
            return None
        return side, fpx
    return None


def trades_for(d, col):
    out = []
    for (day, wts), grp in d.groupby(["day", "wts"], sort=False):
        grp = grp.sort_values("tau", ascending=False)
        rid = int(grp.result_id.iloc[0])
        for _, rr in grp.iterrows():
            if not np.isfinite(rr[col]):
                continue
            s = signal_for(rr[col], rr.ask, rr.bid, rr.fill_ask, rr.fill_bid)
            if s:
                side, fpx = s
                won = 1.0 if ((rid == 0) == (side == "up")) else 0.0
                out.append(dict(day=day, wts=wts, side=side, px=fpx, won=won,
                                pnl=won - fpx - fee(fpx)))
                break
    return pd.DataFrame(out)


def day_boot_diff(a, b, col, n=10000):
    days = np.unique(np.concatenate([a.day.unique(), b.day.unique()]))
    ad = {d: a.loc[a.day == d, col].to_numpy() for d in days}
    bd = {d: b.loc[b.day == d, col].to_numpy() for d in days}
    out = np.full(n, np.nan)
    for i in range(n):
        pick = RNG.choice(days, size=len(days), replace=True)
        av = np.concatenate([ad[d] for d in pick])
        bv = np.concatenate([bd[d] for d in pick])
        if len(av) and len(bv):
            out[i] = av.mean() - bv.mean()
    out = out[np.isfinite(out)]
    diff = a[col].mean() - b[col].mean()
    p = 2 * min((out <= 0).mean(), (out >= 0).mean())
    return diff, np.percentile(out, 2.5), np.percentile(out, 97.5), p


def main():
    d = pd.read_parquet(ROOT / "data/c2/m4_warmup.parquet")
    warm = trades_for(d, "fair_120")
    print(f"WARM (120 samples): n={len(warm)}  win={warm.won.mean():.4f}  "
          f"c/share={100*warm.pnl.mean():.2f}  days={warm.day.nunique()}")
    key = set(zip(warm.day, warm.wts, warm.side))
    print()
    print(f"{'n':>4} {'cold_n':>7} {'cold_only':>10} {'contam%':>8} {'co_win':>8} "
          f"{'co_c/sh':>9} {'diff vs warm':>13} {'p':>7}")
    for n in NS:
        cold = trades_for(d, f"fair_{n}")
        only = cold[[k not in key for k in zip(cold.day, cold.wts, cold.side)]]
        if len(only) == 0:
            print(f"{n:>4} {len(cold):>7} {0:>10} {0.0:>8.1f} {'-':>8} {'-':>9} "
                  f"{'-':>13} {'-':>7}")
            continue
        diff, lo, hi, p = day_boot_diff(only, warm, "pnl")
        print(f"{n:>4} {len(cold):>7} {len(only):>10} "
              f"{100*len(only)/len(warm):>8.1f} {only.won.mean():>8.3f} "
              f"{100*only.pnl.mean():>9.2f} {100*diff:>+12.2f}c {p:>7.3f}")

    print("\n-- cost of the lockout: trades missed per restart --")
    span_days = d.day.nunique()
    rate = len(warm) / span_days
    print(f"  warm trade rate: {rate:.3f} trades/day over {span_days} days")
    for secs in (60, 90, 120, 300, 600):
        print(f"  warmup {secs:>4}s -> {rate*secs/86400:.5f} trades missed per restart "
              f"({100*secs/86400:.3f}% of a day)")


if __name__ == "__main__":
    main()
