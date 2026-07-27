#!/usr/bin/env python3
"""
C4 part 1b -- FLOOR vs LEVEL. Fit the sigma transformation that makes
fair = Phi(ln(S_t/S_open)/(sigma*sqrt(tau))) an honest probability.

Three competing shapes, all fitted on the same decision instants:
  (a) floor only     sigma = max(sigma_hat, F)
  (b) multiplier only sigma = k * sigma_hat
  (c) both           sigma = max(k * sigma_hat, F)
Scored by Brier and by log-loss (log-loss is the one that punishes confident
mistakes, which is exactly the failure mode a floor exists to prevent).

Also scans `fair_cap`, because at tau in [2,5] on a 5m contract ~86% of
decisions saturate the cap, so the cap -- not sigma -- is what sets the model's
tail probability most of the time.
"""
from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/c4"
WINDOWS = [30, 60, 120, 300]


def norm_cdf(z):
    from scipy.special import ndtr
    return ndtr(z)


def score(fair, up, cap=0.98):
    fair = np.clip(fair, 1 - cap, cap)
    brier = float(np.mean((fair - up) ** 2))
    ll = float(-np.mean(up * np.log(fair) + (1 - up) * np.log(1 - fair)))
    return brier, ll


def main():
    rows = []
    caprows = []
    for period in ("repo_IS", "fresh_OOS"):
        D = pd.read_parquet(f"{OUT}/decisions_{period}.parquet")
        up = (D.S_settle >= D.S_open).to_numpy().astype(float)
        lr = np.log(D.S_t / D.S_open).to_numpy()
        sq = np.sqrt(D.tau_eff.to_numpy())
        ties = float(np.mean(D.S_settle.to_numpy() == D.S_open.to_numpy()))
        for w in WINDOWS:
            sh = D[f"sig{w}"].to_numpy(float)
            ok = np.isfinite(sh) & (sh > 0) & np.isfinite(lr)
            s0, l0, u0, q0 = sh[ok], lr[ok], up[ok], sq[ok]

            def sc(sig, cap=0.98):
                return score(norm_cdf(l0 / (sig * q0)), u0, cap)

            # (a) floor only
            best_f, best_fb, best_fl = None, 1e9, 1e9
            for F in np.concatenate([[0.0], np.geomspace(2e-6, 3e-4, 80)]):
                b, ll = sc(np.maximum(s0, F))
                if ll < best_fl:
                    best_f, best_fb, best_fl = F, b, ll
            # (b) multiplier only
            best_k, best_kb, best_kl = None, 1e9, 1e9
            for k in np.geomspace(0.25, 8.0, 80):
                b, ll = sc(k * s0)
                if ll < best_kl:
                    best_k, best_kb, best_kl = k, b, ll
            # (c) both
            best_c, best_cb, best_cl = None, 1e9, 1e9
            for k in np.geomspace(0.4, 4.0, 30):
                for F in np.concatenate([[0.0], np.geomspace(2e-6, 2e-4, 30)]):
                    b, ll = sc(np.maximum(k * s0, F))
                    if ll < best_cl:
                        best_c, best_cb, best_cl = (k, F), b, ll
            b8, l8 = sc(np.maximum(s0, 8e-6))
            b3, l3 = sc(np.maximum(s0, 3e-5))
            rows.append(dict(period=period, vol_window=w, n=int(ok.sum()), tie_frac=ties,
                             shipped_floor_brier=b8, shipped_floor_logloss=l8,
                             floor3e5_brier=b3, floor3e5_logloss=l3,
                             best_floor=best_f, best_floor_brier=best_fb,
                             best_floor_logloss=best_fl,
                             best_mult=best_k, best_mult_brier=best_kb,
                             best_mult_logloss=best_kl,
                             best_both_k=best_c[0], best_both_F=best_c[1],
                             best_both_logloss=best_cl))
            # fair_cap scan at the shipped floor and at the best floor
            for cap in (0.95, 0.96, 0.97, 0.98, 0.99, 0.995):
                b, ll = sc(np.maximum(s0, 8e-6), cap)
                bb, lb = sc(np.maximum(s0, best_f), cap)
                caprows.append(dict(period=period, vol_window=w, fair_cap=cap,
                                    brier_floor8e6=b, logloss_floor8e6=ll,
                                    brier_bestfloor=bb, logloss_bestfloor=lb))
    r = pd.DataFrame(rows)
    r.to_csv(f"{OUT}/sigma_fit.csv", index=False)
    c = pd.DataFrame(caprows)
    c.to_csv(f"{OUT}/faircap_scan.csv", index=False)
    pd.set_option("display.width", 300, "display.max_columns", 40)
    print(r.to_string(index=False))
    print()
    print(c[c.vol_window == 120].to_string(index=False))


if __name__ == "__main__":
    main()
