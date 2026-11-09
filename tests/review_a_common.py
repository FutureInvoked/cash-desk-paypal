"""Synthetic PayPal-shaped inputs; no network clients or external providers."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from cashdesk.paypal.parse import parse_invoice, parse_transaction
from cashdesk.ai import reconcile, FallbackReconciler
from cashdesk.actions import propose
from cashdesk.journal import journal, to_csv

AS_OF = date(2026, 11, 9)
T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
D = Decimal

def money(cents, ccy="USD"):
    return {"currency_code": ccy, "value": f"{D(cents) / 100:.2f}"}

def invoice(total=10000, number="INV-1", ccy="USD", status="SENT", paid=0,
            payments=None, refunds=None, due=None, email="pat@example.com"):
    raw = {"id": f"INV2-{number}", "status": status,
           "detail": {"invoice_number": number, "currency_code": ccy,
                      "invoice_date": "2026-10-01", "payment_term": {"due_date": "2026-11-20"}},
           "amount": money(total, ccy), "due_amount": money(max(total-paid, 0) if due is None else due, ccy),
           "primary_recipients": [{"billing_info": {"email_address": email,
                                    "name": {"full_name": "Pat Example"}}}],
           "configuration": {"partial_payment": {"allow_partial_payment": True}}}
    if paid or payments is not None:
        raw["payments"] = {"paid_amount": money(paid, ccy)}
        if payments is not None:
            raw["payments"]["transactions"] = payments
    if refunds is not None:
        raw["refunds"] = refunds
    return parse_invoice(raw)

def txn(tid, cents, *, ccy="USD", code="T0006", status="S", hour=0,
        invoice_ref="INV-1", ref=None, fee=0, fee_ccy=None, email="pat@example.com",
        at=None, updated=None):
    ti = {"transaction_id": tid, "transaction_event_code": code, "transaction_status": status,
          "transaction_amount": money(cents, ccy),
          "transaction_initiation_date": (at or (T0 + timedelta(hours=hour))).isoformat()}
    if updated:
        ti["transaction_updated_date"] = updated.isoformat()
    if invoice_ref:
        ti["invoice_id"] = invoice_ref
    if ref:
        ti["paypal_reference_id"] = ref
        ti["paypal_reference_id_type"] = "TXN"
    if fee:
        ti["fee_amount"] = money(fee, fee_ccy or ccy)
    return parse_transaction({"transaction_info": ti, "payer_info": {"email_address": email}})

def run(invoices, txns):
    r = reconcile(invoices, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    p = propose(r, invoices, txns, as_of=AS_OF)
    j = journal(r, txns)
    return r, p, j

def balance(lines, account, ccy=None):
    return sum((D(l["debit"] or 0) - D(l["credit"] or 0)
                for l in lines if l["account"] == account and (ccy is None or l["currency"] == ccy)), D(0))

def freeze(r, p, j):
    import dataclasses, json
    def canonical(items):
        return sorted(json.dumps(x, sort_keys=True, default=str) for x in items)
    return (canonical(dataclasses.asdict(x) for x in r.transactions),
            canonical(dataclasses.asdict(x) for x in r.invoices),
            canonical(dataclasses.asdict(x) for x in p), canonical(j))
