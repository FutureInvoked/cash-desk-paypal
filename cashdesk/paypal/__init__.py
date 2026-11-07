"""PayPal integration: one interface, a fixture client and (S1) a sandbox client."""

from __future__ import annotations

from ..config import load_settings
from .base import PayPalClient
from .fixture import FixturePayPalClient


def make_client() -> PayPalClient:
    settings = load_settings()
    if settings.paypal_mode == "fixtures":
        return FixturePayPalClient(settings.fixtures_dir)
    raise NotImplementedError("sandbox mode arrives with backlog item S1")


__all__ = ["PayPalClient", "FixturePayPalClient", "make_client"]
