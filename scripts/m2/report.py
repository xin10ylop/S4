#!/usr/bin/env python3
"""M2 report: every table that goes into audit/M2_dominance_arb.md."""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/m2")
from analyse import RUNS, load, row, tstat, TRAIN_END, ATT, FILLED   # noqa: E402

pd.set_option("display.width", 300)
C = ["cfg", "sig_day", "att", "both", "single", "nofill", "surv%", "leg_risk%",
     "ev_pair_c", "pair_win%", "$/att", "win%", "worst$", "$/day", "t_day",
     "$/day_-1", "$/day_-3"]


def T(names, days=None, cols=C, pnl="pnl_abort"):
    rows = [row(load(n), n, pnl, days) for n in names
            if os.path.exists(f"{RUNS}/{n}.parquet")]
    t = pd.DataFrame(rows)
    c = [x for x in cols if x in t.columns]
    print(t[c].round(3).to_string(index=False))
    return t


def boot(daily, n=20000, seed=0):
    rng = np.random.default_rng(seed)
    m = daily[rng.integers(0, len(daily), size=(n, len(daily)))].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def dayser(df, col="pnl_abort", days=None):
    ad = sorted(days if days is not None else df.day.unique())
    d = df[df.day.isin(ad)]
    sig = d[d.signal == True]                                        # noqa: E712
    att = sig[sig.outcome.fillna("none").isin(ATT)]
    return att.groupby("day")[col].sum().reindex(ad).fillna(0.0).to_numpy(float)


