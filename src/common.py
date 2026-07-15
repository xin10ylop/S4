"""Shared helpers: splits, fees, results table."""
import csv
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results/results_table.csv"

# fixed BEFORE testing (docs/02_hypotheses.md)
SPLITS = {
    "5m": ("2026-02-12", "2026-04-15"),
    "15m": ("2025-10-11", "2026-03-31"),
    "1h": ("2025-10-11", "2026-04-30"),
    "4h": ("2025-10-15", "2026-03-31"),
}
CURRENT_FEE = 0.07


def train_mask(df, family, date_col="date"):
    a, b = SPLITS[family]
    return (df[date_col] >= a) & (df[date_col] <= b)


def test_mask(df, family, date_col="date"):
    a, b = SPLITS[family]
    return df[date_col] > b


def taker_fee(p, rate=CURRENT_FEE):
    """Fee in $ per share for a taker fill at price p."""
    return rate * p * (1.0 - p)


def log_result(hyp, family, dataset, n, metric, value, edge_cents, verdict, notes=""):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    new = not RESULTS.exists()
    with open(RESULTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "hypothesis", "family", "dataset", "n", "metric",
                        "value", "edge_cents_per_share_after_fees", "verdict", "notes"])
        w.writerow([dt.datetime.utcnow().isoformat(timespec="seconds"), hyp, family,
                    dataset, n, metric, round(float(value), 5) if value == value else "",
                    round(float(edge_cents), 3) if edge_cents == edge_cents else "",
                    verdict, notes])


def load_feats(family):
    snap = pd.read_parquet(ROOT / f"data/features/snap_{family}.parquet")
    bnc = pd.read_parquet(ROOT / f"data/features/binance_{family}.parquet")
    df = snap.merge(bnc.drop(columns=["family", "slug", "duration", "date", "fee_rate"]),
                    on=["wts", "result_id"], how="left")
    if family != "1h":
        cl = pd.read_parquet(ROOT / f"data/features/chainlink_{family}.parquet")
        keep = ["wts"] + [c for c in cl.columns if c.startswith(("dist_", "S_open", "bin_up", "rv", "r1"))]
        cl = cl[keep].add_prefix("cl_").rename(columns={"cl_wts": "wts"})
        df = df.merge(cl, on="wts", how="left")
    df["up"] = (df.result_id == 0).astype(int)
    return df
