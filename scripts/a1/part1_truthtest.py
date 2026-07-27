"""A1 Part 1: prove the captured crypto_prices series IS Polymarket's resolution source."""
import glob, os, sys
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
CP = f"{ROOT}/data/data/processed/daily/crypto_prices"

# ---- load full captured chainlink series over available coverage ----
files = sorted(glob.glob(f"{CP}/*.parquet"))
parts = []
for f in files:
    d = pd.read_parquet(f, columns=["timestamp_us", "server_timestamp_us", "local_timestamp_us", "price"])
    parts.append(d)
cl = pd.concat(parts, ignore_index=True).sort_values("timestamp_us").reset_index(drop=True)
print(f"[series] rows={len(cl):,} span={cl.timestamp_us.min()}..{cl.timestamp_us.max()}")
dup = cl.timestamp_us.duplicated().sum()
print(f"[series] duplicate timestamp_us rows: {dup}")
cl = cl.drop_duplicates("timestamp_us", keep="last").reset_index(drop=True)
cl["ts"] = cl.timestamp_us // 1_000_000
lo, hi = int(cl.ts.min()), int(cl.ts.max())
span = hi - lo + 1
print(f"[series] second-coverage {len(cl):,}/{span:,} = {len(cl)/span:.4%}")

price_map = pd.Series(cl.price.values, index=cl.ts.values)

# ---- windows ground truth ----
w = pd.read_parquet(f"{ROOT}/data/data/processed/windows.parquet")
w = w[w.family.isin(["5m", "15m", "4h"])].copy()
w["wts"] = w.wts.astype("int64")
w["close_ts"] = w.wts + w.duration.astype("int64")
gt = w[w.open_chainlink.notna() & w.close_chainlink.notna()].copy()
print(f"\n[gt] rows with open&close chainlink: {len(gt):,}  dates {gt.date.min()}..{gt.date.max()}")
print(gt.groupby("family").size().to_string())

# restrict to captured-series coverage
gt = gt[(gt.wts >= lo) & (gt.close_ts <= hi)].copy()
print(f"[gt] within captured coverage: {len(gt):,}")

def lookup(ts_series):
    return ts_series.map(price_map)

gt["cp_open"] = lookup(gt.wts)
gt["cp_close"] = lookup(gt.close_ts)

print("\n=== (a) EXACT MATCH AT WINDOW BOUNDARY SECONDS ===")
for fam, g in list(gt.groupby("family")) + [("ALL", gt)]:
    n = len(g)
    have = g.cp_open.notna() & g.cp_close.notna()
    gg = g[have]
    do = (gg.cp_open - gg.open_chainlink).abs()
    dc = (gg.cp_close - gg.close_chainlink).abs()
    exact_o = (do == 0).mean() if len(gg) else np.nan
    exact_c = (dc == 0).mean() if len(gg) else np.nan
    tol_o = (do <= 0.01).mean() if len(gg) else np.nan
    tol_c = (dc <= 0.01).mean() if len(gg) else np.nan
    print(f"{fam:>4}: n={n:6,} both-secs-present={have.mean():7.3%} "
          f"open exact={exact_o:7.3%} |d|<=0.01={tol_o:7.3%} med|d|={do.median():.6f} p99|d|={do.quantile(.99):.4f} max={do.max():.4f} | "
          f"close exact={exact_c:7.3%} |d|<=0.01={tol_c:7.3%} med|d|={dc.median():.6f} p99|d|={dc.quantile(.99):.4f} max={dc.max():.4f}")

# mismatch distribution detail
gg = gt[gt.cp_open.notna() & gt.cp_close.notna()].copy()
gg["do"] = (gg.cp_open - gg.open_chainlink).abs()
gg["dc"] = (gg.cp_close - gg.close_chainlink).abs()
print("\n[mismatch dist] |cp_open - open_chainlink| quantiles:")
print(gg.do.describe(percentiles=[.5, .9, .99, .999]).to_string())
print("\n[mismatch dist] |cp_close - close_chainlink| quantiles:")
print(gg.dc.describe(percentiles=[.5, .9, .99, .999]).to_string())
bad = gg[(gg.do > 0.01) | (gg.dc > 0.01)]
print(f"\n[mismatch] rows with either > $0.01: {len(bad):,} ({len(bad)/len(gg):.4%})")
if len(bad):
    print(bad[["slug", "family", "wts", "open_chainlink", "cp_open", "do", "close_chainlink", "cp_close", "dc"]].head(25).to_string())

# does open_chainlink of window == close_chainlink of previous window? (chain continuity)
print("\n[continuity] open==prev close within family:")
for fam, g in gt.groupby("family"):
    g = g.sort_values("wts")
    prev_close = g.close_chainlink.shift(1)
    contiguous = g.wts == g.wts.shift(1) + g.duration.shift(1)
    m = contiguous & prev_close.notna()
    if m.sum():
        print(f"  {fam}: contiguous pairs={m.sum():,} open==prevclose={(g.open_chainlink[m]==prev_close[m]).mean():.4%}")

print("\n=== (b) SIGN(close-open) FROM CAPTURED SERIES vs result_id (0=Up wins) ===")
gt["rid"] = pd.to_numeric(gt.result_id, errors="coerce")
for label, opx, cpx in [("captured-series", "cp_open", "cp_close"),
                        ("ground-truth-cols", "open_chainlink", "close_chainlink")]:
    print(f"\n-- source: {label}")
    for fam, g in list(gt.groupby("family")) + [("ALL", gt)]:
        m = g[opx].notna() & g[cpx].notna() & g.rid.notna()
        gg = g[m]
        if not len(gg):
            print(f"  {fam:>4}: no complete rows"); continue
        pred_up = (gg[cpx].values >= gg[opx].values)          # Up wins iff close>=open
        pred_rid = np.where(pred_up, 0, 1)
        agree = (pred_rid == gg.rid.values).mean()
        drop = len(g) - len(gg)
        print(f"  {fam:>4}: complete={len(gg):6,} (dropped {drop:,} NaN) agreement={agree:.4%} "
              f"disagreements={(pred_rid != gg.rid.values).sum()}")

# strict > vs >= tie handling
m = gt.cp_open.notna() & gt.cp_close.notna() & gt.rid.notna()
g = gt[m]
ties = (g.cp_close == g.cp_open).sum()
print(f"\n[ties] exact cp_close==cp_open: {ties}")
for op, name in [(np.greater_equal, "close>=open => Up"), (np.greater, "close>open => Up")]:
    pr = np.where(op(g.cp_close.values, g.cp_open.values), 0, 1)
    print(f"  {name}: agreement={(pr==g.rid.values).mean():.4%}")

# the NaN trap demonstration
print("\n[NaN trap] naive computation that does NOT drop NaN (what an earlier attempt likely did):")
allrows = gt.copy()
naive_pred = np.where(allrows.cp_close.values >= allrows.cp_open.values, 0, 1)  # NaN>=NaN -> False -> predicts 1
mm = allrows.rid.notna()
print(f"  over ALL {mm.sum():,} rows incl. NaN prices: agreement={(naive_pred[mm.values]==allrows.rid[mm].values).mean():.4%}")
for fam, gx in allrows.groupby("family"):
    npd = np.where(gx.cp_close.values >= gx.cp_open.values, 0, 1)
    mmx = gx.rid.notna()
    print(f"    {fam}: {(npd[mmx.values]==gx.rid[mmx].values).mean():.4%}")

OUT = "/tmp/a1_gt_joined.parquet"  # intermediate; consumed by part1b_full.py
gt.to_parquet(OUT)
print(f"\nsaved {OUT}")
