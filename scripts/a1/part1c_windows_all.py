"""A1: sign test against windows_all.parquet (independent result_id, full captured span),
plus test of whether windows.parquet's chainlink cols are DERIVED from the capture (circularity check)."""
import glob
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
cl = pd.concat([pd.read_parquet(f, columns=["timestamp_us", "price"])
                for f in sorted(glob.glob(f"{ROOT}/data/data/processed/daily/crypto_prices/*.parquet"))],
               ignore_index=True).drop_duplicates("timestamp_us", keep="last").sort_values("timestamp_us")
cl["ts"] = cl.timestamp_us // 1_000_000
price_map = pd.Series(cl.price.values, index=cl.ts.values)
ts_sorted = cl.ts.values
px_sorted = cl.price.values
lo, hi = int(ts_sorted[0]), int(ts_sorted[-1])

wa = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
wa = wa[wa.family.isin(["5m", "15m", "4h"])].copy()
wa["close_ts"] = wa.wts.astype("int64") + wa.duration.astype("int64")
wa = wa[(wa.wts >= lo) & (wa.close_ts <= hi)].copy()
wa["cp_open"] = wa.wts.map(price_map)
wa["cp_close"] = wa.close_ts.map(price_map)
wa["rid"] = pd.to_numeric(wa.result_id, errors="coerce")
print(f"=== windows_all.parquet sign test, {wa.date.min()}..{wa.date.max()} (source col: {wa.source.unique()[:5]}) ===")
for fam, g in list(wa.groupby("family")) + [("ALL", wa)]:
    m = g.cp_open.notna() & g.cp_close.notna() & g.rid.notna()
    gg = g[m]
    pred = np.where(gg.cp_close.values >= gg.cp_open.values, 0, 1)
    dis = int((pred != gg.rid.values).sum())
    print(f"  {fam:>4}: rows={len(g):6,} complete={len(gg):6,} ({len(gg)/len(g):6.2%}) "
          f"agreement={1-dis/max(len(gg),1):.5%} disagreements={dis}")
    if dis:
        b = gg[pred != gg.rid.values]
        print(b[["slug", "date", "cp_open", "cp_close", "result_id"]].head(20).to_string())

# --- how near-tie are the windows? sanity: agreement should hold even for tiny moves ---
m = wa.cp_open.notna() & wa.cp_close.notna() & wa.rid.notna()
g = wa[m].copy()
g["move"] = (g.cp_close - g.cp_open).abs()
print("\n[near-tie robustness] agreement bucketed by |close-open|:")
bins = [0, 0.5, 1, 2, 5, 10, 25, 1e9]
g["bucket"] = pd.cut(g.move, bins)
pred = np.where(g.cp_close.values >= g.cp_open.values, 0, 1)
g["ok"] = pred == g.rid.values
print(g.groupby("bucket", observed=True).ok.agg(["size", "mean"]).to_string())

# --- circularity check: on gap seconds, does windows.parquet's chainlink col == ffill of capture? ---
w = pd.read_parquet(f"{ROOT}/data/data/processed/windows.parquet")
w = w[w.open_chainlink.notna()].copy()
w["close_ts"] = w.wts.astype("int64") + w.duration.astype("int64")
w["cp_open"] = w.wts.map(price_map)
w["cp_close"] = w.close_ts.map(price_map)
gapo = w[w.cp_open.isna()]
gapc = w[w.cp_close.isna()]
print(f"\n[circularity] windows.parquet rows whose OPEN second is missing from capture: {len(gapo)}")

def ffill_at(t):
    i = np.searchsorted(ts_sorted, t, side="right") - 1
    return px_sorted[i] if i >= 0 else np.nan

def bfill_at(t):
    i = np.searchsorted(ts_sorted, t, side="left")
    return px_sorted[i] if i < len(px_sorted) else np.nan

for lbl, df, col, tcol in [("open", gapo, "open_chainlink", "wts"), ("close", gapc, "close_chainlink", "close_ts")]:
    if not len(df):
        continue
    ff = df[tcol].map(ffill_at)
    bf = df[tcol].map(bfill_at)
    print(f"  {lbl}: n={len(df)}  =={'ffill'}: {(df[col].values==ff.values).mean():.2%}  "
          f"==bfill: {(df[col].values==bf.values).mean():.2%}  "
          f"==neither: {((df[col].values!=ff.values)&(df[col].values!=bf.values)).mean():.2%}")
