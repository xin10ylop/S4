#!/usr/bin/env python3
"""C3 extras: regime comparison vs the historical repo tape, outage episodes,
bootstrap CIs, prefilter-leakage scaling, and the 04h-hour guard variant."""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats as sps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = "/home/user/S4"
C3 = f"{ROOT}/data/c3"
pd.set_option("display.width", 250, "display.max_columns", 60, "display.max_rows", 250)
rng = np.random.default_rng(20260727)


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def blk(tr, days, label):
    f = tr[(tr.outcome == "filled") & (tr.day.isin(days))]
    ps = f.pnl_per_share.to_numpy(float) * 100
    dm = f.groupby("day").pnl_per_share.mean().to_numpy(float) * 100
    return dict(label=label, days=len(days), fills=len(f),
                trades_day=len(f) / len(days), ev_c=ps.mean() if len(ps) else np.nan,
                win=f.won.mean() if len(f) else np.nan,
                t_trade=_t(ps), t_day=_t(dm),
                pnl=f.pnl.sum(), pnl_day=f.pnl.sum() / len(days))


fresh = pd.read_parquet(f"{C3}/trades_pre5_c250.parquet")
repo = pd.read_parquet(f"{C3}/repo43_ship_pre5.parquet")
per_day = pd.read_parquet(f"{C3}/per_day.parquet")
FD = sorted(per_day.day.tolist())
RD = sorted(repo.day.unique().tolist())

print("=" * 118)
print("[A] REGIME LADDER — identical code, identical shipped params, identical PRE 5s staleness filter")
print("=" * 118)
regimes = [
    ("HIST Apr 2-15   (repo)", repo, [d for d in RD if d <= "2026-04-15"]),
    ("HIST Apr 16-30  (repo)", repo, [d for d in RD if "2026-04-16" <= d <= "2026-04-30"]),
    ("HIST May 1-12   (repo)", repo, [d for d in RD if d.startswith("2026-05")]),
    ("HIST Jul 6-7    (repo)", repo, [d for d in RD if d.startswith("2026-07")]),
    ("FRESH Jun 1-12", fresh, [d for d in FD if d <= "2026-06-12"]),
    ("FRESH Jun 13-26", fresh, [d for d in FD if "2026-06-13" <= d <= "2026-06-26"]),
    ("FRESH Jun 27-Jul 7", fresh, [d for d in FD if "2026-06-27" <= d <= "2026-07-07"]),
    ("FRESH Jul 8-26", fresh, [d for d in FD if d >= "2026-07-08"]),
]
rows = [blk(t, d, n) for n, t, d in regimes]
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.4g}"))

print("\nWelch tests, day-level means (the pre-committed clustering):")


def dm(tr, days):
    f = tr[(tr.outcome == "filled") & (tr.day.isin(days))]
    return f.groupby("day").pnl_per_share.mean().to_numpy(float) * 100


pairs = [("HIST Apr16-May12", repo, [d for d in RD if "2026-04-16" <= d <= "2026-05-12"],
          "FRESH all 56d", fresh, FD),
         ("HIST all 43d", repo, RD, "FRESH all 56d", fresh, FD),
         ("HIST all 43d", repo, RD, "FRESH Jul 8-26", fresh, [d for d in FD if d >= "2026-07-08"]),
         ("FRESH Jun 1-26", fresh, [d for d in FD if d <= "2026-06-26"],
          "FRESH Jun27-Jul26", fresh, [d for d in FD if d >= "2026-06-27"])]
for na, ta, da, nb, tb, db in pairs:
    x, y = dm(ta, da), dm(tb, db)
    r = sps.ttest_ind(x, y, equal_var=False)
    print(f"  {na} ({x.mean():+6.2f}c/day, n={len(x)}d) vs {nb} ({y.mean():+6.2f}c/day, n={len(y)}d)"
          f"  Welch t={r.statistic:+.2f} p={r.pvalue:.4f}")

# --------------------------------------------------------------------------
print("\n" + "=" * 118)
print("[B] OUTAGE EPISODES THAT PRODUCED FILLS (raw tape, no staleness filter)")
print("=" * 118)
raw = pd.read_parquet(f"{C3}/trades_raw_c250.parquet")
f = raw[raw.outcome == "filled"].copy()
f["age"] = f[["book_age_sig", "book_age_fil"]].max(axis=1)
f["close_dt"] = pd.to_datetime(f.close_s, unit="s", utc=True)
st = f[f.age > 20].sort_values("close_s")
# cluster consecutive 5m closes
st["gap"] = st.close_s.diff().fillna(1e9)
st["ep"] = (st.gap > 900).cumsum()
ep = st.groupby("ep").agg(day=("day", "first"),
                          first_close=("close_dt", "min"), last_close=("close_dt", "max"),
                          n_fills=("pnl", "size"), max_age=("age", "max"),
                          wins=("won", "sum"), pnl=("pnl", "sum"),
                          ev_c=("pnl_per_share", lambda s: s.mean() * 100))
