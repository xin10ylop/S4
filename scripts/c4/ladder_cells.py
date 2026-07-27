#!/usr/bin/env python3
"""C4 -- real (25-level ladder, $250 clip) execution at the cells that matter,
so the go/no-go is evaluated on the execution the bot actually performs rather
than on the top-of-book proxy the grid uses for repo/fresh comparability."""
from __future__ import annotations

import sys
import time
from dataclasses import replace

import pandas as pd

sys.path.insert(0, "/home/user/S4")
from scripts.c4.capacity import FRESH_CL, FRESH_DAYS, load_books_day  # noqa: E402
from scripts.fresh5m.replay import Params, load_chainlink, run_window, summarise  # noqa: E402

OUT = "/home/user/S4/data/c4"
CELLS = [(60, 0.0), (60, 8e-6), (60, 3e-5), (60, 5e-5),
         (120, 8e-6), (120, 3e-5), (120, 5e-5), (30, 8e-6)]


def main():
    base = Params(edge_min=0.03, per_event_cap_usd=250.0, max_book_age_s=5.0,
                  causal=True, sigma_mode="bot", strike_mode="backfill",
                  winner_source="result_id", ladder=True, tau_grid=None)
    ps = {f"w{w}_f{fl:g}": replace(base, vol_window_secs=float(w), sigma_1s_floor=fl)
          for w, fl in CELLS}
    recs = {k: [] for k in ps}
    nd = nw = 0
    for d in FRESH_DAYS:
        t0 = time.time()
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        feeds = {}
        for w, _ in CELLS:
            if w not in feeds:
                feeds[w] = load_chainlink([prev, d], vol_window_secs=float(w),
                                          sigma_mode="bot", dirs=(FRESH_CL,))
        if all(v is None for v in feeds.values()):
            continue
        wins = load_books_day(d)
        if not wins:
            continue
        for k, p in ps.items():
            cl = feeds[int(p.vol_window_secs)]
            for w in wins:
                r = run_window(w, cl, p)
                if r is not None:
                    recs[k].append(r)
        nd += 1
        nw += len(wins)
        print(f"  {d}: {len(wins)} windows {time.time()-t0:.1f}s", flush=True)
    rows = []
    for k, v in recs.items():
        tr = pd.DataFrame(v)
        tr.to_parquet(f"{OUT}/ladder_{k}.parquet", index=False)
        s = summarise(tr, nd, nw, k)
        rows.append(s)
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/ladder_cells.csv", index=False)
    pd.set_option("display.width", 300, "display.max_columns", 40)
    print(df[["label", "days", "signals", "fills", "trades_day", "ev_share",
              "win", "t_trade", "t_day", "pnl_day"]].to_string(index=False))


if __name__ == "__main__":
    main()
