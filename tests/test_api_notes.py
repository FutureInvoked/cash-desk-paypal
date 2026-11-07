"""Guard: the API notes cover every PayPal call the client makes, with its doc source."""

from pathlib import Path

NOTES = Path(__file__).resolve().parent.parent / "docs" / "paypal-api-notes.md"

ENDPOINTS = [
    "/v1/oauth2/token",
    "/v1/reporting/transactions",
    "/invoices/{invoice_id}/remind",
    "/invoices/{invoice_id}/send",
    "/invoices/{invoice_id}/payments",
    "/v2/payments/captures/{capture_id}/refund",
    "PayPal-Request-Id",
    "https://api-m.sandbox.paypal.com",
]


def test_notes_cover_every_endpoint_we_call():
    text = NOTES.read_text()
    missing = [e for e in ENDPOINTS if e not in text]
    assert not missing, f"docs/paypal-api-notes.md is missing: {missing}"


def test_notes_cite_official_sources():
    text = NOTES.read_text()
    assert "github.com/paypal/paypal-rest-api-specifications" in text
    assert "developer.paypal.com" in text
