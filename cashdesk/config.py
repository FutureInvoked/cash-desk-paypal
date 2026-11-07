"""Runtime configuration read from the environment (never logs secret values)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


SANDBOX_HOST = "api-m.sandbox.paypal.com"


def sandbox_base_url() -> str:
    """The only PayPal base URL this app will ever call. Anything else (live included) is refused."""
    from urllib.parse import urlparse

    url = os.environ.get("PAYPAL_BASE_URL", SANDBOX_BASE_URL).rstrip("/")
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != SANDBOX_HOST or u.port not in (None, 443) or u.path not in ("", "/"):
        raise ValueError(f"refusing to use PayPal base URL {url!r}: only {SANDBOX_BASE_URL} is allowed")
    return SANDBOX_BASE_URL


@dataclass(frozen=True)
class Settings:
    paypal_mode: str
    var_dir: Path
    fixtures_dir: Path


def _has_keys() -> bool:
    return bool(os.environ.get("PAYPAL_CLIENT_ID")) and bool(os.environ.get("PAYPAL_CLIENT_SECRET"))


def load_settings() -> Settings:
    mode = os.environ.get("PAYPAL_MODE") or ("sandbox" if _has_keys() else "fixtures")
    if mode not in {"fixtures", "sandbox"}:
        raise ValueError(f"PAYPAL_MODE must be 'fixtures' or 'sandbox', got {mode!r}")
    return Settings(
        paypal_mode=mode,
        var_dir=Path(os.environ.get("CASHDESK_VAR_DIR", REPO_ROOT / "var")),
        fixtures_dir=Path(os.environ.get("CASHDESK_FIXTURES_DIR", REPO_ROOT / "fixtures")),
    )
