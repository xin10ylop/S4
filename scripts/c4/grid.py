#!/usr/bin/env python3
"""
C4 part 2 -- sigma_1s_floor x vol_window_secs grid on 5m.

  floor in {8e-6, 1e-5, 2e-5, 3e-5, 5e-5, none}
  vol_window_secs in {30, 60, 120, 300}

Run twice, and the two runs are NOT interchangeable:
  * repo (2026-04-02..05-12 + 07-06/07, 43 d)  = IN-SAMPLE, for tuning
  * fresh (2026-06-01..07-26 minus 07-06/07, 54 d) = OUT-OF-SAMPLE, for reporting

Both tapes are TOP-OF-BOOK so the two periods are directly comparable:
  repo  -> data/data/processed/daily/5m/bookcurves (one level; Down = 1 - bid_up)
  fresh -> data/fresh5m/quotes (real two-sided top of book, ALL 288 windows/day,
           i.e. no candidate prefilter and therefore no selection bias)

Everything else is the C2 pre-committed frame: shipped edge_min 0.03, the band
the bot actually runs (tau_lo = max(2.5, latency/1000+0.5) = 2.5 -> tau {5,4,3}),
strict Chainlink publication causality, book age <= 5 s rejected AT DECISION TIME,
and t_day (day-clustered) as the headline statistic.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/S4")
from scripts.fresh5m.replay import (  # noqa: E402
    INF, BookTape, ChainlinkFeed, Params, Window, load_chainlink, load_repo_day,
    run_window, summarise,
)

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/c4"
os.makedirs(OUT, exist_ok=True)
FRESH_CL = f"{ROOT}/data/fresh5m/crypto_prices"
FRESH_Q = f"{ROOT}/data/fresh5m/quotes"

WINDOWS = [30, 60, 120, 300]
FLOORS = [("none", 0.0), ("8e-6", 8e-6), ("1e-5", 1e-5), ("2e-5", 2e-5),
          ("3e-5", 3e-5), ("5e-5", 5e-5)]


def daylist(a, b):
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(a, b, freq="D")]


REPO_DAYS = daylist("2026-04-02", "2026-05-12") + ["2026-07-06", "2026-07-07"]
FRESH_DAYS = [d for d in daylist("2026-06-01", "2026-07-26")
              if d not in ("2026-07-06", "2026-07-07")]

_MK = None


def _markets():
    global _MK
    if _MK is None:
        m = pd.read_parquet(f"{ROOT}/data/fresh5m/markets.parquet",
                            columns=["close_s", "result_id"])
        m["result_id"] = pd.to_numeric(m.result_id, errors="coerce")
        _MK = {int(c): (int(v) if pd.notna(v) else None)
               for c, v in zip(m.close_s, m.result_id)}
    return _MK


def load_fresh_quotes_day(day: str):
    """Fresh top-of-book tape -> replay.Window list.

    One row per top-of-book update, real vendor `timestamp_us`, so the
    staleness filter measures true venue silence. Retains the last pre-cut
    update, which is what makes a multi-thousand-second freeze measurable at
    all (verified: 2026-07-21 shows rel = -2374 s on the frozen windows).
    """
    f = f"{FRESH_Q}/{day}.parquet"
    if not os.path.exists(f):
        return []
    q = pd.read_parquet(f, columns=["timestamp_us", "ask_price", "ask_size",
                                    "close_s", "oid"])
    q = q.sort_values("timestamp_us")
    res = _markets()
    out = []
    for cs, g in q.groupby("close_s", sort=True):
        tapes = {}
        for oid in (0, 1):
            h = g[g.oid == oid]
            if h.empty:
                tapes[oid] = BookTape(np.zeros(0, np.int64), np.zeros((0, 1)),
                                      np.zeros((0, 1)))
                continue
            h = h.drop_duplicates("timestamp_us", keep="last")
            tapes[oid] = BookTape(h.timestamp_us.to_numpy(np.int64),
                                  h.ask_price.to_numpy(float)[:, None],
                                  h.ask_size.to_numpy(float)[:, None])
        out.append(Window(day=day, wts=int(cs) - 300, close_s=int(cs),
                          result_id=res.get(int(cs)), up=tapes[0], dn=tapes[1]))
    return out


def cells(base: Params):
    ps = {}
    for w in WINDOWS:
        for lbl, fv in FLOORS:
            ps[f"w{w}_f{lbl}"] = replace(base, vol_window_secs=float(w),
                                         sigma_1s_floor=fv)
    return ps


def run_period(name, days, source, base: Params, verbose=True):
    ps = cells(base)
    recs = {k: [] for k in ps}
    nd = nw = 0
    for d in days:
        t0 = time.time()
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        dirs = (FRESH_CL,) if source == "fresh" else ()
        feeds = {}
        for w in WINDOWS:
            feeds[w] = load_chainlink([prev, d], vol_window_secs=float(w),
                                      sigma_mode=base.sigma_mode, dirs=dirs)
        if all(v is None for v in feeds.values()):
            print(f"  {d}: no chainlink", flush=True)
            continue
        wins = (load_repo_day(d, "bookcurves") if source == "repo"
                else load_fresh_quotes_day(d))
        if not wins:
            print(f"  {d}: no books", flush=True)
            continue
        for k, p in ps.items():
            cl = feeds[int(p.vol_window_secs)]
            if cl is None:
                continue
            for w in wins:
                r = run_window(w, cl, p)
                if r is not None:
                    recs[k].append(r)
        nd += 1
        nw += len(wins)
        if verbose:
            print(f"  {d}: {len(wins)} windows  {time.time()-t0:.1f}s", flush=True)
    rows = []
    for k, v in recs.items():
        tr = pd.DataFrame(v)
        w, fl = k.split("_f")
        s = summarise(tr, nd, nw, k)
        s.update(period=name, vol_window=int(w[1:]), floor=fl)
        rows.append(s)
        if not tr.empty:
            tr.to_parquet(f"{OUT}/trades_{name}_{k}.parquet", index=False)
    return pd.DataFrame(rows), nd, nw


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="both", choices=["repo", "fresh", "both"])
    ap.add_argument("--cap-usd", type=float, default=250.0)
    ap.add_argument("--max-book-age", type=float, default=5.0)
    ap.add_argument("--sigma-mode", default="bot", choices=["bot", "grid_ffill"])
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    base = Params(edge_min=0.03, per_event_cap_usd=a.cap_usd,
                  max_book_age_s=a.max_book_age, causal=True,
                  sigma_mode=a.sigma_mode, strike_mode="backfill",
                  winner_source="result_id", ladder=False, tau_grid=None)
    print("tau grid the bot actually runs:", base.taus(), flush=True)
    allr = []
    if a.period in ("repo", "both"):
        r, nd, nw = run_period("repo_IS", REPO_DAYS, "repo", base)
        print(f"repo_IS: {nd} days, {nw} windows", flush=True)
        allr.append(r)
    if a.period in ("fresh", "both"):
        r, nd, nw = run_period("fresh_OOS", FRESH_DAYS, "fresh", base)
        print(f"fresh_OOS: {nd} days, {nw} windows", flush=True)
        allr.append(r)
    df = pd.concat(allr, ignore_index=True)
    df.to_csv(f"{OUT}/grid{a.tag}.csv", index=False)
    pd.set_option("display.width", 300, "display.max_columns", 40)
    print(df[["period", "vol_window", "floor", "signals", "fills", "trades_day",
              "ev_share", "win", "t_trade", "t_day", "pnl_day"]].to_string(index=False))


if __name__ == "__main__":
    main()
