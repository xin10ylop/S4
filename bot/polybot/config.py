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
