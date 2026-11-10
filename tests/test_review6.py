"""Final review (all areas, commit 8d4bf7d): one or more tests per finding. Each asserts the safe
behaviour. The reviewer's own suites (core/ai/board) were also run against the fix."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler, OpenAICompatibleReconciler, reconcile
from cashdesk.actions import propose
from cashdesk.audit import AuditLog, verify
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.paypal.sandbox import PayPalError
from cashdesk.queue import NotExecutable, PolicyBlocked
from tests.review_a_common import AS_OF as A_AS_OF, D, balance, invoice, money, run, txn
from tests.test_review4 import desk, item_of  # noqa: F401  (fixture)
from tests.accounts_helper import sign_in

AS_OF = datetime(2026, 11, 7).date()
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
LUMEN = "A878C16AB129834C9"   # 1500.00, the Lumen memo payment (heuristic -> NW-1021)
RAVI = "934B6BF994C1A1552"    # 275.00, unbilled
DUP_ROW = None


def fixture_data():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return c.list_invoices(), list(txns)


def model(content: dict):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})
    return OpenAICompatibleReconciler("m", http=httpx.Client(transport=httpx.MockTransport(handler)))


def link(tid, num, conf=0.99, reason="transaction_info.transaction_amount supports it."):
    return {"transaction_id": tid, "invoice_number": num, "confidence": conf, "reason": reason,
            "cited_fields": ["transaction_info.transaction_amount"]}


def board(d):
    from cashdesk.app import create_app
    return sign_in(TestClient(create_app(d), raise_server_exceptions=False), d)


def shown(api, ptype):
    return next(i for i in api.get("/api/approvals").json()["items"] if i["proposal"]["type"] == ptype)


# ---------- H1: an AI link can't create a refund ----------

def test_h1_ai_link_bigger_than_the_invoice_is_dropped_and_the_heuristic_stands():
    invs, txns = fixture_data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=model({"resolutions": [link(LUMEN, "NW-1017")], "reasons": []}))
    props = propose(r, invs, txns, as_of=AS_OF)
    assert not [p for p in props if p.target == LUMEN and p.type.startswith("refund")]
    t = r.txn(LUMEN)
    assert t.invoice_number == "NW-1021" and t.rule == "memo_heuristic"   # the 1500.00 can't land on 350.00
    assert any(p.type == "send_reminder" and p.invoice_number == "NW-1017" for p in props)


def test_h1_no_refund_is_ever_proposed_from_a_suggested_row():
    from cashdesk.matcher import match, Link
    invs, txns = fixture_data()
    r = match(invs, txns, as_of=AS_OF)
    for x in r.transactions:  # force every row to look AI-linked and refundable
        if x.outcome in ("duplicate", "overpaid"):
            x.rule = "ai"
    assert not [p for p in propose(r, invs, txns, as_of=AS_OF) if p.type.startswith("refund")]


def test_h1_every_suggested_link_needs_review():
    invs, txns = fixture_data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=model({"resolutions": [link(LUMEN, "NW-1021", 1.0)], "reasons": []}))
    assert r.txn(LUMEN).rule == "ai" and r.txn(LUMEN).needs_review


# ---------- H2: a payment crossing the 31-day edge ----------

@pytest.mark.parametrize("code", ["T0006", "T0007"])
def test_h2_stand_in_rows_never_replace_or_double_a_real_payment(tmp_path, monkeypatch, code):
    from cashdesk.paypal.sandbox import SandboxPayPalClient
    from cashdesk.service import Desk
    paid_at = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    inv = {"id": "INV2-AAAA-BBBB-CCCC-DDDD", "status": "PAID",
           "detail": {"invoice_number": "INV-1", "currency_code": "USD", "invoice_date": "2026-10-01",
                      "payment_term": {"due_date": "2026-10-20"}},
           "amount": {"currency_code": "USD", "value": "100.00"}, "due_amount": {"currency_code": "USD", "value": "0.00"},
           "primary_recipients": [{"billing_info": {"email_address": "pat@example.com"}}],
           "payments": {"paid_amount": {"currency_code": "USD", "value": "100.00"},
                        "transactions": [{"payment_id": "CAP1AAAAAAAAAAAAA", "payment_date": "2026-10-09", "type": "PAYPAL",
                                          "method": "PAYPAL", "amount": {"currency_code": "USD", "value": "100.00"}}]}}
    row = {"transaction_info": {"transaction_id": "CAP1AAAAAAAAAAAAA", "transaction_event_code": code,
                                "transaction_status": "S", "transaction_initiation_date": "2026-10-09T10:00:00+0000",
                                "transaction_amount": {"currency_code": "USD", "value": "100.00"},
                                "fee_amount": {"currency_code": "USD", "value": "-3.98"}, "invoice_id": "INV-1"},
           "payer_info": {"email_address": "pat@example.com"}, "cart_info": {"paypal_invoice_id": inv["id"]}}

    def handler(req):
        p = req.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"scope": "https://uri.paypal.com/services/reporting/search/read",
                                             "access_token": "T", "expires_in": 32400})
        if p == "/v2/invoicing/invoices":
            return httpx.Response(200, json={"items": [inv], "total_pages": 1})
        f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        rows = [row] if f(req.url.params["start_date"]) <= paid_at <= f(req.url.params["end_date"]) else []
        return httpx.Response(200, json={"transaction_details": rows, "total_pages": 1})
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    now = {"t": datetime(2026, 10, 20, 12, tzinfo=timezone.utc)}
    c = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    d = Desk(client=c, reconciler=FallbackReconciler(), clock=lambda: now["t"])
    d.sync()
    now["t"] = datetime(2026, 11, 9, 15, tzinfo=timezone.utc)  # the payment is now just past the window
    d.sync()
    rows = [t for t in d.store.transactions() if t.id == "CAP1AAAAAAAAAAAAA"]
    assert len(rows) == 1 and rows[0].fee is not None and rows[0].event_code == code
    d.refresh_proposals()
    assert [i.proposal.type for i in d.queue.list()] == []


# ---------- H3: a lone surrogate in model text ----------

def test_h3_lone_surrogate_from_the_model_never_reaches_storage_or_the_board(tmp_path, monkeypatch):
    from cashdesk.service import Desk
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    invs, txns = fixture_data()
    dup = next(t for t in txns if t.id == "E3C2F6D3F2B3E3A65") if False else None
    r0 = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    row = next(x for x in r0.transactions if x.outcome == "duplicate")
    out = {"resolutions": [link(LUMEN, "NW-1021", reason="Lumen \ud83d transaction_info.transaction_amount")],
           "reasons": [{"id": f"{row.txn_id}:{row.event_code}", "reason": "Dup \ud83d transaction_info.transaction_id",
                        "cited_fields": ["transaction_info.transaction_id"]}]}
    api = board(Desk(client=FixturePayPalClient(), reconciler=model(out)))
    assert api.get("/api/reconciliation").status_code == 200
    assert api.post("/api/proposals/refresh").status_code == 200
    assert api.get("/api/approvals").status_code == 200


# ---------- M1: a retry after PayPal provably did nothing is a first run again ----------

@pytest.mark.parametrize("kind", ["422", "connect"])
def test_m1_retry_rechecks_the_limit_when_paypal_never_acted(desk, kind):
    from tests.test_review3 import Moving, txn as rtxn

    class Refuses(Moving):
        fail = True

        def execute_call(self, method, path, body, *, request_id):
            if path.endswith("/refund") and self.fail:
                self.fail = False
                if kind == "422":
                    raise PayPalError(422, "UNPROCESSABLE_ENTITY", "no", "dbg", path)
                raise httpx.ConnectError("refused")
            return super().execute_call(method, path, body, request_id=request_id)
    c = Refuses()
    d = desk(c)
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, "owner")
    with pytest.raises((PayPalError, httpx.ConnectError)):
        d.execute(item.id)
    assert d.queue.get(item.id).started_at is None
    cap = next(t for t in d.store.transactions() if t.id == item.proposal.target)
    c.extra = [rtxn("BYHAND000000001", "-45.00", cap.initiated + timedelta(hours=1), code="T1107", ref=cap.id,
                    invoice_ref=None)]
    d.sync()  # refunded by hand meanwhile
    with pytest.raises(PolicyBlocked):
        d.execute(item.id)
    assert [x for x in c.calls if x["op"] == "refund"] == []


def test_m5_a_failed_run_paypal_never_acted_on_can_be_rejected(desk):
    class Refuses(FixturePayPalClient):
        def execute_call(self, method, path, body, *, request_id):
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "the invoice is already paid", "dbg", path)
    d = desk(Refuses())
    item = item_of(d, "send_reminder")
    d.approve(item.id, "owner")
    with pytest.raises(PayPalError):
        d.execute(item.id)
    assert d.reject(item.id, "owner", "already paid")["status"] == "rejected"


# ---------- M2: the audit witness reads the item's latest state ----------

def test_m2_a_rejected_approval_cannot_be_revived_in_sqlite(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, "owner")
    d.reject(item.id, "owner", "changed my mind")
    with sqlite3.connect(d.queue.path) as con:
        con.execute("update queue_items set status = 'approved' where id = ?", (item.id,))
    with pytest.raises(NotExecutable):
        d.execute(item.id)
    assert d.client.calls == []


def test_m2_an_executed_item_reset_in_sqlite_does_not_run_again(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, "owner")
    d.execute(item.id)
    with sqlite3.connect(d.queue.path) as con:
        con.execute("update queue_items set status = 'approved', started_at = null, request_id = 'cashdesk-x-1' "
                    "where id = ?", (item.id,))
    with pytest.raises(NotExecutable):
        d.execute(item.id)
    assert len([x for x in d.client.calls if x["op"] == "refund"]) == 1


# ---------- M3 / M4: money ----------

def test_m3_a_pending_refund_holds_back_a_second_refund():
    ts = [txn("CAPTURE", 54500), txn("REFUND", -4500, code="T1107", status="P", hour=2, ref="CAPTURE", invoice_ref=None)]
    r, p, j = run([invoice(50000)], ts)
    assert not [x for x in p if x.type.startswith("refund")]
    assert balance(j, "PayPal balance") == D("545.00")  # pending: not booked


def test_m4_totals_only_record_of_a_refunded_overpayment():
    inv = invoice(10000, status="PAID", paid=15000, refunds={"refund_amount": money(5000)}, due=0)
    ts = [txn("CAPTURE", 15000), txn("REFUND", -5000, code="T1107", hour=2, ref="CAPTURE", invoice_ref=None)]
    r, p, j = run([inv], ts)
    assert r.invoices[0].received.amount == D("100.00") and not [x for x in p if x.type.startswith("refund")]


# ---------- M5: ways out ----------

def test_m5_the_hourly_cap_refuses_for_now_without_blocking(desk):
    from cashdesk.policy import Policy, PolicyConfig
    from cashdesk.service import Desk
    d = desk()
    d.queue.policy = Policy(PolicyConfig(max_per_hour=1))
    d.refresh_proposals()
    first, second = [i for i in d.queue.list() if i.proposal.type == "send_reminder"][:2]
    assert {first.status, second.status} == {"pending"}
    d.approve(first.id, "owner")
    with pytest.raises(PolicyBlocked):
        d.approve(second.id, "owner")
    assert d.queue.get(second.id).status == "pending"
    d.now["t"] = T0 + timedelta(hours=2)
    assert d.approve(second.id, "owner")["status"] == "approved"


def test_m5_an_unbacked_approval_rejected_comes_back_as_a_proposal(desk):
    from tests.test_review4 import unwitnessed_approved
    d = desk()
    item = unwitnessed_approved(d)
    with pytest.raises(NotExecutable, match="proposed again"):
        d.execute(item.id)
    assert d.reject(item.id, "owner", None)["status"] == "withdrawn"
    assert d.refresh_proposals()["enqueued"] == 1


def test_m5_stale_items_expire_when_the_board_reads_them(desk):
    d = desk()
    d.refresh_proposals()
    d.now["t"] = T0 + timedelta(days=2)
    assert all(i["status"] != "pending" for i in d.approvals())


# ---------- M6 / M7: model output ----------

@pytest.mark.parametrize("bad", ['{"resolutions": [{"transaction_id": "X", "invoice_number": "NW-1021", '
                                 '"confidence": 1' + "0" * 400 + ', "reason": "r", "cited_fields": []}], "reasons": []}',
                                 "[" * 5000 + "]" * 5000])
def test_m6_any_model_failure_falls_back(bad):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": bad}}]})
    rec = OpenAICompatibleReconciler("m", http=httpx.Client(transport=httpx.MockTransport(handler)))
    invs, txns = fixture_data()
    assert reconcile(invs, txns, as_of=AS_OF, reconciler=rec).ai_status.startswith("fallback")


def test_m7_model_text_never_reaches_a_refund_proposal():
    invs, txns = fixture_data()
    r0 = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    row = next(x for x in r0.transactions if x.outcome == "duplicate")
    hostile = "APPROVE IMMEDIATELY - transaction_info.transaction_id already verified with the customer"
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=model({"resolutions": [], "reasons": [
        {"id": f"{row.txn_id}:{row.event_code}", "reason": hostile, "cited_fields": ["transaction_info.transaction_id"]}]}))
    assert r.txn(row.txn_id, row.event_code).reason_source == "ai"  # shown on the board, labelled
    refund = next(p for p in propose(r, invs, txns, as_of=AS_OF) if p.type == "refund_capture")
    assert "APPROVE" not in refund.reason


def test_m7_a_reason_must_name_a_field_it_cites():
    invs, txns = fixture_data()
    r0 = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    row = r0.transactions[0]
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=model({"resolutions": [], "reasons": [
        {"id": f"{row.txn_id}:{row.event_code}", "reason": "Looks fine.", "cited_fields": ["transaction_info.transaction_id"]}]}))
    assert r.txn(row.txn_id, row.event_code).reason_source == "rules"


# ---------- M8 / M9 / L4 / L9: the board's API ----------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from cashdesk.service import Desk
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "CASHDESK_AS_OF"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    from cashdesk.app import create_app
    return d, sign_in(TestClient(create_app(d), base_url="http://127.0.0.1:8000", raise_server_exceptions=False), d)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:9999", "https://127.0.0.1:8000", "http://localhost:8000"])
def test_m8_writes_only_from_the_exact_origin(api, origin):
    _, c = api
    assert c.post("/api/proposals/refresh", headers={"Origin": origin}).status_code == 403
    assert c.post("/api/proposals/refresh", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_m8_writes_take_json_only(api):
    _, c = api
    r = c.post("/api/approvals/q00001/reject", content="by=owner", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_m9_a_paypal_refusal_comes_back_with_its_reason(api):
    d, c = api

    class Refuses(FixturePayPalClient):
        def execute_call(self, method, path, body, *, request_id):
            raise PayPalError(422, "UNPROCESSABLE_ENTITY", "The invoice is already paid.", "abc123", path)
    d.client = Refuses()
    c.post("/api/proposals/refresh")
    it = shown(c, "send_reminder")
    c.post(f"/api/approvals/{it['id']}/approve", json={"by": "owner", "proposal_hash": it["proposal_hash"]})
    r = c.post(f"/api/approvals/{it['id']}/execute")
    assert r.status_code == 502 and "already paid" in r.json()["detail"]
    assert "already paid" in shown(c, "send_reminder")["error"]


@pytest.mark.parametrize("name", [" ", "​", "\t", "ㅤ"])
def test_l4_an_invisible_approver_name_is_refused(api, name):
    _, c = api
    c.post("/api/proposals/refresh")
    it = shown(c, "refund_capture")
    assert c.post(f"/api/approvals/{it['id']}/approve", json={"by": name, "proposal_hash": it["proposal_hash"]}).status_code == 422


def test_l9_approve_is_bound_to_the_proposal_shown(api):
    _, c = api
    c.post("/api/proposals/refresh")
    it = shown(c, "send_reminder")
    assert c.post(f"/api/approvals/{it['id']}/approve", json={"by": "owner"}).status_code == 422
    assert c.post(f"/api/approvals/{it['id']}/approve", json={"by": "owner", "proposal_hash": "0" * 64}).status_code == 409
    assert c.post(f"/api/approvals/{it['id']}/approve",
                  json={"by": "owner", "proposal_hash": it["proposal_hash"]}).json()["status"] == "approved"


def test_l9_board_headers_docs_and_sri(api):
    _, c = api
    h = c.get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
    assert c.get("/docs").status_code == 404
    html = open("static/index.html").read()
    assert 'integrity="sha384-' in html and "did not load" in html


def test_l9_testserver_is_only_allowed_under_tests(monkeypatch):
    from cashdesk.app import allowed_hosts
    monkeypatch.delenv("CASHDESK_TESTING", raising=False)
    assert "testserver" not in allowed_hosts()


# ---------- L1: the webhook limiter ----------

def test_l1_cf_connecting_ip_is_not_trusted_unless_configured(tmp_path, monkeypatch):
    from tests.test_webhooks import HEADERS, Verifier, event
    from cashdesk.service import Desk
    from cashdesk.webhook_app import create_webhook_app
    from cashdesk.webhooks import REJECT_LIMIT
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")
    monkeypatch.delenv("CASHDESK_TRUST_CF_CONNECTING_IP", raising=False)
    v = Verifier("FAILURE")
    c = TestClient(create_webhook_app(Desk(client=v, reconciler=FallbackReconciler(), clock=lambda: T0)))
    for i in range(100):
        c.post("/webhooks/paypal", content=event(eid=f"F{i}"),
               headers={**HEADERS, "Content-Type": "application/json", "CF-Connecting-IP": f"10.0.{i}.1"})
    assert len(v.verify_calls) == REJECT_LIMIT


# ---------- L2: the audit log ----------

def test_l2_a_record_added_by_hand_fails_verification(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    from cashdesk.audit import GENESIS, _hash
    last = log.records()[-1]
    rec = {"event": "approved", "item_id": "q00001", "seq": 4, "prev_hash": last["hash"], "at": "2026-11-09"}
    rec["hash"] = _hash(rec)
    with open(log.path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    assert not verify(log.path).ok


def test_l2_a_refused_append_changes_nothing(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:-1])  # drop the newline of record 3 with no record announced: not a crash
    (tmp_path / "audit.jsonl.head").write_text(json.dumps({"seq": 2, "hash": log.records()[1]["hash"]}))
    with pytest.raises(ValueError):
        log.append({"event": "y"})
    assert log.path.read_bytes() == raw[:-1]


def test_l2_append_reads_only_what_was_added(tmp_path, monkeypatch):
    import cashdesk.audit as audit
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(50):
        log.append({"event": "x", "i": i})
    seen = []
    real = audit._verify_state
    monkeypatch.setattr(audit, "_verify_state", lambda text, head, start=None: (seen.append(len(text)), real(text, head, start))[1])
    log.append({"event": "y"})
    assert seen and seen[-1] == 0  # nothing new since its own last append


# ---------- L3: a record-payment carries what the payment still holds ----------

def test_l3_record_payment_for_a_part_refunded_payment_is_the_net():
    invs = [invoice(10000, email="pat@example.com")]
    ts = [txn("PAY", 15000, invoice_ref=None, email="someone@else.example"),
          txn("REF", -5000, code="T1107", hour=1, ref="PAY", invoice_ref=None)]
    from cashdesk.matcher import Link
    from cashdesk.matcher import match
    r = match(invs, ts, as_of=A_AS_OF, links={"PAY": Link("INV-1", "ai", "ai", 0.9, {"ai.reason": "x"})})
    (p,) = [x for x in propose(r, invs, ts, as_of=A_AS_OF) if x.type == "record_payment"]
    assert p.amount == Money(D("100.00"), "USD")


# ---------- L5: two "Propose actions" at once ----------

def test_l5_simultaneous_refreshes_add_each_intent_once(desk):
    d = desk()
    errs = []

    def go():
        try:
            d.refresh_proposals()
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    keys = [i.proposal.intent_key for i in d.queue.list()]
    assert errs == [] and len(keys) == len(set(keys)) and all(i.status == "pending" for i in d.queue.list()
                                                              if i.proposal.type != "refund_capture")


# ---------- L7 / L8: rows ----------

def test_l7_a_reversed_payment_without_its_reversal_row_is_not_cash():
    r, p, j = run([invoice(10000)], [txn("CAPTURE", 10000, status="V", fee=-398)])
    assert balance(j, "PayPal balance") == 0


def test_l7_a_chargeback_takes_the_money_back():
    ts = [txn("CAPTURE", 10000), txn("CHARGEBACK", -10000, code="T1201", hour=5, ref="CAPTURE", invoice_ref=None)]
    r, p, j = run([invoice(10000)], ts)
    assert balance(j, "PayPal balance") == 0 and r.invoices[0].received.amount == 0


def test_l8_one_capture_under_two_event_codes_is_one_payment():
    ts = [txn("CAPTURE", 10000, code="T0006"), txn("CAPTURE", 10000, code="T0007", hour=3)]
    r, p, j = run([invoice(10000)], ts)
    assert not [x for x in p if x.type.startswith("refund")] and balance(j, "PayPal balance") == D("100.00")


def test_l8_a_link_applies_to_one_row():
    from cashdesk.matcher import Link, match
    ts = [txn("SAMEID", 5000, invoice_ref=None, email="x@y.example"),
          txn("SAMEID", 5000, code="T0011", invoice_ref=None, email="x@y.example")]
    r = match([invoice(10000)], ts, as_of=A_AS_OF, links={"SAMEID": Link("INV-1", "ai", "ai", 0.9, {})})
    assert sum(1 for x in r.transactions if x.rule == "ai") == 1


# ---------- L10: a stuck item from an older version ----------

def test_l10_an_old_executing_item_without_a_fingerprint_can_be_closed(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    with sqlite3.connect(d.queue.path) as con:
        con.execute("update queue_items set status = 'executing', approved_hash = null, decided_by = 'owner' "
                    "where id = ?", (item.id,))
    # closed, but not proposed again: a run that started may have reached PayPal
    assert d.reject(item.id, "owner", "approved by an older version")["status"] == "rejected"
    assert d.refresh_proposals()["enqueued"] == 0
