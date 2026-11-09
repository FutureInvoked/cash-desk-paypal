"""Partial refund of an overpayment: refund only the excess, never more than the customer overpaid."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cashdesk.actions import ACTION_TYPES, propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.policy import Policy
from cashdesk.queue import ApprovalQueue, PolicyBlocked
from tools.make_fixtures import txn_id

AS_OF = date(2026, 11, 7)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
USD = lambda v: Money(Decimal(v), "USD")


@pytest.fixture
def env(tmp_path):
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    props = propose(r, invs, txns, as_of=AS_OF)
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    return q, c, props


def overpay(props):
    (p,) = [p for p in props if p.type == "refund_overpayment"]
    return p


def test_refund_overpayment_is_an_allowed_action():
    assert "refund_overpayment" in ACTION_TYPES


def test_overpayment_gets_a_partial_refund_proposal_for_the_excess_only(env):
    _, _, props = env
    p = overpay(props)
    assert p.invoice_number == "NW-1014" and p.target == txn_id("NW-1014")
    assert p.amount == USD("45.00")
    (call,) = p.api_calls
    assert call["method"] == "POST" and call["path"] == f"/v2/payments/captures/{txn_id('NW-1014')}/refund"
    assert call["body"]["amount"] == {"currency_code": "USD", "value": "45.00"}
    assert "NW-1014" in call["body"]["note_to_payer"] and "45.00" in call["body"]["note_to_payer"]
    assert "overpaid" in p.reason.lower()


def test_no_overpayment_refund_for_duplicates_partials_or_fee_shortfalls(env):
    _, _, props = env
    assert [p.invoice_number for p in props if p.type == "refund_overpayment"] == ["NW-1014"]


def test_partial_refund_runs_for_the_excess_amount(env):
    q, c, props = env
    item = q.enqueue(overpay(props), invoice_due=USD("45.00"))
    assert item.status == "pending", item.policy
    q.approve(item.id, by="owner")
    done = q.execute(item.id, c)
    assert done.status == "executed"
    (call,) = c.calls
    assert call["op"] == "refund" and call["body"]["amount"] == {"currency_code": "USD", "value": "45.00"}
    assert call["headers"]["PayPal-Request-Id"] == item.request_id


def test_refund_above_the_overpaid_excess_is_blocked(env):
    q, c, props = env
    item = q.enqueue(overpay(props), invoice_due=USD("45.00"))
    q.approve(item.id, by="owner")

    def grow(d):  # someone edits proposal and body consistently to refund more than the excess
        d["amount"] = {"currency_code": "USD", "value": "95.00"}
        d["api_calls"][0]["body"]["amount"] = {"currency_code": "USD", "value": "95.00"}
    q._tamper_for_test(item.id, grow)
    with pytest.raises(PolicyBlocked) as e:
        q.execute(item.id, c)
    # caught by the approval fingerprint (the proposal changed) or the excess cap
    assert {"amount_cap", "integrity"} & {v["code"] for v in e.value.violations}
    assert c.calls == []


def test_overpayment_refund_without_a_known_excess_is_blocked(env):
    q, _, props = env
    item = q.enqueue(overpay(props))  # no limit stored
    assert item.status == "blocked"
    assert any(v["code"] == "amount_cap" for v in item.policy["violations"])


def test_desk_stores_the_excess_as_the_limit(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from cashdesk.service import Desk
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    d.refresh_proposals()
    (item,) = [i for i in d.approvals() if i["proposal"]["type"] == "refund_overpayment"]
    assert item["status"] == "pending"
    assert item["invoice_due"] == {"currency_code": "USD", "value": "45.00"}
