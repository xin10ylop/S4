"""Persistence: SQLite (source of truth) + append-only CSV mirrors.

Tables: signals, fills (every attempt, filled or not), resolutions.
Realized PnL = shares * (payout - avg_price) - fees, payout in {0, 1}.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .fill_engine import FillAttempt
from .logging_setup import get_logger
from .strategy import SnipeSignal, WinnerDetermination

log = get_logger("ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    family TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    side TEXT,
    token_id TEXT,
    fair REAL,
    ask REAL,
    edge REAL,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    ts REAL NOT NULL,
    signal_ts REAL,
    strategy TEXT NOT NULL,
    family TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    side TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    avg_price REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    fees_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    edge_min REAL,
    levels_json TEXT,
    meta_json TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS resolutions (
    market_slug TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    close_ts REAL NOT NULL,
    oracle_winner TEXT,
    oracle_reason TEXT,
    gamma_winner TEXT,
    gamma_closed INTEGER NOT NULL DEFAULT 0,
    resolved_winner TEXT,
    resolution_source TEXT,
    disagreement INTEGER NOT NULL DEFAULT 0,
    resolved_ts REAL,
    pnl_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_slug);
CREATE INDEX IF NOT EXISTS idx_fills_resolved ON fills(resolved);
"""

_FILLS_CSV_HEADER = [
    "ts", "market_slug", "family", "strategy", "side", "token_id", "outcome",
    "shares", "avg_price", "cost_usd", "fees_usd", "latency_ms", "edge_min",
]
_PNL_CSV_HEADER = [
    "resolved_ts", "market_slug", "family", "strategy", "side", "shares",
    "avg_price", "payout", "fees_usd", "realized_pnl", "oracle_winner",
    "gamma_winner", "resolution_source", "disagreement",
]


