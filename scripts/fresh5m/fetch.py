#!/usr/bin/env python3
"""C1: fetch ~20+ days of FRESH Telonex 5m BTC Up/Down data.

Resumable, day-granular. Every stage skips work whose output file already
exists, so re-running is cheap and safe.

Stages (run in this order; each is a subcommand):
  crypto   crypto_prices (Chainlink BTC/USD Data Streams) -> data/fresh5m/crypto_prices/<D>.parquet
           Canonical repo schema: timestamp_us (oracle OBSERVATION time),
           server_timestamp_us (PUBLISH time), local_timestamp_us, price:float64.
  markets  btc-updown-5m-* metadata -> data/fresh5m/markets.parquet
  quotes   top-of-book tape, both outcomes -> data/fresh5m/quotes/<D>.parquet
  prepass  cheap signal-candidate scan -> data/fresh5m/candidates.parquet
  books    book_snapshot_25 for candidates only -> data/fresh5m/books/<D>.parquet
  outage   scan saved quotes/books for frozen-book outage signatures

BOOK FRESHNESS: every saved quote/book row keeps the vendor's own event
timestamp (`timestamp_us`, the exchange/last-update instant) alongside
`local_timestamp_us` (vendor capture instant). Replay computes book age as
decision_instant - timestamp_us. Nothing is forward-filled here.

DATE KEY: a market's whole 5m window lies inside a single UTC date, namely
date(close_s - 1) (closes are on 5-minute boundaries and so is midnight, so a
window can never straddle midnight). Files are keyed by that date `d`, i.e.
day D holds the 288 windows closing in (D 00:00, D+1 00:00].
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/fresh5m"
CA = "/root/.ccr/ca-bundle.crt"
BASE = "https://api.telonex.io"
REPO_CP = f"{ROOT}/data/data/processed/daily/crypto_prices"

load_dotenv(f"{ROOT}/.env")
KEY = os.environ["TELONEX_API_KEY"]  # never printed

_tl = threading.local()


def client() -> httpx.Client:
    c = getattr(_tl, "c", None)
    if c is None:
        c = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=30.0),
            verify=CA,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {KEY}"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        _tl.c = c
    return c


def get_parquet(channel: str, date: str, params: dict, columns=None, tries: int = 5):
    """GET one Telonex file. Returns (df|None, status_string)."""
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
    """Inclusive [a, b] list of YYYY-MM-DD."""
    d0 = dt.date.fromisoformat(a)
    d1 = dt.date.fromisoformat(b)
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


# --------------------------------------------------------------------- crypto
def stage_crypto(days):
    os.makedirs(f"{OUT}/crypto_prices", exist_ok=True)
    todo = [d for d in days if not os.path.exists(f"{OUT}/crypto_prices/{d}.parquet")]
    print(f"[crypto] {len(todo)}/{len(days)} days to fetch", flush=True)

    def one(d):
        df, st = get_parquet("crypto_prices", d, {"asset_id": "btcusd"})
        if df is None:
            return d, st, 0
        missing = [c for c in ("timestamp_us", "server_timestamp_us", "local_timestamp_us", "price")
                   if c not in df.columns]
        if missing:
            return d, f"SCHEMA_MISSING:{missing}", 0
        df = df[["timestamp_us", "server_timestamp_us", "local_timestamp_us", "price"]].copy()
        for c in ("timestamp_us", "server_timestamp_us", "local_timestamp_us"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("int64")
        df["price"] = pd.to_numeric(df["price"], errors="coerce").astype("float64")
        df = df.sort_values("timestamp_us").reset_index(drop=True)
        df.to_parquet(f"{OUT}/crypto_prices/{d}.parquet", index=False)
        return d, "ok", len(df)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(one, d) for d in todo]):
            d, st, n = f.result()
            print(f"  crypto {d} {st} rows={n}", flush=True)


# -------------------------------------------------------------------- markets
def stage_markets(days, refresh: bool):
    src = f"{ROOT}/data/a3/telonex_5m_markets.parquet"
    if refresh or not os.path.exists(src):
        from telonex import get_markets_dataframe
        m = get_markets_dataframe(exchange="polymarket")
        m = m[m.slug.str.match(r"btc-updown-5m-\d+", na=False)].copy()
        m.to_parquet(src, index=False)
    m = pd.read_parquet(src)
    m["close_s"] = (m.end_date_us // 1_000_000).astype("int64")
    m["window_start_s"] = m["close_s"] - 300
    # slug carries the window start; assert it, that is the resolution anchor
    slug_start = m.slug.str.extract(r"btc-updown-5m-(\d+)")[0].astype("int64")
    bad = int((slug_start != m["window_start_s"]).sum())
    m["d"] = pd.to_datetime(m["close_s"] - 1, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    sub = m[m["d"].isin(set(days))].copy()
    cols = ["slug", "market_id", "asset_id_0", "asset_id_1", "outcome_0", "outcome_1",
            "status", "result_id", "start_date_us", "end_date_us", "settled_at_us",
            "close_s", "window_start_s", "d",
            "quotes_from", "quotes_to", "book_snapshot_25_from", "book_snapshot_25_to"]
    sub = sub[[c for c in cols if c in sub.columns]].sort_values("close_s").reset_index(drop=True)
    sub.to_parquet(f"{OUT}/markets.parquet", index=False)
    print(f"[markets] {len(sub)} rows, slug/window mismatches={bad}, "
          f"days={sub.d.nunique()}, resolved={(sub.status=='resolved').sum()}", flush=True)
    return sub


def load_markets():
    return pd.read_parquet(f"{OUT}/markets.parquet")


# --------------------------------------------------------------------- quotes
QCOLS = ["timestamp_us", "local_timestamp_us", "bid_price", "bid_size", "ask_price", "ask_size"]

# Tape kept per market: [close - QUOTE_PRE, close + QUOTE_POST] seconds. The full
# in-window tape runs ~65 top-of-book updates/s (11.4M rows/day, 174 MB/day), which
# does not fit on this box for 56 days. 120s is 24x the widest decision band
# (tau<=5s) and still covers the two-book re-check. The single last update BEFORE
# the cut is always kept, so book age at the decision instant is still exact even
# when the book was frozen for longer than the retained span.
QUOTE_PRE, QUOTE_POST = 120, 5


def _trim(df, close_s, pre, post):
    lo = (close_s - pre) * 1_000_000
    hi = (close_s + post) * 1_000_000
    keep = df[(df.timestamp_us >= lo) & (df.timestamp_us <= hi)]
    pcut = df[df.timestamp_us < lo]
    if len(pcut):
        keep = pd.concat([pcut.tail(1), keep], ignore_index=True)
    return keep


def stage_quotes(days, workers: int):
    os.makedirs(f"{OUT}/quotes", exist_ok=True)
    mk = load_markets()
    todo = [d for d in days if not os.path.exists(f"{OUT}/quotes/{d}.parquet")]
    print(f"[quotes] {len(todo)}/{len(days)} days to fetch", flush=True)
    for d in todo:
        sub = mk[mk.d == d]
        if sub.empty:
            print(f"  quotes {d} NO MARKETS", flush=True)
            continue
        t0 = time.time()
        parts, fails = [], []
        lock = threading.Lock()

        def one(row, oid):
            df, st = get_parquet("quotes", d, {"slug": row.slug, "outcome_id": oid}, columns=QCOLS)
            if df is None or df.empty:
                return st if df is None else "empty", row.slug, oid
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
            print(f"  quotes {d} ALL FAILED {fails[:3]}", flush=True)
            continue
        allq = pd.concat(parts, ignore_index=True).sort_values(["close_s", "oid", "timestamp_us"])
        allq.reset_index(drop=True).to_parquet(f"{OUT}/quotes/{d}.parquet", index=False)
        print(f"  quotes {d} rows={len(allq)} mkts={sub.shape[0]} fails={len(fails)} "
              f"{fails[:3]} {time.time()-t0:.0f}s", flush=True)


# -------------------------------------------------------------------- prepass
def _index_tape(q):
    """{(close_s, oid): (ts array, ask array, bid array, asksz, bidsz)} in one pass."""
    q = q.sort_values(["close_s", "oid", "timestamp_us"], kind="stable")
    key = q.close_s.to_numpy(np.int64) * 2 + q.oid.to_numpy(np.int64)
    cut = np.flatnonzero(np.diff(key)) + 1
    starts = np.concatenate([[0], cut])
    ends = np.concatenate([cut, [len(q)]])
    ts = q.timestamp_us.to_numpy(np.int64)
    ask = q.ask_price.to_numpy(np.float32)
    bid = q.bid_price.to_numpy(np.float32)
    asz = q.ask_size.to_numpy(np.float32)
    out = {}
    for s, e in zip(starts, ends):
        out[(int(q.close_s.iloc[s]), int(q.oid.iloc[s]))] = (ts[s:e], ask[s:e], bid[s:e], asz[s:e])
    return out


def _load_cp(d):
    p = f"{OUT}/crypto_prices/{d}.parquet"
    if not os.path.exists(p):
        p = f"{REPO_CP}/{d}.parquet"
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p)


def stage_prepass(days, taus, edge_thresh, price_lo, price_hi, fee_rate, sigma_floor):
    """Cheap, deliberately GENEROUS candidate scan.

    Causality: S_t uses only reports with server_timestamp_us <= decision instant
    (PUBLISH time), never observation time. The quote used at the decision
    instant is the last one with timestamp_us <= decision instant; its age is
    recorded, and NOTHING is dropped for staleness here (that is the replay's
    call) -- staleness is only measured.
    """
    from math import erf, sqrt
    mk = load_markets()
    rows = []
    for d in days:
        qp = f"{OUT}/quotes/{d}.parquet"
        cp = _load_cp(d)
        # the strike needs the window-start print, which for the 00:00 window
        # lives on the previous UTC date -> load both and concatenate
        prev = _load_cp((dt.date.fromisoformat(d) - dt.timedelta(days=1)).isoformat())
        if not os.path.exists(qp) or cp is None:
            print(f"  prepass {d} SKIP (quotes={os.path.exists(qp)} cp={cp is not None})", flush=True)
            continue
        if prev is not None:
            cp = pd.concat([prev.tail(4000), cp], ignore_index=True)
        cp = cp.sort_values("timestamp_us")
        obs = cp.timestamp_us.to_numpy(np.int64)
        pub = cp.server_timestamp_us.to_numpy(np.int64)
        px = cp.price.to_numpy(np.float64)
        # causal view: sort by PUBLISH time, running-max index of obs
        o = np.argsort(pub, kind="stable")
        pub_s, px_s = pub[o], px[o]

        tape = _index_tape(pd.read_parquet(qp))
        sub = mk[mk.d == d]
        for r in sub.itertuples():
            ws_us = r.window_start_s * 1_000_000
            # strike = first report with OBSERVATION ts >= window_start (backfill)
            i = np.searchsorted(obs, ws_us, side="left")
            if i >= len(obs):
                continue
            strike = px[i]
            for tau in taus:
                t_us = int(r.close_s * 1_000_000 - tau * 1_000_000)
                j = np.searchsorted(pub_s, t_us, side="right") - 1
                if j < 0:
                    continue
                S_t = px_s[j]
                # sigma_1s from the causal trailing 180s of published prints
                k0 = np.searchsorted(pub_s, t_us - 180_000_000, side="left")
                seg = px_s[k0:j + 1]
                if len(seg) >= 20:
                    lr = np.diff(np.log(seg))
                    sig = float(np.std(lr))
                else:
                    sig = 0.0
                sig = max(sig, sigma_floor)
                # sigma is the one genuinely free parameter of `fair`; a replay
                # that estimates it differently would select different windows.
                # Sweep it so the book fetch is a SUPERSET of any reasonable
                # calibration rather than a hostage to this one.
                fairs = []
                for mult in (0.5, 1.0, 2.0):
                    z = np.log(S_t / strike) / (max(sig * mult, sigma_floor) * sqrt(tau))
                    fu = 0.5 * (1.0 + erf(z / sqrt(2.0)))
                    fairs.append(min(max(fu, 0.02), 0.98))
                for oid in (0, 1):
                    g = tape.get((int(r.close_s), oid))
                    if g is None:
                        continue
                    ts, askv, _bidv, _aszv = g
                    p = np.searchsorted(ts, t_us, side="right") - 1
                    if p < 0:
                        continue
                    ask = float(askv[p])
                    if not (price_lo < ask < price_hi):
                        continue
                    fee = fee_rate * ask * (1.0 - ask)
                    best, bfair = -9.0, np.nan
                    for fu in fairs:
                        fair = fu if oid == 0 else 1.0 - fu
                        e = fair - ask - fee
                        if e > best:
                            best, bfair = e, fair
                    if best > edge_thresh:
                        rows.append((d, r.slug, r.close_s, oid, tau, bfair, ask, best,
                                     (t_us - ts[p]) / 1e6, sig))
        print(f"  prepass {d} cum_candidates={len(rows)}", flush=True)
    c = pd.DataFrame(rows, columns=["d", "slug", "close_s", "oid", "tau", "fair", "ask",
                                    "edge", "quote_age_s", "sigma_1s"])
    c["is_control"] = False
    # RANDOM CONTROL: N windows/day that the prefilter REJECTED, so the replay can
    # measure what the selection threw away instead of taking it on trust.
    rng = np.random.default_rng(20260727)
    ctl = []
    for d in sorted(set(c.d)) or days:
        sel = set(c[c.d == d].close_s)
        pool = mk[(mk.d == d) & (~mk.close_s.isin(sel))].close_s.to_numpy()
        if len(pool) == 0:
            continue
        for cs in rng.choice(pool, size=min(12, len(pool)), replace=False):
            for oid in (0, 1):
                ctl.append((d, "", int(cs), oid, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, True))
    if ctl:
        c = pd.concat([c, pd.DataFrame(ctl, columns=list(c.columns))], ignore_index=True)
    slug_by_close = dict(zip(mk.close_s, mk.slug))
    c["slug"] = c.close_s.map(slug_by_close)
    # MERGE, don't clobber: prepass is run per date-range while the quote fetch is
    # still catching up, and stage_books resumes off this file. Overwriting it
    # would silently orphan candidates for days already scanned.
    cf = f"{OUT}/candidates.parquet"
    if os.path.exists(cf):
        old = pd.read_parquet(cf)
        c = pd.concat([old[~old.d.isin(set(c.d))], c], ignore_index=True)
    c = c.sort_values(["close_s", "oid", "tau"]).reset_index(drop=True)
    c.to_parquet(cf, index=False)
    n = c.groupby(["d"]).close_s.nunique()
    print(f"[prepass] candidate rows={len(c)} distinct windows={c.close_s.nunique()}")
    print(n.to_string())
    return c


# ---------------------------------------------------------------------- books
BOOK_PRE, BOOK_POST = 45, 2


def stage_books(days, workers: int, levels: int = 25):
    """book_snapshot_25 for candidate windows only, both outcomes, trimmed to
    [close-BOOK_PRE, close+BOOK_POST] so 25-level depth stays affordable. Vendor
    `timestamp_us` is preserved verbatim -- that IS the book's last-update
    instant, and the last pre-cut row is always retained."""
    os.makedirs(f"{OUT}/books", exist_ok=True)
    cand = pd.read_parquet(f"{OUT}/candidates.parquet")
    cols = ["timestamp_us", "local_timestamp_us"]
    for i in range(levels):
        cols += [f"bid_price_{i}", f"bid_size_{i}", f"ask_price_{i}", f"ask_size_{i}"]
    pcols = cols[2:]
    todo = [d for d in days if not os.path.exists(f"{OUT}/books/{d}.parquet")]
    print(f"[books] {len(todo)}/{len(days)} days", flush=True)
    for d in todo:
        sub = cand[cand.d == d][["slug", "close_s"]].drop_duplicates()
        if sub.empty:
            pd.DataFrame(columns=["close_s", "oid"] + cols).to_parquet(f"{OUT}/books/{d}.parquet", index=False)
            print(f"  books {d} no candidates", flush=True)
            continue
        t0 = time.time()
        parts, fails = [], []
        lock = threading.Lock()

        def one(row, oid):
            df, st = get_parquet("book_snapshot_25", d, {"slug": row.slug, "outcome_id": oid},
                                 columns=cols)
            if df is None or df.empty:
                return (st if df is None else "empty"), row.slug, oid
            # always keep the last row strictly before the cut -- that is the one a
            # frozen book would be filled against, and dropping it would HIDE
            # exactly the staleness this run exists to measure
            keep = _trim(df.sort_values("timestamp_us"), row.close_s, BOOK_PRE, BOOK_POST).copy()
            if keep.empty:
                return "nosnap", row.slug, oid
            keep = f32(keep, pcols)
            keep = keep.astype({"timestamp_us": "int64", "local_timestamp_us": "int64"}).assign(
                close_s=np.int64(row.close_s), oid=np.int8(oid))
            with lock:
                parts.append(keep)
            return "ok", row.slug, oid

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, r, oid) for r in sub.itertuples() for oid in (0, 1)]
            for f in as_completed(futs):
                st, slug, oid = f.result()
                if st != "ok":
                    fails.append((st, slug, oid))
        if not parts:
            print(f"  books {d} ALL FAILED {fails[:3]}", flush=True)
            continue
        allb = pd.concat(parts, ignore_index=True).sort_values(["close_s", "oid", "timestamp_us"])
        allb.reset_index(drop=True).to_parquet(f"{OUT}/books/{d}.parquet", index=False)
        print(f"  books {d} rows={len(allb)} wins={len(sub)} fails={len(fails)} "
              f"{time.time()-t0:.0f}s", flush=True)


# --------------------------------------------------------------------- outage
def _index_books(b):
    """{(close_s, oid): ts array} for book files (freshness only)."""
    b = b.sort_values(["close_s", "oid", "timestamp_us"], kind="stable")
    key = b.close_s.to_numpy(np.int64) * 2 + b.oid.to_numpy(np.int64)
    cut = np.flatnonzero(np.diff(key)) + 1
    starts, ends = np.concatenate([[0], cut]), np.concatenate([cut, [len(b)]])
    ts = b.timestamp_us.to_numpy(np.int64)
    return {(int(b.close_s.iloc[s]), int(b.oid.iloc[s])): (ts[s:e], None, None, None)
            for s, e in zip(starts, ends)}


def stage_outage(days, min_markets: int = 3, min_age: float = 20.0, source: str = "quotes"):
    """Outage signature = many DISTINCT markets whose last book/quote update at
    their own decision instant share one identical vendor timestamp. That is
    the 2026-07-21 04:07 pattern that invalidated A3."""
    mk = load_markets()
    hits = []
    per_day = []
    for d in days:
        qp = f"{OUT}/{source}/{d}.parquet"
        if not os.path.exists(qp):
            continue
        raw = pd.read_parquet(qp)
        if raw.empty:
            continue
        tape = _index_books(raw) if source == "books" else _index_tape(raw)
        sub = mk[mk.d == d][["close_s"]].drop_duplicates()
        if source == "books":
            sub = sub[sub.close_s.isin(set(raw.close_s))]
        recs = []
        for cs in sub.close_s.to_numpy():
            t_us = int(cs * 1_000_000 - 3_000_000)  # tau = 3s, mid-band decision instant
            for oid in (0, 1):
                g = tape.get((int(cs), oid))
                if g is None:
                    continue
                ts = g[0]
                p = np.searchsorted(ts, t_us, side="right") - 1
                if p < 0:
                    continue
                recs.append((cs, oid, int(ts[p]), (t_us - ts[p]) / 1e6))
        if not recs:
            continue
        rd = pd.DataFrame(recs, columns=["close_s", "oid", "last_update_us", "age_s"])
        stale = rd[rd.age_s >= min_age]
        per_day.append((d, len(rd), len(stale), float(rd.age_s.median()),
                        float(rd.age_s.quantile(0.99)), float(rd.age_s.max())))
        # OUTAGE EPISODE = a maximal run of CONSECUTIVE 5m closes in which at
        # least one outcome's book was frozen. Grouping on an exact identical
        # microsecond instant (the A3 write-up's phrasing) under-counts: the two
        # outcomes of one market share an instant, but neighbouring markets each
        # froze on their own last tick a few seconds apart. What identifies a
        # vendor outage is that the last-update instants all predate a common
        # cutoff while the decision instants march on.
        st = stale.sort_values("close_s")
        if len(st):
            cs_all = np.sort(rd.close_s.unique())
            pos = {int(c): i for i, c in enumerate(cs_all)}
            idx = np.array([pos[int(c)] for c in st.close_s.unique()])
            idx.sort()
            brk = np.flatnonzero(np.diff(idx) > 1) + 1
            for run in np.split(idx, brk):
                mkts = cs_all[run]
                ep = st[st.close_s.isin(set(mkts.tolist()))]
                hits.append((d, int(ep.last_update_us.min()),
                             pd.Timestamp(int(ep.last_update_us.min()), unit="us", tz="UTC").isoformat(),
                             int(ep.close_s.nunique()), int(len(ep)),
                             float(ep.age_s.min()), float(ep.age_s.max())))
        gg = st
        print(f"  outage {d} obs={len(rd)} stale>={min_age}s:{len(stale)} "
              f"episodes={st.close_s.nunique() if len(st) else 0} med_age={rd.age_s.median():.2f}s "
              f"p99={rd.age_s.quantile(0.99):.1f}s max={rd.age_s.max():.0f}s", flush=True)
    h = pd.DataFrame(hits, columns=["d", "first_frozen_update_us", "first_frozen_update_utc",
                                    "n_markets", "n_obs", "min_age_s", "max_age_s"])
    h.sort_values("max_age_s", ascending=False).to_parquet(
        f"{OUT}/outage_clusters_{source}.parquet", index=False)
    pd.DataFrame(per_day, columns=["d", "n_obs", "n_stale", "med_age_s", "p99_age_s", "max_age_s"]
                 ).to_parquet(f"{OUT}/age_by_day_{source}.parquet", index=False)
    print(f"[outage/{source}] {len(h)} outage EPISODES (runs of consecutive closes with a "
          f"frozen book, age >= {min_age}s)")
    if len(h):
        print(h.sort_values("max_age_s", ascending=False).head(40).to_string(index=False))
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["crypto", "markets", "quotes", "prepass", "books", "outage"])
    ap.add_argument("--from", dest="d0", required=True)
    ap.add_argument("--to", dest="d1", required=True, help="INCLUSIVE")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--refresh-markets", action="store_true")
    ap.add_argument("--edge-thresh", type=float, default=0.0)
    ap.add_argument("--price-lo", type=float, default=0.30)
    ap.add_argument("--price-hi", type=float, default=0.99)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--sigma-floor", type=float, default=8e-6)
    ap.add_argument("--source", default="quotes", choices=["quotes", "books"])
    a = ap.parse_args()
    days = daterange(a.d0, a.d1)
    os.makedirs(OUT, exist_ok=True)
    if a.stage == "crypto":
        stage_crypto(days)
    elif a.stage == "markets":
        stage_markets(days, a.refresh_markets)
    elif a.stage == "quotes":
        stage_quotes(days, a.workers)
    elif a.stage == "prepass":
        stage_prepass(days, [2.0, 3.0, 4.0, 5.0], a.edge_thresh, a.price_lo, a.price_hi,
                      a.fee_rate, a.sigma_floor)
    elif a.stage == "books":
        stage_books(days, a.workers)
    elif a.stage == "outage":
        stage_outage(days, source=a.source)


if __name__ == "__main__":
    main()