def main():
    days_all = sorted(load("gap000").day.unique())
    train = [d for d in days_all if d <= TRAIN_END]
    test = [d for d in days_all if d > TRAIN_END]
    print(f"days all={len(days_all)} train={len(train)} test={len(test)}\n")

    print("=" * 140); print("R1. BOOK-SANITY SENSITIVITY (fee gate + 1c, 1500ms, $25)"); print("=" * 140)
    T(["sane_off", "feegate10", "maxspr20", "maxspr10", "maxspr5"])

    print("\n" + "=" * 140); print("R2. GATE SWEEP"); print("=" * 140)
    T(["gap000", "gap020", "gap035", "gap050", "gap080",
       "feegate00", "feegate05", "feegate10", "feegate20"])

    print("\n" + "=" * 140); print("R3. LATENCY FRONTIER"); print("=" * 140)
    T(["lat0", "lat250", "lat500", "lat1000", "gap005", "lat3000"])
    print()
    T(["feegate10_lat0", "feegate10_lat250", "feegate10_lat500", "feegate10_lat1000", "feegate10"])
    print()
    T(["final_lat0", "final_lat250", "final_lat500", "final_lat1000", "final"])

    print("\n" + "=" * 140); print("R4. CONTAMINATION CONTROLS (flat 0.5c gate base)"); print("=" * 140)
    T(["gap005", "persist100", "persist250", "persist500", "persist1000", "persist2000",
       "haircut5", "haircut10", "localclock", "age1.0", "age30.0", "ageinf", "fee0"])

    print("\n" + "=" * 140); print("R5. ORDER TYPE"); print("=" * 140)
    T(["gap005", "slack25", "slack50", "slack100", "market",
       "feegate10", "feegate10_slack50", "final", "final_slack50"])

    print("\n" + "=" * 140); print("R6. SIZING / CAPACITY"); print("=" * 140)
    T(["feegate10", "feegate10_clip100", "feegate10_clip250",
       "final", "final_clip100", "final_clip250",
       "gap005", "clip100", "clip250", "depthfrac50", "depthfrac25"])

    print("\n" + "=" * 140); print("R7. TIME-TO-CLOSE"); print("=" * 140)
    T(["feegate10", "tau_late", "tau_early"])

    print("\n" + "=" * 140); print("R8. TRAIN / TEST"); print("=" * 140)
    for n in ("gap035", "gap080", "feegate10", "feegate_strict", "final",
              "final_slack50", "feegate10_slack50"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        rows = [row(d, f"{n}|TRAIN14", "pnl_abort", train),
                row(d, f"{n}|TEST29 ", "pnl_abort", test)]
        t = pd.DataFrame(rows)
        print(t[[x for x in C if x in t.columns]].round(3).to_string(index=False))

    print("\n" + "=" * 140); print("R9. HEADLINE $/day with day-clustered bootstrap 95% CI"); print("=" * 140)
    for n in ("feegate10", "gap080", "final", "final_slack50", "feegate_strict"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        for lab, dd in (("ALL43", days_all), ("TRAIN14", train), ("TEST29", test)):
            s = dayser(load(n), "pnl_abort", dd)
            lo, hi = boot(s)
            print(f"  {n:16s} {lab:8s} $/day {s.mean():+7.2f}  CI95 [{lo:+7.2f},{hi:+7.2f}]  "
                  f"t_day {tstat(s):+6.2f}  -best1 {np.sort(s)[:-1].mean():+7.2f}  "
                  f"-best3 {np.sort(s)[:-3].mean():+7.2f}  worstday {s.min():+7.2f}")

    print("\n" + "=" * 140); print("R10. RESIDUAL POLICY"); print("=" * 140)
    for n in ("feegate10", "final"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        rows = [row(load(n), f"{n}:{c}", c) for c in ("pnl_hold", "pnl_abort", "pnl_complete")]
        t = pd.DataFrame(rows)
        print(t[[x for x in C if x in t.columns]].round(3).to_string(index=False))

    print("\n" + "=" * 140); print("R11. LEG-RISK ANATOMY"); print("=" * 140)
    for n in ("feegate10", "final"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        s = d[(d.signal == True) & (d.outcome == "single_leg")]        # noqa: E712
        print(f"\n{n}: single-leg events {len(s)}   which leg: "
              f"{dict(s.leg.value_counts())}   leg won {100*s.leg_won.mean():.1f}%")
        for c in ("pnl_resid_hold", "pnl_resid_abort", "pnl_resid_complete"):
            x = s[c].astype(float)
            print(f"   {c:22s} mean {x.mean():+7.3f} med {x.median():+7.3f} "
                  f"p05 {x.quantile(.05):+8.3f} p95 {x.quantile(.95):+8.3f} "
                  f"min {x.min():+8.3f} max {x.max():+8.3f} win {100*(x>0).mean():5.1f}%")
        ap = s.dropna(subset=["abort_px"])
        print(f"   abort executable {100*s.abort_ok.fillna(False).mean():.1f}%   "
              f"unwind slip (cents) med {100*(ap.abort_px-ap.leg_px).median():+.2f} "
              f"p05 {100*(ap.abort_px-ap.leg_px).quantile(.05):+.2f}")

    print("\n" + "=" * 140); print("R12. PAIRS THAT FILLED BOTH LEGS (the arb, leg risk removed)"); print("=" * 140)
    for n in ("gap000", "feegate10", "final", "gap080"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        d = load(n)
        b = d[(d.signal == True) & (d.outcome.isin(FILLED))]           # noqa: E712
        if not len(b):
            continue
        pp = 100 * b.pnl_per_pair.astype(float)
        p1 = pp[~b.between.astype(bool).to_numpy()]
        ds = b.groupby("day").pnl_per_pair.mean() * 100
        print(f"  {n:14s} n {len(b):4d} / {b.day.nunique():2d}d  ev {pp.mean():+7.2f}c "
              f"med {pp.median():+7.2f}c  worst {pp.min():+8.2f}c  win {100*(pp>0).mean():5.1f}%  "
              f"t_day {tstat(ds.to_numpy(float)):+5.2f}  | payoff=2 {100*b.between.mean():5.1f}% "
              f"| ev EXCLUDING the $2 bonus {p1.mean():+6.2f}c (n={len(p1)}, worst {p1.min():+.2f}c)")

    print("\n" + "=" * 140); print("R13. DEPTH within a 3c walk, at the fill instant"); print("=" * 140)
    for n in ("feegate10", "final"):
        if not os.path.exists(f"{RUNS}/{n}.parquet"):
            continue
        s = load(n)
        s = s[(s.signal == True)].dropna(subset=["depth_pair_sh"])     # noqa: E712
        q = [.05, .25, .5, .75, .95]
        print(f"  {n}: n={len(s)}")
        for c, lab in (("depth_pair_top_sh", "pair shares AT touch"),
                       ("depth_pair_sh", "pair shares <=3c   "),
                       ("depth_pair_usd", "pair $ <=3c        ")):
            d = s[c].describe(percentiles=q)
            print(f"    {lab}: p05 {d['5%']:8.1f}  p25 {d['25%']:8.1f}  med {d['50%']:8.1f}  "
                  f"p75 {d['75%']:9.1f}  p95 {d['95%']:9.1f}")

    print("\n" + "=" * 140); print("R14. SPREAD REGIME OF THE SIGNALS"); print("=" * 140)
    d = load("feegate10")
    s = d[d.signal == True]                                            # noqa: E712
    print("  spread on the Up-low leg (cents):",
          (100 * s.spr_a_sig).describe(percentiles=[.5, .9]).round(1).to_dict())
    print("  spread on the Down-high leg (cents):",
          (100 * s.spr_b_sig).describe(percentiles=[.5, .9]).round(1).to_dict())
    both = s[s.outcome.isin(FILLED)]
    print(f"  both-leg fills: median spreads {100*both.spr_a_sig.median():.1f}c / "
          f"{100*both.spr_b_sig.median():.1f}c")


if __name__ == "__main__":
    main()
