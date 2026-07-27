#!/usr/bin/env python3
"""Live CLOB book sampler for the multi-coin hourly up/down families.

Samples the CLOB order book for every hourly market whose close is within
--horizon seconds, writing one JSONL row per (poll, token). Cadence adapts:
1 Hz inside the last `--fast-window` seconds before the close, else `--slow`.

Purpose (M1 step 3): quantify depth / spread / staleness near the close per
coin so the close_snipe capacity and edge can be compared across coins.

Usage:
  python3 scripts/multicoin/sample_books.py --minutes 12 --out data/multicoin/books
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gamma import gamma_markets, clob_books, get, CLOB  # noqa: E402

HOURLY_RE = re.compile(
    r"^(bitcoin|ethereum|solana|xrp|dogecoin|bnb|hype)-up-or-down-"
    r"[a-z]+-\d{1,2}-\d{4}-\d{1,2}(am|pm)-et$"
)
COIN_OF = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP",
    "dogecoin": "DOGE", "bnb": "BNB", "hype": "HYPE",
}


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def discover(horizon_secs):
    """Return list of dicts: slug, coin, close_ts, up_token, down_token."""
    now = utcnow()
    iso = now.isoformat().replace("+00:00", "Z")
    found, offset = [], 0
    while offset < 800:
        try:
            batch = gamma_markets(closed="false", limit=100, offset=offset,
                                  order="endDate", ascending="true", end_date_min=iso)
        except Exception as exc:  # noqa: BLE001
            print("discover page fail", offset, exc, file=sys.stderr)
            break
        if not batch:
            break
        for m in batch:
            slug = m.get("slug") or ""
            if not HOURLY_RE.match(slug):
                continue
            end = dt.datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            if (end - now).total_seconds() > horizon_secs:
                continue
            toks = json.loads(m["clobTokenIds"])
            outs = json.loads(m["outcomes"])
            found.append({
                "slug": slug,
                "coin": COIN_OF[slug.split("-up-or-down-")[0]],
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "close_ts": end.timestamp(),
                "end_date": m["endDate"],
                "outcomes": outs,
                "tokens": toks,
                "tick": m.get("orderPriceMinTickSize"),
                "min_order_size": m.get("orderMinSize"),
                "volume": m.get("volumeNum"),
                "liquidity": m.get("liquidityNum"),
            })
        offset += len(batch)
        if len(batch) < 100:
            break
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=12.0,
                    help="how long to keep sampling (wall clock)")
    ap.add_argument("--horizon", type=float, default=900.0,
                    help="only sample markets closing within this many seconds")
    ap.add_argument("--fast-window", type=float, default=90.0,
                    help="seconds before close where cadence goes to --fast")
    ap.add_argument("--fast", type=float, default=1.0)
    ap.add_argument("--slow", type=float, default=15.0)
    ap.add_argument("--post-close", type=float, default=20.0,
                    help="keep sampling this long after the close")
    ap.add_argument("--out", default="/home/user/S4/data/multicoin/books")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    mkts = discover(args.horizon)
    if not mkts:
        print("no hourly markets inside horizon", file=sys.stderr)
        return 1
    by_close = {}
    for m in mkts:
        by_close.setdefault(m["end_date"], []).append(m)
    for k in sorted(by_close):
        print(k, [m["coin"] for m in by_close[k]], file=sys.stderr)

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    meta_path = os.path.join(args.out, f"meta_{stamp}.json")
    with open(meta_path, "w") as fh:
        json.dump(mkts, fh, indent=1)
    out_path = os.path.join(args.out, f"books_{stamp}.jsonl")

    tok_meta = {}
    for m in mkts:
        for i, t in enumerate(m["tokens"]):
            tok_meta[t] = (m["coin"], m["slug"], m["outcomes"][i], m["close_ts"])

    deadline = time.time() + args.minutes * 60.0
    n = 0
    with open(out_path, "a") as fh:
        while time.time() < deadline:
            now = time.time()
            live = [m for m in mkts if now < m["close_ts"] + args.post_close]
            if not live:
                break
            toks = [t for m in live for t in m["tokens"]]
            min_tau = min(m["close_ts"] - now for m in live)
            t0 = time.time()
            try:
                books = clob_books(toks)
            except Exception as exc:  # noqa: BLE001
                books = []
                print("books fail", exc, file=sys.stderr)
            t1 = time.time()
            for b in books:
                tid = b.get("asset_id") or b.get("token_id")
                cm = tok_meta.get(tid)
                if cm is None:
                    continue
                coin, slug, outcome, close_ts = cm
                fh.write(json.dumps({
                    "req_ts": t0, "resp_ts": t1, "rtt": t1 - t0,
                    "coin": coin, "slug": slug, "outcome": outcome,
                    "token_id": tid,
                    "tau": close_ts - t0,
                    "book_ts": b.get("timestamp"),
                    "hash": b.get("hash"),
                    "bids": b.get("bids"), "asks": b.get("asks"),
                    "min_order_size": b.get("min_order_size"),
                    "tick_size": b.get("tick_size"),
                }) + "\n")
                n += 1
            fh.flush()
            cadence = args.fast if min_tau <= args.fast_window else args.slow
            time.sleep(max(0.0, cadence - (time.time() - t0)))
    print(f"wrote {n} rows -> {out_path}", file=sys.stderr)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
