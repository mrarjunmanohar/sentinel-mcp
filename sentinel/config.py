"""Configuration for Shopify AI Sentinel."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .paths import db_path_for, env_path, stores_path


def _load_env(path: Path) -> dict[str, str]:
    """Load .env file into dict. No python-dotenv dependency."""
    env = {}
    try:
        f = open(path)
    except FileNotFoundError:
        return env
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            env[key] = value
    return env


@dataclass
class SentinelConfig:
    # Shopify store
    shop: str = ""
    token: str = ""
    api_version: str = "2026-01"
    store_name: str = ""           # Human display name, e.g. "Acme Store"
    currency: str = "USD"          # ISO currency code: USD, INR, EUR, GBP, etc.
    store_description: str = ""    # One-line description fed to the LLM, e.g. "sells handmade candles"

    # LLM (CLI/dev path only — the MCP server never calls an LLM itself)
    anthropic_api_key: str = ""

    # Report recipients — surfaced to the Claude host so it knows whom to
    # address email drafts to. Sentinel itself sends nothing.
    email_recipients: list[str] = field(default_factory=list)

    # System
    alert_sensitivity: str = "MED"  # LOW, MED, HIGH
    db_path: str = field(default_factory=lambda: str(db_path_for()))

    @property
    def graphql_endpoint(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.token,
        }


def _env_getter() -> Callable[[str, str], str]:
    """Load .env and return a getter that checks env vars then .env values."""
    env = _load_env(env_path())

    def _get(key: str, default: str = "") -> str:
        return os.environ.get(key, env.get(key, default))

    return _get


def _shared_secrets(_get: Callable[[str, str], str]) -> dict:
    """Return the shared-secret fields (API keys) from .env."""
    return dict(
        anthropic_api_key=_get("ANTHROPIC_API_KEY", ""),
    )


def get_config() -> SentinelConfig:
    """Load config from .env file and environment variables."""
    _get = _env_getter()

    recipients_raw = _get("EMAIL_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    shop = _get("SHOPIFY_SHOP", "")
    return SentinelConfig(
        shop=shop,
        token=_get("SHOPIFY_TOKEN", ""),
        api_version=_get("SHOPIFY_API_VERSION", "2026-01"),
        store_name=_get("STORE_NAME", shop.replace(".myshopify.com", "").replace("-", " ").title()),
        currency=_get("CURRENCY", "USD"),
        store_description=_get("STORE_DESCRIPTION", ""),
        **_shared_secrets(_get),
        email_recipients=recipients,
        alert_sensitivity=_get("ALERT_SENSITIVITY", "MED"),
        db_path=_get("DB_PATH", str(db_path_for())),
    )


def get_store_config(store_slug: str) -> SentinelConfig:
    """Load store-specific config from stores.json, with shared secrets from .env.

    Store fields (shop, token, store_name, etc.) come from stores.json[store_slug].
    Shared secrets (API keys) come from .env / environment variables.
    Each store gets its own SQLite DB at {SENTINEL_HOME}/data/{store_slug}.db.

    Raises FileNotFoundError if stores.json is missing.
    Raises KeyError if the store slug is not found.
    """
    try:
        with open(stores_path()) as f:
            stores = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{stores_path()} not found. "
            "Copy stores.json.example to stores.json and fill in your store details."
        )

    if store_slug not in stores:
        available = ", ".join(sorted(stores.keys()))
        raise KeyError(
            f"Store '{store_slug}' not found in stores.json. "
            f"Available stores: {available}"
        )

    store = stores[store_slug]
    _get = _env_getter()

    # Parse recipients from store config
    recipients = store.get("email_recipients", [])
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]

    shop = store.get("shop", "")
    return SentinelConfig(
        shop=shop,
        token=_get(f"SHOPIFY_TOKEN_{store_slug.upper()}", store.get("token", "")),
        api_version=store.get("api_version", _get("SHOPIFY_API_VERSION", "2026-01")),
        store_name=store.get("store_name", shop.replace(".myshopify.com", "").replace("-", " ").title()),
        currency=store.get("currency", "USD"),
        store_description=store.get("store_description", ""),
        **_shared_secrets(_get),
        email_recipients=recipients,
        alert_sensitivity=store.get("alert_sensitivity", "MED"),
        db_path=str(db_path_for(store_slug)),
    )
