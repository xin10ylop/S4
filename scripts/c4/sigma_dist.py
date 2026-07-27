#!/usr/bin/env python3
"""
C4 part 1 -- what IS the 1s Chainlink return process at a 5-minute horizon?

The shipped `sigma_1s_floor = 8e-6` and `vol_window_secs = 120` were calibrated
on the 1h family against a BINANCE 1s-kline series. `oracle.ChainlinkOracle`
carries an explicit CALIBRATION WARNING that the Chainlink feed moves nearly
every second whereas Binance klines are mostly flat, so the floor is not
transferable. This script measures the actual object.

Everything is computed AT REAL 5m DECISION INSTANTS (t = close - tau, tau in
2..5s) under the strict causality rule (`server_timestamp_us <= t`), not on a
convenience grid, because that is where the parameter is used.

Outputs (data/c4/):
  sigma_raw_returns.csv     raw 1s log-return distribution
  sigma_hat_dist.csv        sigma_hat percentiles per vol_window, per period
  sigma_calibration.csv     realized forward vol vs the trailing estimate
  fair_calibration.csv      Phi(z) vs realized P(Up), per (window, floor)
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/S4")
from scripts.fresh5m.replay import ChainlinkFeed, load_chainlink  # noqa: E402

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/c4"
os.makedirs(OUT, exist_ok=True)

WINDOWS = [30, 60, 120, 300]
FLOORS = [("none", 0.0), ("8e-6", 8e-6), ("1e-5", 1e-5), ("2e-5", 2e-5),
          ("3e-5", 3e-5), ("5e-5", 5e-5)]
TAUS = [5.0, 4.0, 3.0, 2.0]
FRESH_CL = f"{ROOT}/data/fresh5m/crypto_prices"


def daylist(a: str, b: str):
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(a, b, freq="D")]


REPO_DAYS = daylist("2026-04-02", "2026-05-12") + ["2026-07-06", "2026-07-07"]
FRESH_DAYS = [d for d in daylist("2026-06-01", "2026-07-26")
              if d not in ("2026-07-06", "2026-07-07")]


def norm_cdf(z):
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


def day_records(day: str, cl_dir: str | None) -> pd.DataFrame:
    """One row per (5m window, tau) decision instant."""
    prev = (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    dirs = (cl_dir,) if cl_dir else ()
    feeds = {}
    for w in WINDOWS:
        f = load_chainlink([prev, day], vol_window_secs=w, sigma_mode="bot", dirs=dirs)
        if f is None:
            return pd.DataFrame()
        feeds[w] = f
    ref = feeds[WINDOWS[0]]

    d0 = int(pd.Timestamp(day, tz="UTC").timestamp())
    closes = np.arange(d0 + 300, d0 + 86400 + 1, 300, dtype=np.int64)
    wts = closes - 300

    strike = ref.strike(wts, mode="backfill")
    settle = ref.settle(closes, mode="backfill")

    rows = []
    for tau in TAUS:
        t_us = np.rint((closes - tau) * 1e6).astype(np.int64)
        px, obs, pub = ref.latest_at(t_us, causal=True)
        tau_eff = (closes * 1e6 - obs) / 1e6
        obs_lag = (t_us - obs) / 1e6
        rec = dict(day=day, close_s=closes, wts=wts, tau=tau, S_t=px,
                   obs=obs, obs_lag=obs_lag, tau_eff=tau_eff,
                   S_open=strike, S_settle=settle)
        for w in WINDOWS:
            rec[f"sig{w}"] = feeds[w].sigma_at_obs(obs)
        rows.append(pd.DataFrame(rec))
    df = pd.concat(rows, ignore_index=True)
    df = df[(df.obs > 0) & np.isfinite(df.S_t) & np.isfinite(df.S_open)
            & np.isfinite(df.S_settle) & (df.obs_lag <= 4.0) & (df.obs_lag >= 0)]
    return df


def raw_returns(days, cl_dir):
    """Raw 1s log-return distribution on the observation-second grid, using
    only seconds we actually hold (the bot convention: no forward fill)."""
    keep = []
    for day in days:
        f = load_chainlink([day], vol_window_secs=120, sigma_mode="bot",
                           dirs=(cl_dir,) if cl_dir else ())
        if f is None:
            continue
        s = pd.Series(np.log(f.px_by_obs), index=f.sec_sorted).groupby(level=0).last()
        secs = s.index.to_numpy(np.int64)
        r = np.diff(s.to_numpy(float))
        gap = np.diff(secs)
        keep.append(pd.DataFrame(dict(r=r, gap=gap)))
    return pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()


def main():
    periods = [("repo_IS", REPO_DAYS, None), ("fresh_OOS", FRESH_DAYS, FRESH_CL)]

    # ---------------- 1. raw 1s return distribution -----------------------
    raw_rows = []
    for name, days, cld in periods:
        rr = raw_returns(days, cld)
        if rr.empty:
            continue
        g1 = rr[rr.gap == 1].r.to_numpy()
        allr = rr.r.to_numpy()
        for lbl, x in (("gap==1s only", g1), ("all consecutive prints", allr)):
            raw_rows.append(dict(
                period=name, subset=lbl, n=len(x),
                std=float(np.std(x, ddof=1)), mad=float(np.median(np.abs(x))),
                zero_frac=float(np.mean(x == 0.0)),
                p50_abs=float(np.percentile(np.abs(x), 50)),
                p90_abs=float(np.percentile(np.abs(x), 90)),
                p99_abs=float(np.percentile(np.abs(x), 99)),
                kurtosis=float(pd.Series(x).kurtosis()),
            ))
        raw_rows.append(dict(period=name, subset="gap distribution", n=len(rr),
                             std=np.nan, mad=np.nan,
                             zero_frac=float(np.mean(rr.gap == 1)),
                             p50_abs=float(np.percentile(rr.gap, 50)),
                             p90_abs=float(np.percentile(rr.gap, 90)),
                             p99_abs=float(np.percentile(rr.gap, 99)), kurtosis=np.nan))
    pd.DataFrame(raw_rows).to_csv(f"{OUT}/sigma_raw_returns.csv", index=False)
    print(pd.DataFrame(raw_rows).to_string(index=False), flush=True)

    # ---------------- 2/3/4. decision-instant measurements ----------------
    hat_rows, cal_rows, fair_rows = [], [], []
    for name, days, cld in periods:
        frames = []
        for day in days:
            d = day_records(day, cld)
            if not d.empty:
                frames.append(d)
        if not frames:
            continue
        D = pd.concat(frames, ignore_index=True)
        D.to_parquet(f"{OUT}/decisions_{name}.parquet", index=False)
        print(f"{name}: {len(D):,} decision instants over {D.day.nunique()} days", flush=True)

        # realized forward move over the actual horizon we are pricing
        D["fwd"] = np.log(D.S_settle / D.S_t)
        D["fwd_1s"] = D.fwd / np.sqrt(D.tau_eff)

        for w in WINDOWS:
            s = D[f"sig{w}"].to_numpy(float)
            ok = np.isfinite(s) & (s > 0)
            hat_rows.append(dict(
                period=name, vol_window=w, n=int(ok.sum()),
                nan_frac=float(1 - ok.mean()),
                **{f"p{q}": float(np.percentile(s[ok], q)) for q in (1, 5, 10, 25, 50, 75, 90, 99)},
                mean=float(s[ok].mean()),
                **{f"binds_{lbl}": float(np.mean(s[ok] < v)) for lbl, v in FLOORS if v > 0},
            ))
            # calibration: realized forward vol vs trailing estimate, by decile
            sub = D[ok & np.isfinite(D.fwd_1s)]
            q = pd.qcut(sub[f"sig{w}"], 10, labels=False, duplicates="drop")
            for k, g in sub.groupby(q):
                cal_rows.append(dict(
                    period=name, vol_window=w, decile=int(k) + 1, n=len(g),
                    sig_hat_mean=float(g[f"sig{w}"].mean()),
                    sig_hat_lo=float(g[f"sig{w}"].min()),
                    sig_hat_hi=float(g[f"sig{w}"].max()),
                    realized_fwd_1s_rms=float(np.sqrt(np.mean(g.fwd_1s ** 2))),
                    realized_fwd_1s_mad=float(np.median(np.abs(g.fwd_1s)) / 0.6745),
                    ratio_rms=float(np.sqrt(np.mean(g.fwd_1s ** 2)) / g[f"sig{w}"].mean()),
                ))
            # unconditional ratio
            cal_rows.append(dict(
                period=name, vol_window=w, decile=0, n=len(sub),
                sig_hat_mean=float(sub[f"sig{w}"].mean()),
                sig_hat_lo=float(sub[f"sig{w}"].min()), sig_hat_hi=float(sub[f"sig{w}"].max()),
                realized_fwd_1s_rms=float(np.sqrt(np.mean(sub.fwd_1s ** 2))),
                realized_fwd_1s_mad=float(np.median(np.abs(sub.fwd_1s)) / 0.6745),
                ratio_rms=float(np.sqrt(np.mean(sub.fwd_1s ** 2)) / sub[f"sig{w}"].mean()),
            ))

            # probability calibration of fair = Phi(z) per floor
            for lbl, fv in FLOORS:
                se = np.maximum(D[f"sig{w}"].to_numpy(float), fv)
                z = np.log(D.S_t / D.S_open) / (se * np.sqrt(D.tau_eff))
                fair = norm_cdf(z)
                fair = np.clip(fair, 0.02, 0.98)
                up = (D.S_settle >= D.S_open).to_numpy()
                m = np.isfinite(fair) & np.isfinite(se) & (se > 0)
                f2, u2 = fair[m], up[m]
                # Brier + a 5-bucket reliability table
                brier = float(np.mean((f2 - u2) ** 2))
                fair_rows.append(dict(period=name, vol_window=w, floor=lbl, bucket="ALL",
                                      n=int(m.sum()), pred=float(f2.mean()),
                                      actual=float(u2.mean()), brier=brier,
                                      frac_capped=float(np.mean((f2 <= 0.0201) | (f2 >= 0.9799)))))
                edges = [0, .1, .3, .5, .7, .9, 1.0001]
                bi = np.digitize(f2, edges) - 1
                for b in range(len(edges) - 1):
                    mm = bi == b
                    if mm.sum() < 30:
                        continue
                    fair_rows.append(dict(
                        period=name, vol_window=w, floor=lbl,
                        bucket=f"[{edges[b]:.2f},{edges[b+1]:.2f})", n=int(mm.sum()),
                        pred=float(f2[mm].mean()), actual=float(u2[mm].mean()),
                        brier=float(np.mean((f2[mm] - u2[mm]) ** 2)), frac_capped=np.nan))

    pd.DataFrame(hat_rows).to_csv(f"{OUT}/sigma_hat_dist.csv", index=False)
    pd.DataFrame(cal_rows).to_csv(f"{OUT}/sigma_calibration.csv", index=False)
    pd.DataFrame(fair_rows).to_csv(f"{OUT}/fair_calibration.csv", index=False)
    print("\n--- sigma_hat distribution ---")
    print(pd.DataFrame(hat_rows).to_string(index=False))
    print("\n--- forward-vol calibration (decile 0 = unconditional) ---")
    print(pd.DataFrame(cal_rows).to_string(index=False))


if __name__ == "__main__":
    pd.set_option("display.width", 260, "display.max_columns", 60)
    main()
