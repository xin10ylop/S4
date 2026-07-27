#!/usr/bin/env python3
"""
C4 part 4 -- 5m CAPACITY from the 25-level `book_snapshot_25` tape.

Question: how much notional can one 5m signal actually absorb inside the bot's
own execution bound, and what does per-share EV do as the clip grows?

The bound is not "the whole book". `fill_engine.walk_asks` stops at the FIRST
level that violates any of:
    price > best_ask + max_walk_above_best (0.03)
    price outside (price_min, price_max) = (0.30, 0.99)
    edge_fn(price) <= edge_min (0.03)          <- fair is frozen at signal time
    the USD cap is exhausted
so the capacity of a signal is the notional stacked in the levels that survive
ALL of those, not the book's total depth.

Method: run the shipped-parameter replay on `data/fresh5m/books` (25 levels,
both tokens, real update timestamps) at caps {25, 100, 250, 500, 1000, inf} and
compare. `cap = inf` measures the unconstrained fillable notional per signal;
the finite caps measure what per-share EV does as you use it.

CAVEAT, stated up front: books were fetched only for `candidates.parquet`
windows. The prepass is a superset (edge_min = 0, sigma swept 0.5x/1x/2x) but it
is still a filter, so §"coverage" below measures what fraction of the fills the
UNFILTERED quotes tape produces actually have a book, rather than assuming it.
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
    INF, Level, Params, fee_per_share, load_chainlink, load_fresh_day,
    run_window, summarise, walk_asks_port,
)

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/c4"
FRESH_CL = f"{ROOT}/data/fresh5m/crypto_prices"
FRESH_BOOKS = f"{ROOT}/data/fresh5m/books"
CAPS = [25.0, 100.0, 250.0, 500.0, 1000.0, 1e12]


def daylist(a, b):
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(a, b, freq="D")]


FRESH_DAYS = [d for d in daylist("2026-06-01", "2026-07-26")
              if d not in ("2026-07-06", "2026-07-07")]

_MK = None


def markets():
    global _MK
    if _MK is None:
        m = pd.read_parquet(f"{ROOT}/data/fresh5m/markets.parquet",
                            columns=["close_s", "result_id"])
        m["result_id"] = pd.to_numeric(m.result_id, errors="coerce")
        _MK = {int(c): (int(v) if pd.notna(v) else None)
               for c, v in zip(m.close_s, m.result_id)}
    return _MK


def load_books_day(day):
    ws = load_fresh_day(day, FRESH_BOOKS)
    mk = markets()
    for w in ws:
        if w.result_id is None:
            w.result_id = mk.get(w.close_s)
    return ws


def depth_profile(levels, fair, p: Params):
    """Notional available to `walk_asks` under each binding constraint, so the
    binding one can be named rather than guessed."""
    if not levels:
        return dict(n_lev=0, best=np.nan, notional_3c=0.0, notional_edge=0.0,
                    notional_all=0.0, shares_edge=0.0, vwap_edge=np.nan,
                    depth_top=0.0)
    best = levels[0].price
    tot = tot3 = tote = 0.0
    she = 0.0
    for i, lv in enumerate(levels):
        if not (p.price_min < lv.price < p.price_max):
            break
        if i > 0 and lv.price > best + p.max_walk_above_best + 1e-9:
            break
        tot3 += lv.price * lv.size
        if fair - lv.price - fee_per_share(lv.price, p.fee_rate) > p.edge_min:
            tote += lv.price * lv.size
            she += lv.size
        else:
            break
    for lv in levels:
        tot += lv.price * lv.size
    return dict(n_lev=len(levels), best=best, notional_3c=tot3,
                notional_edge=tote, notional_all=tot, shares_edge=she,
                vwap_edge=(tote / she if she > 0 else np.nan),
                depth_top=levels[0].price * levels[0].size)


def main():
    base = Params(edge_min=0.03, per_event_cap_usd=1e12, max_book_age_s=5.0,
                  causal=True, sigma_mode="bot", strike_mode="backfill",
                  winner_source="result_id", ladder=True, tau_grid=None)
    ps = {f"cap{int(c) if c < 1e11 else 'INF'}": replace(base, per_event_cap_usd=c)
          for c in CAPS}
    recs = {k: [] for k in ps}
    prof = []
    nd = nw = 0
    for d in FRESH_DAYS:
        t0 = time.time()
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_chainlink([prev, d], vol_window_secs=120.0, sigma_mode="bot",
                            dirs=(FRESH_CL,))
        if cl is None:
            print(f"  {d}: no chainlink", flush=True)
            continue
        wins = load_books_day(d)
        if not wins:
            print(f"  {d}: no books", flush=True)
            continue
        day_inf = []
        for k, p in ps.items():
            for w in wins:
                r = run_window(w, cl, p)
                if r is not None:
                    recs[k].append(r)
                    if k == "capINF":
                        day_inf.append((w, r))
        # depth profile at the fill instant of every unconstrained signal
        lat = int(base.latency_ms * 1000)
        for w, r in day_inf:
            t_us = int(np.rint((r["close_s"] - r["tau"]) * 1e6)) + lat
            tape = w.up if r["side"] == "up" else w.dn
            lv, age = tape.at(t_us)
            pr = depth_profile(lv or [], r["fair"], base)
            pr.update(day=d, close_s=r["close_s"], tau=r["tau"], side=r["side"],
                      fair=r["fair"], sig_ask=r["sig_ask"], outcome=r["outcome"],
                      book_age_fil=age, won=r["won"], filled_shares=r["shares"],
                      avg_price=r["avg_price"], pnl_per_share=r["pnl_per_share"])
            prof.append(pr)
        nd += 1
        nw += len(wins)
        print(f"  {d}: {len(wins)} windows  {time.time()-t0:.1f}s", flush=True)

    rows = []
    for k, v in recs.items():
        tr = pd.DataFrame(v)
        s = summarise(tr, nd, nw, k)
        s["cap"] = k
        rows.append(s)
        tr.to_parquet(f"{OUT}/cap_trades_{k}.parquet", index=False)
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/capacity_caps.csv", index=False)
    pd.set_option("display.width", 300, "display.max_columns", 40)
    print(df[["cap", "days", "signals", "fills", "trades_day", "ev_share", "win",
              "t_trade", "t_day", "shares", "deployed", "pnl", "pnl_day"]].to_string(index=False))
    P = pd.DataFrame(prof)
    P.to_parquet(f"{OUT}/capacity_profile.parquet", index=False)
    F = P[P.outcome == "filled"]
    print(f"\nfilled signals with a book: {len(F)}")
    for col in ("depth_top", "notional_3c", "notional_edge", "notional_all"):
        q = F[col].quantile([.1, .25, .5, .75, .9]).to_dict()
        print(f"  {col:14s} p10={q[0.1]:9.2f} p25={q[0.25]:9.2f} p50={q[0.5]:9.2f} "
              f"p75={q[0.75]:9.2f} p90={q[0.9]:9.2f} mean={F[col].mean():9.2f}")
    print("  levels used within the 3c band:",
          F.n_lev.describe().round(2).to_dict())


if __name__ == "__main__":
    main()
