#!/usr/bin/env python3
"""M3: every table in audit/M3_multicoin_backtest.md.

Deterministic: re-running regenerates every CSV under data/multicoin/m3/.

Sections
  0  harness validation      settle reconciliation + independent data-layer check
  1  headline per coin       shipped params, full sample / train / test
  2  staleness sensitivity   pre-decision book-age filter sweep
  3  frequency decomposition where signals die, band occupancy
  4  competition hypothesis  EV vs depth / volume / staleness (cross-coin + within)
  5  capacity                fillable notional per signal, p10/p50/p90
  6  parameter sanity        sigma floor / vol window / edge_min, TRAIN ONLY
  7  portfolio               combined trades/day, $/day, cross-coin correlation
  8  edge decomposition      direction accuracy vs realised win rate (adverse selection)
"""
from __future__ import annotations

import itertools
import math
import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

ROOT = "/home/user/S4"
sys.path.insert(0, f"{ROOT}/scripts")
from multicoin.replay_hourly import (  # noqa: E402
    COINS, DATA, CoinTape, HParams, INF, summarise, _t,
)

OUT = f"{DATA}/m3"
os.makedirs(OUT, exist_ok=True)
pd.set_option("display.width", 250)

# tick size of the UNDERLYING on Binance (for the "tie" diagnostic).  Read off
# the fetched 1s klines rather than assumed.
TRAIN_FRAC = 0.60


def hdr(s):
    print(f"\n{'='*100}\n{s}\n{'='*100}", flush=True)


def show(d: pd.DataFrame, name: str, fmt="{:,.4f}"):
    print(d.to_string(index=False, float_format=lambda v: fmt.format(v)), flush=True)
    d.to_csv(f"{OUT}/{name}.csv", index=False)


