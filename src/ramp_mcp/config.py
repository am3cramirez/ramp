"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

BASE_URLS = {
    "production": "https://api.ramp.com",
    "sandbox": "https://demo-api.ramp.com",
}

# Read scopes requested by default when using the client-credentials flow.
# Only scopes enabled on your Ramp app can be granted; trim with RAMP_SCOPES.
DEFAULT_SCOPES = (
    "business:read transactions:read bills:read reimbursements:read users:read "
    "cards:read vendors:read departments:read locations:read funds:read "
    "receipts:read memos:read merchants:read entities:read statements:read "
    "purchase_orders:read spend_programs:read accounting:read"
)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    environment: str = "production"
    base_url: str = BASE_URLS["production"]
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    scopes: str = DEFAULT_SCOPES
    allow_writes: bool = False
    allow_vault: bool = False
    timeout: float = 60.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get("RAMP_ENV", "production").strip().lower()
        if env in {"demo", "sandbox", "test"}:
            env = "sandbox"
        elif env in {"prod", "production", "live"}:
            env = "production"
        else:
            raise ValueError("RAMP_ENV must be 'production' or 'sandbox'")
        base_url = os.environ.get("RAMP_BASE_URL") or BASE_URLS[env]
        return cls(
            environment=env,
            base_url=base_url.rstrip("/"),
            client_id=os.environ.get("RAMP_CLIENT_ID") or None,
            client_secret=os.environ.get("RAMP_CLIENT_SECRET") or None,
            access_token=os.environ.get("RAMP_ACCESS_TOKEN") or None,
            scopes=" ".join((os.environ.get("RAMP_SCOPES") or DEFAULT_SCOPES).replace(",", " ").split()),
            allow_writes=_bool("RAMP_ALLOW_WRITES"),
            allow_vault=_bool("RAMP_ALLOW_VAULT"),
            timeout=float(os.environ.get("RAMP_TIMEOUT", "60")),
            max_retries=int(os.environ.get("RAMP_MAX_RETRIES", "3")),
        )
