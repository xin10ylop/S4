"""A1 Part 1(c): characterise the captured Chainlink series - cadence, repeats, publish lag, decimals,
and lead/lag vs Binance spot."""
import glob
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
files = sorted(glob.glob(f"{ROOT}/data/data/processed/daily/crypto_prices/*.parquet"))
cl = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
cl = cl.drop_duplicates("timestamp_us", keep="last").sort_values("timestamp_us").reset_index(drop=True)
cl["ts"] = cl.timestamp_us // 1_000_000

print("=== TIMESTAMP GRID ===")
print(f"  timestamp_us % 1e6 == 0 for {(cl.timestamp_us % 1_000_000 == 0).mean():.4%} of rows "
      f"-> oracle timestamps land on exact whole seconds")
print(f"  rows={len(cl):,}  distinct seconds={cl.ts.nunique():,}")

print("\n=== PRICE GRANULARITY ===")
def ndec(x):
    s = f"{x:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0
samp = cl.price.sample(200_000, random_state=0)
dd = samp.map(ndec)
print(dd.value_counts().sort_index().to_string())
print(f"  -> price carries sub-cent resolution ({(dd>2).mean():.2%} of prints have >2 decimals); "
      "an on-chain 8-decimal aggregator answer would be a *different* shape (see report)")

print("\n=== DOES THE PRICE CHANGE EVERY SECOND? ===")
d = cl.price.diff()
same = (d == 0)
print(f"  consecutive-row repeats (price unchanged vs previous captured row): {same.mean():.4%}")
print(f"  distinct prices / rows: {cl.price.nunique()/len(cl):.4%}")
# inter-change interval measured in ORACLE SECONDS (gap-aware)
ch = cl.loc[cl.price.diff().fillna(1) != 0, "ts"].values
iv = np.diff(ch)
print(f"  n price changes={len(ch):,}")
print("  inter-change interval (s) distribution:")
for q in [50, 75, 90, 95, 99, 99.9, 100]:
    print(f"    p{q}: {np.percentile(iv, q):.0f}")
print(f"    mean={iv.mean():.3f}s  ==1s: {(iv==1).mean():.3%}  <=2s: {(iv<=2).mean():.3%}  "
      f">=10s: {(iv>=10).mean():.4%}  >=60s: {(iv>=60).sum()} events  max={iv.max()}s")

print("\n=== PER-SECOND ABSOLUTE MOVE ===")
step = cl.price.diff().abs()
m1 = cl.ts.diff() == 1
s = step[m1]
print(f"  |Δprice| on adjacent seconds: median=${s.median():.3f} mean=${s.mean():.3f} "
      f"p90=${s.quantile(.9):.3f} p99=${s.quantile(.99):.3f} max=${s.max():.2f}")
print(f"  |Δprice| in bps of price: median={(s/cl.price[m1]).median()*1e4:.3f}bp")

print("\n=== PUBLISH LAG (server_timestamp_us - timestamp_us) ===")
lag = (cl.server_timestamp_us - cl.timestamp_us) / 1e6
print(f"  n={len(lag):,}  min={lag.min():.3f}s")
for q in [1, 5, 25, 50, 75, 90, 95, 99, 99.9]:
    print(f"    p{q}: {lag.quantile(q/100):.3f}s")
print(f"    max={lag.max():.3f}s  mean={lag.mean():.3f}s  negative={(lag<0).mean():.4%}")

print("\n=== CAPTURE LAG (local_timestamp_us - server_timestamp_us) ===")
lag2 = (cl.local_timestamp_us - cl.server_timestamp_us) / 1e6
for q in [50, 90, 99]:
    print(f"    p{q}: {lag2.quantile(q/100):.3f}s")
print(f"    max={lag2.max():.3f}s  mean={lag2.mean():.3f}s")

print("\n=== TOTAL LATENCY oracle-time -> our machine (local - timestamp) ===")
lag3 = (cl.local_timestamp_us - cl.timestamp_us) / 1e6
for q in [50, 90, 99]:
    print(f"    p{q}: {lag3.quantile(q/100):.3f}s")

# ---- lead/lag vs Binance 1s klines ----
print("\n=== CHAINLINK vs BINANCE 1s CLOSE: level + lead/lag ===")
bf = sorted(glob.glob(f"{ROOT}/data/data/processed/binance/klines_1s/**/*.parquet", recursive=True))
print(f"  binance kline files: {len(bf)}")
if bf:
    b = pd.read_parquet(bf[0])
    print("  binance cols:", list(b.columns))
    # take a common window
    bb = pd.concat([pd.read_parquet(f) for f in bf[:6]], ignore_index=True)
    tcol = "open_time" if "open_time" in bb.columns else bb.columns[0]
    bb["ts"] = bb[tcol] // 1_000_000
    ccol = "close" if "close" in bb.columns else None
    bb = bb[["ts", ccol]].rename(columns={ccol: "bpx"}).drop_duplicates("ts")
    j = cl[["ts", "price"]].merge(bb, on="ts", how="inner")
    print(f"  overlapping seconds: {len(j):,}")
    if len(j) > 10000:
        j = j.sort_values("ts").reset_index(drop=True)
        print(f"  level diff (chainlink - binance): median=${(j.price-j.bpx).median():.2f} "
              f"std=${(j.price-j.bpx).std():.2f}")
        dc = j.price.diff()
        db = j.bpx.diff()
        best = None
        for k in range(-12, 13):
            c = dc.corr(db.shift(-k))
            if best is None or c > best[1]:
                best = (k, c)
            if abs(k) <= 6:
                print(f"    corr(Δchainlink_t, Δbinance_t{k:+d}) = {c:.4f}")
        print(f"  -> peak cross-correlation at lag {best[0]:+d}s (positive k = chainlink LAGS binance by k s), r={best[1]:.4f}")