# ==========================================================================
def main():
    coins = list(COINS)
    tapes = {c: CoinTape(c) for c in coins}
    all_days = sorted(set(itertools.chain.from_iterable(t.days for t in tapes.values())))
    n_split = int(round(len(all_days) * TRAIN_FRAC))
    TRAIN, TEST = all_days[:n_split], all_days[n_split:]
    print(f"\nSAMPLE {all_days[0]} .. {all_days[-1]}  ({len(all_days)} days)")
    print(f"TRAIN  {TRAIN[0]} .. {TRAIN[-1]}  ({len(TRAIN)} days)")
    print(f"TEST   {TEST[0]} .. {TEST[-1]}  ({len(TEST)} days)")

    SHIP = HParams()          # bot/config.yaml, verbatim

    # ---------------------------------------------------------------- 0
    hdr("0. HARNESS VALIDATION — settle reconciliation over the replay window")
    rows = []
    for c in coins:
        t = tapes[c]
        n = agree = ties = nod = 0
        for w in t.windows:
            o = t.feed.hour_open(w.open_s, w.close_s)
            cl = t.feed.hour_close(w.open_s, w.close_s)
            if o is None or cl is None:
                nod += 1
                continue
            n += 1
            ties += (cl == o)
            agree += ((0 if cl >= o else 1) == w.result_id)
        rows.append(dict(coin=c, symbol=t.symbol, closes=n, no_data=nod, exact_ties=ties,
                         settle_agreement=agree / n if n else np.nan))
    show(pd.DataFrame(rows), "t0_settle")

    # ---------------------------------------------------------------- 1
    hdr("1. HEADLINE — close_snipe at SHIPPED parameters (edge_min 0.03, tau[2.5,5], "
        "cap $250, latency 1500ms, two-book fill)")
    trades = {}
    for c in coins:
        tr, meta = tapes[c].run(SHIP)
        tr["coin"] = c
        trades[c] = (tr, meta)
    pd.concat([t for t, _ in trades.values()], ignore_index=True).to_parquet(
        f"{OUT}/trades_shipped.parquet")

    def bundle(days, label):
        rr = []
        for c in coins:
            tr, meta = trades[c]
            dd = [d for d in meta["day_list"] if days is None or d in set(days)]
            sub = tr[tr.day.isin(dd)] if len(tr) else tr
            nw = sum(1 for w in tapes[c].windows if w.day in set(dd))
            s = summarise(sub, len(dd), nw, c)
            s["coin"] = c
            s["frame"] = label
            s["pnl_day_25"] = np.nan
            rr.append(s)
        return pd.DataFrame(rr)

    full = bundle(None, "full")
    cols = ["coin", "days", "closes", "signals", "fills", "book_moved", "empty_book",
            "ev_share", "ev_ci_lo", "ev_ci_hi", "win", "trades_day", "t_trade", "t_day",
            "pnl_day", "ev_drop1", "ev_drop3", "pnl_day_drop1", "pnl_day_drop3",
            "shares_per_fill", "notional_per_fill"]
    show(full[cols], "t1_headline_full")

    hdr("1b. TRAIN vs TEST (temporal stability; parameters were fixed by the BTC work "
        "before this run, so neither frame is used for tuning)")
    tt = pd.concat([bundle(TRAIN, "train"), bundle(TEST, "test")], ignore_index=True)
    show(tt[["frame", "coin", "days", "signals", "fills", "ev_share", "win",
             "trades_day", "t_day", "pnl_day"]], "t1b_train_test")

    # $/day at a $25 clip -------------------------------------------------
    hdr("1c. $/day at a $25 per-event clip vs $250 (capacity-limited?)")
    r25 = []
    for c in coins:
        tr, meta = tapes[c].run(replace(SHIP, per_event_cap_usd=25.0))
        s = summarise(tr, meta["days"], meta["windows"], c, cap_usd=25.0)
        s["coin"] = c
        r25.append(s)
    d25 = pd.DataFrame(r25)[["coin", "fills", "ev_share", "pnl_day", "shares_per_fill",
                             "notional_per_fill"]]
    d25.columns = ["coin", "fills_25", "ev_share_25", "pnl_day_25", "shares_25", "notional_25"]
    cap = full[["coin", "fills", "ev_share", "pnl_day", "shares_per_fill",
                "notional_per_fill"]].merge(d25, on="coin")
    show(cap, "t1c_clip")

    # ---------------------------------------------------------------- 2
    hdr("2. BOOK-STALENESS SENSITIVITY (pre-decision rejection, the honest convention)")
    rows = []
    for age in (2.0, 5.0, 10.0, 30.0, INF):
        for c in coins:
            tr, meta = tapes[c].run(replace(SHIP, max_book_age_s=age))
            s = summarise(tr, meta["days"], meta["windows"], c)
            s.update(coin=c, max_book_age=age)
            rows.append(s)
    st = pd.DataFrame(rows)
    show(st[["max_book_age", "coin", "signals", "fills", "ev_share", "win",
             "trades_day", "t_day", "pnl_day", "stale_book_blocked"]], "t2_staleness")

    hdr("2b. What the STALE fills are (age > 5 s at signal or fill), pooled and per coin")
    tr_all = pd.concat([t for t, _ in trades.values()], ignore_index=True)
    f = tr_all[tr_all.outcome == "filled"].copy()
    f["stale5"] = (f.book_age_sig > 5) | (f.book_age_fil > 5)
    g = (f.groupby(["coin", "stale5"])
           .agg(n=("won", "size"), win=("won", "mean"), ev=("pnl_per_share", "mean"),
                pnl=("pnl", "sum"))
           .reset_index())
    show(g, "t2b_stale_fills")

    # ---------------------------------------------------------------- 3
    hdr("3. FREQUENCY DECOMPOSITION — where the funnel loses closes")
    rows = []
    for c in coins:
        tr, meta = trades[c]
        n_cl = meta["windows"]
        o = tr.outcome.value_counts() if len(tr) else pd.Series(dtype=int)
        rows.append(dict(coin=c, closes=n_cl, days=meta["days"],
                         signals=len(tr), signal_rate=len(tr) / n_cl,
                         book_moved=int(o.get("book_moved_no_edge", 0)),
                         empty_book=int(o.get("empty_book", 0)),
                         fills=int(o.get("filled", 0)),
                         fill_conv=(o.get("filled", 0) / len(tr)) if len(tr) else np.nan,
                         signals_day=len(tr) / meta["days"],
                         fills_day=o.get("filled", 0) / meta["days"]))
    show(pd.DataFrame(rows), "t3_funnel")

    # ---------------------------------------------------------------- 4/5
    hdr("4/5. LIQUIDITY, STALENESS AND CAPACITY per coin (measured on the tape at the "
        "decision instant tau=5s, ALL closes — not just signals)")
    liq = []
    for c in coins:
        t = tapes[c]
        recs = []
        for w in t.windows:
            t_us = int((w.close_s - 5.0) * 1e6)
            for tape, oid in ((w.up, 0), (w.dn, 1)):
                lv, age = tape.at(t_us)
                if lv is None:
                    continue
                recs.append((w.day, w.close_s, oid, lv[0].price, lv[0].size, age))
        q = pd.DataFrame(recs, columns=["day", "close_s", "oid", "ask", "size", "age"])
        q["usd"] = q.ask * q["size"]
        band = q[(q.ask > 0.30) & (q.ask < 0.99)]
        liq.append(dict(
            coin=c, obs=len(q),
            frac_in_band=float(((q.groupby("close_s").apply(
                lambda x: ((x.ask > .30) & (x.ask < .99)).any(), include_groups=False)).mean())),
            med_age=q.age.median(), p90_age=q.age.quantile(.90), p99_age=q.age.quantile(.99),
            frac_age_gt5=float((q.age > 5).mean()),
            band_p10_usd=band.usd.quantile(.10), band_p50_usd=band.usd.median(),
            band_p90_usd=band.usd.quantile(.90), band_mean_usd=band.usd.mean(),
        ))
    liqd = pd.DataFrame(liq)
    vol = pd.read_parquet(f"{DATA}/gamma_volume.parquet")
    vcol = "volumeNum" if "volumeNum" in vol.columns else vol.columns[
        [c.lower().startswith("volume") for c in vol.columns]][0]
    ccol = "coin" if "coin" in vol.columns else None
    if ccol:
        v = vol.groupby(ccol)[vcol].median().rename("med_volume_usd").reset_index()
        v.columns = ["coin", "med_volume_usd"]
        liqd = liqd.merge(v, on="coin", how="left")
    show(liqd, "t45_liquidity_capacity")

    hdr("5b. FILLABLE NOTIONAL PER SIGNAL (the fill book, after latency, inside the "
        "3c walk bound) — this is what a clip can actually be")
    rows = []
    for c in coins:
        tr, _ = trades[c]
        s = tr[tr.outcome.isin(["filled", "book_moved_no_edge"])]
        fn = s.fill_notional.dropna()
        ff = tr[tr.outcome == "filled"]
        rows.append(dict(coin=c, n_signals_with_book=len(fn),
                         p10=fn.quantile(.10) if len(fn) else np.nan,
                         p50=fn.median() if len(fn) else np.nan,
                         p90=fn.quantile(.90) if len(fn) else np.nan,
                         mean=fn.mean() if len(fn) else np.nan,
                         filled_notional_med=(ff.avg_price * ff.shares).median()
                         if len(ff) else np.nan,
                         frac_cap_binding=float(((ff.avg_price * ff.shares) > 249.0).mean())
                         if len(ff) else np.nan))
    show(pd.DataFrame(rows), "t5b_capacity_per_signal")

    hdr("4b. COMPETITION HYPOTHESIS — cross-coin regression of EV/share on liquidity")
    X = full[["coin", "ev_share", "fills", "trades_day"]].merge(liqd, on="coin")
    X["log_vol"] = np.log10(X.med_volume_usd)
    out = []
    for xv in ["log_vol", "band_p50_usd", "med_age", "p90_age", "frac_age_gt5"]:
        x, y = X[xv].to_numpy(float), X.ev_share.to_numpy(float)
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 3:
            continue
        b, a = np.polyfit(x[m], y[m], 1)
        r = np.corrcoef(x[m], y[m])[0, 1]
        n = int(m.sum())
        tt_ = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
        # weighted by fills (a 3-fill coin should not drive the line)
        w = X.fills.to_numpy(float)[m]
        bw = np.polyfit(x[m], y[m], 1, w=np.sqrt(w))[0] if w.sum() > 0 else np.nan
        out.append(dict(x=xv, n_coins=n, slope=b, intercept=a, pearson_r=r,
                        t=tt_, r2=r * r, slope_fill_weighted=bw))
    show(pd.DataFrame(out), "t4b_competition_crosscoin")

    hdr("4c. COMPETITION HYPOTHESIS — WITHIN-sample: does a staler book pay more?")
    ff = tr_all[tr_all.outcome == "filled"].copy()
    ff["age"] = ff[["book_age_sig", "book_age_fil"]].max(axis=1)
    bins = [-.001, .5, 2, 5, 30, 1e9]
    ff["age_bin"] = pd.cut(ff.age, bins, labels=["<0.5s", "0.5-2s", "2-5s", "5-30s", ">30s"])
    gg = (ff.groupby("age_bin", observed=True)
            .agg(n=("won", "size"), win=("won", "mean"), ev=("pnl_per_share", "mean"),
                 ask=("avg_price", "mean"), pnl=("pnl", "sum")).reset_index())
    show(gg, "t4c_ev_by_bookage")
    if len(ff) > 5:
        x = ff.age.to_numpy(float)
        y = ff.pnl_per_share.to_numpy(float)
        r = np.corrcoef(np.log1p(x), y)[0, 1]
        print(f"corr(log1p(book_age), pnl/share) over {len(ff)} fills = {r:+.3f}")

    # ---------------------------------------------------------------- 6
    hdr("6. PARAMETER SANITY per coin — TRAIN DAYS ONLY, diagnostic, NOT adopted")
    rows = []
    for c in coins:
        t = tapes[c]
        ws = [w for w in t.windows if w.day in set(TRAIN)]
        sg, zs, rets = [], [], []
        for w in ws:
            t_us = int((w.close_s - 5.0) * 1e6)
            S_t, obs = t.feed.held_at(t_us)
            S_o = t.feed.hour_open(w.open_s, w.close_s)
            s = t.feed.sigma_at(t_us)
            if None in (S_t, S_o) or not np.isfinite(s):
                continue
            tau = (w.close_s * 1e6 - obs) / 1e6
            sg.append(s)
            zs.append(math.log(S_t / S_o) / (max(s, SHIP.sigma_1s_floor) * math.sqrt(tau)))
            rets.append(1e4 * math.log(S_t / S_o))
        sg, zs, rets = np.array(sg), np.array(zs), np.array(rets)
        rows.append(dict(coin=c, n=len(sg),
                         sigma_p05=np.percentile(sg, 5), sigma_p50=np.median(sg),
                         sigma_p95=np.percentile(sg, 95),
                         floor_binds=float((sg <= SHIP.sigma_1s_floor).mean()),
                         med_abs_z=float(np.median(np.abs(zs))),
                         frac_absz_gt10=float((np.abs(zs) > 10).mean()),
                         frac_absz_lt1=float((np.abs(zs) < 1).mean()),
                         med_abs_ret_bp=float(np.median(np.abs(rets))),
                         frac_ret_zero=float((np.abs(rets) < 1e-9).mean())))
    show(pd.DataFrame(rows), "t6_param_sanity")

    hdr("6b. sigma_1s_floor / edge_min sensitivity, TRAIN ONLY")
    rows = []
    for c in coins:
        for sf in (8e-6, 2e-5, 5e-5):
            for em in (0.03, 0.05, 0.10):
                tr, meta = tapes[c].run(replace(SHIP, sigma_1s_floor=sf, edge_min=em), TRAIN)
                s = summarise(tr, meta["days"], meta["windows"], c)
                s.update(coin=c, sigma_floor=sf, edge_min=em)
                rows.append(s)
    sw = pd.DataFrame(rows)
    show(sw[["coin", "sigma_floor", "edge_min", "signals", "fills", "ev_share",
             "win", "trades_day", "t_day", "pnl_day"]], "t6b_param_sweep_train")

    # ---------------------------------------------------------------- 7
    hdr("7. PORTFOLIO — all coins at once")
    port = tr_all[tr_all.outcome == "filled"].copy()
    nd = len(all_days)
    for label, sel in (("ALL 7", coins),
                       ("non-BTC", [c for c in coins if c != "bitcoin"]),
                       ("BTC+ETH+SOL+XRP", ["bitcoin", "ethereum", "solana", "xrp"]),
                       ("BTC+ETH", ["bitcoin", "ethereum"]),
                       ("BTC only", ["bitcoin"])):
        p = port[port.coin.isin(sel)]
        daily = p.groupby("day").pnl.sum().reindex(all_days).fillna(0.0)
        dps = p.groupby("day").pnl_per_share.mean()
        print(f"{label:>18}: fills={len(p):4d}  trades/day={len(p)/nd:5.2f}  "
              f"EV/share={p.pnl_per_share.mean()*100:+7.2f}c  win={p.won.mean():.3f}  "
              f"$/day={daily.mean():+8.2f}  t_day(pnl)={_t(daily.to_numpy()):+6.2f}  "
              f"t_day(ev)={_t(dps.to_numpy()):+6.2f}")
    hdr("7b. cross-coin correlation of DAILY P&L (0 on days with no trade)")
    dm = (port.pivot_table(index="day", columns="coin", values="pnl", aggfunc="sum")
              .reindex(all_days).fillna(0.0))
    show(dm.corr().round(3).reset_index(), "t7b_daily_pnl_corr", "{:,.3f}")
    hdr("7c. SAME-HOUR concurrency: do coins fire and lose together?")
    ch = port.groupby("close_s").agg(n=("coin", "size"), nwin=("won", "sum"),
                                     pnl=("pnl", "sum"))
    print(f"closes with >=2 concurrent fills: {(ch.n>=2).sum()} of {len(ch)}")
    if (ch.n >= 2).sum():
        m = ch[ch.n >= 2]
        print(f"  of those, all-win {(m.nwin==m.n).sum()}, all-lose {(m.nwin==0).sum()}, "
              f"mixed {((m.nwin>0)&(m.nwin<m.n)).sum()}")
        print(f"  expected all-lose if independent at pooled win rate "
              f"{port.won.mean():.3f}: {((1-port.won.mean())**m.n).sum():.2f}")
    ch.reset_index().to_csv(f"{OUT}/t7c_concurrency.csv", index=False)

    # ---------------------------------------------------------------- 8
    hdr("8. EDGE DECOMPOSITION — direction accuracy (all closes) vs realised win rate "
        "on fills. A gap is ADVERSE SELECTION.")
    rows = []
    for c in coins:
        t = tapes[c]
        acc = {}
        for tau in (5.0, 3.0):
            ok = tot = 0
            for w in t.windows:
                t_us = int((w.close_s - tau) * 1e6)
                S_t, obs = t.feed.held_at(t_us)
                S_o = t.feed.hour_open(w.open_s, w.close_s)
                if S_t is None or S_o is None:
                    continue
                tot += 1
                pred_up = (S_t >= S_o)
                ok += (pred_up == (w.result_id == 0))
            acc[tau] = ok / tot if tot else np.nan
        tr, _ = trades[c]
        fl = tr[tr.outcome == "filled"]
        rows.append(dict(coin=c, acc_tau5=acc[5.0], acc_tau3=acc[3.0],
                         fills=len(fl), realised_win=fl.won.mean() if len(fl) else np.nan,
                         mean_ask=fl.avg_price.mean() if len(fl) else np.nan,
                         ev_implied_by_acc=(acc[5.0] - fl.avg_price.mean()
                                            - 0.07 * fl.avg_price.mean()
                                            * (1 - fl.avg_price.mean())) if len(fl) else np.nan,
                         ev_realised=fl.pnl_per_share.mean() if len(fl) else np.nan))
    show(pd.DataFrame(rows), "t8_edge_decomposition")

    hdr("8c. ADVERSE SELECTION, tested. H0: fills win at the coin's UNCONDITIONAL "
        "direction accuracy at tau=5s. One-sided binomial.")
    from math import comb
    rows = []
    for c in coins:
        tr, _ = trades[c]
        fl = tr[tr.outcome == "filled"]
        n = len(fl)
        if n == 0:
            rows.append(dict(coin=c, n=0)); continue
        k = int(fl.won.sum())
        t8 = pd.read_csv(f"{OUT}/t8_edge_decomposition.csv").set_index("coin")
        p0 = float(t8.loc[c, "acc_tau5"])
        pv = sum(comb(n, i) * p0**i * (1 - p0)**(n - i) for i in range(0, k + 1))
        ph = k / n
        se = math.sqrt(max(ph * (1 - ph), 1e-12) / n)
        rows.append(dict(coin=c, n=n, wins=k, win_rate=ph,
                         wilson_lo=(ph + 1.96**2/(2*n) - 1.96*math.sqrt(ph*(1-ph)/n
                                    + 1.96**2/(4*n*n))) / (1 + 1.96**2/n),
                         wilson_hi=(ph + 1.96**2/(2*n) + 1.96*math.sqrt(ph*(1-ph)/n
                                    + 1.96**2/(4*n*n))) / (1 + 1.96**2/n),
                         unconditional_acc=p0, shortfall=ph - p0,
                         p_one_sided=pv, se=se))
    show(pd.DataFrame(rows), "t8c_adverse_selection")

    hdr("8d. JOINT TAIL — late sign flips across coins (n = every close, not just "
        "fills). flip = sign(S_t - S_open) at tau=5s disagrees with the settle.")
    flips = {}
    for c in coins:
        t = tapes[c]
        rec = {}
        for w in t.windows:
            t_us = int((w.close_s - 5.0) * 1e6)
            S_t, _o = t.feed.held_at(t_us)
            S_o = t.feed.hour_open(w.open_s, w.close_s)
            if S_t is None or S_o is None:
                continue
            rec[w.close_s] = int((S_t >= S_o) != (w.result_id == 0))
        flips[c] = pd.Series(rec)
    F = pd.DataFrame(flips).dropna()
    print(f"closes with all {len(coins)} coins: {len(F)}")
    print("flip rate per coin:")
    print((F.mean() * 100).round(3).to_string())
    print("\nPhi correlation of flip events across coins:")
    show(F.corr().round(3).reset_index(), "t8d_flip_corr", "{:,.3f}")
    k = F.sum(axis=1)
    obs = k.value_counts().sort_index()
    ind = pd.Series({i: float(np.prod([1.0]) * 0) for i in obs.index})
    # expected #closes with >=2 simultaneous flips under independence (Poisson-binomial)
    ps = F.mean().to_numpy()
    dist = np.array([1.0])
    for p in ps:
        dist = np.convolve(dist, [1 - p, p])
    exp = dist * len(F)
    print("\n#coins flipping in the same hour:  observed vs independent")
    print(pd.DataFrame({"k": range(len(exp)), "observed": [int(obs.get(i, 0)) for i in range(len(exp))],
                        "expected_indep": exp.round(2)}).to_string(index=False))
    _ = ind

    hdr("8b. DEGENERATE SIGNALS — closes where the underlying did not move at all "
        "(S_t == S_open at tick resolution) so fair = 0.50 and the model knows nothing")
    rows = []
    for c in coins:
        tr, _ = trades[c]
        if not len(tr):
            rows.append(dict(coin=c, signals=0)); continue
        deg = tr[np.abs(tr.ret_bp) < 1e-9]
        near = tr[np.abs(tr.z) < 2.0]
        fl = tr[tr.outcome == "filled"]
        fld = fl[np.abs(fl.z) < 2.0]
        rows.append(dict(coin=c, signals=len(tr), zero_move_signals=len(deg),
                         absz_lt2_signals=len(near),
                         absz_lt2_fills=len(fld),
                         absz_lt2_win=fld.won.mean() if len(fld) else np.nan,
                         absz_lt2_ev=fld.pnl_per_share.mean() if len(fld) else np.nan,
                         absz_ge2_fills=len(fl) - len(fld),
                         absz_ge2_win=fl[np.abs(fl.z) >= 2].won.mean()
                         if (len(fl) - len(fld)) else np.nan,
                         absz_ge2_ev=fl[np.abs(fl.z) >= 2].pnl_per_share.mean()
                         if (len(fl) - len(fld)) else np.nan))
    show(pd.DataFrame(rows), "t8b_degenerate")

    hdr("9. STATISTICAL STRENGTH — day-block bootstrap CI on EV/share and $/day, "
        "and the pooled non-BTC test")
    rng = np.random.default_rng(20260727)
    rows = []
    groups = {c: [c] for c in coins}
    groups["POOLED non-BTC"] = [c for c in coins if c != "bitcoin"]
    groups["POOLED ETH+SOL+XRP"] = ["ethereum", "solana", "xrp"]
    groups["POOLED DOGE+BNB+HYPE"] = ["dogecoin", "bnb", "hype"]
    fl_all = tr_all[tr_all.outcome == "filled"]
    for name, sel in groups.items():
        p = fl_all[fl_all.coin.isin(sel)]
        if not len(p):
            continue
        byday = {d: g for d, g in p.groupby("day")}
        dl = list(all_days)
        evb, pdb = [], []
        for _ in range(4000):
            samp = rng.choice(len(dl), len(dl), replace=True)
            ps_, pn_ = [], 0.0
            for i in samp:
                g = byday.get(dl[i])
                if g is not None:
                    ps_.append(g.pnl_per_share.to_numpy())
                    pn_ += g.pnl.sum()
            if ps_:
                evb.append(np.concatenate(ps_).mean())
            pdb.append(pn_ / len(dl))
        evb = np.array(evb); pdb = np.array(pdb)
        rows.append(dict(group=name, fills=len(p), days=len(all_days),
                         trades_day=len(p) / len(all_days),
                         ev_share=p.pnl_per_share.mean(),
                         ev_boot_lo=np.percentile(evb, 2.5),
                         ev_boot_hi=np.percentile(evb, 97.5),
                         p_ev_le_0=float((evb <= 0).mean()),
                         pnl_day=p.pnl.sum() / len(all_days),
                         pnl_day_lo=np.percentile(pdb, 2.5),
                         pnl_day_hi=np.percentile(pdb, 97.5),
                         p_pnl_le_0=float((pdb <= 0).mean())))
    show(pd.DataFrame(rows), "t9_bootstrap")

    print(f"\nAll CSVs -> {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
