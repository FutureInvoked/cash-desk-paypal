"""Follow-ups found on the live sandbox (2026-11-09):

1. A payment the desk recorded on an invoice keeps its match. The invoice is then MARKED_AS_PAID
   (no longer open), but its recorded payment's note names the PayPal transaction.
2. A duplicate or overpayment that has been refunded leaves the exceptions: a refund row points
   back at the payment through transaction_info.paypal_reference_id.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from decimal import Decimal

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.journal import ACCOUNTS, journal
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from tools.make_fixtures import txn_id

AS_OF = date(2026, 11, 7)
LUMEN, DUP, OVER = txn_id("NW-1021"), txn_id("NW-1015-dup"), txn_id("NW-1014")


def data():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return c.list_invoices(), list(txns)


def run(invs, txns):
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return r, propose(r, invs, txns, as_of=AS_OF), journal(r, txns)


def refund_of(txns, original_id, amount: str, rid: str):
    tmpl = next(t for t in txns if t.event_code == "T1107")
    orig = next(t for t in txns if t.id == original_id)
    return dataclasses.replace(tmpl, id=rid, reference_id=orig.id, amount=Money(-Decimal(amount), orig.amount.currency),
                               invoice_ref=None, paypal_invoice_id=None, note=None, subject=None,
                               initiated=orig.initiated)


def lines_for(lines, tid):
    out = {}
    for l in lines:
        if l["paypal_transaction_id"] == tid:
            out[l["account"]] = (l["debit"] or "0", l["credit"] or "0")
    return out


# ---------- 1. recorded payments ----------

def recorded(invs, number, tid):
    out = []
    for i in invs:
        if i.number == number:
            raw = dict(i.raw)
            raw["payments"] = {"paid_amount": i.total.to_paypal(), "transactions": [{
                "type": "EXTERNAL", "payment_id": "EXTR-63L72930K2468451K", "method": "PAYPAL",
                "payment_date": "2026-11-07", "amount": i.total.to_paypal(),
                "note": f"PayPal transaction {tid} (matched by Cash Desk)"}]}
            i = dataclasses.replace(i, status="MARKED_AS_PAID", raw=raw)
        out.append(i)
    return out


def test_recorded_payment_keeps_its_match_after_the_invoice_is_marked_paid():
    invs, txns = data()
    r, props, _ = run(recorded(invs, "NW-1021", LUMEN), txns)
    t = r.txn(LUMEN)
    assert t.outcome == "matched" and t.invoice_number == "NW-1021" and t.rule == "invoice_ref"
    assert "PayPal transaction" in t.evidence["invoice.payments.transactions[].note"]
    assert not t.needs_review and "recorded on NW-1021" in t.reason
    inv = r.invoice_by_number("NW-1021")
    assert inv.outcome == "paid" and inv.received == Money(Decimal("1500.00"), "USD") and LUMEN in inv.transactions
    assert not [p for p in props if p.type == "record_payment"]  # nothing left to record


def test_a_note_naming_another_transaction_does_not_match():
    invs, txns = data()
    r, _, _ = run(recorded(invs, "NW-1021", "9ZZ99999ZZ9999999"), txns)
    assert r.txn(LUMEN).outcome == "unmatched"


# ---------- 2. refunded duplicates and overpayments ----------

def test_refunded_duplicate_is_no_longer_an_exception():
    invs, txns = data()
    txns.append(refund_of(txns, DUP, "800.00", "REFUNDDUP0000001"))
    r, props, lines = run(invs, txns)
    t = r.txn(DUP)
    assert t.outcome == "refunded" and t.duplicate_of == txn_id("NW-1015")
    assert t.evidence["refunded_by.transaction_id"] == "REFUNDDUP0000001"
    assert "refunded" in t.reason.lower() and "duplicate" in t.reason.lower()
    assert r.invoice_by_number("NW-1015").outcome == "paid"
    assert not [p for p in props if p.type == "refund_capture"]
    # books: the duplicate went to customer credits, and the refund comes back out of them
    assert lines_for(lines, DUP)[ACCOUNTS["credits"]] == ("0", "800.00")
    assert lines_for(lines, "REFUNDDUP0000001")[ACCOUNTS["credits"]] == ("800.00", "0")


def test_overpayment_with_the_excess_refunded_becomes_a_clean_match():
    invs, txns = data()
    txns.append(refund_of(txns, OVER, "45.00", "REFUNDOVER000001"))
    r, props, lines = run(invs, txns)
    t = r.txn(OVER)
    assert t.outcome == "matched" and t.evidence["refunded_excess"] == "45.00 USD"
    assert "excess" in t.reason and "refunded" in t.reason
    inv = r.invoice_by_number("NW-1014")
    assert inv.outcome == "paid" and inv.received == Money(Decimal("450.00"), "USD")
    assert not [p for p in props if p.type == "refund_overpayment"]
    assert lines_for(lines, OVER)[ACCOUNTS["receivable"]] == ("0", "450.00")
    assert lines_for(lines, OVER)[ACCOUNTS["credits"]] == ("0", "45.00")
    assert lines_for(lines, "REFUNDOVER000001")[ACCOUNTS["credits"]] == ("45.00", "0")


def test_refunding_less_than_the_excess_leaves_it_overpaid():
    invs, txns = data()
    txns.append(refund_of(txns, OVER, "20.00", "REFUNDOVER000002"))
    r, _, _ = run(invs, txns)
    assert r.txn(OVER).outcome == "overpaid"


def test_summary_no_longer_counts_refunded_duplicates_as_unreconciled(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from cashdesk.service import Desk
    import copy
    from cashdesk.paypal.parse import parse_transaction
    invs, txns = data()
    tmpl = next(t for t in txns if t.event_code == "T1107")
    dup = next(t for t in txns if t.id == DUP)
    raw = copy.deepcopy(tmpl.raw)  # the desk stores and re-parses PayPal's raw JSON
    ti = raw["transaction_info"]
    ti.update(transaction_id="REFUNDDUP0000001", paypal_reference_id=DUP,
              transaction_amount={"currency_code": "USD", "value": "-800.00"},
              transaction_initiation_date=dup.raw["transaction_info"]["transaction_initiation_date"])
    ti.pop("invoice_id", None)
    extra = parse_transaction(raw)

    class C(FixturePayPalClient):
        def search_transactions(self, start, end):
            return super().search_transactions(start, end) + [extra]

    T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
    s = Desk(client=C(), reconciler=FallbackReconciler(), clock=lambda: T0).reconciliation()["summary"]
    # before: 1,124.00 USD unreconciled (two unbilled payments 275 + 49 and the 800 duplicate)
    assert s["unreconciled"]["USD"] == "324.00"


# ---------- review findings #1 and #5: refunds other than "exactly the excess" ----------

NW1001 = txn_id("NW-1001")


def test_refund_larger_than_the_excess_leaves_the_invoice_partly_unpaid():
    invs, txns = data()
    txns.append(refund_of(txns, OVER, "300.00", "REFUNDOVER000003"))
    r, props, lines = run(invs, txns)
    t = r.txn(OVER)
    assert t.outcome == "partial" and t.evidence["refunded_amount"] == "300.00 USD"
    inv = r.invoice_by_number("NW-1014")
    assert inv.outcome == "partial" and inv.received == Money(Decimal("195.00"), "USD")
    assert inv.outstanding == Money(Decimal("255.00"), "USD")
    # books: the payment split stays (AR 450 + credits 45); the refund takes the 45 credit first, then 255 of AR
    assert lines_for(lines, OVER)[ACCOUNTS["credits"]] == ("0", "45.00")
    ref = lines_for(lines, "REFUNDOVER000003")
    assert ref[ACCOUNTS["credits"]] == ("45.00", "0") and ref[ACCOUNTS["receivable"]] == ("255.00", "0")


def test_full_refund_of_an_overpayment_refunds_the_invoice():
    invs, txns = data()
    txns.append(refund_of(txns, OVER, "495.00", "REFUNDOVER000004"))
    r, _, lines = run(invs, txns)
    assert r.txn(OVER).outcome == "refunded"
    inv = r.invoice_by_number("NW-1014")
    assert inv.outcome == "refunded" and inv.received == Money(Decimal("0.00"), "USD")
    ref = lines_for(lines, "REFUNDOVER000004")
    assert ref[ACCOUNTS["credits"]] == ("45.00", "0") and ref[ACCOUNTS["receivable"]] == ("450.00", "0")


def test_small_refund_on_an_overpayment_keeps_the_rest_of_the_excess():
    invs, txns = data()
    txns.append(refund_of(txns, OVER, "20.00", "REFUNDOVER000005"))
    r, props, _ = run(invs, txns)
    t = r.txn(OVER)
    assert t.outcome == "overpaid" and t.evidence["refunded_amount"] == "20.00 USD"
    assert r.invoice_by_number("NW-1014").received == Money(Decimal("475.00"), "USD")
    # the overpayment refund proposal now offers only what is left
    (p,) = [p for p in props if p.type == "refund_overpayment"]
    assert p.amount == Money(Decimal("25.00"), "USD")


def test_partial_refund_of_a_normal_payment_reopens_the_invoice():
    invs, txns = data()
    txns.append(refund_of(txns, NW1001, "400.00", "REFUND1001000001"))
    r, _, lines = run(invs, txns)
    t = r.txn(NW1001)
    assert t.outcome == "partial" and t.evidence["refunded_amount"] == "400.00 USD"
    inv = r.invoice_by_number("NW-1001")
    assert inv.outcome in ("partial", "overdue") and inv.outstanding == Money(Decimal("400.00"), "USD")
    assert lines_for(lines, "REFUND1001000001")[ACCOUNTS["receivable"]] == ("400.00", "0")


def test_full_refund_of_a_normal_payment_marks_both_refunded():
    invs, txns = data()
    txns.append(refund_of(txns, NW1001, "1250.00", "REFUND1001000002"))
    r, _, _ = run(invs, txns)
    assert r.txn(NW1001).outcome == "refunded"
    assert r.invoice_by_number("NW-1001").outcome == "refunded"
