#!/usr/bin/env python3
"""C4 part 4b -- capacity tables from the cap sweep + depth profile."""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

OUT = "/home/user/S4/data/c4"
CAPS = ["cap25", "cap100", "cap250", "cap500", "cap1000", "capINF"]
BREAK = "2026-06-27"


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def main():
    pd.set_option("display.width", 320, "display.max_columns", 40)
    P = pd.read_parquet(f"{OUT}/capacity_profile.parquet")
    F = P[P.outcome == "filled"].copy()
    F["post"] = F.day >= BREAK

    print("=== 1. Fillable notional per FILLED signal, 25-level book at the fill instant ===")
    qs = [.1, .25, .5, .75, .9, .95]
    rows = []
    for lab, col in (("top level only", "depth_top"),
                     ("within 3c of best (+ price band)", "notional_3c"),
                     ("...and still clearing edge_min (= walk_asks capacity)", "notional_edge"),
                     ("whole 25-level book (reference only)", "notional_all")):
        r = dict(measure=lab, n=len(F), mean=F[col].mean())
        r.update({f"p{int(q*100)}": F[col].quantile(q) for q in qs})
        rows.append(r)
    t1 = pd.DataFrame(rows)
    print(t1.round(2).to_string(index=False))
    t1.to_csv(f"{OUT}/table_capacity_depth.csv", index=False)

    print("\n=== 1b. same, split at the 2026-06-27 liquidity break ===")
    rows = []
    for lab, sub in (("pre-break (Jun 1-26)", F[~F.post]), ("post-break (Jun 27-Jul 26)", F[F.post])):
        for col in ("depth_top", "notional_3c", "notional_edge"):
            r = dict(frame=lab, measure=col, n=len(sub), mean=sub[col].mean())
            r.update({f"p{int(q*100)}": sub[col].quantile(q) for q in (.1, .5, .9)})
            rows.append(r)
    t1b = pd.DataFrame(rows)
    print(t1b.round(2).to_string(index=False))
    t1b.to_csv(f"{OUT}/table_capacity_depth_split.csv", index=False)

    print("\n=== 1c. how often the USD cap, not the book, is the binding constraint ===")
    rows = []
    for c in (25, 100, 250, 500, 1000):
        rows.append(dict(cap_usd=c,
                         frac_cap_binds=float((F.notional_edge > c).mean()),
                         frac_book_binds=float((F.notional_edge <= c).mean()),
                         median_filled=float(np.minimum(F.notional_edge, c).median()),
                         mean_filled=float(np.minimum(F.notional_edge, c).mean())))
    t2 = pd.DataFrame(rows)
    print(t2.round(4).to_string(index=False))
    t2.to_csv(f"{OUT}/table_capacity_binding.csv", index=False)

    print("\n=== 2. marginal EV by clip ===")
    rows = []
    prev_pnl = prev_cap = 0.0
    for c in CAPS:
        tr = pd.read_parquet(f"{OUT}/cap_trades_{c}.parquet")
        f = tr[tr.outcome == "filled"]
        nd = tr.day.nunique()
        capv = 1e12 if c == "capINF" else float(c[3:])
        dep = float((f.avg_price * f.shares).sum())
        dpnl = f.groupby("day").pnl.sum().reindex(sorted(tr.day.unique()), fill_value=0.0)
        r = dict(cap=c, fills=len(f), ev_share_c=f.pnl_per_share.mean() * 100,
                 shares=f.shares.sum(), deployed=dep, deployed_day=dep / nd,
                 pnl=f.pnl.sum(), pnl_day=f.pnl.sum() / nd,
                 t_day_pnl=_t(dpnl.to_numpy()),
                 ret_on_notional=f.pnl.sum() / dep if dep else np.nan,
                 worst_trade=f.pnl.min(), best_trade=f.pnl.max(),
                 worst_day=dpnl.min(),
                 pnl_day_post=float(f[f.day >= BREAK].pnl.sum() / (tr[tr.day >= BREAK].day.nunique())),
                 ev_share_post_c=float(f[f.day >= BREAK].pnl_per_share.mean() * 100))
        if prev_cap:
            r["marg_$day_per_$cap"] = (r["pnl_day"] - prev_pnl) / (min(capv, 2000) - prev_cap)
        prev_pnl, prev_cap = r["pnl_day"], min(capv, 2000)
        rows.append(r)
    t3 = pd.DataFrame(rows)
    print(t3.round(4).to_string(index=False))
    t3.to_csv(f"{OUT}/table_capacity_caps.csv", index=False)

    print("\n=== 3. where the unbounded clip goes wrong (largest fills) ===")
    tr = pd.read_parquet(f"{OUT}/cap_trades_capINF.parquet")
    f = tr[tr.outcome == "filled"].copy()
    f["notional"] = f.avg_price * f.shares
    big = f.nlargest(12, "notional")[["day", "close_s", "side", "avg_price", "shares",
                                      "notional", "won", "pnl_per_share", "pnl"]]
    print(big.round(3).to_string(index=False))
    for thr in (250, 500, 1000, 2000):
        s = f[f.notional > thr]
        print(f"  fills with notional > ${thr}: n={len(s)}, win={s.won.mean():.3f}, "
              f"total pnl=${s.pnl.sum():.0f}, ev/share={s.pnl_per_share.mean()*100:.2f}c")


if __name__ == "__main__":
    main()
