"""Approval queue + executor. The only code path that can make a PayPal write.

Lifecycle:  enqueue -> pending | blocked
            pending -> approved (human) | rejected (human) | expired
            approved -> executing -> executed | failed (retryable with the same request id)
The policy (cashdesk/policy.py) runs at enqueue, at approval and again right before execution,
against the proposal as stored now, so tampering, expiry, rate bursts and duplicate intents are
caught at the last moment. Each item executes with PayPal-Request-Id = cashdesk-<item id>; an
executed item returns its stored result and is never sent twice.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .actions import Proposal
from .models import Money
from .paypal.base import PayPalClient
from .policy import HistoryEntry, Policy

SCHEMA = """
create table if not exists queue_items (
    seq integer primary key autoincrement,
    id text unique not null,
    proposal_id text not null,
    intent_key text not null,
    type text not null,
    target text not null,
    proposal text not null,
    invoice_due text,
    status text not null,
    policy text not null,
    created_at text not null,
    decided_at text,
    decided_by text,
    note text,
    request_id text,
    executed_at text,
    result text,
    error text
);
"""
# queue status -> what the policy's history sees
HISTORY_STATUS = {"pending": "pending", "approved": "approved", "executing": "approved", "failed": "approved",
                  "executed": "executed", "rejected": "rejected", "blocked": "rejected", "expired": "expired"}
EXECUTABLE = {"approved", "executing", "failed"}


class NotExecutable(Exception):
    """The item is not in a state that allows this transition."""


class PolicyBlocked(Exception):
    def __init__(self, item, violations):
        super().__init__("; ".join(v["message"] for v in violations))
        self.item, self.violations = item, violations


def proposal_to_dict(p: Proposal) -> dict:
    d = asdict(p)
    d["amount"] = p.amount.to_paypal() if p.amount else None
    return d


def proposal_from_dict(d: dict) -> Proposal:
    d = dict(d)
    a = d.get("amount")
    d["amount"] = Money(Decimal(a["value"]), a["currency_code"]) if a else None
    return Proposal(**d)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class QueueItem:
    id: str
    proposal: Proposal
    status: str
    policy: dict
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    note: str | None
    request_id: str | None
    executed_at: datetime | None
    result: dict | None
    error: str | None
    invoice_due: Money | None

    def to_dict(self) -> dict:
        p = proposal_to_dict(self.proposal)
        return {"id": self.id, "status": self.status, "proposal": p, "policy": self.policy,
                "created_at": _iso(self.created_at), "decided_at": _iso(self.decided_at),
                "decided_by": self.decided_by, "note": self.note, "request_id": self.request_id,
                "executed_at": _iso(self.executed_at), "result": self.result, "error": self.error,
                "invoice_due": self.invoice_due.to_paypal() if self.invoice_due else None}


class ApprovalQueue:
    def __init__(self, path: Path | str, *, policy: Policy | None = None,
                 clock: Callable[[], datetime] | None = None, on_event: Callable[[dict], None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_event = on_event
        with self._conn() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ reads
    def _row_to_item(self, r) -> QueueItem:
        due = json.loads(r["invoice_due"]) if r["invoice_due"] else None
        return QueueItem(
            id=r["id"], proposal=proposal_from_dict(json.loads(r["proposal"])), status=r["status"],
            policy=json.loads(r["policy"]), created_at=_dt(r["created_at"]), decided_at=_dt(r["decided_at"]),
            decided_by=r["decided_by"], note=r["note"], request_id=r["request_id"],
            executed_at=_dt(r["executed_at"]), result=json.loads(r["result"]) if r["result"] else None,
            error=r["error"], invoice_due=Money(Decimal(due["value"]), due["currency_code"]) if due else None)

    def get(self, item_id: str) -> QueueItem:
        with self._conn() as con:
            r = con.execute("select * from queue_items where id = ?", (item_id,)).fetchone()
        if r is None:
            raise KeyError(item_id)
        return self._row_to_item(r)

    def list(self, status: str | None = None) -> list[QueueItem]:
        with self._conn() as con:
            if status:
                rows = con.execute("select * from queue_items where status = ? order by seq", (status,)).fetchall()
            else:
                rows = con.execute("select * from queue_items order by seq").fetchall()
        return [self._row_to_item(r) for r in rows]

    def history(self, exclude: str | None = None) -> list[HistoryEntry]:
        out = []
        for i in self.list():
            if i.id == exclude:
                continue
            at = i.executed_at or i.decided_at or i.created_at
            out.append(HistoryEntry(i.proposal.intent_key, HISTORY_STATUS[i.status], at, i.request_id, i.id))
        return out

    # ------------------------------------------------------------------ writes
    def _emit(self, event: str, item: QueueItem, **extra) -> None:
        if self.on_event:
            self.on_event({"event": event, "item_id": item.id, "at": _iso(self.clock()),
                           "type": item.proposal.type, "target": item.proposal.target,
                           "intent_key": item.proposal.intent_key,
                           "amount": item.proposal.amount.to_paypal() if item.proposal.amount else None,
                           "status": item.status, **extra})

    def _update(self, item_id: str, **cols) -> QueueItem:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._conn() as con:
            con.execute(f"update queue_items set {sets} where id = ?", (*cols.values(), item_id))
        return self.get(item_id)

    def _check(self, item: QueueItem, request_id: str | None = None):
        # The proposal's own history entry is identified by the queue item id.
        p = item.proposal
        check_p = Proposal(**{**p.__dict__, "id": item.id})
        return self.policy.check(check_p, now=self.clock(), created_at=item.created_at,
                                 history=self.history(exclude=item.id), invoice_due=item.invoice_due,
                                 request_id=request_id)

    def enqueue(self, p: Proposal, *, invoice_due: Money | None = None) -> QueueItem:
        now = self.clock()
        with self._conn() as con:
            seq = con.execute("select coalesce(max(seq), 0) + 1 from queue_items").fetchone()[0]
            item_id = f"q{seq:05d}"
            con.execute(
                """insert into queue_items (id, proposal_id, intent_key, type, target, proposal, invoice_due,
                                            status, policy, created_at) values (?,?,?,?,?,?,?,?,?,?)""",
                (item_id, p.id, p.intent_key, p.type, p.target, json.dumps(proposal_to_dict(p)),
                 json.dumps(invoice_due.to_paypal()) if invoice_due else None, "pending", "{}", _iso(now)))
        item = self.get(item_id)
        d = self._check(item)
        item = self._update(item_id, status="pending" if d.allowed else "blocked", policy=json.dumps(d.to_dict()))
        self._emit("enqueued" if d.allowed else "blocked", item, violations=d.violations)
        return item

    def _block(self, item: QueueItem, d) -> None:
        status = "expired" if "expired" in d.codes else "blocked"
        item = self._update(item.id, status=status, policy=json.dumps(d.to_dict()))
        self._emit(status, item, violations=d.violations)
        raise PolicyBlocked(item, d.violations)

    def approve(self, item_id: str, *, by: str) -> QueueItem:
        item = self.get(item_id)
        if item.status != "pending":
            raise NotExecutable(f"{item_id} is {item.status}; only pending items can be approved")
        d = self._check(item)
        if not d.allowed:
            self._block(item, d)
        item = self._update(item_id, status="approved", decided_at=_iso(self.clock()), decided_by=by,
                            policy=json.dumps(d.to_dict()))
        self._emit("approved", item, by=by)
        return item

    def reject(self, item_id: str, *, by: str, note: str | None = None) -> QueueItem:
        item = self.get(item_id)
        if item.status != "pending":
            raise NotExecutable(f"{item_id} is {item.status}; only pending items can be rejected")
        item = self._update(item_id, status="rejected", decided_at=_iso(self.clock()), decided_by=by, note=note)
        self._emit("rejected", item, by=by, note=note)
        return item

    def expire_stale(self) -> list[str]:
        out = []
        for item in self.list(status="pending"):
            if self.clock() - item.created_at > self.policy.config.expiry:
                item = self._update(item.id, status="expired")
                self._emit("expired", item)
                out.append(item.id)
        return out

    def execute(self, item_id: str, client: PayPalClient) -> QueueItem:
        item = self.get(item_id)
        if item.status == "executed":
            return item  # idempotent: never send twice
        if item.status not in EXECUTABLE:
            raise NotExecutable(f"{item_id} is {item.status}; it was never approved")
        request_id = f"cashdesk-{item.id}"
        d = self._check(item, request_id=request_id)
        if not d.allowed:
            self._block(item, d)
        item = self._update(item_id, status="executing", request_id=request_id)
        self._emit("executing", item, request_id=request_id, api_calls=item.proposal.api_calls)
        try:
            result = self._dispatch(item.proposal, client, request_id)
        except Exception as e:  # recorded, then re-raised; retry uses the same request id
            item = self._update(item_id, status="failed", error=f"{type(e).__name__}: {e}")
            self._emit("failed", item, error=item.error)
            raise
        item = self._update(item_id, status="executed", executed_at=_iso(self.clock()),
                            result=json.dumps(result, default=str), error=None)
        self._emit("executed", item, request_id=request_id, result=item.result)
        return item

    @staticmethod
    def _dispatch(p: Proposal, client: PayPalClient, rid: str) -> dict:
        body = p.api_calls[0]["body"] or {}
        if p.type == "send_reminder":
            return client.send_reminder(p.target, note=body.get("note", ""), subject=body.get("subject"),
                                        request_id=rid)
        if p.type == "refund_capture":
            return client.refund_capture(p.target, p.amount, request_id=rid,
                                         note_to_payer=body.get("note_to_payer", ""))
        if p.type == "record_payment":
            return client.record_payment(p.target, p.amount, payment_date=date.fromisoformat(body["payment_date"]),
                                         method=body.get("method", "PAYPAL"), note=body.get("note", ""),
                                         request_id=rid)
        if p.type == "create_draft_invoice":
            rec = body["primary_recipients"][0]["billing_info"]
            name = " ".join(x for x in (rec.get("name", {}).get("given_name"), rec.get("name", {}).get("surname")) if x)
            inv = client.create_draft_invoice(recipient_email=rec["email_address"], recipient_name=name,
                                              item=body["items"][0]["name"], amount=p.amount,
                                              note=body.get("detail", {}).get("note"), request_id=f"{rid}-create")
            sent = client.send_invoice(inv.id, request_id=f"{rid}-send")
            return {"invoice_id": inv.id, "invoice_number": inv.number, "send": sent}
        raise NotExecutable(f"unknown action type {p.type}")

    # test hook: simulate someone editing the stored proposal after approval
    def _tamper_for_test(self, item_id: str, fn) -> None:
        item = self.get(item_id)
        d = proposal_to_dict(item.proposal)
        fn(d)
        with self._conn() as con:
            con.execute("update queue_items set proposal = ? where id = ?", (json.dumps(d), item_id))
