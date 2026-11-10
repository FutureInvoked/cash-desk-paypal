"""P2: approvals belong to signed-in users. The two-person rule compares accounts, not typed names."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.app import create_app
from cashdesk.audit import verify
from cashdesk.paypal import FixturePayPalClient
from cashdesk.queue import NotExecutable
from cashdesk.service import Desk
from tests.accounts_helper import demo_sign_in, sign_in, user

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL",
              "CASHDESK_AS_OF"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    d.refresh_proposals()
    return d


def client(desk):
    return TestClient(create_app(desk), base_url="http://127.0.0.1:8000")


def item_of(desk, ptype):
    return next(i for i in desk.queue.list() if i.proposal.type == ptype and i.status == "pending")


def approve(api, item, **extra):
    return api.post(f"/api/approvals/{item.id}/approve",
                    json={"proposal_hash": desk_hash(item), **extra})


def desk_hash(item):
    from cashdesk.queue import proposal_hash
    return proposal_hash(item.proposal)


def audit(desk, event, item_id):
    return [r for r in desk.audit.records() if r.get("event") == event and r.get("item_id") == item_id]


# ---------- the approver is whoever is signed in ----------

def test_a_typed_name_in_the_body_is_ignored(desk):
    api = sign_in(client(desk), desk, "alice", name="Alice Smith")
    item = item_of(desk, "send_reminder")
    r = approve(api, item, by="Mallory")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decided_by"] == "user:alice" and body["decided_by_name"] == "Alice Smith"
    (rec,) = audit(desk, "approved", item.id)
    assert rec["by"] == "user:alice" and rec["by_name"] == "Alice Smith"
    assert "Mallory" not in desk.audit.path.read_text()


def test_reject_records_the_signed_in_user(desk):
    api = sign_in(client(desk), desk, "alice", name="Alice Smith")
    item = item_of(desk, "send_reminder")
    r = api.post(f"/api/approvals/{item.id}/reject", json={"by": "Mallory", "note": "not now"})
    assert r.status_code == 200 and r.json()["decided_by"] == "user:alice"
    assert audit(desk, "rejected", item.id)[0]["by"] == "user:alice"


def test_the_run_records_who_started_it(desk):
    api = sign_in(client(desk), desk, "alice")
    item = item_of(desk, "send_reminder")
    approve(api, item)
    assert api.post(f"/api/approvals/{item.id}/execute").json()["status"] == "executed"
    (rec,) = audit(desk, "executing", item.id)
    assert rec["by"] == "user:alice"


# ---------- two-person rule on accounts ----------

def test_two_different_accounts_are_needed_for_a_large_refund(desk):
    alice = sign_in(client(desk), desk, "alice", name="Alice Smith")
    item = item_of(desk, "refund_capture")
    assert item.approvals_needed == 2
    r = approve(alice, item)
    assert r.json()["status"] == "awaiting_second" and r.json()["first_by_name"] == "Alice Smith"
    assert alice.post(f"/api/approvals/{item.id}/execute").status_code == 409
    # the same account in a second browser is still the same person
    alice2 = sign_in(client(desk), desk, "alice")
    assert approve(alice2, item).status_code == 409
    # a typed name can't stand in for a second person either
    assert approve(alice2, item, by="Bob").status_code == 409
    bob = sign_in(client(desk), desk, "bob", name="Bob Jones")
    r = approve(bob, item)
    assert r.json()["status"] == "approved"
    assert r.json()["first_by_name"] == "Alice Smith" and r.json()["decided_by_name"] == "Bob Jones"
    assert bob.post(f"/api/approvals/{item.id}/execute").json()["status"] == "executed"
    first, = audit(desk, "first_approval", item.id)
    second, = audit(desk, "approved", item.id)
    assert (first["by"], second["by"]) == ("user:alice", "user:bob")
    assert verify(desk.audit.path).ok


def test_the_two_demo_approvers_complete_a_large_refund(desk):
    alex, sam = demo_sign_in(client(desk), "alex"), demo_sign_in(client(desk), "sam")
    item = item_of(desk, "refund_capture")
    assert approve(alex, item).json()["status"] == "awaiting_second"
    assert approve(sam, item).json()["status"] == "approved"
    assert sam.post(f"/api/approvals/{item.id}/execute").json()["status"] == "executed"


# ---------- the Desk only takes accounts ----------

def test_desk_refuses_a_plain_string_approver(desk):
    item = item_of(desk, "send_reminder")
    with pytest.raises(TypeError):
        desk.approve(item.id, "owner")
    with pytest.raises(TypeError):
        desk.reject(item.id, "owner", None)


def test_queue_in_account_mode_refuses_names_that_are_not_accounts(desk):
    item = item_of(desk, "send_reminder")
    for by in ("owner", "user:", "user:Alice Smith", "legacy:owner"):
        with pytest.raises(ValueError):
            desk.queue.approve(item.id, by=by)


def test_a_disabled_account_cannot_approve_through_the_desk(desk):
    alice = user(desk, "alice")
    desk.users.disable("alice")
    with pytest.raises(PermissionError):
        desk.approve(item_of(desk, "send_reminder").id, alice)


def test_a_viewer_cannot_approve_through_the_desk(desk):
    val = user(desk, "val", role="viewer")
    with pytest.raises(PermissionError):
        desk.approve(item_of(desk, "send_reminder").id, val)


# ---------- typed names left by older versions ----------

def typed(desk, fn):
    """Act as the older version did: a typed name, no accounts."""
    desk.queue.require_users = False
    try:
        return fn()
    finally:
        desk.queue.require_users = True


def test_a_typed_first_approval_never_counts_as_one_of_the_two(desk):
    item = item_of(desk, "refund_capture")
    typed(desk, lambda: desk.queue.approve(item.id, by="owner"))
    assert desk.queue.get(item.id).status == "awaiting_second"
    with pytest.raises(NotExecutable, match="typed name"):
        desk.approve(item.id, user(desk, "alice"))
    assert desk.queue.get(item.id).status == "pending"
    assert desk.approve(item.id, user(desk, "alice"))["status"] == "awaiting_second"
    assert desk.approve(item.id, user(desk, "bob"))["status"] == "approved"
    assert desk.execute(item.id, user(desk, "bob"))["status"] == "executed"


def test_a_typed_approval_that_never_ran_cannot_run_and_is_proposed_again(desk):
    item = item_of(desk, "send_reminder")
    typed(desk, lambda: desk.queue.approve(item.id, by="owner"))
    with pytest.raises(NotExecutable, match="typed name"):
        desk.execute(item.id, user(desk, "alice"))
    assert desk.reject(item.id, user(desk, "alice"), None)["status"] == "withdrawn"
    assert desk.refresh_proposals()["enqueued"] == 1  # back in the queue for a signed-in approval


class TimesOutOnce(FixturePayPalClient):
    """The reminder reaches PayPal but the reply is lost: PayPal may have acted."""
    calls = 0

    def execute_call(self, method, path, body, *, request_id):
        if path.endswith("/remind"):
            TimesOutOnce.calls += 1
            if TimesOutOnce.calls == 1:
                raise httpx.ReadTimeout("reply lost")
        return super().execute_call(method, path, body, request_id=request_id)


def test_a_typed_run_that_already_started_can_still_be_finished(desk, tmp_path):
    TimesOutOnce.calls = 0
    d = Desk(client=TimesOutOnce(), reconciler=FallbackReconciler(), clock=lambda: T0)
    item = item_of(d, "send_reminder")
    typed(d, lambda: d.queue.approve(item.id, by="owner"))
    with pytest.raises(httpx.ReadTimeout):
        typed(d, lambda: d.queue.execute(item.id, d.client))
    assert d.queue.get(item.id).status == "failed" and d.queue.get(item.id).started_at is not None
    # never strand a run PayPal may have acted on: retried with its own request id
    assert d.execute(item.id, user(d, "alice"))["status"] == "executed"


def test_the_board_shows_typed_names_as_such(desk):
    item = item_of(desk, "send_reminder")
    typed(desk, lambda: desk.queue.approve(item.id, by="owner"))
    shown = next(i for i in desk.approvals() if i["id"] == item.id)
    assert shown["decided_by_name"] == "owner (typed name, older version)"
