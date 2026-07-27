#!/usr/bin/env python3
"""A3 — turn the per-second eval frame into trades + summary stats."""
import numpy as np
import pandas as pd

FEE_RATE = 0.07
PRICE_MIN, PRICE_MAX = 0.30, 0.99
MAX_WALK = 0.03
CAP_USD = 25.0
STALE_MAX = 4.0     # A1 recommended oracle staleness guard (secs)


def fee(p):
    return FEE_RATE * p * (1 - p)


def build_trades(d, edge_min, stale_max=STALE_MAX, taus=(6, 5, 4, 3, 2),
                 side_filter=None, use_real_down=False):
    """Bot semantics: scan taus in descending order (earliest first), take the
    first side (up then down) that clears edge_min on the SIGNAL book, then
    fill against the book at +1.5s (must independently clear edge_min)."""
    d = d[d.tau.isin(taus)].copy()
    if stale_max is not None:
        d = d[d.obs_lag <= stale_max]
    d["fair_down"] = 1 - d.fair_up

    # candidate prices
    d["sig_ask_dn"] = d["sig_ask_dn"] if use_real_down else (1 - d.sig_bid_up)
    d["fil_ask_dn"] = d["fil_ask_dn"] if use_real_down else (1 - d.fil_bid_up)
    d["fil_asz_dn"] = d["fil_asz_dn"] if use_real_down else d.fil_bsz_up

    recs = []
    for (day, wts), g in d.groupby(["day", "wts"], sort=False):
        g = g.sort_values("tau", ascending=False)   # earliest second first
        fired = False
        for _, r in g.iterrows():
            for side in ("up", "down"):
                fv = r.fair_up if side == "up" else r.fair_down
                sa = r.sig_ask_up if side == "up" else r.sig_ask_dn
                fa = r.fil_ask_up if side == "up" else r.fil_ask_dn
                fz = r.fil_asz_up if side == "up" else r.fil_asz_dn
                if not np.isfinite(sa) or not (PRICE_MIN < sa < PRICE_MAX):
                    continue
                if fv - sa - fee(sa) <= edge_min:
                    continue
                if side_filter and side != side_filter:
                    continue
                # ---- SIGNAL fires ----
                fired = True
                out = "filled"
                shares = 0.0
                if not np.isfinite(fa) or not np.isfinite(fz) or fz <= 0:
                    out = "empty_book"
                elif not (PRICE_MIN < fa < PRICE_MAX):
                    out = "book_moved_no_edge"
                elif fv - fa - fee(fa) <= edge_min:
                    out = "book_moved_no_edge"
                else:
                    shares = min(fz, CAP_USD / fa)
                won = (r.rid == 0) if side == "up" else (r.rid == 1)
                pnl_ps = (1.0 if won else 0.0) - fa - fee(fa) if out == "filled" else np.nan
                recs.append(dict(day=day, wts=wts, tau=r.tau, side=side,
                                 fair=fv, sig_ask=sa, fill_ask=fa, size=fz,
                                 shares=shares, outcome=out, won=won,
                                 pnl_per_share=pnl_ps,
                                 pnl=(pnl_ps * shares) if out == "filled" else 0.0,
                                 sigma=r.sigma, tau_eff=r.tau_eff, obs_lag=r.obs_lag,
                                 rid=r.rid,
                                 fil_ask_up=r.fil_ask_up, fil_asz_up=r.fil_asz_up,
                                 fil_avg50=r.fil_avg50, fil_sh50=r.fil_sh50, fil_ex50=r.fil_ex50,
                                 fil_avg200=r.fil_avg200, fil_sh200=r.fil_sh200, fil_ex200=r.fil_ex200,
                                 fil_avg1000=r.fil_avg1000, fil_sh1000=r.fil_sh1000, fil_ex1000=r.fil_ex1000,
                                 fil_avg5000=r.fil_avg5000, fil_sh5000=r.fil_sh5000, fil_ex5000=r.fil_ex5000,
                                 ))
                break
            if fired:
                break
    return pd.DataFrame(recs)


def summarise(tr, n_windows, n_days, label=""):
    if tr is None or tr.empty:
        return dict(label=label, windows=n_windows, days=n_days, signals=0, fills=0,
                    trades_day=0, ev_share=np.nan, win=np.nan, t=np.nan, pnl=0.0, shares=0.0)
    f = tr[tr.outcome == "filled"]
    ps = f.pnl_per_share.to_numpy(float)
    t = (ps.mean() / (ps.std(ddof=1) / np.sqrt(len(ps)))) if len(ps) > 1 and ps.std(ddof=1) > 0 else np.nan
    return dict(label=label, windows=n_windows, days=n_days,
                signals=len(tr), fills=len(f),
                signals_day=len(tr) / n_days, trades_day=len(f) / n_days,
                ev_share=ps.mean() if len(ps) else np.nan,
                med_share=np.median(ps) if len(ps) else np.nan,
                win=f.won.mean() if len(f) else np.nan,
                t=t, pnl=f.pnl.sum(), shares=f.shares.sum(),
                deployed=(f.fill_ask * f.shares).sum(),
                nofill=(tr.outcome != "filled").sum(),
                up_frac=(f.side == "up").mean() if len(f) else np.nan)


def capacity(tr, edge_min):
    """Max notional fillable per FILLED signal within the 3c walk bound and
    with marginal price still clearing edge_min. Uses the UP-token buy curves
    (bookcurves) at fill time; only meaningful for up-side trades."""
    f = tr[(tr.outcome == "filled") & (tr.side == "up")].copy()
    rows = []
    for _, r in f.iterrows():
        best = r.fill_ask
        lim = best + MAX_WALK
        # price at which edge falls to edge_min:  fair - p - fee(p) = edge_min
        # fee(p)=0.07 p(1-p) -> solve quadratic 0.07p^2 -1.07p + (fair-edge_min)=0
        a, b, c = FEE_RATE, -(1 + FEE_RATE), (r.fair - edge_min)
        disc = b * b - 4 * a * c
        p_edge = ((-b - np.sqrt(disc)) / (2 * a)) if disc >= 0 else np.nan
        lim = min(lim, p_edge if np.isfinite(p_edge) else lim, PRICE_MAX - 1e-9)
        tiers = [(50, r.fil_avg50, r.fil_sh50, r.fil_ex50),
                 (200, r.fil_avg200, r.fil_sh200, r.fil_ex200),
                 (1000, r.fil_avg1000, r.fil_sh1000, r.fil_ex1000),
                 (5000, r.fil_avg5000, r.fil_sh5000, r.fil_ex5000)]
        prevN, prevSh, cap_n, cap_sh = 0.0, 0.0, 0.0, 0.0
        for N, avg, sh, ex in tiers:
            if not np.isfinite(avg) or not np.isfinite(sh) or sh <= prevSh:
                break
            marg = (N - prevN) / (sh - prevSh)   # VWAP of the incremental slice
            if marg > lim + 1e-9:
                break
            if ex:                                # book exhausted before N
                cap_n, cap_sh = prevN + (sh - prevSh) * marg, sh
                break
            cap_n, cap_sh = float(N), float(sh)
            prevN, prevSh = float(N), float(sh)
        rows.append(dict(day=r.day, wts=r.wts, best=best, lim=lim,
                         cap_usd=cap_n, cap_shares=cap_sh,
                         top_size_usd=r.fil_asz_up * best,
                         pnl_per_share=r.pnl_per_share))
    return pd.DataFrame(rows)
