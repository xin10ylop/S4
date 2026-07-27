#!/usr/bin/env python3
"""A2 — shadow-replay of the live paper-trading period (2026-07-16 .. 2026-07-25).

Replays `close_snipe` on the 1h family against the Telonex quote tape and exact
Binance 1s klines, reproducing bot/polybot/{strategy,fill_engine}.py EXACTLY:

  S_open   = Binance 1H candle OPEN at window start (oracle.hour_open_close)
  for each whole second t in the last `snipe_last_secs` before close (tau>0):
      sigma_1s = std(1s log-returns, trailing vol_window_secs), FLOORED at
                 sigma_1s_floor
      sigma    = sigma_1s * sqrt(tau)
      z        = ln(S_t / S_open) / sigma
      fair_up  = Phi(z), clipped to [1-fair_cap, fair_cap]
      for side in (up, down):            # <- strategy.py iteration order
          ask = best ask on that side, AS OF t, from the quote tape
          require price_min < ask < price_max
          edge = fair_side - ask - fee_rate*ask*(1-ask)
          fire if edge > edge_min
      take the FIRST qualifying side; ONE entry per window (snipe_done)
  fill: latency_ms later, RE-READ the tape and walk the ask ladder while
        edge_fn(p) > edge_min and p <= best_ask + max_walk_above_best, capped
        at per_event_cap_usd. `fair` is FROZEN at its signal-time value
        (engine.py closes over `_fair=fair`).

Two fill models, both reported (the CSV carries both):
  * default          - the Telonex `quotes` channel is a TOP-OF-BOOK tape, so
                       the ask ladder has exactly one level; fills are bounded
                       by displayed best-ask size and are a LOWER bound on what
                       the live bot (which walked the full CLOB /book ladder)
                       could take.
  * --depth          - fills against the real book_snapshot_5 ask ladder via a
                       line-for-line port of fill_engine.walk_asks, including
                       the max_walk_above_best bound. This is the faithful one.

Outputs
  audit/A2_replay_trades.csv     one row per signal (filled or not), both models
  audit/A2_replay_tau_scan.csv   --tau-scan: the fixed-tau counterfactual grid
  stdout                         the summary tables used in audit/A2_replay.md
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "data/live_audit"
KLINES = ROOT / "data/data/processed/binance/klines_1s"
OUT_CSV = ROOT / "audit/A2_replay_trades.csv"

SEC = 1_000_000  # microseconds per second


# --------------------------------------------------------------------- config
def load_cfg() -> dict:
    with open(ROOT / "bot/config.yaml") as fh:
        return yaml.safe_load(fh)


def fee_per_share(price: float, fee_rate: float) -> float:
    """fill_engine.fee_per_share"""
    return fee_rate * price * (1.0 - price)


def normal_cdf(z: float) -> float:
    """oracle.normal_cdf"""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------- price data
class Klines:
    """Second-indexed Binance 1s klines with O(1) lookup.

    `open_at(s)`  = open of the 1s candle starting at s == price at instant s.
                    This is oracle.price_at_second's convention and is strictly
                    non-anticipative at decision time s.
    """

    def __init__(self, days: list[str]):
        frames = []
        for d in days:
            f = KLINES / f"{d}.parquet"
            if f.exists():
                k = pd.read_parquet(f, columns=["open_time", "open", "close"])
                frames.append(k)
        if not frames:
            raise SystemExit("no klines found")
        k = pd.concat(frames, ignore_index=True)
        k["sec"] = (k.open_time // SEC).astype("int64")
        k = k.drop_duplicates("sec").sort_values("sec")
        self.lo = int(k.sec.iloc[0])
        self.hi = int(k.sec.iloc[-1])
        n = self.hi - self.lo + 1
        self._open = np.full(n, np.nan)
        self._close = np.full(n, np.nan)
        idx = k.sec.values - self.lo
        self._open[idx] = k["open"].values
        self._close[idx] = k["close"].values
        # forward-fill any missing seconds (data gaps) with the last close
        for arr in (self._close,):
            mask = np.isnan(arr)
            if mask.any():
                good = np.where(~mask)[0]
                arr[mask] = np.interp(np.where(mask)[0], good, arr[good])
        m = np.isnan(self._open)
        self._open[m] = self._close[m]
        # log-price and its 1s differences, for fast rolling std
        self._logp = np.log(self._open)
        self._ret = np.diff(self._logp, prepend=np.nan)

    def open_at(self, s: int) -> float:
        i = s - self.lo
        if i < 0 or i >= len(self._open):
            return float("nan")
        return float(self._open[i])

    def sigma_1s(self, t: int, window_secs: int) -> float:
        """Sample std (ddof=1) of the 1s log-returns over the trailing
        `window_secs` ending at t — oracle.rolling_log_return_std with an exact
        1Hz series instead of REST-poll samples."""
        j = t - self.lo + 1
        i = j - window_secs
        if i < 1 or j > len(self._ret):
            return float("nan")
        seg = self._ret[i:j]
        seg = seg[np.isfinite(seg)]
        if len(seg) < 2:
            return float("nan")
        return float(np.std(seg, ddof=1))


def load_hour_candles(days: list[str]) -> dict:
    """Binance 1H candle open/close, aggregated from the 1s klines.

    (Binance builds the 1H candle from the same trade stream: open = open of
    the first second of the hour, close = close of the last second.)
    """
    kl = {}
    for d in days:
        f = KLINES / f"{d}.parquet"
        if not f.exists():
            continue
        k = pd.read_parquet(f, columns=["open_time", "open", "close"])
        k["sec"] = (k.open_time // SEC).astype("int64")
        k["hour"] = (k.sec // 3600) * 3600
        g = k.groupby("hour").agg(o=("open", "first"), c=("close", "last"))
        for h, row in g.iterrows():
            kl[int(h)] = (float(row.o), float(row.c))
    return kl


# ---------------------------------------------------------------- quote tape
def load_tape(slug: str, outcome_id: int, days: list[str]) -> tuple:
    """Return (ts_us sorted asc, ask_price, ask_size) for one outcome.

    Rows where ask_price is NaN are KEPT: "the book has no ask" is a real
    observable state (OrderBook.best_ask is None), not missing data.
    """
    frames = []
    for d in days:
        pat = str(LIVE / f"quotes/outcome_{outcome_id}"
                  / f"polymarket_quotes_{d}_{slug}_{outcome_id}.parquet")
        for f in glob.glob(pat):
            frames.append(pd.read_parquet(f, columns=["timestamp_us", "ask_price", "ask_size"]))
    if not frames:
        return None
    q = pd.concat(frames, ignore_index=True)
    q["ask_price"] = pd.to_numeric(q["ask_price"], errors="coerce")
    q["ask_size"] = pd.to_numeric(q["ask_size"], errors="coerce")
    q = q.sort_values("timestamp_us", kind="mergesort")
    return (q.timestamp_us.values.astype("int64"),
            q.ask_price.values.astype("float64"),
            q.ask_size.values.astype("float64"))


def book_as_of(tape, ts_us: int):
    """Top-of-book ask (price, size) as of ts_us; (nan, nan) => no ask standing,
    None => tape has nothing at or before ts_us at all (== no_book)."""
    if tape is None:
        return None
    ts, ap, az = tape
    i = np.searchsorted(ts, ts_us, side="right") - 1
    if i < 0:
        return None
    return float(ap[i]), float(az[i])


def load_depth(slug: str, outcome_id: int, days: list[str]):
    """book_snapshot_5 ask ladder tape: (ts_us, prices[n,5], sizes[n,5]).

    Only downloaded for the windows that produced a signal (see
    scripts/fetch_live_audit_data.py notes) — used to answer "what would the
    live bot's FULL-ladder walk have filled?"."""
    frames = []
    for d in days:
        pat = str(LIVE / f"books/outcome_{outcome_id}"
                  / f"polymarket_book_snapshot_5_{d}_{slug}_{outcome_id}.parquet")
        for f in glob.glob(pat):
            frames.append(pd.read_parquet(f))
    if not frames:
        return None
    q = pd.concat(frames, ignore_index=True).sort_values("timestamp_us", kind="mergesort")
    pcols = [f"ask_price_{i}" for i in range(5)]
    scols = [f"ask_size_{i}" for i in range(5)]
    P = np.column_stack([pd.to_numeric(q[c], errors="coerce").values for c in pcols])
    S = np.column_stack([pd.to_numeric(q[c], errors="coerce").values for c in scols])
    return q.timestamp_us.values.astype("int64"), P.astype("float64"), S.astype("float64")


