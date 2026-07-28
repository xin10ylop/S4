"""Configuration loading: bot/config.yaml + environment overrides.

Secrets (private keys, API creds) are read from environment variables ONLY,
never from config.yaml, and never logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# NB: plain stdlib logging, not .logging_setup — that module imports Config, so
# using it here would be a circular import. Config is loaded before logging is
# configured anyway, so these lines go to the root handler.
import logging

log = logging.getLogger("polybot.config")

BOT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BOT_ROOT.parent
DEFAULT_CONFIG_PATH = BOT_ROOT / "config.yaml"


def _resolve_path(p: str) -> Path:
    """Config paths are written relative to the repo root (e.g. 'bot/data/x')."""
    path = Path(p)
    if path.is_absolute():
        return path
    return REPO_ROOT / path


@dataclass
class FamilyConfig:
    name: str
    enabled: bool
    duration_secs: int
    oracle: str
    close_snipe: bool
    settle_sweep: bool


@dataclass
class Config:
    raw: Dict[str, Any]
    path: Path

    # --- convenience accessors -------------------------------------------------
    @property
    def paper(self) -> bool:
        """True unless explicitly disabled in config AND live is armed via env.

        Real-money orders require ALL of:
          1. mode.paper: false in config.yaml
          2. POLYBOT_LIVE=1 in the environment
          3. a private key present (POLYBOT_PK)
        Any single guard missing => paper mode. See execution.py for the final gate.
        """
        cfg_paper = bool(self.raw.get("mode", {}).get("paper", True))
        env_live = os.environ.get("POLYBOT_LIVE", "0") == "1"
        return cfg_paper or not env_live

    @property
    def gamma_base(self) -> str:
        return self.raw["endpoints"]["gamma_base"]

    @property
    def clob_base(self) -> str:
        return self.raw["endpoints"]["clob_base"]

    @property
    def clob_ws(self) -> str:
        return self.raw["endpoints"]["clob_ws"]

    @property
    def binance_rest_base(self) -> str:
        return self.raw["endpoints"]["binance_rest_base"]

    @property
    def binance_ws_base(self) -> str:
        return self.raw["endpoints"]["binance_ws_base"]

    @property
    def binance_futures_rest_base(self) -> str:
        """USD-M futures REST base. Only HYPE resolves on futures, and HYPE is
        disabled, so this is unused today — but it must not silently default to
        the spot base, because that would give HYPE a wrong-feed price instead
        of no price. Confirmed unreachable from this sandbox (HTTP 451)."""
        return self.raw["endpoints"].get("binance_futures_rest_base",
                                          "https://fapi.binance.com")

    @property
    def fee_rate(self) -> float:
        return float(self.raw["fees"]["fee_rate"])

    @property
    def chainlink_cfg(self) -> Dict[str, Any]:
        """Chainlink Data Streams oracle settings (see oracle.ChainlinkOracle).

        Every endpoint here is public and unauthenticated — there is no secret
        to put in this dict. If a future transport ever needs a credentialled
        URL (e.g. a paid RPC or Chainlink's own gated Data Streams API), it
        must be supplied through the environment variable named by
        `rpc_url_env` and NEVER written into config.yaml.
        """
        cfg = dict((self.raw.get("oracles", {}) or {}).get("chainlink", {}) or {})
        onchain = dict(cfg.get("onchain_check") or {})
        env_var = onchain.get("rpc_url_env")
        if env_var and os.environ.get(env_var):
            onchain["rpc_url"] = os.environ[env_var]
            cfg["onchain_check"] = onchain
        return cfg

    def families(self) -> Dict[str, FamilyConfig]:
        out = {}
        for name, d in self.raw["families"].items():
            out[name] = FamilyConfig(
                name=name,
                enabled=bool(d.get("enabled", True)),
                duration_secs=int(d["duration_secs"]),
                oracle=str(d["oracle"]),
                close_snipe=bool(d.get("close_snipe", False)),
                settle_sweep=bool(d.get("settle_sweep", False)),
            )
        return out

    @property
    def snipe_cfg(self) -> Dict[str, Any]:
        return self.raw["strategy"]["close_snipe"]

    # --- multi-coin (M5) ------------------------------------------------------
    # Three INDEPENDENT lists, deliberately not one. Collapsing them is how a
    # recon result ("this coin exists") turns into a trade ("this coin is
    # profitable") without anyone deciding it should.
    #
    #   discovery.hourly_coins            -> which coins we even look up
    #   strategy.close_snipe.shadow_coins -> evaluated + logged, can NEVER fill
    #   strategy.close_snipe.allowed_coins-> the only coins that may be filled
    #
    # Every one of them defaults to bitcoin-only when the key is absent, so a
    # pre-M5 config.yaml keeps exactly today's behaviour.
    def hourly_coins(self) -> list:
        """Coins whose hourly markets are DISCOVERED. Never a trading permission."""
        coins = self.discovery_cfg.get("hourly_coins")
        if not coins:
            return ["bitcoin"]
        return [str(c) for c in coins]

    def allowed_coins(self) -> list:
        """Coins that may actually be FILLED. Defaults to bitcoin alone.

        M1 §7 item 4 / M3 §12 item 1: widening the slug regex must not by itself
        widen what trades. Five of the six non-BTC coins measured at or below
        zero, so an implicit allowlist is a direct route to -$1.81/day (BNB).
        """
        coins = self.snipe_cfg.get("allowed_coins")
        if not coins:
            return ["bitcoin"]
        return [str(c) for c in coins]

    def shadow_coins(self) -> list:
        """Coins evaluated and logged but structurally unable to fill.

        A coin listed here (and not in allowed_coins) produces real signals in
        the ledger and the log, priced off its OWN verified oracle, but the fill
        dispatch is skipped. This is how out-of-sample live evidence gets
        collected for a coin whose backtest did not survive stress, at zero
        risk. Defaults to empty.
        """
        coins = self.snipe_cfg.get("shadow_coins") or []
        allowed = set(self.allowed_coins())
        # A coin in BOTH lists is a config contradiction. Resolve it the safe
        # way: shadow wins, i.e. it does not trade. Loudly, at load time.
        out = []
        for c in coins:
            c = str(c)
            if c in allowed:
                log.warning("coin %r appears in BOTH allowed_coins and shadow_coins; "
                            "treating it as SHADOW (no fills). Remove it from one list.", c)
            out.append(c)
        return out

    def coin_cap_usd(self, coin: str) -> float:
        """Per-event clip for a coin. `sizing.per_coin_cap_usd` overrides the
        shared `sizing.per_event_cap_usd`.

        M3 §6: every coin including BTC offers a median of only $6-12 of
        fillable notional per signal, so a big cap buys little upside and sizes
        the tail loss. Any new coin starts small on purpose.
        """
        per_coin = (self.sizing_cfg.get("per_coin_cap_usd") or {})
        if coin in per_coin:
            return float(per_coin[coin])
        return float(self.sizing_cfg["per_event_cap_usd"])

    @property
    def max_book_age_s(self) -> Optional[float]:
        """`strategy.close_snipe.max_book_age_s` — refuse to act on a book the
        CLOB last updated more than this many seconds ago. None/absent = off."""
        v = self.snipe_cfg.get("max_book_age_s")
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            log.warning("bad max_book_age_s %r — treating the guard as OFF", v)
            return None
        return f if f > 0 else None

    @property
    def settle_cfg(self) -> Dict[str, Any]:
        return self.raw["strategy"]["settle_sweep"]

    # --- M4 risk guards -------------------------------------------------------
    # All three read with defaults so a config.yaml written before M4 still
    # loads. The defaults are the SAFE ones: the adverse-size filter is the one
    # guard that defaults OFF, and only because measurement said so (see
    # audit/M4_risk_guards.md §1) — warmup and the circuit breaker default ON
    # even when the file says nothing.
    @property
    def adverse_size_cfg(self) -> Dict[str, Any]:
        """strategy.close_snipe.adverse_size — the per-level size filter."""
        cfg = dict(self.snipe_cfg.get("adverse_size") or {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("mode", "cap")
        cfg.setdefault("max_size_ratio", 8.0)
        cfg.setdefault("history_n", 200)
        cfg.setdefault("min_samples", 30)
        return cfg

    @property
    def warmup_cfg(self) -> Dict[str, Any]:
        """strategy.close_snipe.warmup — post-restart trading lockout."""
        cfg = dict(self.snipe_cfg.get("warmup") or {})
        cfg.setdefault("enabled", True)
        cfg.setdefault("min_oracle_samples", 60)
        # 120 == one full vol_window_secs, and matches the shipped config.yaml.
        # A fallback LOOSER than what we ship would mean a pre-M4 config.yaml
        # silently gets a weaker guard than the documented one.
        cfg.setdefault("min_uptime_secs", 120)
        cfg.setdefault("log_every_secs", 15)
        return cfg

    @property
    def risk_cfg(self) -> Dict[str, Any]:
        """Top-level `risk:` block — daily loss limit + consecutive-loss brake."""
        cfg = dict(self.raw.get("risk") or {})
        daily = dict(cfg.get("daily_loss_limit") or {})
        daily.setdefault("enabled", True)
        daily.setdefault("bankroll_usd", 1250)
        daily.setdefault("max_daily_loss_pct", 8.0)
        cfg["daily_loss_limit"] = daily
        streak = dict(cfg.get("consecutive_loss_brake") or {})
        streak.setdefault("enabled", True)
        streak.setdefault("max_consecutive_losses", 4)
        cfg["consecutive_loss_brake"] = streak
        return cfg

    @property
    def risk_override_path(self) -> Path:
        """Where `python -m polybot.main resume` writes the manual override.
        Defaults next to the ledger so `reset_paper_data.sh` archives it."""
        p = (self.raw.get("risk") or {}).get("override_path")
        if p:
            return _resolve_path(str(p))
        return self.sqlite_path.parent / "risk_override.json"

    @property
    def sizing_cfg(self) -> Dict[str, Any]:
        return self.raw["sizing"]

    @property
    def execution_cfg(self) -> Dict[str, Any]:
        return self.raw["execution"]

    @property
    def resolution_cfg(self) -> Dict[str, Any]:
        return self.raw["resolution"]

    @property
    def discovery_cfg(self) -> Dict[str, Any]:
        return self.raw["discovery"]

    @property
    def logging_cfg(self) -> Dict[str, Any]:
        return self.raw["logging"]

    @property
    def status_cfg(self) -> Dict[str, Any]:
        return self.raw["status"]

    @property
    def sqlite_path(self) -> Path:
        return _resolve_path(self.raw["storage"]["sqlite_path"])

    @property
    def fills_csv(self) -> Path:
        return _resolve_path(self.raw["storage"]["fills_csv"])

    @property
    def pnl_csv(self) -> Path:
        return _resolve_path(self.raw["storage"]["pnl_csv"])

    @property
    def log_file(self) -> Path:
        return _resolve_path(self.logging_cfg["file"])

    @property
    def status_json_path(self) -> Path:
        return _resolve_path(self.raw["storage"]["sqlite_path"]).parent / "status.json"

    @property
    def http_port(self) -> int:
        env_var = self.status_cfg["http_port_env"]
        default = int(self.status_cfg["http_port_default"])
        return int(os.environ.get(env_var, default))


def load_config(path: Optional[str] = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r") as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, path=cfg_path)
