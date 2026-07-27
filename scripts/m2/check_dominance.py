#!/usr/bin/env python3
"""STRUCTURAL CHECK (no books needed).

For every shared close (15m window + its last 5m sub-window) on every day where
crypto_prices exists, derive both strikes from Chainlink and assert the
dominance identity:  buying Up on the low-strike market and Down on the
high-strike market pays >= $1.  Any payoff of 0 falsifies the whole idea.
"""
import os, sys, math
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/fresh5m")
from replay import load_chainlink

CP = f"{ROOT}/data/data/processed/daily/crypto_prices"
days = sorted(f[:-8] for f in os.listdir(CP))

w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
w5 = w[w.family == "5m"].set_index("wts")["result_id"].to_dict()
w15 = w[w.family == "15m"].set_index("wts")["result_id"].to_dict()

rows = []
for d in days:
    prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    cl = load_chainlink([prev, d])
    if cl is None:
        continue
    d0 = int(pd.Timestamp(d).timestamp())
    for wts15 in range(d0, d0 + 86400, 900):
        wts5 = wts15 + 600
        if wts15 not in w15 or wts5 not in w5:
            continue
        r15, r5 = w15[wts15], w5[wts5]
        if pd.isna(r15) or pd.isna(r5):
            continue
        st = cl.strike(np.array([wts15, wts5, wts15 + 900]), mode="backfill")
        O15, O5, C = float(st[0]), float(st[1]), float(st[2])
        if not (math.isfinite(O15) and math.isfinite(O5) and math.isfinite(C)):
            continue
        low_is_5 = O5 <= O15
        up_low = (r5 == 0) if low_is_5 else (r15 == 0)
        dn_high = (r15 == 1) if low_is_5 else (r5 == 1)
        # chainlink-derived winners, for the cross-check
        cl_up5 = C >= O5
        cl_up15 = C >= O15
        rows.append(dict(day=d, wts15=wts15, O5=O5, O15=O15, C=C,
                         r5=int(r5), r15=int(r15),
                         payoff=float(up_low) + float(dn_high),
                         strike_gap=abs(O5 - O15),
                         low_is_5=low_is_5,
                         agree5=(cl_up5 == (r5 == 0)),
                         agree15=(cl_up15 == (r15 == 0)),
                         between_cl=bool(min(O5, O15) <= C < max(O5, O15))))

df = pd.DataFrame(rows)
df.to_parquet(f"{ROOT}/data/multicoin/m2/dominance_structural.parquet", index=False)
print(f"shared closes checked: {len(df)}  over {df.day.nunique()} days "
      f"({df.day.min()} .. {df.day.max()})")
print("\npayoff distribution (vendor result_id):")
print(df.payoff.value_counts().sort_index().to_string())
print(f"\nPAYOFF == 0 (dominance broken): {int((df.payoff == 0).sum())}"
      f"   ({(df.payoff == 0).mean()*100:.4f}%)")
print(f"PAYOFF == 2 (close between strikes): {int((df.payoff == 2).sum())}"
      f"   ({(df.payoff == 2).mean()*100:.2f}%)")
print(f"\nChainlink-vs-vendor winner agreement:  5m {df.agree5.mean()*100:.4f}%   "
      f"15m {df.agree15.mean()*100:.4f}%")
print(f"chainlink 'between' vs payoff==2 agreement: "
      f"{(df.between_cl == (df.payoff == 2)).mean()*100:.4f}%")
print(f"\nidentical strikes O5==O15: {int((df.O5 == df.O15).sum())}")
print("\n|O5-O15| (the 10-minute BTC move), $:")
print(df.strike_gap.describe(percentiles=[.1, .25, .5, .75, .9, .99]).to_string())
bad = df[df.payoff == 0]
if len(bad):
    print("\nCOUNTEREXAMPLES:")
    print(bad.head(20).to_string())
