#!/usr/bin/env python3
"""A3 — decompose the gap between the 2026 research claim and the current bot."""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, '/home/user/S4/scripts/a3')

FEE = 0.07
PMIN, PMAX = 0.30, 0.99
CAP = 25.0


def fee(p):
    return FEE * p * (1 - p)


def variant(d, fair_col, edge_min, exec_mode, stale_max=4.0, taus=(6, 5, 4, 3, 2)):
    """exec_mode:
       'bot'      signal on book@t, fill on book@t+1.5s, edge must re-clear
       'oneshot'  evaluate + fill on the SAME book@t+1.5s (the 2026 research
                  backtest's convention -- no book-movement risk at all)
    """
    x = d[d.tau.isin(taus)].copy()
    if stale_max is not None:
        x = x[x.obs_lag <= stale_max]
    fu = x[fair_col].to_numpy(float)
    x["_fu"], x["_fd"] = fu, 1 - fu
    x["_sig_dn"] = 1 - x.sig_bid_up
    x["_fil_dn"] = 1 - x.fil_bid_up
    recs = []
    for (day, wts), g in x.groupby(["day", "wts"], sort=False):
        g = g.sort_values("tau", ascending=False)
        hit = False
        for _, r in g.iterrows():
            for side in ("up", "down"):
                fv = r._fu if side == "up" else r._fd
                sa = r.sig_ask_up if side == "up" else r._sig_dn
                fa = r.fil_ask_up if side == "up" else r._fil_dn
                fz = r.fil_asz_up if side == "up" else r.fil_bsz_up
                if exec_mode == "oneshot":
                    sa = fa                       # decision made on the lagged book
                if not np.isfinite(sa) or not (PMIN < sa < PMAX):
                    continue
                if fv - sa - fee(sa) <= edge_min:
                    continue
                hit = True
                filled = (np.isfinite(fa) and (PMIN < fa < PMAX)
                          and np.isfinite(fz) and fz > 0
                          and fv - fa - fee(fa) > edge_min)
                won = (r.rid == 0) if side == "up" else (r.rid == 1)
                ps = ((1.0 if won else 0.0) - fa - fee(fa)) if filled else np.nan
                recs.append(dict(day=day, wts=wts, tau=r.tau, side=side, filled=filled,
                                 fill_ask=fa, shares=(min(fz, CAP / fa) if filled else 0.0),
                                 won=won, ps=ps))
                break
            if hit:
                break
    t = pd.DataFrame(recs)
    return t


def summ(t, ndays, label):
    if t.empty:
        return dict(variant=label, signals=0, fills=0, trades_day=0)
    f = t[t.filled]
    ps = f.ps.to_numpy(float)
    ts = ps.mean() / (ps.std(ddof=1) / np.sqrt(len(ps))) if len(ps) > 1 else np.nan
    return dict(variant=label, signals=len(t), fills=len(f),
                sig_day=round(len(t) / ndays, 1), trades_day=round(len(f) / ndays, 1),
                ev_share=round(ps.mean(), 4) if len(ps) else np.nan,
                win=round(f.won.mean(), 3) if len(f) else np.nan,
                t=round(ts, 2) if np.isfinite(ts) else np.nan,
                pnl=round((f.ps * f.shares).sum(), 1))


if __name__ == "__main__":
    d = pd.read_parquet('/home/user/S4/data/a3/evals_all.parquet')
    periods = {
        "Apr02-Jul07 (all 43d)": (d.day >= '2026-04-02'),
        "Apr16-May12+Jul6/7 (orig OOS)": (d.day >= '2026-04-16'),
        "Jul06-07 (latest)": (d.day >= '2026-07-06'),
    }
    for pname, mask in periods.items():
        dd = d[mask]
        nd = dd.day.nunique()
        rows = []
        rows.append(summ(variant(dd, "fair_up", 0.05, "bot"), nd, "A current bot (cap.98/floor/lag/2-book)"))
        rows.append(summ(variant(dd, "fair_up_nolag", 0.05, "bot"), nd, "B  + zero publication lag (cheat)"))
        rows.append(summ(variant(dd, "fair_up_raw", 0.05, "bot"), nd, "C  no fair_cap / no sigma floor"))
        rows.append(summ(variant(dd, "fair_up", 0.05, "oneshot"), nd, "D  one-book execution (2026 convention)"))
        rows.append(summ(variant(dd, "fair_up_raw", 0.05, "oneshot"), nd, "E  C + D = 2026 research setup"))
        rows.append(summ(variant(dd, "fair_up_raw", 0.05, "oneshot", stale_max=None), nd, "F  E + no staleness guard"))
        print(f"\n### {pname}   days={nd}")
        print(pd.DataFrame(rows).to_string(index=False))
