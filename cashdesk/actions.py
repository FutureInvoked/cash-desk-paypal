"""Action proposer: turn reconciliation results into proposals. It never executes anything.

Each proposal carries the exact PayPal call(s) it would make (method, path, body), so the
approval UI can show the human precisely what will run. The policy engine (E4) and the
approval queue + executor (E5) decide whether it ever does. Shapes: docs/paypal-api-notes.md.

Note: in Transaction Search, a PayPal checkout payment's transaction_id is the capture id used by
POST /v2/payments/captures/{capture_id}/refund.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

from .matcher import MatchResult
from .models import Invoice, Money, Transaction

ACTION_TYPES = {"send_reminder", "create_draft_invoice", "record_payment", "refund_capture", "refund_overpayment"}
REQUEST_ID_PLACEHOLDER = "<set at execution>"
BUSINESS = "Northwind Studio"


@dataclass
class Proposal:
    id: str
    type: str
    target: str  # invoice id (reminder, record payment), capture id (refund), source txn id (draft)
    invoice_number: str | None
    amount: Money | None
    api_calls: list[dict]
    reason: str
    evidence: dict = field(default_factory=dict)
    needs_review: bool = False
    source: str = "rules"  # rules | ai | fallback

    @property
    def intent_key(self) -> str:
        """One intent per (action, target): the policy engine blocks a second one."""
        return f"{self.type}:{self.target}"


def _pid(type_: str, target: str) -> str:
    return "p_" + hashlib.sha256(f"{type_}:{target}".encode()).hexdigest()[:12]


def _call(method: str, path: str, body: dict | None) -> dict:
    return {"method": method, "path": path, "body": body, "headers": {"PayPal-Request-Id": REQUEST_ID_PLACEHOLDER}}


def propose(result: MatchResult, invoices: list[Invoice], txns: list[Transaction], *, as_of: date) -> list[Proposal]:
    inv_by_num = {i.number: i for i in invoices}
    txn_by_key = {(t.id, t.event_code, t.status): t for t in txns}  # ids repeat across event codes
    out: list[Proposal] = []

    # 1. reminders for overdue invoices
    for r in result.invoices:
        if r.outcome != "overdue":
            continue
        inv = inv_by_num[r.invoice_number]
        body = {
            "subject": f"Reminder: invoice {inv.number} from {BUSINESS} is {r.days_overdue} day{'' if r.days_overdue == 1 else 's'} overdue",
            "note": (f"Hi {inv.recipient_name or 'there'}, a friendly reminder that invoice {inv.number} "
                     f"({r.outstanding}) was due on {inv.due_date.isoformat()}. "
                     f"You can pay it from the original invoice link. Thank you!"),
            "send_to_invoicer": True,
            "send_to_recipient": True,
        }
        out.append(Proposal(
            _pid("send_reminder", inv.id), "send_reminder", inv.id, inv.number, r.outstanding,
            [_call("POST", f"/v2/invoicing/invoices/{inv.id}/remind", body)],
            f"Send a payment reminder for {inv.number}: {r.reason}",
            {"invoice.id": inv.id, "invoice.detail.payment_term.due_date": inv.due_date.isoformat(),
             "invoice.due_amount": str(r.outstanding), "days_overdue": r.days_overdue,
             "invoice.primary_recipients[0].billing_info.email_address": inv.recipient_email},
        ))

    for r in result.transactions:
        t = txn_by_key.get((r.txn_id, r.event_code, r.status))
        if t is None:
            continue
        # 2. refund the duplicate capture
        if r.outcome == "duplicate":
            left = r.difference if r.difference is not None and r.difference.amount > 0 else t.amount  # unrefunded part
            body = {"amount": left.to_paypal(),
                    "note_to_payer": f"Refund of a duplicate payment for invoice {r.invoice_number}. "
                                     f"Your first payment was received; thank you.",
                    "invoice_id": r.invoice_number}
            out.append(Proposal(
                _pid("refund_capture", t.id), "refund_capture", t.id, r.invoice_number, left,
                [_call("POST", f"/v2/payments/captures/{t.id}/refund", body)],
                f"Refund the duplicate payment: {r.reason}",
                dict(r.evidence),
            ))
        # 2b. refund only the excess of an overpayment (a partial refund of the same capture)
        elif r.outcome == "overpaid" and r.difference is not None and r.difference.amount > 0:
            excess = r.difference
            body = {"amount": excess.to_paypal(),
                    "note_to_payer": f"Refund of {excess} overpaid on invoice {r.invoice_number}. "
                                     f"The invoice is paid in full; thank you.",
                    "invoice_id": r.invoice_number}
            out.append(Proposal(
                _pid("refund_overpayment", t.id), "refund_overpayment", t.id, r.invoice_number, excess,
                [_call("POST", f"/v2/payments/captures/{t.id}/refund", body)],
                f"Refund the overpaid {excess} (partial refund of this payment): {r.reason}",
                {**r.evidence, "overpaid_by": str(excess)},
            ))
        # 3. draft + send an invoice for money received with no invoice
        elif r.outcome == "unmatched" and t.payer_email and t.amount.amount > 0:
            item = t.subject or (t.items[0] if t.items else "Payment received")
            given, _, surname = (t.payer_name or "").partition(" ")
            create = {
                "detail": {"currency_code": t.amount.currency, "invoice_date": as_of.isoformat(),
                           "note": f"Invoice for your PayPal payment {t.id} on {t.initiated.date().isoformat()}.",
                           "memo": f"Created by Cash Desk for unbilled payment {t.id}",
                           "payment_term": {"term_type": "DUE_ON_RECEIPT"}},
                "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname},
                                                         "email_address": t.payer_email}}],
                "items": [{"name": item[:200], "quantity": "1", "unit_amount": t.amount.to_paypal()}],
            }
            out.append(Proposal(
                _pid("create_draft_invoice", t.id), "create_draft_invoice", t.id, None, t.amount,
                [_call("POST", "/v2/invoicing/invoices", create),
                 _call("POST", "/v2/invoicing/invoices/{new_invoice_id}/send",
                       {"send_to_recipient": False, "send_to_invoicer": True})],
                f"Create an invoice for the unbilled payment so the books balance: {r.reason}",
                dict(r.evidence),
            ))
        # 4. record a payment received outside Invoicing against the suggested invoice
        elif r.outcome == "matched" and r.rule in ("ai", "memo_heuristic") and r.invoice_id:
            body = {"method": "PAYPAL", "payment_date": t.initiated.date().isoformat(),
                    "amount": t.amount.to_paypal(),
                    "note": f"PayPal transaction {t.id} (matched by Cash Desk)"}
            out.append(Proposal(
                _pid("record_payment", r.invoice_id), "record_payment", r.invoice_id, r.invoice_number,
                t.amount, [_call("POST", f"/v2/invoicing/invoices/{r.invoice_id}/payments", body)],
                f"Mark {r.invoice_number} as paid by this payment: {r.reason}",
                dict(r.evidence), needs_review=r.needs_review, source=r.resolved_by,
            ))

    return sorted(out, key=lambda p: (sorted(ACTION_TYPES).index(p.type), p.invoice_number or "", p.target))
