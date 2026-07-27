#!/usr/bin/env python3
"""M2 census: EVERY cross-family dominance violation on the shared 5m/15m tape,
independent of any trading rule.

For each shared close we walk the event grid (every book update on either side,
restricted to the legal decision window) and cut it into maximal contiguous runs
where the dominance floor is crossed:  ask_UpLow < bid_UpHigh.

Per run we record when it started, how long it lasted, how deep it got, how old
both books were, and - the number that decides everything - whether the crossing
was still there 1.5 s later.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/m2")
sys.path.insert(0, f"{ROOT}/scripts/fresh5m")
import dom                                    # noqa: E402
from replay import load_chainlink             # noqa: E402
from run_m2 import common_days                # noqa: E402

OUT = f"{ROOT}/data/multicoin/m2"
LAT_US = 1_500_000
MAX_AGE = 5.0


def main():
    days = common_days()
    w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    w5 = w[w.family == "5m"].set_index("wts")["result_id"].to_dict()
    w15 = w[w.family == "15m"].set_index("wts")["result_id"].to_dict()
    rows = []
    tick_rows = []
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_chainlink([prev, d])
        T5, T15 = dom.load_day(d, "5m"), dom.load_day(d, "15m")
        if cl is None or not T5 or not T15:
            continue
        for wts15 in sorted(T15):
            wts5 = wts15 + 600
            if wts5 not in T5 or wts15 not in w15 or wts5 not in w5:
                continue
            st = cl.strike(np.array([wts15, wts5, wts15 + 900]), mode="backfill")
            O15, O5, C = float(st[0]), float(st[1]), float(st[2])
            if not (math.isfinite(O15) and math.isfinite(O5) and math.isfinite(C)):
                continue
            i = int(np.searchsorted(cl.obs_sorted, wts5 * 1_000_000, side="left"))
            pub5 = int(cl.pub_by_obs[i]) if i < len(cl.obs_sorted) else 0
            close_us = (wts15 + 900) * 1_000_000
            t0 = max(int(wts5 * 1e6), pub5)
            t1 = close_us - LAT_US - 500_000
            t5, t15 = T5[wts5], T15[wts15]
            low_is_5 = O5 < O15
            A = t5 if low_is_5 else t15
            B = t15 if low_is_5 else t5
            grid = dom._grid(t0, t1, (t15, t5), 0.0)
            if len(grid) == 0:
                continue
            ia, ib = A.idx(grid), B.idx(grid)
            aa, ab = A.age(grid, ia), B.age(grid, ib)
            ok = (ia >= 0) & (ib >= 0)
            ja, jb = np.clip(ia, 0, None), np.clip(ib, 0, None)
            ask_lo = np.where(ok, A.ask[ja], np.nan)
            bid_hi = np.where(ok, B.bid[jb], np.nan)
            gap = bid_hi - ask_lo
            fresh = (aa <= MAX_AGE) & (ab <= MAX_AGE)
            live = ok & dom._valid(ask_lo) & dom._valid(bid_hi) & (gap > 0.0)
            r5, r15 = w5[wts5], w15[wts15]
            payoff = (float((r5 == 0) if low_is_5 else (r15 == 0))
                      + float((r15 == 1) if low_is_5 else (r5 == 1)))
            # time-weighted census of ticks (for "% of the window crossed")
            dt = np.diff(np.append(grid, t1)) / 1e6
            tick_rows.append(dict(day=d, wts15=wts15, n_ticks=len(grid),
                                  secs=float(dt.sum()),
                                  secs_live=float(dt[live].sum()),
                                  secs_live_fresh=float(dt[live & fresh].sum()),
                                  secs_live_1c=float(dt[live & (gap > 0.01)].sum()),
                                  strike_gap=abs(O5 - O15), payoff=payoff))
            if not live.any():
                continue
            # maximal runs of `live`
            idx = np.flatnonzero(live)
            brk = np.flatnonzero(np.diff(idx) != 1)
            starts = np.concatenate([[idx[0]], idx[brk + 1]])
            ends = np.concatenate([idx[brk], [idx[-1]]])
            for s, e in zip(starts, ends):
                ts = int(grid[s])
                # end of the crossing: the first non-live grid point after e
                te = int(grid[e + 1]) if e + 1 < len(grid) else t1
                tf = ts + LAT_US
                fa, fb = A.idx(np.array([tf])), B.idx(np.array([tf]))
                alive_after = False
                gap_after = np.nan
                if fa[0] >= 0 and fb[0] >= 0:
                    gap_after = float(B.bid[fb[0]] - A.ask[fa[0]])
                    alive_after = bool(gap_after > 0.0)
                rows.append(dict(
                    day=d, wts15=wts15, tau=(close_us - ts) / 1e6,
                    life_s=(te - ts) / 1e6, n_ticks=int(e - s + 1),
                    gap0=float(gap[s]), gap_max=float(np.nanmax(gap[s:e + 1])),
                    age_a=float(aa[s]), age_b=float(ab[s]),
                    fresh=bool(fresh[s]), ask_lo=float(ask_lo[s]),
                    bid_hi=float(bid_hi[s]), low_is_5=bool(low_is_5),
                    strike_gap=abs(O5 - O15), payoff=payoff,
                    gap_after=gap_after, alive_after_1500ms=alive_after))
        print(f"  {d}: cum runs={len(rows)}", flush=True)
    df = pd.DataFrame(rows)
    df.to_parquet(f"{OUT}/census_runs.parquet", index=False)
    tk = pd.DataFrame(tick_rows)
    tk.to_parquet(f"{OUT}/census_ticks.parquet", index=False)
    print("runs:", len(df), " windows:", len(tk), " days:", tk.day.nunique())


if __name__ == "__main__":
    main()
