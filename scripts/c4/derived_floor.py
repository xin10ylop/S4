#!/usr/bin/env python3
"""C4 -- test the floor DERIVED from the return distribution (4e-5), not a grid value.

Three independent empirical routes (C4 §1) put the honest 5m floor at 3-6e-5,
centred on ~4e-5.  The 6x4 grid brackets it (3e-5, 5e-5) but never evaluates it,
so this runs the derived value itself, on both tapes and both execution models.
"""
from __future__ import annotations
import sys, time
from dataclasses import replace
import pandas as pd

sys.path.insert(0, "/home/user/S4")
from scripts.c4.capacity import FRESH_CL, FRESH_DAYS, load_books_day       # noqa: E402
from scripts.c4.grid import REPO_DAYS, load_fresh_quotes_day               # noqa: E402
from scripts.fresh5m.replay import (Params, load_chainlink, load_repo_day, # noqa: E402
                                    run_window, summarise)

OUT = "/home/user/S4/data/c4"
FLOOR = 4e-5
CELLS = [(60, FLOOR), (120, FLOOR)]


def run(tag, days, loader, dirs, ladder, cap):
    base = Params(edge_min=0.03, per_event_cap_usd=cap, max_book_age_s=5.0,
                  causal=True, sigma_mode="bot", strike_mode="backfill",
                  winner_source="result_id", ladder=ladder, tau_grid=None)
    ps = {f"w{w}_f4e-05": replace(base, vol_window_secs=float(w), sigma_1s_floor=fl)
          for w, fl in CELLS}
    recs = {k: [] for k in ps}
    nd = nw = 0
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        feeds = {w: load_chainlink([prev, d], vol_window_secs=float(w),
                                   sigma_mode="bot", dirs=dirs) for w, _ in CELLS}
        if all(v is None for v in feeds.values()):
            continue
        wins = loader(d)
        if not wins:
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
    rows = []
    for k, v in recs.items():
        tr = pd.DataFrame(v)
        tr.to_parquet(f"{OUT}/derived_{tag}_{k}.parquet", index=False)
        s = summarise(tr, nd, nw, k)
        s["tag"] = tag
        rows.append(s)
    return pd.DataFrame(rows)


def main():
    out = []
    t0 = time.time()
    out.append(run("repo_IS_top", REPO_DAYS, lambda d: load_repo_day(d, "bookcurves"),
                   (), False, 250.0))
    print("repo done", time.time() - t0, flush=True)
    out.append(run("fresh_OOS_top", FRESH_DAYS, load_fresh_quotes_day,
                   (FRESH_CL,), False, 250.0))
    print("fresh top done", time.time() - t0, flush=True)
    out.append(run("fresh_OOS_ladder", FRESH_DAYS, load_books_day,
                   (FRESH_CL,), True, 250.0))
    print("fresh ladder done", time.time() - t0, flush=True)
    df = pd.concat(out, ignore_index=True)
    df["ev_c"] = df.ev_share * 100
    df.to_csv(f"{OUT}/derived_floor.csv", index=False)
    pd.set_option("display.width", 300, "display.max_columns", 40)
    print(df[["tag", "label", "days", "signals", "fills", "trades_day", "ev_c",
              "win", "t_trade", "t_day", "pnl_day"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
