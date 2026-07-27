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