def ladder_as_of(depth, ts_us: int):
    if depth is None:
        return None
    ts, P, S = depth
    i = np.searchsorted(ts, ts_us, side="right") - 1
    if i < 0:
        return None
    p, s = P[i], S[i]
    m = np.isfinite(p)
    return list(zip(p[m], np.nan_to_num(s[m])))


def walk_asks_ladder(levels, fair, edge_min, price_min, price_max, cap_usd, fee_rate,
                     max_above_best):
    """Faithful port of fill_engine.walk_asks over a real multi-level ladder."""
    remaining = cap_usd
    best = None
    shares = cost = fees = 0.0
    for price, size in levels:
        if remaining <= 0:
            break
        if not (price_min < price < price_max):
            break
        if best is None:
            best = price
        elif max_above_best is not None and price > best + max_above_best + 1e-9:
            break
        if fair - price - fee_per_share(price, fee_rate) <= edge_min:
            break
        sh = min(size, remaining / price)
        if sh <= 1e-9:
            break
        shares += sh
        cost += sh * price
        fees += fee_per_share(price, fee_rate) * sh
        remaining -= sh * price
    return shares, cost, fees


# ------------------------------------------------------------------- replay
def walk_asks_single(price, size, fair, edge_min, price_min, price_max, cap_usd, fee_rate):
    """fill_engine.walk_asks over a one-level (top-of-book) ladder."""
    if not np.isfinite(price):
        return 0.0, 0.0, 0.0  # shares, cost, fees
    if not (price_min < price < price_max):
        return 0.0, 0.0, 0.0
    edge = fair - price - fee_per_share(price, fee_rate)
    if edge <= edge_min:
        return 0.0, 0.0, 0.0
    sz = size if np.isfinite(size) else 0.0
    shares = min(sz, cap_usd / price)
    if shares <= 1e-9:
        return 0.0, 0.0, 0.0
    return shares, shares * price, fee_per_share(price, fee_rate) * shares


