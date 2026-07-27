"""A1 Part 1b-extended: sign test over the FULL captured-series coverage (not just gt-populated rows),
plus inspection of the 11 rows where windows.parquet's own chainlink cols disagree with result_id."""
import glob
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
CP = f"{ROOT}/data/data/processed/daily/crypto_prices"

cl = pd.concat([pd.read_parquet(f, columns=["timestamp_us", "price"]) for f in sorted(glob.glob(f"{CP}/*.parquet"))],
               ignore_index=True)
cl = cl.drop_duplicates("timestamp_us", keep="last")
cl["ts"] = cl.timestamp_us // 1_000_000
price_map = pd.Series(cl.price.values, index=cl.ts.values)
lo, hi = int(cl.ts.min()), int(cl.ts.max())

for name in ["windows.parquet"]:
    w = pd.read_parquet(f"{ROOT}/data/data/processed/{name}")
wa = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
print("windows_all cols:", list(wa.columns), len(wa))
print(wa.family.value_counts().to_string())

w = w[w.family.isin(["5m", "15m", "4h"])].copy()
w["close_ts"] = w.wts.astype("int64") + w.duration.astype("int64")
w = w[(w.wts >= lo) & (w.close_ts <= hi) & (w.status == "resolved")].copy()
w["cp_open"] = w.wts.map(price_map)
w["cp_close"] = w.close_ts.map(price_map)
w["rid"] = pd.to_numeric(w.result_id, errors="coerce")

print(f"\n=== FULL-COVERAGE SIGN TEST ({w.date.min()}..{w.date.max()}) ===")
tot_n = tot_d = 0
for fam, g in list(w.groupby("family")) + [("ALL", w)]:
    m = g.cp_open.notna() & g.cp_close.notna() & g.rid.notna()
    gg = g[m]
    pred = np.where(gg.cp_close.values >= gg.cp_open.values, 0, 1)
    dis = int((pred != gg.rid.values).sum())
    print(f"  {fam:>4}: resolved={len(g):6,}  complete={len(gg):6,} ({len(gg)/len(g):.2%})  "
          f"agreement={1-dis/len(gg):.5%}  disagreements={dis}")
    if fam != "ALL":
        tot_n += len(gg); tot_d += dis
    if dis and fam != "ALL":
        bad = gg[pred != gg.rid.values]
        print(bad[["slug", "date", "wts", "cp_open", "cp_close", "result_id"]].head(10).to_string())

# ---- the 11 rows where windows.parquet chainlink cols disagree with result_id ----
gt = pd.read_parquet("/tmp/a1_gt_joined.parquet")
gt["rid"] = pd.to_numeric(gt.result_id, errors="coerce")
pred_gt = np.where(gt.close_chainlink.values >= gt.open_chainlink.values, 0, 1)
bad = gt[pred_gt != gt.rid.values].copy()
print(f"\n=== rows where windows.parquet open/close_chainlink disagrees with result_id: {len(bad)} ===")
bad["cp_present"] = bad.cp_open.notna() & bad.cp_close.notna()
print(f"  of which captured-series had a price at BOTH boundary seconds: {bad.cp_present.sum()}")
print(bad[["slug", "family", "date", "open_chainlink", "close_chainlink", "cp_open", "cp_close", "result_id"]].to_string())

# ---- how are gaps distributed (missing seconds)? ----
allsec = np.arange(lo, hi + 1)
present = np.isin(allsec, cl.ts.values)
miss = allsec[~present]
print(f"\n=== GAPS in captured series: {len(miss):,} missing seconds of {len(allsec):,} ({len(miss)/len(allsec):.3%}) ===")
if len(miss):
    d = np.diff(miss)
    runs = np.split(miss, np.where(d != 1)[0] + 1)
    rl = np.array([len(r) for r in runs])
    print(f"  gap runs: {len(runs):,}  len: median={np.median(rl):.0f} p90={np.percentile(rl,90):.0f} "
          f"p99={np.percentile(rl,99):.0f} max={rl.max()}")
    print(f"  runs of length 1: {(rl==1).mean():.2%}; >60s: {(rl>60).sum()}; >600s: {(rl>600).sum()}")
