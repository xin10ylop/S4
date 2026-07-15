"""Structured logging to stdout + a rotating file handler.

Never log secrets: callers must not pass private keys / API creds into log
messages. This module does not scrub messages (that would be unreliable);
discipline is enforced by not threading secret values through the logger
anywhere else in the codebase (see execution.py).
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config

_FMT = "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class UTCFormatter(logging.Formatter):
    converter = staticmethod(__import__("time").gmtime)


def setup_logging(config: Config) -> logging.Logger:
    log_cfg = config.logging_cfg
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    root = logging.getLogger("polybot")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    formatter = UTCFormatter(_FMT, datefmt=_DATEFMT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_path = config.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=int(log_cfg.get("max_bytes", 10_485_760)),
        backupCount=int(log_cfg.get("backup_count", 5)),
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"polybot.{name}")