def evaluate_second(kl, t, close_ts, s_open, tape_up, tape_down, cfg):
    """One decision tick == strategy.evaluate_close_snipe. Returns a dict or None."""
    tau = close_ts - t
    if tau <= 0:
        return None
    s_t = kl.open_at(t)
    sigma_1s = kl.sigma_1s(t, cfg["vol_window_secs"])
    if not np.isfinite(sigma_1s) or sigma_1s <= 0:
        return None
    if not (np.isfinite(s_t) and np.isfinite(s_open)) or s_t <= 0 or s_open <= 0:
        return None
    sigma_1s = max(sigma_1s, cfg["sigma_1s_floor"])
    sigma = sigma_1s * math.sqrt(tau)
    z = math.log(s_t / s_open) / sigma
    fair_up = normal_cdf(z)
    cap = cfg["fair_cap"]
    fair_up = min(max(fair_up, 1.0 - cap), cap)
    fair_down = 1.0 - fair_up

    for side, tape, fair in (("up", tape_up, fair_up), ("down", tape_down, fair_down)):
        b = book_as_of(tape, t * SEC)
        if b is None:
            continue
        ask, _sz = b
        if not np.isfinite(ask):
            continue
        if not (cfg["price_min"] < ask < cfg["price_max"]):
            continue
        edge = fair - ask - fee_per_share(ask, cfg["fee_rate"])
        if edge > cfg["edge_min"]:
            return dict(side=side, fair=fair, ask=ask, edge=edge, tau=tau, s_t=s_t,
                        s_open=s_open, sigma_1s=sigma_1s, z=z, t=t)
    return None


