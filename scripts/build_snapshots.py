#!/usr/bin/env python3
"""Per-window snapshot feature builder.

For each market window, extracts book state (bid/ask/sizes) at fixed offsets
relative to open and close, trade-flow aggregates per life segment, and
post-close (settlement window) stats.

Output: data/features/snap_{family}.parquet, one row per window.
Usage: python scripts/build_snapshots.py 5m [--workers 4]
"""
import argparse
import concurrent.futures as cf
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data/data/processed"

# offsets in seconds relative to OPEN (negative = before open)
OPEN_OFFS = [-300, -120, -60, -30, -20, -10, -5, -2, 0, 2, 5, 10, 20, 30, 60]
# offsets relative to CLOSE (negative = before close, positive = settlement window)
CLOSE_OFFS = [-120, -60, -30, -15, -10, -5, -2, -1, 0, 1, 2, 5, 10, 15, 20, 30]


def snap_quotes(q: pd.DataFrame, wins: pd.DataFrame, offs, anchor: str, prefix: str):
    """Snapshot book state at wts+off (anchor='open') or wts+duration+off (anchor='close')."""
    out = {}
    q = q.sort_values("timestamp_us")
    for off in offs:
        t = wins["wts"] * 1_000_000 + off * 1_000_000
        if anchor == "close":
            t = t + wins["duration"] * 1_000_000
        probe = pd.DataFrame({
            "wts": wins["wts"].values,
            "timestamp_us": t.values,
        }).sort_values("timestamp_us")
        m = pd.merge_asof(
            probe, q[["timestamp_us", "wts", "bid_price", "bid_size", "ask_price", "ask_size"]],
            on="timestamp_us", by="wts", direction="backward",
        ).set_index("wts")
        tag = f"{prefix}{off:+d}".replace("+", "p").replace("-", "m")
        for col, short in [("bid_price", "b"), ("ask_price", "a"),
                           ("bid_size", "bs"), ("ask_size", "as")]:
            out[f"{short}_{tag}"] = m[col]
    return pd.DataFrame(out)


def trade_aggs(t: pd.DataFrame, wins: pd.DataFrame):
    """Signed flow / volume / vwap per life segment."""
    t = t.copy()
    dur = wins.set_index("wts")["duration"]
    t["dur"] = t["wts"].map(dur)
    t = t.dropna(subset=["dur"])
    t["rel"] = t["timestamp_us"] / 1e6 - t["wts"]
    t["relc"] = t["rel"] - t["dur"]  # relative to close
    t["signed"] = np.where(t["side"] == "buy", t["size"], -t["size"])
    t["notional"] = t["price"] * t["size"]

    segs = {
        "pre300": (t.rel >= -300) & (t.rel < 0),
        "pre30": (t.rel >= -30) & (t.rel < 0),
        "pre10": (t.rel >= -10) & (t.rel < 0),
        "o30": (t.rel >= 0) & (t.rel < 30),
        "mid": (t.rel >= 30) & (t.relc < -60),
        "l60": (t.relc >= -60) & (t.relc < -10),
        "l10": (t.relc >= -10) & (t.relc < 0),
        "post": (t.relc >= 0) & (t.relc < 60),
    }
    frames = {}
    for name, mask in segs.items():
        g = t[mask].groupby("wts")
        agg = g.agg(vol=("size", "sum"), n=("size", "count"),
                    flow=("signed", "sum"), notion=("notional", "sum"),
                    pmin=("price", "min"), pmax=("price", "max"),
                    last=("price", "last"))
        agg["vwap"] = agg["notion"] / agg["vol"]
        frames[name] = agg[["vol", "n", "flow", "vwap", "pmin", "pmax", "last"]]
    out = pd.concat(frames, axis=1)
    out.columns = [f"t_{seg}_{col}" for seg, col in out.columns]
    return out


def process_day(args):
    fam, day = args
    qf = P / f"daily/{fam}/quotes/{day}.parquet"
    tf = P / f"daily/{fam}/trades/{day}.parquet"
    if not qf.exists():
        return None
    q = pd.read_parquet(qf)
    t = pd.read_parquet(tf) if tf.exists() else pd.DataFrame(
        columns=["timestamp_us", "price", "size", "side", "wts"])
    q = q.dropna(subset=["wts"]).astype({"wts": "int64"})
    t = t.dropna(subset=["wts"]).astype({"wts": "int64"})
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    wins = wall[(wall.family == fam) & (wall.date == day)][["wts", "duration", "result_id"]]
    if wins.empty:
        # windows whose date isn't in table (e.g. spillover) — derive from quotes
        return None
    a = snap_quotes(q, wins, OPEN_OFFS, "open", "o")
    b = snap_quotes(q, wins, CLOSE_OFFS, "close", "c")
    c = trade_aggs(t, wins)
    out = wins.set_index("wts").join([a, b, c])
    out["date"] = day
    return out.reset_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    fam = args.family

    days = sorted(Path(f.replace("\\", "/")).stem
                  for f in glob.glob(str(P / f"daily/{fam}/quotes/*.parquet")))
    print(f"{fam}: {len(days)} days")
    outs = []
    with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, res in enumerate(ex.map(process_day, [(fam, d) for d in days])):
            if res is not None:
                outs.append(res)
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(days)}", flush=True)
    full = pd.concat(outs, ignore_index=True)
    dest = ROOT / f"data/features/snap_{fam}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    full.to_parquet(dest, index=False)
    print(f"wrote {dest}: {full.shape}")


if __name__ == "__main__":
    main()
