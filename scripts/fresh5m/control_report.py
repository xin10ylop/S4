#!/usr/bin/env python3
"""C2 control-test report: run the staleness / causality / stress sweeps over
the repo's own 43 days of 5m data and write the tables to data/c2/.

  python3 scripts/fresh5m/control_report.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay as R  # noqa: E402

OUT = f"{R.ROOT}/data/c2"
os.makedirs(OUT, exist_ok=True)
pd.set_option("display.width", 260, "display.max_columns", 60)

COLS = ["label", "days", "signals", "fills", "trades_day", "ev_share", "win",
        "t_trade", "t_day", "stale_book_blocked", "book_moved", "empty_book",
        "pnl_day", "stale_fills_removed", "stale_pnl_frac"]

# A3 convention (what the published 43-day numbers were produced under)
A3 = R.Params(edge_min=0.05, tau_grid=[6, 5, 4, 3, 2], sigma_mode="grid_ffill",
              strike_mode="exact_else_ffill", ladder=False, per_event_cap_usd=25.0)
# Shipped-parameter convention as the verifier quoted it (tau in [2,5])
SHIP_V = replace(A3, edge_min=0.03, tau_grid=[5, 4, 3, 2])
# Shipped parameters as the bot ACTUALLY runs them: tau_lo = max(2.5, 1.5+0.5)
SHIP_T = replace(A3, edge_min=0.03, tau_grid=None, latency_ms=1500)


def show(df, name):
    print(df[[c for c in COLS if c in df.columns]].to_string(index=False))
    df.to_csv(f"{OUT}/{name}.csv", index=False)
    print(f"  -> {OUT}/{name}.csv\n")


def main():
    days = R.CONTROL_DAYS

    # ---------------------------------------------------------------- 1+2+4
    # every variant that only changes decision-time behaviour, one tape pass
    sets = {
        "a3_base":        A3,
        "ship_verifier":  SHIP_V,
        "ship_true":      SHIP_T,
        "noncausal":      replace(A3, causal=False),
        "sigma_bot":      replace(A3, sigma_mode="bot"),
        "strike_a1":      replace(A3, strike_mode="backfill"),
        "winner_cl":      replace(A3, winner_source="chainlink"),
        "sigfloor_3e5":   replace(A3, sigma_1s_floor=3.0e-5),
        "fee_010":        replace(A3, fee_rate=0.10),
        "depth_50":       replace(A3, depth_fraction=0.50),
        "lat_3000":       replace(A3, latency_ms=3000),
        "lat_3000_band":  replace(A3, latency_ms=3000, tau_grid=None),
        "stress_all":     replace(A3, fee_rate=0.10, depth_fraction=0.50,
                                  latency_ms=3000, tau_grid=None, max_book_age_s=5.0),
    }
    # pre-decision staleness rejection at each threshold, on both conventions
    for th in (2, 5, 10, 30, 60):
        sets[f"pre_{th}s"] = replace(A3, max_book_age_s=float(th))
        sets[f"ship_pre_{th}s"] = replace(SHIP_V, max_book_age_s=float(th))

    print(f"=== replaying {len(days)} days x {len(sets)} parameter sets ===")
    tr, meta = R.run_days_multi(days, sets, source="repo", verbose=True)
    nd, nw = meta["days"], meta["windows"]
    for k, v in tr.items():
        v.to_parquet(f"{OUT}/trades_{k}.parquet", index=False)

    # ---------------------------------------------------- Table 1: control
    print("\n=== TABLE 1. CONTROL TEST vs docs/07 sec 3.4 ===")
    t1 = pd.DataFrame([
        R.summarise(tr["a3_base"], nd, nw, "A3 baseline em=0.05 tau{6..2}  [A3: 1503 fills, +9.44c, t 8.79]"),
        R.summarise(tr["a3_base"], nd, nw, "  post-hoc drop book age >30s  [TARGET +8.38c t=7.56, 71/1503, 14% PnL]", 30.0),
        R.summarise(tr["ship_verifier"], nd, nw, "shipped em=0.03 tau[2,5]      [TARGET +10.89c t=10.33]"),
        R.summarise(tr["ship_verifier"], nd, nw, "  post-hoc drop book age >30s", 30.0),
        R.summarise(tr["ship_true"], nd, nw, "shipped as the bot RUNS it: tau[2.5,5] -> {5,4,3}"),
        R.summarise(tr["ship_true"], nd, nw, "  post-hoc drop book age >30s", 30.0),
    ])
    show(t1, "table1_control")

    # ------------------------------------------ Table 2: staleness sweep
    print("=== TABLE 2. BOOK-STALENESS SENSITIVITY (A3 convention) ===")
    rows = []
    for th in (2, 5, 10, 30, 60):
        rows.append(R.summarise(tr[f"pre_{th}s"], nd, nw, f"PRE-decision reject age >{th}s"))
    rows.append(R.summarise(tr["a3_base"], nd, nw, "PRE-decision unlimited"))
    for th in (2, 5, 10, 30, 60):
        rows.append(R.summarise(tr["a3_base"], nd, nw, f"POST-hoc  drop fills age >{th}s", float(th)))
    rows.append(R.summarise(tr["a3_base"], nd, nw, "POST-hoc  unlimited"))
    show(pd.DataFrame(rows), "table2_staleness_a3")

    print("=== TABLE 2b. BOOK-STALENESS SENSITIVITY (shipped params em=0.03 tau[2,5]) ===")
    rows = []
    for th in (2, 5, 10, 30, 60):
        rows.append(R.summarise(tr[f"ship_pre_{th}s"], nd, nw, f"PRE-decision reject age >{th}s"))
    rows.append(R.summarise(tr["ship_verifier"], nd, nw, "PRE-decision unlimited"))
    for th in (2, 5, 10, 30, 60):
        rows.append(R.summarise(tr["ship_verifier"], nd, nw, f"POST-hoc  drop fills age >{th}s", float(th)))
    rows.append(R.summarise(tr["ship_verifier"], nd, nw, "POST-hoc  unlimited"))
    show(pd.DataFrame(rows), "table2b_staleness_shipped")

    # ------------------------------------------- Table 3: causality trap
    print("=== TABLE 3. CHAINLINK CAUSALITY ===")
    t3 = pd.DataFrame([
        R.summarise(tr["a3_base"], nd, nw, "CAUSAL   server_timestamp_us <= t  (truth, shipped default)"),
        R.summarise(tr["noncausal"], nd, nw, "NONCAUSAL timestamp_us <= t       (the trap)"),
    ])
    show(t3, "table3_causality")
    print("  audit:", R.agg_causality(meta["causality"]), "\n")

    # ------------------------------- Table 4: convention / stress knobs
    print("=== TABLE 4. CONVENTIONS AND STRESS KNOBS (A3 baseline unless noted) ===")
    t4 = pd.DataFrame([
        R.summarise(tr["a3_base"], nd, nw, "baseline"),
        R.summarise(tr["sigma_bot"], nd, nw, "sigma via oracle.py convention (held prints, no ffill)"),
        R.summarise(tr["strike_a1"], nd, nw, "strike = A1 backfill (first print at-or-after wts)"),
        R.summarise(tr["winner_cl"], nd, nw, "winner recomputed from Chainlink, not result_id"),
        R.summarise(tr["sigfloor_3e5"], nd, nw, "sigma_1s_floor 8e-6 -> 3e-5 (Chainlink-calibrated)"),
        R.summarise(tr["fee_010"], nd, nw, "fee_rate 0.07 -> 0.10"),
        R.summarise(tr["depth_50"], nd, nw, "depth_fraction 50%"),
        R.summarise(tr["lat_3000"], nd, nw, "latency 3.0s, tau grid UNCHANGED (not executable)"),
        R.summarise(tr["lat_3000_band"], nd, nw, "latency 3.0s with the bot's tau_lo=lat+0.5 rule"),
        R.summarise(tr["stress_all"], nd, nw, "ALL: fee .10 + 50% depth + lat 3.0s(band) + age<=5s"),
    ])
    show(t4, "table4_conventions_stress")

    # --------------------------------- Table 5: what the stale fills are
    print("=== TABLE 5. THE STALE FILLS THEMSELVES (A3 baseline, unfiltered) ===")
    f = tr["a3_base"]
    f = f[f.outcome == "filled"].copy()
    f["age"] = f[["book_age_sig", "book_age_fil"]].max(axis=1)
    st = f[f.age > 30].sort_values("age", ascending=False)
    print(f"fills={len(f)}  stale(>30s)={len(st)}  share of P&L={st.pnl.sum()/f.pnl.sum():.4f}")
    print(f"stale win rate={st.won.mean():.3f} vs fresh {f[f.age<=30].won.mean():.3f}; "
          f"stale c/share={st.pnl_per_share.mean()*100:.2f} vs fresh "
          f"{f[f.age<=30].pnl_per_share.mean()*100:.2f}")
    by_day = st.groupby("day").agg(n=("age", "size"), max_age=("age", "max"),
                                   pnl=("pnl", "sum"), win=("won", "mean"))
    print(by_day.sort_values("n", ascending=False).head(15).to_string())
    st.to_csv(f"{OUT}/table5_stale_fills.csv", index=False)
    print(f"  -> {OUT}/table5_stale_fills.csv")
    print("\nage distribution of ALL fills (seconds):")
    print(f.age.describe(percentiles=[.5, .9, .99, .995, .999]).to_string())

    # -------------------------- Table 6: is the staleness a tape artifact?
    print("\n=== TABLE 6. bookcurves vs quotes tape on the same windows ===")
    cmp_rows = []
    for d in sorted(st.day.unique())[:6]:
        bc = R.load_repo_day(d, "bookcurves")
        qt = R.load_repo_day(d, "quotes")
        qmap = {w.wts: w for w in qt}
        sub = st[st.day == d]
        for _, r in sub.iterrows():
            w2 = qmap.get(int(r.wts))
            if w2 is None:
                continue
            t_us = int((r.close_s - r.tau) * 1e6)
            tape = w2.up if r.side == "up" else w2.dn
            _, age_q = tape.at(t_us)
            cmp_rows.append(dict(day=d, wts=int(r.wts), side=r.side,
                                 age_bookcurves=r.book_age_sig, age_quotes=age_q))
        _ = bc
    cq = pd.DataFrame(cmp_rows)
    if len(cq):
        print(cq.to_string(index=False))
        cq.to_csv(f"{OUT}/table6_tape_crosscheck.csv", index=False)
        print(f"\n  median age bookcurves {cq.age_bookcurves.median():.1f}s vs "
              f"quotes {cq.age_quotes.median():.1f}s  -> "
              f"{'REAL vendor silence' if cq.age_quotes.median() > 30 else 'BOOKCURVES SAMPLING ARTIFACT'}")

    print("\ndone.")


if __name__ == "__main__":
    main()