def fill_signal(sig, tape_up, tape_down, cfg, depth_up=None, depth_down=None):
    """fill_engine.execute_taker_signal against the RE-READ tape."""
    tape = tape_up if sig["side"] == "up" else tape_down
    depth = depth_up if sig["side"] == "up" else depth_down
    fill_ts_us = int(round((sig["t"] + cfg["latency_ms"] / 1000.0) * SEC))
    if depth is not None:
        lv = ladder_as_of(depth, fill_ts_us)
        if lv is None:
            return dict(outcome="no_book", shares=0.0, cost=0.0, fees=0.0,
                        ask_fill=np.nan, ask_size_fill=np.nan, fill_ts_us=fill_ts_us, levels=0)
        if not lv:
            return dict(outcome="empty_book", shares=0.0, cost=0.0, fees=0.0,
                        ask_fill=np.nan, ask_size_fill=np.nan, fill_ts_us=fill_ts_us, levels=0)
        sh, cost, fees = walk_asks_ladder(lv, sig["fair"], cfg["edge_min"], cfg["price_min"],
                                          cfg["price_max"], cfg["cap_usd"], cfg["fee_rate"],
                                          cfg["max_walk_above_best"])
        return dict(outcome=("filled" if sh > 0 else "book_moved_no_edge"), shares=sh,
                    cost=cost, fees=fees, ask_fill=lv[0][0], ask_size_fill=lv[0][1],
                    fill_ts_us=fill_ts_us, levels=len(lv))
    b = book_as_of(tape, fill_ts_us)
    if b is None:
        return dict(outcome="no_book", shares=0.0, cost=0.0, fees=0.0,
                    ask_fill=np.nan, ask_size_fill=np.nan, fill_ts_us=fill_ts_us)
    ask, sz = b
    if not np.isfinite(ask):
        return dict(outcome="empty_book", shares=0.0, cost=0.0, fees=0.0,
                    ask_fill=np.nan, ask_size_fill=np.nan, fill_ts_us=fill_ts_us)
    shares, cost, fees = walk_asks_single(
        ask, sz, sig["fair"], cfg["edge_min"], cfg["price_min"], cfg["price_max"],
        cfg["cap_usd"], cfg["fee_rate"])
    return dict(outcome=("filled" if shares > 0 else "book_moved_no_edge"),
                shares=shares, cost=cost, fees=fees, ask_fill=ask, ask_size_fill=sz,
                fill_ts_us=fill_ts_us)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-min", type=float, default=None)
    ap.add_argument("--cap-usd", type=float, default=None)
    ap.add_argument("--snipe-last", type=int, default=None)
    ap.add_argument("--out", default=str(OUT_CSV))
    ap.add_argument("--tau-scan", action="store_true",
                    help="also run the fixed-tau counterfactual grid")
    ap.add_argument("--latency-ms", type=int, default=None)
    ap.add_argument("--price-min", type=float, default=None)
    ap.add_argument("--price-max", type=float, default=None)
    ap.add_argument("--depth", action="store_true",
                    help="fill against the real book_snapshot_5 ask ladder (full "
                         "fill_engine.walk_asks) instead of the top-of-book quote tape")
    args = ap.parse_args()

    ycfg = load_cfg()
    sc = ycfg["strategy"]["close_snipe"]
    cfg = dict(
        snipe_last_secs=int(args.snipe_last or sc["snipe_last_secs"]),
        edge_min=float(args.edge_min if args.edge_min is not None else sc["edge_min"]),
        price_min=float(args.price_min if args.price_min is not None else sc["price_min"]),
        price_max=float(args.price_max if args.price_max is not None else sc["price_max"]),
        vol_window_secs=int(sc["vol_window_secs"]),
        sigma_1s_floor=float(sc["sigma_1s_floor"]),
        fair_cap=float(sc["fair_cap"]),
        fee_rate=float(ycfg["fees"]["fee_rate"]),
        cap_usd=float(args.cap_usd if args.cap_usd is not None
                      else ycfg["sizing"]["per_event_cap_usd"]),
        latency_ms=int(args.latency_ms if args.latency_ms is not None
                       else ycfg["execution"]["latency_ms"]),
        max_walk_above_best=float(ycfg["execution"]["max_walk_above_best"]),
    )
    print("config:", json.dumps(cfg, indent=None))

    mk = pd.read_parquet(LIVE / "hourly_markets.parquet").sort_values("close_ts")
    days = sorted({d for d in mk.close_date} | {d for d in mk.window_start_date})
    all_days = sorted({(pd.Timestamp(d) + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
                       for d in days for k in (-1, 0, 1)})
    kl = Klines(all_days)
    hours = load_hour_candles(all_days)

    rows, tau_rows = [], []
    n_eval_ticks = 0
    n_no_kline = 0
    n_no_tape = 0
    for _, m in mk.iterrows():
        close_ts, ws = int(m.close_ts), int(m.window_start_ts)
        hc = hours.get(ws)
        if hc is None:
            n_no_kline += 1
            continue
        s_open, s_close = hc
        qdays = sorted({m.close_date, m.window_start_date,
                        (pd.Timestamp(m.close_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")})
        tape_up = load_tape(m.slug, 0, qdays)
        tape_down = load_tape(m.slug, 1, qdays)
        if tape_up is None and tape_down is None:
            n_no_tape += 1
            continue
        depth_up = load_depth(m.slug, 0, qdays) if args.depth else None
        depth_down = load_depth(m.slug, 1, qdays) if args.depth else None
        won_up = int(m.result_id) == 0

        # --- the bot's own loop: first qualifying second, one entry per window
        sig = None
        for k in range(cfg["snipe_last_secs"], 0, -1):     # tau = 6,5,4,3,2,1
            t = close_ts - k
            n_eval_ticks += 1
            s = evaluate_second(kl, t, close_ts, s_open, tape_up, tape_down, cfg)
            if s is not None:
                sig = s
                break

        # --- fixed-tau counterfactual grid (ignores one-per-window)
        if args.tau_scan:
            for k in range(cfg["snipe_last_secs"], 0, -1):
                t = close_ts - k
                s = evaluate_second(kl, t, close_ts, s_open, tape_up, tape_down, cfg)
                if s is None:
                    continue
                f = fill_signal(s, tape_up, tape_down, cfg, depth_up, depth_down)
                won = won_up if s["side"] == "up" else (not won_up)
                pnl = (1.0 if won else 0.0) * f["shares"] - f["cost"] - f["fees"]
                tau_rows.append(dict(slug=m.slug, tau=k, side=s["side"], fair=s["fair"],
                                     ask=s["ask"], edge=s["edge"], outcome=f["outcome"],
                                     shares=f["shares"], cost=f["cost"], fees=f["fees"],
                                     won=won, pnl=pnl))
        if sig is None:
            continue

        f = fill_signal(sig, tape_up, tape_down, cfg, depth_up, depth_down)
        won = won_up if sig["side"] == "up" else (not won_up)
        payout = (1.0 if won else 0.0) * f["shares"]
        pnl = payout - f["cost"] - f["fees"]
        # always also record the top-of-book-only fill, so one CSV carries both
        # fill models (quotes tape = 1 level; book_snapshot_5 = real ladder)
        ftob = fill_signal(sig, tape_up, tape_down, cfg)
        pnl_tob = (1.0 if won else 0.0) * ftob["shares"] - ftob["cost"] - ftob["fees"]
        rows.append(dict(
            slug=m.slug, close_ts=close_ts,
            close_dt=pd.Timestamp(close_ts, unit="s", tz="UTC").isoformat(),
            side=sig["side"], tau_signal=sig["tau"], signal_ts=sig["t"],
            fair=round(sig["fair"], 6), ask_signal=sig["ask"], edge_signal=round(sig["edge"], 6),
            s_t=sig["s_t"], s_open=sig["s_open"], sigma_1s=sig["sigma_1s"], z=round(sig["z"], 4),
            fill_ts_us=f["fill_ts_us"], ask_fill=f["ask_fill"], ask_size_fill=f["ask_size_fill"],
            outcome=f["outcome"], shares=round(f["shares"], 4), cost=round(f["cost"], 4),
            fees=round(f["fees"], 4),
            avg_price=(round(f["cost"] / f["shares"], 6) if f["shares"] > 0 else np.nan),
            won=bool(won), payout=round(payout, 4), pnl=round(pnl, 4),
            result_id=int(m.result_id), binance_1h_open=s_open, binance_1h_close=s_close,
            binance_says_up=bool(s_close >= s_open),
            fill_model=("book_snapshot_5_ladder" if args.depth else "top_of_book"),
            outcome_tob=ftob["outcome"], shares_tob=round(ftob["shares"], 4),
            cost_tob=round(ftob["cost"], 4), pnl_tob=round(pnl_tob, 4),
        ))

    tr = pd.DataFrame(rows)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    tr.to_csv(args.out, index=False)

    # ------------------------------------------------------------- reporting
    n_closes = len(mk) - n_no_kline - n_no_tape
    print(f"\ncloses in period            : {len(mk)}")
    print(f"  skipped (no 1h kline)     : {n_no_kline}")
    print(f"  skipped (no quote tape)   : {n_no_tape}")
    print(f"closes evaluated            : {n_closes}")
    print(f"evaluation ticks            : {n_eval_ticks}")
    # resolution cross-check: does result_id obey the Binance 1H candle rule?
    chk = []
    for _, m in mk.iterrows():
        hc = hours.get(int(m.window_start_ts))
        if hc:
            chk.append((int(m.result_id) == 0) == (hc[1] >= hc[0]))
    print(f"result_id == binance 1H rule: {sum(chk)}/{len(chk)}")

    if tr.empty:
        print("NO SIGNALS")
        return
    print(f"\nsignals                     : {len(tr)}")
    print(tr.outcome.value_counts().to_string())
    fl = tr[tr.outcome == "filled"]
    print(f"\nfills                       : {len(fl)}")
    print(f"shares                      : {fl.shares.sum():.2f}")
    print(f"notional deployed           : ${fl.cost.sum():.2f}")
    print(f"fees                        : ${fl.fees.sum():.2f}")
    print(f"total PnL                   : ${fl.pnl.sum():+.2f}")
    if fl.shares.sum() > 0:
        print(f"EV / share                  : {100*fl.pnl.sum()/fl.shares.sum():+.2f}c")
    print(f"win rate                    : {fl.won.mean()*100:.1f}%  ({fl.won.sum()}/{len(fl)})")
    if len(fl) > 1:
        per = fl.pnl / fl.shares
        print(f"t-stat (per-trade c/share)  : {per.mean()/ (per.std(ddof=1)/np.sqrt(len(per))):.2f}")
    print(f"avg fill size               : ${fl.cost.mean():.2f}")
    print(f"signals/day                 : {len(tr)/ (n_closes/24):.2f}")

    print("\n-- tau at which the signal FIRED (all signals) --")
    print(tr.tau_signal.value_counts().sort_index(ascending=False).to_string())
    print("\n-- tau of FILLED signals, with PnL --")
    g = fl.groupby("tau_signal").agg(n=("pnl", "size"), shares=("shares", "sum"),
                                     pnl=("pnl", "sum"), wins=("won", "sum"))
    g["c_per_share"] = 100 * g.pnl / g.shares
    print(g.to_string())

    if args.tau_scan and tau_rows:
        ts = pd.DataFrame(tau_rows)
        ts.to_csv(Path(args.out).with_name("A2_replay_tau_scan.csv"), index=False)
        print("\n-- FIXED-tau counterfactual (fire at exactly tau=k, ignore one-per-window) --")
        rowsout = []
        for k, sub in ts.groupby("tau"):
            f2 = sub[sub.outcome == "filled"]
            rowsout.append(dict(tau=k, signals=len(sub), fills=len(f2),
                                shares=f2.shares.sum(), pnl=f2.pnl.sum(),
                                c_per_share=(100 * f2.pnl.sum() / f2.shares.sum()
                                             if f2.shares.sum() > 0 else np.nan),
                                win_rate=(100 * f2.won.mean() if len(f2) else np.nan)))
        print(pd.DataFrame(rowsout).to_string(index=False))
    print(f"\nper-trade CSV -> {args.out}")


if __name__ == "__main__":
    main()
