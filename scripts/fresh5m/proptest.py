#!/usr/bin/env python3
"""Property-test the replay harness's ported primitives against the REAL bot
functions in bot/polybot/{strategy,fill_engine}.py on randomised inputs.

If this is not 0 mismatches, no number the harness produces means anything:
the whole point of the control test is that the harness runs the bot's logic,
not a plausible reimplementation of it.

  python3 scripts/fresh5m/proptest.py [N]
"""
from __future__ import annotations

import datetime as dt
import math
import os
import random
import sys

sys.path.insert(0, "/home/user/S4")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.polybot import fill_engine as FE            # noqa: E402
from bot.polybot import strategy as ST               # noqa: E402
from bot.polybot.polymarket import BookLevel, Market, OrderBook  # noqa: E402

import replay as R                                   # noqa: E402


def _market(close_ts: int) -> Market:
    end = dt.datetime.fromtimestamp(close_ts, dt.timezone.utc)
    return Market(slug="btc-updown-5m-x", family="5m", question="q", condition_id="c",
                  up_token_id="UP", down_token_id="DN",
                  start_date=end - dt.timedelta(seconds=300), end_date=end,
                  accepting_orders=True, closed=False, active=True,
                  enable_order_book=True, order_min_size=5.0, tick_size=0.01, raw={})


def _book(levels):
    if levels is None:
        return None
    return OrderBook(token_id="T",
                     bids=[],
                     asks=[BookLevel(p, s) for p, s in levels],
                     fetched_at=0.0)


def _ladder(rng, n=None, allow_unsorted=False):
    n = rng.randint(0, 6) if n is None else n
    ps = sorted(round(rng.uniform(0.01, 1.05), 4) for _ in range(n))
    if allow_unsorted and n > 1 and rng.random() < 0.15:
        rng.shuffle(ps)
    return [(p, round(rng.choice([0.0, rng.uniform(0.01, 5000.0)]), 4)) for p in ps]


def test_fair_value(n, rng):
    bad = 0
    for _ in range(n):
        S_open = rng.choice([rng.uniform(1e4, 2e5), 0.0, -5.0, float("nan"), float("inf")])
        S_t = rng.choice([S_open * math.exp(rng.gauss(0, 3e-4)) if S_open > 0 else 1.0,
                          0.0, float("nan")])
        sig = rng.choice([rng.uniform(1e-7, 1e-3), 0.0, -1e-5, float("nan")])
        tau = rng.choice([rng.uniform(-2, 10), 0.0, float("nan")])
        a = R.fair_value_up_port(S_t, S_open, sig, tau)
        b = ST.fair_value_up(S_t, S_open, sig, tau)
        if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-15):
            bad += 1
    return bad


def test_tau_bounds(n, rng):
    bad = 0
    for _ in range(n):
        cfg = {"snipe_last_secs": rng.choice([4, 5, 6, 8.0])}
        if rng.random() < 0.8:
            cfg["snipe_min_tau_secs"] = rng.choice([2.0, 2.5, 3.0])
        if rng.random() < 0.8:
            cfg["snipe_fill_margin_secs"] = rng.choice([0.0, 0.5, 1.0])
        lat = rng.choice([0, 500, 1500, 3000, 5000])
        if R.snipe_tau_bounds_port(cfg, lat) != ST.snipe_tau_bounds(cfg, lat):
            bad += 1
    return bad


