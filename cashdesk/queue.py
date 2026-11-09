"""Approval queue + executor. The only code path that can make a PayPal write.

Lifecycle:  enqueue -> pending | blocked
            pending -> approved (human) | rejected (human) | expired
            pending -> awaiting_second (first of two approvers, for large refunds) -> approved (a
                       different second approver) | rejected | expired
            approved -> executing -> executed | failed (retryable with the same request id)
The policy (cashdesk/policy.py) runs at enqueue, at approval and again right before execution,
against the proposal as stored now, so tampering, expiry, rate bursts and duplicate intents are
caught at the last moment. Each item executes with PayPal-Request-Id = cashdesk-<item id>; an
executed item returns its stored result and is never sent twice.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
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
    error text,
    first_by text,
    first_at text,
    approved_hash text,
    started_at text
);
"""
# queue status -> what the policy's history sees
HISTORY_STATUS = {"pending": "pending", "awaiting_second": "pending", "approved": "approved", "executing": "approved", "failed": "approved",
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


def _person(name: str | None) -> str:
    """Compare approver names as people would read them: NFKC (fullwidth -> ASCII), no invisible
    format characters (zero-width spaces), no spaces, case-folded."""
    import unicodedata
    n = unicodedata.normalize("NFKC", name or "")
    n = "".join(ch for ch in n if unicodedata.category(ch) not in ("Cf", "Cc", "Zs") and not ch.isspace())
    return n.casefold()


def proposal_hash(p: Proposal) -> str:
    """Fingerprint of everything that would run; taken at approval, checked again before running."""
    return hashlib.sha256(json.dumps(proposal_to_dict(p), sort_keys=True, default=str).encode()).hexdigest()


STALE_EXECUTING_SECONDS = 300  # a run that crashed mid-call may be retried after this
KEEP = object()  # approve/execute: keep the stored limit


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
    first_by: str | None = None
    first_at: datetime | None = None
    approvals_needed: int = 1
    approved_hash: str | None = None
    started_at: datetime | None = None

    def to_dict(self) -> dict:
        p = proposal_to_dict(self.proposal)
        return {"id": self.id, "status": self.status, "proposal": p, "policy": self.policy,
                "created_at": _iso(self.created_at), "decided_at": _iso(self.decided_at),
                "decided_by": self.decided_by, "note": self.note, "request_id": self.request_id,
                "executed_at": _iso(self.executed_at), "result": self.result, "error": self.error,
                "invoice_due": self.invoice_due.to_paypal() if self.invoice_due else None,
                "first_by": self.first_by, "first_at": _iso(self.first_at), "approvals_needed": self.approvals_needed}


class ApprovalQueue:
    def __init__(self, path: Path | str, *, policy: Policy | None = None,
                 clock: Callable[[], datetime] | None = None, on_event: Callable[[dict], None] | None = None,
                 witness: Callable[[str], list[dict] | None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_event = on_event
        self.witness = witness  # item id -> its approval records from the audit log (None: log broken)
        with self._conn() as con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("pragma table_info(queue_items)")}
            for col in ("first_by", "first_at", "approved_hash", "started_at"):  # older databases
                if col not in cols:
                    con.execute(f"alter table queue_items add column {col} text")

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
            error=r["error"], invoice_due=Money(Decimal(due["value"]), due["currency_code"]) if due else None,
            first_by=r["first_by"], first_at=_dt(r["first_at"]), approved_hash=r["approved_hash"],
            started_at=_dt(r["started_at"]))

    def get(self, item_id: str) -> QueueItem:
        with self._conn() as con:
            r = con.execute("select * from queue_items where id = ?", (item_id,)).fetchone()
        if r is None:
            raise KeyError(item_id)
        return self._with_needed(self._row_to_item(r))

    def _with_needed(self, item: QueueItem) -> QueueItem:
        item.approvals_needed = self.policy.config.approvals_needed(item.proposal)
        return item

    def list(self, status: str | None = None) -> list[QueueItem]:
        with self._conn() as con:
            if status:
                rows = con.execute("select * from queue_items where status = ? order by seq", (status,)).fetchall()
            else:
                rows = con.execute("select * from queue_items order by seq").fetchall()
        return [self._with_needed(self._row_to_item(r)) for r in rows]

    def history(self, exclude: str | None = None) -> list[HistoryEntry]:
        out = []
        for i in self.list():
            if i.id == exclude:
                continue
            # a run counts from when it started, so in-flight calls count toward the hourly cap at once
            at = i.started_at or i.executed_at or i.decided_at or i.created_at
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

    @contextmanager
    def _locked(self):
        """One state transition at a time, across threads and processes: read, check and write
        inside a single BEGIN IMMEDIATE transaction, so nothing changes between the check and the write."""
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("begin immediate")
            try:
                yield con
            except BaseException:
                con.execute("rollback")
                raise
            con.execute("commit")
        finally:
            con.close()

    def _read(self, con, item_id: str) -> QueueItem:
        r = con.execute("select * from queue_items where id = ?", (item_id,)).fetchone()
        if r is None:
            raise KeyError(item_id)
        return self._with_needed(self._row_to_item(r))

    @staticmethod
    def _write(con, item_id: str, **cols) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        con.execute(f"update queue_items set {sets} where id = ?", (*cols.values(), item_id))

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
            # unique across databases and reinstalls: q00001 restarts, the random part doesn't
            request_id = f"cashdesk-{item_id}-{secrets.token_hex(4)}"
            con.execute(
                """insert into queue_items (id, proposal_id, intent_key, type, target, proposal, invoice_due,
                                            status, policy, created_at, request_id) values (?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, p.id, p.intent_key, p.type, p.target, json.dumps(proposal_to_dict(p)),
                 json.dumps(invoice_due.to_paypal()) if invoice_due else None, "pending", "{}", _iso(now), request_id))
        item = self.get(item_id)
        d = self._check(item)
        item = self._update(item_id, status="pending" if d.allowed else "blocked", policy=json.dumps(d.to_dict()))
        self._emit("enqueued" if d.allowed else "blocked", item, violations=d.violations)
        return item

    def _block(self, item: QueueItem, d) -> None:
        """Outside a transition: record the block and raise."""
        status = "expired" if "expired" in d.codes else "blocked"
        item = self._update(item.id, status=status, policy=json.dumps(d.to_dict()))
        self._emit(status, item, violations=d.violations)
        raise PolicyBlocked(item, d.violations)

    def _block_in(self, con, item: QueueItem, d) -> tuple:
        status = "expired" if "expired" in d.codes else "blocked"
        self._write(con, item.id, status=status, policy=json.dumps(d.to_dict()))
        return status, d

    def _raise_blocked(self, item_id: str, blocked) -> None:
        status, d = blocked
        item = self.get(item_id)
        self._emit(status, item, violations=d.violations)
        raise PolicyBlocked(item, d.violations)

    @staticmethod
    def _same_person(a: str | None, b: str | None) -> bool:
        return _person(a) == _person(b)

    @staticmethod
    def _tamper_decision(item: QueueItem):
        """A block decision when the stored proposal differs from what was approved, else None."""
        from .policy import Decision
        if item.approved_hash and proposal_hash(item.proposal) != item.approved_hash:
            return Decision(False, [{"code": "integrity", "message": "the proposal changed after it was approved"}])
        return None

    def _set_limit(self, con, item: QueueItem, limit) -> QueueItem:
        """Replace the stored limit with one re-read from current data (see Desk)."""
        if limit is not KEEP:
            item.invoice_due = limit
            self._write(con, item.id, invoice_due=json.dumps(limit.to_paypal()) if limit else None)
        return item

    def approve(self, item_id: str, *, by: str, limit=None) -> QueueItem:
        limit = KEEP if limit is None else limit
        blocked = event = None
        with self._locked() as con:
            item = self._read(con, item_id)
            if item.status not in ("pending", "awaiting_second"):
                raise NotExecutable(f"{item_id} is {item.status}; only pending items can be approved")
            if item.status == "awaiting_second" and self._same_person(by, item.first_by):
                raise NotExecutable(f"{item_id} was first approved by {item.first_by}; "
                                    f"a different person must give the second approval")
            item = self._set_limit(con, item, limit)
            # the second approver must approve what the first one saw
            d = (self._tamper_decision(item) if item.status == "awaiting_second" else None) or self._check(item)
            if not d.allowed:
                blocked = self._block_in(con, item, d)
            else:
                now = _iso(self.clock())
                h = item.approved_hash or proposal_hash(item.proposal)
                if item.status == "pending" and item.approvals_needed > 1:
                    self._write(con, item_id, status="awaiting_second", first_by=by, first_at=now,
                                policy=json.dumps(d.to_dict()), approved_hash=h)
                    event = ("first_approval", {"by": by, "proposal_hash": h})
                else:
                    self._write(con, item_id, status="approved", decided_at=now, decided_by=by,
                                policy=json.dumps(d.to_dict()), approved_hash=h)
                    event = ("approved", {"by": by, "proposal_hash": h,
                                          **({"first_by": item.first_by} if item.first_by else {})})
        if blocked:
            self._raise_blocked(item_id, blocked)
        item = self.get(item_id)
        self._emit(event[0], item, **event[1])
        return item

    def reject(self, item_id: str, *, by: str, note: str | None = None) -> QueueItem:
        with self._locked() as con:
            item = self._read(con, item_id)
            if item.status not in ("pending", "awaiting_second"):
                raise NotExecutable(f"{item_id} is {item.status}; only pending items can be rejected")
            self._write(con, item_id, status="rejected", decided_at=_iso(self.clock()), decided_by=by, note=note)
        item = self.get(item_id)
        self._emit("rejected", item, by=by, note=note)
        return item

    def expire_stale(self) -> list[str]:
        out = []
        for item in self.list(status="pending") + self.list(status="awaiting_second"):
            if self.clock() - item.created_at > self.policy.config.expiry:
                with self._locked() as con:
                    cur = con.execute("update queue_items set status = 'expired' where id = ? "
                                      "and status in ('pending', 'awaiting_second')", (item.id,))
                    if cur.rowcount != 1:
                        continue
                item = self.get(item.id)
                self._emit("expired", item)
                out.append(item.id)
        return out

    def _witnessed(self, item: QueueItem) -> str | None:
        """Why the approval isn't backed by the audit log, or None. The queue row can be edited by
        anyone with the database; the hash-chained audit log must also show the approval(s) of this
        exact proposal fingerprint, by the people the row names."""
        if self.witness is None:
            return None
        recs = self.witness(item.id)
        if recs is None:
            return "the audit log does not verify"
        def by(ev: str) -> list[str]:
            return [r.get("by") for r in recs if r.get("event") == ev and r.get("proposal_hash") == item.approved_hash]
        final = by("approved")
        if not any(self._same_person(b, item.decided_by) for b in final):
            return "no approval of this exact proposal is recorded in the audit log"
        if item.approvals_needed > 1:
            firsts = by("first_approval")
            if not any(self._same_person(f, item.first_by) and not self._same_person(f, item.decided_by) for f in firsts):
                return "the audit log does not show two different approvers for this exact proposal"
        return None

    def execute(self, item_id: str, client: PayPalClient, *, limit=None) -> QueueItem:
        limit = KEEP if limit is None else limit
        item = self.get(item_id)
        if item.status == "executed":
            return item  # idempotent: never send twice
        blocked = None
        with self._locked() as con:  # check and claim in one transaction: of two runs, one goes on
            item = self._read(con, item_id)
            if item.status == "executed":
                return item
            stale = self.clock() - timedelta(seconds=STALE_EXECUTING_SECONDS)
            if item.status == "executing" and item.started_at and item.started_at >= stale:
                raise NotExecutable(f"{item_id} is already running")
            if item.status not in EXECUTABLE:
                raise NotExecutable(f"{item_id} is {item.status}; it was never approved")
            if item.approvals_needed > 1 and (not item.first_by or not item.decided_by
                                              or self._same_person(item.first_by, item.decided_by)):
                raise NotExecutable(f"{item_id} needs two different approvers before it can run")
            if not item.approved_hash:
                raise NotExecutable(f"{item_id} has no approval fingerprint; approve it again")
            why = self._witnessed(item)
            if why:
                raise NotExecutable(f"{item_id}: {why}")
            item = self._set_limit(con, item, limit)
            request_id = item.request_id or f"cashdesk-{item.id}-{secrets.token_hex(4)}"
            d = self._tamper_decision(item) or self._check(item, request_id=request_id)
            if not d.allowed:
                blocked = self._block_in(con, item, d)
            else:
                # started_at counts this run toward the hourly cap from now, not from its approval
                self._write(con, item_id, status="executing", request_id=request_id, started_at=_iso(self.clock()))
        if blocked:
            self._raise_blocked(item_id, blocked)
        item = self.get(item_id)
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
        """Send the proposal's stored calls exactly as approved. A two-call proposal (create, then
        send an invoice) fills the new invoice id into the second path and gets -create/-send ids."""
        calls = p.api_calls
        if len(calls) == 1:
            c = calls[0]
            return client.execute_call(c["method"], c["path"], c["body"], request_id=rid)
        created = client.execute_call(calls[0]["method"], calls[0]["path"], calls[0]["body"], request_id=f"{rid}-create")
        new_id = created.get("id") if isinstance(created, dict) else None
        if not new_id:
            raise RuntimeError("PayPal did not return the new invoice id")
        second = calls[1]
        sent = client.execute_call(second["method"], second["path"].replace("{new_invoice_id}", new_id),
                                   second["body"], request_id=f"{rid}-send")
        return {"invoice_id": new_id, "invoice_number": (created.get("detail") or {}).get("invoice_number"),
                "send": sent}

    # test hook: simulate someone editing the stored proposal after approval
    def _tamper_for_test(self, item_id: str, fn) -> None:
        item = self.get(item_id)
        d = proposal_to_dict(item.proposal)
        fn(d)
        with self._conn() as con:
            con.execute("update queue_items set proposal = ? where id = ?", (json.dumps(d), item_id))
