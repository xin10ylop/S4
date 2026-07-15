"""Shared HTTP/TLS setup.

TLS must work BOTH in this dev sandbox (behind a proxy that requires a custom CA
bundle) AND on a plain production server (no proxy, system trust store is fine).

Resolution order for the CA bundle:
  1. env var POLYBOT_CA_BUNDLE, if set (explicit override).
  2. /root/.ccr/ca-bundle.crt, if that file exists (this dev sandbox).
  3. None -> requests/urllib use the system default trust store (production).
"""
from __future__ import annotations

import os
import ssl
from functools import lru_cache
from typing import Optional

import requests

_DEFAULT_SANDBOX_BUNDLE = "/root/.ccr/ca-bundle.crt"


@lru_cache(maxsize=1)
def ca_bundle_path() -> Optional[str]:
    """Return the CA bundle path to use, or None to use the system default."""
    env_path = os.environ.get("POLYBOT_CA_BUNDLE")
    if env_path:
        return env_path
    if os.path.exists(_DEFAULT_SANDBOX_BUNDLE):
        return _DEFAULT_SANDBOX_BUNDLE
    return None


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """An ssl.SSLContext for raw-socket clients (e.g. websockets)."""
    bundle = ca_bundle_path()
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """A shared requests.Session configured with the right CA bundle.

    requests accepts either a path (custom bundle) or True (system default) for
    the `verify` kwarg; we bake that choice into the session so callers don't
    need to think about it.
    """
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    bundle = ca_bundle_path()
    s.verify = bundle if bundle else True
    s.headers.update({"User-Agent": "polybot/0.1"})
    _session = s
    return s
