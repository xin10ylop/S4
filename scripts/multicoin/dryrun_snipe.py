#!/usr/bin/env python3
"""End-to-end DRY RUN of the M5 multi-coin decision path against LIVE data.

Waiting for a real top-of-hour close to observe the wiring would take up to an
hour, so this drives the real engine over real gamma discovery, real Binance
oracles and real CLOB books, and shifts ONE thing: the close timestamp of the
markets under test, so `tau` lands inside the 2.5-5.0 s snipe band immediately.

WHAT THIS PROVES (all of it real):
  * per-coin oracle routing — BTC priced off BTCUSDT, ETH off ETHUSDT
  * batched POST /books against the live CLOB
  * the stale-book guard reading the CLOB's own timestamp
  * the coin allowlist and the shadow gate
  * a full paper fill through the 1.5 s latency + book re-fetch

WHAT THIS DOES NOT PROVE, and must not be read as: anything about P&L. The book
is priced for a close ~an hour away while the model is told the close is 3.5 s
away, so any "edge" printed here is an artifact of the shifted clock. This is a
WIRING smoke test. Edge evidence lives in audit/M3_multicoin_backtest.md.

Everything runs in PAPER mode into a temp directory.

Usage:  python3 scripts/multicoin/dryrun_snipe.py [tmpdir]
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bot"))

import yaml  # noqa: E402

from polybot.config import Config  # noqa: E402
from polybot.engine import Engine  # noqa: E402
from polybot.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    tmp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/polybot_dryrun")
    tmp.mkdir(parents=True, exist_ok=True)

    raw = yaml.safe_load(open(REPO / "bot" / "config.yaml"))
    raw["storage"] = {"sqlite_path": str(tmp / "d.db"), "fills_csv": str(tmp / "f.csv"),
                      "pnl_csv": str(tmp / "p.csv")}
    raw["logging"] = dict(raw["logging"], file=str(tmp / "dryrun.log"))
    raw["risk"]["override_path"] = str(tmp / "override.json")
    raw["status"]["http_port_default"] = 8913
    cfg = Config(raw=raw, path=REPO / "bot" / "config.yaml")
    assert cfg.paper, "refusing to dry-run outside paper mode"
    setup_logging(cfg)

    eng = Engine(cfg)
    print("=" * 78)
    print("DRY RUN — M5 multi-coin decision path against LIVE gamma / CLOB / Binance")
    print("=" * 78)
    print(f"mode            : {'PAPER' if cfg.paper else 'LIVE'}")
    print(f"discover coins  : {cfg.hourly_coins()}")
    print(f"allowed (fill)  : {cfg.allowed_coins()}")
    print(f"shadow (no fill): {cfg.shadow_coins()}")
    print(f"max_book_age_s  : {cfg.max_book_age_s}")
    for coin, o in eng._binance_oracles().items():
        print(f"oracle          : {coin:<9} -> {o.symbol}@{o.venue}  "
              f"cap=${cfg.coin_cap_usd(coin):.0f}")
    print()

    eng.start()
    try:
        # ---- 1. warm up, and MEASURE the poll rate while doing it -----------
        window = float(cfg.snipe_cfg["vol_window_secs"])
        need = int(cfg.warmup_cfg["min_uptime_secs"])
        print(f"--- warming up ({need}s; M4 guard 2) — measuring poll rate per coin ---")
        t0 = time.time()
        while time.time() - t0 < need + 3:
            time.sleep(10)
            row = "  ".join(f"{c}={o.n_samples(window)}"
                            for c, o in eng._binance_oracles().items())
            el = time.time() - t0
            print(f"  t+{el:5.0f}s  {row}")
        elapsed = time.time() - t0
        print("\n  poll rate per coin (samples inside the trailing "
              f"{window:.0f}s window):")
        for c, o in eng._binance_oracles().items():
            n = o.n_samples(window)
            print(f"    {c:<9} {n:>4} samples / {min(window, elapsed):.0f}s = "
                  f"{n / min(window, elapsed):.3f} polls/s  "
                  f"(gate needs {cfg.warmup_cfg['min_oracle_samples']})")

        wu = eng.warmup.check(eng.binance, window)
        print(f"\n  warmup: ready={wu.ready} reason={wu.reason} "
              f"samples={wu.samples}/{wu.required_samples} "
              f"uptime={wu.uptime_secs:.0f}/{wu.required_uptime_secs:.0f}s")
        if not wu.ready:
            print("  !! still cold; aborting")
            return 1

        # ---- 2. drive the real decision path --------------------------------
        # The NEXT still-open market per coin. Picking the earliest close would
        # pick one that has already closed, whose book is post-cancellation and
        # therefore uninformative about the wiring under test.
        now0 = time.time()
        hourly = [m for m in eng.markets.values()
                  if m.family == "1h" and m.close_ts > now0 + 120]
        by_coin = {}
        for m in sorted(hourly, key=lambda m: m.close_ts):
            by_coin.setdefault(m.coin, m)
        print(f"\n--- decision path on {len(by_coin)} live 1h market(s) "
              f"(close time shifted to tau=3.5s — WIRING TEST, NOT P&L) ---")

        for coin, real in by_coin.items():
            m = copy.copy(real)
            now = time.time()



            # Market.close_ts is a property over end_date, so shift end_date.
            import datetime as dt
            m.end_date = dt.datetime.fromtimestamp(now + 3.5, tz=dt.timezone.utc)
            m.start_date = dt.datetime.fromtimestamp(now + 3.5 - 3600, tz=dt.timezone.utc)

            oracle = eng._oracle_for(m)
            latest = oracle.latest() if oracle else None
            print(f"\n  [{coin}] {real.slug}")
            print(f"    oracle       : {oracle.symbol if oracle else None}"
                  f"@{oracle.venue if oracle else None}  "
                  f"last={latest.price if latest else None}")
            books = eng._fetch_books([m.up_token_id, m.down_token_id])
            for side, tid in (("UP", m.up_token_id), ("DOWN", m.down_token_id)):
                b = books.get(tid)
                if b is None:
                    print(f"    book {side:<5}: NONE")
                    continue
                age = b.age_secs()
                print(f"    book {side:<5}: best_ask="
                      f"{b.best_ask.price if b.best_ask else None} "
                      f"levels={len(b.asks)} tick={b.tick_size} "
                      f"clob_age={age if age is None else round(age, 2)}s "
                      f"stale={eng._book_too_stale(b) is not None}")
            eng._maybe_snipe(m, time.time())
            time.sleep(2.0)   # let the fill worker's 1.5s latency elapse

        # ---- 3. what the ledger and status say -------------------------------
        time.sleep(2.0)
        import sqlite3
        con = sqlite3.connect(str(tmp / "d.db"))
        print("\n--- signals recorded ---")
        rows = con.execute("SELECT market_slug, side, fair, ask, edge, meta_json "
                            "FROM signals ORDER BY id").fetchall()
        if not rows:
            print("  (none — the live books offered no edge at these prices)")
        for slug, side, fair, ask, edge, meta in rows:
            md = json.loads(meta)
            print(f"  {slug}")
            print(f"    coin={md.get('coin')} shadow={md.get('shadow')} "
                  f"book_age_s={md.get('book_age_s')} side={side} "
                  f"fair={fair:.4f} ask={ask:.4f} edge={edge:.4f}")
        print("\n--- fill attempts recorded ---")
        frows = con.execute("SELECT market_slug, outcome, shares, cost_usd "
                             "FROM fills ORDER BY id").fetchall()
        if not frows:
            print("  (none)")
        for slug, outcome, shares, cost in frows:
            print(f"  {slug:<45} outcome={outcome:<20} shares={shares:.2f} "
                  f"cost=${cost:.2f}")
        con.close()

        print("\n--- the safety assertion this dry run exists for ---")
        shadow = set(cfg.shadow_coins())
        bad = [s for s, o, sh, c in frows
               if any(s.startswith(x + "-") for x in shadow)]
        print(f"  fills dispatched for a SHADOW coin: {len(bad)} "
              f"{'<-- MUST BE 0' if bad else '(correct)'}")
        return 1 if bad else 0
    finally:
        eng.stop()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
