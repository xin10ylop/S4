#!/usr/bin/env python3
"""A3 — 5m close_snipe re-validation engine (repo data).

Faithful port of bot/polybot/{strategy,fill_engine}.py to a vectorised replay
over the repo's 5m quotes/bookcurves + captured Chainlink crypto_prices.

CRITICAL HONESTY RULES
  * The Chainlink print for observation-second T is PUBLISHED at
    server_timestamp_us (~T+1.0..1.4s). At decision time t the bot can only
    know the newest print with server_timestamp_us <= t. We use exactly that.
  * The uncertainty horizon tau runs from that print's OBSERVATION time to the
    close observation second (verified strike/settle convention: the print
    stamped exactly at wts / wts+duration).
  * The settle print is never used to decide a pre-close trade.
  * Signal is evaluated on the book at t; the FILL happens against the book at
    t + latency (1.5 s) and must independently clear edge_min (walk_asks).
"""
from __future__ import annotations

import math
import os
import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = "/home/user/S4"
P = f"{ROOT}/data/data/processed"

# ---- current bot parameters (bot/config.yaml) -----------------------------
FEE_RATE = 0.07
FAIR_CAP = 0.98
SIGMA_FLOOR = 8.0e-6
PRICE_MIN, PRICE_MAX = 0.30, 0.99
VOL_WINDOW = 120
LATENCY_S = 1.5
MAX_WALK = 0.03
CAP_USD = 25.0


def fee(p):
    return FEE_RATE * p * (1.0 - p)


# --------------------------------------------------------------------------
# Chainlink oracle with publication lag
# --------------------------------------------------------------------------
class CLFeed:
    """Chainlink BTC/USD 1s series with an honest 'what did we know at t' view.

    obs_ts  : observation second (timestamp_us) -- what Polymarket settles on
    pub_ts  : server_timestamp_us -- when the report became available
    """

    def __init__(self, df: pd.DataFrame):
        df = df.dropna(subset=["price"]).drop_duplicates("timestamp_us")
        df = df.sort_values("server_timestamp_us")
        self.pub = df.server_timestamp_us.to_numpy(np.int64)
        self.obs = df.timestamp_us.to_numpy(np.int64)
        self.px = df.price.to_numpy(float)
        # observation-time-indexed view (for the strike, which is old news)
        o = np.argsort(self.obs)
        self.obs_sorted = self.obs[o]
        self.px_by_obs = self.px[o]
        self.logpx_by_obs = np.log(self.px_by_obs)

    # --- strike / settle: indexed by observation second -------------------
    def at_obs(self, sec: np.ndarray):
        """Exact print stamped at `sec` (NaN if that second is missing)."""
        i = np.searchsorted(self.obs_sorted, sec.astype(np.int64) * 1_000_000)
        ok = (i < len(self.obs_sorted))
        out = np.full(len(sec), np.nan)
        j = np.clip(i, 0, len(self.obs_sorted) - 1)
        hit = ok & (self.obs_sorted[j] == sec.astype(np.int64) * 1_000_000)
        out[hit] = self.px_by_obs[j[hit]]
        return out

    def at_obs_or_before(self, sec: np.ndarray):
        """Newest print with observation second <= sec."""
        i = np.searchsorted(self.obs_sorted, sec.astype(np.int64) * 1_000_000, side="right") - 1
        out = np.full(len(sec), np.nan)
        ok = i >= 0
        out[ok] = self.px_by_obs[i[ok]]
        return out

    # --- live view: indexed by PUBLICATION time ---------------------------
    def latest_at(self, t_us: np.ndarray):
        """Newest print PUBLISHED at or before t_us.
        Returns (price, observation_ts_us, published_ts_us). NaN/-1 if none."""
        i = np.searchsorted(self.pub, t_us.astype(np.int64), side="right") - 1
        px = np.full(len(t_us), np.nan)
        obs = np.full(len(t_us), -1, np.int64)
        pub = np.full(len(t_us), -1, np.int64)
        ok = i >= 0
        px[ok] = self.px[i[ok]]
        obs[ok] = self.obs[i[ok]]
        pub[ok] = self.pub[i[ok]]
        return px, obs, pub

    # --- realised 1s vol over the trailing VOL_WINDOW seconds -------------
    def build_sigma_table(self):
        """sigma_1s(T) = std of 1s log-returns over observation seconds
        (T-VOL_WINDOW, T], computed on a gap-filled 1s grid. Indexed by the
        observation second T of the newest print we hold."""
        sec = self.obs_sorted // 1_000_000
        lo, hi = int(sec[0]), int(sec[-1])
        grid = np.arange(lo, hi + 1)
        lp = pd.Series(self.logpx_by_obs, index=sec).groupby(level=0).last()
        lp = lp.reindex(grid).ffill()
        r = lp.diff()
        # rolling sample std (ddof=1) over VOL_WINDOW returns
        s = r.rolling(VOL_WINDOW, min_periods=30).std(ddof=1)
        self.sig_sec = grid
        self.sig_val = s.to_numpy()

    def sigma_at_obs(self, obs_us: np.ndarray):
        sec = obs_us // 1_000_000
        i = np.searchsorted(self.sig_sec, sec)
        out = np.full(len(sec), np.nan)
        ok = (i >= 0) & (i < len(self.sig_sec))
        j = np.clip(i, 0, len(self.sig_sec) - 1)
        hit = ok & (self.sig_sec[j] == sec)
        out[hit] = self.sig_val[j[hit]]
        return out


def load_cl(days, src="repo"):
    frames = []
    for d in days:
        if src == "repo":
            f = f"{P}/daily/crypto_prices/{d}.parquet"
            if os.path.exists(f):
                frames.append(pd.read_parquet(
                    f, columns=["timestamp_us", "server_timestamp_us", "price"]))
        else:
            f = f"{ROOT}/data/a3/crypto_prices/polymarket_crypto_prices_{d}_btcusd.parquet"
            if os.path.exists(f):
                x = pd.read_parquet(f, columns=["timestamp_us", "server_timestamp_us", "price"])
                x["price"] = x.price.astype(float)
                frames.append(x)
    if not frames:
        return None
    c = pd.concat(frames, ignore_index=True)
    fd = CLFeed(c)
    fd.build_sigma_table()
    return fd


# --------------------------------------------------------------------------
# fair value (verbatim strategy.fair_value_up + caps)
# --------------------------------------------------------------------------
def fair_up(S_t, S_open, sigma_1s, tau):
    sigma_1s = np.maximum(sigma_1s, SIGMA_FLOOR)
    sig = sigma_1s * np.sqrt(tau)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.log(S_t / S_open) / sig
    f = norm.cdf(z)
    f = np.clip(f, 1.0 - FAIR_CAP, FAIR_CAP)
    f[~np.isfinite(z)] = np.nan
    return f
