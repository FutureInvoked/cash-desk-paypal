"""The app refuses to talk to anything but the PayPal sandbox."""

import pytest

from cashdesk.config import SANDBOX_BASE_URL, sandbox_base_url


def test_default_is_sandbox(monkeypatch):
    monkeypatch.delenv("PAYPAL_BASE_URL", raising=False)
    assert sandbox_base_url() == "https://api-m.sandbox.paypal.com" == SANDBOX_BASE_URL


@pytest.mark.parametrize("url", [
    "https://api-m.paypal.com",                     # live
    "https://api.paypal.com",
    "http://api-m.sandbox.paypal.com",              # not TLS
    "https://api-m.sandbox.paypal.com.evil.example",
    "https://evil.example/api-m.sandbox.paypal.com",
    "https://api-m.sandbox.paypal.com:8443",
    "https://user@api-m.sandbox.paypal.com.evil.example",
    "https://api-m.sandbox.paypal.com/v1/../../other",
])
def test_anything_else_is_refused(monkeypatch, url):
    monkeypatch.setenv("PAYPAL_BASE_URL", url)
    with pytest.raises(ValueError, match="only https://api-m.sandbox.paypal.com"):
        sandbox_base_url()


def test_trailing_slash_is_fine(monkeypatch):
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com/")
    assert sandbox_base_url() == SANDBOX_BASE_URL
