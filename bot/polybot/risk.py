"""Risk guards that gate *whether we may open a position at all*.

Two independent gates live here:

WarmupGate (M4 guard 2)
-----------------------
`fair = Phi( ln(S_t/S_open) / (sigma_1s*sqrt(tau)) )`. sigma_1s is the std of
1s log returns over a rolling 120s buffer of oracle polls. Straight after a
restart (deploy, watchdog, crash, OOM kill) that buffer is nearly empty, and
`BinanceOracle.rolling_log_return_std` is willing to answer from as few as TWO
returns. A sigma that comes out too SMALL inflates |z| and pushes `fair`
toward `fair_cap`, manufacturing edge that is not there — the mechanism behind
this project's first -$25 loss. So: no close_snipe until the buffer is deep
enough AND the process has been up long enough.

Both conditions are required on purpose. Sample count is the statistic that
actually matters, but it can be satisfied by a burst of duplicate polls after
a network stall; wall-clock uptime is an independent, un-gameable floor that
also guarantees at least one discovery pass has completed.

CircuitBreaker (M4 guard 3)
---------------------------
A per-event cap ($250) and a global open-notional cap ($1,000) bound a single
trade and simultaneous exposure, but nothing bounded a BAD DAY. An 85%-win
strategy at a $250 clip can still string losses. This stops the opening of new
positions once realized PnL since UTC midnight breaches a limit, or once N
resolved trades in a row have lost. It resets automatically at UTC midnight
(the daily figure is computed from UTC-midnight, so no cron is involved) and
can be released early by the operator with `python -m polybot.main resume`.

The manual override is deliberately NOT a permanent off switch:
  * it is scoped to one UTC day, and
  * it records the loss level at which it was granted, so the breaker re-arms
    if the day gets another full limit worse.
Both properties mean a forgotten override cannot silently disable the guard.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .logging_setup import get_logger

log = get_logger("risk")


def utc_midnight(now: Optional[float] = None) -> float:
    """Unix timestamp of the most recent UTC midnight at or before `now`."""
    t = time.time() if now is None else now
    return t - (t % 86400.0)


def utc_day_str(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    return dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Guard 2: warmup after restart
# ---------------------------------------------------------------------------

@dataclass
class WarmupStatus:
    ready: bool
    reason: str
    samples: int
    required_samples: int
    uptime_secs: float
    required_uptime_secs: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "oracle_samples": self.samples,
            "required_oracle_samples": self.required_samples,
            "uptime_secs": round(self.uptime_secs, 1),
            "required_uptime_secs": self.required_uptime_secs,
        }


class WarmupGate:
    """Blocks close_snipe until the vol buffer and the process are both warm."""

    def __init__(self, cfg: Optional[dict], started_at: Optional[float] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.min_samples = int(cfg.get("min_oracle_samples", 60))
        self.min_uptime_secs = float(cfg.get("min_uptime_secs", 90))
        self.log_every_secs = float(cfg.get("log_every_secs", 15))
        self.started_at = time.time() if started_at is None else started_at
        self._last_log = 0.0
        self._announced_ready = False
        self._lock = threading.Lock()

    def check(self, oracle, window_secs: float, now: Optional[float] = None) -> WarmupStatus:
        """`oracle` must expose `n_samples(window_secs)`. Missing method (an
        oracle implementation that predates this guard) is treated as 0
        samples, i.e. the SAFE direction — never as "warm"."""
        now = time.time() if now is None else now
        uptime = max(0.0, now - self.started_at)
        getter = getattr(oracle, "n_samples", None)
        try:
            samples = int(getter(window_secs)) if callable(getter) else 0
        except Exception:  # noqa: BLE001 - a broken oracle must not read as warm
            samples = 0
        if not self.enabled:
            return WarmupStatus(True, "warmup_disabled", samples, self.min_samples,
                                uptime, self.min_uptime_secs)
        if samples < self.min_samples:
            return WarmupStatus(False, "insufficient_oracle_samples", samples,
                                self.min_samples, uptime, self.min_uptime_secs)
        if uptime < self.min_uptime_secs:
            return WarmupStatus(False, "insufficient_uptime", samples, self.min_samples,
                                uptime, self.min_uptime_secs)
        return WarmupStatus(True, "warm", samples, self.min_samples, uptime,
                            self.min_uptime_secs)

    def log_progress(self, st: WarmupStatus, family: str,
                     now: Optional[float] = None) -> bool:
        """Rate-limited "still warming up" logging. Returns True if it logged.

        Deliberately noisy-once-then-throttled: a silent bot that is refusing
        to trade is indistinguishable from a bot with no signals, which is how
        an outage hides for a day.
        """
        now = time.time() if now is None else now
        with self._lock:
            if st.ready:
                if not self._announced_ready:
                    self._announced_ready = True
                    log.info("close_snipe WARMUP COMPLETE: %d oracle samples, uptime %.0fs "
                             "— trading enabled", st.samples, st.uptime_secs)
                    return True
                return False
            if now - self._last_log < self.log_every_secs:
                return False
            self._last_log = now
        log.warning("close_snipe WARMING UP (%s): family=%s oracle_samples=%d/%d "
                    "uptime=%.0f/%.0fs — refusing to trade on a thin vol buffer",
                    st.reason, family, st.samples, st.required_samples,
                    st.uptime_secs, st.required_uptime_secs)
        return True


# ---------------------------------------------------------------------------
# Guard 3: daily loss limit / consecutive-loss brake
# ---------------------------------------------------------------------------

@dataclass
class BreakerState:
    tripped: bool
    reasons: list = field(default_factory=list)
    daily_pnl: float = 0.0
    daily_limit_usd: Optional[float] = None
    consecutive_losses: int = 0
    max_consecutive_losses: Optional[int] = None
    override_active: bool = False
    override_detail: Optional[dict] = None
    utc_day: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tripped": self.tripped,
            "reasons": list(self.reasons),
            "daily_realized_pnl": round(self.daily_pnl, 4),
            "daily_loss_limit_usd": self.daily_limit_usd,
            "consecutive_losses": self.consecutive_losses,
            "max_consecutive_losses": self.max_consecutive_losses,
            "override_active": self.override_active,
            "override": self.override_detail,
            "utc_day": self.utc_day,
        }


def resolve_daily_limit(cfg: dict) -> Optional[float]:
    """Daily loss limit in POSITIVE dollars, or None when not configured.

    Both a percent-of-bankroll and an absolute dollar figure may be given; the
    TIGHTER (smaller) of the two wins, because this is a safety limit and the
    safe way to combine two stated intentions is to honour the stricter one.
    """
    cands = []
    pct = cfg.get("max_daily_loss_pct")
    bankroll = cfg.get("bankroll_usd")
    if pct is not None and bankroll is not None:
        try:
            v = abs(float(pct)) / 100.0 * abs(float(bankroll))
            if v > 0:
                cands.append(v)
        except (TypeError, ValueError):
            log.warning("risk.daily_loss_limit: bad pct/bankroll (%r/%r)", pct, bankroll)
    usd = cfg.get("max_daily_loss_usd")
    if usd is not None:
        try:
            v = abs(float(usd))
            if v > 0:
                cands.append(v)
        except (TypeError, ValueError):
            log.warning("risk.daily_loss_limit: bad max_daily_loss_usd (%r)", usd)
    return min(cands) if cands else None


class CircuitBreaker:
    """Stop opening new positions after a bad day. Read-only w.r.t. the ledger."""

    def __init__(self, cfg: Optional[dict], ledger, override_path: Optional[Path] = None):
        cfg = cfg or {}
        self.raw = cfg
        self.ledger = ledger
        daily = dict(cfg.get("daily_loss_limit") or {})
        streak = dict(cfg.get("consecutive_loss_brake") or {})
        self.daily_enabled = bool(daily.get("enabled", True))
        self.daily_limit = resolve_daily_limit(daily)
        self.streak_enabled = bool(streak.get("enabled", True))
        self.max_streak = int(streak.get("max_consecutive_losses", 4))
        self.override_path = Path(override_path) if override_path else None
        self._lock = threading.Lock()
        self._last_log = 0.0
        self.log_every_secs = float(cfg.get("log_every_secs", 60))

    # ---------------------------------------------------------- override I/O
    def read_override(self, now: Optional[float] = None) -> Optional[dict]:
        """Return today's override record, or None. An override from a
        previous UTC day is ignored (and left on disk for forensics)."""
        if self.override_path is None or not self.override_path.exists():
            return None
        try:
            with open(self.override_path) as f:
                rec = json.load(f)
        except Exception as exc:  # noqa: BLE001 - a corrupt override must not resume trading
            log.warning("risk override at %s is unreadable (%s) — ignoring it",
                        self.override_path, exc)
            return None
        if rec.get("utc_day") != utc_day_str(now):
            return None
        return rec

    def write_override(self, now: Optional[float] = None) -> dict:
        """Grant a manual resume for the remainder of this UTC day.

        Records the current daily PnL and resolved-trade count so the breaker
        re-arms if the day loses another full limit, or if a NEW losing streak
        forms after the override.
        """
        if self.override_path is None:
            raise RuntimeError("risk.override_path is not configured")
        pnl = float(self.ledger.pnl_today().get("net_pnl", 0.0))
        seq = self.ledger.recent_trade_pnls(limit=1)
        rec = {
            "utc_day": utc_day_str(now),
            "granted_at": time.time() if now is None else now,
            "pnl_at_override": pnl,
            "n_resolved_at_override": int(self.ledger.n_resolved_trades()),
            "last_resolved_ts": (seq[0][0] if seq else None),
        }
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.override_path.parent))
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, self.override_path)
        log.warning("RISK OVERRIDE GRANTED for %s at daily pnl $%.2f — trading resumes; "
                    "the breaker re-arms if the day loses another full limit",
                    rec["utc_day"], pnl)
        return rec

    def clear_override(self) -> bool:
        if self.override_path is None or not self.override_path.exists():
            return False
        self.override_path.unlink()
        log.warning("risk override cleared")
        return True

    # -------------------------------------------------------------- the gate
    def evaluate(self, now: Optional[float] = None) -> BreakerState:
        pnl_today = float(self.ledger.pnl_today().get("net_pnl", 0.0))
        streak = int(self.ledger.consecutive_losses(since_ts=utc_midnight(now)))
        override = self.read_override(now)
        st = BreakerState(
            tripped=False, daily_pnl=pnl_today, daily_limit_usd=self.daily_limit,
            consecutive_losses=streak,
            max_consecutive_losses=self.max_streak if self.streak_enabled else None,
            override_active=override is not None, override_detail=override,
            utc_day=utc_day_str(now),
        )

        # daily loss limit -------------------------------------------------
        if self.daily_enabled and self.daily_limit is not None:
            floor = -self.daily_limit
            if override is not None:
                # second chance, same size: re-arm one full limit below the
                # loss the operator explicitly accepted.
                floor = float(override.get("pnl_at_override", 0.0)) - self.daily_limit
            if pnl_today <= floor:
                st.tripped = True
                st.reasons.append(
                    f"daily_loss_limit: realized ${pnl_today:.2f} today <= ${floor:.2f}"
                )

        # consecutive-loss brake -------------------------------------------
        if self.streak_enabled and self.max_streak > 0:
            eff_streak = streak
            if override is not None:
                # only losses resolved AFTER the override count toward a new streak
                after = override.get("last_resolved_ts")
                if after is not None:
                    eff_streak = int(self.ledger.consecutive_losses(since_ts=float(after)))
                else:
                    eff_streak = int(self.ledger.consecutive_losses(
                        since_ts=float(override.get("granted_at", 0.0))))
            st.consecutive_losses = eff_streak
            if eff_streak >= self.max_streak:
                st.tripped = True
                st.reasons.append(
                    f"consecutive_loss_brake: {eff_streak} losing trades in a row "
                    f">= {self.max_streak}"
                )
        return st

    def allow_new_position(self, now: Optional[float] = None) -> BreakerState:
        st = self.evaluate(now)
        if st.tripped:
            self._maybe_log(st, now)
        return st

    def _maybe_log(self, st: BreakerState, now: Optional[float] = None) -> None:
        t = time.time() if now is None else now
        with self._lock:
            if t - self._last_log < self.log_every_secs:
                return
            self._last_log = t
        log.error("CIRCUIT BREAKER TRIPPED — refusing to open new positions: %s "
                  "(daily realized $%.2f, streak %d). Resume with "
                  "`python -m polybot.main resume`.",
                  "; ".join(st.reasons), st.daily_pnl, st.consecutive_losses)
