"""Second, independent adversarial review (fresh operator, commit 76dde44): one test per finding."""

from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from cashdesk.actions import propose
from cashdesk.ai import ClaudeReconciler, FallbackReconciler, reconcile
from cashdesk.journal import journal
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.paypal.sandbox import SandboxPayPalClient
from cashdesk.policy import Policy, PolicyConfig
from cashdesk.queue import ApprovalQueue, NotExecutable, PolicyBlocked
from tests.test_matcher_followups import refund_of
from tools.make_fixtures import txn_id
from tests.accounts_helper import sign_in

AS_OF = date(2026, 11, 7)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
USD = lambda v: Money(Decimal(v), "USD")
FIRST, DUP = txn_id("NW-1015"), txn_id("NW-1015-dup")


def data():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return c, c.list_invoices(), list(txns)


def run(invs, txns):
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return r, propose(r, invs, txns, as_of=AS_OF)


def props_by_type(invs, txns):
    r, props = run(invs, txns)
    return {p.type: p for p in props}


# ---------- H1: a refunded FIRST payment must not turn the second into a refundable duplicate ----------

def test_h1_refunded_first_payment_leaves_the_second_as_the_real_payment():
    _, invs, txns = data()
    txns.append(refund_of(txns, FIRST, "800.00", "REFUNDFIRST00001"))
    r, props = run(invs, txns)
    assert r.txn(FIRST).outcome == "refunded"
    assert r.txn(DUP).outcome == "matched"
    inv = r.invoice_by_number("NW-1015")
    assert inv.outcome == "paid" and inv.received == USD("800.00")
    assert not [p for p in props if p.type == "refund_capture"]


# ---------- H2: what runs is exactly what the approve dialog showed ----------

class Capture:
    def __init__(self):
        self.reqs = []

    def __call__(self, req: httpx.Request):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AAtok", "expires_in": 3600, "scope": ""})
        self.reqs.append(req)
        p = req.url.path
        if p == "/v2/invoicing/invoices":
            return httpx.Response(201, json={"id": "INV2-AAAA-BBBB-CCCC-DDDD", "status": "DRAFT",
                                             "detail": {"invoice_number": "0099", "currency_code": "USD"},
                                             "amount": {"currency_code": "USD", "value": "275.00"}})
        if p.endswith("/send"):
            return httpx.Response(202, json={"href": "https://www.sandbox.paypal.com/invoice/p/#INV2"})
        if p.endswith("/refund"):
            return httpx.Response(201, json={"id": "REF123", "status": "COMPLETED"})
        return httpx.Response(204)


def executed_bodies(ptype, tmp_path):
    _, invs, txns = data()
    p = props_by_type(invs, txns)[ptype]
    cap = Capture()
    client = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(cap))
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0, policy=Policy(PolicyConfig(second_approval_over=None)))
    limit = {"refund_overpayment": USD("45.00")}.get(ptype)
    item = q.enqueue(p, invoice_due=limit)
    q.approve(item.id, by="owner")
    q.execute(item.id, client)
    return p, [(r.method, r.url.path, json.loads(r.content) if r.content else None, r.headers.get("PayPal-Request-Id"))
               for r in cap.reqs], q.get(item.id)


def test_h2_draft_invoice_sends_exactly_the_shown_bodies(tmp_path):
    p, sent, item = executed_bodies("create_draft_invoice", tmp_path)
    assert [s[2] for s in sent] == [c["body"] for c in p.api_calls]
    assert sent[1][1] == "/v2/invoicing/invoices/INV2-AAAA-BBBB-CCCC-DDDD/send"
    assert sent[1][2]["send_to_recipient"] is False  # the customer already paid: don't email them an invoice


def test_h2_refund_sends_exactly_the_shown_body(tmp_path):
    p, sent, item = executed_bodies("refund_capture", tmp_path)
    (only,) = sent
    assert only[2] == p.api_calls[0]["body"] and "invoice_id" in only[2]
    assert only[3] == item.request_id


def test_h2_no_made_up_invoicer_on_draft_invoices():
    _, invs, txns = data()
    body = props_by_type(invs, txns)["create_draft_invoice"].api_calls[0]["body"]
    assert "invoicer" not in body  # PayPal fills in the real account's business details


# ---------- H3: request ids are unique across databases and shown before running ----------

def test_h3_request_ids_differ_across_queue_databases(tmp_path):
    _, invs, txns = data()
    p = props_by_type(invs, txns)["send_reminder"]
    ids = []
    for n in (1, 2):  # e.g. after `make clean`, or a second desk on the same PayPal app
        q = ApprovalQueue(tmp_path / f"q{n}.db", clock=lambda: T0)
        item = q.enqueue(copy.deepcopy(p))
        assert item.request_id and item.request_id.startswith(f"cashdesk-{item.id}-")
        q.approve(item.id, by="owner")
        ids.append(q.execute(item.id, FixturePayPalClient()).request_id)
    assert ids[0] != ids[1]


