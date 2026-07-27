#!/usr/bin/env python3
"""A4 — robustness grid for the snipe-timing question.

Loads klines + tapes ONCE, then re-runs the full signal/fill replay for every
(latency_ms, edge_min, cap_usd) variant and reports the window-policy table for
each, plus a chronological split-half stability check and per-trade t-tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from a4_timing_scan import (Klines, evaluate_second, fill_signal, load_tapes,  # noqa: E402
                            load_windows)

D0, D1 = "2026-05-01", "2026-07-12"
TAUS = list(range(1, 9))
WINDOWS = [(1, 6), (2, 6), (1, 5), (2, 5), (2, 4), (1, 3), (2, 3), (3, 5), (3, 6), (1, 8), (1, 4)]


def build(cfg, w, kl, tapes, taus=TAUS):
    rows = []
    for _, m in w.iterrows():
        wts, close_ts, rid = int(m.wts), int(m.close_ts), int(m.result_id)
        tape = tapes.get(wts)
        if tape is None:
            continue
        s_open = kl.hour_open(wts)
        if not np.isfinite(s_open):
            continue
        won_up = rid == 0
        for k in taus:
            s = evaluate_second(kl, close_ts - k, close_ts, s_open, tape, cfg)
            if s is None:
                rows.append(dict(wts=wts, date=m.date, tau=k, signal=False, outcome="none",
                                 shares=0.0, cost=0.0, pnl=0.0, won=False))
                continue
            f = fill_signal(s, tape, cfg)
            won = won_up if s["side"] == "up" else (not won_up)
            rows.append(dict(wts=wts, date=m.date, tau=k, signal=True, outcome=f["outcome"],
                             side=s["side"], edge=s["edge"], ask=s["ask"],
                             shares=f["shares"], cost=f["cost"], fees=f["fees"],
                             won=won, pnl=(1.0 if won else 0.0) * f["shares"] - f["cost"] - f["fees"]))
    return pd.DataFrame(rows)


def policy(g, lo, hi, n_closes):
    picks = []
    for wts, sub in g.groupby("wts"):
        s = sub[(sub.tau >= lo) & (sub.tau <= hi) & (sub.signal)]
        if s.empty:
            continue
        picks.append(s.sort_values("tau", ascending=False).iloc[0])
    if not picks:
        return dict(window=f"[{lo},{hi}]", signals=0, fills=0, pnl=0.0)
    pk = pd.DataFrame(picks)
    fl = pk[pk.outcome == "filled"]
    per = (fl.pnl / fl.shares) if len(fl) else pd.Series(dtype=float)
    t = (per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))) if len(per) > 1 else np.nan
    return dict(window=f"[{lo},{hi}]", signals=len(pk), fills=len(fl),
                fill_rate=100 * len(fl) / len(pk),
                win=(100 * fl.won.mean() if len(fl) else np.nan),
                shares=fl.shares.sum(), notional=fl.cost.sum(), pnl=fl.pnl.sum(),
                c_per_share=(100 * fl.pnl.sum() / fl.shares.sum() if fl.shares.sum() else np.nan),
                t_stat=t, per_day=fl.pnl.sum() / (n_closes / 24.0))


def main():
    with open(ROOT / "bot/config.yaml") as fh:
        y = yaml.safe_load(fh)
    sc = y["strategy"]["close_snipe"]
    base = dict(vol_window_secs=int(sc["vol_window_secs"]),
                sigma_1s_floor=float(sc["sigma_1s_floor"]), fair_cap=float(sc["fair_cap"]),
                price_min=float(sc["price_min"]), price_max=float(sc["price_max"]),
                fee_rate=float(y["fees"]["fee_rate"]))

    w = load_windows(D0, D1)
    days = sorted(pd.date_range(pd.Timestamp(D0) - pd.Timedelta(days=1),
                                pd.Timestamp(D1) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    kl = Klines(days)
    tapes = load_tapes(days, set(w.wts.astype("int64")))
    n_closes = len(tapes)
    print(f"closes with tape: {n_closes}\n")

    variants = [
        ("lat=1500 edge=0.05 cap=25", 1500, 0.05, 25),
        ("lat=1000 edge=0.05 cap=25", 1000, 0.05, 25),
        ("lat=2000 edge=0.05 cap=25", 2000, 0.05, 25),
        ("lat=1500 edge=0.03 cap=25", 1500, 0.03, 25),
        ("lat=1500 edge=0.02 cap=25", 1500, 0.02, 25),
        ("lat=1500 edge=0.03 cap=250", 1500, 0.03, 250),
        ("lat=1500 edge=0.05 cap=250", 1500, 0.05, 250),
    ]
    store = {}
    for name, lat, em, cap in variants:
        cfg = dict(base, latency_ms=lat, edge_min=em, cap_usd=cap)
        g = build(cfg, w, kl, tapes)
        store[name] = g
        print(f"--- {name} ---")
        rows = [policy(g, lo, hi, n_closes) for lo, hi in WINDOWS]
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:9.2f}"))
        # fixed-tau
        ft = []
        for k, sub in g.groupby("tau"):
            s = sub[sub.signal]
            fl = s[s.outcome == "filled"]
            ft.append(dict(tau=k, sig=len(s), fills=len(fl),
                           fill_rate=100 * len(fl) / len(s) if len(s) else np.nan,
                           pnl=fl.pnl.sum(),
                           cps=100 * fl.pnl.sum() / fl.shares.sum() if fl.shares.sum() else np.nan,
                           win=100 * fl.won.mean() if len(fl) else np.nan))
        print("  fixed-tau:", pd.DataFrame(ft).to_string(index=False,
                                                         float_format=lambda v: f"{v:7.2f}")
              .replace("\n", "\n             "))
        print()

    # ------------------------------------------------ chronological split-half
    print("\n=== SPLIT-HALF STABILITY (lat=1500 edge=0.03 cap=25) ===")
    g = store["lat=1500 edge=0.03 cap=25"]
    mid = sorted(g.date.unique())[len(g.date.unique()) // 2]
    for label, sub in (("first half", g[g.date < mid]), ("second half", g[g.date >= mid])):
        nc = sub.wts.nunique()
        print(f"\n  {label} ({nc} closes, dates < {mid} )" if label == "first half"
              else f"\n  {label} ({nc} closes, dates >= {mid})")
        rows = [policy(sub, lo, hi, nc) for lo, hi in [(1, 6), (2, 5), (2, 4), (1, 3), (2, 3)]]
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:9.2f}"))

    # ------------------------------------------------ paired test [1,6] vs [2,5]
    print("\n=== PAIRED per-close comparison, [1,6] vs candidate windows "
          "(lat=1500 edge=0.03 cap=25) ===")
    def pnl_map(gg, lo, hi):
        out = {}
        for wts, sub in gg.groupby("wts"):
            s = sub[(sub.tau >= lo) & (sub.tau <= hi) & (sub.signal)]
            out[wts] = (s.sort_values("tau", ascending=False).iloc[0].pnl if not s.empty else 0.0)
        return out
    a = pnl_map(g, 1, 6)
    for lo, hi in [(2, 5), (2, 4), (1, 3), (2, 3), (3, 5)]:
        b = pnl_map(g, lo, hi)
        keys = sorted(set(a) | set(b))
        d = np.array([b.get(k, 0.0) - a.get(k, 0.0) for k in keys])
        nz = d[d != 0]
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else np.nan
        print(f"  [{lo},{hi}] - [1,6]: total {d.sum():+8.2f}  mean/close {d.mean():+.4f} "
              f"t={t:+.2f}  n_differing={len(nz)}  better/worse={int((nz>0).sum())}/{int((nz<0).sum())}")


if __name__ == "__main__":
    main()