def test_walk(n, rng):
    bad = 0
    for _ in range(n):
        lv = _ladder(rng, allow_unsorted=True)
        fair = rng.uniform(0.0, 1.0)
        fee_rate = rng.choice([0.0, 0.07, 0.10])
        edge_min = rng.choice([0.0, 0.02, 0.03, 0.05])
        pmin, pmax = 0.30, 0.99
        cap = rng.choice([25.0, 250.0, 0.0, 1e9])
        mab = rng.choice([None, 0.03, 0.10])

        def ef(p, _f=fair, _r=fee_rate):
            return _f - p - FE.fee_per_share(p, _r)

        a = R.walk_asks_port([R.Level(p, s) for p, s in lv], ef, edge_min, pmin, pmax,
                             cap, fee_rate, max_above_best=mab)
        b = FE.walk_asks([BookLevel(p, s) for p, s in lv], ef, edge_min, pmin, pmax,
                         cap, fee_rate, max_above_best=mab)
        if len(a.fills) != len(b.fills):
            bad += 1
            continue
        for x, y in zip(a.fills, b.fills):
            if (abs(x.price - y.price) > 1e-15 or abs(x.shares - y.shares) > 1e-12
                    or abs(x.fee_per_share - y.fee_per_share) > 1e-15):
                bad += 1
                break
        else:
            if (abs(a.total_shares - b.total_shares) > 1e-12
                    or abs(a.total_cost - b.total_cost) > 1e-12
                    or abs(a.total_fees - b.total_fees) > 1e-12):
                bad += 1
    return bad


def test_evaluate(n, rng):
    bad, fired = 0, 0
    for _ in range(n):
        close_ts = 1_780_000_000 + rng.randint(0, 10_000) * 300
        tau = rng.choice([rng.uniform(-1, 8), 0.0])
        now = close_ts - tau
        S_open = rng.uniform(1e4, 2e5)
        S_t = S_open * math.exp(rng.gauss(0, 4e-4))
        if rng.random() < 0.05:
            S_t = rng.choice([0.0, float("nan")])
        sigma = rng.choice([rng.uniform(1e-7, 3e-4), 0.0, float("nan")])
        cfg = {"edge_min": rng.choice([0.02, 0.03, 0.05]),
               "price_min": 0.30, "price_max": 0.99,
               "sigma_1s_floor": rng.choice([8e-6, 3e-5]),
               "fair_cap": rng.choice([0.98, 0.99, 1.0])}
        fee_rate = rng.choice([0.0, 0.07, 0.10])
        lu = None if rng.random() < 0.2 else _ladder(rng)
        ld = None if rng.random() < 0.2 else _ladder(rng)
        # None inputs (the "no trustworthy oracle this tick" path)
        st_in = None if rng.random() < 0.05 else S_t
        so_in = None if rng.random() < 0.05 else S_open
        sg_in = None if rng.random() < 0.05 else sigma

        a = R.evaluate_close_snipe_port(close_ts, now, st_in, so_in, sg_in,
                                        None if lu is None else [R.Level(p, s) for p, s in lu],
                                        None if ld is None else [R.Level(p, s) for p, s in ld],
                                        cfg, fee_rate)
        b = ST.evaluate_close_snipe(_market(close_ts), now, st_in, so_in, sg_in,
                                    _book(lu), _book(ld), cfg, fee_rate)
        if (a is None) != (b is None):
            bad += 1
            continue
        if a is None:
            continue
        fired += 1
        if (a.side != b.side or abs(a.fair - b.fair) > 1e-15
                or abs(a.ask - b.ask) > 1e-15 or abs(a.edge - b.edge) > 1e-15
                or abs(a.tau_secs - b.tau_secs) > 1e-12):
            bad += 1
    return bad, fired


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    rng = random.Random(20260727)
    print(f"randomised inputs per test: {n}\n")
    r1 = test_fair_value(n, rng)
    print(f"fair_value_up_port      vs strategy.fair_value_up        : {r1} mismatches")
    r2 = test_tau_bounds(n, rng)
    print(f"snipe_tau_bounds_port   vs strategy.snipe_tau_bounds     : {r2} mismatches")
    r3 = test_walk(n, rng)
    print(f"walk_asks_port          vs fill_engine.walk_asks         : {r3} mismatches")
    r4, fired = test_evaluate(n, rng)
    print(f"evaluate_close_snipe_port vs strategy.evaluate_close_snipe: {r4} mismatches "
          f"({fired} of {n} inputs produced a signal, so the positive path is exercised)")
    total = r1 + r2 + r3 + r4
    print(f"\nTOTAL MISMATCHES: {total}")
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    main()
