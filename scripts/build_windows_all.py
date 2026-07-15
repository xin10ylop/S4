#!/usr/bin/env python3
"""Build a unified windows table covering all 4 families and the full date range.

Sources:
- vault windows.parquet (5m/15m/4h through 2026-05-12)
- Telonex markets metadata (data/telonex_btc_markets.parquet) for:
  * the 1h family (bitcoin-up-or-down-*) — wts mapped via the vault's 1h daily files
  * 5m/15m/4h windows after 2026-05-12 (slug carries wts directly)

Output: data/windows_all.parquet
  columns: family, slug, wts, duration, date, result_id (0=Up wins), settled_at_us, fee_rate
"""
import glob
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data/data/processed"

FEE_ERAS = [  # (start_date, end_date_inclusive, rate) per audit
    ("0000-00-00", "2026-01-04", 0.0),
    ("2026-01-05", "2026-03-29", 0.0624),
    ("2026-03-30", "2026-05-06", 0.072),
    ("2026-05-07", "9999-12-31", 0.07),
]


def fee_rate_for(date: str, family: str) -> float:
    # 5m launched already in fee era; 15m/4h were fee-free before 2026-01-05
    for a, b, r in FEE_ERAS:
        if a <= date <= b:
            return r
    return 0.07


def main():
    w = pd.read_parquet(P / "windows.parquet")
    base = w[["family", "slug", "wts", "duration", "date", "result_id",
              "settled_at_us", "fee_rate"]].copy()
    base["result_id"] = base["result_id"].astype(int)
    base["source"] = "vault"

    tl = pd.read_parquet(ROOT / "data/telonex_btc_markets.parquet")
    tl = tl[tl.status == "resolved"].copy()
    tl["result_id"] = pd.to_numeric(tl["result_id"], errors="coerce")
    tl = tl.dropna(subset=["result_id"])
    tl["result_id"] = tl["result_id"].astype(int)

    # --- families with wts in slug, rows not already in vault table
    rows = []
    pat = re.compile(r"btc-updown-(5m|15m|4h)-(\d+)")
    have = set(zip(base["family"], base["wts"]))
    dur_map = {"5m": 300, "15m": 900, "4h": 14400}
    for _, r in tl.iterrows():
        m = pat.fullmatch(r.slug)
        if not m:
            continue
        fam, wts = m.group(1), int(m.group(2))
        if (fam, wts) in have:
            continue
        date = pd.Timestamp(wts, unit="s", tz="UTC").strftime("%Y-%m-%d")
        rows.append((fam, r.slug, wts, dur_map[fam], date, int(r.result_id),
                     int(r.settled_at_us), fee_rate_for(date, fam), "telonex"))

    # --- 1h family: wts <-> slug mapping lives in the vault 1h daily files
    maps = []
    for f in sorted(glob.glob(str(P / "daily/1h/trades/*.parquet"))):
        df = pd.read_parquet(f, columns=["wts", "slug"])
        maps.append(df.drop_duplicates())
    m1h = pd.concat(maps).drop_duplicates().dropna(subset=["wts", "slug"])
    m1h["wts"] = m1h["wts"].astype("int64")
    # sanity: one wts per slug
    amb = m1h.groupby("slug")["wts"].nunique()
    assert (amb <= 1).all(), f"ambiguous slugs: {amb[amb > 1]}"
    m1h = m1h.drop_duplicates("slug").set_index("slug")["wts"]

    h = tl[tl.slug.str.startswith("bitcoin-up-or-down-")].copy()
    h["wts"] = h["slug"].map(m1h)
    h = h.dropna(subset=["wts"])
    h["wts"] = h["wts"].astype(int)
    for _, r in h.iterrows():
        date = pd.Timestamp(int(r.wts), unit="s", tz="UTC").strftime("%Y-%m-%d")
        rows.append(("1h", r.slug, int(r.wts), 3600, date, int(r.result_id),
                     int(r.settled_at_us), fee_rate_for(date, "1h"), "telonex"))

    extra = pd.DataFrame(rows, columns=["family", "slug", "wts", "duration", "date",
                                        "result_id", "settled_at_us", "fee_rate", "source"])
    out = pd.concat([base, extra], ignore_index=True)
    out = out.sort_values(["family", "wts"]).reset_index(drop=True)
    # dedupe (family, wts)
    out = out.drop_duplicates(["family", "wts"], keep="first")
    out.to_parquet(ROOT / "data/windows_all.parquet", index=False)
    print(out.groupby("family").agg(n=("slug", "count"), dmin=("date", "min"),
                                    dmax=("date", "max")).to_string())
    print("\nby source:")
    print(out.groupby(["family", "source"]).size().to_string())


if __name__ == "__main__":
    main()
