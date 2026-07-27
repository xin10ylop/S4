#!/usr/bin/env python3
"""M2 analysis: turn the run parquets into the tables the audit needs."""
from __future__ import annotations

import glob
import json
import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/m2")
import dom                                     # noqa: E402

OUT = f"{ROOT}/data/multicoin/m2"
RUNS = f"{OUT}/runs"
pd.set_option("display.width", 250)

TRAIN_END = "2026-04-15"          # pre-committed: train Apr 2-15, report on the rest
FILLED = ("filled_both", "filled_partial")
ATT = FILLED + ("single_leg", "no_fill")


def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(f"{RUNS}/{name}.parquet")


def tstat(x) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def daily_series(att: pd.DataFrame, col: str, all_days) -> pd.Series:
    return att.groupby("day")[col].sum().reindex(all_days).fillna(0.0)


def row(df: pd.DataFrame, label: str, pnl_col="pnl_abort", days=None) -> dict:
    all_days = sorted(days if days is not None else df.day.unique())
    nd = len(all_days)
    d = df[df.day.isin(all_days)]
    sig = d[d.signal == True]                                    # noqa: E712
    o = dict(cfg=label, days=nd, closes=len(d), sig=len(sig),
             sig_day=len(sig) / nd if nd else np.nan)
    if sig.empty:
        return o
    oc = sig.outcome.fillna("none")
    att = sig[oc.isin(ATT)]
    both = sig[oc.isin(FILLED)]
    o["stale"] = int((oc == "stale_book").sum())
    o["att"] = len(att)
    o["both"] = len(both)
    o["single"] = int((oc == "single_leg").sum())
    o["nofill"] = int((oc == "no_fill").sum())
    o["surv%"] = 100 * len(both) / len(att) if len(att) else np.nan
    o["leg_risk%"] = 100 * o["single"] / len(att) if len(att) else np.nan
    if len(both):
        pp = both.pnl_per_pair.to_numpy(float)
        o["ev_pair_c"] = 100 * float(np.mean(pp))
        o["pair_win%"] = 100 * float((pp > 0).mean())
        o["pair_worst_c"] = 100 * float(np.min(pp))
        o["between%"] = 100 * float(both.between.mean())
    if len(att):
        x = att[pnl_col].fillna(0.0).to_numpy(float)
        ds = daily_series(att, pnl_col, all_days)
        o["$/att"] = float(x.mean())
        o["win%"] = 100 * float((x > 0).mean())
        o["worst$"] = float(x.min())
        o["best$"] = float(x.max())
        o["$/day"] = float(ds.mean())
        o["t_trade"] = tstat(x)
        o["t_day"] = tstat(ds.to_numpy(float))
        ds2 = ds.sort_values(ascending=False)
        o["$/day_-1"] = float(ds2.iloc[1:].mean())
        o["t_day_-1"] = tstat(ds2.iloc[1:].to_numpy(float))
        o["$/day_-3"] = float(ds2.iloc[3:].mean())
        o["t_day_-3"] = tstat(ds2.iloc[3:].to_numpy(float))
        o["total$"] = float(x.sum())
    return o


def table(names, pnl_col="pnl_abort", days=None, title=""):
    rows = []
    for n in names:
        f = f"{RUNS}/{n}.parquet"
        if not os.path.exists(f):
            continue
        rows.append(row(load(n), n, pnl_col, days))
    t = pd.DataFrame(rows)
    if title:
        print(f"\n### {title}")
    return t


COLS = ["cfg", "days", "sig", "sig_day", "att", "both", "single", "nofill",
        "surv%", "leg_risk%", "ev_pair_c", "pair_win%", "between%",
        "$/att", "win%", "worst$", "$/day", "t_trade", "t_day",
        "$/day_-1", "$/day_-3", "total$"]


def show(t, cols=None):
    c = [x for x in (cols or COLS) if x in t.columns]
    print(t[c].round(3).to_string(index=False))


