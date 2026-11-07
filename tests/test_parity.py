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


# ---------------------------------------------------------------- S4b: Transaction Search
from cashdesk.matcher import match, paypal_fee  # noqa: E402
from cashdesk.paypal.parse import parse_transaction  # noqa: E402

TXN_FIX = json.loads((ROOT / "fixtures" / "transactions.json").read_text())["transaction_details"]
READ_PATHS_TXN = ["transaction_info.transaction_id", "transaction_info.transaction_event_code",
                  "transaction_info.transaction_status", "transaction_info.transaction_initiation_date",
                  "transaction_info.transaction_amount.value", "transaction_info.transaction_amount.currency_code",
                  "transaction_info.fee_amount.value", "transaction_info.invoice_id",
                  "payer_info.email_address", "payer_info.payer_name.given_name",
                  "cart_info.paypal_invoice_id", "cart_info.item_details.0.item_name"]


def real_rows():
    return rec("transactions_page")["transaction_details"]


def test_real_transaction_page_has_the_envelope_the_client_pages_through():
    page = rec("transactions_page")
    for k in ("transaction_details", "total_pages", "page", "last_refreshed_datetime"):
        assert k in page


def test_every_field_we_read_exists_in_the_real_payment_row():
    pay = next(t for t in real_rows() if t["transaction_info"]["transaction_event_code"] == "T0006")
    missing = []
    for path in READ_PATHS_TXN:
        try:
            dig(pay, path)
        except (KeyError, IndexError, TypeError):
            missing.append(path)
    assert not missing, missing


def test_real_payment_row_parses_like_a_fixture_payment_row():
    real = parse_transaction(next(t for t in real_rows() if t["transaction_info"]["invoice_id"] == "0011"
                                  and t["transaction_info"]["transaction_event_code"] == "T0006"))
    fix = parse_transaction(next(t for t in TXN_FIX if t["transaction_info"]["transaction_event_code"] == "T0006"))
    for t in (real, fix):
        assert isinstance(t.amount.amount, Decimal) and t.amount.amount > 0
        assert t.fee is not None and t.fee.amount < 0 and t.fee.currency == t.amount.currency
        assert t.status == "S" and t.initiated is not None
    assert real.amount == Money(Decimal("125.00"), "USD") and real.invoice_ref == "0011"
    assert real.paypal_invoice_id and real.paypal_invoice_id.startswith("INV2-")
    assert real.payer_email == "buyer@personal.example.com" and real.items == ("Menu board redesign",)


def test_real_currency_conversion_rows_carry_the_invoice_id_but_are_ignored():
    # Real finding: the business account's primary currency is CZK, so PayPal books each USD payment
    # as T0006 plus two T0200 conversion rows, and the conversion rows repeat invoice_id. They must not
    # count as payments (no fake duplicate/overpaid on 0011).
    rows = [parse_transaction(t) for t in real_rows()]
    conv = [t for t in rows if t.event_code == "T0200"]
    assert conv and all(t.invoice_ref for t in conv)
    inv = parse_invoice(rec("invoice_paid"))
    r = match([inv], [t for t in rows if t.invoice_ref == "0011"], as_of=date(2026, 11, 8))
    outcomes = sorted(x.outcome for x in r.transactions)
    assert outcomes == ["ignored", "ignored", "matched"], outcomes
    assert r.invoice_by_number("0011").outcome == "paid"


def test_real_fees_stay_under_the_fee_tolerance_ceiling():
    # The fee-short rule allows a shortfall up to the standard fee + slack. Real sandbox fees were lower
    # (4.55 on 125.00, 3.22 on 86.00), so a payer who deducts the real fee still falls inside the rule.
    for t in real_rows():
        ti = t["transaction_info"]
        if ti["transaction_event_code"] != "T0006":
            continue
        p = parse_transaction(t)
        assert -p.fee.amount <= paypal_fee(p.amount).amount
