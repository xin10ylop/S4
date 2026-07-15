"""CLI entry point.

    python -m polybot.main run       # main loop (paper by default)
    python -m polybot.main status    # pretty-print status.json + pnl summary
    python -m polybot.main markets   # list currently-tracked BTC markets
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from .config import load_config
from .logging_setup import setup_logging


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Engine

    config = load_config(args.config)
    setup_logging(config)
    engine = Engine(config)

    def _handle_sigterm(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)
    engine.run_forever()
    return 0


def cmd_markets(args: argparse.Namespace) -> int:
    from .polymarket import GammaClient, discover_markets

    config = load_config(args.config)
    gamma = GammaClient(config)
    markets = discover_markets(gamma, config)
    if not markets:
        print("No BTC Up/Down markets discovered.")
        return 1

    now = time.time()
    rows = sorted(markets.values(), key=lambda m: m.close_ts)
    fam_width = 4
    slug_width = max(len(m.slug) for m in rows) + 2
    header = f"{'FAM':<{fam_width}} {'SLUG':<{slug_width}} {'CLOSES (UTC)':<21} {'T-CLOSE':>10} {'ACCEPT':>7} {'CLOSED':>7}"
    print(header)
    print("-" * len(header))
    for m in rows:
        t_close = m.close_ts - now
        sign = "-" if t_close < 0 else ""
        t_str = f"{sign}{abs(int(t_close))}s"
        print(f"{m.family:<{fam_width}} {m.slug:<{slug_width}} "
              f"{m.end_date.strftime('%Y-%m-%d %H:%M:%S'):<21} {t_str:>10} "
              f"{str(m.accepting_orders):>7} {str(m.closed):>7}")
    print(f"\n{len(rows)} markets discovered.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = config.status_json_path
    if not path.exists():
        print(f"No status.json found at {path} — is the bot running? (python -m polybot.main run)")
        return 1
    with open(path) as f:
        data = json.load(f)

    print(f"polybot status — mode={data['mode']}  uptime={data['uptime_secs']}s  "
          f"generated_at={data['generated_at']}")
    print(f"markets tracked: {data['n_markets_tracked']}   open positions: {data['n_open_positions']}")
    oracle = data.get("oracle", {})
    print(f"oracle: last_price={oracle.get('last_price')} "
          f"({oracle.get('last_update_secs_ago')}s ago)")

    pnl = data["pnl"]
    print("\n--- PnL ---")
    print(f"today:      n={pnl['today']['n']:>4}  gross=${pnl['today']['gross_pnl']:.4f}  "
          f"fees=${pnl['today']['fees_paid']:.4f}  net=${pnl['today']['net_pnl']:.4f}")
    print(f"cumulative: gross=${pnl['cumulative_gross']:.4f}  fees=${pnl['fees_paid']:.4f}  "
          f"net=${pnl['cumulative_net']:.4f}")

    m = data["metrics"]
    print("\n--- Metrics ---")
    print(f"fill attempts: {m['n_fill_attempts']}  by outcome: {m['attempts_by_outcome']}")
    print(f"resolved positions: {m['n_resolved_positions']}  win_rate="
          f"{m['win_rate']}  resolution_disagreements={m['resolution_disagreements']}")
    if m["per_strategy_family"]:
        print("\nper strategy/family:")
        for key, agg in m["per_strategy_family"].items():
            print(f"  {key:<20} n={agg['n']:>4} win_rate={agg['win_rate']} "
                  f"pnl=${agg['pnl']:.4f} ev_per_share={agg['ev_per_share']}")

    if data["open_positions"]:
        print("\n--- Open positions ---")
        for p in data["open_positions"]:
            print(f"  {p['market_slug']} [{p['strategy']}/{p['family']}] side={p['side']} "
                  f"shares={p['shares']:.2f} avg_px={p['avg_price']:.4f} cost=${p['cost_usd']:.2f}")

    if data["recent_events"]:
        print("\n--- Recent events (most recent first) ---")
        for ev in data["recent_events"][:20]:
            ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
            print(f"  {ts} [{ev['kind']}] {ev['message']}")
    return 0


def cmd_pnl(args: argparse.Namespace) -> int:
    """Per-strategy / per-family breakdown straight from the SQLite ledger."""
    import sqlite3

    config = load_config(args.config)
    db_path = config.sqlite_path
    if not db_path.exists():
        print(f"No ledger DB at {db_path} — has the bot run yet?")
        return 1
    db = sqlite3.connect(str(db_path))

    print("== SIGNALS per strategy/family ==")
    rows = list(db.execute(
        "SELECT strategy, family, COUNT(*) FROM signals GROUP BY 1,2 ORDER BY 1,2"))
    for st, fam, n in rows:
        print(f"  {st:<14} {fam:<4} signals={n}")
    if not rows:
        print("  (none yet)")

    print("\n== FILL ATTEMPTS per strategy/family/outcome ==")
    rows = list(db.execute(
        "SELECT strategy, family, outcome, COUNT(*), ROUND(SUM(shares),2), "
        "ROUND(SUM(cost_usd),2) FROM fills GROUP BY 1,2,3 ORDER BY 1,2,3"))
    for st, fam, out, n, sh, cost in rows:
        print(f"  {st:<14} {fam:<4} {out:<20} n={n:<5} shares={sh or 0:<9} cost=${cost or 0}")
    if not rows:
        print("  (none yet)")

    print("\n== REALIZED PnL per strategy/family (resolved positions) ==")
    tot: dict = {}
    for fam, pj in db.execute(
            "SELECT family, pnl_json FROM resolutions WHERE pnl_json IS NOT NULL"):
        for p in json.loads(pj):
            k = (p.get("strategy"), fam)
            agg = tot.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0, "shares": 0.0})
            agg["n"] += 1
            agg["pnl"] += p.get("realized_pnl", 0.0)
            agg["shares"] += p.get("shares", 0.0)
            if p.get("realized_pnl", 0.0) > 0:
                agg["wins"] += 1
    for (st, fam), a in sorted(tot.items()):
        evps = (a["pnl"] / a["shares"]) if a["shares"] else float("nan")
        print(f"  {st:<14} {fam:<4} resolved={a['n']:<4} win_rate={a['wins']/a['n']:.3f} "
              f"pnl=${a['pnl']:.4f} ev_per_share=${evps:.4f}")
    if not tot:
        print("  (no resolved positions yet)")

    print("\n== close_snipe evaluation activity (log) ==")
    log_path = config.log_file
    n_eval = n_sig = 0
    if log_path.exists():
        with open(log_path) as f:
            for line in f:
                if "snipe eval" in line:
                    n_eval += 1
                    if "-> SIGNAL" in line:
                        n_sig += 1
        print(f"  eval ticks logged: {n_eval}  signals: {n_sig}  "
              f"(no SIGNAL on a close = evaluated, book fairly priced — normal)")
    else:
        print(f"  (log file {log_path} not found)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m polybot.main")
    p.add_argument("--config", default=None, help="path to config.yaml (default: bot/config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the main trading loop").set_defaults(func=cmd_run)
    sub.add_parser("status", help="pretty-print status.json + pnl summary").set_defaults(func=cmd_status)
    sub.add_parser("markets", help="list currently-tracked BTC markets").set_defaults(func=cmd_markets)
    sub.add_parser("pnl", help="per-strategy/per-family PnL + attempts from the ledger DB").set_defaults(func=cmd_pnl)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
