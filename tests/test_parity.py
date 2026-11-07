"""S4: real sandbox responses parse exactly like the fixtures (recorded 2026-11-07, PII scrubbed)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from cashdesk.models import Invoice, Money
from cashdesk.paypal.parse import parse_invoice
from tests.test_fixtures import walk

ROOT = Path(__file__).resolve().parent.parent
REC = ROOT / "fixtures" / "sandbox-recorded"
FIX = json.loads((ROOT / "fixtures" / "invoices.json").read_text())["items"]

# Every invoice field the parser and matcher read. Each must be present in the real responses.
READ_PATHS_PAID = ["id", "status", "detail.invoice_number", "detail.currency_code", "detail.invoice_date",
                   "detail.payment_term.due_date", "amount.value", "amount.currency_code", "due_amount.value",
                   "primary_recipients.0.billing_info.email_address", "payments.paid_amount.value",
                   "payments.transactions.0.payment_id", "payments.transactions.0.amount.value",
                   "payments.transactions.0.payment_date"]


def rec(name):
    return json.loads((REC / f"{name}.json").read_text())


def dig(d, path):
    for part in path.split("."):
        d = d[int(part)] if part.isdigit() else d[part]
    return d


def test_every_field_we_read_exists_in_the_real_paid_invoice():
    real = rec("invoice_paid")
    missing = []
    for path in READ_PATHS_PAID:
        try:
            dig(real, path)
        except (KeyError, IndexError, TypeError):
            missing.append(path)
    assert not missing, missing


def test_unpaid_real_invoice_has_the_fields_for_overdue_detection():
    real = rec("invoice_unpaid")
    for path in ("detail.payment_term.due_date", "due_amount.value"):
        dig(real, path)
    # Real finding: the list endpoint's metadata has only create_time (no last_sent_time), so
    # "last sent" is optional everywhere; the parser and the reasons must cope without it.
    assert "last_sent_time" not in real["detail"].get("metadata", {})
    assert parse_invoice(real).last_sent is None


def test_fixture_invoices_use_only_keys_the_real_api_also_returns_at_the_top_level():
    real = set(rec("invoice_paid")) | set(rec("invoice_unpaid"))
    fixture_keys = set().union(*(set(i) for i in FIX))
    # fixtures may add documented optional blocks (refunds), but never invent undocumented ones
    assert fixture_keys - real <= {"refunds", "invoicer", "configuration", "items"}, fixture_keys - real


def test_real_paid_invoice_parses_like_a_fixture_paid_invoice():
    real = parse_invoice(rec("invoice_paid"))
    fix = parse_invoice(next(i for i in FIX if i["status"] == "PAID"))
    for inv in (real, fix):
        assert isinstance(inv, Invoice) and inv.status == "PAID"
        assert isinstance(inv.total.amount, Decimal) and inv.paid == inv.total
        assert inv.due.amount == Decimal("0") and inv.payment_ids and inv.number and inv.recipient_email
        assert isinstance(inv.invoice_date, date)
    assert real.total == Money(Decimal("125.00"), "USD") and real.number == "0011"
    assert real.recipient_email == "buyer@personal.example.com"


def test_real_unpaid_invoice_parses_like_a_fixture_open_invoice():
    real = parse_invoice(rec("invoice_unpaid"))
    assert real.status == "SENT" and real.paid == Money(Decimal("0"), "USD")
    assert real.due == real.total == Money(Decimal("240.00"), "USD")
    assert real.due_date is not None and real.payment_ids == []
