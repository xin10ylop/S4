#!/usr/bin/env python3
"""M4 guard 2 measurement: how many oracle samples does sigma_1s need before
close_snipe may be trusted?

Mechanism being guarded: `fair = Phi( ln(S_t/S_open) / (sigma_1s*sqrt(tau)) )`.
sigma_1s is the std of 1s log returns over a rolling 120s buffer. Straight
after a restart that buffer holds n < 120 samples. BinanceOracle.
rolling_log_return_std only requires len(series) >= 3 and >= 2 returns, so the
bot is willing to trade 3 seconds after boot on a sigma built from TWO
returns. A sigma that is too SMALL inflates |z| and pushes `fair` toward the
cap, manufacturing edge that is not there — the mechanism behind this
project's first -$25 loss.

This script measures, on real 1h closes:
  (a) the distribution of sigma_n / sigma_120 as a function of n
  (b) |fair(sigma_n) - fair(sigma_120)| at the taus we actually trade
  (c) the DECISION error: how often a cold sigma manufactures a signal that
      the warm sigma would not have produced, and what those trades earn.

(c) is the number that sets the threshold: warmup must be long enough that
cold-only trades stop being a meaningful share of the tape.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / "data/data/processed"

FEE = 0.07
EDGE_MIN = 0.03
PRICE_MIN, PRICE_MAX = 0.30, 0.99
SIGMA_FLOOR = 8e-6
FAIR_CAP = 0.98
TAUS = (5, 4, 3)
FILL_LAG = 2
NS = (3, 5, 10, 20, 30, 45, 60, 90, 120)


def fee(p):
    return FEE * p * (1.0 - p)


def clip_fair(x):
    return min(max(x, 1.0 - FAIR_CAP), FAIR_CAP)


def sigma_from(returns):
    """Mirror of BinanceOracle.rolling_log_return_std on a return array."""
    r = returns[np.isfinite(returns)]
    if len(r) < 2:
        return float("nan")
    return float(np.std(r, ddof=1))


def main(days_limit=None):
    wall = pd.read_parquet(ROOT / "data/windows_all.parquet")
    w1 = wall[wall.family == "1h"]
    days = sorted({f.stem for f in (P / "daily/1h/quotes").glob("*.parquet")})
    if days_limit:
        days = days[:days_limit]

    rows = []
    kcache = {}

    def kl(day):
        if day in kcache:
            return kcache[day]
        f = P / f"binance/klines_1s/{day}.parquet"
        out = None
        if f.exists():
            k = pd.read_parquet(f, columns=["open_time", "open", "close"])
            k["sec"] = k.open_time // 1_000_000
            out = k[["sec", "open", "close"]]
        if len(kcache) > 4:
            kcache.clear()
        kcache[day] = out
        return out

    for di, day in enumerate(days):
        wins = w1[w1.date == day]
        if wins.empty:
            continue
        qf = P / f"daily/1h/quotes/{day}.parquet"
        if not qf.exists():
            continue
        q = pd.read_parquet(qf, columns=["timestamp_us", "bid_price", "bid_size",
                                          "ask_price", "ask_size", "wts"])
        q = q.dropna(subset=["wts"]).astype({"wts": "int64"})
        q["sec"] = q.timestamp_us // 1_000_000
        g = (q.groupby(["wts", "sec"])
               .agg(bid=("bid_price", "last"), ask=("ask_price", "last"),
                    bs=("bid_size", "last"), az=("ask_size", "last")).reset_index())
        frames = [x for x in (kl((pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")),
                              kl(day),
                              kl((pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")))
                  if x is not None]
        if not frames:
            continue
        k = pd.concat(frames).drop_duplicates("sec").set_index("sec").sort_index()
        full = range(int(k.index.min()), int(k.index.max()) + 1)
        kc = k["close"].reindex(full).ffill()
        ko = k["open"].reindex(full).fillna(kc.shift(1))

        for _, wrow in wins.sort_values("wts").iterrows():
            wts = int(wrow.wts)
            rid = int(wrow.result_id)
            close_s = wts + 3600
            gw = g[g.wts == wts]
            if gw.empty:
                continue
            grid = pd.DataFrame({"sec": np.arange(close_s - 20, close_s)})
            grid = pd.merge_asof(grid, gw.drop(columns=["wts"]).sort_values("sec"),
                                 on="sec", direction="backward").set_index("sec")
            S_open = ko.reindex([wts]).values[0]
            if not np.isfinite(S_open):
                continue
            for tau in TAUS:
                t = close_s - tau
                if t not in grid.index:
                    continue
                S_t = kc.reindex([t]).values[0]
                if not np.isfinite(S_t):
                    continue
                seg = kc.reindex(range(t - 130, t + 1)).to_numpy()
                r = np.diff(np.log(seg))
                if len(r) < 121:
                    continue
                lr = np.log(S_t / S_open)
                row = grid.loc[t]
                rowf = grid.loc[t + FILL_LAG] if (t + FILL_LAG) in grid.index else None
                rec = dict(day=day, wts=wts, tau=tau, result_id=rid,
                           ask=row.ask, bid=row.bid, az=row.az, bs=row.bs,
                           fill_ask=(rowf.ask if rowf is not None else np.nan),
                           fill_bid=(rowf.bid if rowf is not None else np.nan),
                           lr=lr)
                for n in NS:
                    # n samples in the buffer -> n-1 log returns, the most
                    # recent ones (buffer refilled since the restart)
                    s = sigma_from(r[-(n - 1):])
                    s = max(s, SIGMA_FLOOR) if np.isfinite(s) else np.nan
                    rec[f"sigma_{n}"] = s
                    if np.isfinite(s):
                        rec[f"fair_{n}"] = clip_fair(float(norm.cdf(lr / (s * np.sqrt(tau)))))
                    else:
                        rec[f"fair_{n}"] = np.nan
                rows.append(rec)
        if di % 40 == 0:
            print(f"  ... {day}  rows={len(rows)}", flush=True)

    d = pd.DataFrame(rows)
    out = ROOT / "data/c2/m4_warmup.parquet"
    d.to_parquet(out)
    print(f"\nrows={len(d)} windows={d.wts.nunique()} days={d.day.nunique()} -> {out}")
    report(d)
    return d


def _signal(fair_up, row_ask, row_bid, fill_ask, fill_bid):
    """Return (side, sig_px, fill_px) for the first side clearing edge_min at
    signal time AND still clearing at fill time, else None."""
    for side in ("up", "down"):
        px = row_ask if side == "up" else (1 - row_bid if np.isfinite(row_bid) else np.nan)
        fv = fair_up if side == "up" else 1 - fair_up
        if not (np.isfinite(px) and PRICE_MIN < px < PRICE_MAX):
            continue
        if fv - px - fee(px) <= EDGE_MIN:
            continue
        fpx = fill_ask if side == "up" else (1 - fill_bid if np.isfinite(fill_bid) else np.nan)
        if not (np.isfinite(fpx) and PRICE_MIN < fpx < PRICE_MAX):
            return (side, px, np.nan)
        if fv - fpx - fee(fpx) <= EDGE_MIN:
            return (side, px, np.nan)
        return (side, px, fpx)
    return None


def report(d):
    print("\n=== (a) sigma_n / sigma_120 ===")
    print(f"{'n':>4} {'p05':>8} {'p25':>8} {'median':>8} {'p75':>8} {'p95':>8} "
          f"{'P(<0.7x)':>9} {'P(<0.5x)':>9}")
    for n in NS:
        if n == 120:
            continue
        r = (d[f"sigma_{n}"] / d["sigma_120"]).dropna()
        print(f"{n:>4} {r.quantile(.05):8.3f} {r.quantile(.25):8.3f} {r.median():8.3f} "
              f"{r.quantile(.75):8.3f} {r.quantile(.95):8.3f} "
              f"{(r < 0.7).mean():9.3f} {(r < 0.5).mean():9.3f}")

    print("\n=== (b) |fair(sigma_n) - fair(sigma_120)| at traded taus ===")
    print(f"{'n':>4} {'median':>9} {'p90':>9} {'p99':>9} {'P(>0.03)':>9} {'P(>0.10)':>9}")
    for n in NS:
        if n == 120:
            continue
        e = (d[f"fair_{n}"] - d["fair_120"]).abs().dropna()
        print(f"{n:>4} {e.median():9.4f} {e.quantile(.90):9.4f} {e.quantile(.99):9.4f} "
              f"{(e > 0.03).mean():9.4f} {(e > 0.10).mean():9.4f}")

    print("\n=== (c) DECISION error: signals that exist ONLY because sigma was cold ===")
    print(f"{'n':>4} {'warm_sig':>9} {'cold_sig':>9} {'cold_only':>10} {'cold_only_wr':>13} "
          f"{'cold_only_c/sh':>15} {'warm_wr':>9}")
    # one entry per window: first tau that fires
    for n in NS:
        warm_tr, cold_tr, cold_only = [], [], []
        for wts, grp in d.groupby("wts"):
            grp = grp.sort_values("tau", ascending=False)
            rid = int(grp.result_id.iloc[0])

            def first(col):
                for _, rr in grp.iterrows():
                    if not np.isfinite(rr[col]):
                        continue
                    s = _signal(rr[col], rr.ask, rr.bid, rr.fill_ask, rr.fill_bid)
                    if s:
                        return s, rid
                return None, rid

            w, _ = first("fair_120")
            c, _ = first(f"fair_{n}")
            if w and np.isfinite(w[2]):
                warm_tr.append((w[0], w[2], rid))
            if c and np.isfinite(c[2]):
                cold_tr.append((c[0], c[2], rid))
                if not (w and np.isfinite(w[2]) and w[0] == c[0]):
                    cold_only.append((c[0], c[2], rid))

        def stats(trs):
            if not trs:
                return 0, float("nan"), float("nan")
            wins = [1.0 if ((r == 0) == (s == "up")) else 0.0 for s, p, r in trs]
            pnl = [w - p - fee(p) for w, (s, p, r) in zip(wins, trs)]
            return len(trs), float(np.mean(wins)), float(np.mean(pnl))

        nw, wrw, _ = stats(warm_tr)
        nc, _, _ = stats(cold_tr)
        no, wro, pno = stats(cold_only)
        print(f"{n:>4} {nw:>9} {nc:>9} {no:>10} "
              f"{wro if np.isfinite(wro) else float('nan'):13.3f} "
              f"{100*pno if np.isfinite(pno) else float('nan'):15.2f} {wrw:9.3f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    if a.report_only:
        report(pd.read_parquet(ROOT / "data/c2/m4_warmup.parquet"))
    else:
        main(a.days)
