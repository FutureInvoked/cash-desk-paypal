"""Desk: the one service object the web app talks to.

Wires the PayPal client, the SQLite store, reconciliation (rules + AI), the action proposer,
the approval queue (policy-gated) and the audit log. All app code goes through here.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable

from .actions import propose
from .ai import make_reconciler, reconcile
from .audit import AuditLog, verify
from .config import load_settings
from .matcher import MatchResult
from .models import Money
from .paypal import make_client
from .paypal.base import PayPalClient
from .policy import Policy
from .queue import ApprovalQueue
from .store import Store

FIXTURES_AS_OF = date(2026, 11, 7)
UNRECONCILED = {"unmatched", "currency_mismatch", "duplicate"}
OUTSTANDING = {"open", "overdue", "partial"}


def money_json(m: Money | None) -> dict | None:
    return {"value": f"{m.amount:.2f}", "currency": m.currency} if m else None


class Desk:
    def __init__(self, *, client: PayPalClient | None = None, reconciler=None,
                 clock: Callable[[], datetime] | None = None, policy: Policy | None = None):
        self.settings = load_settings()
        self.client = client or make_client()
        self.reconciler = reconciler or make_reconciler()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        var = self.settings.var_dir
        self.store = Store(var / "cashdesk.db")
        self.audit = AuditLog(var / "audit.jsonl")
        self.queue = ApprovalQueue(var / "cashdesk.db", policy=policy, clock=self.clock, on_event=self.audit.append,
                                   witness=self._approval_records)
        self._result: MatchResult | None = None
        self._result_run: int | None = None
        self._lock = threading.Lock()

    # ---- time window ----
    @property
    def as_of(self) -> date:
        env = os.environ.get("CASHDESK_AS_OF")
        if env:
            return date.fromisoformat(env)
        return FIXTURES_AS_OF if self.client.mode == "fixtures" else self.clock().date()

    def window(self) -> tuple[datetime, datetime]:
        end = datetime.combine(self.as_of + timedelta(days=1), time(0), tzinfo=timezone.utc)
        if self.client.mode != "fixtures":
            end = min(end, self.clock())
        return end - timedelta(days=31), end

    # ---- sync + reconcile ----
    def sync(self) -> dict:
        start, end = self.window()
        res = self.store.sync(self.client, start, end)
        self._result = None
        return res

    def result(self) -> MatchResult:
        last = self.store.last_sync()
        if last is None:
            self.sync()
            last = self.store.last_sync()
        run = last["id"]
        cached, cached_run = self._result, self._result_run
        if cached is not None and cached_run == run:
            return cached
        # Reconciling can take a minute (the AI call). It is cached under the sync run it read, and
        # only if no newer sync landed meanwhile (here or in the webhook server), so a slow result
        # computed from older data never replaces a newer one.
        res = reconcile(self.store.invoices(), self.store.transactions(), as_of=self.as_of,
                        reconciler=self.reconciler)
        with self._lock:
            latest = self.store.last_sync()
            if latest and latest["id"] == run and (self._result_run != run or self._result is None):
                self._result, self._result_run = res, run
        return res

    def reconciliation(self) -> dict:
        r = self.result()
        txns = {(t.id, t.event_code, t.status): t for t in self.store.transactions()}  # ids repeat across event codes
        invs = {i.number: i for i in self.store.invoices()}
        rows = []
        for x in r.transactions:
            t = txns[(x.txn_id, x.event_code, x.status)]
            rows.append({
                "txn_id": x.txn_id, "event_code": x.event_code, "status": t.status, "outcome": x.outcome,
                "rule": x.rule, "invoice_number": x.invoice_number, "invoice_id": x.invoice_id,
                "date": t.initiated.date().isoformat(), "amount": money_json(t.amount), "fee": money_json(t.fee),
                "payer_email": t.payer_email, "payer_name": t.payer_name, "subject": t.subject, "note": t.note,
                "difference": money_json(x.difference), "duplicate_of": x.duplicate_of, "candidates": x.candidates,
                "reason": x.reason, "reason_source": x.reason_source, "resolved_by": x.resolved_by,
                "confidence": x.confidence, "needs_review": x.needs_review, "evidence": x.evidence,
            })
        inv_rows = []
        for x in r.invoices:
            i = invs[x.invoice_number]
            inv_rows.append({
                "invoice_number": x.invoice_number, "invoice_id": x.invoice_id, "status": x.status,
                "outcome": x.outcome, "customer": i.recipient_business or i.recipient_name,
                "recipient_email": i.recipient_email, "total": money_json(x.total), "received": money_json(x.received),
                "outstanding": money_json(x.outstanding), "due_date": i.due_date.isoformat() if i.due_date else None,
                "days_overdue": x.days_overdue, "transactions": x.transactions, "reason": x.reason,
                "reason_source": x.reason_source,
            })
        last = self.store.last_sync()
        return {"as_of": self.as_of.isoformat(), "mode": self.client.mode, "ai_status": r.ai_status,
                "last_sync": last["at"] if last else None, "transactions": rows, "invoices": inv_rows,
                "summary": self.summary(r, txns)}

    def summary(self, r: MatchResult, txns) -> dict:
        payments = [x for x in r.transactions if x.outcome not in ("ignored", "refund")]
        matched = [x for x in payments if x.outcome == "matched"]
        unrec: dict[str, Decimal] = {}
        for x in r.transactions:
            if x.outcome in UNRECONCILED:
                a = txns[(x.txn_id, x.event_code, x.status)].amount
                unrec[a.currency] = unrec.get(a.currency, Decimal("0")) + a.amount
        outstanding: dict[str, Decimal] = {}
        for x in r.invoices:
            if x.outcome in OUTSTANDING:
                outstanding[x.outstanding.currency] = outstanding.get(x.outstanding.currency, Decimal("0")) + x.outstanding.amount
        s = r.summary()
        return {**s, "payments": len(payments), "matched": len(matched), "matched_pct": round(100 * len(matched) / len(payments), 1) if payments else 0.0,
                "unreconciled": {k: f"{v:.2f}" for k, v in sorted(unrec.items())},
                "outstanding": {k: f"{v:.2f}" for k, v in sorted(outstanding.items())},
                "pending_actions": len(self.queue.list(status="pending")) + len(self.queue.list(status="awaiting_second"))}

    # ---- proposals + approvals ----
    def refresh_proposals(self) -> dict:
        r = self.result()
        # Anything already queued (live, blocked or rejected) is not proposed again; expired can be.
        # (declined = PayPal answered no, nothing moved: it may be proposed again)
        # (declined = PayPal answered no; withdrawn = an approval the audit log didn't back: nothing
        # moved, so both may be proposed again)
        self.queue.expire_stale()
        seen = {i.proposal.intent_key for i in self.queue.list()
                if i.status not in ("expired", "declined", "withdrawn")}
        enq = skipped = 0
        for p in propose(r, self.store.invoices(), self.store.transactions(), as_of=self.as_of):
            if p.intent_key in seen:
                skipped += 1
                continue
            # the limit is stored apart from the proposal, from reconciliation (see current_limit)
            if self.queue.enqueue_new(p, invoice_due=self.current_limit(p)) is None:
                skipped += 1  # another "Propose actions" added it a moment ago
                continue
            enq += 1
        return {"enqueued": enq, "skipped": skipped}

    def _approval_records(self, item_id: str) -> list[dict] | None:
        """The audit log's records for one queue item (its whole history, so a later reject, expiry or
        finished run is seen); None when the log doesn't verify."""
        if not verify(self.audit.path).ok:
            return None
        return [r for r in self.audit.records() if r.get("item_id") == item_id]  # every state, in order

    def current_limit(self, p) -> Money:
        """What the proposal may move now, from the latest sync, not from when it was proposed: the
        amount the proposer would offer today for the same action (for a record-payment, also no more
        than PayPal says the invoice still owes). Zero when the reason for it is gone: the invoice was
        paid, the duplicate or overpayment was refunded elsewhere, the unbilled payment was matched."""
        zero = Money(Decimal("0"), p.amount.currency if p.amount else "USD")
        r = self.result()
        fresh = next((q for q in propose(r, self.store.invoices(), self.store.transactions(), as_of=self.as_of)
                      if q.intent_key == p.intent_key), None)
        if fresh is None or fresh.amount is None:
            return zero
        if p.type == "record_payment":
            inv = next((i for i in self.store.invoices() if i.id == p.target), None)
            if inv is None or inv.due.currency != fresh.amount.currency:
                return zero
            return min((fresh.amount, inv.due), key=lambda m: m.amount)
        return fresh.amount

    def approvals(self) -> list[dict]:
        self.queue.expire_stale()  # nothing waits past its 24 hours just because nobody clicked
        return [i.to_dict() for i in self.queue.list()]

    def approve(self, item_id: str, by: str, expected_hash: str | None = None) -> dict:
        item = self.queue.get(item_id)
        return self.queue.approve(item_id, by=by, limit=self.current_limit(item.proposal),
                                  expected_hash=expected_hash).to_dict()

    def reject(self, item_id: str, by: str, note: str | None) -> dict:
        return self.queue.reject(item_id, by=by, note=note).to_dict()

    def execute(self, item_id: str) -> dict:
        item = self.queue.get(item_id)
        item = self.queue.execute(item_id, self.client, limit=self.current_limit(item.proposal))
        self._result = None  # PayPal state changed; re-reconcile on next read (after a sync)
        return item.to_dict()

    # ---- accounting export ----
    def journal_csv(self) -> str:
        from .journal import journal, to_csv
        return to_csv(journal(self.result(), self.store.transactions()))

    # ---- audit ----
    def audit_view(self) -> dict:
        v = verify(self.audit.path)
        return {"records": self.audit.records(), "verify": {"ok": v.ok, "count": v.count, "head_hash": v.head_hash,
                                                            "first_bad_seq": v.first_bad_seq, "error": v.error}}
