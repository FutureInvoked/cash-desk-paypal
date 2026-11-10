"""Fourth review (failure angle, commit e0c359e): crashes, lost replies, two processes, old data,
Retry-After and clock steps. One or more tests per finding; crash points are simulated in-process."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from cashdesk.ai import FallbackReconciler
from cashdesk.audit import AuditLog, main as audit_main, verify
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.queue import NotExecutable, PolicyBlocked
from tests.accounts_helper import user

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
USD = lambda v: Money(Decimal(v), "USD")


@pytest.fixture
def desk(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL",
              "CASHDESK_AS_OF"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from cashdesk.service import Desk
    now = {"t": T0}

    def make(client=None):
        d = Desk(client=client or FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: now["t"])
        d.now = now
        return d
    return make


def item_of(d, ptype):
    d.refresh_proposals()
    return next(i for i in d.queue.list() if i.proposal.type == ptype)


class NoDedup(FixturePayPalClient):
    """Like PayPal Invoicing as far as we know: no PayPal-Request-Id deduplication."""

    def _once(self, op, method, path, request_id, body, result):
        res = result() if callable(result) else result
        self.calls.append({"op": op, "method": method, "path": path, "body": body,
                           "headers": {"PayPal-Request-Id": request_id}})
        return res


class LoseReply:
    """Mixin: the first matching write reaches PayPal, then the reply is lost."""
    lose = ()

    def execute_call(self, method, path, body, *, request_id):
        res = super().execute_call(method, path, body, request_id=request_id)
        for frag in self.lose:
            if frag in path and frag not in self.lost:
                self.lost.add(frag)
                raise httpx.ReadTimeout("reply lost")
        return res


# ---------- H1: a crash while writing the audit log ----------

class Crash(Exception):
    pass


def test_h1_crash_between_line_and_head_still_verifies_and_heals(tmp_path, monkeypatch):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    real = AuditLog._write_head

    def dies_before_moving_the_head(self, seq, h, *, nxt=None):
        if nxt is None:
            raise Crash()  # the line is written; the process dies before the head moves onto it
        return real(self, seq, h, nxt=nxt)
    monkeypatch.setattr(AuditLog, "_write_head", dies_before_moving_the_head)
    with pytest.raises(Crash):
        log.append({"event": "x", "i": 3})
    monkeypatch.setattr(AuditLog, "_write_head", real)
    assert verify(log.path).ok and verify(log.path).count == 4
    assert audit_main(["verify", "--path", str(log.path)]) == 0
    AuditLog(log.path).append({"event": "x", "i": 4})  # a fresh process repairs the head, then appends
    v = verify(log.path)
    assert v.ok and v.count == 5 and json.loads((tmp_path / "audit.jsonl.head").read_text())["seq"] == 5


def test_h1_torn_last_line_is_dropped_by_the_next_append(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    v = verify(log.path)
    log._write_head(3, v.head_hash, nxt="ab" * 32)  # step 1 of an append: the record is announced
    with open(log.path, "a") as f:
        f.write('{"event": "x", "seq": 4, "prev')  # then the process died mid-line
    assert verify(log.path).ok and len(log.records()) == 3
    AuditLog(log.path).append({"event": "y"})
    v = verify(log.path)
    assert v.ok and v.count == 4 and log.records()[-1]["event"] == "y"


def test_h1_bytes_appended_without_an_announced_record_fail(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    with open(log.path, "a") as f:
        f.write('{"event": "x", "seq": 4')
    assert not verify(log.path).ok


def test_h1_unreadable_head_fails_cleanly(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"event": "x"})
    log.append({"event": "x"})
    (tmp_path / "audit.jsonl.head").write_text("")
    v = verify(log.path)
    assert not v.ok and "head" in v.error


def test_h1_a_failed_audit_write_leaves_the_queue_unchanged(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    real = d.queue.on_event

    def broken(ev):
        raise OSError("disk full")
    d.queue.on_event = broken
    with pytest.raises(OSError):
        d.approve(item.id, user(d, "owner"))
    assert d.queue.get(item.id).status == "pending"  # not approved without a record of it
    with pytest.raises(OSError):
        d.reject(item.id, user(d, "owner"), None)
    assert d.queue.get(item.id).status == "pending"
    d.queue.on_event = real
    assert d.approve(item.id, user(d, "owner"))["status"] == "approved"


# ---------- H2 / M4: an approval the audit log doesn't show has a way out ----------

def unwitnessed_approved(d, ptype="refund_overpayment", with_hash=True):
    from cashdesk.queue import proposal_hash
    item = item_of(d, ptype)
    with sqlite3.connect(d.queue.path) as con:  # approved in the database only (crash, or an older version)
        con.execute("update queue_items set status = 'approved', decided_by = 'owner', approved_hash = ? where id = ?",
                    (proposal_hash(item.proposal) if with_hash else None, item.id))
    return item


@pytest.mark.parametrize("with_hash", [True, False])
def test_h2_m4_an_unbacked_approval_can_be_rejected_and_proposed_again(desk, with_hash):
    d = desk()
    item = unwitnessed_approved(d, with_hash=with_hash)
    with pytest.raises(NotExecutable):
        d.execute(item.id)
    # withdrawn: never really approved, so the action is proposed again on the next refresh
    assert d.reject(item.id, user(d, "owner"), "approval not in the audit log")["status"] == "withdrawn"
    assert d.client.calls == []


def test_h2_an_unbacked_approval_expires(desk):
    d = desk()
    item = unwitnessed_approved(d)
    d.now["t"] = T0 + timedelta(hours=25)
    assert item.id in d.queue.expire_stale()
    assert d.queue.get(item.id).status == "expired"


def test_m4_first_approval_from_an_older_version_starts_over(desk):
    d = desk()
    d.refresh_proposals()
    item = next(i for i in d.queue.list() if i.proposal.type == "refund_capture")  # 800.00: two people
    with sqlite3.connect(d.queue.path) as con:
        con.execute("update queue_items set status = 'awaiting_second', first_by = 'owner' where id = ?", (item.id,))
    with pytest.raises(NotExecutable):
        d.approve(item.id, user(d, "partner"))
    assert d.queue.get(item.id).status == "pending"
    d.approve(item.id, user(d, "owner"))
    d.approve(item.id, user(d, "partner"))
    assert d.execute(item.id)["status"] == "executed"


def test_a_started_run_cannot_be_rejected(desk):
    class Lossy(LoseReply, FixturePayPalClient):
        lose = ("/refund",)
        lost = set()
    d = desk(Lossy())
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, user(d, "owner"))
    with pytest.raises(httpx.ReadTimeout):
        d.execute(item.id)
    with pytest.raises(NotExecutable):
        d.reject(item.id, user(d, "owner"), None)


# ---------- H3: a retry after 24 h, or after the sync shows the refund ----------

def test_h3_lost_reply_near_expiry_is_retried_not_refunded_twice(desk):
    class Lossy(LoseReply, FixturePayPalClient):
        lose = ("/refund",)
        lost = set()
    c = Lossy()
    d = desk(c)
    item = item_of(d, "refund_overpayment")
    d.now["t"] = T0 + timedelta(hours=23, minutes=50)
    d.approve(item.id, user(d, "owner"))
    with pytest.raises(httpx.ReadTimeout):
        d.execute(item.id)
    d.now["t"] = T0 + timedelta(hours=24, minutes=5)
    assert d.execute(item.id)["status"] == "executed"
    d.refresh_proposals()
    refunds = [x for x in c.calls if x["op"] == "refund"]
    assert len({x["headers"]["PayPal-Request-Id"] for x in refunds}) == 1
    assert len([i for i in d.queue.list() if i.proposal.type == "refund_overpayment"]) == 1


def test_h3_crashed_run_is_retried_after_expiry_with_its_own_id(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, user(d, "owner"))
    with sqlite3.connect(d.queue.path) as con:  # died after claiming the run
        con.execute("update queue_items set status = 'executing', started_at = ? where id = ?",
                    ((T0 + timedelta(minutes=1)).isoformat(), item.id))
    d.now["t"] = T0 + timedelta(hours=25)
    done = d.execute(item.id)
    assert done["status"] == "executed" and done["request_id"] == item.request_id
    assert "expired" not in [r["event"] for r in d.audit.records() if r.get("item_id") == item.id]


def test_h3_a_sync_showing_the_refund_does_not_block_the_retry(desk):
    from tests.test_review3 import Moving, txn
    class Lossy(LoseReply, Moving):
        lose = ("/refund",)
        lost = set()
    c = Lossy()
    d = desk(c)
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, user(d, "owner"))
    with pytest.raises(httpx.ReadTimeout):
        d.execute(item.id)
    cap = next(t for t in d.store.transactions() if t.id == item.proposal.target)
    c.extra = [txn("REFUNDDONE00001", "-45.00", cap.initiated + timedelta(hours=1), code="T1107", ref=cap.id,
                   invoice_ref=None)]
    d.sync()
    assert d.execute(item.id)["status"] == "executed"  # same request id: PayPal returns the first result


# ---------- M1: invoicing writes are not resent after a lost reply ----------

def test_m1_record_payment_is_not_recorded_twice():
    from tests.test_review2 import data
    from cashdesk.actions import Proposal, _call
    from cashdesk.queue import ApprovalQueue

    class Lossy(LoseReply, NoDedup):
        lose = ("/payments",)
        lost = set()
    c = Lossy()
    inv = c._invoices[0]
    body = {"method": "PAYPAL", "payment_date": "2026-11-07", "amount": {"currency_code": "USD", "value": "10.00"},
            "note": "PayPal transaction ABC123 (matched by Cash Desk)"}
    p = Proposal("p1", "record_payment", inv["id"], "X", USD("10.00"),
                 [_call("POST", f"/v2/invoicing/invoices/{inv['id']}/payments", body)], "r")
    import tempfile
    q = ApprovalQueue(tempfile.mkdtemp() + "/q.db", clock=lambda: T0)
    it = q.enqueue(p, invoice_due=USD("100.00"))
    q.approve(it.id, by="owner")
    with pytest.raises(httpx.ReadTimeout):
        q.execute(it.id, c)
    assert q.execute(it.id, c).status == "executed"
    assert len([x for x in c.calls if x["op"] == "record_payment"]) == 1


def test_m1_draft_invoice_sent_but_reply_lost_finishes(desk):
    class Lossy(LoseReply, NoDedup):
        lose = ("/send",)
        lost = set()
    c = Lossy()
    d = desk(c)
    item = item_of(d, "create_draft_invoice")
    d.approve(item.id, user(d, "owner"))
    with pytest.raises(httpx.ReadTimeout):
        d.execute(item.id)
    done = d.execute(item.id)
    assert done["status"] == "executed"
    assert [x["op"] for x in c.calls].count("create_invoice") == 1
    assert [x["op"] for x in c.calls].count("send_invoice") == 1


def test_m1_crash_between_create_and_send_reuses_the_new_invoice(desk):
    class Lossy(LoseReply, NoDedup):
        lose = ("/send",)
        lost = set()

        def execute_call(self, method, path, body, *, request_id):
            if path.endswith("/send") and "/send" not in self.lost:
                self.lost.add("/send")
                raise httpx.ConnectError("died before send")
            return super().execute_call(method, path, body, request_id=request_id)
    c = Lossy()
    d = desk(c)
    item = item_of(d, "create_draft_invoice")
    d.approve(item.id, user(d, "owner"))
    with pytest.raises(httpx.ConnectError):
        d.execute(item.id)
    assert d.execute(item.id)["status"] == "executed"
    ops = [x["op"] for x in c.calls]
    assert ops.count("create_invoice") == 1 and ops.count("send_invoice") == 1


# ---------- M2: PayPal says the refund failed ----------

@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_m2_failed_refund_is_not_done_and_can_be_proposed_again(desk, status):
    class Refuses(FixturePayPalClient):
        def execute_call(self, method, path, body, *, request_id):
            if path.endswith("/refund"):
                return {"id": "R1", "status": status}
            return super().execute_call(method, path, body, request_id=request_id)
    d = desk(Refuses())
    item = item_of(d, "refund_overpayment")
    d.approve(item.id, user(d, "owner"))
    assert d.execute(item.id)["status"] == "declined"
    d.refresh_proposals()
    assert [i.status for i in d.queue.list() if i.proposal.type == "refund_overpayment"] == ["declined", "pending"]


# ---------- M3: reads while another process writes ----------

def test_m3_verify_and_records_never_see_a_half_written_log(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    stop, bad = threading.Event(), []

    def writer():
        other = AuditLog(tmp_path / "audit.jsonl")
        for i in range(300):
            other.append({"event": "x", "pad": "y" * 2000, "i": i})
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                if not verify(log.path).ok:
                    bad.append("verify")
                log.records()
            except Exception as e:  # noqa: BLE001
                bad.append(repr(e))
    ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert bad == []


# ---------- L1: a crashed run has a button ----------

def test_l1_board_offers_retry_for_a_stuck_run():
    html = open("static/index.html").read()
    assert "const stuck = it =>" in html and "|| stuck(it)" in html


# ---------- L2: Retry-After ----------

def paypal_429(retry_after):
    from cashdesk.paypal.sandbox import SandboxPayPalClient
    seen = []

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AAtok", "expires_in": 3600})
        seen.append(1)
        return httpx.Response(429, headers={"Retry-After": retry_after}, json={"name": "RATE_LIMIT_REACHED"})
    slept = []
    c = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(handler), sleep=slept.append)
    return c, slept


@pytest.mark.parametrize("value", ["Wed, 21 Oct 2099 07:28:00 GMT", "3600", "soon"])
def test_l2_long_or_odd_retry_after_fails_fast_as_a_paypal_error(value):
    from cashdesk.paypal.sandbox import PayPalError
    c, slept = paypal_429(value)
    with pytest.raises(PayPalError):
        c._call("GET", "/v1/reporting/transactions")
    assert sum(slept) <= 30


def test_l2_http_date_retry_after_is_understood():
    from email.utils import format_datetime
    from cashdesk.paypal.sandbox import retry_after
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    assert 0 <= retry_after(soon, default=1) <= 6
    assert retry_after("2", default=1) == 2.0 and retry_after(None, default=4) == 4.0


# ---------- L3: the clock steps back ----------

def test_l3_a_run_dated_in_the_future_still_counts_toward_the_hourly_cap():
    from cashdesk.actions import Proposal, _call
    from cashdesk.policy import HistoryEntry, Policy, PolicyConfig
    pol = Policy(PolicyConfig(max_per_hour=1))
    body = {"subject": "s", "note": "n", "send_to_invoicer": True, "send_to_recipient": True}
    p = Proposal("p2", "send_reminder", "INV2-AAAA", "X", USD("10.00"),
                 [_call("POST", "/v2/invoicing/invoices/INV2-AAAA/remind", body)], "r")
    hist = [HistoryEntry("send_reminder:INV2-BBBB", "executed", T0 + timedelta(minutes=3), "rid", "q1")]
    d = pol.check(p, now=T0, created_at=T0, history=hist)
    assert "rate_cap" in d.codes


def test_l3_clock_skew_refuses_for_now_without_blocking_for_good(desk):
    d = desk()
    item = item_of(d, "refund_overpayment")
    d.now["t"] = T0 - timedelta(minutes=20)  # clock stepped back
    with pytest.raises(PolicyBlocked):
        d.approve(item.id, user(d, "owner"))
    assert d.queue.get(item.id).status == "pending"
    d.now["t"] = T0 + timedelta(minutes=1)
    assert d.approve(item.id, user(d, "owner"))["status"] == "approved"
