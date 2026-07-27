#!/usr/bin/env python3
"""C3 — analysis of the decisive fresh-5m run.  Reads data/c3/trades_*.parquet."""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay as R  # noqa: E402

ROOT = "/home/user/S4"
C3 = f"{ROOT}/data/c3"
INF = float("inf")
pd.set_option("display.width", 260, "display.max_columns", 80, "display.max_rows", 200)

per_day = pd.read_parquet(f"{C3}/per_day.parquet")
NDAYS = len(per_day)
NWIN = int(per_day.windows.sum())
ALL_DAYS = sorted(per_day.day.tolist())


def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(f"{C3}/trades_{name}.parquet")


def _t(x) -> float:
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def stats(tr: pd.DataFrame, days: list[str] | None = None, post_age: float = INF,
          label: str = "") -> dict:
    """Per-share EV in cents, day-clustered t, $/day.  `post_age` = post-hoc drop."""
    d = days if days is not None else ALL_DAYS
    nd = len(d)
    tr = tr[tr.day.isin(d)]
    f = tr[tr.outcome == "filled"]
    n_all, pnl_all = len(f), f.pnl.sum()
    removed, frac = 0, np.nan
    if math.isfinite(post_age):
        stale = (f.book_age_sig > post_age) | (f.book_age_fil > post_age)
        removed = int(stale.sum())
        frac = float(f.pnl[stale].sum() / pnl_all) if pnl_all else np.nan
        f = f[~stale]
    ps = f.pnl_per_share.to_numpy(float) * 100.0
    daily = (f.groupby("day").pnl_per_share.mean() * 100.0)
    daily = daily.reindex(d).dropna().to_numpy(float)
    return dict(label=label, days=nd, signals=len(tr), fills=len(f),
                trades_day=len(f) / nd if nd else np.nan,
                ev_c=float(ps.mean()) if len(ps) else np.nan,
                win=float(f.won.mean()) if len(f) else np.nan,
                t_trade=_t(ps), t_day=_t(daily), n_day_clusters=len(daily),
                pnl=float(f.pnl.sum()), pnl_day=float(f.pnl.sum() / nd) if nd else np.nan,
                shares=float(f.shares.sum()),
                stale_removed=removed, stale_pnl_frac=frac,
                stale_blocked=int((tr.outcome == "stale_book").sum()),
                no_fill=int((tr.outcome != "filled").sum()))


COLS = ["label", "days", "signals", "fills", "trades_day", "ev_c", "win",
        "t_trade", "t_day", "pnl", "pnl_day", "stale_removed", "stale_pnl_frac"]


def show(rows, cols=COLS):
    df = pd.DataFrame(rows)
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.4g}"))
    return df


# =========================================================================
print("=" * 110)
print(f"SAMPLE: {NDAYS} days {ALL_DAYS[0]}..{ALL_DAYS[-1]}, {NWIN} windows with book depth")
print("=" * 110)

raw250, raw25 = load("raw_c250"), load("raw_c25")

# ---- 0. prefilter leakage: do control windows produce fills? -------------
f = raw250[raw250.outcome == "filled"]
print(f"\n[0] control-window check: {int(raw250.is_control.sum())} signals and "
      f"{int(f.is_control.sum())} fills came from windows the fetch prefilter REJECTED "
      f"(is_control=True).")

# ---- 1. HEADLINE ---------------------------------------------------------
print("\n" + "=" * 110)
print("[1] HEADLINE — shipped params, full fresh period")
print("=" * 110)
rows = []
for a, nm in [(INF, "unlimited (raw)"), (60, "PRE 60s"), (30, "PRE 30s"), (20, "PRE 20s"),
              (10, "PRE 10s"), (5, "PRE 5s  <-- PRIMARY"), (2, "PRE 2s")]:
    key = "raw_c250" if a is INF else f"pre{a:g}_c250"
    rows.append(stats(load(key), label=nm + "  [$250]"))
show(rows)

print("\nPOST-hoc convention (drop after the fact, docs/07 style), from the raw $250 tape:")
rows = [stats(raw250, post_age=a, label=f"POST {a}s" if math.isfinite(a) else "unlimited")
        for a in (2, 5, 10, 20, 30, 60, INF)]
show(rows)

print("\n$25 clip:")
rows = [stats(raw25, label="unlimited (raw) [$25]"),
        stats(load("pre5_c25"), label="PRE 5s [$25]")]
show(rows)

