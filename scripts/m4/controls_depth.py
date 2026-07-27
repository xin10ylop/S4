#!/usr/bin/env python3
"""M4 guard 1: confound controls for the size_ratio result.

The headline comparison (LARGE offers win MORE) could be an artefact of price:
large offers sit at lower prices, and a lower price mechanically pays more per
share when it wins. So control for it three ways:
  1. within-price-bucket comparison
  2. OLS of pnl_per_share on log(size_ratio) with price as a covariate
  3. win/lose only (payout is binary, price-free)
Plus a check that the result is not driven by one regime: split by half-sample.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

RNG = np.random.default_rng(4242)


def day_boot_diff(a, b, col, n=20000):
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


def main(path, thr=5.0):
    f = pd.read_parquet(path)
    f = f[f.filled & np.isfinite(f.depth_ref_family) & (f.depth_ref_family > 0)].copy()
    f["ratio"] = f.fill_size / f.depth_ref_family
    f["large"] = f.ratio > thr
    print(f"{path.name}: n={len(f)} days={f.day.nunique()} "
          f"LARGE(>{thr:g}x)={int(f.large.sum())}")
    print(f"  overall: win={f.won.mean():.3f} c/share={100*f.pnl_per_share.mean():.2f}")

    print("\n-- 1. within price bucket --")
    f["pb"] = pd.cut(f.fill_px, [0.30, 0.45, 0.60, 0.75, 0.99])
    for b, sub in f.groupby("pb", observed=True):
        big, nrm = sub[sub.large], sub[~sub.large]
        if len(big) < 3 or len(nrm) < 3:
            print(f"  {str(b):<14} n={len(sub):<4} (too few in one arm: "
                  f"large={len(big)} normal={len(nrm)})")
            continue
        d, lo, hi, p = day_boot_diff(big, nrm, "pnl_per_share", n=5000)
        print(f"  {str(b):<14} n={len(sub):<4} large n={len(big):<3} win={big.won.mean():.3f} "
              f"{100*big.pnl_per_share.mean():6.2f}c | normal n={len(nrm):<3} "
              f"win={nrm.won.mean():.3f} {100*nrm.pnl_per_share.mean():6.2f}c | "
              f"diff {100*d:+6.2f}c p={p:.3f}")

    print("\n-- 2. OLS pnl_per_share ~ log10(size_ratio) + fill_px --")
    X = np.column_stack([np.ones(len(f)), np.log10(f.ratio.clip(lower=1e-6)), f.fill_px])
    y = f.pnl_per_share.to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(f) - X.shape[1]
    s2 = (resid @ resid) / dof
    cov = s2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    names = ["intercept", "log10(size_ratio)", "fill_px"]
    for nm, b, s in zip(names, beta, se):
        print(f"  {nm:<20} coef={b:+.4f} se={s:.4f} t={b/s:+.2f}")

    print("\n-- 3. win/lose only (price-free) --")
    big, nrm = f[f.large], f[~f.large]
    print(f"  LARGE  wins {int(big.won.sum())}/{len(big)} = {big.won.mean():.4f}")
    print(f"  normal wins {int(nrm.won.sum())}/{len(nrm)} = {nrm.won.mean():.4f}")
    d, lo, hi, p = day_boot_diff(big, nrm, "won", n=20000)
    print(f"  diff win-rate {d:+.4f} 95%CI [{lo:+.4f},{hi:+.4f}] p={p:.3f}")
    try:
        from scipy.stats import fisher_exact
        tbl = [[int(big.won.sum()), len(big) - int(big.won.sum())],
               [int(nrm.won.sum()), len(nrm) - int(nrm.won.sum())]]
        odds, pf = fisher_exact(tbl)
        print(f"  fisher exact (unclustered, optimistic) p={pf:.4f} table={tbl}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (fisher unavailable: {exc})")

    print("\n-- 4. stability: first half vs second half of the sample --")
    days = sorted(f.day.unique())
    cut = days[len(days) // 2]
    for label, sub in (("first half", f[f.day < cut]), ("second half", f[f.day >= cut])):
        big, nrm = sub[sub.large], sub[~sub.large]
        if len(big) < 3 or len(nrm) < 3:
            print(f"  {label}: too few")
            continue
        print(f"  {label:<12} large n={len(big):<3} win={big.won.mean():.3f} "
              f"{100*big.pnl_per_share.mean():6.2f}c | normal n={len(nrm):<3} "
              f"win={nrm.won.mean():.3f} {100*nrm.pnl_per_share.mean():6.2f}c")

    print("\n-- 5. worst-case check: any catastrophic LARGE losses? --")
    for label, sub in (("LARGE", f[f.large]), ("normal", f[~f.large])):
        losses = sub[~sub.won]
        print(f"  {label}: {len(losses)} losses; worst c/share="
              f"{100*sub.pnl_per_share.min():.2f}; "
              f"worst $={(sub.pnl_per_share*sub.shares).min():.2f}")


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/c2/m4_depth_fills.parquet"
    thr = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    main(p, thr)
