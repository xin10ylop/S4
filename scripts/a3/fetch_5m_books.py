#!/usr/bin/env python3
"""A3: fetch FRESH Telonex book_snapshot_5 for 5m BTC up/down markets.

For each (market, outcome) it downloads the close-date file, snapshots the book
(5 levels) on a 0.5 s grid spanning [close-12 s, close+2 s], and keeps only
those ~29 rows. Raw bytes are never written to disk, so the working set is tiny
and memory stays flat.
"""
import io
import os
import sys
import time
import threading
import pandas as pd
import numpy as np
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv('/home/user/S4/.env')
KEY = os.environ['TELONEX_API_KEY']
BASE = "https://api.telonex.io"
CA = "/root/.ccr/ca-bundle.crt"
OUT = "/home/user/S4/data/a3/books5"
os.makedirs(OUT, exist_ok=True)

FROM_D = sys.argv[1]
TO_D = sys.argv[2]                     # inclusive close-date

# snapshot grid, seconds relative to close
GRID = np.round(np.arange(-12.0, 2.01, 0.5), 2)

PCOLS = ([f"bid_price_{i}" for i in range(5)] + [f"bid_size_{i}" for i in range(5)]
         + [f"ask_price_{i}" for i in range(5)] + [f"ask_size_{i}" for i in range(5)])

m5 = pd.read_parquet('/home/user/S4/data/a3/telonex_5m_markets.parquet')
m5['end'] = pd.to_datetime(m5.end_date_us, unit='us', utc=True)
sub = m5[(m5.status == 'resolved')
         & (m5['end'] >= pd.Timestamp(FROM_D, tz='UTC'))
         & (m5['end'] < pd.Timestamp(TO_D, tz='UTC') + pd.Timedelta(days=1))].copy()
sub['close_s'] = sub.end_date_us // 1_000_000
sub['d'] = sub['end'].dt.strftime('%Y-%m-%d')
sub = sub.sort_values('end')
print(f"markets: {len(sub)}  {sub['end'].min()} .. {sub['end'].max()}", flush=True)

lock = threading.Lock()
rows = []
fail = []
client = httpx.Client(timeout=180, verify=CA, follow_redirects=True,
                      headers={"Authorization": f"Bearer {KEY}"},
                      limits=httpx.Limits(max_connections=20))


def one(row, oid):
    url = f"{BASE}/v1/downloads/polymarket/book_snapshot_5/{row.d}"
    df = None
    for attempt in range(4):
        try:
            r = client.get(url, params={"slug": row.slug, "outcome_id": oid})
            if r.status_code == 404:
                return ("404", row.slug, oid)
            r.raise_for_status()
            df = pd.read_parquet(io.BytesIO(r.content),
                                 columns=["timestamp_us"] + PCOLS)
            break
        except Exception as e:
            if attempt == 3:
                return ("err:" + type(e).__name__, row.slug, oid)
            time.sleep(1.0 + attempt)
    if df is None or df.empty:
        return ("empty", row.slug, oid)
    df = df.sort_values("timestamp_us")
    ts = df.timestamp_us.to_numpy(np.int64)
    tgt = (row.close_s * 1_000_000 + (GRID * 1_000_000)).astype(np.int64)
    idx = np.searchsorted(ts, tgt, side="right") - 1
    ok = idx >= 0
    if not ok.any():
        return ("nosnap", row.slug, oid)
    vals = df[PCOLS].to_numpy(dtype="float32")
    take = np.clip(idx, 0, len(ts) - 1)
    out = pd.DataFrame(vals[take], columns=PCOLS)
    out["rel"] = GRID
    out["close_s"] = np.int64(row.close_s)
    out["oid"] = np.int8(oid)
    out["book_age"] = (tgt - ts[take]) / 1e6
    out.loc[~ok, PCOLS] = np.nan
    out["d"] = row.d
    with lock:
        rows.append(out)
    return ("ok", row.slug, oid)


t0 = time.time()
done = 0
with ThreadPoolExecutor(max_workers=16) as ex:
    futs = [ex.submit(one, r, oid) for _, r in sub.iterrows() for oid in (0, 1)]
    for f in as_completed(futs):
        st, slug, oid = f.result()
        done += 1
        if st != "ok":
            fail.append((st, slug, oid))
        if done % 250 == 0:
            print(f"  {done}/{len(futs)}  {time.time()-t0:.0f}s  fails={len(fail)}", flush=True)

allr = pd.concat(rows, ignore_index=True)
allr.to_parquet(f"{OUT}/snaps.parquet", index=False)
print("wrote snaps", allr.shape, flush=True)
print("fails:", len(fail), fail[:20])
sub[["slug", "close_s", "d", "result_id", "market_id"]].to_parquet(f"{OUT}/_markets.parquet", index=False)
print("elapsed", time.time() - t0)