# ---- 2. CONTAMINATION ----------------------------------------------------
print("\n" + "=" * 110)
print("[2] CONTAMINATION ACCOUNTING")
print("=" * 110)
f = raw250[raw250.outcome == "filled"].copy()
f["age"] = f[["book_age_sig", "book_age_fil"]].max(axis=1)
print(f"raw fills {len(f)}, raw P&L ${f.pnl.sum():.2f}, raw EV {f.pnl_per_share.mean()*100:.4f}c")
print("\nbook-age distribution at the decision instant (max of signal/fill instants), seconds:")
q = f.age.describe(percentiles=[.5, .9, .95, .99]).to_dict()
print({k: round(v, 4) for k, v in q.items()})
for thr in (2, 5, 10, 20, 30, 60, 300):
    s = f[f.age > thr]
    print(f"  age > {thr:4d}s : {len(s):4d} fills ({len(s)/len(f)*100:5.2f}%)  "
          f"P&L ${s.pnl.sum():9.2f} ({s.pnl.sum()/f.pnl.sum()*100:6.2f}% of raw)  "
          f"win {s.won.mean()*100 if len(s) else float('nan'):5.1f}%  "
          f"EV {s.pnl_per_share.mean()*100 if len(s) else float('nan'):+7.2f}c")
stale = f[f.age > 5]
if len(stale):
    print("\nEvery fill with book age > 5s (the excluded set):")
    ss = stale[["day", "close_s", "tau", "side", "book_age_sig", "book_age_fil",
                "avg_price", "won", "pnl_per_share", "pnl"]].copy()
    ss["close_utc"] = pd.to_datetime(ss.close_s, unit="s", utc=True).dt.strftime("%m-%d %H:%M")
    ss["pnl_per_share"] *= 100
    print(ss.sort_values("book_age_sig", ascending=False)
          .to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\nBy UTC hour of close, all raw fills vs stale fills:")
    f["h"] = pd.to_datetime(f.close_s, unit="s", utc=True).dt.hour
    tab = pd.crosstab(f.h, f.age > 5)
    print(tab.to_string())
stale.to_csv(f"{C3}/stale_fills.csv", index=False)

# ---- 3. STRESS -----------------------------------------------------------
print("\n" + "=" * 110)
print("[3] STRESS STACK (all on top of PRE 5s staleness)")
print("=" * 110)
rows = []
for k, nm in [("pre5", "baseline: shipped + age<=5s"), ("fee10", "fee 0.07 -> 0.10"),
              ("lat3000", "latency 1.5s -> 3.0s (tau_lo widened)"),
              ("depth50", "50% of displayed depth"),
              ("stress_all", "ALL THREE TOGETHER")]:
    for cap in (250, 25):
        rows.append(stats(load(f"{k}_c{cap}"), label=f"{nm}  [${cap}]"))
show(rows)

# ---- 4. TIME STRUCTURE ---------------------------------------------------
print("\n" + "=" * 110)
print("[4] TIME STRUCTURE")
print("=" * 110)
p5 = load("pre5_c250")
fp = p5[p5.outcome == "filled"]
byday = (fp.groupby("day")
         .agg(fills=("pnl_per_share", "size"), ev_c=("pnl_per_share", lambda s: s.mean() * 100),
              win=("won", "mean"), pnl=("pnl", "sum")).reindex(ALL_DAYS))
byday["fills"] = byday.fills.fillna(0).astype(int)
print(byday.to_string(float_format=lambda x: f"{x:.3f}"))
byday.to_csv(f"{C3}/by_day.csv")

print("\nBy ISO week:")
fp = fp.copy()
fp["week"] = pd.to_datetime(fp.day).dt.strftime("%G-W%V")
wk = (fp.groupby("week").agg(days=("day", "nunique"), fills=("pnl_per_share", "size"),
                             ev_c=("pnl_per_share", lambda s: s.mean() * 100),
                             win=("won", "mean"), pnl=("pnl", "sum")))
wk["t_trade"] = fp.groupby("week").pnl_per_share.apply(lambda s: _t(s.to_numpy() * 100))
wk["t_day"] = fp.groupby("week").apply(
    lambda g: _t(g.groupby("day").pnl_per_share.mean().to_numpy() * 100))
print(wk.to_string(float_format=lambda x: f"{x:.3f}"))

print("\nPeriod splits (PRE 5s, $250):")
CLEAN_JUNE_EXCL = {"2026-06-10", "2026-06-11"}
periods = {
    "June (all)": [d for d in ALL_DAYS if d < "2026-07-01"],
    "June, ex 06-10/06-11 (bad Chainlink)": [d for d in ALL_DAYS if d < "2026-07-01"
                                             and d not in CLEAN_JUNE_EXCL],
    "pre-break  Jun 1-26": [d for d in ALL_DAYS if d <= "2026-06-26"],
    "post-break Jun 27-Jul 26": [d for d in ALL_DAYS if d >= "2026-06-27"],
    "July (all)": [d for d in ALL_DAYS if d >= "2026-07-01"],
    "target period Jul 8-26 (genuinely new)": [d for d in ALL_DAYS if d >= "2026-07-08"],
    "last 14 days Jul 13-26": [d for d in ALL_DAYS if d >= "2026-07-13"],
    "A3's 5 days Jul 21-25": [d for d in ALL_DAYS if "2026-07-21" <= d <= "2026-07-25"],
}
rows = [stats(p5, days=v, label=k) for k, v in periods.items()]
show(rows)

print("\nSame splits under the FULL STRESS STACK ($250):")
sa = load("stress_all_c250")
show([stats(sa, days=v, label=k) for k, v in periods.items()])

print("\nWelch t-tests on per-fill pnl/share (cents):")
from scipy import stats as sps  # noqa: E402
fl = p5[p5.outcome == "filled"]


def arr(days):
    return fl[fl.day.isin(days)].pnl_per_share.to_numpy(float) * 100


def dayarr(days):
    g = fl[fl.day.isin(days)]
    return g.groupby("day").pnl_per_share.mean().to_numpy(float) * 100


for a, b in [("pre-break  Jun 1-26", "post-break Jun 27-Jul 26"),
             ("June (all)", "July (all)")]:
    x, y = arr(periods[a]), arr(periods[b])
    xd, yd = dayarr(periods[a]), dayarr(periods[b])
    tt = sps.ttest_ind(x, y, equal_var=False)
    td = sps.ttest_ind(xd, yd, equal_var=False)
    print(f"  {a} ({x.mean():+.2f}c, n={len(x)}) vs {b} ({y.mean():+.2f}c, n={len(y)}): "
          f"per-trade Welch p={tt.pvalue:.4f} | day-level Welch p={td.pvalue:.4f}")

# ---- 5. diagnostics ------------------------------------------------------
print("\n" + "=" * 110)
print("[5] DIAGNOSTICS / ROBUSTNESS (all PRE 5s, $250)")
print("=" * 110)
rows = [stats(load("pre5_c250"), label="causal (truth)"),
        stats(load("noncausal_c250"), label="NON-causal (the trap, obs_ts<=t)"),
        stats(load("sigfloor3e5_c250"), label="sigma_1s_floor 8e-6 -> 3e-5"),
        stats(load("sigmabot_c250"), label="sigma via oracle.py convention"),
        stats(load("clwinner_c250"), label="winner from Chainlink not result_id")]
show(rows)

cau = pd.read_parquet(f"{C3}/causality.parquet")
cau["day"] = ALL_DAYS[:len(cau)]
n = cau.n.sum()
print(f"\ncausality audit: {int(n)} decisions, "
      f"{int(cau.future_report_used.sum())} ({cau.future_report_used.sum()/n*100:.2f}%) would use an "
      f"UNPUBLISHED report under obs_ts<=t; n-weighted mean foresight "
      f"{(cau.mean_foresight_s*cau.n).sum()/n:.3f}s, "
      f"median day {cau.mean_foresight_s.median():.3f}s, "
      f"worst day {cau.loc[cau.mean_foresight_s.idxmax(),'day']} "
      f"{cau.mean_foresight_s.max():.1f}s")

# outcome mix
print("\noutcome mix, PRE 5s $250:", p5.outcome.value_counts().to_dict())
print("outcome mix, raw  $250:", raw250.outcome.value_counts().to_dict())
print("\nfill-price / size profile (PRE 5s, $250):")
ff = p5[p5.outcome == "filled"]
print(ff[["avg_price", "shares", "fair", "edge_sig", "sig_ask", "tau"]]
      .describe(percentiles=[.5, .9]).to_string(float_format=lambda x: f"{x:.4f}"))
print("levels used:", ff.n_levels.value_counts().to_dict())
print("side split up/down:", ff.side.value_counts().to_dict())
