#!/usr/bin/env python3
"""Intra-window event scanner: overreaction (H26) and thin-book pushes (H27).

Per family/day: build 1s series of market mid (from bookcurves; quotes for 1h)
joined with Binance 1s price. Detect events, record forward mid changes and
final outcome. Also accumulates the H12 mispricing surface on the same pass.

Output: data/features/events_{family}.parquet and surface_{family}.parquet
"""
import argparse
import concurrent.futures as cf
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data/data/processed"

FWD = [15, 30, 60]


def day_series(fam, day, wall):
    """1s grid per window: mid, bid, ask, binance dist, time-in-window."""
    wins = wall[(wall.family == fam) & (wall.date == day)]
    if wins.empty:
        return None
    if fam == "1h":
        f = P / f"daily/1h/quotes/{day}.parquet"
        if not f.exists():
            return None
        q = pd.read_parquet(f).dropna(subset=["wts"]).astype({"wts": "int64"})
        q["sec"] = q.timestamp_us // 1_000_000
        g = q.groupby(["wts", "sec"]).agg(bid=("bid_price", "last"), ask=("ask_price", "last"),
                                          bs=("bid_size", "last"), askz=("ask_size", "last")).reset_index()
        g["depth5c"] = np.nan
    else:
        f = P / f"daily/{fam}/bookcurves/{day}.parquet"
        if not f.exists():
            return None
        b = pd.read_parquet(f, columns=["timestamp_us", "bid_p0", "ask_p0", "bid_s0",
                                        "ask_s0", "bid_depth_5c", "ask_depth_5c", "wts"])
        b = b.dropna(subset=["wts"]).astype({"wts": "int64"})
        b["sec"] = b.timestamp_us // 1_000_000
        g = b.groupby(["wts", "sec"]).agg(bid=("bid_p0", "last"), ask=("ask_p0", "last"),
                                          bs=("bid_s0", "last"), askz=("ask_s0", "last"),
                                          d5b=("bid_depth_5c", "last"), d5a=("ask_depth_5c", "last")).reset_index()
        g["depth5c"] = g[["d5b", "d5a"]].min(axis=1)

    # binance 1s closes
    bf = P / f"binance/klines_1s/{day}.parquet"
    prev = (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frames = []
    for dd in [prev, day]:
        ff = P / f"binance/klines_1s/{dd}.parquet"
        if ff.exists():
            k = pd.read_parquet(ff, columns=["open_time", "close"])
            k["sec"] = k.open_time // 1_000_000
            frames.append(k[["sec", "close"]])
    if not frames:
        return None
    k = pd.concat(frames).drop_duplicates("sec").set_index("sec")["close"].sort_index()
    k = k.reindex(range(int(k.index.min()), int(k.index.max()) + 1)).ffill()

    out = []
    meta = wins.set_index("wts")[["duration", "result_id"]]
    for wts, grp in g.groupby("wts"):
        if wts not in meta.index:
            continue
        dur, rid = int(meta.loc[wts, "duration"]), int(meta.loc[wts, "result_id"])
        grid = pd.DataFrame({"sec": np.arange(wts, wts + dur)})
        grp = grp.sort_values("sec")
        grid = pd.merge_asof(grid, grp.drop(columns=["wts"]), on="sec", direction="backward")
        grid["mid"] = (grid.bid + grid.ask) / 2
        grid["wts"] = wts
        grid["t"] = grid.sec - wts
        grid["dur"] = dur
        grid["up"] = 1 - rid
        grid["S"] = k.reindex(grid.sec).values
        grid["S_open"] = k.reindex([wts]).values[0]
        out.append(grid)
    if not out:
        return None
    return pd.concat(out, ignore_index=True)


def scan_day(args):
    fam, day = args
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    gr = day_series(fam, day, wall)
    if gr is None:
        return None, None
    gr["dS20"] = gr.groupby("wts")["S"].diff(20)
    gr["dmid20"] = gr.groupby("wts")["mid"].diff(20)
    gr["rv20"] = gr.groupby("wts")["S"].transform(lambda s: s.diff().rolling(120, min_periods=30).std()) * np.sqrt(20)
    for h in FWD:
        gr[f"fmid{h}"] = gr.groupby("wts")["mid"].shift(-h)
    # events (not in first 25s, not in last 90s of life)
    ok = (gr.t >= 25) & (gr.t <= gr.dur - 90) & gr.mid.notna() & gr.dmid20.notna()
    spike = ok & (gr.dS20.abs() >= 2.5 * gr.rv20) & (gr.dmid20.abs() >= 0.03) & \
        (np.sign(gr.dS20) == np.sign(gr.dmid20))
    push = ok & (gr.dS20.abs() <= 0.5 * gr.rv20) & (gr.dmid20.abs() >= 0.03)
    ev = gr[spike | push].copy()
    ev["kind"] = np.where(spike[spike | push], "spike", "push")
    # thin-ish sampling: one event per window per 30s bucket
    ev["bkt"] = ev.t // 30
    ev = ev.drop_duplicates(["wts", "kind", "bkt"])
    ev["date"] = day
    evcols = ["date", "wts", "t", "dur", "kind", "mid", "bid", "ask", "bs", "askz", "depth5c",
              "dS20", "dmid20", "rv20", "up"] + [f"fmid{h}" for h in FWD]
    # H12 surface accumulation: bias by (life fraction, mid bucket)
    gr["lf"] = (gr.t / gr.dur * 10).astype(int).clip(0, 9)
    gr["mb"] = (gr.mid * 20).round().clip(0, 20)
    surf = gr.dropna(subset=["mid"]).groupby(["lf", "mb"]).agg(
        n=("up", "count"), up=("up", "sum"), mid=("mid", "sum"),
        bid=("bid", "sum"), ask=("ask", "sum")).reset_index()
    surf["date"] = day
    return ev[evcols], surf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    fam = args.family
    sub = "quotes" if fam == "1h" else "bookcurves"
    days = sorted(Path(f).stem for f in glob.glob(str(P / f"daily/{fam}/{sub}/*.parquet")))
    evs, surfs = [], []
    with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, (ev, surf) in enumerate(ex.map(scan_day, [(fam, d) for d in days])):
            if ev is not None:
                evs.append(ev)
                surfs.append(surf)
            if (i + 1) % 25 == 0:
                print(f"{i+1}/{len(days)}", flush=True)
    pd.concat(evs, ignore_index=True).to_parquet(ROOT / f"data/features/events_{fam}.parquet", index=False)
    pd.concat(surfs, ignore_index=True).to_parquet(ROOT / f"data/features/surface_{fam}.parquet", index=False)
    print("done", fam)


if __name__ == "__main__":
    main()
