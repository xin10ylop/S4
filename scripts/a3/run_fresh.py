#!/usr/bin/env python3
"""A3 Part 2 — 5m close_snipe on FRESH Telonex data (out of sample by design).

Both outcome books are real (no 1-bid_up synthetic), 5 ladder levels available,
Chainlink signal obeys the measured publication lag.
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay5m import load_cl, fair_up, ROOT

FEE = 0.07
PMIN, PMAX = 0.30, 0.99
MAX_WALK = 0.03
CAP = 25.0
TAUS = [6, 5, 4, 3, 2]
LAT = 1.5
BOOKS = f"{ROOT}/data/a3/books5"


def fee(p):
    return FEE * p * (1 - p)


def build_evals(days):
    sn = pd.read_parquet(f"{BOOKS}/snaps.parquet")
    mk = pd.read_parquet(f"{BOOKS}/_markets.parquet")
    mk["result_id"] = pd.to_numeric(mk.result_id, errors="coerce")
    res = dict(zip(mk.close_s, mk.result_id))
    sn["rel"] = sn.rel.round(2)
    key = sn.set_index(["close_s", "oid", "rel"]).sort_index()

    out = []
    for day in days:
        prevd = (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_cl([prevd, day], src="a3")
        if cl is None:
            print("no CL", day)
            continue
        closes = np.array(sorted(sn.loc[sn.d == day, "close_s"].unique()))
        if not len(closes):
            continue
        wts = closes - 300
        S_open = cl.at_obs(wts)
        miss = ~np.isfinite(S_open)
        if miss.any():
            S_open[miss] = cl.at_obs_or_before(wts[miss])
        for k in TAUS:
            t_us = (closes - k) * 1_000_000
            px, obs_us, pub_us = cl.latest_at(t_us)
            tau_eff = (closes * 1_000_000 - obs_us) / 1e6
            sg = cl.sigma_at_obs(obs_us)
            fu = fair_up(px, S_open, sg, tau_eff)
            df = pd.DataFrame(dict(day=day, close_s=closes, wts=wts, tau=k,
                                   rid=[res.get(c, np.nan) for c in closes],
                                   S_open=S_open, S_t=px,
                                   obs_lag=(t_us - obs_us) / 1e6,
                                   pub_lag=(pub_us - obs_us) / 1e6,
                                   tau_eff=tau_eff, sigma=sg, fair_up=fu))
            for oid, tag in ((0, "up"), (1, "dn")):
                for when, rel in (("sig", -k), ("fil", -k + LAT)):
                    r = round(rel, 2)
                    try:
                        blk = key.xs((oid, r), level=("oid", "rel"))
                    except KeyError:
                        continue
                    blk = blk.reindex(closes)
                    if when == "sig":
                        df[f"sig_ask_{tag}"] = blk.ask_price_0.to_numpy()
                        df[f"sig_bid_{tag}"] = blk.bid_price_0.to_numpy()
                    else:
                        for L in range(5):
                            df[f"fil_ap{L}_{tag}"] = blk[f"ask_price_{L}"].to_numpy()
                            df[f"fil_as{L}_{tag}"] = blk[f"ask_size_{L}"].to_numpy()
                        df[f"fil_ask_{tag}"] = blk.ask_price_0.to_numpy()
                        df[f"fil_asz_{tag}"] = blk.ask_size_0.to_numpy()
                        df[f"fil_bid_{tag}"] = blk.bid_price_0.to_numpy()
                        df[f"fil_bsz_{tag}"] = blk.bid_size_0.to_numpy()
                        df[f"fil_age_{tag}"] = blk.book_age.to_numpy()
            out.append(df)
        print(day, "ok", flush=True)
    e = pd.concat(out, ignore_index=True)
    e = e[np.isfinite(e.rid) & np.isfinite(e.fair_up)]
    return e


def walk(prices, sizes, fair, edge_min, cap=CAP, max_above=MAX_WALK):
    """fill_engine.walk_asks, vectorised over one ladder."""
    shares = cost = 0.0
    rem = cap
    best = None
    for p, s in zip(prices, sizes):
        if rem <= 0 or not np.isfinite(p) or not np.isfinite(s):
            break
        if not (PMIN < p < PMAX):
            break
        if best is None:
            best = p
        elif p > best + max_above + 1e-9:
            break
        if fair - p - fee(p) <= edge_min:
            break
        sh = min(s, rem / p)
        if sh <= 1e-9:
            break
        shares += sh
        cost += sh * p
        rem -= sh * p
    return shares, cost


def build_trades(e, edge_min, down_mode="real", taus=TAUS, stale_max=4.0, ladder=True):
    x = e[e.tau.isin(taus)].copy()
    if stale_max is not None:
        x = x[x.obs_lag <= stale_max]
    recs = []
    for (day, cs), g in x.groupby(["day", "close_s"], sort=False):
        g = g.sort_values("tau", ascending=False)
        hit = False
        for _, r in g.iterrows():
            for side in ("up", "down"):
                fv = r.fair_up if side == "up" else 1 - r.fair_up
                if side == "up":
                    sa, fa, fz = r.sig_ask_up, r.fil_ask_up, r.fil_asz_up
                    lp = [r[f"fil_ap{L}_up"] for L in range(5)]
                    ls = [r[f"fil_as{L}_up"] for L in range(5)]
                else:
                    if down_mode == "real":
                        sa, fa, fz = r.sig_ask_dn, r.fil_ask_dn, r.fil_asz_dn
                        lp = [r[f"fil_ap{L}_dn"] for L in range(5)]
                        ls = [r[f"fil_as{L}_dn"] for L in range(5)]
                    else:      # synthetic: buy Down == sell Up at the Up bid
                        sa, fa, fz = 1 - r.sig_bid_up, 1 - r.fil_bid_up, r.fil_bsz_up
                        lp, ls = [fa], [fz]
                if not np.isfinite(sa) or not (PMIN < sa < PMAX):
                    continue
                if fv - sa - fee(sa) <= edge_min:
                    continue
                hit = True
                if ladder:
                    sh, cost = walk(lp, ls, fv, edge_min)
                else:
                    sh, cost = (0.0, 0.0)
                    if np.isfinite(fa) and (PMIN < fa < PMAX) and np.isfinite(fz) and fz > 0 \
                       and fv - fa - fee(fa) > edge_min:
                        sh = min(fz, CAP / fa)
                        cost = sh * fa
                filled = sh > 0
                avg = cost / sh if filled else np.nan
                won = (r.rid == 0) if side == "up" else (r.rid == 1)
                ps = ((1.0 if won else 0.0) - avg - fee(avg)) if filled else np.nan
                recs.append(dict(day=day, close_s=cs, tau=r.tau, side=side, fair=fv,
                                 sig_ask=sa, fill_ask=fa, avg=avg, shares=sh,
                                 filled=filled, won=won, ps=ps,
                                 pnl=(ps * sh) if filled else 0.0,
                                 lp=lp, ls=ls, obs_lag=r.obs_lag))
                break
            if hit:
                break
    return pd.DataFrame(recs)


def summ(t, nwin, nd, label):
    if t is None or t.empty:
        return dict(label=label, windows=nwin, days=nd, signals=0, fills=0, trades_day=0.0)
    f = t[t.filled]
    ps = f.ps.to_numpy(float)
    ts = ps.mean() / (ps.std(ddof=1) / np.sqrt(len(ps))) if len(ps) > 1 else np.nan
    return dict(label=label, windows=nwin, days=nd, signals=len(t), fills=len(f),
                sig_day=round(len(t) / nd, 1), trades_day=round(len(f) / nd, 1),
                ev_share=round(ps.mean(), 4) if len(ps) else np.nan,
                med_share=round(np.median(ps), 4) if len(ps) else np.nan,
                win=round(f.won.mean(), 3) if len(f) else np.nan,
                t=round(ts, 2) if np.isfinite(ts) else np.nan,
                pnl=round(f.pnl.sum(), 1), shares=round(f.shares.sum(), 1),
                up_frac=round((f.side == "up").mean(), 3) if len(f) else np.nan)


if __name__ == "__main__":
    days = sys.argv[1].split(",")
    e = build_evals(days)
    e.to_parquet(f"{ROOT}/data/a3/evals_fresh.parquet", index=False)
    print("evals", len(e), "windows", e.groupby(["day", "close_s"]).ngroups)
