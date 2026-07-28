#!/usr/bin/env python3
"""Re-measure the M1 §6.3 book-fetch latency claim and store the artifact.

The verifier's one un-reproducible M1 claim: "GET /book x1 = 417ms, GET /book
x14 sequential = 5,492ms, batched POST /books x14 = 344ms" was a live-network
measurement with nothing written to disk. This script re-measures it, writes
the raw per-trial timings to data/multicoin/book_latency.csv, and also records
whether the CLOB book payload carries a usable last-update `timestamp` (needed
for the max_book_age_s guard).

Usage:  python3 scripts/multicoin/measure_book_latency.py [n_trials]
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = REPO / "data" / "multicoin" / "book_latency.csv"

COINS = ["bitcoin", "ethereum", "solana", "xrp", "dogecoin", "bnb", "hype"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def _ca() -> object:
    for p in (os.environ.get("POLYBOT_CA_BUNDLE"), "/root/.ccr/ca-bundle.crt"):
        if p and Path(p).exists():
            return p
    return True


def hourly_slug(coin: str, et) -> str:
    h12 = et.hour % 12 or 12
    ampm = "am" if et.hour < 12 else "pm"
    return (f"{coin}-up-or-down-{MONTHS[et.month - 1]}-{et.day}-{et.year}-"
            f"{h12}{ampm}-et")


def main() -> int:
    import datetime as dt
    from zoneinfo import ZoneInfo

    n_trials = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    s = requests.Session()
    s.verify = _ca()

    # ---- collect live token ids across every hourly coin --------------------
    et_now = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    et_hour = et_now.replace(minute=0, second=0, microsecond=0)
    tokens: list[tuple[str, str]] = []   # (coin, token_id)
    for coin in COINS:
        for i in (0, 1):
            slug = hourly_slug(coin, et_hour + dt.timedelta(hours=i))
            try:
                r = s.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=15)
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                print(f"  {slug}: gamma error {exc}")
                continue
            if not data:
                continue
            tid = json.loads(data[0]["clobTokenIds"])
            tokens.append((coin, tid[0]))
            tokens.append((coin, tid[1]))
            break
    print(f"live tokens found: {len(tokens)} across "
          f"{len(set(c for c, _ in tokens))} coins")
    if not tokens:
        print("no live markets found; cannot measure")
        return 1
    ids = [t for _, t in tokens]

    rows = []

    # ---- 1. single GET /book -------------------------------------------------
    for k in range(n_trials):
        t0 = time.perf_counter()
        r = s.get(f"{CLOB}/book", params={"token_id": ids[0]}, timeout=15)
        ms = (time.perf_counter() - t0) * 1000
        rows.append({"trial": k, "method": "get_book_single", "n_tokens": 1,
                     "ms": round(ms, 1), "ok": r.status_code == 200})

    # ---- 2. sequential GET /book for every token ----------------------------
    for k in range(n_trials):
        t0 = time.perf_counter()
        ok = True
        for tid in ids:
            r = s.get(f"{CLOB}/book", params={"token_id": tid}, timeout=15)
            ok = ok and r.status_code == 200
        ms = (time.perf_counter() - t0) * 1000
        rows.append({"trial": k, "method": "get_book_sequential", "n_tokens": len(ids),
                     "ms": round(ms, 1), "ok": ok})

    # ---- 3. batched POST /books ---------------------------------------------
    body = [{"token_id": tid} for tid in ids]
    batch_sample = None
    for k in range(n_trials):
        t0 = time.perf_counter()
        r = s.post(f"{CLOB}/books", json=body, timeout=15)
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 200
        if ok and batch_sample is None:
            batch_sample = r.json()
        rows.append({"trial": k, "method": "post_books_batch", "n_tokens": len(ids),
                     "ms": round(ms, 1), "ok": ok})

    # ---- 4. batched POST /books for ONE market's two tokens ------------------
    for k in range(n_trials):
        t0 = time.perf_counter()
        r = s.post(f"{CLOB}/books", json=[{"token_id": ids[0]}, {"token_id": ids[1]}],
                   timeout=15)
        ms = (time.perf_counter() - t0) * 1000
        rows.append({"trial": k, "method": "post_books_batch_2", "n_tokens": 2,
                     "ms": round(ms, 1), "ok": r.status_code == 200})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["trial", "method", "n_tokens", "ms", "ok"])
        w.writeheader()
        w.writerows(rows)

    print(f"\nwrote {OUT}")
    print(f"{'method':<24} {'n':>3} {'median_ms':>10} {'min':>8} {'max':>8}")
    for meth in ("get_book_single", "get_book_sequential", "post_books_batch",
                 "post_books_batch_2"):
        v = [r["ms"] for r in rows if r["method"] == meth and r["ok"]]
        if not v:
            print(f"{meth:<24} {'-':>3} {'FAILED':>10}")
            continue
        n = [r["n_tokens"] for r in rows if r["method"] == meth][0]
        print(f"{meth:<24} {n:>3} {statistics.median(v):>10.1f} {min(v):>8.1f} "
              f"{max(v):>8.1f}")

    # ---- 5. does the payload carry a last-update timestamp? -----------------
    print("\n--- book payload shape (batch) ---")
    if batch_sample:
        one = batch_sample[0] if isinstance(batch_sample, list) else batch_sample
        print("keys:", sorted(one.keys()))
        ts = one.get("timestamp")
        print("timestamp:", ts, type(ts).__name__)
        if ts:
            age = time.time() - float(ts) / 1000.0
            print(f"implied book age at fetch: {age:.2f}s")
        print("tick_size:", one.get("tick_size"), " min_order_size:",
              one.get("min_order_size"))
        print("n entries returned:", len(batch_sample) if isinstance(batch_sample, list) else 1,
              "for", len(ids), "requested")
    r = s.get(f"{CLOB}/book", params={"token_id": ids[0]}, timeout=15)
    print("\n--- book payload shape (single GET) ---")
    print("keys:", sorted(r.json().keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
