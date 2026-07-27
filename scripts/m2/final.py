#!/usr/bin/env python3
"""M2 final tables: the numbers that go in audit/M2_dominance_arb.md."""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/m2")
from analyse import RUNS, load, row, show, tstat, TRAIN_END, ATT, FILLED  # noqa: E402

pd.set_option("display.width", 260)
C = ["cfg", "days", "sig_day", "att", "both", "single", "nofill", "surv%",
     "leg_risk%", "ev_pair_c", "pair_win%", "$/att", "win%", "worst$",
     "$/day", "t_day", "$/day_-1", "$/day_-3"]


def boot_ci(daily: np.ndarray, n=20000, seed=0):
    """Bootstrap CI on the mean of DAILY P&L (day is the cluster unit)."""
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(daily), size=(n, len(daily)))
    m = daily[idx].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def dayser(df, col="pnl_abort", days=None):
    all_days = sorted(days if days is not None else df.day.unique())
    d = df[df.day.isin(all_days)]
    sig = d[d.signal == True]                                       # noqa: E712
    att = sig[sig.outcome.fillna("none").isin(ATT)]
    return att.groupby("day")[col].sum().reindex(all_days).fillna(0.0)


def main():
    days_all = sorted(load("gap005").day.unique())
    train = [d for d in days_all if d <= TRAIN_END]
    test = [d for d in days_all if d > TRAIN_END]

    print("=" * 130)
    print("F1. FEE-AWARE GATE vs FLAT GATE  (43 days, latency 1500ms, age<=5s, per-window fee, $25 clip, ABORT policy)")
    print("=" * 130)
    names = ["gap000", "gap020", "gap035", "gap050", "gap080",
             "feegate00", "feegate05", "feegate10", "feegate20"]
    t = pd.DataFrame([row(load(n), n) for n in names if os.path.exists(f"{RUNS}/{n}.parquet")])
    show(t, C)

    print("\n" + "=" * 130)
    print("F2. LATENCY FRONTIER, fee-gate + 1c  (this is the whole answer)")
    print("=" * 130)
    names = ["feegate10_lat0", "feegate10_lat250", "feegate10_lat500",
             "feegate10_lat1000", "feegate10"]
    t = pd.DataFrame([row(load(n), n) for n in names if os.path.exists(f"{RUNS}/{n}.parquet")])
    show(t, C)

    print("\n" + "=" * 130)
    print("F3. SIZING at 1500ms, fee-gate + 1c")
    print("=" * 130)
    names = ["feegate10", "feegate10_clip100", "feegate10_clip250", "feegate10_slack50"]
    t = pd.DataFrame([row(load(n), n) for n in names if os.path.exists(f"{RUNS}/{n}.parquet")])
    show(t, C)

    print("\n" + "=" * 130)
    print("F4. TIME-TO-CLOSE BUCKETS")
    print("=" * 130)
    names = ["feegate10", "tau_late", "tau_early"]
    t = pd.DataFrame([row(load(n), n) for n in names if os.path.exists(f"{RUNS}/{n}.parquet")])
    show(t, C)

    print("\n" + "=" * 130)
    print("F5. TRAIN / TEST for every candidate rule  (train = Apr 2-15 = 14d; TEST = Apr 16-May 12 + Jul 6-7 = 29d)")
    print("=" * 130)
    rows = []
    for n in ("gap035", "gap080", "feegate10", "feegate_strict", "strict",
              "feegate10_slack50", "slack50"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        rows.append(row(d, f"{n}|TRAIN", "pnl_abort", train))
        rows.append(row(d, f"{n}|TEST", "pnl_abort", test))
    show(pd.DataFrame(rows), C)

    print("\n" + "=" * 130)
    print("F6. HEADLINE with day-clustered 95% bootstrap CI on $/day  (ABORT policy, $25 clip)")
    print("=" * 130)
    for n in ("feegate10", "gap035", "gap080", "feegate_strict", "strict"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        for lab, dd in (("ALL43", days_all), ("TEST29", test)):
            s = dayser(load(n), "pnl_abort", dd).to_numpy(float)
            lo, hi = boot_ci(s)
            print(f"  {n:16s} {lab:7s} $/day {s.mean():+7.2f}  95% CI [{lo:+7.2f}, {hi:+7.2f}]  "
                  f"t_day {tstat(s):+6.2f}  n_days {len(s)}  "
                  f"-best1 {np.sort(s)[:-1].mean():+7.2f}  -best3 {np.sort(s)[:-3].mean():+7.2f}")

    print("\n" + "=" * 130)
    print("F7. P&L DECOMPOSITION per attempt ($25 clip)  -- where the money actually goes")
    print("=" * 130)
    for n in ("feegate10", "gap035", "feegate_strict"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        sig = d[d.signal == True]                                    # noqa: E712
        att = sig[sig.outcome.fillna("none").isin(ATT)]
        pp = att.pnl_pair.fillna(0.0)
        rh = att.pnl_resid_hold.fillna(0.0)
        ra = att.pnl_resid_abort.fillna(0.0)
        rc = att.pnl_resid_complete.fillna(0.0)
        print(f"  {n:16s} n_att {len(att):4d}   matched-pair P&L {pp.sum():+9.2f} "
              f"({pp.mean():+6.3f}/att)   residual: HOLD {rh.sum():+9.2f} ({rh.mean():+6.3f}) "
              f"| ABORT {ra.sum():+9.2f} ({ra.mean():+6.3f}) | COMPLETE {rc.sum():+9.2f} ({rc.mean():+6.3f})")

    print("\n" + "=" * 130)
    print("F8. IF BOTH LEGS FILLED (the arb itself, leg risk removed): cents per pair-share")
    print("=" * 130)
    for n in ("gap000", "feegate10", "gap035", "gap080"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        b = d[(d.signal == True) & (d.outcome.isin(FILLED))]          # noqa: E712
        if not len(b):
            continue
        pp = 100 * b.pnl_per_pair.astype(float)
        dsr = b.groupby("day").pnl_per_pair.mean() * 100
        print(f"  {n:12s} n_pairs {len(b):4d} on {b.day.nunique():2d} days   "
              f"ev {pp.mean():+7.2f}c  med {pp.median():+7.2f}c  worst {pp.min():+8.2f}c  "
              f"win {100*(pp>0).mean():5.1f}%  between {100*b.between.mean():5.1f}%  "
              f"t_day {tstat(dsr.to_numpy(float)):+6.2f}")


if __name__ == "__main__":
    main()
