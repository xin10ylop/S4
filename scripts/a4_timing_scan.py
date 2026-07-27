#!/usr/bin/env python3
"""A4 — snipe-timing forensics on the repo's own 1h vault.

Answers the question docs/06_live_audit.md sec.5 raised but did not test with the
bot's real fill mechanics:

  "The bot fires on the FIRST qualifying second in a 6s window. Is firing early
   strictly worse, or does waiting risk losing the trade entirely?"

Method — a line-for-line reimplementation of the shipped decision path:

  signal at t = close - k   (k = tau, whole seconds)
      S_t      = Binance 1s kline OPEN at t          (oracle.price_at_second)
      S_open   = Binance 1H candle OPEN at wts       (oracle.hour_open_close)
      sigma_1s = std(1s log-returns, trailing 120s), FLOORED at sigma_1s_floor
      fair_up  = Phi(ln(S_t/S_open) / (sigma_1s*sqrt(tau))), clipped to fair_cap
      side order up-then-down, first side with
          price_min < ask < price_max  and  fair - ask - fee(ask) > edge_min

  fill  at t + latency_ms/1000 — the book is RE-READ at that instant and walked
        with `fair` FROZEN at its signal value (engine.py closes over _fair).

Book source: data/data/processed/daily/1h/quotes (top-of-book tape for the Up
token). The Down side is priced complementarily, ask_down = 1 - bid_up with
size = bid_size_up — the same convention scripts/backtest_1h.py uses.

Outputs the tables used by audit/A4_change_spec.md.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data/data/processed"
SEC = 1_000_000


def fee_per_share(p, fee_rate):
    return fee_rate * p * (1.0 - p)


def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ------------------------------------------------------------------ klines
class Klines:
    def __init__(self, days):
        frames = []
        for d in days:
            f = P / f"binance/klines_1s/{d}.parquet"
            if f.exists():
                frames.append(pd.read_parquet(f, columns=["open_time", "open", "close"]))
        k = pd.concat(frames, ignore_index=True)
        k["sec"] = (k.open_time // SEC).astype("int64")
        k = k.drop_duplicates("sec").sort_values("sec")
        self.lo, self.hi = int(k.sec.iloc[0]), int(k.sec.iloc[-1])
        n = self.hi - self.lo + 1
        self._open = np.full(n, np.nan)
        self._close = np.full(n, np.nan)
        idx = k.sec.values - self.lo
        self._open[idx] = k["open"].values
        self._close[idx] = k["close"].values
        mask = np.isnan(self._close)
        if mask.any():
            good = np.where(~mask)[0]
            self._close[mask] = np.interp(np.where(mask)[0], good, self._close[good])
        m = np.isnan(self._open)
        self._open[m] = self._close[m]
        self._logp = np.log(self._open)
        self._ret = np.diff(self._logp, prepend=np.nan)

    def open_at(self, s):
        i = s - self.lo
        if i < 0 or i >= len(self._open):
            return float("nan")
        return float(self._open[i])

    def sigma_1s(self, t, window_secs):
        j = t - self.lo + 1
        i = j - window_secs
        if i < 1 or j > len(self._ret):
            return float("nan")
        seg = self._ret[i:j]
        seg = seg[np.isfinite(seg)]
        if len(seg) < 2:
            return float("nan")
        return float(np.std(seg, ddof=1))

    def hour_open(self, wts):
        return self.open_at(wts)


# ---------------------------------------------------------------- book tape
class Tape:
    """Top-of-book as-of lookups at microsecond resolution for one window."""

    def __init__(self, ts, ask_p, ask_s, bid_p, bid_s):
        self.ts, self.ask_p, self.ask_s, self.bid_p, self.bid_s = ts, ask_p, ask_s, bid_p, bid_s

    def as_of(self, ts_us, side):
        i = np.searchsorted(self.ts, ts_us, side="right") - 1
        if i < 0:
            return None  # no_book
        if side == "up":
            return float(self.ask_p[i]), float(self.ask_s[i])
        b, s = float(self.bid_p[i]), float(self.bid_s[i])
        if not np.isfinite(b):
            return float("nan"), float("nan")
        return 1.0 - b, s


def evaluate_second(kl, t, close_ts, s_open, tape, cfg):
    """== strategy.evaluate_close_snipe for one decision tick."""
    tau = close_ts - t
    if tau <= 0:
        return None
    s_t = kl.open_at(t)
    sg = kl.sigma_1s(t, cfg["vol_window_secs"])
    if not np.isfinite(sg) or sg <= 0 or not np.isfinite(s_t) or s_t <= 0 or not (s_open > 0):
        return None
    sg = max(sg, cfg["sigma_1s_floor"])
    sigma = sg * math.sqrt(tau)
    z = math.log(s_t / s_open) / sigma
    fair_up = normal_cdf(z)
    cap = cfg["fair_cap"]
    fair_up = min(max(fair_up, 1.0 - cap), cap)
    for side, fair in (("up", fair_up), ("down", 1.0 - fair_up)):
        b = tape.as_of(int(t) * SEC, side)
        if b is None:
            continue
        ask, sz = b
        if not np.isfinite(ask):
            continue
        if not (cfg["price_min"] < ask < cfg["price_max"]):
            continue
        edge = fair - ask - fee_per_share(ask, cfg["fee_rate"])
        if edge > cfg["edge_min"]:
            return dict(side=side, fair=fair, ask=ask, ask_size=sz, edge=edge, tau=tau, t=t, z=z)
    return None


def fill_signal(sig, tape, cfg, cap_usd=None, latency_ms=None):
    """== fill_engine.execute_taker_signal against the RE-READ tape (1 level)."""
    cap_usd = cfg["cap_usd"] if cap_usd is None else cap_usd
    lat = cfg["latency_ms"] if latency_ms is None else latency_ms
    fill_ts = int(round((sig["t"] + lat / 1000.0) * SEC))
    b = tape.as_of(fill_ts, sig["side"])
    if b is None:
        return dict(outcome="no_book", shares=0.0, cost=0.0, fees=0.0, ask_fill=np.nan)
    ask, sz = b
    if not np.isfinite(ask):
        return dict(outcome="empty_book", shares=0.0, cost=0.0, fees=0.0, ask_fill=np.nan)
    if not (cfg["price_min"] < ask < cfg["price_max"]):
        return dict(outcome="book_moved_no_edge", shares=0.0, cost=0.0, fees=0.0, ask_fill=ask)
    if sig["fair"] - ask - fee_per_share(ask, cfg["fee_rate"]) <= cfg["edge_min"]:
        return dict(outcome="book_moved_no_edge", shares=0.0, cost=0.0, fees=0.0, ask_fill=ask)
    sz = sz if np.isfinite(sz) else 0.0
    shares = min(sz, cap_usd / ask)
    if shares <= 1e-9:
        return dict(outcome="book_moved_no_edge", shares=0.0, cost=0.0, fees=0.0, ask_fill=ask)
    return dict(outcome="filled", shares=shares, cost=shares * ask,
                fees=fee_per_share(ask, cfg["fee_rate"]) * shares, ask_fill=ask)


# ------------------------------------------------------------------- driver
def load_windows(d0, d1):
    w = pd.read_parquet(ROOT / "data/windows_all.parquet")
    w = w[(w.family == "1h") & (w.date >= d0) & (w.date <= d1)].copy()
    w["close_ts"] = w.wts + w.duration
    return w.sort_values("wts")


def load_tapes(days, wanted_closes, pre=140, post=12):
    """Return {wts: Tape} built from near-close quote rows across all day files."""
    keep = []
    for d in days:
        f = P / f"daily/1h/quotes/{d}.parquet"
        if not f.exists():
            continue
        q = pd.read_parquet(f, columns=["timestamp_us", "bid_price", "bid_size",
                                        "ask_price", "ask_size", "wts"])
        q = q.dropna(subset=["wts"])
        q["wts"] = q.wts.astype("int64")
        q = q[q.wts.isin(wanted_closes)]
        if q.empty:
            continue
        rel = q.timestamp_us // SEC - (q.wts + 3600)
        q = q[(rel >= -pre) & (rel <= post)]
        if not q.empty:
            keep.append(q)
    if not keep:
        return {}
    allq = pd.concat(keep, ignore_index=True).sort_values(["wts", "timestamp_us"],
                                                          kind="mergesort")
    out = {}
    for wts, g in allq.groupby("wts", sort=False):
        out[int(wts)] = Tape(g.timestamp_us.values.astype("int64"),
                             g.ask_price.values.astype("float64"),
                             g.ask_size.values.astype("float64"),
                             g.bid_price.values.astype("float64"),
                             g.bid_size.values.astype("float64"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2026-05-01")
    ap.add_argument("--to-date", default="2026-07-12")
    ap.add_argument("--edge-min", type=float, default=0.05)
    ap.add_argument("--cap-usd", type=float, default=25.0)
    ap.add_argument("--latency-ms", type=int, default=1500)
    ap.add_argument("--max-tau", type=int, default=10)
    ap.add_argument("--out", default=str(ROOT / "audit/A4_tau_grid.csv"))
    args = ap.parse_args()

    import yaml
    with open(ROOT / "bot/config.yaml") as fh:
        y = yaml.safe_load(fh)
    sc = y["strategy"]["close_snipe"]
    cfg = dict(vol_window_secs=int(sc["vol_window_secs"]),
               sigma_1s_floor=float(sc["sigma_1s_floor"]),
               fair_cap=float(sc["fair_cap"]),
               price_min=float(sc["price_min"]), price_max=float(sc["price_max"]),
               edge_min=float(args.edge_min), fee_rate=float(y["fees"]["fee_rate"]),
               cap_usd=float(args.cap_usd), latency_ms=int(args.latency_ms))

    w = load_windows(args.from_date, args.to_date)
    days = sorted({d for d in pd.date_range(pd.Timestamp(args.from_date) - pd.Timedelta(days=1),
                                            pd.Timestamp(args.to_date) + pd.Timedelta(days=1))
                   .strftime("%Y-%m-%d")})
    kl = Klines(days)
    tapes = load_tapes(days, set(w.wts.astype("int64")))
    print(f"windows={len(w)}  with-tape={len(tapes)}  edge_min={cfg['edge_min']} "
          f"lat={cfg['latency_ms']}ms cap=${cfg['cap_usd']}")

    taus = list(range(1, args.max_tau + 1))
    rows = []
    n_eval = 0
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
            n_eval += 1
            s = evaluate_second(kl, close_ts - k, close_ts, s_open, tape, cfg)
            if s is None:
                rows.append(dict(wts=wts, tau=k, signal=False))
                continue
            f = fill_signal(s, tape, cfg)
            won = won_up if s["side"] == "up" else (not won_up)
            pnl = (1.0 if won else 0.0) * f["shares"] - f["cost"] - f["fees"]
            rows.append(dict(wts=wts, tau=k, signal=True, side=s["side"], fair=s["fair"],
                             ask=s["ask"], ask_size=s["ask_size"], edge=s["edge"],
                             outcome=f["outcome"], ask_fill=f["ask_fill"],
                             shares=f["shares"], cost=f["cost"], fees=f["fees"],
                             won=won, pnl=pnl))
    g = pd.DataFrame(rows)
    g["signal"] = g.signal.fillna(False)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    g.to_csv(args.out, index=False)
    n_windows = g.wts.nunique()
    print(f"closes evaluated={n_windows}  eval ticks={n_eval}")

    # ---------------------------------------------------------- fixed-tau grid
    print("\n=== A) FIXED-tau counterfactual (fire at exactly tau=k) ===")
    out = []
    for k, sub in g.groupby("tau"):
        sig = sub[sub.signal]
        fl = sig[sig.outcome == "filled"]
        oc = sig.outcome.value_counts() if len(sig) else {}
        out.append(dict(tau=k, signals=len(sig),
                        fills=len(fl), fill_rate=(len(fl) / len(sig) if len(sig) else np.nan),
                        empty=int(oc.get("empty_book", 0)) + int(oc.get("no_book", 0)),
                        moved=int(oc.get("book_moved_no_edge", 0)),
                        shares=fl.shares.sum(), notional=fl.cost.sum(), pnl=fl.pnl.sum(),
                        c_per_share=(100 * fl.pnl.sum() / fl.shares.sum()
                                     if fl.shares.sum() > 0 else np.nan),
                        win=(100 * fl.won.mean() if len(fl) else np.nan)))
    fixed = pd.DataFrame(out)
    print(fixed.to_string(index=False, float_format=lambda v: f"{v:8.2f}"))

    # ------------------------------------------- survival: edge@6 -> edge@k
    print("\n=== B) SURVIVAL of the opportunity (the key number) ===")
    piv = g.pivot_table(index="wts", columns="tau", values="signal", aggfunc="first").fillna(False)
    for anchor in (6, 5, 4):
        if anchor not in piv.columns:
            continue
        base = piv[piv[anchor]]
        print(f"\n  conditional on a SIGNAL at tau={anchor}  (n={len(base)} closes)")
        line = []
        for k in sorted([c for c in piv.columns if c <= anchor]):
            line.append((k, int(base[k].sum()), 100 * base[k].mean()))
        print("    tau: " + "  ".join(f"{k}:{n}({p:.0f}%)" for k, n, p in line))
        # any signal at tau<=3 / <=2
        for lim in (3, 2, 1):
            cols = [c for c in piv.columns if c <= lim]
            anyc = base[cols].any(axis=1)
            print(f"    still ANY signal at tau<={lim}: {int(anyc.sum())}/{len(base)} "
                  f"({100*anyc.mean():.0f}%)")

    # paired PnL: fire@6 vs wait-and-fire@k (same closes)
    print("\n  PAIRED PnL on the closes that signalled at tau=6:")
    sig6 = g[(g.tau == 6) & (g.signal)]
    base_w = set(sig6.wts)
    prow = []
    for k in sorted(g.tau.unique()):
        sub = g[(g.tau == k) & (g.wts.isin(base_w))]
        s = sub[sub.signal]
        fl = s[s.outcome == "filled"]
        prow.append(dict(policy=f"wait->fire@tau={k}", n_closes=len(base_w), signals=len(s),
                         fills=len(fl), shares=fl.shares.sum(), pnl=fl.pnl.sum(),
                         c_per_share=(100 * fl.pnl.sum() / fl.shares.sum()
                                      if fl.shares.sum() > 0 else np.nan)))
    print(pd.DataFrame(prow).to_string(index=False, float_format=lambda v: f"{v:8.2f}"))

    # -------------------------------------------------- window policies
    print("\n=== C) WINDOW policies: first qualifying second in [lo,hi] ===")
    res = []
    windows = [(1, 6), (1, 10), (1, 3), (2, 4), (1, 2), (2, 3), (3, 5), (2, 6), (3, 6), (4, 6),
               (2, 5), (1, 4), (1, 5), (3, 4), (2, 2), (3, 3), (4, 4)]
    per_close = {int(k): v for k, v in g.groupby("wts")}
    for lo, hi in windows:
        tot_pnl = tot_sh = tot_cost = 0.0
        nsig = nfill = nwin = 0
        pershare = []
        for wts, sub in per_close.items():
            s = sub[(sub.tau >= lo) & (sub.tau <= hi) & (sub.signal)]
            if s.empty:
                continue
            r = s.sort_values("tau", ascending=False).iloc[0]  # first == largest tau
            nsig += 1
            if r.outcome == "filled":
                nfill += 1
                nwin += int(r.won)
                tot_pnl += r.pnl
                tot_sh += r.shares
                tot_cost += r.cost
                pershare.append(r.pnl / r.shares)
        t = (np.mean(pershare) / (np.std(pershare, ddof=1) / np.sqrt(len(pershare)))
             if len(pershare) > 1 else np.nan)
        res.append(dict(window=f"[{lo},{hi}]", signals=nsig, fills=nfill,
                        win=(100 * nwin / nfill if nfill else np.nan),
                        shares=tot_sh, notional=tot_cost, pnl=tot_pnl,
                        c_per_share=(100 * tot_pnl / tot_sh if tot_sh > 0 else np.nan),
                        t_stat=t, pnl_per_day=tot_pnl / (n_windows / 24.0)))
    print(pd.DataFrame(res).to_string(index=False, float_format=lambda v: f"{v:8.2f}"))
    return g


if __name__ == "__main__":
    main()
