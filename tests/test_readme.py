"""R1: the README gives judges everything they need, and every claim points at real things."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def test_commands_judges_need():
    for cmd in ("make demo", "make test", "make audit-verify", "make eval", "PORT="):
        assert cmd in README, cmd


def test_sandbox_setup_and_ai_sections():
    low = README.lower()
    for s in ("sandbox", "transaction search", "paypal_client_id", "paypal_mode=sandbox",
              "claude-sonnet-5-5", "anthropic_api_key", "fallback"):
        assert s in low, s


def test_every_paypal_endpoint_is_listed():
    for path in ("/v1/oauth2/token", "/v1/reporting/transactions", "/v2/invoicing/invoices",
                 "/v2/invoicing/invoices/{invoice_id}/remind", "/v2/invoicing/invoices/{invoice_id}/send",
                 "/v2/invoicing/invoices/{invoice_id}/payments", "/v2/payments/captures/{capture_id}/refund"):
        assert path in README, path


def test_safety_design_is_explained():
    for s in ("policy", "approval", "PayPal-Request-Id", "hash", "audit"):
        assert s in README, s


def test_whats_new_and_licence():
    assert "What's new" in README and "2026-11-07" in README
    assert "MIT" in README


def test_linked_files_exist():
    for link in re.findall(r"\]\(([^)#]+)\)", README):
        if link.startswith("http"):
            continue
        assert (ROOT / link).exists(), link


def test_screenshots_are_shown():
    assert README.count("docs/screenshots/") >= 3


def test_no_secrets_or_logos():
    assert not re.search(r"(sk-ant|access_token\": \"A|PAYPAL_CLIENT_SECRET=[A-Za-z0-9])", README)
    assert "logo" not in README.lower()  # names only, no brand marks
