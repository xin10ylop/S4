#!/usr/bin/env python3
"""C1: honest coverage report over whatever data/fresh5m/ currently holds."""
import glob
import os

import numpy as np
import pandas as pd

OUT = "/home/user/S4/data/fresh5m"
pd.set_option("display.width", 200)

mk = pd.read_parquet(f"{OUT}/markets.parquet")
rows = []
for d in sorted(mk.d.unique()):
    m = mk[mk.d == d]
    r = {"d": d, "markets": len(m), "resolved": int((m.status == "resolved").sum()),
         "evaluable": int(m.evaluable.sum()) if "evaluable" in m else -1}
    cp = f"{OUT}/crypto_prices/{d}.parquet"
    r["cp_rows"] = len(pd.read_parquet(cp, columns=["timestamp_us"])) if os.path.exists(cp) else 0
    qp = f"{OUT}/quotes/{d}.parquet"
    if os.path.exists(qp):
        q = pd.read_parquet(qp, columns=["close_s", "oid"])
        r["q_rows"] = len(q)
        r["q_tapes"] = len(q.drop_duplicates())
        r["q_mkts"] = q.close_s.nunique()
        r["q_mb"] = round(os.path.getsize(qp) / 1e6, 1)
    else:
        r.update(q_rows=0, q_tapes=0, q_mkts=0, q_mb=0.0)
    bp = f"{OUT}/books/{d}.parquet"
    if os.path.exists(bp):
        b = pd.read_parquet(bp, columns=["close_s", "oid"])
        r["b_rows"] = len(b)
        r["b_mkts"] = b.close_s.nunique()
        r["b_mb"] = round(os.path.getsize(bp) / 1e6, 1)
    else:
        r.update(b_rows=0, b_mkts=0, b_mb=0.0)
    rows.append(r)
s = pd.DataFrame(rows)
s.to_parquet(f"{OUT}/coverage_summary.parquet", index=False)
print(s.to_string(index=False))
print()
full = s[s.q_mkts > 0]
print(f"DAYS WITH QUOTES: {len(full)}  ({full.d.min()} .. {full.d.max()})")
print(f"markets={full.markets.sum()}  quote tapes={full.q_tapes.sum()}  quote rows={full.q_rows.sum():,}")
print(f"book days={int((s.b_mkts>0).sum())}  book windows={s.b_mkts.sum()}  book rows={s.b_rows.sum():,}")
print(f"quotes {full.q_mb.sum()/1000:.2f} GB   books {s.b_mb.sum()/1000:.2f} GB")
miss = full[full.q_mkts < full.markets]
if len(miss):
    print("\nDAYS WHERE SOME MARKET HAS NO QUOTE TAPE:")
    print(miss[["d", "markets", "q_mkts"]].to_string(index=False))
