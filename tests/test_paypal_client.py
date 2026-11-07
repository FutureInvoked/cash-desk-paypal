"""F4: one PayPalClient interface; the fixture client parses real response shapes into models."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from cashdesk.models import Invoice, Money, Transaction
from cashdesk.paypal import FixturePayPalClient, PayPalClient, make_client
from cashdesk.paypal.parse import parse_invoice, parse_money, parse_transaction
from tools.make_fixtures import inv_id, txn_id

START = datetime(2026, 10, 8, tzinfo=timezone.utc)
END = datetime(2026, 11, 7, tzinfo=timezone.utc)


@pytest.fixture
def client():
    return FixturePayPalClient()


def test_fixture_client_implements_the_interface(client):
    assert isinstance(client, PayPalClient)
    abstract = {n for n, m in inspect.getmembers(PayPalClient) if getattr(m, "__isabstractmethod__", False)}
    assert {"list_invoices", "search_transactions", "send_reminder", "create_draft_invoice",
            "send_invoice", "record_payment", "refund_capture"} <= abstract


def test_money_is_decimal_never_float():
    m = parse_money({"currency_code": "USD", "value": "-17.94"})
    assert m == Money(Decimal("-17.94"), "USD")
    assert isinstance(m.amount, Decimal)
    with pytest.raises(TypeError):
        Money(1.5, "USD")  # floats are rejected outright
    assert parse_money(None) is None


def test_money_arithmetic_refuses_mixed_currencies():
    with pytest.raises(ValueError):
        Money(Decimal("1"), "USD") + Money(Decimal("1"), "EUR")
    assert Money(Decimal("1.10"), "USD") + Money(Decimal("2.05"), "USD") == Money(Decimal("3.15"), "USD")


def test_lists_all_invoices_as_models(client):
    invs = client.list_invoices()
    assert len(invs) == 26
    assert all(isinstance(i, Invoice) for i in invs)
    i = next(x for x in invs if x.number == "NW-1013")
    assert i.id == inv_id("NW-1013")
    assert i.status == "PARTIALLY_PAID"
    assert i.total == Money(Decimal("1200.00"), "USD")
    assert i.due == Money(Decimal("600.00"), "USD")
    assert i.paid == Money(Decimal("600.00"), "USD")
    assert i.invoice_date == date(2026, 10, 13) and i.due_date == date(2026, 11, 12)
    assert i.recipient_email == "accounts@oakmont.example.com"
    assert i.recipient_business == "Oakmont Realty"
    assert i.payment_ids == [txn_id("NW-1013")]
    assert i.raw["detail"]["invoice_number"] == "NW-1013"


def test_eur_invoice_keeps_its_currency(client):
    i = next(x for x in client.list_invoices() if x.number == "NW-1022")
    assert i.total.currency == "EUR"


def test_search_transactions_parses_every_field_we_use(client):
    txns = client.search_transactions(START, END)
    assert len(txns) == 26
    t = next(x for x in txns if x.id == txn_id("NW-1001"))
    assert isinstance(t, Transaction)
    assert t.event_code == "T0006" and t.status == "S"
    assert t.amount == Money(Decimal("1250.00"), "USD")
    assert t.fee == Money(Decimal("-44.12"), "USD")
    assert t.payer_email == "dana@harborlane.example.com"
    assert t.payer_name == "Dana Whitfield"
    assert t.invoice_ref == "NW-1001"
    assert t.paypal_invoice_id == inv_id("NW-1001")
    assert t.initiated == datetime(2026, 10, 9, 14, 22, 5, tzinfo=timezone.utc)


def test_refund_row_points_back_to_original(client):
    refund = next(x for x in client.search_transactions(START, END) if x.event_code == "T1107")
    assert refund.reference_id == txn_id("NW-1024")
    assert refund.amount.amount == Decimal("-300.00")
    assert refund.fee is None


def test_withdrawal_has_no_payer(client):
    w = next(x for x in client.search_transactions(START, END) if x.event_code == "T0400")
    assert w.payer_email is None and w.payer_name is None


def test_search_window_filters_by_date(client):
    txns = client.search_transactions(datetime(2026, 11, 1, tzinfo=timezone.utc), END)
    assert txns and all(t.initiated >= datetime(2026, 11, 1, tzinfo=timezone.utc) for t in txns)


def test_search_window_over_31_days_is_rejected(client):
    with pytest.raises(ValueError, match="31 days"):
        client.search_transactions(datetime(2026, 9, 1, tzinfo=timezone.utc), END)


def test_fixture_writes_are_simulated_and_recorded(client):
    iid = inv_id("NW-1016")
    assert client.send_reminder(iid, note="Friendly reminder", request_id="cashdesk-1") == {"status": 204}
    refund = client.refund_capture(txn_id("NW-1015-dup"), Money(Decimal("800.00"), "USD"),
                                   request_id="cashdesk-2", note_to_payer="Duplicate refunded")
    assert refund["status"] == "COMPLETED" and refund["amount"] == {"currency_code": "USD", "value": "800.00"}
    # same idempotency key returns the same result and is not recorded twice
    again = client.refund_capture(txn_id("NW-1015-dup"), Money(Decimal("800.00"), "USD"),
                                  request_id="cashdesk-2", note_to_payer="Duplicate refunded")
    assert again == refund
    assert [c["op"] for c in client.calls] == ["remind", "refund"]
    assert client.calls[1]["path"] == f"/v2/payments/captures/{txn_id('NW-1015-dup')}/refund"
    assert client.calls[1]["headers"]["PayPal-Request-Id"] == "cashdesk-2"


def test_fixture_draft_invoice_and_record_payment(client):
    draft = client.create_draft_invoice(
        recipient_email="ravi.menon@example.com", recipient_name="Ravi Menon",
        item="Logo refresh deposit", amount=Money(Decimal("275.00"), "USD"), request_id="cashdesk-3")
    assert draft.status == "DRAFT" and draft.total == Money(Decimal("275.00"), "USD")
    assert client.send_invoice(draft.id, request_id="cashdesk-4")["status"] in (200, 202)
    rec = client.record_payment(inv_id("NW-1021"), Money(Decimal("1500.00"), "USD"),
                                payment_date=date(2026, 11, 3), method="PAYPAL",
                                note="Paid from owner's personal account", request_id="cashdesk-5")
    assert rec["payment_id"]
    # fixture data on disk is never modified
    assert len(FixturePayPalClient().list_invoices()) == 26


def test_unknown_invoice_raises(client):
    with pytest.raises(KeyError):
        client.send_reminder("INV2-NOPE-NOPE-NOPE-NOPE", note="x", request_id="r")


def test_make_client_defaults_to_fixtures(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PAYPAL_MODE", raising=False)
    assert isinstance(make_client(), FixturePayPalClient)


def test_parse_functions_accept_raw_dicts(client):
    raw_inv = client.list_invoices()[0].raw
    assert parse_invoice(raw_inv).id == raw_inv["id"]
    raw_t = client.search_transactions(START, END)[0].raw
    assert parse_transaction(raw_t).id == raw_t["transaction_info"]["transaction_id"]
