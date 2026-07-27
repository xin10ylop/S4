#!/usr/bin/env python3
"""Fetch the Telonex data covering the live paper-trading period (A2 audit).

Steps
  1. Pull the public Telonex markets dataset, filter to the hourly BTC family
     (slug 'bitcoin-up-or-down-*') with close (end_date) inside the audit
     period, cache to data/live_audit/hourly_markets.parquet.
  2. Download channel 'quotes' for BOTH outcomes (Up = outcome_id 0,
     Down = outcome_id 1) of every such market, for the market's close date
     (and the prior date when the close lands in the first hours of a UTC day,
     because a window that closes just after midnight starts the day before).
     to_date is EXCLUSIVE.

Usage:
    python3 scripts/fetch_live_audit_data.py --from 2026-07-16 --to 2026-07-26
    python3 scripts/fetch_live_audit_data.py --markets-only
Secrets: TELONEX_API_KEY comes from .env; it is never printed.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import os
import re
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/live_audit"
QUOTES = OUT / "quotes"
MARKETS_CACHE = OUT / "hourly_markets.parquet"
RAW_MARKETS_CACHE = OUT / "_markets_raw_btc.parquet"

# Live paper-trading period. The bot went live 2026-07-16 18:00Z.
PERIOD_START = "2026-07-16T18:00:00Z"

# EXACTLY the discovery regex the live bot uses (polymarket.py _HOURLY_RE).
# It deliberately excludes the DAILY 'bitcoin-up-or-down-on-<month>-<d>-<yyyy>'
# markets, which also start with the same prefix but are 24h windows.
HOURLY_RE = re.compile(r"^bitcoin-up-or-down-[a-z]+-\d{1,2}-\d{4}-\d{1,2}(am|pm)-et$")


def load_key() -> str:
    load_dotenv(ROOT / ".env")
    return os.environ["TELONEX_API_KEY"]


def build_markets(from_date: str, to_date: str) -> pd.DataFrame:
    """Filter the Telonex markets dataset down to the hourly family in-period."""
    if RAW_MARKETS_CACHE.exists():
        raw = pd.read_parquet(RAW_MARKETS_CACHE)
    else:
        from telonex import get_markets_dataframe

        m = get_markets_dataframe(exchange="polymarket")
        raw = m[m["slug"].str.startswith("bitcoin-up-or-down", na=False)].copy()
        OUT.mkdir(parents=True, exist_ok=True)
        raw.to_parquet(RAW_MARKETS_CACHE, index=False)

    lo = int(pd.Timestamp(from_date, tz="UTC").timestamp() * 1e6)
    hi = int(pd.Timestamp(to_date, tz="UTC").timestamp() * 1e6)
    h = raw[(raw.end_date_us >= lo) & (raw.end_date_us < hi)].copy()
    h = h[h.slug.str.match(HOURLY_RE)].copy()

    h["close_ts"] = (h.end_date_us // 1_000_000).astype("int64")
    h["window_start_ts"] = h.close_ts - 3600
    h["close_dt"] = pd.to_datetime(h.close_ts, unit="s", utc=True)
    h["close_date"] = h.close_dt.dt.strftime("%Y-%m-%d")
    h["window_start_date"] = pd.to_datetime(h.window_start_ts, unit="s", utc=True).dt.strftime(
        "%Y-%m-%d"
    )
    h["result_id"] = pd.to_numeric(h["result_id"], errors="coerce")
    cols = [
        "slug", "market_id", "asset_id_0", "asset_id_1", "window_start_ts", "close_ts",
        "close_dt", "close_date", "window_start_date", "result_id", "status",
        "quotes_from", "quotes_to",
    ]
    h = h[cols].sort_values("close_ts").reset_index(drop=True)
    OUT.mkdir(parents=True, exist_ok=True)
    h.to_parquet(MARKETS_CACHE, index=False)
    return h


def download_one(args) -> str:
    key, slug, outcome_id, day = args
    from telonex import download

    from telonex.exceptions import NotFoundError

    dest = QUOTES / f"outcome_{outcome_id}"
    for attempt in range(4):
        try:
            files = download(
                api_key=key, exchange="polymarket", channel="quotes", slug=slug,
                outcome_id=outcome_id, from_date=day,
                to_date=(dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat(),
                download_dir=str(dest),
            )
            return f"ok   {slug} o{outcome_id} {day} -> {len(files)} file(s)"
        except NotFoundError as exc:
            return f"MISS {slug} o{outcome_id} {day}: no data"
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                return f"FAIL {slug} o{outcome_id} {day}: {type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return "unreachable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", default=PERIOD_START)
    ap.add_argument("--to", dest="to_date", default="2026-07-26")
    ap.add_argument("--markets-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    h = build_markets(args.from_date, args.to_date)
    print(f"hourly markets in period: {len(h)}  "
          f"{h.close_dt.min()} .. {h.close_dt.max()}  -> {MARKETS_CACHE}")
    if args.markets_only:
        return

    key = load_key()
    QUOTES.mkdir(parents=True, exist_ok=True)
    jobs = []
    for _, r in h.iterrows():
        # close date + the PRIOR utc date: the "book as of t" is a backward
        # fill over the whole tape, and a window closing in the first hours of
        # a UTC day (or with no quote update for hours) needs the day before.
        prev = (dt.date.fromisoformat(r.close_date) - dt.timedelta(days=1)).isoformat()
        days = {r.close_date, r.window_start_date, prev}
        for oid in (0, 1):
            for d in sorted(days):
                jobs.append((key, r.slug, oid, d))
    print(f"{len(jobs)} download jobs")
    ok = fail = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(download_one, jobs):
            if res.startswith(("FAIL", "MISS")):
                fail += 1
                print(res, flush=True)
            else:
                ok += 1
    print(f"downloads: {ok} ok, {fail} failed")


if __name__ == "__main__":
    main()
