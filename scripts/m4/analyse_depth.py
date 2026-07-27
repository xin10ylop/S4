#!/usr/bin/env python3
"""M4 guard 1 analysis: outcome of fills against unusually LARGE offers vs normal.

Statistic under test:
    size_ratio = size_of_the_ask_level / median(recent best-ask sizes for this
                 market family), the reference built ONLY from observations
                 strictly before the window in question (no lookahead).

Reported for three reference definitions (family-rolling per side,
family-rolling pooled, and the market's own trailing depth) so the conclusion
does not hinge on one arbitrary choice. Significance is day-clustered
bootstrap: trades on the same UTC day are resampled together, because BTC
regimes cluster within a day and naive per-trade t-stats overstate confidence.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
F = ROOT / "data/c2/m4_depth_fills.parquet"

RNG = np.random.default_rng(20260727)


def day_bootstrap(df, col, n=20000):
    """Day-clustered bootstrap mean of `col`. Returns (mean, lo95, hi95)."""
    if df.empty:
        return np.nan, np.nan, np.nan
    days = df.day.unique()
    by_day = {d: df.loc[df.day == d, col].to_numpy() for d in days}
    means = np.empty(n)
    for i in range(n):
        pick = RNG.choice(days, size=len(days), replace=True)
        vals = np.concatenate([by_day[d] for d in pick])
        means[i] = vals.mean()
    return df[col].mean(), np.percentile(means, 2.5), np.percentile(means, 97.5)


def diff_bootstrap(a, b, col, n=20000):
    """Day-clustered bootstrap of mean(a) - mean(b) where a,b are disjoint
    subsets of the same trade table (days resampled jointly)."""
    all_days = np.unique(np.concatenate([a.day.unique(), b.day.unique()]))
    ad = {d: a.loc[a.day == d, col].to_numpy() for d in all_days}
    bd = {d: b.loc[b.day == d, col].to_numpy() for d in all_days}
    out = np.full(n, np.nan)
    for i in range(n):
        pick = RNG.choice(all_days, size=len(all_days), replace=True)
        av = np.concatenate([ad[d] for d in pick])
        bv = np.concatenate([bd[d] for d in pick])
        if len(av) and len(bv):
            out[i] = av.mean() - bv.mean()
    out = out[np.isfinite(out)]
    d = a[col].mean() - b[col].mean()
    p = 2 * min((out <= 0).mean(), (out >= 0).mean())
    return d, np.percentile(out, 2.5), np.percentile(out, 97.5), p


def describe(df, label):
    n = len(df)
    if n == 0:
        print(f"  {label:<34} n=0")
        return
    wr = df.won.mean()
    m, lo, hi = day_bootstrap(df, "pnl_per_share")
    print(f"  {label:<34} n={n:<4} win={wr:6.3f}  c/share={100*m:7.2f} "
          f"[{100*lo:6.2f},{100*hi:6.2f}]  px={df.fill_px.mean():.3f} "
          f"size={df.fill_size.median():8.0f}")


def main():
    f = pd.read_parquet(F)
    f = f[f.filled].copy()
    print(f"filled trades: {len(f)}  days: {f.day.nunique()}")
    describe(f, "ALL")
    print()

    for refcol, name in (("depth_ref_family", "family-rolling (per side)"),
                          ("depth_ref_pooled", "family-rolling (pooled)"),
                          ("depth_ref_own", "own market trailing 300s")):
        sub = f[np.isfinite(f[refcol]) & (f[refcol] > 0)].copy()
        sub["ratio"] = sub.fill_size / sub[refcol]
        print(f"=== reference: {name}  (usable n={len(sub)}) ===")
        q = sub.ratio.describe(percentiles=[.5, .75, .9, .95, .99])
        print("  size_ratio quantiles: "
              + "  ".join(f"{k}={v:.2f}" for k, v in q.items() if k != "count"))
        for thr in (2.0, 3.0, 5.0, 8.0, 10.0):
            big = sub[sub.ratio > thr]
            nrm = sub[sub.ratio <= thr]
            print(f"  -- threshold {thr:g}x --")
            describe(big, f"LARGE (>{thr:g}x ref)")
            describe(nrm, f"normal (<={thr:g}x ref)")
            if len(big) >= 5 and len(nrm) >= 5:
                d, lo, hi, p = diff_bootstrap(big, nrm, "pnl_per_share")
                print(f"     diff(LARGE - normal) = {100*d:+.2f} c/share "
                      f"95%CI [{100*lo:+.2f},{100*hi:+.2f}]  p={p:.3f}")
        print()

    # cross-tab with price: "unusually large AND cheap"
    sub = f[np.isfinite(f.depth_ref_family) & (f.depth_ref_family > 0)].copy()
    sub["ratio"] = sub.fill_size / sub.depth_ref_family
    print("=== large x cheap cross-tab (family reference) ===")
    for pmax in (0.55, 0.70, 0.85):
        cheap = sub[sub.fill_px <= pmax]
        if len(cheap) < 10:
            continue
        print(f"  price <= {pmax}: n={len(cheap)}")
        for thr in (3.0, 5.0):
            describe(cheap[cheap.ratio > thr], f"    cheap & LARGE >{thr:g}x")
            describe(cheap[cheap.ratio <= thr], f"    cheap & normal <={thr:g}x")
    print()

    # what would the guard cost/save? cap the level instead of skipping
    print("=== effect of SKIPPING large levels on total PnL ($ terms) ===")
    sub["pnl_usd"] = sub.pnl_per_share * sub.shares
    tot = sub.pnl_usd.sum()
    for thr in (3.0, 5.0, 8.0, 10.0):
        kept = sub[sub.ratio <= thr]
        print(f"  thr={thr:g}x  kept {len(kept)}/{len(sub)} trades  "
              f"PnL ${kept.pnl_usd.sum():.2f} of ${tot:.2f}  "
              f"(dropped trades PnL ${sub[sub.ratio > thr].pnl_usd.sum():.2f})")

    # correlation check: is size at all informative, continuously?
    print("\n=== continuous check: rank correlation size_ratio vs pnl_per_share ===")
    from scipy.stats import spearmanr
    r, p = spearmanr(sub.ratio, sub.pnl_per_share)
    print(f"  spearman rho={r:+.4f} p={p:.3f}  (n={len(sub)})")
    r2, p2 = spearmanr(sub.ratio, sub.won.astype(float))
    print(f"  spearman rho(size_ratio, won)={r2:+.4f} p={p2:.3f}")


if __name__ == "__main__":
    main()
