"""Bankroll path projection: $100 start, fixed-fraction sizing, profits reinvested."""
import numpy as np
import pandas as pd


def project(trades: pd.DataFrame, start=100.0, base_trade=5.0, base_bank=100.0,
            cap_shares_col="shares", n_paths=2000, seed=7):
    """trades: needs pnl_share, px, shares (available size), ordered by time.

    Sizing: trade_dollars = base_trade * (bank / base_bank), shares bought =
    min(trade_dollars / px, available shares). Bootstrap paths resample trade
    sequences to give a distribution; the 'actual' path uses chronological order.
    """
    t = trades.reset_index(drop=True)

    def run(seq):
        bank = start
        path = [bank]
        for i in seq:
            r = t.iloc[i]
            dollars = base_trade * bank / base_bank
            sh = min(dollars / r.px, r[cap_shares_col])
            bank += sh * r.pnl_share
            path.append(bank)
        return np.array(path)

    actual = run(range(len(t)))
    rng = np.random.default_rng(seed)
    finals = []
    maxdds = []
    for _ in range(n_paths):
        seq = rng.permutation(len(t))
        p = run(seq)
        finals.append(p[-1])
        peak = np.maximum.accumulate(p)
        maxdds.append(((peak - p) / peak).max())
    peak = np.maximum.accumulate(actual)
    dd = ((peak - actual) / peak).max()
    return {
        "actual_final": actual[-1],
        "actual_maxdd": dd,
        "p5_final": float(np.percentile(finals, 5)),
        "p50_final": float(np.percentile(finals, 50)),
        "p95_final": float(np.percentile(finals, 95)),
        "median_maxdd": float(np.median(maxdds)),
        "path": actual,
    }
