#!/usr/bin/env python3
"""INDEPENDENT re-implementation of the 5m close_snipe replay, written from the
bot's source semantics WITHOUT importing scripts/fresh5m/replay.py.
Purpose: cross-check the C2 harness trade-by-trade on a subset of days.

A3 control convention: edge_min 0.05, tau grid {6,5,4,3,2}, sigma = ffill 1s grid
rolling std (120s, min_periods 30), strike = exact second else last-at-or-before,
top-of-book fill, $25 cap, causal Chainlink (server_timestamp_us <= t),
latency 1500ms, fee 0.07, sigma floor 8e-6, fair_cap 0.98, price in (0.30,0.99),
max_walk_above_best 0.03, oracle staleness cap 4.0s.
"""
import math, sys
import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
PROC = f"{ROOT}/data/data/processed/daily"

EDGE_MIN, PMIN, PMAX = 0.05, 0.30, 0.99
SIG_FLOOR, FAIR_CAP, FEE = 8e-6, 0.98, 0.07
TAUS = [6, 5, 4, 3, 2]
LAT_US = 1_500_000
CAP = 25.0
MAX_ABOVE = 0.03
ORACLE_STALE = 4.0
VOLW = 120


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fee(p):
    return FEE * p * (1.0 - p)


def load_cl(days):
    fr = []
    for d in days:
        fr.append(pd.read_parquet(f"{PROC}/crypto_prices/{d}.parquet",
                                  columns=["timestamp_us", "server_timestamp_us", "price"]))
    x = pd.concat(fr, ignore_index=True).dropna(subset=["price"]).drop_duplicates("timestamp_us")
    x["price"] = x.price.astype(float)
    # publication-ordered view (causal)
    xp = x.sort_values("server_timestamp_us")
    pub = xp.server_timestamp_us.to_numpy(np.int64)
    pub_obs = xp.timestamp_us.to_numpy(np.int64)
    pub_px = xp.price.to_numpy(float)
    # observation-ordered view (for strike/settle and sigma)
    xo = x.sort_values("timestamp_us")
    obs = xo.timestamp_us.to_numpy(np.int64)
    opx = xo.price.to_numpy(float)
    # sigma on ffill 1s grid
    s = pd.Series(np.log(opx), index=obs // 1_000_000).groupby(level=0).last()
    grid = np.arange(int(s.index[0]), int(s.index[-1]) + 1)
    lp = s.reindex(grid).ffill()
    sig = lp.diff().rolling(VOLW, min_periods=30).std(ddof=1)
    return dict(pub=pub, pub_obs=pub_obs, pub_px=pub_px, obs=obs, opx=opx,
                sig_sec=grid, sig_val=sig.to_numpy())


def latest_causal(C, t_us):
    i = np.searchsorted(C["pub"], t_us, side="right") - 1
    if i < 0:
        return None
    return C["pub_px"][i], C["pub_obs"][i]


def sigma_at(C, obs_us):
    sec = int(obs_us // 1_000_000)
    i = np.searchsorted(C["sig_sec"], sec)
    if i >= len(C["sig_sec"]) or C["sig_sec"][i] != sec:
        return float("nan")
    return C["sig_val"][i]


def strike_a3(C, sec):
    """A3 legacy: exact second, else newest print at-or-before."""
    tgt = np.int64(sec) * 1_000_000
    i = np.searchsorted(C["obs"], tgt)
    if i < len(C["obs"]) and C["obs"][i] == tgt:
        return C["opx"][i]
    j = np.searchsorted(C["obs"], tgt, side="right") - 1
    return C["opx"][j] if j >= 0 else float("nan")


def main(days):
    C = load_cl(sorted(set([(pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                            for d in days] + list(days))))
    W = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    rows = []
    for d in days:
        b = pd.read_parquet(f"{PROC}/5m/bookcurves/{d}.parquet",
                            columns=["timestamp_us", "bid_p0", "bid_s0", "ask_p0", "ask_s0", "wts"])
        b = b.dropna(subset=["wts"]).sort_values("timestamp_us")
        grp = {int(k): v for k, v in b.groupby("wts", sort=False)}
        w = W[(W.family == "5m") & (W.date == d)][["wts", "duration", "result_id"]]
        w = w.drop_duplicates("wts").sort_values("wts")
        for _, r in w.iterrows():
            wts, dur = int(r.wts), int(r.duration)
            if pd.isna(r.result_id):
                continue
            g = grp.get(wts)
            if g is None or g.empty:
                continue
            close_s = wts + dur
            up_won = (int(r.result_id) == 0)
            S_open = strike_a3(C, wts)
            if not math.isfinite(S_open):
                continue
            ts = g.timestamp_us.to_numpy(np.int64)
            ask = g.ask_p0.to_numpy(float); asz = g.ask_s0.to_numpy(float)
            bid = g.bid_p0.to_numpy(float); bsz = g.bid_s0.to_numpy(float)

            def book(t_us, side):
                i = int(np.searchsorted(ts, t_us, side="right")) - 1
                if i < 0:
                    return None, float("inf")
                age = (t_us - ts[i]) / 1e6
                if side == "up":
                    p, s_ = ask[i], asz[i]
                else:
                    p, s_ = (1.0 - bid[i]), bsz[i]
                if not np.isfinite(p):
                    return None, age
                return (float(p), float(s_) if np.isfinite(s_) and s_ > 0 else 0.0), age

            fired = False
            for tau in TAUS:
                if fired:
                    break
                t_us = int(round((close_s - tau) * 1e6))
                got = latest_causal(C, t_us)
                if got is None:
                    continue
                S_t, obs_us = got
                if (t_us - obs_us) / 1e6 > ORACLE_STALE:
                    continue
                sg = sigma_at(C, obs_us)
                if not (math.isfinite(sg) and sg > 0):
                    continue
                tau_eff = (close_s * 1e6 - obs_us) / 1e6
                if tau_eff <= 0:
                    continue
                sg = max(sg, SIG_FLOOR)
                z = math.log(S_t / S_open) / (sg * math.sqrt(tau_eff))
                fu = min(max(phi(z), 1.0 - FAIR_CAP), FAIR_CAP)
                for side, fair in (("up", fu), ("down", 1.0 - fu)):
                    bk, age = book(t_us, side)
                    if bk is None:
                        continue
                    a = bk[0]
                    if not (PMIN < a < PMAX):
                        continue
                    e = fair - a - fee(a)
                    if e <= EDGE_MIN:
                        continue
                    # SIGNAL -> fill at t+latency, top of book
                    fb, fage = book(t_us + LAT_US, side)
                    fired = True
                    rec = dict(day=d, wts=wts, tau=tau, side=side, fair=fair,
                               book_age_sig=age, book_age_fil=fage,
                               won=(up_won if side == "up" else not up_won),
                               outcome="filled", shares=0.0, avg_price=np.nan,
                               pnl_per_share=np.nan, pnl=0.0)
                    if fb is None:
                        rec["outcome"] = "empty_book"; rows.append(rec); break
                    fp, fs = fb
                    if not (PMIN < fp < PMAX) or (fair - fp - fee(fp)) <= EDGE_MIN or fs <= 1e-9:
                        rec["outcome"] = "book_moved_no_edge"; rows.append(rec); break
                    sh = min(fs, CAP / fp)
                    if sh <= 1e-9:
                        rec["outcome"] = "book_moved_no_edge"; rows.append(rec); break
                    ps = (1.0 if rec["won"] else 0.0) - fp - fee(fp)
                    rec.update(shares=sh, avg_price=fp, pnl_per_share=ps, pnl=ps * sh)
                    rows.append(rec)
                    break
    return pd.DataFrame(rows)


if __name__ == "__main__":
    days = sys.argv[1].split(",")
    tr = main(days)
    tr.to_parquet("/tmp/claude-0/-home-user-S4/481385a7-e66e-52ff-943a-8c87efc9551d/scratchpad/indep_trades.parquet", index=False)
    f = tr[tr.outcome == "filled"]
    print(f"INDEP: signals={len(tr)} fills={len(f)} ev={100*f.pnl_per_share.mean():.4f}c win={f.won.mean():.4f}")