class Ledger:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        db_path = config.sqlite_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._fills_csv = config.fills_csv
        self._pnl_csv = config.pnl_csv
        self._ensure_csv(self._fills_csv, _FILLS_CSV_HEADER)
        self._ensure_csv(self._pnl_csv, _PNL_CSV_HEADER)

    @staticmethod
    def _ensure_csv(path: Path, header: List[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)

    # --- signals -----------------------------------------------------------
    def record_snipe_signal(self, sig: SnipeSignal) -> int:
        meta = {
            "tau_secs": sig.tau_secs, "s_t": sig.s_t, "s_open": sig.s_open,
            "sigma_1s": sig.sigma_1s,
        }
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO signals (ts, strategy, family, market_slug, side, token_id, "
                "fair, ask, edge, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), "close_snipe", sig.family, sig.market_slug, sig.side,
                 sig.token_id, sig.fair, sig.ask, sig.edge, json.dumps(meta)),
            )
            self._conn.commit()
            return cur.lastrowid

    def record_settle_signal(self, family: str, market_slug: str, side: str, token_id: str,
                              reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO signals (ts, strategy, family, market_slug, side, token_id, "
                "fair, ask, edge, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), "settle_sweep", family, market_slug, side, token_id,
                 None, None, None, json.dumps({"reason": reason})),
            )
            self._conn.commit()
            return cur.lastrowid

    # --- fills ---------------------------------------------------------------
    def record_fill(self, *, strategy: str, family: str, market_slug: str,
                     signal_id: Optional[int], attempt: FillAttempt, edge_min: float,
                     extra_meta: Optional[dict] = None) -> int:
        levels = [asdict(f) for f in attempt.walk.fills]
        meta = dict(extra_meta or {})
        if attempt.book_at_signal is not None and attempt.book_at_signal.best_ask:
            meta["signal_best_ask"] = attempt.book_at_signal.best_ask.price
        if attempt.book_at_fill is not None:
            meta["fill_best_ask"] = attempt.book_at_fill.best_ask.price if attempt.book_at_fill.best_ask else None
            meta["fill_n_ask_levels"] = len(attempt.book_at_fill.asks)
        shares = attempt.walk.total_shares
        cost = attempt.walk.total_cost
        fees = attempt.walk.total_fees
        avg_price = attempt.walk.avg_price
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO fills (signal_id, ts, signal_ts, strategy, family, market_slug, "
                "side, token_id, outcome, shares, avg_price, cost_usd, fees_usd, latency_ms, "
                "edge_min, levels_json, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (signal_id, attempt.fill_time, attempt.signal_time, strategy, family,
                 market_slug, attempt.side, attempt.token_id, attempt.outcome, shares,
                 avg_price, cost, fees, attempt.latency_ms, edge_min,
                 json.dumps(levels), json.dumps(meta)),
            )
            self._conn.commit()
            fill_id = cur.lastrowid
        with open(self._fills_csv, "a", newline="") as f:
            csv.writer(f).writerow([
                attempt.fill_time, market_slug, family, strategy, attempt.side,
                attempt.token_id, attempt.outcome, shares, avg_price, cost, fees,
                attempt.latency_ms, edge_min,
            ])
        log.info(
            "FILL %s/%s %s side=%s outcome=%s shares=%.2f avg_px=%s cost=$%.2f fees=$%.4f",
            strategy, family, market_slug, attempt.side, attempt.outcome, shares,
            f"{avg_price:.4f}" if avg_price else "-", cost, fees,
        )
        return fill_id

    # --- open positions / resolution -----------------------------------------
    def unresolved_markets(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT market_slug FROM fills WHERE resolved = 0 AND outcome = 'filled'"
            ).fetchall()
        return [r[0] for r in rows]

    def total_open_notional(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM fills WHERE resolved=0 AND outcome='filled'"
            ).fetchone()
        return float(row[0])

    def strategy_cost_for_market(self, market_slug: str, strategy: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM fills WHERE market_slug=? AND "
                "strategy=? AND resolved=0 AND outcome='filled'",
                (market_slug, strategy),
            ).fetchone()
        return float(row[0])

    def all_open_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT market_slug, family, strategy, side, token_id, SUM(shares), "
                "SUM(cost_usd), SUM(fees_usd) FROM fills WHERE outcome='filled' AND resolved=0 "
                "GROUP BY market_slug, family, strategy, side, token_id"
            ).fetchall()
        out = []
        for slug, family, strategy, side, token_id, shares, cost, fees in rows:
            if shares and shares > 0:
                out.append({
                    "market_slug": slug, "family": family, "strategy": strategy, "side": side,
                    "token_id": token_id, "shares": shares, "cost_usd": cost, "fees_usd": fees,
                    "avg_price": cost / shares,
                })
        return out

    def open_position_summary(self, market_slug: str) -> List[Dict[str, Any]]:
        """Aggregate filled shares/cost/fees per (strategy, side) for a market."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy, side, token_id, SUM(shares), SUM(cost_usd), SUM(fees_usd) "
                "FROM fills WHERE market_slug=? AND outcome='filled' AND resolved=0 "
                "GROUP BY strategy, side, token_id",
                (market_slug,),
            ).fetchall()
        out = []
        for strategy, side, token_id, shares, cost, fees in rows:
            if shares and shares > 0:
                out.append({
                    "strategy": strategy, "side": side, "token_id": token_id,
                    "shares": shares, "cost_usd": cost, "fees_usd": fees,
                    "avg_price": cost / shares,
                })
        return out

    def resolve_market(self, market_slug: str, family: str, close_ts: float,
                        oracle: WinnerDetermination, gamma_winner: Optional[str],
                        gamma_closed: bool, resolution_source: str) -> Optional[dict]:
        """Finalize a market: compute realized PnL per (strategy, side) group,
        write the resolutions row + pnl.csv rows, and mark those fills resolved.
        Returns a summary dict, or None if there was nothing to resolve.
        """
        resolved_winner = gamma_winner if resolution_source == "gamma" else oracle.winner
        disagreement = bool(gamma_winner and oracle.winner and gamma_winner != oracle.winner)
        if disagreement:
            log.warning("RESOLUTION DISAGREEMENT %s: oracle=%s gamma=%s", market_slug,
                        oracle.winner, gamma_winner)

        positions = self.open_position_summary(market_slug)
        pnl_rows = []
        now = time.time()
        if resolved_winner is not None:
            for pos in positions:
                payout = 1.0 if pos["side"] == resolved_winner else 0.0
                realized = pos["shares"] * (payout - pos["avg_price"]) - pos["fees_usd"]
                pnl_rows.append({
                    "resolved_ts": now, "market_slug": market_slug, "family": family,
                    "strategy": pos["strategy"], "side": pos["side"], "shares": pos["shares"],
                    "avg_price": pos["avg_price"], "payout": payout, "fees_usd": pos["fees_usd"],
                    "realized_pnl": realized, "oracle_winner": oracle.winner,
                    "gamma_winner": gamma_winner, "resolution_source": resolution_source,
                    "disagreement": disagreement,
                })

        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO resolutions (market_slug, family, close_ts, "
                "oracle_winner, oracle_reason, gamma_winner, gamma_closed, resolved_winner, "
                "resolution_source, disagreement, resolved_ts, pnl_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (market_slug, family, close_ts, oracle.winner, oracle.reason, gamma_winner,
                 int(gamma_closed), resolved_winner, resolution_source, int(disagreement),
                 now if resolved_winner is not None else None, json.dumps(pnl_rows)),
            )
            if resolved_winner is not None:
                self._conn.execute(
                    "UPDATE fills SET resolved=1 WHERE market_slug=? AND outcome='filled'",
                    (market_slug,),
                )
            self._conn.commit()

        if pnl_rows:
            with open(self._pnl_csv, "a", newline="") as f:
                w = csv.writer(f)
                for r in pnl_rows:
                    w.writerow([r[c] for c in _PNL_CSV_HEADER])
            for r in pnl_rows:
                log.info(
                    "RESOLVED %s %s/%s side=%s shares=%.2f payout=%.0f pnl=$%.4f "
                    "(oracle=%s gamma=%s src=%s disagree=%s)",
                    market_slug, r["strategy"], family, r["side"], r["shares"], r["payout"],
                    r["realized_pnl"], oracle.winner, gamma_winner, resolution_source,
                    disagreement,
                )
        return {"market_slug": market_slug, "resolved_winner": resolved_winner,
                "pnl_rows": pnl_rows, "disagreement": disagreement}

    def pnl_today(self) -> Dict[str, Any]:
        """Realized net PnL for resolutions finalized since UTC midnight today."""
        midnight = time.time() - (time.time() % 86400)
        with self._lock:
            rows = self._conn.execute(
                "SELECT pnl_json FROM resolutions WHERE resolved_ts >= ?", (midnight,)
            ).fetchall()
        gross = 0.0
        fees = 0.0
        n = 0
        for (pnl_json,) in rows:
            if not pnl_json:
                continue
            for r in json.loads(pnl_json):
                n += 1
                gross += r["realized_pnl"] + r["fees_usd"]
                fees += r["fees_usd"]
        return {"n": n, "gross_pnl": gross, "fees_paid": fees, "net_pnl": gross - fees}

    def is_resolution_pending(self, market_slug: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT resolved_ts FROM resolutions WHERE market_slug=?", (market_slug,)
            ).fetchone()
        return row is None or row[0] is None

    # --- metrics ---------------------------------------------------------------
    def metrics(self) -> Dict[str, Any]:
        with self._lock:
            total_trades = self._conn.execute(
                "SELECT COUNT(*) FROM fills WHERE outcome='filled'"
            ).fetchone()[0]
            attempts = self._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            by_outcome = dict(self._conn.execute(
                "SELECT outcome, COUNT(*) FROM fills GROUP BY outcome"
            ).fetchall())
            pnl_rows = self._conn.execute("SELECT pnl_json FROM resolutions").fetchall()

            per_strategy_family: Dict[str, Dict[str, Any]] = {}
            gross = 0.0
            fees = 0.0
            wins = 0
            n = 0
            for (pnl_json,) in pnl_rows:
                if not pnl_json:
                    continue
                for r in json.loads(pnl_json):
                    n += 1
                    gross += r["realized_pnl"] + r["fees_usd"]
                    fees += r["fees_usd"]
                    if r["realized_pnl"] > 0:
                        wins += 1
                    key = f"{r['strategy']}/{r['family']}"
                    agg = per_strategy_family.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0,
                                                                 "shares": 0.0})
                    agg["n"] += 1
                    agg["pnl"] += r["realized_pnl"]
                    agg["wins"] += 1 if r["realized_pnl"] > 0 else 0
                    agg["shares"] += r["shares"]
            net_pnl = gross - fees
            disagreements = self._conn.execute(
                "SELECT COUNT(*) FROM resolutions WHERE disagreement=1"
            ).fetchone()[0]

        for key, agg in per_strategy_family.items():
            agg["win_rate"] = agg["wins"] / agg["n"] if agg["n"] else None
            agg["ev_per_share"] = agg["pnl"] / agg["shares"] if agg["shares"] else None

        return {
            "n_resolved_positions": n,
            "n_fill_attempts": attempts,
            "attempts_by_outcome": by_outcome,
            "n_filled": total_trades,
            "win_rate": (wins / n) if n else None,
            "gross_pnl": gross,
            "fees_paid": fees,
            "net_pnl": net_pnl,
            "resolution_disagreements": disagreements,
            "per_strategy_family": per_strategy_family,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
