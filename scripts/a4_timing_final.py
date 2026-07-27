#!/usr/bin/env python3
"""A4 — final timing analysis: extended tau, tick-phase sensitivity, bootstrap
CIs on policy differences, cap sweep, and edge-ramp policies."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from a4_timing_scan import (SEC, Klines, fee_per_share, load_tapes, load_windows,  # noqa: E402
                            normal_cdf)

D0, D1 = "2026-05-01", "2026-07-12"
rng = np.random.default_rng(7)


def eval_at(kl, t_float, close_ts, s_open, tape, cfg):
    """Decision tick at an arbitrary (possibly fractional) wall-clock t.

    S_t / sigma come from the 1s kline at floor(t) — the live bot's oracle is a
    ~1 Hz REST poller, so its S_t is the last price at or before `now`.
    """
    tau = close_ts - t_float
    if tau <= 0:
        return None
    ts = int(math.floor(t_float))
    s_t = kl.open_at(ts)
    sg = kl.sigma_1s(ts, cfg["vol_window_secs"])
    if not np.isfinite(sg) or sg <= 0 or not np.isfinite(s_t) or s_t <= 0 or not (s_open > 0):
        return None
    sg = max(sg, cfg["sigma_1s_floor"])
    z = math.log(s_t / s_open) / (sg * math.sqrt(tau))
    fair_up = min(max(normal_cdf(z), 1.0 - cfg["fair_cap"]), cfg["fair_cap"])
    for side, fair in (("up", fair_up), ("down", 1.0 - fair_up)):
        b = tape.as_of(int(round(t_float * SEC)), side)
        if b is None:
            continue
        ask, sz = b
        if not np.isfinite(ask) or not (cfg["price_min"] < ask < cfg["price_max"]):
            continue
        edge = fair - ask - fee_per_share(ask, cfg["fee_rate"])
        if edge > cfg["edge_min"]:
            return dict(side=side, fair=fair, ask=ask, ask_size=sz, edge=edge, tau=tau, t=t_float)
    return None


def fill_at(sig, tape, cfg, cap_usd=None):
    cap_usd = cfg["cap_usd"] if cap_usd is None else cap_usd
    ts = int(round((sig["t"] + cfg["latency_ms"] / 1000.0) * SEC))
    b = tape.as_of(ts, sig["side"])
    if b is None:
        return dict(outcome="no_book", shares=0.0, cost=0.0, fees=0.0, avail=0.0)
    ask, sz = b
    if not np.isfinite(ask):
        return dict(outcome="empty_book", shares=0.0, cost=0.0, fees=0.0, avail=0.0)
    sz = sz if np.isfinite(sz) else 0.0
    if not (cfg["price_min"] < ask < cfg["price_max"]) or \
       sig["fair"] - ask - fee_per_share(ask, cfg["fee_rate"]) <= cfg["edge_min"]:
        return dict(outcome="book_moved_no_edge", shares=0.0, cost=0.0, fees=0.0, avail=ask * sz)
    shares = min(sz, cap_usd / ask)
    if shares <= 1e-9:
        return dict(outcome="book_moved_no_edge", shares=0.0, cost=0.0, fees=0.0, avail=ask * sz)
    return dict(outcome="filled", shares=shares, cost=shares * ask,
                fees=fee_per_share(ask, cfg["fee_rate"]) * shares, avail=ask * sz)


def run_grid(w, kl, tapes, cfg, taus, phase=0.0, cap_usd=None):
    rows = []
    for _, m in w.iterrows():
        wts, close_ts, rid = int(m.wts), int(m.close_ts), int(m.result_id)
        tape = tapes.get(wts)
        if tape is None:
            continue
        s_open = kl.hour_open(wts)
        if not np.isfinite(s_open):
            continue
        won_up = rid == 0
        for k in taus:
            tt = close_ts - (k - phase)
            s = eval_at(kl, tt, close_ts, s_open, tape, cfg)
            if s is None:
                rows.append(dict(wts=wts, date=m.date, tau=k, signal=False, outcome="none",
                                 shares=0.0, cost=0.0, pnl=0.0, won=False, avail=0.0))
                continue
            f = fill_at(s, tape, cfg, cap_usd)
            won = won_up if s["side"] == "up" else (not won_up)
            rows.append(dict(wts=wts, date=m.date, tau=k, signal=True, outcome=f["outcome"],
                             side=s["side"], edge=s["edge"], ask=s["ask"], avail=f["avail"],
                             shares=f["shares"], cost=f["cost"], fees=f["fees"], won=won,
                             pnl=(1.0 if won else 0.0) * f["shares"] - f["cost"] - f["fees"]))
    return pd.DataFrame(rows)


def pick(g, lo, hi, ramp=None):
    """First qualifying second scanning tau high->low inside [lo,hi].
    `ramp` = {tau: extra_edge_required} applied on top of the base edge_min."""
    out = {}
    for wts, sub in g.groupby("wts"):
        s = sub[(sub.tau >= lo) & (sub.tau <= hi) & (sub.signal)]
        if ramp:
            s = s[s.apply(lambda r: r.edge > ramp.get(r.tau, 0.0), axis=1)] if len(s) else s
        out[wts] = (s.sort_values("tau", ascending=False).iloc[0] if len(s) else None)
    return out


def summarize(picks, n_closes, label):
    rs = [p for p in picks.values() if p is not None]
    if not rs:
        return dict(policy=label, signals=0, fills=0, pnl=0.0)
    pk = pd.DataFrame(rs)
    fl = pk[pk.outcome == "filled"]
    per = (fl.pnl / fl.shares) if len(fl) else pd.Series(dtype=float)
    t = (per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))) if len(per) > 1 else np.nan
    return dict(policy=label, signals=len(pk), fills=len(fl),
                fill_rate=100 * len(fl) / len(pk),
                win=(100 * fl.won.mean() if len(fl) else np.nan),
                shares=fl.shares.sum(), notional=fl.cost.sum(), pnl=fl.pnl.sum(),
                c_per_share=(100 * fl.pnl.sum() / fl.shares.sum() if fl.shares.sum() else np.nan),
                t_stat=t, per_day=fl.pnl.sum() / (n_closes / 24.0))


def main():
    with open(ROOT / "bot/config.yaml") as fh:
        y = yaml.safe_load(fh)
    sc = y["strategy"]["close_snipe"]
    base = dict(vol_window_secs=int(sc["vol_window_secs"]),
                sigma_1s_floor=float(sc["sigma_1s_floor"]), fair_cap=float(sc["fair_cap"]),
                price_min=float(sc["price_min"]), price_max=float(sc["price_max"]),
                fee_rate=float(y["fees"]["fee_rate"]), latency_ms=1500, cap_usd=25.0)

    w = load_windows(D0, D1)
    days = sorted(pd.date_range(pd.Timestamp(D0) - pd.Timedelta(days=1),
                                pd.Timestamp(D1) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    kl = Klines(days)
    tapes = load_tapes(days, set(w.wts.astype("int64")), pre=200)
    n_closes = len(tapes)
    print(f"closes with tape: {n_closes}")

    # ---------------------------------------------------- A) extended tau grid
    print("\n=== A) EXTENDED fixed-tau grid (edge_min=0.05, lat=1500ms, cap $25) ===")
    cfg05 = dict(base, edge_min=0.05)
    taus = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 45, 60, 90, 120]
    gx = run_grid(w, kl, tapes, cfg05, taus)
    rows = []
    for k, sub in gx.groupby("tau"):
        s = sub[sub.signal]
        fl = s[s.outcome == "filled"]
        per = (fl.pnl / fl.shares) if len(fl) else pd.Series(dtype=float)
        rows.append(dict(tau=k, signals=len(s), fills=len(fl),
                         fill_rate=100 * len(fl) / len(s) if len(s) else np.nan,
                         pnl=fl.pnl.sum(),
                         cps=100 * fl.pnl.sum() / fl.shares.sum() if fl.shares.sum() else np.nan,
                         win=100 * fl.won.mean() if len(fl) else np.nan,
                         t=(per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))
                            if len(per) > 1 else np.nan)))
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:8.2f}"))

    # ------------------------------------------------- B) tick-phase sweep
    print("\n=== B) TICK-PHASE sensitivity (edge_min=0.03) ===")
    print("the live 1Hz tick has an arbitrary phase phi; it evaluates at tau = k - phi")
    cfg03 = dict(base, edge_min=0.03)
    stores = {}
    for phi in (0.0, 0.25, 0.5, 0.75, 0.9):
        g = run_grid(w, kl, tapes, cfg03, list(range(1, 9)), phase=phi)
        stores[phi] = g
        out = []
        for lo, hi in [(1, 6), (2, 6), (2, 5), (2, 4), (1, 3)]:
            r = summarize(pick(g, lo, hi), n_closes, f"[{lo},{hi}]")
            r["phi"] = phi
            out.append(r)
        print(pd.DataFrame(out)[["phi", "policy", "signals", "fills", "fill_rate", "win",
                                 "pnl", "c_per_share", "t_stat"]]
              .to_string(index=False, float_format=lambda v: f"{v:8.2f}"))
    print("\n  phase-averaged (mean over the 5 phases):")
    agg = {}
    for lo, hi in [(1, 6), (2, 6), (2, 5), (2, 4), (1, 3)]:
        pnls, cps, fills = [], [], []
        for phi, g in stores.items():
            r = summarize(pick(g, lo, hi), n_closes, "")
            pnls.append(r["pnl"]); cps.append(r["c_per_share"]); fills.append(r["fills"])
        agg[f"[{lo},{hi}]"] = dict(pnl=np.mean(pnls), pnl_sd=np.std(pnls),
                                   cps=np.nanmean(cps), fills=np.mean(fills))
    print(pd.DataFrame(agg).T.to_string(float_format=lambda v: f"{v:8.2f}"))

    # ------------------------------------------ C) bootstrap CI on differences
    print("\n=== C) BOOTSTRAP on the policy difference (phi=0, edge_min=0.03, 5000 reps) ===")
    g = stores[0.0]
    def pnl_vec(lo, hi):
        p = pick(g, lo, hi)
        ks = sorted(p)
        return np.array([(p[k].pnl if p[k] is not None else 0.0) for k in ks]), ks
    a, ks = pnl_vec(1, 6)
    for lo, hi in [(2, 6), (2, 5), (2, 4), (1, 3), (3, 5)]:
        b, _ = pnl_vec(lo, hi)
        d = b - a
        idx = rng.integers(0, len(d), size=(5000, len(d)))
        boot = d[idx].sum(axis=1)
        lo_ci, hi_ci = np.percentile(boot, [2.5, 97.5])
        print(f"  [{lo},{hi}] - [1,6]: total {d.sum():+8.2f}  95% CI [{lo_ci:+.2f}, {hi_ci:+.2f}]"
              f"  P(better)={100*(boot>0).mean():.1f}%")

    # -------------------------------------------------- D) edge-ramp policies
    print("\n=== D) EDGE-RAMP policies (base edge_min=0.03, extra bar at high tau) ===")
    rows = []
    rows.append(summarize(pick(g, 1, 6), n_closes, "flat 0.03, window [1,6]  (baseline)"))
    rows.append(summarize(pick(g, 2, 5), n_closes, "flat 0.03, window [2,5]"))
    for hi_bar in (0.05, 0.08, 0.12):
        ramp = {5: hi_bar, 6: hi_bar, 7: hi_bar, 8: hi_bar}
        rows.append(summarize(pick(g, 1, 6, ramp), n_closes,
                              f"ramp: tau>=5 needs {hi_bar}, tau<=4 needs 0.03"))
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:8.2f}"))

    # -------------------------------------------------------- E) cap sweep
    print("\n=== E) CAP sweep (edge_min=0.03, phi=0) ===")
    rows = []
    for cap in (25, 50, 100, 250, 500, 1000):
        gc = run_grid(w, kl, tapes, cfg03, list(range(1, 7)), cap_usd=cap)
        for lo, hi in [(1, 6), (2, 5)]:
            r = summarize(pick(gc, lo, hi), n_closes, f"cap=${cap} window [{lo},{hi}]")
            fl_full = None
            rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: f"{v:8.2f}"))

    # ------------------------------------- F) available depth at fill instant
    print("\n=== F) top-of-book notional AVAILABLE at the fill instant (edge_min=0.03) ===")
    av = g[(g.signal) & (g.tau >= 2) & (g.tau <= 5) & (g.outcome == "filled")].avail
    print(f"  n={len(av)}  median ${av.median():.2f}  mean ${av.mean():.2f}  "
          f"p75 ${av.quantile(.75):.2f}  p90 ${av.quantile(.90):.2f}  max ${av.max():.2f}")
    for c in (25, 50, 100, 250):
        print(f"    fraction of fill moments with >= ${c} available: "
              f"{100*(av >= c).mean():.1f}%")


if __name__ == "__main__":
    main()
