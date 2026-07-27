#!/usr/bin/env python3
"""M1: fetch the multi-coin HOURLY up/down history into data/multicoin/.

Seven coins run an hourly Up/Down family on Polymarket (bitcoin, ethereum,
solana, xrp, dogecoin, bnb, hype).  All seven resolve on the BINANCE
<COIN>/USDT 1H candle (close >= open => Up) -- verified per-family from each
market's own `description` / `resolution_source`, see audit/M1_multicoin_recon.md.
HYPE is the exception that matters: its resolution source is the Binance
*perpetual futures* pair (https://www.binance.com/en/futures/HYPEUSDT); there is
no HYPE/USDT spot pair on Binance at all.

Resumable and day-granular: every stage skips work whose output file already
exists, so re-running is cheap and safe.

Stages (subcommands):
  markets   hourly market metadata for all 7 coins -> data/multicoin/markets.parquet
            (includes result_id, so the settle is known without re-deriving it)
  quotes    top-of-book tape, both outcomes, [close-120s, close+5s]
            -> data/multicoin/quotes/<coin>/<D>.parquet
  binance   underlying 1s series per USDT pair -> data/multicoin/binance/<SYM>/<D>.parquet
            spot 1s klines for the 6 spot pairs; HYPE is rebuilt from futures
            aggTrades (1s klines are NOT published for futures).
  verify    re-derive the Up/Down settle from the fetched Binance candles and
            compare against Telonex result_id.  This is the safety gate: it
            proves the feed each coin is wired to is the feed it resolves on.

FRESHNESS: every quote row keeps the vendor event timestamp (`timestamp_us`,
the book's own last-update instant) next to `local_timestamp_us` (vendor
capture instant).  Book age at a decision instant is decision - timestamp_us.
Nothing is forward-filled here.

DATE KEY: an hourly window closes on an exact UTC hour boundary, so the whole
[close-120s, close] decision band lies inside date(close_s - 1).  Files are
keyed by that date.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/multicoin"
CA = "/root/.ccr/ca-bundle.crt"
BASE = "https://api.telonex.io"

# api.binance.com is 451-blocked in this sandbox; these two mirrors are not.
BINANCE_REST = "https://data-api.binance.vision"   # spot REST, same API paths
BINANCE_VISION = "https://data.binance.vision"     # bulk daily zips (spot + futures)

load_dotenv(f"{ROOT}/.env")
KEY = os.environ["TELONEX_API_KEY"]  # never printed

# coin (polymarket slug prefix) -> (binance symbol, market kind)
COINS = {
    "bitcoin":  ("BTCUSDT",  "spot"),
    "ethereum": ("ETHUSDT",  "spot"),
    "solana":   ("SOLUSDT",  "spot"),
    "xrp":      ("XRPUSDT",  "spot"),
    "dogecoin": ("DOGEUSDT", "spot"),
    "bnb":      ("BNBUSDT",  "spot"),
    # HYPE resolves on Binance USD-M FUTURES; no spot pair exists.
    "hype":     ("HYPEUSDT", "futures"),
}

_tl = threading.local()


def client() -> httpx.Client:
    c = getattr(_tl, "c", None)
    if c is None:
        c = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=30.0),
            verify=CA,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {KEY}"},
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=6),
        )
        _tl.c = c
    return c


def plain() -> httpx.Client:
    """No-auth client for Binance (never send the Telonex key to a third party)."""
    c = getattr(_tl, "p", None)
    if c is None:
        c = httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0), verify=CA,
                         follow_redirects=True)
        _tl.p = c
    return c


def get_parquet(channel: str, date: str, params: dict, columns=None, tries: int = 5):
    """GET one Telonex day-file. Returns (df|None, status_string)."""
    url = f"{BASE}/v1/downloads/polymarket/{channel}/{date}"
    for attempt in range(tries):
        try:
            r = client().get(url, params=params)
            if r.status_code == 404:
                return None, "404"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            if not r.content:
                return None, "empty"
            return pd.read_parquet(io.BytesIO(r.content), columns=columns), "ok"
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                return None, f"err:{type(e).__name__}"
            time.sleep(1.0 + attempt)
    return None, "exhausted"


def daterange(a: str, b: str):
    d0, d1 = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    return out


def f32(df, cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    return df


# -------------------------------------------------------------------- markets
RAW = f"{OUT}/telonex_hourly_markets_raw.parquet"


def stage_markets(refresh: bool):
    if refresh or not os.path.exists(RAW):
        from telonex import get_markets_dataframe
        m = get_markets_dataframe(exchange="polymarket")
        m = m[m.slug.str.contains("-up-or-down-", na=False)].copy()
        m.to_parquet(RAW, index=False)
    m = pd.read_parquet(RAW)
    m["coin"] = m.slug.str.extract(r"^([a-z0-9]+)-up-or-down-[a-z]+-\d{1,2}-\d{4}-\d{1,2}(?:am|pm)-et$")[0]
    m = m[m.coin.isin(COINS)].copy()
    m["close_s"] = (m.end_date_us // 1_000_000).astype("int64")
    m["open_s"] = m["close_s"] - 3600          # the 1H Binance candle open
    m["d"] = pd.to_datetime(m["close_s"] - 1, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    m["symbol"] = m.coin.map(lambda c: COINS[c][0])
    m["venue"] = m.coin.map(lambda c: COINS[c][1])
    # every window must be exactly one hour and land on an hour boundary
    bad = int((m["close_s"] % 3600 != 0).sum())
    cols = ["coin", "symbol", "venue", "slug", "market_id", "asset_id_0", "asset_id_1",
            "outcome_0", "outcome_1", "status", "result_id",
            "start_date_us", "end_date_us", "settled_at_us", "close_s", "open_s", "d",
            "resolution_source", "quotes_from", "quotes_to",
            "book_snapshot_5_from", "book_snapshot_5_to"]
    m = m[[c for c in cols if c in m.columns]].sort_values(["close_s", "coin"])
    m.reset_index(drop=True).to_parquet(f"{OUT}/markets.parquet", index=False)
    print(f"[markets] {len(m)} rows, coins={m.coin.nunique()}, days={m.d.nunique()}, "
          f"resolved={(m.status=='resolved').sum()}, off-hour-boundary={bad}", flush=True)
    return m


def load_markets():
    return pd.read_parquet(f"{OUT}/markets.parquet")


# --------------------------------------------------------------------- quotes
QCOLS = ["timestamp_us", "local_timestamp_us", "bid_price", "bid_size", "ask_price", "ask_size"]
QUOTE_PRE, QUOTE_POST = 120, 5   # seconds kept around each close


def _trim(df, close_s, pre, post):
    lo, hi = (close_s - pre) * 1_000_000, (close_s + post) * 1_000_000
    keep = df[(df.timestamp_us >= lo) & (df.timestamp_us <= hi)]
    pcut = df[df.timestamp_us < lo]
    if len(pcut):                       # keep the last update BEFORE the cut so
        keep = pd.concat([pcut.tail(1), keep], ignore_index=True)   # staleness stays exact
    return keep


def stage_quotes(days, coins, workers):
    mk = load_markets()
    for coin in coins:
        os.makedirs(f"{OUT}/quotes/{coin}", exist_ok=True)
    todo = [(c, d) for c in coins for d in days
            if not os.path.exists(f"{OUT}/quotes/{c}/{d}.parquet")]
    print(f"[quotes] {len(todo)} (coin,day) cells to fetch", flush=True)
    for coin, d in todo:
        sub = mk[(mk.coin == coin) & (mk.d == d)]
        if sub.empty:
            print(f"  quotes {coin} {d} NO MARKETS", flush=True)
            continue
        t0 = time.time()
        parts, fails = [], []
        lock = threading.Lock()

        def one(row, oid):
            df, st = get_parquet("quotes", d, {"slug": row.slug, "outcome_id": oid},
                                 columns=QCOLS)
            if df is None or df.empty:
                return (st if df is None else "empty"), row.slug, oid
            df = _trim(df.sort_values("timestamp_us"), row.close_s, QUOTE_PRE, QUOTE_POST).copy()
            if df.empty:
                return "notape", row.slug, oid
            df = f32(df, ["bid_price", "bid_size", "ask_price", "ask_size"])
            df["timestamp_us"] = df["timestamp_us"].astype("int64")
            df["local_timestamp_us"] = df["local_timestamp_us"].astype("int64")
            df["close_s"] = np.int64(row.close_s)
            df["oid"] = np.int8(oid)
            with lock:
                parts.append(df)
            return "ok", row.slug, oid

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, r, oid) for r in sub.itertuples() for oid in (0, 1)]
            for f in as_completed(futs):
                st, slug, oid = f.result()
                if st != "ok":
                    fails.append((st, slug, oid))
        if not parts:
            print(f"  quotes {coin} {d} ALL FAILED {fails[:3]}", flush=True)
            continue
        allq = pd.concat(parts, ignore_index=True).sort_values(["close_s", "oid", "timestamp_us"])
        allq.reset_index(drop=True).to_parquet(f"{OUT}/quotes/{coin}/{d}.parquet", index=False)
        print(f"  quotes {coin} {d} rows={len(allq)} mkts={len(sub)} fails={len(fails)} "
              f"{fails[:2]} {time.time()-t0:.0f}s", flush=True)


# -------------------------------------------------------------------- binance
def _spot_1s_day(sym, d):
    """Daily 1s-kline zip from data.binance.vision (spot)."""
    url = f"{BINANCE_VISION}/data/spot/daily/klines/{sym}/1s/{sym}-1s-{d}.zip"
    r = plain().get(url)
    if r.status_code == 404:
        return None, "404"
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name), header=None, usecols=[0, 1, 2, 3, 4, 5],
                         names=["open_time", "open", "high", "low", "close", "volume"])
    # Binance switched open_time to microseconds in newer archives; normalise to ms.
    if df.open_time.max() > 1e15:
        df["open_time"] = df.open_time // 1000
    df["ts_ms"] = df.open_time.astype("int64")
    return df[["ts_ms", "open", "high", "low", "close", "volume"]], "ok"


def _fut_1s_day(sym, d):
    """Futures has NO 1s klines -- rebuild 1s bars from USD-M aggTrades."""
    url = f"{BINANCE_VISION}/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{d}.zip"
    r = plain().get(url)
    if r.status_code == 404:
        return None, "404"
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        raw = pd.read_csv(z.open(name))
    cols = {c.lower(): c for c in raw.columns}
    tcol = cols.get("transact_time") or cols.get("time")
    raw = raw.rename(columns={tcol: "ts", cols["price"]: "price", cols["quantity"]: "qty"})
    raw["ts_ms"] = (raw.ts.astype("int64") // 1000) * 1000
    g = raw.groupby("ts_ms")
    df = pd.DataFrame({
        "open": g.price.first(), "high": g.price.max(),
        "low": g.price.min(), "close": g.price.last(), "volume": g.qty.sum(),
    }).reset_index()
    return df[["ts_ms", "open", "high", "low", "close", "volume"]], "ok"


def stage_binance(days, coins, workers):
    syms = sorted({COINS[c] for c in coins})
    todo = []
    for sym, venue in syms:
        os.makedirs(f"{OUT}/binance/{sym}", exist_ok=True)
        todo += [(sym, venue, d) for d in days
                 if not os.path.exists(f"{OUT}/binance/{sym}/{d}.parquet")]
    print(f"[binance] {len(todo)} (symbol,day) files to fetch", flush=True)

    def one(job):
        sym, venue, d = job
        try:
            df, st = (_fut_1s_day if venue == "futures" else _spot_1s_day)(sym, d)
        except Exception as e:  # noqa: BLE001
            return sym, d, f"err:{type(e).__name__}", 0
        if df is None:
            return sym, d, st, 0
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        df = df.sort_values("ts_ms").reset_index(drop=True)
        df.to_parquet(f"{OUT}/binance/{sym}/{d}.parquet", index=False)
        return sym, d, "ok", len(df)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(one, j) for j in todo]):
            sym, d, st, n = f.result()
            print(f"  binance {sym} {d} {st} rows={n}", flush=True)


# --------------------------------------------------------------------- verify
def stage_verify(days, coins):
    """Re-derive Up/Down from the fetched Binance 1s bars; compare to result_id.

    This is the safety gate.  A coin that does not reconcile at ~100% is wired
    to the wrong feed and must NOT be traded.

    ANCHOR (this is subtle and got it wrong once): Binance's 1H candle open is
    the first *traded* price in [open, close) and its close is the last traded
    price.  The 1s-kline archive back-fills seconds with NO trades by carrying
    the previous close at volume 0.  Taking the bar that merely sits at open_s
    therefore returns the carried price, not the hour's first trade, and it
    disagreed with the real 1H candle on 3 of 5,873 hours -- every one of them
    a case where the hour opened on an empty second.  Only bars with
    volume > 0 are real prints, so the anchors are taken from those.
    """
    mk = load_markets()
    mk = mk[(mk.status == "resolved") & mk.d.isin(set(days)) & mk.coin.isin(coins)]
    rows = []
    for coin in coins:
        sym, _ = COINS[coin]
        sub = mk[mk.coin == coin]
        if sub.empty:
            continue
        # load only the days we need, plus the day the window opens on
        need = sorted(set(sub.d) | set(pd.to_datetime(sub.open_s, unit="s", utc=True)
                                       .dt.strftime("%Y-%m-%d")))
        frames = []
        for d in need:
            p = f"{OUT}/binance/{sym}/{d}.parquet"
            if os.path.exists(p):
                frames.append(pd.read_parquet(p))
        if not frames:
            rows.append((coin, sym, 0, 0, float("nan"), "no binance data"))
            continue
        k = pd.concat(frames, ignore_index=True).sort_values("ts_ms")
        k = k[k.volume > 0]                                # real prints only
        ts = k.ts_ms.to_numpy()
        op, cl = k.open.to_numpy(), k.close.to_numpy()
        n = agree = nodata = ties = 0
        for r in sub.itertuples():
            lo, hi = r.open_s * 1000, r.close_s * 1000
            i = np.searchsorted(ts, lo, "left")            # first PRINT at/after open
            j = np.searchsorted(ts, hi, "left") - 1        # last PRINT before close
            if i >= len(ts) or j < 0 or ts[i] >= hi or ts[j] < lo:
                nodata += 1
                continue
            n += 1
            if cl[j] == op[i]:
                ties += 1
            derived = 0 if cl[j] >= op[i] else 1           # outcome_0 == "Up"
            if derived == int(r.result_id):
                agree += 1
        acc = agree / n if n else float("nan")
        rows.append((coin, sym, n, nodata, ties, acc,
                     "OK" if acc > 0.995 else "MISMATCH"))
    out = pd.DataFrame(rows, columns=["coin", "symbol", "n_checked", "n_nodata",
                                      "n_exact_ties", "settle_agreement", "verdict"])
    out.to_csv(f"{OUT}/verify_settle.csv", index=False)
    print(out.to_string(index=False), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["markets", "quotes", "binance", "verify", "all"])
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (default: end-34d)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--refresh-markets", action="store_true")
    a = ap.parse_args()

    end = a.end or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    start = a.start or (dt.date.fromisoformat(end) - dt.timedelta(days=34)).isoformat()
    days = daterange(start, end)
    coins = [c for c in a.coins.split(",") if c in COINS]
    print(f"days {days[0]}..{days[-1]} ({len(days)}) coins={coins}", flush=True)

    os.makedirs(OUT, exist_ok=True)
    if a.stage in ("markets", "all") or not os.path.exists(f"{OUT}/markets.parquet"):
        stage_markets(a.refresh_markets)
    if a.stage in ("binance", "all"):
        stage_binance(days, coins, a.workers)
    if a.stage in ("quotes", "all"):
        stage_quotes(days, coins, a.workers)
    if a.stage in ("verify", "all"):
        stage_verify(days, coins)
    return 0


if __name__ == "__main__":
    sys.exit(main())
