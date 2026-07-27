#!/usr/bin/env python3
"""Render the M1 report tables from the measured CSVs and splice them into
audit/M1_multicoin_recon.md at the <!--LIQ_TABLE--> / <!--SUMMARY_TABLE--> marks.

Idempotent: the markers are kept, so re-running after more data lands simply
refreshes the numbers.
"""
from __future__ import annotations

import os
import re

import pandas as pd

ROOT = "/home/user/S4"
OUT = f"{ROOT}/data/multicoin"
DOC = f"{ROOT}/audit/M1_multicoin_recon.md"

DISPLAY = ["bitcoin", "ethereum", "solana", "xrp", "dogecoin", "bnb", "hype"]
SYM = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT",
       "xrp": "XRPUSDT", "dogecoin": "DOGEUSDT", "bnb": "BNBUSDT",
       "hype": "HYPEUSDT (futures)"}
SRC = {"bitcoin": "Binance BTC/USDT 1H", "ethereum": "Binance ETH/USDT 1H",
       "solana": "Binance SOL/USDT 1H", "xrp": "Binance XRP/USDT 1H",
       "dogecoin": "Binance DOGE/USDT 1H", "bnb": "Binance BNB/USDT 1H",
       "hype": "**Binance HYPE/USDT 1H (USD-M FUTURES)**"}


def fmt(v, nd=2, money=False, pct=False):
    if v is None or v != v:
        return "—"
    if pct:
        return f"{v*100:.1f}%"
    if money:
        return f"${v:,.0f}" if abs(v) >= 10 else f"${v:,.2f}"
    return f"{v:,.{nd}f}"


def liq_table():
    q = pd.read_csv(f"{OUT}/quote_summary_by_coin.csv").set_index("coin")
    rows = ["| coin | days | closes | frac ask in band | median ask depth (USD) | p25 / p75 | median quote age | median updates in last 120 s | indicative signals/day |",
            "|---|---|---|---|---|---|---|---|---|"]
    for c in DISPLAY:
        if c not in q.index:
            continue
        r = q.loc[c]
        rows.append(
            f"| {c} | {int(r.days)} | {int(r.closes)} | **{fmt(r.frac_close_ask_in_band, pct=True)}** | "
            f"{fmt(r.med_ask_usd, money=True)} | {fmt(r.p25_ask_usd, money=True)} / {fmt(r.p75_ask_usd, money=True)} | "
            f"{fmt(r.med_quote_age_s)} s | {fmt(r.med_upd_120s, 0)} | **{fmt(r.indic_signals_per_day)}** |")
    tot = q.indic_signals_per_day.sum()
    rows.append(f"| **TOTAL** | | | | | | | | **{tot:.2f}/day** |")
    return "\n".join(rows)


def summary_table():
    q = pd.read_csv(f"{OUT}/quote_summary_by_coin.csv").set_index("coin")
    v = pd.read_parquet(f"{OUT}/gamma_volume.parquet").groupby("coin").volumeNum.median()
    ver = pd.read_csv(f"{OUT}/verify_settle.csv").set_index("coin")
    liq = None
    p = f"{OUT}/liquidity_by_coin.csv"
    if os.path.exists(p):
        liq = pd.read_csv(p).set_index("coin")
    cmap = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "xrp": "XRP",
            "dogecoin": "DOGE", "bnb": "BNB", "hype": "HYPE"}

    rows = ["| coin | slug pattern | resolution source (verified) | settle agree | closes/day | median vol/market | median depth near close | days of data | problems |",
            "|---|---|---|---|---|---|---|---|---|"]
    probs = {
        "bitcoin": "none — this is the control",
        "ethereum": "none material; best expansion candidate",
        "solana": "none material",
        "xrp": "none material",
        "dogecoin": "thin: ~$22 liftable per close",
        "bnb": "**sign only 96.8% determined at τ=5 s** (5x BTC's flip rate); ~$16 liftable",
        "hype": "**no live futures feed reachable (451)**; $0.25 liftable, 2 ask levels — defer",
    }
    for c in DISPLAY:
        d = liq.loc[cmap[c]] if liq is not None and cmap[c] in liq.index else None
        depth = fmt(d.med_cap_usd_3c, money=True) if d is not None else "—"
        days = int(q.loc[c].days) if c in q.index else 0
        agree = f"{ver.loc[c].settle_agreement*100:.3f}%" if c in ver.index else "—"
        rows.append(
            f"| **{c}** | `{c}-up-or-down-<month>-<day>-<year>-<hour><am\\|pm>-et` | {SRC[c]} | "
            f"{agree} | 24 | {fmt(v.get(c), money=True)} | {depth} | {days} | {probs[c]} |")
    return "\n".join(rows)


def splice(doc, marker, table):
    pat = re.compile(rf"(<!--{marker}-->)(.*?)(?=\n\n|\Z)", re.S)
    if not pat.search(doc):
        raise SystemExit(f"marker {marker} not found")
    return pat.sub(lambda m: m.group(1) + "\n\n" + table, doc)


def main():
    doc = open(DOC).read()
    doc = splice(doc, "LIQ_TABLE", liq_table())
    doc = splice(doc, "SUMMARY_TABLE", summary_table())
    open(DOC, "w").write(doc)
    print("spliced tables into", DOC)


if __name__ == "__main__":
    main()
