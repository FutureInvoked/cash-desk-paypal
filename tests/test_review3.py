"""Third independent adversarial review (fresh operator, commit 76dde44): one test per finding,
run against the code after the second review's fixes. Synthetic invoices and transactions."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, OpenAICompatibleReconciler, reconcile
from cashdesk.journal import journal
from cashdesk.matcher import match
from cashdesk.models import Invoice, Money, Transaction
from cashdesk.paypal import FixturePayPalClient
from cashdesk.queue import ApprovalQueue, NotExecutable, PolicyBlocked

AS_OF = date(2026, 11, 7)
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
USD = lambda v: Money(Decimal(v), "USD")
PAYER = "pat@example.com"


def inv(number="INV-1", total="100.00", status="SENT", paid=None, due=None, due_date=date(2026, 11, 20),
        payments=None, iid=None):
    raw = {"id": iid or f"INV2-{number}", "status": status}
    if paid is not None or payments is not None:
        raw["payments"] = {"paid_amount": USD(paid or "0").to_paypal(), "transactions": payments or []}
        if payments is None:
            del raw["payments"]["transactions"]
    p = USD(paid or "0")
    return Invoice(id=iid or f"INV2-{number}", number=number, status=status, total=USD(total),
                   due=USD(due) if due else USD(total) - p, paid=p, invoice_date=date(2026, 10, 1), due_date=due_date,
                   recipient_email=PAYER, recipient_name="Pat Lee", recipient_business=None, items=("Work",),
                   memo=None, raw=raw)


def txn(tid, amount, at, code="T0006", status="S", ref=None, invoice_ref="INV-1", email=PAYER):
    from cashdesk.paypal.parse import parse_transaction
    ti = {"transaction_id": tid, "transaction_event_code": code, "transaction_status": status,
          "transaction_initiation_date": at.isoformat(), "transaction_amount": USD(amount).to_paypal()}
    if ref:
        ti["paypal_reference_id"] = ref
    if invoice_ref:
        ti["invoice_id"] = invoice_ref
    d = {"transaction_info": ti}
    if email:
        d["payer_info"] = {"email_address": email, "payer_name": {"given_name": "Pat", "surname": "Lee"}}
    return parse_transaction(d)


def run(invs, txns):
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return r, propose(r, invs, txns, as_of=AS_OF)


# ---------- 1. refund, then a replacement payment ----------

def test_1_refund_then_replacement_payment_is_not_a_duplicate():
    a = txn("PAYA00000000001", "100.00", T0)
    back = txn("REFA00000000001", "-100.00", T0 + timedelta(hours=1), code="T1107", ref=a.id, invoice_ref=None)
    b = txn("PAYB00000000001", "100.00", T0 + timedelta(hours=2))
    r, props = run([inv()], [a, back, b])
    assert r.txn(b.id).outcome == "matched"
    i = r.invoice_by_number("INV-1")
    assert i.received == USD("100.00") and i.outcome == "paid"
    assert not [p for p in props if p.type.startswith("refund")]


# ---------- 2. approval bound to what was approved, anchored outside the queue row ----------

@pytest.fixture
def desk(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from cashdesk.service import Desk

    def make(client=None):
        return Desk(client=client or FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    return make


def overpay_item(d):
    d.refresh_proposals()
    return next(i for i in d.queue.list() if i.proposal.type == "refund_overpayment")


def test_2_rewriting_proposal_and_fingerprint_in_sqlite_is_refused(desk):
    from cashdesk.queue import proposal_hash, proposal_to_dict, proposal_from_dict
    d = desk()
    item = overpay_item(d)
    d.approve(item.id, "owner")
    p = proposal_to_dict(d.queue.get(item.id).proposal)
    p["target"] = "CAPTUREOTHER0001"
    p["api_calls"][0]["path"] = "/v2/payments/captures/CAPTUREOTHER0001/refund"
    with sqlite3.connect(d.queue.path) as con:  # an attacker with the database rewrites both
        con.execute("update queue_items set proposal = ?, target = ?, approved_hash = ? where id = ?",
                    (json.dumps(p), "CAPTUREOTHER0001", proposal_hash(proposal_from_dict(p)), item.id))
    with pytest.raises((PolicyBlocked, NotExecutable)):
        d.execute(item.id)
    assert d.client.calls == []


def test_2_status_flipped_to_approved_in_sqlite_does_not_run(desk):
    from cashdesk.queue import proposal_hash
    d = desk()
    item = overpay_item(d)
    with sqlite3.connect(d.queue.path) as con:
        con.execute("update queue_items set status = 'approved', decided_by = 'owner', approved_hash = ? where id = ?",
                    (proposal_hash(item.proposal), item.id))
    with pytest.raises((PolicyBlocked, NotExecutable)):
        d.execute(item.id)
    assert d.client.calls == []


def test_2_a_normal_approval_still_runs(desk):
    d = desk()
    item = overpay_item(d)
    d.approve(item.id, "owner")
    assert d.execute(item.id)["status"] == "executed"


# ---------- 3. limits re-read from current data at approval and execution ----------

class Moving(FixturePayPalClient):
    """Fixture data plus extra rows that appear on a later sync."""

    def __init__(self):
        super().__init__()
        self.extra = []

    def search_transactions(self, start, end):
        return list(super().search_transactions(start, end)) + self.extra


def test_3_overpayment_limit_is_revalidated_after_sync(desk):
    c = Moving()
    d = desk(c)
    item = overpay_item(d)
    assert item.proposal.amount == USD("45.00")
    cap = next(t for t in d.store.transactions() if t.id == item.proposal.target)
    c.extra = [txn("EXTREF000000001", "-25.00", cap.initiated + timedelta(hours=1), code="T1107", ref=cap.id,
                   invoice_ref=None)]
    d.sync()
    with pytest.raises(PolicyBlocked):
        d.approve(item.id, "owner")
        d.execute(item.id)
    assert c.calls == []


def test_3_duplicate_refunded_elsewhere_is_not_refunded_again(desk):
    c = Moving()
    d = desk(c)
    d.refresh_proposals()
    item = next(i for i in d.queue.list() if i.proposal.type == "refund_capture")
    d.approve(item.id, "owner")
    d.approve(item.id, "partner")  # 800.00: two people
    cap = next(t for t in d.store.transactions() if t.id == item.proposal.target)
    c.extra = [txn("EXTREF000000002", str(-cap.amount.amount), cap.initiated + timedelta(hours=1), code="T1107",
                   ref=cap.id, invoice_ref=None)]
    d.sync()
    with pytest.raises(PolicyBlocked):
        d.execute(item.id)
    assert c.calls == []


# ---------- 4. successive overpayments ----------

def test_4_second_overpayment_refunds_only_its_own_money():
    a = txn("PAYA00000000004", "150.00", T0)
    b = txn("PAYB00000000004", "60.00", T0 + timedelta(hours=1))
    r, props = run([inv()], [a, b])
    refunds = {p.target: p.amount for p in props if p.type == "refund_overpayment"}
    assert refunds == {a.id: USD("50.00"), b.id: USD("60.00")}
    lines = journal(r, [a, b])
    credits = sum(Decimal(l["credit"] or "0") for l in lines if l["account"] == "Customer credits")
    receivable = [Decimal(l["credit"]) for l in lines if l["account"] == "Accounts receivable" and l["credit"]]
    assert credits == Decimal("110.00") and sum(receivable) == Decimal("100.00") and min(receivable) > 0


# ---------- 5. PayPal's own record of earlier payments ----------

def test_5_partly_paid_invoice_with_no_local_payments_owes_only_the_rest():
    i = inv(status="PARTIALLY_PAID", paid="60.00", due_date=date(2026, 11, 1))
    r, props = run([i], [])
    x = r.invoice_by_number("INV-1")
    assert x.received == USD("60.00") and x.outstanding == USD("40.00")
    for p in props:
        if p.type == "send_reminder":
            assert p.amount == USD("40.00")


def test_5_aggregate_only_paid_amount_is_read_conservatively():
    # review A: a paid_amount with no payment list might already include the local payment, so it
    # only counts beyond what local rows explain (it must never create a refundable excess)
    i = inv(status="PARTIALLY_PAID", paid="60.00", due_date=date(2026, 11, 1))
    now = txn("PAYC00000000015", "40.00", T0)
    r, props = run([i], [now])
    assert r.invoice_by_number("INV-1").received == USD("60.00")
    assert not [p for p in props if p.type.startswith("refund")]


def test_5_old_payment_plus_current_payment_settles_the_invoice():
    old = [{"payment_id": "OLDPAY000000005", "amount": USD("60.00").to_paypal(), "method": "PAYPAL"}]
    i = inv(status="PARTIALLY_PAID", paid="60.00", due_date=date(2026, 11, 1), payments=old)
    now = txn("PAYC00000000005", "40.00", T0)
    r, props = run([i], [now])
    x = r.invoice_by_number("INV-1")
    assert x.received == USD("100.00") and x.outstanding == USD("0.00") and x.outcome == "paid"
    assert r.txn(now.id).outcome == "matched"
    assert not [p for p in props if p.type == "send_reminder"]


def test_5_a_payment_paypal_already_lists_is_not_counted_twice():
    now = txn("PAYD00000000005", "40.00", T0)
    pays = [{"payment_id": "OLDPAY000000005", "amount": USD("60.00").to_paypal(), "method": "PAYPAL"},
            {"payment_id": now.id, "amount": USD("40.00").to_paypal(), "method": "PAYPAL"}]
    i = inv(status="PAID", paid="100.00", payments=pays)
    r, _ = run([i], [now])
    x = r.invoice_by_number("INV-1")
    assert x.received == USD("100.00") and x.outcome == "paid"


# ---------- 6. queue races ----------

def test_6_reject_during_approval_wins(tmp_path):
    from tests.test_overpayment_refund import overpay
    from tests.test_overpayment_refund import env as _env  # noqa: F401
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    props = propose(reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler()), invs, txns, as_of=AS_OF)
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(overpay(props), invoice_due=USD("45.00"))
    real = q._check
    paused, go = threading.Event(), threading.Event()

    def slow_check(it, request_id=None):
        d = real(it, request_id)
        paused.set()
        go.wait(5)
        return d
    q._check = slow_check
    errs = []

    def approve():
        try:
            q.approve(item.id, by="owner")
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    th = threading.Thread(target=approve)
    th.start()
    paused.wait(5)
    threading.Timer(0.2, go.set).start()  # the reject below waits for the approval's transaction
    rejected = None
    try:
        rejected = q.reject(item.id, by="owner", note="no")
    except Exception as e:  # noqa: BLE001 - blocked until the approval finishes is also fine
        errs.append(e)
    go.set()
    th.join()
    final = q.get(item.id).status
    assert final in ("approved", "rejected")
    if rejected is not None and rejected.status == "rejected":
        assert final == "rejected"
    with pytest.raises((NotExecutable, PolicyBlocked)) if final == "rejected" else _null():
        q.execute(item.id, c)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_6_in_flight_runs_count_toward_the_hourly_cap(tmp_path):
    from cashdesk.actions import Proposal, _call
    now = {"t": T0}
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: now["t"])

    def refund(i):
        cap = f"CAPTURE{i:09d}"
        body = {"amount": USD("10.00").to_paypal(), "note_to_payer": "dup", "invoice_id": "X"}
        return Proposal(f"p{i}", "refund_capture", cap, "X", USD("10.00"),
                        [_call("POST", f"/v2/payments/captures/{cap}/refund", body)], "dup")
    items = [q.enqueue(refund(i)) for i in range(11)]
    for it in items[:10]:
        q.approve(it.id, by="owner")
    now["t"] = T0 + timedelta(hours=2)
    q.approve(items[10].id, by="owner")

    barrier = threading.Barrier(11, timeout=1)  # only ten may ever be inside the call at once
    sent = []

    class Held(FixturePayPalClient):
        def execute_call(self, method, path, body, *, request_id):
            sent.append(path)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return {"id": "R", "status": "COMPLETED"}
    c = Held()
    out = []

    def go(it):
        try:
            out.append(q.execute(it.id, c).status)
        except Exception as e:  # noqa: BLE001
            out.append(type(e).__name__)
    ths = [threading.Thread(target=go, args=(it,)) for it in items]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(sent) <= 10, out


# ---------- 7. rows sharing a transaction id ----------

def test_7_conversion_row_does_not_turn_a_duplicate_into_a_negative_refund(desk):
    one = txn("PAYMENTONE00007", "100.00", T0)
    two = txn("PAYMENTTWO00007", "100.00", T0 + timedelta(minutes=5))
    conv = txn("PAYMENTTWO00007", "-100.00", T0 + timedelta(minutes=5), code="T0200", invoice_ref=None, email=None)
    r, props = run([inv(status="PAID", paid="100.00")], [one, two, conv])
    assert r.txn(two.id, "T0006").outcome == "duplicate"
    (p,) = [p for p in props if p.type == "refund_capture"]
    assert p.amount == USD("100.00")
    assert "-100" not in r.txn(two.id, "T0006").reason


def test_7_grid_rows_have_distinct_ids_per_event_code():
    html = open("static/index.html").read()
    assert "rowKey" in html and "getRowId: p => p.data.rowKey" in html


# ---------- 8. journal: settled cash only ----------

@pytest.mark.parametrize("status", ["P", "D"])
def test_8_unfinished_refunds_are_not_booked(status):
    a = txn("PAYA00000000008", "100.00", T0)
    back = txn("REFA00000000008", "-40.00", T0 + timedelta(hours=1), code="T1107", status=status, ref=a.id,
               invoice_ref=None)
    r, _ = run([inv()], [a, back])
    assert not [l for l in journal(r, [a, back]) if l["paypal_transaction_id"] == back.id]


def test_8_reversal_takes_the_cash_back():
    a = txn("PAYA00000000018", "100.00", T0, status="V")
    rev = txn("REVA00000000018", "-100.00", T0 + timedelta(hours=1), code="T1106", ref=a.id, invoice_ref=None)
    r, _ = run([inv()], [a, rev])
    lines = journal(r, [a, rev])
    net = sum(Decimal(l["debit"] or "0") - Decimal(l["credit"] or "0") for l in lines if l["account"] == "PayPal balance")
    assert net == 0


# ---------- 9. preview == wire for the invoice send ----------

def test_9_send_flags_on_the_wire_match_the_preview(tmp_path):
    from tests.test_overpayment_refund import env as _e  # noqa: F401
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    props = propose(reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler()), invs, txns, as_of=AS_OF)
    p = next(p for p in props if p.type == "create_draft_invoice")
    q = ApprovalQueue(tmp_path / "q.db", clock=lambda: T0)
    item = q.enqueue(p)
    q.approve(item.id, by="owner")
    q.execute(item.id, c)
    sent = [x for x in c.calls if x["op"] == "send" or str(x.get("path", "")).endswith("/send")]
    assert sent and sent[-1]["body"] == p.api_calls[1]["body"]


# ---------- 10. AI output shape and citations ----------

def ai_with(content: dict):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})
    return OpenAICompatibleReconciler("m", http=httpx.Client(transport=httpx.MockTransport(handler)))


def fixture_data():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return c.list_invoices(), list(txns)


@pytest.mark.parametrize("bad", [{"resolutions": 1, "reasons": []}, {"resolutions": [], "reasons": 3},
                                 {"resolutions": [{"transaction_id": ["x"], "invoice_number": "NW-1",
                                                   "confidence": 0.9, "reason": "r", "cited_fields": []}],
                                  "reasons": []},
                                 {"resolutions": [], "reasons": [{"id": ["x"], "reason": "r", "cited_fields": []}]}])
def test_10_malformed_ai_output_falls_back(bad):
    invs, txns = fixture_data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ai_with(bad))
    assert r.ai_status.startswith("fallback")


def test_10_a_link_citing_a_field_that_does_not_exist_is_dropped():
    from cashdesk.ai import build_payload, candidates
    invs, txns = fixture_data()
    first = match(invs, txns, as_of=AS_OF)
    payload = build_payload(first, invs, txns)
    u = payload["unmatched_payments"][0]
    t = next(t for t in txns if t.id == u["transaction_id"])
    num = next(iter(candidates(first, invs, t))).number
    out = {"resolutions": [{"transaction_id": t.id, "invoice_number": num, "confidence": 0.99,
                            "reason": "Verified by payer_info.verified.", "cited_fields": ["payer_info.verified"]}],
           "reasons": []}
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ai_with(out))
    assert r.txn(t.id).rule != "ai"


# ---------- 11. an abandoned webhook claim ----------

def test_11_abandoned_processing_claim_is_retried(tmp_path, monkeypatch):
    from tests.test_webhooks import HEADERS, Verifier, event
    from cashdesk.webhooks import WebhookInbox, handle
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")
    from cashdesk.service import Desk
    d = Desk(client=Verifier(), reconciler=FallbackReconciler(), clock=lambda: T0)
    ev = json.loads(event())
    inbox = WebhookInbox(d.settings.var_dir / "cashdesk.db")
    assert inbox.claim(ev) == "new"  # a worker took it, then died before syncing
    with sqlite3.connect(d.settings.var_dir / "cashdesk.db") as con:
        con.execute("update webhook_events set received_at = ? where event_id = ?",
                    ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), ev["id"]))
    runs = len(d.store.sync_runs())
    code, body = handle(d, event().encode(), HEADERS)
    assert code == 200 and body["status"] == "synced"
    assert len(d.store.sync_runs()) == runs + 1


def test_11_a_claim_still_in_progress_asks_paypal_to_retry(tmp_path, monkeypatch):
    from tests.test_webhooks import HEADERS, Verifier, event
    from cashdesk.webhooks import WebhookInbox, handle
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")
    monkeypatch.setenv("AI_PROVIDER", "none")
    from cashdesk.service import Desk
    d = Desk(client=Verifier(), reconciler=FallbackReconciler(), clock=lambda: T0)
    assert WebhookInbox(d.settings.var_dir / "cashdesk.db").claim(json.loads(event())) == "new"
    code, body = handle(d, event().encode(), HEADERS)
    assert code >= 400 and body["status"] == "retry"


# ---------- found while checking the live desk: a slow reconcile started before a sync ----------

def test_a_reconcile_started_before_a_sync_does_not_overwrite_the_newer_result(desk):
    c = Moving()
    d = desk(c)
    d.result()
    started, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    class Slow(FallbackReconciler):
        def suggest(self, result, invoices, txns):
            calls["n"] += 1
            if calls["n"] == 1:  # the board's first read: its AI call is still running when the sync lands
                started.set()
                release.wait(5)
            return super().suggest(result, invoices, txns)
    d.reconciler = Slow()
    d.sync()
    old = threading.Thread(target=d.result)
    old.start()
    started.wait(5)
    extra = txn("LATEROW00000001", "5.00", T0, invoice_ref=None, email="new@example.com")
    c.extra = [extra]
    d.sync()
    fresh = threading.Thread(target=d.result)  # the read after the sync finishes first...
    fresh.start()
    fresh.join(1)
    release.set()  # ...then the old computation finishes
    old.join()
    fresh.join()
    assert any(t.txn_id == extra.id for t in d.result().transactions)
