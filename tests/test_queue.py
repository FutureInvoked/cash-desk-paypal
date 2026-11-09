"""E5: approval queue + executor. Rejected, expired, blocked or unapproved items never execute."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.policy import Policy, PolicyConfig
from cashdesk.queue import ApprovalQueue, NotExecutable, PolicyBlocked
from tools.make_fixtures import inv_id, txn_id

AS_OF = date(2026, 11, 7)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    props = {p.type + ":" + (p.invoice_number or p.target): p for p in propose(r, invs, txns, as_of=AS_OF)}
    clock = Clock(T0)
    q = ApprovalQueue(tmp_path / "q.db", clock=clock)
    return q, c, props, clock


REFUND = "refund_capture:NW-1015"


def approve_refund(q, item_id):
    """The 800.00 duplicate refund is above the two-person threshold: two different approvers."""
    q.approve(item_id, by="owner")
    return q.approve(item_id, by="bookkeeper")
REMIND = "send_reminder:NW-1016"
RECORD = "record_payment:NW-1021"
DRAFT = f"create_draft_invoice:{txn_id('unbilled-ravi')}"


def test_enqueue_runs_policy_and_stores_pending(env):
    q, c, props, _ = env
    item = q.enqueue(props[REFUND])
    assert item.status == "pending" and item.policy["allowed"]
    assert q.get(item.id).proposal.target == txn_id("NW-1015-dup")
    assert c.calls == []


def test_blocked_proposal_is_stored_as_blocked_and_never_runs(env):
    q, c, props, _ = env
    p = copy.deepcopy(props[REFUND])
    p.api_calls[0]["body"]["amount"]["value"] = "9999.00"
    item = q.enqueue(p)
    assert item.status == "blocked" and "integrity" in [v["code"] for v in item.policy["violations"]]
    with pytest.raises(NotExecutable):
        q.approve(item.id, by="owner")
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)
    assert c.calls == []


def test_unapproved_item_never_executes(env):
    q, c, props, _ = env
    item = q.enqueue(props[REMIND])
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)
    assert c.calls == []


def test_rejected_item_never_executes(env):
    q, c, props, _ = env
    item = q.enqueue(props[REMIND])
    q.reject(item.id, by="owner", note="customer called")
    assert q.get(item.id).status == "rejected"
    with pytest.raises(NotExecutable):
        q.approve(item.id, by="owner")
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)
    assert c.calls == []


def test_expired_item_cannot_be_approved_or_executed(env):
    q, c, props, clock = env
    item = q.enqueue(props[REMIND])
    clock.advance(hours=25)
    with pytest.raises(PolicyBlocked):
        q.approve(item.id, by="owner")
    assert q.get(item.id).status == "expired"
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)
    assert c.calls == []


def test_approved_then_expired_before_execution_does_not_run(env):
    q, c, props, clock = env
    item = q.enqueue(props[REMIND])
    q.approve(item.id, by="owner")
    clock.advance(hours=25)
    with pytest.raises(PolicyBlocked):
        q.execute(item.id, c)
    assert q.get(item.id).status == "expired" and c.calls == []


def test_expire_stale_sweeps_pending_items(env):
    q, _, props, clock = env
    a = q.enqueue(props[REMIND])
    clock.advance(hours=30)
    assert q.expire_stale() == [a.id]
    assert q.get(a.id).status == "expired"


def test_approved_refund_executes_with_idempotency_key(env):
    q, c, props, _ = env
    item = q.enqueue(props[REFUND])
    approve_refund(q, item.id)
    done = q.execute(item.id, c)
    assert done.status == "executed" and done.result["status"] == "COMPLETED"
    assert done.request_id == item.request_id
    (call,) = c.calls
    assert call["path"] == f"/v2/payments/captures/{txn_id('NW-1015-dup')}/refund"
    assert call["headers"]["PayPal-Request-Id"] == item.request_id
    assert call["body"]["amount"] == {"currency_code": "USD", "value": "800.00"}


def test_re_executing_returns_stored_result_without_a_second_call(env):
    q, c, props, _ = env
    item = q.enqueue(props[REFUND])
    approve_refund(q, item.id)
    first = q.execute(item.id, c)
    second = q.execute(item.id, FixturePayPalClient())  # a fresh client would double-refund if called
    assert second.result == first.result and len(c.calls) == 1


def test_crash_mid_execution_retries_with_the_same_request_id(env):
    q, c, props, _ = env
    item = q.enqueue(props[REFUND])
    approve_refund(q, item.id)

    class Boom(FixturePayPalClient):
        def execute_call(self, *a, **kw):  # the executor sends approved calls only through this
            raise ConnectionError("network dropped after send")

    with pytest.raises(ConnectionError):
        q.execute(item.id, Boom())
    assert q.get(item.id).status == "failed"
    done = q.execute(item.id, c)
    assert done.status == "executed" and c.calls[0]["headers"]["PayPal-Request-Id"] == item.request_id


def test_duplicate_intent_is_blocked_at_enqueue(env):
    q, c, props, _ = env
    q.enqueue(props[REFUND])
    second = q.enqueue(copy.deepcopy(props[REFUND]))
    assert second.status == "blocked"
    assert "duplicate_intent" in [v["code"] for v in second.policy["violations"]]


def test_rejected_intent_can_be_proposed_again(env):
    q, _, props, _ = env
    a = q.enqueue(props[REMIND])
    q.reject(a.id, by="owner")
    assert q.enqueue(copy.deepcopy(props[REMIND])).status == "pending"


def test_rate_cap_is_enforced_at_execution(env, tmp_path):
    _, c, props, clock = env
    q = ApprovalQueue(tmp_path / "rate.db", clock=clock, policy=Policy(PolicyConfig(max_per_hour=1)))
    a, b = q.enqueue(props[REMIND]), q.enqueue(props["send_reminder:NW-1017"])
    q.approve(a.id, by="owner")
    q.execute(a.id, c)
    with pytest.raises(PolicyBlocked):
        q.approve(b.id, by="owner")
    assert len(c.calls) == 1


def test_proposal_tampered_in_the_db_after_approval_is_blocked(env):
    q, c, props, _ = env
    item = q.enqueue(props[REFUND])
    approve_refund(q, item.id)
    q._tamper_for_test(item.id, lambda d: d["api_calls"][0]["body"]["amount"].update(value="999.00"))
    with pytest.raises(PolicyBlocked):
        q.execute(item.id, c)
    assert c.calls == []


def test_record_payment_checks_the_due_amount(env):
    q, c, props, _ = env
    blocked = q.enqueue(props[RECORD])  # no due amount known => blocked
    assert blocked.status == "blocked"
    q.reject  # noqa: B018 (reference only)
    q2_item = q.enqueue(copy.deepcopy(props[RECORD]), invoice_due=Money(Decimal("1500.00"), "USD"))
    assert q2_item.status == "pending"
    q.approve(q2_item.id, by="owner")
    done = q.execute(q2_item.id, c)
    assert done.result["payment_id"] and c.calls[0]["path"].endswith(f"{inv_id('NW-1021')}/payments")


def test_draft_invoice_creates_then_sends(env):
    q, c, props, _ = env
    item = q.enqueue(props[DRAFT])
    q.approve(item.id, by="owner")
    done = q.execute(item.id, c)
    assert [x["op"] for x in c.calls] == ["create_invoice", "send_invoice"]
    assert done.result["invoice_id"].startswith("INV2-")
    assert c.calls[0]["headers"]["PayPal-Request-Id"] == f"{item.request_id}-create"


def test_listing_and_decisions_are_recorded(env):
    q, _, props, clock = env
    a = q.enqueue(props[REMIND])
    clock.advance(minutes=3)
    q.approve(a.id, by="owner")
    got = q.get(a.id)
    assert got.decided_by == "owner" and got.decided_at == T0 + timedelta(minutes=3)
    assert [i.id for i in q.list()] == [a.id]
    assert [i.id for i in q.list(status="approved")] == [a.id]


def test_events_are_emitted_for_the_audit_log(env, tmp_path):
    _, c, props, clock = env
    events = []
    q = ApprovalQueue(tmp_path / "ev.db", clock=clock, on_event=events.append)
    a = q.enqueue(props[REFUND])
    approve_refund(q, a.id)
    q.execute(a.id, c)
    assert [e["event"] for e in events] == ["enqueued", "first_approval", "approved", "executing", "executed"]
    assert all(e["item_id"] == a.id for e in events)
