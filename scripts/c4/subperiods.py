#!/usr/bin/env python3
"""
C4 -- stability of every grid cell across sub-periods, and the honest
overfitting diagnostics.

Reads the per-cell trade tapes written by grid.py and reports, per cell:
  * repo (IS) vs fresh (OOS) EV/share and day-level t
  * fresh split at the 2026-06-27 liquidity break C1 dated
  * the rank of each cell in-sample and out-of-sample (rank correlation is the
    honest test of whether tuning on the repo period transfers at all)
  * a day-level stationary bootstrap CI on the chosen cell
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


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def stat(tr, n_days):
    f = tr[tr.outcome == "filled"]
    if f.empty:
        return dict(fills=0, trades_day=0.0, ev_share=np.nan, win=np.nan,
                    t_day=np.nan, pnl_day=np.nan, fill_rate=np.nan)
    daily = f.groupby("day").pnl_per_share.mean().to_numpy(float)
    return dict(fills=len(f), trades_day=len(f) / n_days,
                ev_share=float(f.pnl_per_share.mean()) * 100,
                win=float(f.won.mean()),
                t_day=_t(daily), pnl_day=float(f.pnl.sum() / n_days),
                fill_rate=len(f) / len(tr))


def day_bootstrap(tr, n=20000, seed=0):
    f = tr[tr.outcome == "filled"]
    daily = f.groupby("day").pnl_per_share.mean().to_numpy(float)
    if len(daily) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(daily), size=(n, len(daily)))
    m = daily[idx].mean(axis=1) * 100
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main():
    rows = []
    for w in WINDOWS:
        for fl in FLOORS:
            k = f"w{w}_f{fl}"
            r = dict(vol_window=w, floor=fl)
            for per, lab in (("repo_IS", "IS"), ("fresh_OOS", "OOS")):
                f = f"{OUT}/trades_{per}_{k}.parquet"
                if not os.path.exists(f):
                    continue
                tr = pd.read_parquet(f)
                nd = tr.day.nunique()
                s = stat(tr, nd)
                for kk, vv in s.items():
                    r[f"{lab}_{kk}"] = vv
                r[f"{lab}_days"] = nd
                if per == "fresh_OOS":
                    pre = tr[tr.day < "2026-06-27"]
                    post = tr[tr.day >= "2026-06-27"]
                    for sub, lab2 in ((pre, "PRE627"), (post, "POST627")):
                        s2 = stat(sub, max(sub.day.nunique(), 1))
                        r[f"{lab2}_ev"] = s2["ev_share"]
                        r[f"{lab2}_td"] = s2["t_day"]
                        r[f"{lab2}_n"] = s2["fills"]
                    lo, hi = day_bootstrap(tr)
                    r["OOS_ci_lo"], r["OOS_ci_hi"] = lo, hi
            rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/subperiods.csv", index=False)
    pd.set_option("display.width", 320, "display.max_columns", 60)
    cols = ["vol_window", "floor", "IS_fills", "IS_trades_day", "IS_ev_share", "IS_t_day",
            "OOS_fills", "OOS_trades_day", "OOS_ev_share", "OOS_t_day", "OOS_win",
            "OOS_fill_rate", "OOS_ci_lo", "OOS_ci_hi", "PRE627_ev", "POST627_ev",
            "POST627_td", "POST627_n"]
    print(df[cols].round(3).to_string(index=False))
    # rank transfer
    a = df.dropna(subset=["IS_ev_share", "OOS_ev_share"])
    from scipy.stats import spearmanr, pearsonr
    print("\nIS vs OOS EV/share across the 24 cells:")
    print("  Spearman rho = %.3f (p=%.3f)" % spearmanr(a.IS_ev_share, a.OOS_ev_share))
    print("  Pearson  r   = %.3f (p=%.3f)" % pearsonr(a.IS_ev_share, a.OOS_ev_share))
    b = a.sort_values("IS_ev_share", ascending=False)
    print("\n  best IS cell:", b.iloc[0].vol_window, b.iloc[0].floor,
          "IS %.2f -> OOS %.2f (t_day %.2f)" % (b.iloc[0].IS_ev_share,
                                                b.iloc[0].OOS_ev_share, b.iloc[0].OOS_t_day))
    c = a.sort_values("OOS_ev_share", ascending=False)
    print("  best OOS cell:", c.iloc[0].vol_window, c.iloc[0].floor,
          "OOS %.2f (t_day %.2f), its IS %.2f" % (c.iloc[0].OOS_ev_share,
                                                  c.iloc[0].OOS_t_day, c.iloc[0].IS_ev_share))


if __name__ == "__main__":
    main()
