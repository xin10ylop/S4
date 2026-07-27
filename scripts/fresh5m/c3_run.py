#!/usr/bin/env python3
"""C3 — the decisive 5m run on the fresh 56-day tape (data/fresh5m/).

Driver only: every decision, fill and statistic comes from scripts/fresh5m/replay.py
(the harness verified in audit/C2_harness.md).  The one thing re-implemented here is
the book loader, which is byte-identical to replay.load_fresh_day except that it reads
only the columns the replay uses (the fresh book files carry 25 bid levels we never
touch, and reading them costs ~2x RAM and ~2x wall clock).  `--verify-loader` asserts
the two loaders produce identical tapes.

Usage:
  python3 scripts/fresh5m/c3_run.py --days-from 2026-06-01 --days-to 2026-07-26 \
      --out-dir data/c3
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay as R  # noqa: E402

ROOT = "/home/user/S4"
BOOKS = f"{ROOT}/data/fresh5m/books"
CRYPTO = f"{ROOT}/data/fresh5m/crypto_prices"
MARKETS = f"{ROOT}/data/fresh5m/markets.parquet"
INF = float("inf")


# --------------------------------------------------------------------------
# book loader (column-limited clone of replay.load_fresh_day)
# --------------------------------------------------------------------------
def _nlev(path: str) -> int:
    import pyarrow.parquet as pq
    names = set(pq.ParquetFile(path).schema.names)
    n = 0
    while f"ask_price_{n}" in names:
        n += 1
    return n


def load_fresh_day_fast(day: str, books_dir: str, res: dict, control_cs: set | None = None):
    f = f"{books_dir}/{day}.parquet"
    if not os.path.exists(f):
        return []
    nlev = _nlev(f)
    cols = ["timestamp_us", "close_s", "oid"]
    for L in range(nlev):
        cols += [f"ask_price_{L}", f"ask_size_{L}"]
    df = pd.read_parquet(f, columns=cols).sort_values("timestamp_us")
    out = []
    for cs, g in df.groupby("close_s", sort=True):
        tapes = {}
        for oid in (0, 1):
            h = g[g.oid == oid]
            if h.empty:
                tapes[oid] = R.BookTape(np.zeros(0, np.int64), np.zeros((0, 1)), np.zeros((0, 1)))
                continue
            h = h.drop_duplicates("timestamp_us", keep="last")
            ts = h.timestamp_us.to_numpy(np.int64)
            pr = np.column_stack([h[f"ask_price_{L}"].to_numpy(float) for L in range(nlev)])
            sz = np.column_stack([h[f"ask_size_{L}"].to_numpy(float) for L in range(nlev)])
            keep = np.isfinite(pr).any(axis=1)
            tapes[oid] = R.BookTape(ts[keep], pr[keep], sz[keep])
        w = R.Window(day=day, wts=int(cs) - 300, close_s=int(cs),
                     result_id=res.get(int(cs)), up=tapes[0], dn=tapes[1])
        w.is_control = bool(control_cs and int(cs) in control_cs)  # type: ignore[attr-defined]
        out.append(w)
    return out


def verify_loader(day: str, res: dict) -> None:
    """Assert the fast loader == replay.load_fresh_day on a real day."""
    tmp = f"{BOOKS}/_markets.parquet"
    made = False
    if not os.path.exists(tmp):
        mk = pd.read_parquet(MARKETS, columns=["close_s", "result_id"])
        mk.to_parquet(tmp, index=False)
        made = True
    try:
        a = R.load_fresh_day(day, BOOKS)
    finally:
        if made:
            os.remove(tmp)
    b = load_fresh_day_fast(day, BOOKS, res)
    assert len(a) == len(b), f"window count {len(a)} vs {len(b)}"
    for wa, wb in zip(a, b):
        assert wa.close_s == wb.close_s and wa.wts == wb.wts
        assert wa.result_id == wb.result_id, (wa.close_s, wa.result_id, wb.result_id)
        for ta, tb in ((wa.up, wb.up), (wa.dn, wb.dn)):
            assert np.array_equal(ta.ts, tb.ts)
            assert np.array_equal(ta.prices, tb.prices, equal_nan=True)
            assert np.array_equal(ta.sizes, tb.sizes, equal_nan=True)
    print(f"loader verify {day}: OK ({len(a)} windows, tapes identical)", flush=True)


# --------------------------------------------------------------------------
# parameter sets
# --------------------------------------------------------------------------
SHIP = R.Params(edge_min=0.03, sigma_1s_floor=8.0e-6, fee_rate=0.07,
                latency_ms=1500, depth_fraction=1.0, per_event_cap_usd=250.0,
                max_book_age_s=INF, causal=True, sigma_mode="grid_ffill",
                strike_mode="backfill", winner_source="result_id", ladder=True)

PRIMARY_AGE = 5.0


def param_sets() -> dict:
    ps = {}
    for cap in (250.0, 25.0):
        tag = f"c{int(cap)}"
        ps[f"raw_{tag}"] = replace(SHIP, per_event_cap_usd=cap)
        for a in (2.0, 5.0, 10.0, 20.0, 30.0, 60.0):
            if cap == 25.0 and a != PRIMARY_AGE:
                continue
            ps[f"pre{a:g}_{tag}"] = replace(SHIP, per_event_cap_usd=cap, max_book_age_s=a)
        ps[f"fee10_{tag}"] = replace(SHIP, per_event_cap_usd=cap, max_book_age_s=PRIMARY_AGE,
                                     fee_rate=0.10)
        ps[f"lat3000_{tag}"] = replace(SHIP, per_event_cap_usd=cap, max_book_age_s=PRIMARY_AGE,
                                       latency_ms=3000)
        ps[f"depth50_{tag}"] = replace(SHIP, per_event_cap_usd=cap, max_book_age_s=PRIMARY_AGE,
                                       depth_fraction=0.5)
        ps[f"stress_all_{tag}"] = replace(SHIP, per_event_cap_usd=cap, max_book_age_s=PRIMARY_AGE,
                                          fee_rate=0.10, latency_ms=3000, depth_fraction=0.5)
    # diagnostics (cap 250 only)
    ps["noncausal_c250"] = replace(SHIP, causal=False, max_book_age_s=PRIMARY_AGE)
    ps["sigfloor3e5_c250"] = replace(SHIP, sigma_1s_floor=3.0e-5, max_book_age_s=PRIMARY_AGE)
    ps["sigmabot_c250"] = replace(SHIP, sigma_mode="bot", max_book_age_s=PRIMARY_AGE)
    ps["clwinner_c250"] = replace(SHIP, winner_source="chainlink", max_book_age_s=PRIMARY_AGE)
    return ps


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-from", default="2026-06-01")
    ap.add_argument("--days-to", default="2026-07-26")
    ap.add_argument("--out-dir", default=f"{ROOT}/data/c3")
    ap.add_argument("--verify-loader", action="store_true")
    ap.add_argument("--only", default=None, help="comma list of param-set names")
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    days = [d.strftime("%Y-%m-%d")
            for d in pd.date_range(a.days_from, a.days_to, freq="D")]
    days = [d for d in days if os.path.exists(f"{BOOKS}/{d}.parquet")]
    print(f"{len(days)} days with books: {days[0]} .. {days[-1]}", flush=True)

    mk = pd.read_parquet(MARKETS, columns=["close_s", "result_id", "slug", "d"])
    mk["result_id"] = pd.to_numeric(mk.result_id, errors="coerce")
    res = {int(c): (int(v) if pd.notna(v) else None)
           for c, v in zip(mk.close_s, mk.result_id)}
    cand = pd.read_parquet(f"{ROOT}/data/fresh5m/candidates.parquet",
                           columns=["close_s", "is_control"])
    ctrl_only = set(cand.groupby("close_s").is_control.all().pipe(lambda s: s[s].index).astype(int))

    if a.verify_loader:
        verify_loader(days[len(days) // 2], res)
        verify_loader(days[-1], res)

    ps = param_sets()
    if a.only:
        keep = set(a.only.split(","))
        ps = {k: v for k, v in ps.items() if k in keep}
    print(f"{len(ps)} parameter sets: {list(ps)}", flush=True)

    recs = {k: [] for k in ps}
    meta = dict(days=0, windows=0, causality=[], per_day=[])
    t0 = time.time()
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        feeds = {}
        for p in ps.values():
            key = (p.sigma_mode, p.vol_window_secs)
            if key not in feeds:
                feeds[key] = R.load_chainlink([prev, d], vol_window_secs=p.vol_window_secs,
                                              sigma_mode=p.sigma_mode, dirs=(CRYPTO,))
        if all(v is None for v in feeds.values()):
            print(f"  {d}: NO CHAINLINK", flush=True)
            continue
        wins = load_fresh_day_fast(d, BOOKS, res, ctrl_only)
        if not wins:
            print(f"  {d}: no books", flush=True)
            continue
        ref = next(v for v in feeds.values() if v is not None)
        probe = np.array([int((w.close_s - t) * 1e6) for w in wins for t in SHIP.taus()],
                         dtype=np.int64)
        meta["causality"].append(ref.causality_violations(probe))
        nsig = {}
        for name, p in ps.items():
            cl = feeds[(p.sigma_mode, p.vol_window_secs)]
            if cl is None:
                continue
            n0 = len(recs[name])
            for w in wins:
                r = R.run_window(w, cl, p)
                if r is not None:
                    r["is_control"] = getattr(w, "is_control", False)
                    recs[name].append(r)
            nsig[name] = len(recs[name]) - n0
        meta["days"] += 1
        meta["windows"] += len(wins)
        meta["per_day"].append(dict(day=d, windows=len(wins),
                                    controls=sum(1 for w in wins if getattr(w, "is_control", False)),
                                    signals_raw=nsig.get("raw_c250", 0)))
        print(f"  {d}: {len(wins)} win, raw signals {nsig.get('raw_c250', 0)}"
              f"  [{time.time()-t0:6.1f}s]", flush=True)

    for name, rows in recs.items():
        pd.DataFrame(rows).to_parquet(f"{a.out_dir}/trades_{name}.parquet", index=False)
    pd.DataFrame(meta["per_day"]).to_parquet(f"{a.out_dir}/per_day.parquet", index=False)
    pd.DataFrame(meta["causality"]).to_parquet(f"{a.out_dir}/causality.parquet", index=False)
    print("days", meta["days"], "windows", meta["windows"])
    print("causality:", R.agg_causality(meta["causality"]))
    print(f"done in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
