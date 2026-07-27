#!/usr/bin/env python3
"""M1 step 3: quantify per-coin liquidity and quote STALENESS near the hourly close.

Reads the JSONL emitted by sample_books.py and reports, per coin:

  liquidity  spread, best-ask notional, ASK capacity inside the bot's own
             slippage bound (best_ask .. best_ask+0.03 -- the walk rule in
             bot/polybot/strategy.py), level count, and how often the best ask
             sits inside the tradeable price band [0.30, 0.99].
  staleness  book AGE = poll instant - the CLOB book's own last-update stamp,
             and the QUOTE UPDATE RATE = fraction of polls at which the book
             hash changed.  The update rate is the direct proxy for "how many
             bots are competing here".

VERIFIED: the `timestamp` field in a CLOB /books response is a real
last-update stamp, not a response stamp -- across the whole capture it never
moved while the book `hash` was unchanged (0 exceptions).  So book age is
meaningful.

CLOCK: raw age carries a constant offset (the CLOB stamp runs ~1.2s ahead of
this box's clock; /time is second-granular and cannot resolve it).  Every coin
is polled by the same clock in the same request, so the offset is common-mode.
`--calibrate` subtracts the minimum observed age so the freshest book reads 0
and cross-coin comparisons are exact.  Raw ages are printed too.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

SLIP = 0.03              # strategy.py walks asks while price <= best_ask + SLIP
PMIN, PMAX = 0.30, 0.99  # strategy.py price band


def levels(x):
    if not x:
        return []
    out = []
    for lv in x:
        try:
            out.append((float(lv["price"]), float(lv["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def row_stats(r):
    asks, bids = levels(r.get("asks")), levels(r.get("bids"))
    d = {"n_ask_lv": len(asks), "n_bid_lv": len(bids)}
    if bids:
        d["best_bid"] = bids[-1][0]          # ascending -> last is best
        d["bid_usd_total"] = sum(p * s for p, s in bids)
    if asks:
        ba = asks[0][0]
        d["best_ask"] = ba
        d["best_ask_sz"] = asks[0][1]
        d["best_ask_usd"] = asks[0][1] * ba
        d["ask_usd_total"] = sum(p * s for p, s in asks)
        # what the bot could actually lift in one walk
        d["cap_usd_slip3"] = sum(p * s for p, s in asks if p <= ba + SLIP + 1e-9)
        # ... restricted to the tradeable price band
        d["cap_usd_band"] = sum(p * s for p, s in asks
                                if p <= ba + SLIP + 1e-9 and PMIN <= p <= PMAX)
        d["in_band"] = float(PMIN <= ba <= PMAX)
    if asks and bids:
        d["spread"] = d["best_ask"] - d["best_bid"]
        d["mid"] = 0.5 * (d["best_ask"] + d["best_bid"])
    return d


def load(paths):
    recs = []
    for p in paths:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    stats = pd.DataFrame([row_stats(r) for r in recs])
    df = pd.concat([df.drop(columns=["bids", "asks"]), stats], axis=1)
    df["book_ts"] = pd.to_numeric(df.book_ts, errors="coerce") / 1000.0
    df["age_raw"] = df.req_ts - df.book_ts
    return df


def update_rate(df):
    """Fraction of consecutive polls at which the book hash changed, per coin."""
    out = {}
    for coin, g in df.groupby("coin"):
        ch = tot = 0
        for _, t in g.groupby("token_id"):
            t = t.sort_values("req_ts")
            prev = t.hash.shift()
            ch += int(t.hash.ne(prev).sum()) - 1
            tot += len(t) - 1
        out[coin] = ch / tot if tot else float("nan")
    return pd.Series(out, name="quote_update_rate")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/home/user/S4/data/multicoin/books")
    ap.add_argument("--tau-lo", type=float, default=2.0)
    ap.add_argument("--tau-hi", type=float, default=5.0)
    ap.add_argument("--calibrate", action="store_true", default=True)
    ap.add_argument("--csv", default="/home/user/S4/data/multicoin/liquidity_by_coin.csv")
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(a.dir, "books_*.jsonl")))
    df = load(paths)
    if df.empty:
        print("no book rows", file=sys.stderr)
        return 1
    off = df.age_raw.min() if a.calibrate else 0.0
    df["age_s"] = df.age_raw - off
    print(f"files={len(paths)} rows={len(df)} closes={df.slug.nunique()} "
          f"coins={df.coin.nunique()} clock_offset_applied={-off:+.2f}s", flush=True)
    print("closes sampled per coin:",
          df.groupby('coin').slug.nunique().to_dict(), flush=True)

    band = df[(df.tau >= a.tau_lo) & (df.tau <= a.tau_hi)]

    def agg(g):
        return pd.Series({
            "n_obs": len(g),
            "n_closes": g.slug.nunique(),
            "med_spread": g.spread.median(),
            "med_best_ask_usd": g.best_ask_usd.median(),
            "med_cap_usd_3c": g.cap_usd_slip3.median(),
            "p25_cap_usd_3c": g.cap_usd_slip3.quantile(0.25),
            "p75_cap_usd_3c": g.cap_usd_slip3.quantile(0.75),
            "med_cap_usd_band": g.cap_usd_band.median(),
            "med_ask_levels": g.n_ask_lv.median(),
            "frac_best_ask_in_band": g.in_band.mean(),
            "frac_no_asks": float((g.n_ask_lv == 0).mean()),
            "med_age_s": g.age_s.median(),
            "p90_age_s": g.age_s.quantile(0.90),
        })

    tab = band.groupby("coin").apply(agg, include_groups=False)
    tab = tab.join(update_rate(band).rename("upd_rate_band"))
    tab = tab.join(update_rate(df).rename("upd_rate_all"))
    tab = tab.sort_values("upd_rate_all", ascending=False)
    print(f"\n=== DECISION BAND tau in [{a.tau_lo},{a.tau_hi}]s  n={len(band)} ===")
    print(tab.to_string(float_format=lambda v: f"{v:,.3f}"))
    tab.to_csv(a.csv)

    print("\n=== book age (s, calibrated) over the whole capture ===")
    print(df.groupby("coin").age_s.describe(percentiles=[0.5, 0.9, 0.99])
          .to_string(float_format=lambda v: f"{v:,.2f}"))

    b = pd.cut(df.tau, [-30, 0, 2, 5, 10, 30, 60, 150, 600])
    for col, fmt in (("cap_usd_slip3", "{:,.0f}"), ("spread", "{:,.3f}"),
                     ("age_s", "{:,.2f}")):
        print(f"\n=== median {col} vs tau ===")
        piv = df.pivot_table(index=b, columns="coin", values=col,
                             aggfunc="median", observed=True)
        print(piv.to_string(float_format=lambda v: fmt.format(v)))

    print("\n=== tick size observed ===")
    print(df.groupby(["coin", "tick_size"]).size().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
