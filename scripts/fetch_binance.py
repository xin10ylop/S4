#!/usr/bin/env python3
"""Fetch Binance BTCUSDT 1s klines from data.binance.vision for missing dates.

Writes parquet files matching the vault's binance/klines_1s schema into
data/data/processed/binance/klines_1s/.
"""
import concurrent.futures as cf
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).resolve().parent.parent / "data/data/processed/binance/klines_1s"
OUT.mkdir(parents=True, exist_ok=True)

COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


def fetch_day(day: dt.date) -> str:
    dest = OUT / f"{day.isoformat()}.parquet"
    if dest.exists():
        return f"skip {day}"
    url = (
        "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/"
        f"BTCUSDT-1s-{day.isoformat()}.zip"
    )
    r = requests.get(url, timeout=120)
    if r.status_code == 404:
        return f"404  {day}"
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    name = zf.namelist()[0]
    df = pd.read_csv(zf.open(name), header=None, names=COLS)
    if df.iloc[0]["open_time"] == "open_time":  # header row present in some files
        df = df.iloc[1:].reset_index(drop=True)
    df = df.drop(columns=["ignore"])
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_base", "taker_buy_quote"]:
        df[c] = df[c].astype(float)
    df["n_trades"] = df["n_trades"].astype("int32")
    for c in ["open_time", "close_time"]:
        df[c] = df[c].astype("int64")
    # vault files use microseconds; binance.vision daily 1s klines are in
    # microseconds already for recent data, milliseconds for older - normalize
    if df["open_time"].iloc[0] < 10**15:
        df["open_time"] *= 1000
        df["close_time"] = df["close_time"] * 1000 + 999
    df.to_parquet(dest, index=False)
    return f"ok   {day} ({len(df)} rows)"


def main():
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    days = [start + dt.timedelta(i) for i in range((end - start).days + 1)]
    days = [d for d in days if not (OUT / f"{d.isoformat()}.parquet").exists()]
    print(f"fetching {len(days)} days")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(fetch_day, days):
            print(res, flush=True)


if __name__ == "__main__":
    main()
