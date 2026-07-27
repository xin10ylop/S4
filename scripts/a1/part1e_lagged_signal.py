"""A1 extra: given the MEASURED Chainlink publish lag, how good is the close-snipe signal for 5m/15m/4h?
At wall-clock decision time close-k, the freshest oracle print we can possibly have observed is the
one stamped close-k-L (L = publish lag). Measure P(sign(S_avail - S_open) == outcome) vs k.
Also: what does the same table look like using Binance instead (the basis-risk counterfactual)?"""
import glob
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
cl = pd.concat([pd.read_parquet(f, columns=["timestamp_us", "price"])
                for f in sorted(glob.glob(f"{ROOT}/data/data/processed/daily/crypto_prices/*.parquet"))],
               ignore_index=True).drop_duplicates("timestamp_us")
cl["ts"] = cl.timestamp_us // 1_000_000
pm = pd.Series(cl.price.values, index=cl.ts.values)

bb = pd.concat([pd.read_parquet(f, columns=["open_time", "close"])
                for f in sorted(glob.glob(f"{ROOT}/data/data/processed/binance/klines_1s/2026-0[4-7]-*.parquet"))],
               ignore_index=True)
bb["ts"] = bb.open_time // 1_000_000
bm = pd.Series(bb["close"].values, index=bb.ts.values)

w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
w = w[w.family.isin(["5m", "15m", "4h"])].copy()
w["close_ts"] = w.wts + w.duration
lo, hi = int(cl.ts.min()), int(cl.ts.max())
w = w[(w.wts >= lo) & (w.close_ts <= hi)].copy()
w["rid"] = pd.to_numeric(w.result_id, errors="coerce")
w["S_open"] = w.wts.map(pm)
w["truth_up"] = (w.rid == 0)

L = 2  # measured publish lag, rounded UP to whole seconds (median 1.12 s, p90 1.53 s)
print(f"publish lag assumed L={L}s (measured: p50=1.12s p90=1.53s p99=2.02s server-side)")
print("\nk = seconds before close at which we decide; freshest oracle print stamped close-k-L")
print(f"{'k':>4} | " + " | ".join(f"{f:>22}" for f in ["5m", "15m", "4h"]))
for k in [1, 2, 3, 5, 8, 10, 15, 20, 30, 60]:
    cells = []
    for fam in ["5m", "15m", "4h"]:
        g = w[w.family == fam]
        S_av = (g.close_ts - k - L).map(pm)
        m = S_av.notna() & g.S_open.notna() & g.rid.notna()
        gg = g[m]; sa = S_av[m]
        pred_up = sa.values >= gg.S_open.values
        acc = (pred_up == gg.truth_up.values).mean()
        cells.append(f"n={m.sum():5d} acc={acc:6.2%}")
    print(f"{k:>4} | " + " | ".join(f"{c:>22}" for c in cells))

print("\nSame table but the decision uses BINANCE spot at close-k (no lag, but wrong feed):")
print(f"{'k':>4} | " + " | ".join(f"{f:>22}" for f in ["5m", "15m", "4h"]))
for k in [1, 2, 3, 5, 8, 10, 15, 20, 30, 60]:
    cells = []
    for fam in ["5m", "15m", "4h"]:
        g = w[w.family == fam]
        b_t = (g.close_ts - k).map(bm)
        b_o = g.wts.map(bm)
        m = b_t.notna() & b_o.notna() & g.rid.notna()
        gg = g[m]
        pred_up = b_t[m].values >= b_o[m].values
        acc = (pred_up == gg.truth_up.values).mean()
        cells.append(f"n={m.sum():5d} acc={acc:6.2%}")
    print(f"{k:>4} | " + " | ".join(f"{c:>22}" for c in cells))

print("\nConditional on |S_avail - S_open| being large (the trades a snipe would actually take), 5m, k=2:")
g = w[w.family == "5m"]
S_av = (g.close_ts - 2 - L).map(pm)
m = S_av.notna() & g.S_open.notna() & g.rid.notna()
gg = g[m].copy(); gg["dist"] = (S_av[m] - gg.S_open).abs()
gg["ok"] = ((S_av[m].values >= gg.S_open.values) == gg.truth_up.values)
gg["bucket"] = pd.cut(gg.dist, [0, 2, 5, 10, 20, 40, 80, 1e9])
t = gg.groupby("bucket", observed=True).ok.agg(["size", "mean"])
t["cum_share"] = t["size"][::-1].cumsum()[::-1] / t["size"].sum()
print(t.to_string())