def main():
    days_all = sorted(load("gap005").day.unique())
    train = [d for d in days_all if d <= TRAIN_END]
    test = [d for d in days_all if d > TRAIN_END]
    print(f"DAYS  all={len(days_all)} ({days_all[0]}..{days_all[-1]})  "
          f"train={len(train)} ({train[0]}..{train[-1]})  "
          f"test={len(test)} ({test[0]}..{test[-1]})")

    print("\n" + "=" * 100)
    print("T1. GAP THRESHOLD SWEEP  (all 43 days, latency 1500ms, age<=5s, per-window fee, $25)")
    print("=" * 100)
    g = [f"gap{int(x*1000):03d}" for x in (0.0, 0.005, 0.01, 0.02, 0.035, 0.05, 0.08)]
    show(table(g))

    print("\n" + "=" * 100)
    print("T2. LATENCY SWEEP (gap>0.5c)")
    print("=" * 100)
    show(table(["lat0", "lat250", "lat500", "lat1000", "gap005", "lat3000"]))

    print("\n" + "=" * 100)
    print("T3. ORDER TYPE  (slackN = give away N% of the gap on each leg; market = no limit)")
    print("=" * 100)
    show(table(["gap005", "slack25", "slack50", "slack100", "market"]))

    print("\n" + "=" * 100)
    print("T4. CONTAMINATION CONTROLS")
    print("=" * 100)
    show(table(["gap005", "persist100", "persist250", "persist500", "persist1000",
                "persist2000", "haircut5", "haircut10", "localclock",
                "age1.0", "age30.0", "ageinf", "fee0"]))

    print("\n" + "=" * 100)
    print("T5. SIZING")
    print("=" * 100)
    show(table(["gap005", "clip100", "clip250", "depthfrac50", "depthfrac25"]))

    print("\n" + "=" * 100)
    print("T6. RESIDUAL POLICY  (same run, three ways of handling the naked leg)")
    print("=" * 100)
    rows = []
    for n in ("gap005", "gap020", "strict", "slack50"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        for col in ("pnl_hold", "pnl_abort", "pnl_complete"):
            r = row(d, f"{n}:{col}", col)
            rows.append(r)
    show(pd.DataFrame(rows))

    print("\n" + "=" * 100)
    print("T7. TRAIN / TEST  (train = Apr 2-15; TEST = Apr 16 - May 12 + Jul 6-7, untouched)")
    print("=" * 100)
    for n in ("gap005", "gap020", "gap035", "strict", "slack50"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        rows = [row(d, f"{n} TRAIN", "pnl_abort", train),
                row(d, f"{n} TEST ", "pnl_abort", test)]
        show(pd.DataFrame(rows))
        print()

    print("\n" + "=" * 100)
    print("T8. LEG RISK ANATOMY  (gap005)")
    print("=" * 100)
    d = load("gap005")
    s = d[(d.signal == True) & (d.outcome == "single_leg")]           # noqa: E712
    print(f"single-leg events: {len(s)}")
    if len(s):
        print("\nwhich leg filled:")
        print(s.leg.value_counts().to_string())
        print("\nnaked position outcome (P&L in $ at the $25 clip):")
        for c in ("pnl_resid_hold", "pnl_resid_abort", "pnl_resid_complete"):
            if c in s:
                x = s[c].astype(float)
                print(f"  {c:22s} mean {x.mean():+7.3f}  median {x.median():+7.3f}  "
                      f"p05 {x.quantile(.05):+8.3f}  p95 {x.quantile(.95):+7.3f}  "
                      f"min {x.min():+8.3f}  max {x.max():+7.3f}  win% {100*(x>0).mean():.1f}")
        print(f"\nleg won: {100*s.leg_won.mean():.1f}%   "
              f"abort executable: {100*s.abort_ok.fillna(False).mean():.1f}%   "
              f"complete executable: {100*s.complete_ok.fillna(False).mean():.1f}%")
        if "abort_px" in s:
            ap = s.dropna(subset=["abort_px"])
            print(f"unwind slippage (entry px -> abort px), cents: "
                  f"{(100*(ap.abort_px - ap.leg_px)).describe(percentiles=[.05,.5,.95]).round(2).to_dict()}")

    print("\n" + "=" * 100)
    print("T9. DEPTH at the fill instant (3c walk bound), pair-shares and $")
    print("=" * 100)
    for n in ("gap005", "gap020"):
        d = load(n)
        s = d[(d.signal == True)].dropna(subset=["depth_pair_sh"])    # noqa: E712
        if not len(s):
            continue
        q = [.05, .25, .5, .75, .95]
        print(f"\n{n}: n={len(s)}")
        print("  pair shares within 3c :", s.depth_pair_sh.describe(percentiles=q).round(1).to_dict())
        print("  pair $ within 3c      :", s.depth_pair_usd.describe(percentiles=q).round(1).to_dict())
        print("  pair shares AT the top:", s.depth_pair_top_sh.describe(percentiles=q).round(1).to_dict())

    print("\n" + "=" * 100)
    print("T10. SURVIVAL DIAGNOSTIC — what happens to the gap over the latency")
    print("=" * 100)
    for n in ("gap005", "gap020", "gap035"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        s = d[(d.signal == True)].dropna(subset=["gap_fil"])          # noqa: E712
        print(f"{n}: n={len(s)}  gap_sig mean {100*s.gap_sig.mean():.2f}c -> "
              f"gap_fil mean {100*s.gap_fil.mean():+.2f}c   "
              f"still crossed at fill: {100*(s.gap_fil > 0).mean():.1f}%   "
              f"still >= signal gap: {100*(s.gap_fil >= s.gap_sig).mean():.1f}%   "
              f"median violation life {s.violation_life_s.median():.2f}s")


if __name__ == "__main__":
    main()
