"""Parse PayPal REST responses into models. Shared by the fixture and sandbox clients.

Shapes: docs/paypal-api-notes.md (Transaction Search v1 `transaction_detail`,
Invoicing v2 `invoice`). Unknown or missing optional fields are tolerated.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from ..models import Invoice, Money, Transaction


def parse_money(m: dict | None) -> Money | None:
    if not m or m.get("value") in (None, ""):
        return None
    return Money(Decimal(str(m["value"])), m["currency_code"])


def parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _full_name(n: dict | None) -> str | None:
    if not n:
        return None
    if n.get("full_name"):
        return n["full_name"]
    if n.get("alternate_full_name"):
        return n["alternate_full_name"]
    parts = [n.get("given_name"), n.get("surname")]
    return " ".join(p for p in parts if p) or None


def parse_transaction(d: dict) -> Transaction:
    ti = d.get("transaction_info", {})
    pi = d.get("payer_info") or {}
    cart = d.get("cart_info") or {}
    amount = parse_money(ti.get("transaction_amount"))
    if amount is None:
        raise ValueError(f"transaction {ti.get('transaction_id')} has no transaction_amount")
    return Transaction(
        id=ti["transaction_id"],
        event_code=ti.get("transaction_event_code", ""),
        status=ti.get("transaction_status", ""),
        initiated=parse_datetime(ti.get("transaction_initiation_date")),
        amount=amount,
        fee=parse_money(ti.get("fee_amount")),
        payer_email=pi.get("email_address"),
        payer_name=_full_name(pi.get("payer_name")),
        invoice_ref=ti.get("invoice_id") or None,
        paypal_invoice_id=cart.get("paypal_invoice_id") or None,
        subject=ti.get("transaction_subject") or None,
        note=ti.get("transaction_note") or None,
        reference_id=ti.get("paypal_reference_id") or None,
        items=tuple(i.get("item_name", "") for i in cart.get("item_details", []) if i.get("item_name")),
        raw=d,
    )


def parse_invoice(d: dict) -> Invoice:
    det = d.get("detail", {})
    ccy = det.get("currency_code") or d.get("amount", {}).get("currency_code")
    total = parse_money({"currency_code": ccy, "value": d.get("amount", {}).get("value")}) or Money(Decimal("0"), ccy)
    pays = d.get("payments") or {}
    refunds = d.get("refunds") or {}
    paid = parse_money(pays.get("paid_amount")) or Money(Decimal("0"), ccy)
    due = parse_money(d.get("due_amount")) or (total - paid)
    rec = (d.get("primary_recipients") or [{}])[0].get("billing_info", {})
    meta = det.get("metadata") or {}
    return Invoice(
        id=d["id"],
        number=det.get("invoice_number", ""),
        status=d.get("status", ""),
        total=total,
        due=due,
        paid=paid,
        invoice_date=parse_date(det.get("invoice_date")),
        due_date=parse_date((det.get("payment_term") or {}).get("due_date")),
        recipient_email=rec.get("email_address"),
        recipient_name=_full_name(rec.get("name")),
        recipient_business=rec.get("business_name"),
        items=tuple(i.get("name", "") for i in d.get("items", [])),
        memo=det.get("memo") or det.get("note"),
        payment_ids=[p["payment_id"] for p in pays.get("transactions", []) if p.get("payment_id")],
        refund_ids=[r["refund_id"] for r in refunds.get("transactions", []) if r.get("refund_id")],
        last_sent=parse_datetime(meta.get("last_sent_time")),
        raw=d,
    )
