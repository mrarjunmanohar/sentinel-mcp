"""Filesystem locations for Sentinel config, data, and reports.

Claude Desktop launches MCP servers with an arbitrary CWD, and pip/uvx/.mcpb
installs put the package in ephemeral or managed directories — so nothing may
live relative to CWD. Everything lives under SENTINEL_HOME.

Resolution order for the home directory:
  1. SENTINEL_HOME environment variable
  2. Legacy dev-repo layout (the sentinel/ package dir) — used only if it
     already holds a stores.json or data/ dir, so existing checkouts keep
     working without migration
  3. ~/Sentinel (default; created lazily on first write)
"""

import os
from pathlib import Path

# Legacy pre-MCP layout kept config/data inside the package directory.
_PACKAGE_DIR = Path(__file__).parent


def sentinel_home() -> Path:
    env = os.environ.get("SENTINEL_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if (_PACKAGE_DIR / "stores.json").exists() or (_PACKAGE_DIR / "data").exists():
        return _PACKAGE_DIR
    return Path.home() / "Sentinel"


def env_path() -> Path:
    return sentinel_home() / ".env"


def stores_path() -> Path:
    return sentinel_home() / "stores.json"


def data_dir() -> Path:
    return sentinel_home() / "data"


def reports_dir(store_slug: str | None = None) -> Path:
    base = sentinel_home() / "reports"
    return base / store_slug if store_slug else base


def db_path_for(store_slug: str | None = None) -> Path:
    return data_dir() / (f"{store_slug}.db" if store_slug else "sentinel.db")
