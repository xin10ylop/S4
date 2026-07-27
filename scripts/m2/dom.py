#!/usr/bin/env python3
"""
M2 - CROSS-FAMILY DOMINANCE ARBITRAGE between the 5m and 15m BTC Up/Down books.

THE STRUCTURE
-------------
The last 5m window of every 15m window shares the SAME CLOSE (same Chainlink
settle print). Let

    O5  = strike of the 5m market  (first Chainlink obs at/after wts5  = wts15+600)
    O15 = strike of the 15m market (first Chainlink obs at/after wts15)
    C   = the shared settle print

5m Up wins iff C >= O5.  15m Up wins iff C >= O15.  Therefore, writing
O_lo = min(O5,O15) and O_hi = max(O5,O15) and buying

    1 share of  Up   on the LOW-strike market
  + 1 share of  Down on the HIGH-strike market

the payoff is
    C <  O_lo            -> Down(hi) wins                      -> $1
    O_lo <= C <  O_hi    -> Up(lo) wins AND Down(hi) wins       -> $2
    C >= O_hi            -> Up(lo) wins                         -> $1

i.e. >= $1 ALWAYS, with a $1 bonus whenever the close lands between the two
strikes.  The combo's fair value is therefore  1 + P(O_lo <= C < O_hi)  >= 1,
so ANY cost below $1 is a hard arbitrage (a floor violation), not a bet.

Because the repo vault reconstructs Down asks as (1 - Up bid), the entry
condition is exactly a CROSSED CROSS-MARKET QUOTE:

    cost = ask_UpLow + (1 - bid_UpHigh) < 1   <=>   ask_UpLow < bid_UpHigh

RIGOR (all mandated by audit/C2_harness.md, each already burned this project)
---------------------------------------------------------------------------
* TWO-BOOK FILLS.  The signal is read off the books at t; BOTH legs fill
  against books RE-FETCHED at t + latency.  Never the signal book.
* BOOK STALENESS.  Every leg's book carries an age; a leg whose book is older
  than the limit is untradeable at that instant (pre-decision rejection).
* CAUSALITY.  O5 is only knowable once its Chainlink report is PUBLISHED
  (server_timestamp_us), ~1.1 s after the observation second.  Decisions before
  that instant are illegal and are dropped.  O15 is published 10 min earlier.
* LEG RISK.  Legs are independent marketable-limit (IOC) orders.  Each fills
  only if its own re-fetched book still offers the limit price.  Single-leg
  fills are carried as naked positions and unwound or held, both priced.
* DAY-CLUSTERED t.  Reported alongside the (inflated) per-trade t.

DATA
----
repo vault  data/data/processed/daily/{5m,15m}/bookcurves/DAY.parquet
            data/data/processed/daily/crypto_prices/DAY.parquet
            data/windows_all.parquet     (result_id per market)
data/fresh5m is READ ONLY and owned by another workflow; outputs go to
data/multicoin/m2/.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = os.environ.get("S4_ROOT", "/home/user/S4")
sys.path.insert(0, f"{ROOT}/scripts/fresh5m")
from replay import ChainlinkFeed, fee_per_share, load_chainlink  # noqa: E402

REPO_PROC = f"{ROOT}/data/data/processed/daily"
OUT = f"{ROOT}/data/multicoin/m2"
INF = float("inf")

# bookcurves columns we need: top of book + the vendor's notional walk curve
_NOTIONALS = (50, 200, 1000, 5000)
_BC_COLS = (["timestamp_us", "bid_p0", "bid_s0", "ask_p0", "ask_s0", "wts",
             "bid_depth_5c", "ask_depth_5c"]
            + [f"buy_avgpx_{n}" for n in _NOTIONALS]
            + [f"buy_shares_{n}" for n in _NOTIONALS]
            + [f"buy_exhaust_{n}" for n in _NOTIONALS]
            + [f"sell_avgpx_{n}" for n in _NOTIONALS]
            + [f"sell_shares_{n}" for n in _NOTIONALS]
            + [f"sell_exhaust_{n}" for n in _NOTIONALS])


# ==========================================================================
# 1. TAPES
# ==========================================================================

class Tape:
    """Event tape for one market's Up-token book.

    Arrays are parallel and sorted by ts.  `idx(t)` gives the index of the last
    update at or before t (-1 if none) so every read is an explicit as-of with
    a measurable age.

    Down-token quantities are derived by the vault's verified identity
    (A3 §3.2, 5,017/5,017 exact):  ask_Down = 1 - bid_Up,  size_Down = size_bid_Up.
    """

    __slots__ = ("ts", "ask", "asz", "bid", "bsz",
                 "buy_sh", "buy_cost", "sell_sh", "sell_proc")

    def __init__(self, g: pd.DataFrame, clock: str = "exchange"):
        self.ts = (g.local_timestamp_us if clock == "local"
                   else g.timestamp_us).to_numpy(np.int64)
        self.ask = g.ask_p0.to_numpy(float)
        self.asz = g.ask_s0.to_numpy(float)
        self.bid = g.bid_p0.to_numpy(float)
        self.bsz = g.bid_s0.to_numpy(float)
        # cumulative (shares, cost) ladder points for BUYING Up
        bs = [self.asz] + [g[f"buy_shares_{n}"].to_numpy(float) for n in _NOTIONALS]
        bc = [self.ask * self.asz] + [
            g[f"buy_shares_{n}"].to_numpy(float) * g[f"buy_avgpx_{n}"].to_numpy(float)
            for n in _NOTIONALS]
        # cumulative (shares, proceeds) for SELLING Up  (== buying Down)
        ss = [self.bsz] + [g[f"sell_shares_{n}"].to_numpy(float) for n in _NOTIONALS]
        sp = [self.bid * self.bsz] + [
            g[f"sell_shares_{n}"].to_numpy(float) * g[f"sell_avgpx_{n}"].to_numpy(float)
            for n in _NOTIONALS]
        self.buy_sh = np.column_stack(bs)
        self.buy_cost = np.column_stack(bc)
        self.sell_sh = np.column_stack(ss)
        self.sell_proc = np.column_stack(sp)

    def idx(self, t_us: np.ndarray) -> np.ndarray:
        return np.searchsorted(self.ts, np.asarray(t_us, np.int64), side="right") - 1

    def age(self, t_us: np.ndarray, i: np.ndarray) -> np.ndarray:
        a = np.full(len(i), INF)
        ok = i >= 0
        a[ok] = (np.asarray(t_us, np.int64)[ok] - self.ts[i[ok]]) / 1e6
        return a


def _valid(x: np.ndarray) -> np.ndarray:
    return np.isfinite(x) & (x > 0.0) & (x < 1.0)


def load_day(day: str, fam: str, clock: str = "exchange") -> Dict[int, Tape]:
    f = f"{REPO_PROC}/{fam}/bookcurves/{day}.parquet"
    if not os.path.exists(f):
        return {}
    cols = list(_BC_COLS) + (["local_timestamp_us"] if clock == "local" else [])
    b = pd.read_parquet(f, columns=cols)
    tcol = "local_timestamp_us" if clock == "local" else "timestamp_us"
    b = b.dropna(subset=["wts"]).sort_values(tcol)
    return {int(k): Tape(v, clock) for k, v in b.groupby("wts", sort=False)}


# ==========================================================================
# 2. LADDER WALK  (synthetic ladder from the vendor's notional curve)
# ==========================================================================

def walk_cost(cum_sh: np.ndarray, cum_cost: np.ndarray, want: np.ndarray,
              best: np.ndarray, max_above_best: float,
              depth_fraction: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """Cost of taking `want` shares against a piecewise ladder.

    `cum_sh[k]`, `cum_cost[k]` are cumulative shares / dollars after consuming
    ladder point k (k=0 is top-of-book).  The MARGINAL price of segment k is
    (cum_cost[k]-cum_cost[k-1]) / (cum_sh[k]-cum_sh[k-1]); a segment whose
    marginal price exceeds best + max_above_best is refused, exactly like
    fill_engine.walk_asks' `max_above_best` guard.  Vectorised over rows.

    Returns (shares_filled, dollars).  Partial fills are allowed and reported.
    """
    n, K = cum_sh.shape
    sh = np.zeros(n)
    cost = np.zeros(n)
    prev_sh = np.zeros(n)
    prev_cost = np.zeros(n)
    remaining = want.astype(float).copy()
    blocked = np.zeros(n, bool)
    lim = best + max_above_best + 1e-9
    for k in range(K):
        d_sh = cum_sh[:, k] - prev_sh
        d_c = cum_cost[:, k] - prev_cost
        good = (~blocked) & np.isfinite(d_sh) & np.isfinite(d_c) & (d_sh > 1e-9)
        marg = np.divide(d_c, d_sh, out=np.full(n, np.inf), where=good)
        ok = good & (marg <= lim)
        blocked |= good & ~ok          # a too-expensive segment stops the walk
        d_sh_eff = d_sh * depth_fraction
        take = np.where(ok, np.minimum(remaining, np.maximum(d_sh_eff, 0.0)), 0.0)
        sh += take
        # marg is +inf on skipped rows; 0*inf = nan, so gate the accumulation
        cost += np.where(take > 0.0, take * np.where(np.isfinite(marg), marg, 0.0), 0.0)
        remaining -= take
        # advance the cursor only through segments we actually inspected
        adv = good
        prev_sh = np.where(adv, cum_sh[:, k], prev_sh)
        prev_cost = np.where(adv, cum_cost[:, k], prev_cost)
        if not np.any(remaining > 1e-9):
            break
    return sh, cost


# ==========================================================================
# 3. THE REPLAY
# ==========================================================================

@dataclass
class P:
    gap_min: float = 0.005          # required (bid_hi - ask_lo) at SIGNAL time, $
    latency_ms: int = 1500
    max_book_age_s: float = 5.0
    fee_rate: float = 0.07
    clip_usd: float = 25.0          # per PAIR (i.e. n_shares = clip/1.0)
    max_walk_above_best: float = 0.03
    depth_fraction: float = 1.0
    fill_margin_s: float = 0.5      # order must land this long before the close
    grid_hz: float = 0.0            # 0 => event-driven (every book update)
    # ---- ORDER TYPE ------------------------------------------------------
    # slack_frac: each leg's marketable-limit price gives away this fraction of
    #   the detected gap.  0.0 = pay exactly the detected quote (strictest);
    #   0.5 = give away the whole gap, so a both-legs fill still costs <= $1.
    #   market=True ignores limits entirely (both legs always fill at whatever
    #   the re-fetched book shows) - this is the naive model and it DESTROYS
    #   the arbitrage floor; it is included only to price that mistake.
    slack_frac: float = 0.0
    market: bool = False
    unwind_latency_ms: int = 1500   # for the single-leg abort test
    require_both_fresh: bool = True
    # ---- CONTAMINATION CONTROLS -----------------------------------------
    # persist_ms: the violation must have been CONTINUOUSLY present for this
    #   long before we are allowed to act on it.  A crossed cross-market quote
    #   that lives 40 ms is far more likely to be the vendor publishing the two
    #   streams out of sync than a real arbitrage.
    persist_ms: float = 0.0
    # dn_haircut: cents added to the Down leg's price, at BOTH signal and fill
    #   time, to price the fact that ask_Down is reconstructed as (1 - bid_Up)
    #   rather than observed.  Measured error: mean -0.03c, 1% of ticks
    #   optimistic by >= 0.5c (see M2 doc S2.2).
    dn_haircut: float = 0.0
    # clock: "exchange" (timestamp_us) or "local" (local_timestamp_us, i.e. the
    #   instant the capture host actually held the update).
    clock: str = "exchange"
    # fee_rate=None -> use the per-window fee_rate recorded in windows_all.
    per_window_fee: bool = False


def _grid(t0: int, t1: int, tapes: Sequence[Tape], hz: float) -> np.ndarray:
    """Decision instants in [t0, t1] (us)."""
    if t1 <= t0:
        return np.zeros(0, np.int64)
    if hz > 0:
        step = int(1e6 / hz)
        return np.arange(t0, t1 + 1, step, dtype=np.int64)
    ev = np.concatenate([tp.ts for tp in tapes]) if tapes else np.zeros(0, np.int64)
    ev = ev[(ev >= t0) & (ev <= t1)]
    ev = np.unique(np.concatenate([[t0], ev]))
    return ev.astype(np.int64)


def run_window(day: str, wts15: int, t15: Tape, t5: Tape,
               O15: float, O5: float, pub5_us: int,
               res15: int, res5: int, C: float, p: P,
               fee_rate: Optional[float] = None) -> Optional[dict]:
    """One shared-close pair.  Returns a record or None if no signal fired."""
    fee = p.fee_rate if fee_rate is None else float(fee_rate)
    close_s = wts15 + 900
    wts5 = wts15 + 600
    close_us = close_s * 1_000_000
    lat = int(p.latency_ms * 1000)

    # ---- direction: buy Up on the LOW strike, Down on the HIGH strike ----
    if O5 == O15:
        low_is_5 = True                     # degenerate: identical markets
    else:
        low_is_5 = O5 < O15
    # long leg  = Up  on low-strike market   -> we take its ASK ladder
    # short leg = Down on high-strike market -> we take the other's BID ladder
    A = t5 if low_is_5 else t15             # Up-low tape
    B = t15 if low_is_5 else t5             # Down-high tape (via its bid side)

    # ---- causality: O5 must be PUBLISHED, and the order must land pre-close
    t0 = max(int(wts5 * 1e6), int(pub5_us))
    t1 = close_us - lat - int(p.fill_margin_s * 1e6)
    grid = _grid(t0, t1, (t15, t5), p.grid_hz)
    if len(grid) == 0:
        return None

    ia = A.idx(grid)
    ib = B.idx(grid)
    aa = A.age(grid, ia)
    ab = B.age(grid, ib)
    ok = (ia >= 0) & (ib >= 0)
    ja, jb = np.clip(ia, 0, None), np.clip(ib, 0, None)
    ask_lo = np.where(ok, A.ask[ja], np.nan)
    bid_hi = np.where(ok, B.bid[jb], np.nan)
    fresh = (aa <= p.max_book_age_s) & (ab <= p.max_book_age_s)
    # the Down leg is reconstructed as (1 - bid_Up); charge the haircut to the
    # detected gap so a reconstruction error cannot manufacture a signal.
    gap = bid_hi - ask_lo - p.dn_haircut
    live = ok & _valid(ask_lo) & _valid(bid_hi) & (gap > p.gap_min)
    sig = live & fresh if p.require_both_fresh else live
    if p.persist_ms > 0 and sig.any():
        # rs[i] = grid time at which the CURRENT unbroken `live` run began
        rs = np.empty(len(grid), np.int64)
        cur = np.iinfo(np.int64).max
        for i in range(len(grid)):
            if not live[i]:
                cur = np.iinfo(np.int64).max
            elif cur == np.iinfo(np.int64).max:
                cur = grid[i]
            rs[i] = cur
        sig = sig & (grid - rs >= int(p.persist_ms * 1000))
    hit = np.flatnonzero(sig)
    if len(hit) == 0:
        # still record whether an unfiltered violation existed (diagnostic)
        return dict(day=day, wts15=wts15, close_s=close_s, signal=False,
                    n_violation_ticks=int(live.sum()),
                    stale_blocked=int((live & ~fresh).sum()))
    k = hit[0]
    t_sig = int(grid[k])

    rec = dict(day=day, wts15=wts15, close_s=close_s, signal=True, fee_rate=fee,
               t_sig=t_sig, tau_sig=(close_us - t_sig) / 1e6,
               low_is_5=bool(low_is_5), O5=O5, O15=O15, C=C,
               strike_gap=abs(O5 - O15),
               ask_lo_sig=float(ask_lo[k]), bid_hi_sig=float(bid_hi[k]),
               gap_sig=float(gap[k]),
               age_a_sig=float(aa[k]), age_b_sig=float(ab[k]),
               n_violation_ticks=int(live.sum()),
               stale_blocked=int((live & ~fresh).sum()))

    # violation lifetime measured forward from the signal on the same grid
    nxt = np.flatnonzero(~live[k:])
    rec["violation_life_s"] = (float(grid[k + nxt[0]] - t_sig) / 1e6
                               if len(nxt) else (close_us - t_sig) / 1e6)

    # ---- payoff (vendor truth) -------------------------------------------
    up_low_wins = (res5 == 0) if low_is_5 else (res15 == 0)
    dn_high_wins = (res15 == 1) if low_is_5 else (res5 == 1)
    payoff = float(up_low_wins) + float(dn_high_wins)
    rec.update(up_low_wins=bool(up_low_wins), dn_high_wins=bool(dn_high_wins),
               payoff=payoff, between=bool(payoff == 2.0))

    # ---- TWO-BOOK FILL: re-fetch BOTH books at t + latency ---------------
    tf = np.array([t_sig + lat], np.int64)
    fa, fb = A.idx(tf), B.idx(tf)
    aga, agb = A.age(tf, fa), B.age(tf, fb)
    rec["age_a_fil"] = float(aga[0])
    rec["age_b_fil"] = float(agb[0])
    if fa[0] < 0 or fb[0] < 0:
        rec["outcome"] = "empty_book"
        return rec
    ask_lo_f = float(A.ask[fa[0]])
    bid_hi_f = float(B.bid[fb[0]])
    rec.update(ask_lo_fil=ask_lo_f, bid_hi_fil=bid_hi_f,
               gap_fil=bid_hi_f - ask_lo_f)
    stale_fill = (aga[0] > p.max_book_age_s) or (agb[0] > p.max_book_age_s)
    rec["stale_fill"] = bool(stale_fill)
    if stale_fill:
        rec["outcome"] = "stale_book"
        return rec

    # ---- leg-by-leg marketable-limit fills -------------------------------
    # Leg A: BUY Up on the low-strike market, limit = detected ask + slack.
    # Leg B: BUY Down on the high-strike market, i.e. SELL Up at the detected
    #        bid - slack, so the DOWN limit is (1 - bid_sig) + slack.
    slack = p.slack_frac * rec["gap_sig"]
    want = np.array([p.clip_usd / 1.0])     # $1 of collateral per pair share
    dn_sig = 1.0 - rec["bid_hi_sig"] + p.dn_haircut
    dn_fil = 1.0 - bid_hi_f + p.dn_haircut
    if p.market:
        lim_a, lim_b_dn = INF, INF
    else:
        lim_a = rec["ask_lo_sig"] + slack
        lim_b_dn = dn_sig + slack
    rec.update(limit_a=lim_a, limit_b_dn=lim_b_dn, dn_sig=dn_sig, dn_fil=dn_fil)

    a_ok = bool(_valid(np.array([ask_lo_f]))[0]) and ask_lo_f <= lim_a + 1e-9
    b_ok = bool(_valid(np.array([dn_fil]))[0]) and dn_fil <= lim_b_dn + 1e-9

    sh_a = cost_a = 0.0
    if a_ok:
        bound = min(p.max_walk_above_best,
                    INF if p.market else max(0.0, lim_a - ask_lo_f))
        s, c = walk_cost(A.buy_sh[fa], A.buy_cost[fa], want,
                         np.array([ask_lo_f]), bound, p.depth_fraction)
        sh_a, cost_a = float(s[0]), float(c[0])
    sh_b = proc_b = 0.0
    if b_ok:
        bound = min(p.max_walk_above_best,
                    INF if p.market else max(0.0, lim_b_dn - dn_fil))
        s, c = walk_cost(B.sell_sh[fb],
                         B.sell_sh[fb] - B.sell_proc[fb],   # cumulative DOWN cost
                         want, np.array([1.0 - bid_hi_f]), bound, p.depth_fraction)
        sh_b, proc_b = float(s[0]), float(c[0])
        proc_b += p.dn_haircut * sh_b       # reconstruction haircut, per share

    # ---- DEPTH CENSUS at the fill instant, 3c walk bound, size-unbounded ----
    BIG = np.array([1e9])
    sda, cda = walk_cost(A.buy_sh[fa], A.buy_cost[fa], BIG,
                         np.array([ask_lo_f]), 0.03, 1.0)
    sdb, cdb = walk_cost(B.sell_sh[fb], B.sell_sh[fb] - B.sell_proc[fb], BIG,
                         np.array([1.0 - bid_hi_f]), 0.03, 1.0)
    rec.update(depth_a_sh=float(sda[0]), depth_a_usd=float(cda[0]),
               depth_b_sh=float(sdb[0]), depth_b_usd=float(cdb[0]),
               depth_pair_sh=float(min(sda[0], sdb[0])),
               depth_pair_usd=float(min(sda[0], sdb[0])
                                    * ((cda[0] / sda[0] if sda[0] > 1e-9 else 0.0)
                                       + (cdb[0] / sdb[0] if sdb[0] > 1e-9 else 0.0))))
    # top-of-book only (walk bound 0) - the size a strict at-the-quote IOC gets
    s0a, _ = walk_cost(A.buy_sh[fa], A.buy_cost[fa], BIG,
                       np.array([ask_lo_f]), 0.0, 1.0)
    s0b, _ = walk_cost(B.sell_sh[fb], B.sell_sh[fb] - B.sell_proc[fb], BIG,
                       np.array([1.0 - bid_hi_f]), 0.0, 1.0)
    rec.update(depth_pair_top_sh=float(min(s0a[0], s0b[0])))

    px_a = (cost_a / sh_a) if sh_a > 1e-9 else np.nan
    px_b = (proc_b / sh_b) if sh_b > 1e-9 else np.nan       # avg DOWN price paid
    n_pair = min(sh_a, sh_b)
    rec.update(shares_a=sh_a, shares_b=sh_b, pair_shares=n_pair,
               px_a=px_a, px_b=px_b)

    # ---- (1) the matched portion: a true arbitrage pair -------------------
    pnl_pair = 0.0
    if n_pair > 1e-9:
        ca = px_a + fee_per_share(px_a, fee)
        cb = px_b + fee_per_share(px_b, fee)
        rec.update(cost_per_pair=ca + cb, pnl_per_pair=payoff - (ca + cb),
                   gross_cost_per_pair=px_a + px_b,
                   fee_per_pair=(ca - px_a) + (cb - px_b))
        pnl_pair = n_pair * (payoff - (ca + cb))

    # ---- (2) the UNMATCHED residual: naked directional risk ---------------
    # This is LEG RISK.  It exists whenever one leg fills more than the other,
    # including the extreme case where one leg fills nothing at all.
    resid = abs(sh_a - sh_b)
    rec["resid_shares"] = resid
    pnl_resid_hold = pnl_resid_abort = 0.0
    if resid > 1e-9:
        if sh_a > sh_b:
            leg, px, won, tape, long_up = "long_up_low", px_a, up_low_wins, A, True
        else:
            leg, px, won, tape, long_up = "long_dn_high", px_b, dn_high_wins, B, False
        entry = resid * (px + fee_per_share(px, fee))
        rec.update(leg=leg, leg_px=px, leg_won=bool(won), leg_entry=entry)
        pnl_resid_hold = (1.0 if won else 0.0) * resid - entry
        # ABORT: unwind by crossing back at t + latency + unwind_latency
        tu = np.array([t_sig + lat + int(p.unwind_latency_ms * 1000)], np.int64)
        iu = tape.idx(tu)
        agu = tape.age(tu, iu)
        if iu[0] < 0 or agu[0] > p.max_book_age_s or tu[0] > close_us:
            pnl_resid_abort = pnl_resid_hold          # cannot unwind -> must hold
            rec["abort_ok"] = False
        else:
            if long_up:                    # long Up -> sell Up into its bid
                s, c = walk_cost(tape.sell_sh[iu], tape.sell_proc[iu],
                                 np.array([resid]), np.array([tape.bid[iu[0]]]),
                                 INF, p.depth_fraction)
                got, proceeds = float(s[0]), float(c[0])
                exit_px = proceeds / got if got > 1e-9 else 0.0
            else:                          # long Down -> sell Down == buy Up
                s, c = walk_cost(tape.buy_sh[iu], tape.buy_cost[iu],
                                 np.array([resid]), np.array([tape.ask[iu[0]]]),
                                 INF, p.depth_fraction)
                got, spent = float(s[0]), float(c[0])
                exit_px = (1.0 - spent / got) if got > 1e-9 else 0.0
            unsold = resid - got
            pnl_resid_abort = (got * (exit_px - fee_per_share(exit_px, fee))
                               + (1.0 if won else 0.0) * unsold - entry)
            rec.update(abort_ok=True, abort_px=exit_px,
                       abort_filled_frac=got / resid if resid else 0.0)
        # COMPLETE: instead of unwinding, market the MISSING leg to restore the
        # pair structure (payoff >= $1).  Same clock as the abort.
        other = B if long_up else A
        pnl_resid_complete = pnl_resid_hold
        rec["complete_ok"] = False
        io = other.idx(tu)
        ago = other.age(tu, io)
        if io[0] >= 0 and ago[0] <= p.max_book_age_s and tu[0] <= close_us:
            if long_up:                    # missing leg = Down on the high mkt
                s, c = walk_cost(other.sell_sh[io],
                                 other.sell_sh[io] - other.sell_proc[io],
                                 np.array([resid]),
                                 np.array([1.0 - other.bid[io[0]]]),
                                 INF, p.depth_fraction)
                got, spent = float(s[0]), float(c[0])
                opx = spent / got if got > 1e-9 else 0.0
                o_won = dn_high_wins
            else:                          # missing leg = Up on the low mkt
                s, c = walk_cost(other.buy_sh[io], other.buy_cost[io],
                                 np.array([resid]), np.array([other.ask[io[0]]]),
                                 INF, p.depth_fraction)
                got, spent = float(s[0]), float(c[0])
                opx = spent / got if got > 1e-9 else 0.0
                o_won = up_low_wins
            if got > 1e-9:
                paid = got * (opx + fee_per_share(opx, fee))
                # got shares now form pairs -> payoff; the rest stays naked
                un = resid - got
                pnl_resid_complete = (got * payoff - paid
                                      + (1.0 if won else 0.0) * un
                                      - entry)
                rec.update(complete_ok=True, complete_px=opx,
                           complete_filled_frac=got / resid, complete_won=bool(o_won))
        rec.update(pnl_resid_hold=pnl_resid_hold, pnl_resid_abort=pnl_resid_abort,
                   pnl_resid_complete=pnl_resid_complete)
    else:
        pnl_resid_complete = 0.0

    if n_pair > 1e-9 and resid <= 1e-9:
        rec["outcome"] = "filled_both"
    elif n_pair > 1e-9:
        rec["outcome"] = "filled_partial"
    elif resid > 1e-9:
        rec["outcome"] = "single_leg"
    else:
        rec["outcome"] = "no_fill"
    rec.update(pnl_pair=pnl_pair,
               pnl_hold=pnl_pair + pnl_resid_hold,
               pnl_abort=pnl_pair + pnl_resid_abort,
               pnl_complete=pnl_pair + pnl_resid_complete,
               notional=(n_pair * rec.get("cost_per_pair", 0.0)
                         + resid * (0.0 if resid <= 1e-9 else rec["leg_px"])))
    return rec


# ==========================================================================
# 4. DRIVER
# ==========================================================================

def run_days(days: Sequence[str], p: P, verbose: bool = True) -> Tuple[pd.DataFrame, dict]:
    w = pd.read_parquet(f"{ROOT}/data/windows_all.parquet")
    w5 = w[w.family == "5m"].set_index("wts")["result_id"].to_dict()
    w15 = w[w.family == "15m"].set_index("wts")["result_id"].to_dict()
    f5 = w[w.family == "5m"].set_index("wts")["fee_rate"].to_dict()
    f15 = w[w.family == "15m"].set_index("wts")["fee_rate"].to_dict()
    recs: List[dict] = []
    meta = dict(days=0, pairs=0, no_strike=0, no_book=0, no_result=0,
                dominance_violations=0)
    for d in days:
        prev = (pd.Timestamp(d) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        cl = load_chainlink([prev, d])
        T5, T15 = load_day(d, "5m", p.clock), load_day(d, "15m", p.clock)
        if cl is None or not T5 or not T15:
            if verbose:
                print(f"  {d}: missing inputs", flush=True)
            continue
        n0 = len(recs)
        pairs = 0
        for wts15 in sorted(T15):
            wts5 = wts15 + 600
            if wts5 not in T5:
                meta["no_book"] += 1
                continue
            if wts15 not in w15 or wts5 not in w5:
                meta["no_result"] += 1
                continue
            st = cl.strike(np.array([wts15, wts5, wts15 + 900]), mode="backfill")
            O15, O5, C = float(st[0]), float(st[1]), float(st[2])
            if not (math.isfinite(O15) and math.isfinite(O5) and math.isfinite(C)):
                meta["no_strike"] += 1
                continue
            # publication instant of the 5m strike print
            i = int(np.searchsorted(cl.obs_sorted, wts5 * 1_000_000, side="left"))
            pub5 = int(cl.pub_by_obs[i]) if i < len(cl.obs_sorted) else 0
            r5, r15 = w5[wts5], w15[wts15]
            if pd.isna(r5) or pd.isna(r15):
                meta["no_result"] += 1
                continue
            fr = None
            if p.per_window_fee:
                a, b = f5.get(wts5), f15.get(wts15)
                fr = float(max(a if pd.notna(a) else 0.0, b if pd.notna(b) else 0.0))
            rec = run_window(d, wts15, T15[wts15], T5[wts5], O15, O5, pub5,
                             int(r15), int(r5), C, p, fee_rate=fr)
            pairs += 1
            if rec is not None:
                recs.append(rec)
        meta["days"] += 1
        meta["pairs"] += pairs
        if verbose:
            n = sum(1 for r in recs[n0:] if r.get("signal"))
            print(f"  {d}: {pairs} shared closes, {n} signals", flush=True)
    return pd.DataFrame(recs), meta


# ==========================================================================
# 5. STATS
# ==========================================================================

def tstat(x) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")


def daily_t(df: pd.DataFrame, col: str) -> Tuple[float, int, np.ndarray]:
    dd = df.groupby("day")[col].mean().to_numpy(float)
    return tstat(dd), len(dd), dd


FILLED = ("filled_both", "filled_partial")


def summarise(tr: pd.DataFrame, n_days: int, n_pairs: int, label: str,
              pnl_col: str = "pnl_abort") -> dict:
    """`pnl_col` is DOLLARS per attempt at the configured clip, INCLUDING the
    naked residual (leg risk).  `ev_pair` is cents per matched pair-share, i.e.
    the clean arbitrage economics with leg risk stripped out - report both."""
    out = dict(label=label, days=n_days, shared_closes=n_pairs)
    if tr is None or tr.empty or "signal" not in tr:
        return out
    sig = tr[tr.signal == True].copy()  # noqa: E712
    out["signals"] = len(sig)
    out["signals_day"] = len(sig) / n_days if n_days else np.nan
    if sig.empty:
        return out
    oc = sig.outcome.fillna("no_signal")
    for k in ("filled_both", "filled_partial", "single_leg", "no_fill",
              "stale_book", "empty_book"):
        out[k] = int((oc == k).sum())
    att = sig[sig.outcome.isin(FILLED + ("single_leg", "no_fill"))]
    out["attempts"] = len(att)
    both = sig[sig.outcome.isin(FILLED)]
    out["both_legs"] = len(both)
    out["survival"] = len(both) / len(att) if len(att) else np.nan
    out["leg_risk_rate"] = (out["single_leg"] / len(att)) if len(att) else np.nan
    # --- clean pair economics (matched portion only) ----------------------
    if len(both):
        pp = both.pnl_per_pair.to_numpy(float)
        out.update(ev_pair=float(np.mean(pp)), pair_win=float((pp > 0).mean()),
                   pair_worst=float(np.min(pp)),
                   cost_pair=float(both.cost_per_pair.mean()),
                   between_frac=float(both.between.mean()),
                   pair_shares_med=float(both.pair_shares.median()))
    # --- dollars per attempt, leg risk included ---------------------------
    x = att[pnl_col].fillna(0.0).to_numpy(float)
    if len(x):
        daily = att.groupby("day")[pnl_col].sum().reindex(
            sorted(tr.day.unique())).fillna(0.0)
        out.update(usd_attempt=float(x.mean()), usd_med=float(np.median(x)),
                   usd_worst=float(x.min()), usd_best=float(x.max()),
                   win=float((x > 0).mean()),
                   t_trade=tstat(x), t_day=tstat(daily.to_numpy(float)),
                   n_day_clusters=int(len(daily)),
                   pnl_total=float(x.sum()),
                   pnl_day=float(daily.mean()))
    return out


def drop_best(att: pd.DataFrame, pnl_col: str, k: int, n_days: int) -> dict:
    """P&L with the k best DAYS removed."""
    daily = att.groupby("day")[pnl_col].sum().sort_values(ascending=False)
    keep = daily.index[k:]
    d2 = daily.loc[keep]
    return dict(days_kept=len(d2), pnl_total=float(d2.sum()),
                pnl_day=float(d2.mean()) if len(d2) else np.nan,
                t_day=tstat(d2.to_numpy(float)))
