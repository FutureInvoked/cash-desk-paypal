"""Two-person approval: a refund above the threshold needs two different approvers before it can run."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.policy import Policy, PolicyConfig
from cashdesk.queue import ApprovalQueue, NotExecutable
from tests.accounts_helper import sign_in

AS_OF = date(2026, 11, 7)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
USD = lambda v: Money(Decimal(v), "USD")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    props = {p.type: p for p in propose(r, invs, txns, as_of=AS_OF) if p.type != "send_reminder"}
    props["send_reminder"] = next(p for p in propose(r, invs, txns, as_of=AS_OF)
                                  if p.type == "send_reminder" and p.invoice_number == "NW-1016")  # 2,400.00
    clock = Clock(T0)
    events = []
    q = ApprovalQueue(tmp_path / "q.db", clock=clock, on_event=events.append)
    return q, c, props, clock, events


def test_default_threshold_is_500_for_refunds():
    cfg = PolicyConfig()
    assert cfg.second_approval_over == Decimal("500.00")
    assert cfg.second_approval_types == frozenset({"refund_capture", "refund_overpayment"})


def test_large_refund_needs_a_second_different_approver(env):
    q, c, props, _, events = env
    item = q.enqueue(props["refund_capture"])  # 800.00 duplicate
    assert item.to_dict()["approvals_needed"] == 2
    first = q.approve(item.id, by="owner")
    assert first.status == "awaiting_second" and first.first_by == "owner"
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)
    with pytest.raises(NotExecutable, match="different person"):
        q.approve(item.id, by=" Owner ")  # same person, other spelling
    second = q.approve(item.id, by="bookkeeper")
    assert second.status == "approved" and second.decided_by == "bookkeeper" and second.first_by == "owner"
    done = q.execute(item.id, c)
    assert done.status == "executed" and len(c.calls) == 1
    assert [e["event"] for e in events] == ["enqueued", "first_approval", "approved", "executing", "executed"]
    assert events[1]["by"] == "owner" and events[2]["by"] == "bookkeeper" and events[2]["first_by"] == "owner"


def test_small_refund_needs_one_approver(env):
    q, c, props, _, _ = env
    item = q.enqueue(props["refund_overpayment"], invoice_due=USD("45.00"))
    assert item.to_dict()["approvals_needed"] == 1
    assert q.approve(item.id, by="owner").status == "approved"
    assert q.execute(item.id, c).status == "executed"


def test_large_non_refund_actions_need_one_approver(env):
    q, _, props, _, _ = env
    item = q.enqueue(props["send_reminder"])
    assert item.proposal.amount.amount > Decimal("500")
    assert q.approve(item.id, by="owner").status == "approved"


def test_item_waiting_for_the_second_approver_can_be_rejected(env):
    q, c, props, _, _ = env
    item = q.enqueue(props["refund_capture"])
    q.approve(item.id, by="owner")
    assert q.reject(item.id, by="bookkeeper", note="customer asked us to keep it as credit").status == "rejected"
    with pytest.raises(NotExecutable):
        q.execute(item.id, c)


def test_waiting_for_second_approval_still_counts_as_a_live_intent(env):
    q, _, props, _, _ = env
    item = q.enqueue(props["refund_capture"])
    q.approve(item.id, by="owner")
    again = q.enqueue(props["refund_capture"])
    assert again.status == "blocked" and "duplicate_intent" in [v["code"] for v in again.policy["violations"]]


def test_waiting_items_expire(env):
    q, _, props, clock, _ = env
    item = q.enqueue(props["refund_capture"])
    q.approve(item.id, by="owner")
    clock.t = T0 + timedelta(hours=25)
    assert q.expire_stale() == [item.id]
    assert q.get(item.id).status == "expired"


def test_executor_refuses_a_large_refund_approved_by_one_person(env):
    # someone flips the status in the database, skipping the second approval
    q, c, props, _, _ = env
    item = q.enqueue(props["refund_capture"])
    q._update(item.id, status="approved", decided_by="owner")
    with pytest.raises(NotExecutable, match="two"):
        q.execute(item.id, c)
    assert c.calls == []


def test_threshold_is_configurable_and_can_be_switched_off(env, tmp_path):
    _, c, props, _, _ = env
    q = ApprovalQueue(tmp_path / "q2.db", clock=lambda: T0,
                      policy=Policy(PolicyConfig(second_approval_over=None)))
    item = q.enqueue(props["refund_capture"])
    assert q.approve(item.id, by="owner").status == "approved"
    q3 = ApprovalQueue(tmp_path / "q3.db", clock=lambda: T0,
                       policy=Policy(PolicyConfig(second_approval_over=Decimal("10.00"))))
    small = q3.enqueue(props["refund_overpayment"], invoice_due=USD("45.00"))
    assert q3.approve(small.id, by="owner").status == "awaiting_second"


def test_existing_database_gets_the_new_columns(tmp_path):
    import sqlite3
    from cashdesk.queue import SCHEMA
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.replace("    first_by text,\n    first_at text,\n", ""))
    con.close()
    ApprovalQueue(db, clock=lambda: T0)
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(queue_items)")}
    assert {"first_by", "first_at"} <= cols


def test_api_reports_the_second_approval_step(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from cashdesk.app import create_app
    from cashdesk.service import Desk
    desk = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    api = sign_in(TestClient(create_app(desk)), desk)
    api.post("/api/proposals/refresh")
    item = next(i for i in api.get("/api/approvals").json()["items"] if i["proposal"]["type"] == "refund_capture")
    assert item["approvals_needed"] == 2
    r = api.post(f"/api/approvals/{item['id']}/approve", json={"by": "owner", "proposal_hash": item["proposal_hash"]})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_second" and r.json()["first_by"] == "owner"
    assert api.post(f"/api/approvals/{item['id']}/execute").status_code == 409
    assert api.post(f"/api/approvals/{item['id']}/approve", json={"by": "owner", "proposal_hash": item["proposal_hash"]}).status_code == 409
    r = api.post(f"/api/approvals/{item['id']}/approve", json={"by": "bookkeeper", "proposal_hash": item["proposal_hash"]})
    assert r.json()["status"] == "approved"
    assert api.post(f"/api/approvals/{item['id']}/execute").json()["status"] == "executed"
