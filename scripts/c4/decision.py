#!/usr/bin/env python3
"""
C4 part 3 -- does the floor change the DECISION?

The pre-committed 5m go/no-go (docs/07 §6.5) is:
    on 20-30 days of fresh book data, with a <=5 s staleness filter, at the
    shipped parameters and the recalibrated sigma floor:
      * EV/share below +3c, OR day-level t below 2   -> do not trade 5m
      * realised fill rate below ~50% of signals     -> do not trade 5m
      * realised trades/day below ~10                -> do not trade 5m

This evaluates all three conditions for every (vol_window, floor) cell and for
every reporting frame, so "does the floor flip a condition" is answered by
counting, not by narrative.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/c4"
WINDOWS = [30, 60, 120, 300]
FLOORS = ["none", "8e-6", "1e-5", "2e-5", "3e-5", "5e-5"]
BAD_CL_DAYS = {"2026-06-10", "2026-06-11"}   # C1: 80.5% / 49.5% Chainlink coverage
BREAK = "2026-06-27"                          # C1 §7: dated liquidity regime break


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def verdict(tr, label):
    f = tr[tr.outcome == "filled"]
    nd = tr.day.nunique()
    if f.empty or nd == 0:
        return dict(frame=label, days=nd, fills=0, ev=np.nan, t_day=np.nan,
                    trades_day=0, fill_rate=np.nan, PASS=False)
    daily = f.groupby("day").pnl_per_share.mean().to_numpy(float)
    ev = float(f.pnl_per_share.mean()) * 100
    td = _t(daily)
    tpd = len(f) / nd
    fr = len(f) / len(tr)
    return dict(frame=label, days=nd, fills=len(f), ev=ev, t_day=td,
                trades_day=tpd, fill_rate=fr, win=float(f.won.mean()),
                cond_ev=ev >= 3.0, cond_t=td >= 2.0, cond_fr=fr >= 0.50,
                cond_tpd=tpd >= 10.0,
                PASS=bool(ev >= 3.0 and td >= 2.0 and fr >= 0.50 and tpd >= 10.0))


def frames(tr):
    yield "fresh 54d (all)", tr
    yield "fresh 52d (ex bad CL)", tr[~tr.day.isin(BAD_CL_DAYS)]
    yield f"pre-break (<{BREAK}, 26d)", tr[tr.day < BREAK]
    yield f"post-break (>={BREAK}, 28d)", tr[tr.day >= BREAK]
    yield "July only (19d)", tr[tr.day >= "2026-07-08"]


def main():
    rows = []
    for w in WINDOWS:
        for fl in FLOORS:
            p = f"{OUT}/trades_fresh_OOS_w{w}_f{fl}.parquet"
            if not os.path.exists(p):
                continue
            tr = pd.read_parquet(p)
            for lab, sub in frames(tr):
                r = verdict(sub, lab)
                r.update(vol_window=w, floor=fl, exec="top_of_book_cap250")
                rows.append(r)
    # the ladder (real execution) run at the shipped floor
    for cap in ("cap25", "cap100", "cap250", "cap500", "cap1000", "capINF"):
        p = f"{OUT}/cap_trades_{cap}.parquet"
        if os.path.exists(p):
            tr = pd.read_parquet(p)
            for lab, sub in frames(tr):
                r = verdict(sub, lab)
                r.update(vol_window=120, floor="8e-6", exec=f"ladder_{cap}")
                rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/decision.csv", index=False)
    pd.set_option("display.width", 320, "display.max_columns", 40)
    print("=== every cell, full fresh OOS (top of book, $250) ===")
    a = df[(df.frame == "fresh 54d (all)") & (df["exec"] == "top_of_book_cap250")]
    print(a[["vol_window", "floor", "days", "fills", "ev", "t_day", "trades_day",
             "fill_rate", "cond_ev", "cond_t", "cond_fr", "cond_tpd", "PASS"]].round(3).to_string(index=False))
    print(f"\ncells PASSING the full rule on fresh 54d: {int(a.PASS.sum())} of {len(a)}")
    print("=== post-break only ===")
    b = df[(df.frame.str.startswith("post-break")) & (df["exec"] == "top_of_book_cap250")]
    print(b[["vol_window", "floor", "days", "fills", "ev", "t_day", "trades_day",
             "fill_rate", "PASS"]].round(3).to_string(index=False))
    print(f"\ncells PASSING post-break: {int(b.PASS.sum())} of {len(b)}")
    print("=== ladder execution, shipped floor, by cap ===")
    c = df[df["exec"].str.startswith("ladder")]
    print(c[["exec", "frame", "days", "fills", "ev", "t_day", "trades_day",
             "fill_rate", "PASS"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
