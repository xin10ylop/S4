#!/usr/bin/env python3
"""A4 — the decisive joint test: snipe window x per_event_cap x tick phase,
plus tail characterisation of the large-cap trades."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from a4_timing_scan import Klines, load_tapes, load_windows  # noqa: E402
from a4_timing_final import pick, run_grid, summarize  # noqa: E402

D0, D1 = "2026-05-01", "2026-07-12"
PHASES = (0.0, 0.2, 0.4, 0.6, 0.8)
WINDOWS = [(1, 6), (2, 6), (2, 5), (2, 4), (3, 5), (1, 3), (3, 6)]


def main():
    with open(ROOT / "bot/config.yaml") as fh:
        y = yaml.safe_load(fh)
    sc = y["strategy"]["close_snipe"]
    base = dict(vol_window_secs=int(sc["vol_window_secs"]),
                sigma_1s_floor=float(sc["sigma_1s_floor"]), fair_cap=float(sc["fair_cap"]),
                price_min=float(sc["price_min"]), price_max=float(sc["price_max"]),
                fee_rate=float(y["fees"]["fee_rate"]), latency_ms=1500,
                cap_usd=25.0, edge_min=0.03)

    w = load_windows(D0, D1)
    days = sorted(pd.date_range(pd.Timestamp(D0) - pd.Timedelta(days=1),
                                pd.Timestamp(D1) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    kl = Klines(days)
    tapes = load_tapes(days, set(w.wts.astype("int64")), pre=200)
    n_closes = len(tapes)
    days_span = n_closes / 24.0
    print(f"closes with tape: {n_closes}  ({days_span:.1f} days)\n")

    # one grid per (phase, cap) — signals do not depend on cap, but fills do
    print("=== JOINT: window x cap x tick-phase, edge_min=0.03, lat=1500ms ===")
    print("   (each cell = mean over 5 tick phases; +/- = sd across phases)\n")
    results = {}
    trades = []
    for cap in (25, 100, 250, 500):
        for phi in PHASES:
            g = run_grid(w, kl, tapes, dict(base, cap_usd=cap), list(range(1, 9)),
                         phase=phi, cap_usd=cap)
            for lo, hi in WINDOWS:
                p = pick(g, lo, hi)
                r = summarize(p, n_closes, f"[{lo},{hi}]")
                results.setdefault((cap, lo, hi), []).append(r)
                if phi == 0.0:
                    for wts, row in p.items():
                        if row is not None and row.outcome == "filled":
                            trades.append(dict(cap=cap, window=f"[{lo},{hi}]", wts=wts,
                                               tau=row.tau, pnl=row.pnl, cost=row.cost,
                                               shares=row.shares, won=row.won))
    rows = []
    for (cap, lo, hi), rs in results.items():
        pnl = np.array([r["pnl"] for r in rs])
        cps = np.array([r["c_per_share"] for r in rs], dtype=float)
        fills = np.array([r["fills"] for r in rs])
        win = np.array([r["win"] for r in rs], dtype=float)
        rows.append(dict(cap=cap, window=f"[{lo},{hi}]", fills=fills.mean(),
                         win=np.nanmean(win), pnl=pnl.mean(), pnl_sd=pnl.std(),
                         pnl_min=pnl.min(), pnl_max=pnl.max(),
                         cps=np.nanmean(cps), per_day=pnl.mean() / days_span))
    df = pd.DataFrame(rows).sort_values(["cap", "window"])
    print(df.to_string(index=False, float_format=lambda v: f"{v:9.2f}"))

    print("\n=== TAIL: biggest single-trade losses at phi=0 ===")
    td = pd.DataFrame(trades)
    for cap in (25, 250, 500):
        for wname in ("[1,6]", "[2,5]", "[2,4]"):
            sub = td[(td.cap == cap) & (td.window == wname)]
            if sub.empty:
                continue
            worst = sub.nsmallest(3, "pnl")
            tot = sub.pnl.sum()
            top1 = sub.nlargest(1, "pnl").pnl.sum()
            print(f"  cap=${cap:4d} {wname}: total {tot:+8.2f} | worst 3 = "
                  f"{', '.join(f'{v:+.0f}' for v in worst.pnl)} | "
                  f"best single {top1:+.0f} ({100*top1/tot if tot else 0:.0f}% of total) | "
                  f"losses={int((sub.pnl<0).sum())}/{len(sub)}")

    print("\n=== per-trade notional distribution at cap=$250, window [2,5], phi=0 ===")
    sub = td[(td.cap == 250) & (td.window == "[2,5]")]
    if not sub.empty:
        print(f"  n={len(sub)}  median ${sub.cost.median():.2f}  mean ${sub.cost.mean():.2f}  "
              f"p90 ${sub.cost.quantile(.9):.2f}  max ${sub.cost.max():.2f}")
        print(f"  trades using the FULL $250 clip: {int((sub.cost > 249).sum())}/{len(sub)}")
        print(f"  max single-trade loss: ${sub.pnl.min():.2f}")

    print("\n=== drawdown proxy: worst rolling-10-trade PnL, cap=$250, phi=0 ===")
    for wname in ("[1,6]", "[2,5]", "[2,4]"):
        sub = td[(td.cap == 250) & (td.window == wname)].sort_values("wts")
        if len(sub) < 10:
            continue
        roll = sub.pnl.rolling(10).sum()
        print(f"  {wname}: worst 10-trade window {roll.min():+.2f}  "
              f"best {roll.max():+.2f}  n={len(sub)}")


if __name__ == "__main__":
    main()
