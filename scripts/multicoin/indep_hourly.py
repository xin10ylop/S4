#!/usr/bin/env python3
"""Independent cross-check of the M3 hourly replay DATA LAYER.

`scripts/fresh5m/proptest.py` already proves the ported bot primitives match
`bot.polybot.strategy` / `fill_engine`.  What it does NOT cover is the new
Binance/quote data layer written for M3 (`replay_hourly.BinanceFeed`,
`load_coin_windows`, `run_window`).  This file re-implements that layer from
the raw parquet in a deliberately different style -- plain loops over pandas
frames, its own anchor search, its own sigma grid, its own book lookup, its own
fee/fair arithmetic, importing NOTHING from replay_hourly or replay.py -- and
diffs every field of every decision.

Usage:  python3 scripts/multicoin/indep_hourly.py bitcoin,bnb 2026-07-01,2026-07-02,...
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

DATA = "/home/user/S4/data/multicoin"
SYM = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
       "xrp": "XRPUSDT", "dogecoin": "DOGEUSDT", "bnb": "BNBUSDT", "hype": "HYPEUSDT"}

EDGE_MIN, PMIN, PMAX = 0.03, 0.30, 0.99
FLOOR, CAP, FEE = 8e-6, 0.98, 0.07
VOL, LAT, CLIP, WALK = 120, 1.5, 250.0, 0.03
TAUS = [5.0, 4.0, 3.0]


def fee(p):
    return FEE * p * (1 - p)


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def run(coin, days):
    # ---------------- underlying, built my own way -------------------------
    kf = sorted(glob.glob(f"{DATA}/binance/{SYM[coin]}/*.parquet"))
    k = pd.concat([pd.read_parquet(f) for f in kf], ignore_index=True)
    k = k[k.volume > 0].sort_values("ts_ms").reset_index(drop=True)
    ksec = (k.ts_ms // 1000).to_numpy(np.int64)
    kopen = k.open.to_numpy(float)
    kclose = k.close.to_numpy(float)
    # 1s ffilled grid of last-trade price
    g0, g1 = int(ksec[0]), int(ksec[-1])
    grid = np.arange(g0, g1 + 1)
    px = pd.Series(kclose, index=ksec).groupby(level=0).last().reindex(grid).ffill().to_numpy()
    lp = np.log(px)
    dif = np.diff(lp, prepend=np.nan)

    def sigma_at_second(sec):
        """std(ddof=1) of the 120 one-second log returns ending at `sec`."""
        i = sec - g0
        if i < 0 or i >= len(dif):
            return float("nan")
        a = max(1, i - VOL + 1)
        w = dif[a:i + 1]
        w = w[np.isfinite(w)]
        if len(w) < 30:
            return float("nan")
        return float(np.std(w, ddof=1))

    # ---------------- markets ----------------------------------------------
    mk = pd.read_parquet(f"{DATA}/markets.parquet")
    mk = mk[(mk.coin == coin) & (mk.status == "resolved")]

    out = []
    # a close at 00:00Z lives in the PREVIOUS day's quote file (fetch.py keys
    # files by date(close_s - 1)), so load one extra file per requested day and
    # filter on date(close_s) afterwards.
    want = set(days)
    files = sorted({f"{DATA}/quotes/{coin}/{x}.parquet" for d in days
                    for x in (d, (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))})
    for qf in files:
        if not os.path.exists(qf):
            continue
        q = pd.read_parquet(qf)
        for close_s in sorted(q.close_s.unique()):
            row = mk[mk.close_s == close_s]
            if row.empty:
                continue
            rid = int(row.result_id.iloc[0])
            open_s = int(row.open_s.iloc[0])
            day = pd.Timestamp(int(close_s), unit="s", tz="UTC").strftime("%Y-%m-%d")
            if day not in want:
                continue
            # hour open = first traded print in [open_s, close_s)
            sel = np.where((ksec >= open_s) & (ksec < close_s))[0]
            if len(sel) == 0:
                continue
            S_open = float(kopen[sel[0]])
            books = {o: q[(q.close_s == close_s) & (q.oid == o)]
                     .sort_values(["timestamp_us", "local_timestamp_us"],
                                  kind="mergesort") for o in (0, 1)}

            def best(o, t_us):
                b = books[o]
                b = b[b.timestamp_us <= t_us]
                if b.empty:
                    return None, float("inf")
                r = b.iloc[-1]
                age = (t_us - int(r.timestamp_us)) / 1e6
                if not np.isfinite(r.ask_price):
                    return None, age
                sz = float(r.ask_size) if np.isfinite(r.ask_size) and r.ask_size > 0 else 0.0
                # snap float32 back onto the venue's decimal tick grid
                return (round(float(r.ask_price), 4), sz), age

            fired = False
            for tau in TAUS:
                if fired:
                    break
                t_us = int(round((close_s - tau) * 1e6))
                cut = t_us // 1_000_000
                j = np.searchsorted(ksec, cut - 1, "right") - 1
                if j < 0:
                    continue
                S_t = float(kclose[j])
                obs_s = int(ksec[j]) + 1
                sg = sigma_at_second(cut - 1)
                if not np.isfinite(sg) or sg <= 0:
                    continue
                tau_eff = close_s - obs_s
                if tau_eff <= 0:
                    continue
                s = max(sg, FLOOR)
                z = math.log(S_t / S_open) / (s * math.sqrt(tau_eff))
                fu = min(max(phi(z), 1 - CAP), CAP)
                for side, oid, fv in (("up", 0, fu), ("down", 1, 1 - fu)):
                    bb, age = best(oid, t_us)
                    if bb is None:
                        continue
                    a = bb[0]
                    if not (PMIN < a < PMAX):
                        continue
                    e = fv - a - fee(a)
                    if e <= EDGE_MIN:
                        continue
                    # SIGNAL
                    fired = True
                    fb, fage = best(oid, t_us + int(LAT * 1e6))
                    rec = dict(coin=coin, day=day, close_s=int(close_s), tau=tau,
                               side=side, fair=fv, sig_ask=a,
                               book_age_sig=age, book_age_fil=fage,
                               won=int((rid == 0) if side == "up" else (rid == 1)),
                               shares=0.0, avg_price=float("nan"),
                               pnl_per_share=float("nan"), outcome="filled")
                    if fb is None:
                        rec["outcome"] = "empty_book"
                    else:
                        fp, fs = fb
                        if not (PMIN < fp < PMAX) or (fv - fp - fee(fp)) <= EDGE_MIN:
                            rec["outcome"] = "book_moved_no_edge"
                        else:
                            sh = min(fs, CLIP / fp)
                            if sh <= 1e-9:
                                rec["outcome"] = "book_moved_no_edge"
                            else:
                                rec["shares"] = sh
                                rec["avg_price"] = fp
                                rec["pnl_per_share"] = rec["won"] - fp - fee(fp)
                    out.append(rec)
                    break
    return pd.DataFrame(out)


def main():
    coins = sys.argv[1].split(",")
    days = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    sys.path.insert(0, "/home/user/S4/scripts")
    from multicoin.replay_hourly import CoinTape, HParams  # noqa

    tot = 0
    for coin in coins:
        tape = CoinTape(coin, verbose=False)
        dd = days or tape.days
        h, _ = tape.run(HParams(), dd)
        i = run(coin, dd)
        h = h.sort_values(["close_s"]).reset_index(drop=True)
        i = i.sort_values(["close_s"]).reset_index(drop=True)
        j = h.merge(i, on=["close_s"], how="outer", suffixes=("_h", "_i"), indicator=True)
        lo = int((j._merge == "left_only").sum())
        ro = int((j._merge == "right_only").sum())
        b = j[j._merge == "both"]
        mism = {}
        for f, rt in (("tau", 0), ("side", None), ("outcome", None), ("fair", 1e-9),
                      ("book_age_sig", 1e-6), ("book_age_fil", 1e-6),
                      ("avg_price", 1e-9), ("shares", 1e-9), ("pnl_per_share", 1e-9),
                      ("won", 0)):
            x, y = b[f"{f}_h"], b[f"{f}_i"]
            if f == "won":
                m = int((x.astype(float) != y.astype(float)).sum())
            elif rt is None or rt == 0:
                m = int((x.astype(str) != y.astype(str)).sum())
            else:
                m = int((~np.isclose(x.astype(float), y.astype(float),
                                     rtol=rt, atol=rt, equal_nan=True)).sum())
            mism[f] = m
        tot += lo + ro + sum(mism.values())
        print(f"{coin:9s} harness={len(h):4d} indep={len(i):4d} both={len(b):4d} "
              f"left_only={lo} right_only={ro} | " +
              " ".join(f"{k}={v}" for k, v in mism.items()), flush=True)
    print(f"\nTOTAL DISCREPANCIES: {tot}")
    return 0 if tot == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