ep["first_close"] = ep.first_close.dt.strftime("%Y-%m-%d %H:%M")
ep["last_close"] = ep.last_close.dt.strftime("%H:%M")
print(ep.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
print(f"\ntotal: {len(st)} fills, {int(st.won.sum())} wins ({st.won.mean()*100:.1f}%), "
      f"${st.pnl.sum():.0f} = {st.pnl.sum()/f.pnl.sum()*100:.1f}% of raw P&L, "
      f"EV {st.pnl_per_share.mean()*100:+.2f}c/share")
h4 = st[st.close_dt.dt.hour == 4]
print(f"of which closing in the 04:00-04:59 UTC hour: {len(h4)} fills, ${h4.pnl.sum():.0f} "
      f"({h4.pnl.sum()/f.pnl.sum()*100:.1f}% of raw P&L)")

# --------------------------------------------------------------------------
print("\n" + "=" * 118)
print("[C] BOOTSTRAP / PERMUTATION on the primary result (PRE 5s, $250, 56 days)")
print("=" * 118)
fl = fresh[fresh.outcome == "filled"]
daily = fl.groupby("day").pnl_per_share.mean() * 100
daily = daily.reindex(FD).fillna(0.0).to_numpy()      # a day with no fill contributes 0
dnz = fl.groupby("day").pnl_per_share.mean().to_numpy() * 100
print(f"day means: n={len(dnz)} days with fills, mean {dnz.mean():+.3f}c, sd {dnz.std(ddof=1):.3f}, "
      f"t_day {_t(dnz):.3f}, two-sided p={sps.ttest_1samp(dnz,0).pvalue:.4f}")
bs = np.array([rng.choice(dnz, len(dnz), replace=True).mean() for _ in range(20000)])
print(f"day-bootstrap 95% CI on EV/share: [{np.percentile(bs,2.5):+.2f}, {np.percentile(bs,97.5):+.2f}] c "
      f"; P(EV<=0) = {(bs<=0).mean():.4f}; P(EV<3.0c) = {(bs<3.0).mean():.4f}")
# block bootstrap over whole days of the trade tape (weights by fills too)
gp = [g.pnl_per_share.to_numpy() * 100 for _, g in fl.groupby("day")]
bs2 = np.array([np.concatenate([gp[i] for i in rng.integers(0, len(gp), len(gp))]).mean()
                for _ in range(5000)])
print(f"day-block bootstrap of pooled EV: [{np.percentile(bs2,2.5):+.2f}, {np.percentile(bs2,97.5):+.2f}] c"
      f" ; P(EV<=0)={(bs2<=0).mean():.4f} ; P(EV<3.0c)={(bs2<3.0).mean():.4f}")

# --------------------------------------------------------------------------
print("\n" + "=" * 118)
print("[D] PREFILTER LEAKAGE — what the fetch's candidate prescreen threw away")
print("=" * 118)
cand = pd.read_parquet(f"{ROOT}/data/fresh5m/candidates.parquet", columns=["close_s", "is_control"])
n_ctrl = int(cand.groupby("close_s").is_control.all().sum())
mk = pd.read_parquet(f"{ROOT}/data/fresh5m/markets.parquet", columns=["close_s", "d"])
mk = mk[mk.d.isin(FD)]
n_all = len(mk)
n_cand = int(cand[~cand.is_control].close_s.nunique())
n_rej = n_all - n_cand
ctrl_sig = int(raw.is_control.sum())
ctrl_fill = int(raw[(raw.outcome == "filled") & raw.is_control].sum().iloc[0] > 0) if False else \
    int(((raw.outcome == "filled") & raw.is_control).sum())
rate = ctrl_fill / n_ctrl if n_ctrl else 0.0
print(f"markets in period {n_all}; prescreen kept {n_cand} as candidates, rejected {n_rej}; "
      f"{n_ctrl} rejected windows were re-fetched as CONTROLS.")
print(f"controls produced {ctrl_sig} signals and {ctrl_fill} fills -> fill rate "
      f"{rate*100:.3f}% of rejected windows.")
print(f"=> scaled to all {n_rej} rejected windows: ~{rate*n_rej:.0f} missed fills over {len(FD)} days "
      f"= {rate*n_rej/len(FD):.2f}/day on top of the measured "
      f"{len(fl)/len(FD):.2f}/day ({rate*n_rej/max(len(fl),1)*100:.1f}% of the fill count).")

# --------------------------------------------------------------------------
print("\n" + "=" * 118)
print("[E] OPERATIONAL VARIANT — hard-skip the 04:00-04:59 UTC rollover hour (C1 §6 recommendation)")
print("=" * 118)
rows = []
for nm, tr in (("PRE 5s", fresh), ("raw (no staleness filter)", raw),
               ("full stress stack", pd.read_parquet(f"{C3}/trades_stress_all_c250.parquet"))):
    t = tr.copy()
    t["h"] = pd.to_datetime(t.close_s, unit="s", utc=True).dt.hour
    rows.append(blk(t, FD, f"{nm}: all hours"))
    rows.append(blk(t[t.h != 4], FD, f"{nm}: 04h hour skipped"))
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.4g}"))

# --------------------------------------------------------------------------
print("\n" + "=" * 118)
print("[F] SIGNAL ATTRITION — the two-book execution tax")
print("=" * 118)
for nm, tr in (("raw", raw), ("PRE 5s", fresh)):
    vc = tr.outcome.value_counts()
    tot = len(tr)
    print(f"  {nm:8s}: {tot} signals -> filled {vc.get('filled',0)} "
          f"({vc.get('filled',0)/tot*100:.1f}%), book_moved_no_edge {vc.get('book_moved_no_edge',0)} "
          f"({vc.get('book_moved_no_edge',0)/tot*100:.1f}%), stale_book {vc.get('stale_book',0)}, "
          f"empty_book {vc.get('empty_book',0)}")

print("\nmonthly recap of the primary run:")
fl2 = fresh[fresh.outcome == "filled"].copy()
fl2["m"] = fl2.day.str[:7]
print(fl2.groupby("m").agg(fills=("pnl", "size"), ev_c=("pnl_per_share", lambda s: s.mean() * 100),
                           win=("won", "mean"), pnl=("pnl", "sum"))
      .to_string(float_format=lambda x: f"{x:.3f}"))
