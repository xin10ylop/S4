#!/usr/bin/env python3
"""LIVE end-to-end smoke test for ChainlinkOracle. Not a unit test — it hits
the real network and is never run by pytest.

Proves, in one screen, that the wired oracle works against production:
  * connects to Polymarket RTDS and receives the Chainlink Data Streams
    BTC/USD report, with the real publish lag measured
  * cross-checks it against the independent GMX-relayed SIGNED report
    (asserting feedId == the mainnet BTC/USD stream)
  * prints Binance BTC/USDT alongside, so the Chainlink-vs-Binance basis is
    visible rather than assumed
  * pulls the live Polymarket 5m market and prints its implied probability,
    next to what the oracle says the window has done so far

    cd bot && python3 scripts/smoke_chainlink.py [--secs 40]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.config import load_config
from polybot.oracle import (CHAINLINK_BTC_USD_FEED_ID, BinanceOracle,
                            ChainlinkOracle, read_onchain_aggregator)
from polybot.polymarket import ClobClientREST, GammaClient, discover_markets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=40.0, help="how long to stream")
    ap.add_argument("--no-window", action="store_true",
                    help="skip the ~5min full-window strike/settle follow")
    args = ap.parse_args()

    config = load_config()
    cl = ChainlinkOracle(config)
    binance = BinanceOracle(config)

    print("=" * 78)
    print("LIVE SMOKE TEST — ChainlinkOracle")
    print(f"  ws       {cl.ws_url}  topic={cl.topic}  symbol={cl.symbol}")
    print(f"  feed_id  {cl.feed_id}")
    print(f"  match mainnet BTC/USD Data Stream: "
          f"{cl.feed_id == CHAINLINK_BTC_USD_FEED_ID}")
    print("=" * 78)

    cl.start()
    t0 = time.time()
    while cl.latest() is None and time.time() - t0 < 30:
        time.sleep(0.25)
    if cl.latest() is None:
        print("FAILED: no Chainlink print within 30s")
        print("health:", cl.health())
        cl.stop()
        return 1
    print(f"first fresh print after {time.time() - t0:.2f}s\n")

    print(f"streaming for {args.secs:.0f}s "
          f"(obs_sec = Chainlink observation second, lag = now - obs_sec)")
    lags, seen = [], set()
    end = time.time() + args.secs
    while time.time() < end:
        pt = cl.latest()
        if pt is not None and pt.ts not in seen:
            seen.add(pt.ts)
            lag = time.time() - pt.ts
            lags.append(lag)
            if len(seen) <= 5 or len(seen) % 10 == 0:
                s = cl.latest_raw()
                print(f"  obs_sec={int(pt.ts)}  price={pt.price:,.6f}  "
                      f"lag={lag:.3f}s  wei={s.wei}  src={s.source}")
        time.sleep(0.05)

    lags.sort()
    p50 = lags[len(lags) // 2] if lags else float("nan")
    p90 = lags[int(len(lags) * 0.9)] if lags else float("nan")

    cl_pt = cl.latest()
    b_px = binance.fetch_price()

    print("\n" + "-" * 78)
    print("PRICE COMPARISON (same instant)")
    print("-" * 78)
    print(f"  Chainlink BTC/USD (Data Streams, resolution source) : "
          f"${cl_pt.price:,.6f}   obs_sec={int(cl_pt.ts)} lag={time.time()-cl_pt.ts:.2f}s")
    print(f"  Binance   BTC/USDT (spot, NOT the resolution source): "
          f"${b_px:,.2f}" if b_px else "  Binance: unavailable")
    if b_px:
        d = cl_pt.price - b_px
        print(f"  basis (Chainlink - Binance)                         : "
              f"${d:+,.2f}   ({d / b_px * 1e4:+.2f} bp)")

    # independent cross-check: the SIGNED report relayed by GMX
    obs = cl.poll_standby_once()
    print(f"\n  GMX signed-report cross-check: obs_sec={obs} "
          f"(feedId asserted == mainnet BTC/USD)  disagreements={cl.n_disagreements}")

    oc = read_onchain_aggregator(
        (cl._cfg.get("onchain_check") or {}).get(
            "rpc_url", "https://polygon-bor-rpc.publicnode.com"),
        (cl._cfg.get("onchain_check") or {}).get(
            "feed_address", "0xc907e116054Ad103354f2D350FD2514433D57F6f"))
    if oc:
        print(f"  Polygon on-chain aggregator (liveness only)  : "
              f"${oc['price']:,.8f}  answer age {oc['age_secs']:.1f}s "
              f"(33.8s cadence — never a signal)")

    print(f"\n  RTDS lag over {len(lags)} prints: p50={p50:.3f}s p90={p90:.3f}s")
    print(f"  health: {cl.health()}")

    # ---------------------------------------------------------------- 5m market
    print("\n" + "-" * 78)
    print("LIVE POLYMARKET 5m MARKET")
    print("-" * 78)
    gamma = GammaClient(config)
    clob = ClobClientREST(config)

    def implied_p_up(m):
        """Market-implied P(Up). These books are frequently one-sided near a
        certain close, so take whichever quote exists: UP mid when two-sided,
        else the tightest bound from UP bid / (1 - DOWN ask)."""
        bu = clob.get_book(m.up_token_id)
        bd = clob.get_book(m.down_token_id)
        u_bid = bu.best_bid.price if (bu and bu.best_bid) else None
        u_ask = bu.best_ask.price if (bu and bu.best_ask) else None
        d_bid = bd.best_bid.price if (bd and bd.best_bid) else None
        d_ask = bd.best_ask.price if (bd and bd.best_ask) else None
        lo = max([x for x in (u_bid, (1 - d_ask) if d_ask is not None else None)
                  if x is not None] or [float("nan")])
        hi = min([x for x in (u_ask, (1 - d_bid) if d_bid is not None else None)
                  if x is not None] or [float("nan")])
        p = (lo + hi) / 2 if (lo == lo and hi == hi) else (lo if lo == lo else hi)
        return p, u_bid, u_ask, d_bid, d_ask

    def pick_5m():
        ms = discover_markets(gamma, config)
        return sorted([m for m in ms.values()
                       if m.family == "5m" and m.close_ts > time.time()],
                      key=lambda m: m.close_ts)

    fives = pick_5m()
    if not fives:
        print("  no open 5m market found right now")
        cl.stop()
        return 1

    m = fives[0]
    p, u_bid, u_ask, d_bid, d_ask = implied_p_up(m)
    print(f"  slug   {m.slug}   window {m.window_start_ts} -> {m.close_ts}"
          f"   (tau={m.close_ts - time.time():.0f}s)")
    print(f"  UP  bid={u_bid} ask={u_ask}   DOWN bid={d_bid} ask={d_ask}")
    print(f"  market-implied P(Up) = {p:.4f}")

    print("\n  >>> THE THREE NUMBERS SIDE BY SIDE, RIGHT NOW <<<")
    cl_pt = cl.latest()
    print(f"    Chainlink BTC/USD (resolves this market) : ${cl_pt.price:,.6f}")
    print(f"    Binance   BTC/USDT (reference only)      : "
          f"${b_px:,.2f}" if b_px else "    Binance: unavailable")
    print(f"    Polymarket 5m implied P(Up)              : {p:.4f}   ({m.slug})")

    # --- follow one FULL window so the strike is genuinely ours --------------
    nxt = m.close_ts                      # next window opens when this one closes
    wait = nxt - time.time()
    if wait > 0 and not args.no_window:
        print(f"\n  waiting {wait:.0f}s for the next 5m window to open, so the strike"
              f" is captured live rather than read from the connect snapshot...")
        time.sleep(wait + 3)
        nm = [x for x in pick_5m() if x.window_start_ts == nxt]
        target = nm[0] if nm else None
        if target is None:
            print("  next window not listed yet; skipping the window follow")
        else:
            strike = cl.strike(target.window_start_ts)
            print(f"\n  NEW WINDOW {target.slug}")
            print(f"    strike = first Chainlink print >= {target.window_start_ts}"
                  f" : ${strike:,.6f}" if strike else "    strike unavailable")
            for tau_mark in (240, 150, 60, 10):
                delay = target.close_ts - tau_mark - time.time()
                if delay > 0:
                    time.sleep(delay)
                now2 = time.time()
                if now2 >= target.close_ts:
                    break
                cur = cl.latest()
                p2, *_ = implied_p_up(target)
                if cur and strike:
                    move = cur.price - strike
                    print(f"    t-{target.close_ts - now2:5.0f}s  chainlink="
                          f"${cur.price:,.2f}  move={move:+8.2f}  "
                          f"leaning={'UP' if move >= 0 else 'DOWN':<4}  "
                          f"market P(Up)={p2:.3f}", flush=True)
            # settle
            while time.time() < target.close_ts + 4:
                time.sleep(0.5)
            w = cl.winner(target.window_start_ts, target.close_ts)
            s_o = cl.sample_at_or_after(target.window_start_ts)
            s_c = cl.sample_at_or_after(target.close_ts)
            print(f"\n    SETTLED by our oracle: winner={w}")
            if s_o and s_c:
                print(f"      strike wei = {s_o.wei}  (obs_sec {s_o.obs_sec})")
                print(f"      settle wei = {s_c.wei}  (obs_sec {s_c.obs_sec})")
                print(f"      settle - strike = {s_c.wei - s_o.wei} wei "
                      f"= ${(s_c.wei - s_o.wei) / 1e18:+,.6f}")
            raw = gamma.get_market_by_slug(target.slug, closed=True)
            print(f"      gamma outcomePrices (ground truth): "
                  f"{raw.get('outcomePrices') if raw else 'not resolved yet'}")

    print(f"\n  sigma_1s (chainlink, 120s): {cl.rolling_log_return_std(120.0):.3e}"
          f"   vs config sigma_1s_floor={config.snipe_cfg['sigma_1s_floor']:.1e}"
          f" (tuned for Binance — MUST be recalibrated)")
    print(f"  final health: {cl.health()}")
    cl.stop()
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
