#!/usr/bin/env python3
"""M1 step 3: per-market traded volume / liquidity per coin, straight from gamma.

Slugs are deterministic (<coin>-up-or-down-<month>-<day>-<year>-<hour><am|pm>-et,
ET wall clock), so the last N hourly closes are addressed directly rather than
paginated -- gamma caps offset at 3000 and cannot reach far-past markets.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gamma import gamma_markets  # noqa: E402

COINS = ["bitcoin", "ethereum", "solana", "xrp", "dogecoin", "bnb", "hype"]
ET = dt.timezone(dt.timedelta(hours=-4))   # EDT; slugs use ET wall clock


def slug_for(coin, close_utc):
    """Slug names the hour the candle OPENS, in ET."""
    o = (close_utc - dt.timedelta(hours=1)).astimezone(ET)
    h = o.hour % 12 or 12
    ap = "am" if o.hour < 12 else "pm"
    return f"{coin}-up-or-down-{calendar.month_name[o.month].lower()}-{o.day}-{o.year}-{h}{ap}-et"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--out", default="/home/user/S4/data/multicoin/gamma_volume.parquet")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    closes = [now - dt.timedelta(hours=i) for i in range(1, a.hours + 1)]
    jobs = [(c, t) for c in COINS for t in closes]

    def one(job):
        coin, t = job
        s = slug_for(coin, t)
        # gamma /markets hides resolved markets unless closed=true is passed
        r = None
        for extra in ({"closed": "true"}, {}):
            try:
                r = gamma_markets(slug=s, **extra)
            except Exception:
                r = None
            if r:
                break
        if not r:
            return None
        m = r[0]
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            outs = json.loads(m.get("outcomes") or "[]")
        except json.JSONDecodeError:
            toks, outs = [], []
        return {
            "coin": coin, "slug": s, "close_utc": t,
            "volumeNum": m.get("volumeNum"), "volumeClob": m.get("volumeClob"),
            "liquidityNum": m.get("liquidityNum"), "spread": m.get("spread"),
            "tick": m.get("orderPriceMinTickSize"), "min_order": m.get("orderMinSize"),
            "outcomes": ",".join(outs), "token0": toks[0] if toks else None,
            "token1": toks[1] if len(toks) > 1 else None,
            "closed": m.get("closed"), "outcomePrices": m.get("outcomePrices"),
            "resolutionSource": m.get("resolutionSource"),
            "takerBaseFee": m.get("takerBaseFee"), "makerBaseFee": m.get("makerBaseFee"),
            "rewardsMinSize": m.get("rewardsMinSize"),
            "rewardsMaxSpread": m.get("rewardsMaxSpread"),
        }

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for f in as_completed([ex.submit(one, j) for j in jobs]):
            r = f.result()
            if r:
                rows.append(r)
    d = pd.DataFrame(rows)
    if d.empty:
        print("nothing fetched", file=sys.stderr)
        return 1
    for c in ("volumeNum", "volumeClob", "liquidityNum", "spread"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d.to_parquet(a.out, index=False)

    g = d.groupby("coin").agg(
        n=("slug", "size"),
        med_volume=("volumeNum", "median"),
        p25_volume=("volumeNum", lambda s: s.quantile(0.25)),
        p75_volume=("volumeNum", lambda s: s.quantile(0.75)),
        total_volume=("volumeNum", "sum"),
        med_liquidity=("liquidityNum", "median"),
        med_spread=("spread", "median"),
    ).sort_values("med_volume", ascending=False)
    print(g.to_string(float_format=lambda v: f"{v:,.1f}"))
    print("\ntick sizes:", d.groupby(["coin", "tick"]).size().to_dict())
    print("min order:", d.groupby(["coin", "min_order"]).size().to_dict())
    print("fees (bps): taker", sorted(d.takerBaseFee.dropna().unique()),
          "maker", sorted(d.makerBaseFee.dropna().unique()))
    print("outcome ordering:", d.outcomes.value_counts().to_dict())
    print(f"\nwrote {a.out} rows={len(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
