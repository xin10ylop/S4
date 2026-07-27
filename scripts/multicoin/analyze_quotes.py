#!/usr/bin/env python3
"""M1: per-coin summary of the historical hourly quote tape.

For every close, snapshot the top of book as it stood at the decision instant
(the last quote at or before close - TAU) on both outcomes, and report per coin:

  frequency  what fraction of closes had a best ask inside the strategy's
             tradeable price band [0.30, 0.99] -- this is the binding
             constraint the multi-coin expansion is meant to relieve.
  depth      top-of-book ask size in shares and USD at the decision instant.
  staleness  quote age = decision instant - the vendor's own event timestamp
             (`timestamp_us`, the book's last-update instant), plus the number
             of top-of-book updates in the 120s before the close.  A low update
             count means few competing bots.

INDICATIVE SIGNAL SCREEN (not a backtest): using the shipped fair value
    fair = Phi( ln(S_t/S_open) / (sigma_1s * sqrt(tau)) )
with sigma_1s estimated over the trailing `vol_window_secs` and floored at
`sigma_1s_floor`, count closes where some ask is at least `edge_min` below
fair and inside the price band.  No fill simulation, no re-fetch latency, no
fee -- it upper-bounds trade frequency, it does not estimate PnL.  The real
backtest is M2's job.
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import sys

import numpy as np
import pandas as pd

OUT = "/home/user/S4/data/multicoin"
COINS = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
         "xrp": "XRPUSDT", "dogecoin": "DOGEUSDT", "bnb": "BNBUSDT",
         "hype": "HYPEUSDT"}

# shipped strategy parameters (bot/polybot/strategy.py + bot/config.yaml)
EDGE_MIN, PMIN, PMAX = 0.03, 0.30, 0.99
VOL_WINDOW, SIGMA_FLOOR, FAIR_CAP = 120, 8e-6, 0.98


def _phi(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def load_underlying(sym, days):
    fr = []
    for d in days:
        p = f"{OUT}/binance/{sym}/{d}.parquet"
        if os.path.exists(p):
            fr.append(pd.read_parquet(p))
    if not fr:
        return None
    k = pd.concat(fr, ignore_index=True).sort_values("ts_ms")
    k = k[k.volume > 0]                     # real prints only (see fetch.py stage_verify)
    return k.ts_ms.to_numpy(), k.open.to_numpy(), k.close.to_numpy()


def price_at(ts, cl, t_ms):
    """Last traded price at or before t_ms; NaN if none."""
    j = np.searchsorted(ts, t_ms, "right") - 1
    return np.where(j >= 0, cl[np.clip(j, 0, len(cl) - 1)], np.nan)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau", type=float, default=3.0,
                    help="decision instant for the depth/staleness snapshot")
    ap.add_argument("--taus", default="2.5,3.0,3.5,4.0,4.5,5.0",
                    help="taus swept by the indicative signal screen; the live bot "
                         "scans the whole band and takes the first qualifying side")
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--csv", default=f"{OUT}/quote_summary_by_coin.csv")
    a = ap.parse_args()

    mk = pd.read_parquet(f"{OUT}/markets.parquet")
    rows = []
    for coin in [c for c in a.coins.split(",") if c in COINS]:
        files = sorted(glob.glob(f"{OUT}/quotes/{coin}/*.parquet"))
        if not files:
            continue
        q = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        days = sorted({os.path.basename(f)[:-8] for f in files})
        sym = COINS[coin]
        und = load_underlying(sym, sorted(set(days) | {
            (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d") for d in days}))

        dec_us = ((q.close_s - a.tau) * 1_000_000).astype("int64")
        q = q[q.timestamp_us <= dec_us].copy()
        q["dec_us"] = ((q.close_s - a.tau) * 1_000_000).astype("int64")
        # last quote at or before the decision instant, per (close, outcome)
        snap = (q.sort_values("timestamp_us")
                  .groupby(["close_s", "oid"], as_index=False).tail(1).copy())
        snap["age_s"] = (snap.dec_us - snap.timestamp_us) / 1e6
        snap["vendor_lag_s"] = (snap.local_timestamp_us - snap.timestamp_us) / 1e6
        # updates in the 120s before close
        upd = (q[q.timestamp_us >= (q.close_s - VOL_WINDOW) * 1_000_000]
               .groupby(["close_s", "oid"]).size().rename("n_upd"))
        snap = snap.merge(upd, on=["close_s", "oid"], how="left")
        snap["n_upd"] = snap.n_upd.fillna(0)
        snap["ask_usd"] = snap.ask_price * snap.ask_size
        snap["in_band"] = snap.ask_price.between(PMIN, PMAX)

        n_closes = snap.close_s.nunique()
        # per close: was ANY side's ask inside the band?
        any_band = snap.groupby("close_s").in_band.max()

        # ---- indicative signal screen, swept over the whole decision band ----
        n_sig = np.nan
        if und is not None:
            ts, op, cl = und
            qs = q.sort_values("timestamp_us")
            hit_closes = set()
            for tau in [float(t) for t in a.taus.split(",")]:
                d_us = ((qs.close_s - tau) * 1_000_000).astype("int64")
                s2 = (qs[qs.timestamp_us <= d_us]
                      .groupby(["close_s", "oid"], as_index=False).tail(1))
                if s2.empty:
                    continue
                cs = s2.close_s.to_numpy()
                s_t = price_at(ts, cl, ((cs - tau) * 1000).astype("int64"))
                s_open = price_at(ts, cl, ((cs - 3600) * 1000).astype("int64"))
                s_pre = price_at(ts, cl, ((cs - tau - VOL_WINDOW) * 1000).astype("int64"))
                with np.errstate(divide="ignore", invalid="ignore"):
                    sig = np.abs(np.log(s_t / s_pre)) / math.sqrt(VOL_WINDOW)
                    sig = np.maximum(np.nan_to_num(sig, nan=SIGMA_FLOOR), SIGMA_FLOOR)
                    z = np.log(s_t / s_open) / (sig * math.sqrt(tau))
                fair_up = np.clip(_phi(z), 1 - FAIR_CAP, FAIR_CAP)
                fair = np.where(s2.oid.to_numpy() == 0, fair_up, 1.0 - fair_up)
                ask = s2.ask_price.to_numpy()
                ok = (ask >= PMIN) & (ask <= PMAX) & ((fair - ask) >= EDGE_MIN)
                hit_closes |= set(cs[np.nan_to_num(ok, nan=False).astype(bool)])
            n_sig = len(hit_closes)

        rows.append({
            "coin": coin, "symbol": sym, "days": len(days),
            "closes": n_closes,
            "frac_close_ask_in_band": float(any_band.mean()),
            "med_ask_size_sh": snap.loc[snap.in_band, "ask_size"].median(),
            "med_ask_usd": snap.loc[snap.in_band, "ask_usd"].median(),
            "p25_ask_usd": snap.loc[snap.in_band, "ask_usd"].quantile(0.25),
            "p75_ask_usd": snap.loc[snap.in_band, "ask_usd"].quantile(0.75),
            "med_quote_age_s": snap.age_s.median(),
            "p90_quote_age_s": snap.age_s.quantile(0.90),
            "med_upd_120s": snap.n_upd.median(),
            "med_vendor_lag_s": snap.vendor_lag_s.median(),
            "indic_signal_closes": n_sig,
            "indic_signals_per_day": n_sig / len(days) if n_sig == n_sig else np.nan,
        })
        print(f"  {coin}: {len(days)}d {n_closes} closes", flush=True)

    d = pd.DataFrame(rows)
    d.to_csv(a.csv, index=False)
    print(f"\n=== per-coin quote summary, decision instant tau={a.tau}s ===")
    print(d.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    if len(d) > 1:
        print(f"\nTOTAL indicative signals/day across coins: "
              f"{d.indic_signals_per_day.sum():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
