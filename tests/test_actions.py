"""E3: the action proposer turns reconciliation results into proposals. Nothing executes here."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cashdesk.actions import ACTION_TYPES, Proposal, propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from tools.make_fixtures import inv_id, txn_id

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "fixtures" / "cases.json").read_text())
AS_OF = date(2026, 11, 7)


@pytest.fixture(scope="module")
def setup():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return c, invs, txns, r, propose(r, invs, txns, as_of=AS_OF)


def by(props, type_, target):
    return [p for p in props if p.type == type_ and p.target == target]


def test_action_types_are_exactly_the_five_allowed():
    assert ACTION_TYPES == {"send_reminder", "create_draft_invoice", "record_payment", "refund_capture",
                            "refund_overpayment"}


def test_every_oracle_proposed_action_is_proposed(setup):
    *_, props = setup
    for name, case in CASES.items():
        pa = case.get("proposed_action")
        if not pa:
            continue
        target = pa.get("transaction") or inv_id(pa["invoice"])
        assert by(props, pa["type"], target), (name, pa)


def test_full_proposal_set(setup):
    *_, props = setup
    got = sorted((p.type, p.invoice_number or p.target) for p in props)
    assert got == sorted([
        ("send_reminder", "NW-1016"),
        ("send_reminder", "NW-1017"),
        ("create_draft_invoice", txn_id("unbilled-ravi")),
        ("create_draft_invoice", txn_id("unbilled-ella")),
        ("record_payment", "NW-1021"),
        ("refund_capture", "NW-1015"),
        ("refund_overpayment", "NW-1014"),
    ])


def test_reminder_shows_the_exact_api_call(setup):
    *_, props = setup
    p = by(props, "send_reminder", inv_id("NW-1016"))[0]
    assert p.amount == Money(Decimal("2400.00"), "USD")
    (call,) = p.api_calls
    assert call["method"] == "POST" and call["path"] == f"/v2/invoicing/invoices/{inv_id('NW-1016')}/remind"
    assert call["body"]["send_to_recipient"] is True
    assert "NW-1016" in call["body"]["subject"] and "2400.00" in call["body"]["note"]


def test_refund_targets_the_duplicate_capture_for_its_amount(setup):
    *_, props = setup
    p = by(props, "refund_capture", txn_id("NW-1015-dup"))[0]
    (call,) = p.api_calls
    assert call["path"] == f"/v2/payments/captures/{txn_id('NW-1015-dup')}/refund"
    assert call["body"]["amount"] == {"currency_code": "USD", "value": "800.00"}
    assert call["headers"] == {"PayPal-Request-Id": "<set at execution>"}
    assert p.evidence["duplicate_of.transaction_id"] == txn_id("NW-1015")


def test_draft_invoice_for_an_unbilled_payment(setup):
    *_, props = setup
    p = by(props, "create_draft_invoice", txn_id("unbilled-ravi"))[0]
    create, send = p.api_calls
    assert create["path"] == "/v2/invoicing/invoices" and send["path"].endswith("/send")
    rec = create["body"]["primary_recipients"][0]["billing_info"]
    assert rec["email_address"] == "ravi.menon@example.com"
    assert create["body"]["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "275.00"}
    assert create["body"]["detail"]["currency_code"] == "USD"


def test_record_payment_for_a_suggested_match_needs_review(setup):
    *_, props = setup
    p = by(props, "record_payment", inv_id("NW-1021"))[0]
    (call,) = p.api_calls
    assert call["path"] == f"/v2/invoicing/invoices/{inv_id('NW-1021')}/payments"
    assert call["body"] == {"method": "PAYPAL", "payment_date": "2026-11-03",
                            "amount": {"currency_code": "USD", "value": "1500.00"},
                            "note": f"PayPal transaction {txn_id('NW-1021')} (matched by Cash Desk)"}
    assert p.needs_review


def test_no_actions_for_clean_open_pending_or_ignored_rows(setup):
    *_, props = setup
    targets = {p.target for p in props}
    for n in ("NW-1001", "NW-1018", "NW-1025", "NW-1026", "NW-1013"):
        assert inv_id(n) not in targets
    assert txn_id("withdrawal-1") not in targets


def test_proposals_are_deterministic_and_carry_reasons(setup):
    c, invs, txns, r, props = setup
    again = propose(r, invs, txns, as_of=AS_OF)
    assert [p.id for p in again] == [p.id for p in props]
    assert len({p.id for p in props}) == len(props)
    assert all(isinstance(p, Proposal) and p.reason and p.intent_key for p in props)


def test_proposing_never_touches_paypal(setup):
    c, *_ = setup
    assert c.calls == []


def test_reminder_subject_says_1_day_not_1_days():
    # Found in the live S3 run on 2026-11-08: "is 1 days overdue".
    from dataclasses import replace
    c = FixturePayPalClient()
    invs = c.list_invoices()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    overdue = [x for x in r.invoices if x.outcome == "overdue"]
    assert overdue
    r.invoices = [replace(x, days_overdue=1) if x.outcome == "overdue" else x for x in r.invoices]
    subjects = [p.api_calls[0]["body"]["subject"] for p in propose(r, invs, txns, as_of=AS_OF) if p.type == "send_reminder"]
    assert subjects and all(s.endswith("is 1 day overdue") for s in subjects), subjects