def test_h3_the_id_shown_at_enqueue_is_the_one_used(tmp_path):
    _, invs, txns = data()
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(props_by_type(invs, txns)["send_reminder"])
    shown = item.to_dict()["request_id"]
    q.approve(item.id, by="owner")
    c = FixturePayPalClient()
    assert q.execute(item.id, c).request_id == shown == c.calls[0]["headers"]["PayPal-Request-Id"]


# ---------- M1: the board refuses foreign Host and Origin (DNS rebinding) ----------

@pytest.fixture
def board(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from cashdesk.app import create_app
    from cashdesk.service import Desk
    desk = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    return desk, create_app(desk)


def test_m1_foreign_host_is_refused(board):
    from fastapi.testclient import TestClient
    desk, app = board
    api = TestClient(app, base_url="http://rebind.attacker.example:8000")
    assert api.post("/api/proposals/refresh").status_code == 400
    assert desk.queue.list() == []
    local = TestClient(app, base_url="http://localhost:8000")
    assert local.get("/api/health").status_code == 200


def test_m1_foreign_origin_on_writes_is_refused(board):
    from fastapi.testclient import TestClient
    desk, app = board
    api = sign_in(TestClient(app, base_url="http://127.0.0.1:8000"), desk)
    assert api.post("/api/proposals/refresh", headers={"Origin": "https://evil.example"}).status_code == 403
    assert desk.queue.list() == []
    assert api.post("/api/proposals/refresh", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


# ---------- M2 / M3: denied refunds aren't booked; reversals count like refunds ----------

def test_m2_denied_refund_is_not_booked():
    _, invs, txns = data()
    t = refund_of(txns, txn_id("NW-1001"), "45.00", "REFUNDDENIED0001")
    txns.append(dataclasses.replace(t, status="D"))
    r, _ = run(invs, txns)
    assert r.txn("REFUNDDENIED0001").outcome != "refund"
    assert not [l for l in journal(r, txns) if l["paypal_transaction_id"] == "REFUNDDENIED0001"]


def test_m3_a_reversal_takes_the_money_back():
    _, invs, txns = data()
    rev = dataclasses.replace(refund_of(txns, txn_id("NW-1001"), "1250.00", "REVERSAL00000001"), event_code="T1106")
    txns.append(rev)
    r, _ = run(invs, txns)
    assert r.txn(txn_id("NW-1001")).outcome == "refunded"
    inv = r.invoice_by_number("NW-1001")
    assert inv.outcome == "refunded" and inv.received == USD("0.00")


# ---------- M4: an approved proposal edited in the database never runs ----------

def test_m4_retargeted_after_approval_is_blocked(tmp_path):
    _, invs, txns = data()
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(props_by_type(invs, txns)["refund_capture"])
    q.approve(item.id, by="owner")
    q.approve(item.id, by="bookkeeper")
    other = txn_id("NW-1001")

    def retarget(d):
        d["target"] = other
        d["api_calls"][0]["path"] = f"/v2/payments/captures/{other}/refund"
    q._tamper_for_test(item.id, retarget)
    c = FixturePayPalClient()
    with pytest.raises(PolicyBlocked):
        q.execute(item.id, c)
    assert c.calls == []


def test_m4_edit_between_first_and_second_approval_is_refused(tmp_path):
    _, invs, txns = data()
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(props_by_type(invs, txns)["refund_capture"])
    q.approve(item.id, by="owner")
    q._tamper_for_test(item.id, lambda d: d["api_calls"][0]["body"].update(note_to_payer="changed"))
    with pytest.raises(PolicyBlocked):
        q.approve(item.id, by="bookkeeper")


# ---------- M5: an earlier same-payer payment is the original, not an unbilled one ----------

def test_m5_earlier_same_payer_payment_makes_the_later_one_the_duplicate():
    _, invs, txns = data()
    a = next(t for t in txns if t.id == txn_id("NW-1001"))
    b = dataclasses.replace(a, id="EARLIERPAYMENT01", invoice_ref=None, paypal_invoice_id=None, subject=None,
                            note=None, initiated=a.initiated - timedelta(minutes=30),
                            raw={**a.raw, "transaction_info": {**a.raw["transaction_info"], "transaction_id": "EARLIERPAYMENT01"}})
    txns.append(b)
    r, props = run(invs, txns)
    assert r.txn("EARLIERPAYMENT01").outcome == "matched" and r.txn("EARLIERPAYMENT01").invoice_number == "NW-1001"
    assert r.txn(a.id).outcome == "duplicate"
    assert not [p for p in props if p.type == "create_draft_invoice" and p.target == "EARLIERPAYMENT01"]


# ---------- M6: two simultaneous executes send one call ----------

def test_m6_concurrent_executes_send_once(tmp_path):
    _, invs, txns = data()
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(props_by_type(invs, txns)["send_reminder"])
    q.approve(item.id, by="owner")

    class Slow(FixturePayPalClient):
        def execute_call(self, *a, **kw):
            time.sleep(0.2)
            return super().execute_call(*a, **kw)
    c = Slow()
    errors = []

    def go():
        try:
            q.execute(item.id, c)
        except NotExecutable as e:
            errors.append(e)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(c.calls) == 1 and len(errors) == 1


# ---------- L1-L6 ----------

def test_l1_nan_confidence_is_not_trusted():
    from tests.test_ai_providers import chat_reply, mocked, rec
    from tests.test_ai import LUMEN, good_payload
    _, invs, txns = data()
    p = good_payload([])
    p["resolutions"][0]["confidence"] = float("nan")
    client, _ = mocked(chat_reply(json.dumps(p)))
    t = reconcile(invs, txns, as_of=AS_OF, reconciler=rec(client)).txn(LUMEN)
    assert t.rule != "ai"


def test_l2_claude_reply_that_is_not_an_object_falls_back():
    from tests.test_ai import fake_client
    _, invs, txns = data()
    client, _ = fake_client(payload="[]")
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    assert r.ai_status.startswith("fallback")


@pytest.mark.parametrize("second", ["owner​", "ｏｗｎｅｒ", "o w n e r".replace(" ", "")])
def test_l3_lookalike_names_are_the_same_person(tmp_path, second):
    _, invs, txns = data()
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(props_by_type(invs, txns)["refund_capture"])
    q.approve(item.id, by="owner")
    with pytest.raises(NotExecutable):
        q.approve(item.id, by=second)


def test_l4_rows_sharing_an_id_keep_their_own_amounts(board):
    desk, _ = board
    _, invs, txns = data()
    dup = next(t for t in txns if t.id == DUP)
    conv = dataclasses.replace(dup, event_code="T0200", amount=Money(Decimal("-92.00"), "EUR"), fee=None)
    r, props = run(invs, txns + [conv])
    (refund,) = [p for p in props if p.type == "refund_capture"]
    assert refund.amount == USD("800.00")


def test_l5_malformed_event_body_is_a_400_not_a_500(tmp_path, monkeypatch):
    from tests.test_webhooks import HEADERS, Verifier
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")
    from fastapi.testclient import TestClient
    from cashdesk.service import Desk
    from cashdesk.webhook_app import create_webhook_app
    api = TestClient(create_webhook_app(Desk(client=Verifier(), reconciler=FallbackReconciler(), clock=lambda: T0)))
    body = json.dumps({"id": "WH-X", "event_type": "INVOICING.INVOICE.PAID", "resource": "str"})
    assert api.post("/webhooks/paypal", content=body, headers={**HEADERS, "Content-Type": "application/json"}).status_code < 500
    bad_len = api.post("/webhooks/paypal", content=b"{}", headers={**HEADERS, "Content-Length": "abc"})
    assert bad_len.status_code < 500


def test_l6_a_flood_from_one_source_does_not_lock_out_paypal(tmp_path, monkeypatch):
    from tests.test_webhooks import HEADERS, Verifier, event
    from cashdesk.webhooks import REJECT_LIMIT
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")
    from fastapi.testclient import TestClient
    from cashdesk.service import Desk
    from cashdesk.webhook_app import create_webhook_app
    monkeypatch.setenv("CASHDESK_TRUST_CF_CONNECTING_IP", "1")  # a Cloudflare tunnel is in front
    v = Verifier("FAILURE")
    inner = create_webhook_app(Desk(client=v, reconciler=FallbackReconciler(), clock=lambda: T0))

    async def via_cloudflared(scope, receive, send):  # cloudflared connects from this machine
        await inner({**scope, "client": ("127.0.0.1", 40000)}, receive, send)
    api = TestClient(via_cloudflared)
    h = {**HEADERS, "Content-Type": "application/json"}
    for i in range(REJECT_LIMIT + 3):
        api.post("/webhooks/paypal", content=event(eid=f"F{i}"), headers={**h, "CF-Connecting-IP": "203.0.113.9"})
    v.status = "SUCCESS"
    r = api.post("/webhooks/paypal", content=event(eid="REAL-1"), headers={**h, "CF-Connecting-IP": "173.0.80.116"})
    assert r.status_code == 200 and r.json()["status"] == "synced"
