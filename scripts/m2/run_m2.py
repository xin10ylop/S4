#!/usr/bin/env python3
"""M2 driver: run every dominance-arb configuration over the shared 5m/15m tape
in ONE pass over the days (parquet load dominates, so configs are almost free).

Writes one parquet per config under data/multicoin/m2/runs/.
"""
from __future__ import annotations

import math
import os
import sys
import time
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts/m2")
sys.path.insert(0, f"{ROOT}/scripts/fresh5m")
import dom                                    # noqa: E402
from replay import load_chainlink             # noqa: E402

OUT = f"{ROOT}/data/multicoin/m2"
RUNS = f"{OUT}/runs"
os.makedirs(RUNS, exist_ok=True)


def common_days() -> List[str]:
    import glob
    def d(p):
        return set(os.path.basename(f)[:-8] for f in glob.glob(p + "/*.parquet"))
    P = f"{ROOT}/data/data/processed/daily"
    return sorted(d(f"{P}/5m/bookcurves") & d(f"{P}/15m/bookcurves") & d(f"{P}/crypto_prices"))


def run_multi(days: Sequence[str], sets: Dict[str, dom.P], verbose=True):
    w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    w5 = w[w.family == "5m"].set_index("wts")["result_id"].to_dict()
    w15 = w[w.family == "15m"].set_index("wts")["result_id"].to_dict()
    f5 = w[w.family == "5m"].set_index("wts")["fee_rate"].to_dict()
    f15 = w[w.family == "15m"].set_index("wts")["fee_rate"].to_dict()
    clocks = sorted({p.clock for p in sets.values()})
    recs: Dict[str, List[dict]] = {k: [] for k in sets}
    meta = dict(days=0, pairs=0, no_strike=0, no_book=0, no_result=0)
    t0 = time.time()
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_chainlink([prev, d])
        T = {c: (dom.load_day(d, "5m", c), dom.load_day(d, "15m", c)) for c in clocks}
        if cl is None or not T[clocks[0]][0] or not T[clocks[0]][1]:
            if verbose:
                print(f"  {d}: missing inputs", flush=True)
            continue
        pairs = 0
        for wts15 in sorted(T[clocks[0]][1]):
            wts5 = wts15 + 600
            if wts5 not in T[clocks[0]][0]:
                meta["no_book"] += 1
                continue
            if wts15 not in w15 or wts5 not in w5:
                meta["no_result"] += 1
                continue
            st = cl.strike(np.array([wts15, wts5, wts15 + 900]), mode="backfill")
            O15, O5, C = float(st[0]), float(st[1]), float(st[2])
            if not (math.isfinite(O15) and math.isfinite(O5) and math.isfinite(C)):
                meta["no_strike"] += 1
                continue
            i = int(np.searchsorted(cl.obs_sorted, wts5 * 1_000_000, side="left"))
            pub5 = int(cl.pub_by_obs[i]) if i < len(cl.obs_sorted) else 0
            r5, r15 = w5[wts5], w15[wts15]
            if pd.isna(r5) or pd.isna(r15):
                meta["no_result"] += 1
                continue
            a, b = f5.get(wts5), f15.get(wts15)
            fr = float(max(a if pd.notna(a) else 0.0, b if pd.notna(b) else 0.0))
            pairs += 1
            for name, p in sets.items():
                t5m, t15m = T[p.clock]
                if wts5 not in t5m or wts15 not in t15m:
                    continue
                rec = dom.run_window(d, wts15, t15m[wts15], t5m[wts5], O15, O5,
                                     pub5, int(r15), int(r5), C, p,
                                     fee_rate=(fr if p.per_window_fee else None))
                if rec is not None:
                    recs[name].append(rec)
        meta["days"] += 1
        meta["pairs"] += pairs
        if verbose:
            print(f"  {d}: {pairs} shared closes  [{time.time()-t0:.0f}s]", flush=True)
    out = {}
    for k, v in recs.items():
        df = pd.DataFrame(v)
        df.to_parquet(f"{RUNS}/{k}.parquet", index=False)
        out[k] = df
    pd.Series(meta).to_json(f"{RUNS}/_meta.json")
    return out, meta


BASE = dict(latency_ms=1500, max_book_age_s=5.0, per_window_fee=True,
            clip_usd=25.0, slack_frac=0.0, fill_margin_s=0.5,
            max_walk_above_best=0.03)


def build_sets() -> Dict[str, dom.P]:
    S: Dict[str, dom.P] = {}
    # --- gap threshold sweep (the primary knob) ---------------------------
    for g in (0.0, 0.005, 0.01, 0.02, 0.035, 0.05, 0.08):
        S[f"gap{int(g*1000):03d}"] = dom.P(gap_min=g, **BASE)
    G = 0.005                                   # reference gap for the stress runs
    # --- latency ----------------------------------------------------------
    for L in (0, 250, 500, 1000, 3000):
        S[f"lat{L}"] = dom.P(gap_min=G, **{**BASE, "latency_ms": L})
    # --- order type -------------------------------------------------------
    for s in (0.25, 0.5, 1.0):
        S[f"slack{int(s*100)}"] = dom.P(gap_min=G, **{**BASE, "slack_frac": s})
    S["market"] = dom.P(gap_min=G, **{**BASE, "market": True})
    # --- contamination controls ------------------------------------------
    for pm in (100, 250, 500, 1000, 2000):
        S[f"persist{pm}"] = dom.P(gap_min=G, **{**BASE, "persist_ms": pm})
    for h in (0.005, 0.01):
        S[f"haircut{int(h*1000)}"] = dom.P(gap_min=G, **{**BASE, "dn_haircut": h})
    S["localclock"] = dom.P(gap_min=G, **{**BASE, "clock": "local"})
    for a in (1.0, 30.0, float("inf")):
        S[f"age{a}"] = dom.P(gap_min=G, **{**BASE, "max_book_age_s": a})
    # --- sizing / depth ---------------------------------------------------
    for c in (100.0, 250.0):
        S[f"clip{int(c)}"] = dom.P(gap_min=G, **{**BASE, "clip_usd": c})
    for f in (0.5, 0.25):
        S[f"depthfrac{int(f*100)}"] = dom.P(gap_min=G, **{**BASE, "depth_fraction": f})
    S["fee0"] = dom.P(gap_min=G, **{**BASE, "per_window_fee": False, "fee_rate": 0.0})
    # --- the combined 'best defensible' config ---------------------------
    S["strict"] = dom.P(gap_min=0.02, **{**BASE, "persist_ms": 500, "dn_haircut": 0.005,
                                         "clock": "local"})
    return S


if __name__ == "__main__":
    days = common_days()
    print(f"{len(days)} days: {days[0]} .. {days[-1]}", flush=True)
    sets = build_sets()
    print(f"{len(sets)} configs", flush=True)
    out, meta = run_multi(days, sets)
    print(meta)
    for k, v in out.items():
        n = int(v.signal.sum()) if len(v) and "signal" in v else 0
        print(f"  {k:16s} rows={len(v):5d} signals={n}")
