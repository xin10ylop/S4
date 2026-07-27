#!/usr/bin/env python3
"""A3 Part 1 — re-run 5m close_snipe on the REPO's own data with CURRENT bot params."""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from replay5m import (CLFeed, load_cl, fair_up, fee, P, ROOT, PRICE_MIN, PRICE_MAX,
                      LATENCY_S, CAP_USD, MAX_WALK)

BC_COLS = ["timestamp_us", "bid_p0", "bid_s0", "ask_p0", "ask_s0", "wts",
           "buy_avgpx_50", "buy_shares_50", "buy_exhaust_50",
           "buy_avgpx_200", "buy_shares_200", "buy_exhaust_200",
           "buy_avgpx_1000", "buy_shares_1000", "buy_exhaust_1000",
           "buy_avgpx_5000", "buy_shares_5000", "buy_exhaust_5000",
           "ask_depth_5c"]


def run_day(day, taus, edge_mins, cl):
    f = f"{P}/daily/5m/bookcurves/{day}.parquet"
    if not os.path.exists(f):
        return None
    b = pd.read_parquet(f, columns=BC_COLS).dropna(subset=["wts"])
    b = b.sort_values("timestamp_us")
    w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    w = w[(w.family == "5m") & (w.date == day)]
    if w.empty:
        return None
    wins = w[["wts", "duration", "result_id"]].drop_duplicates("wts").sort_values("wts")

    rows = []
    grp = {k: v for k, v in b.groupby("wts", sort=False)}
    for _, wr in wins.iterrows():
        wts, dur, rid = int(wr.wts), int(wr.duration), int(wr.result_id)
        close_s = wts + dur
        g = grp.get(wts)
        if g is None or g.empty:
            continue
        ts = g.timestamp_us.to_numpy(np.int64)

        S_open = cl.at_obs(np.array([wts]))[0]
        if not np.isfinite(S_open):
            S_open = cl.at_obs_or_before(np.array([wts]))[0]
        if not np.isfinite(S_open):
            continue

        t_arr = np.array([close_s - k for k in taus], np.int64)
        t_us = t_arr * 1_000_000
        px, obs_us, pub_us = cl.latest_at(t_us)
        tau_eff = (close_s * 1_000_000 - obs_us) / 1e6
        sig = cl.sigma_at_obs(obs_us)
        fu = fair_up(px, S_open, sig, tau_eff)
        # --- counterfactuals for the decomposition -------------------------
        # (a) NO publication lag: pretend the print stamped t is already ours
        px_nl = cl.at_obs_or_before(t_arr)
        sig_nl = cl.sigma_at_obs(t_arr * 1_000_000)
        fu_nolag = fair_up(px_nl, S_open, sig_nl, np.full(len(t_arr), float(0)) + (close_s - t_arr))
        # (b) OLD params: no fair_cap, no sigma floor (raw Phi)
        from scipy.stats import norm as _n
        with np.errstate(divide="ignore", invalid="ignore"):
            z_raw = np.log(px / S_open) / (sig * np.sqrt(tau_eff))
        fu_raw = _n.cdf(z_raw)

        # books at signal time t and at fill time t+latency
        i_sig = np.searchsorted(ts, t_us, side="right") - 1
        i_fil = np.searchsorted(ts, t_us + int(LATENCY_S * 1e6), side="right") - 1
        for n, k in enumerate(taus):
            if not np.isfinite(fu[n]) or i_sig[n] < 0 or i_fil[n] < 0:
                continue
            rs, rf = g.iloc[i_sig[n]], g.iloc[i_fil[n]]
            # staleness guard: book snapshot must be from this window's life
            rows.append(dict(
                day=day, wts=wts, close_s=close_s, rid=rid, tau=k,
                S_open=S_open, S_t=px[n], obs_lag=(t_us[n] - obs_us[n]) / 1e6,
                pub_lag=(obs_us[n] and (pub_us[n] - obs_us[n]) / 1e6),
                tau_eff=tau_eff[n], sigma=sig[n], fair_up=fu[n],
                fair_up_nolag=fu_nolag[n], fair_up_raw=fu_raw[n],
                sig_ask_up=rs.ask_p0, sig_bid_up=rs.bid_p0,
                fil_ask_up=rf.ask_p0, fil_asz_up=rf.ask_s0,
                fil_bid_up=rf.bid_p0, fil_bsz_up=rf.bid_s0,
                fil_avg50=rf.buy_avgpx_50, fil_sh50=rf.buy_shares_50, fil_ex50=rf.buy_exhaust_50,
                fil_avg200=rf.buy_avgpx_200, fil_sh200=rf.buy_shares_200, fil_ex200=rf.buy_exhaust_200,
                fil_avg1000=rf.buy_avgpx_1000, fil_sh1000=rf.buy_shares_1000, fil_ex1000=rf.buy_exhaust_1000,
                fil_avg5000=rf.buy_avgpx_5000, fil_sh5000=rf.buy_shares_5000, fil_ex5000=rf.buy_exhaust_5000,
                fil_askdepth5c=rf.ask_depth_5c,
                book_age_sig=(t_us[n] - ts[i_sig[n]]) / 1e6,
                book_age_fil=(t_us[n] + LATENCY_S * 1e6 - ts[i_fil[n]]) / 1e6,
            ))
    return pd.DataFrame(rows)


def main():
    days = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    tag = sys.argv[2] if len(sys.argv) > 2 else "run"
    taus = [6, 5, 4, 3, 2]
    out = []
    prev = None
    for d in days:
        # CL feed needs the previous day for the 120s vol history at midnight
        pd_ = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_cl([pd_, d], src="repo")
        if cl is None:
            print("no CL", d)
            continue
        r = run_day(d, taus, None, cl)
        if r is not None and len(r):
            out.append(r)
            print(d, len(r), "evals", r.wts.nunique(), "windows", flush=True)
    if not out:
        print("nothing")
        return
    df = pd.concat(out, ignore_index=True)
    df.to_parquet(f"{ROOT}/data/a3/evals_{tag}.parquet", index=False)
    print("total evals", len(df), "windows", df.groupby(['day', 'wts']).ngroups)


if __name__ == "__main__":
    main()
