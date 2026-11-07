"""Generate the Northwind Studio fixtures (deterministic; re-run to rebuild fixtures/).

Usage: python tools/make_fixtures.py

Shapes copy the official PayPal responses field for field (see docs/paypal-api-notes.md):
- fixtures/invoices.json     = GET /v2/invoicing/invoices?fields=all&total_required=true  (`invoices`)
- fixtures/transactions.json = GET /v1/reporting/transactions?fields=all            (`search_response`)
- fixtures/cases.json        = which rows test which reconciliation case, with the expected outcome

All people and businesses are fictional; every email is @example.com or a subdomain of it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api-m.sandbox.paypal.com"
WINDOW_START = "2026-10-08T00:00:00+0000"
WINDOW_END = "2026-11-07T00:00:00+0000"
MERCHANT_ACCOUNT = "NWSTUDIO8Q2LK"

FEE = {"USD": (Decimal("0.0349"), Decimal("0.49")), "EUR": (Decimal("0.0349"), Decimal("0.39"))}


def _h(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest().upper()[:n]


def txn_id(seed: str) -> str:
    return _h("txn:" + seed, 17)


def inv_id(number: str) -> str:
    h = _h("inv:" + number, 16)
    return "INV2-" + "-".join(h[i : i + 4] for i in range(0, 16, 4))


def money(value, ccy="USD") -> dict:
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"currency_code": ccy, "value": f"{d:.2f}"}


def fee_for(amount: Decimal, ccy: str) -> Decimal:
    pct, fixed = FEE[ccy]
    return (amount * pct + fixed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Customer:
    key: str
    business: str
    given: str
    surname: str
    email: str
    country: str = "US"


C = {c.key: c for c in [
    Customer("harbor", "Harbor Lane Coffee", "Dana", "Whitfield", "dana@harborlane.example.com"),
    Customer("pinecrest", "Pinecrest Dental", "Omar", "Reyes", "billing@pinecrest-dental.example.com"),
    Customer("juniper", "Juniper & Sage Florists", "Priya", "Nair", "priya@junipersage.example.com"),
    Customer("brightside", "Brightside Tutoring", "Leo", "Martins", "leo@brightside.example.com"),
    Customer("copperfield", "Copperfield Bikes", "Hana", "Sato", "hana@copperfield.example.com"),
    Customer("oakmont", "Oakmont Realty", "Grace", "Kim", "accounts@oakmont.example.com"),
    Customer("lumen", "Lumen Bakery", "Maria", "Lumen", "ap@lumen-bakery.example.com"),
    Customer("northshore", "Northshore Yoga", "Ben", "Carter", "ben@northshore-yoga.example.com"),
    Customer("vogel", "Atelier Vogel", "Klara", "Vogel", "klara@atelier-vogel.example.com", "DE"),
    Customer("durand", "Maison Durand", "Luc", "Durand", "luc@maison-durand.example.com", "FR"),
    Customer("redwood", "Redwood Legal", "Sam", "Ortiz", "finance@redwood-legal.example.com"),
    Customer("fieldstone", "Fieldstone Farms", "Ana", "Lopez", "ana@fieldstone.example.com"),
    Customer("tidewater", "Tidewater Kayaks", "Jon", "Park", "jon@tidewater.example.com"),
]}


@dataclass
class Inv:
    number: str
    cust: str
    amount: str
    item: str
    invoice_date: str
    due_date: str
    status: str
    ccy: str = "USD"
    paid_by: list = field(default_factory=list)  # txn seeds recorded on the invoice by PayPal
    refunded_by: list = field(default_factory=list)
    allow_partial: bool = False
    reminders: int = 0


@dataclass
class Txn:
    seed: str
    when: str
    amount: str
    ccy: str = "USD"
    payer_email: str | None = None
    payer_given: str = ""
    payer_surname: str = ""
    payer_country: str = "US"
    code: str = "T0006"
    status: str = "S"
    invoice_ref: str | None = None  # transaction_info.invoice_id
    paypal_invoice: str | None = None  # cart_info.paypal_invoice_id (invoice number -> INV2 id)
    subject: str | None = None
    note: str | None = None
    ref_seed: str | None = None  # paypal_reference_id (TXN) for refunds
    fee: bool = True
    item: str | None = None


def build():
    invs: list[Inv] = []
    txns: list[Txn] = []

    def payer(c: Customer) -> dict:
        return {"payer_email": c.email, "payer_given": c.given, "payer_surname": c.surname, "payer_country": c.country}

    # --- clean matches paid through PayPal Invoicing (rule 1: invoice reference) ---
    clean = [
        ("NW-1001", "harbor", "1250.00", "Brand identity refresh", "2026-10-01", "2026-10-31", "2026-10-09T14:22:05+0000"),
        ("NW-1002", "pinecrest", "860.00", "Website care plan, Q4", "2026-10-02", "2026-11-01", "2026-10-10T09:41:37+0000"),
        ("NW-1003", "juniper", "425.00", "Spring catalogue layout", "2026-10-05", "2026-11-04", "2026-10-12T17:03:11+0000"),
        ("NW-1004", "brightside", "1980.00", "Learning portal UI design", "2026-10-06", "2026-11-05", "2026-10-14T11:15:49+0000"),
        ("NW-1005", "copperfield", "640.00", "Product photo retouching (40 images)", "2026-10-08", "2026-11-07", "2026-10-16T08:58:20+0000"),
        ("NW-1006", "oakmont", "2750.00", "Listing site redesign, milestone 1", "2026-10-09", "2026-11-08", "2026-10-19T15:30:02+0000"),
        ("NW-1007", "northshore", "390.00", "Class schedule poster series", "2026-10-12", "2026-11-11", "2026-10-21T10:12:44+0000"),
        ("NW-1008", "redwood", "3200.00", "Pitch deck and report templates", "2026-10-14", "2026-11-13", "2026-10-24T13:47:31+0000"),
        ("NW-1009", "fieldstone", "720.00", "Farm stand signage", "2026-10-19", "2026-11-18", "2026-10-28T16:05:58+0000"),
        ("NW-1010", "tidewater", "1100.00", "Booking flow UX audit", "2026-10-22", "2026-11-21", "2026-11-02T12:20:13+0000"),
    ]
    for num, ck, amt, item, d, due, when in clean:
        invs.append(Inv(num, ck, amt, item, d, due, "PAID", paid_by=[num]))
        txns.append(Txn(num, when, amt, invoice_ref=num, paypal_invoice=num, item=item, **payer(C[ck])))

    # --- clean matches by exact amount + payer email (rule 2), paid with Send Money ---
    invs.append(Inv("NW-1011", "pinecrest", "315.00", "Appointment card reprint", "2026-10-15", "2026-11-14", "SENT"))
    txns.append(Txn("NW-1011", "2026-10-25T09:02:17+0000", "315.00", code="T0000", note="thanks!", **payer(C["pinecrest"])))
    invs.append(Inv("NW-1012", "copperfield", "980.00", "Spring campaign social kit", "2026-10-20", "2026-11-19", "SENT"))
    txns.append(Txn("NW-1012", "2026-10-30T18:44:09+0000", "980.00", code="T0000", subject="Social kit", **payer(C["copperfield"])))

    # --- partial payment ---
    invs.append(Inv("NW-1013", "oakmont", "1200.00", "Agent headshot session edit", "2026-10-13", "2026-11-12",
                    "PARTIALLY_PAID", paid_by=["NW-1013"], allow_partial=True))
    txns.append(Txn("NW-1013", "2026-10-23T14:10:36+0000", "600.00", invoice_ref="NW-1013", paypal_invoice="NW-1013",
                    item="Agent headshot session edit", **payer(C["oakmont"])))

    # --- overpayment (Send Money with the invoice number in the note) ---
    invs.append(Inv("NW-1014", "juniper", "450.00", "Wedding season flyer set", "2026-10-16", "2026-11-15", "SENT"))
    txns.append(Txn("NW-1014", "2026-10-27T10:31:52+0000", "495.00", code="T0000",
                    note="Payment for NW-1014 plus a little extra for the rush, thank you", **payer(C["juniper"])))

    # --- duplicate payment (double checkout; the second capture is a refund candidate) ---
    invs.append(Inv("NW-1015", "brightside", "800.00", "Tutor onboarding illustrations", "2026-10-18", "2026-11-17",
                    "PAID", paid_by=["NW-1015"]))
    txns.append(Txn("NW-1015", "2026-10-29T19:05:03+0000", "800.00", invoice_ref="NW-1015", paypal_invoice="NW-1015",
                    item="Tutor onboarding illustrations", **payer(C["brightside"])))
    txns.append(Txn("NW-1015-dup", "2026-10-29T19:06:41+0000", "800.00", invoice_ref="NW-1015",
                    paypal_invoice="NW-1015", item="Tutor onboarding illustrations", **payer(C["brightside"])))

    # --- overdue unpaid invoices (reminder candidates) ---
    invs.append(Inv("NW-1016", "redwood", "2400.00", "Annual report design", "2026-09-25", "2026-10-25", "SENT", reminders=1))
    invs.append(Inv("NW-1017", "tidewater", "350.00", "Rental waiver form layout", "2026-10-01", "2026-10-31", "SENT"))

    # --- open, not yet due (no action expected) ---
    invs.append(Inv("NW-1018", "harbor", "680.00", "Holiday menu boards", "2026-11-02", "2026-12-02", "SENT"))
    invs.append(Inv("NW-1019", "fieldstone", "1250.00", "CSA box packaging design", "2026-11-04", "2026-12-04", "SENT"))

    # --- fee-only difference (payer sent the invoice total minus PayPal's fee) ---
    invs.append(Inv("NW-1020", "northshore", "500.00", "October class pass artwork", "2026-10-17", "2026-11-16", "SENT"))
    net = Decimal("500.00") - fee_for(Decimal("500.00"), "USD")
    txns.append(Txn("NW-1020", "2026-10-31T07:55:26+0000", f"{net:.2f}", code="T0000", note="October artwork",
                    **payer(C["northshore"])))

    # --- payer email differs from the invoice recipient (owner pays from a personal account) ---
    invs.append(Inv("NW-1021", "lumen", "1500.00", "Website retainer, October", "2026-10-10", "2026-11-09", "SENT"))
    txns.append(Txn("NW-1021", "2026-11-03T08:27:40+0000", "1500.00", code="T0000",
                    note="Lumen Bakery - October website retainer", payer_email="maria.lumen@example.com",
                    payer_given="Maria", payer_surname="Lumen"))

    # --- currencies: a clean EUR match, and a USD invoice paid in EUR ---
    invs.append(Inv("NW-1022", "vogel", "900.00", "Exhibition catalogue design", "2026-10-07", "2026-11-06", "PAID",
                    ccy="EUR", paid_by=["NW-1022"]))
    txns.append(Txn("NW-1022", "2026-10-20T13:14:22+0000", "900.00", ccy="EUR", invoice_ref="NW-1022",
                    paypal_invoice="NW-1022", item="Exhibition catalogue design", **payer(C["vogel"])))
    invs.append(Inv("NW-1023", "durand", "1000.00", "Packaging mock-ups", "2026-10-11", "2026-11-10", "SENT"))
    txns.append(Txn("NW-1023", "2026-11-01T15:38:10+0000", "920.00", ccy="EUR", code="T0000",
                    note="Maison Durand NW-1023", **payer(C["durand"])))

    # --- refunded transaction (project cancelled, full refund issued) ---
    invs.append(Inv("NW-1024", "juniper", "300.00", "Valentine's promo banner", "2026-10-03", "2026-11-02", "REFUNDED",
                    paid_by=["NW-1024"], refunded_by=["NW-1024-refund"]))
    txns.append(Txn("NW-1024", "2026-10-11T10:02:55+0000", "300.00", status="V", invoice_ref="NW-1024",
                    paypal_invoice="NW-1024", item="Valentine's promo banner", **payer(C["juniper"])))
    txns.append(Txn("NW-1024-refund", "2026-10-15T09:20:31+0000", "-300.00", code="T1107", invoice_ref="NW-1024",
                    ref_seed="NW-1024", fee=False, **payer(C["juniper"])))

    # --- pending transaction (eCheck not yet cleared) ---
    invs.append(Inv("NW-1025", "oakmont", "650.00", "Open house flyer pack", "2026-10-26", "2026-11-25",
                    "PAYMENT_PENDING"))
    txns.append(Txn("NW-1025", "2026-11-05T11:48:19+0000", "650.00", status="P", invoice_ref="NW-1025",
                    paypal_invoice="NW-1025", item="Open house flyer pack", **payer(C["oakmont"])))

    # --- a draft that was never sent (ignored by matching) ---
    invs.append(Inv("NW-1026", "brightside", "540.00", "Parent newsletter template", "2026-11-06", "2026-12-06", "DRAFT"))

    # --- payments with no invoice (draft-invoice candidates) ---
    txns.append(Txn("unbilled-ravi", "2026-10-26T16:22:48+0000", "275.00", code="T0000", subject="Logo refresh deposit",
                    note="Deposit for the logo refresh we discussed on the call", payer_email="ravi.menon@example.com",
                    payer_given="Ravi", payer_surname="Menon"))
    txns.append(Txn("unbilled-ella", "2026-11-04T20:11:05+0000", "49.00", code="T0000", subject="Stock photo license",
                    payer_email="ella.brooks@example.com", payer_given="Ella", payer_surname="Brooks"))

    # --- non-sales: withdrawal to the bank (ignored) ---
    txns.append(Txn("withdrawal-1", "2026-10-30T06:00:00+0000", "-5000.00", code="T0400", fee=False))

    return invs, txns


def _txn_json(t: Txn) -> dict:
    amt = Decimal(t.amount)
    info = {
        "paypal_account_id": _h("acct:" + (t.payer_email or "bank"), 13),
        "transaction_id": txn_id(t.seed),
        "transaction_event_code": t.code,
        "transaction_initiation_date": t.when,
        "transaction_updated_date": t.when,
        "transaction_amount": money(amt, t.ccy),
        "transaction_status": t.status,
        "protection_eligibility": "01" if amt > 0 else "02",
    }
    if t.fee and amt > 0:
        info["fee_amount"] = money(-fee_for(amt, t.ccy), t.ccy)
    if t.ref_seed:
        info["paypal_reference_id"] = txn_id(t.ref_seed)
        info["paypal_reference_id_type"] = "TXN"
    if t.invoice_ref:
        info["invoice_id"] = t.invoice_ref
    if t.subject:
        info["transaction_subject"] = t.subject
    if t.note:
        info["transaction_note"] = t.note
    if t.code != "T0400":
        info["instrument_type"] = "PAYPAL"
    detail: dict = {"transaction_info": info}
    if t.payer_email:
        detail["payer_info"] = {
            "account_id": info["paypal_account_id"],
            "email_address": t.payer_email,
            "address_status": "Y",
            "payer_status": "Y",
            "payer_name": {"given_name": t.payer_given, "surname": t.payer_surname,
                           "alternate_full_name": f"{t.payer_given} {t.payer_surname}"},
            "country_code": t.payer_country,
        }
    else:
        detail["payer_info"] = {}
    cart: dict = {}
    if t.item:
        cart["item_details"] = [{
            "item_name": t.item,
            "item_quantity": "1",
            "item_unit_price": money(amt, t.ccy),
            "item_amount": money(amt, t.ccy),
            "total_item_amount": money(amt, t.ccy),
            "invoice_number": t.invoice_ref or "",
        }]
        cart["tax_inclusive"] = False
    if t.paypal_invoice:
        cart["paypal_invoice_id"] = inv_id(t.paypal_invoice)
    detail["cart_info"] = cart
    detail["store_info"] = {}
    detail["auction_info"] = {}
    detail["incentive_info"] = {}
    return detail


def _inv_json(i: Inv, txn_by_seed: dict[str, Txn]) -> dict:
    c = C[i.cust]
    iid = inv_id(i.number)
    total = Decimal(i.amount)
    paid = sum((Decimal(txn_by_seed[s].amount) for s in i.paid_by), Decimal("0"))
    refunded = sum((-Decimal(txn_by_seed[s].amount) for s in i.refunded_by), Decimal("0"))
    sent = i.status != "DRAFT"
    meta = {
        "create_time": f"{i.invoice_date}T08:00:00Z",
        "last_update_time": f"{i.invoice_date}T08:00:00Z",
        "created_by_flow": "REGULAR_SINGLE",
        "invoicer_view_url": f"https://www.sandbox.paypal.com/invoice/details/{iid}",
    }
    if sent:
        meta["first_sent_time"] = f"{i.invoice_date}T08:05:00Z"
        meta["last_sent_time"] = f"{i.invoice_date}T08:05:00Z" if not i.reminders else "2026-10-28T09:00:00Z"
        meta["recipient_view_url"] = f"https://www.sandbox.paypal.com/invoice/p/#{iid}"
    inv = {
        "id": iid,
        "status": i.status,
        "detail": {
            "invoice_number": i.number,
            "reference": f"Project {i.number[3:]}",
            "invoice_date": i.invoice_date,
            "currency_code": i.ccy,
            "note": "Thank you for working with Northwind Studio.",
            "terms_and_conditions": "Payment due within 30 days.",
            "payment_term": {"term_type": "NET_30", "due_date": i.due_date},
            "metadata": meta,
        },
        "invoicer": {
            "name": {"given_name": "Ada", "surname": "Norberg"},
            "business_name": "Northwind Studio",
            "email_address": "billing@northwind-studio.example.com",
            "website": "https://northwind-studio.example.com",
        },
        "primary_recipients": [{
            "billing_info": {
                "name": {"given_name": c.given, "surname": c.surname, "full_name": f"{c.given} {c.surname}"},
                "business_name": c.business,
                "email_address": c.email,
                "language": "en-US",
            }
        }],
        "items": [{
            "id": _h("item:" + i.number, 12),
            "name": i.item,
            "quantity": "1",
            "unit_amount": money(total, i.ccy),
            "unit_of_measure": "AMOUNT",
        }],
        "configuration": {
            "partial_payment": {"allow_partial_payment": i.allow_partial},
            "allow_tip": False,
            "tax_inclusive": False,
        },
        "amount": {"currency_code": i.ccy, "value": f"{total:.2f}",
                   "breakdown": {"item_total": money(total, i.ccy)}},
        "due_amount": money(max(total - paid, Decimal("0")) if i.status != "REFUNDED" else Decimal("0"), i.ccy),
        "links": [{"href": f"{API}/v2/invoicing/invoices/{iid}", "rel": "self", "method": "GET"}],
    }
    if i.paid_by:
        inv["payments"] = {
            "paid_amount": money(paid, i.ccy),
            "transactions": [{
                "payment_id": txn_id(s),
                "payment_date": txn_by_seed[s].when[:10],
                "type": "PAYPAL",
                "method": "PAYPAL",
                "amount": money(txn_by_seed[s].amount, i.ccy),
            } for s in i.paid_by],
        }
    if i.refunded_by:
        inv["refunds"] = {
            "refund_amount": money(refunded, i.ccy),
            "transactions": [{
                "refund_id": txn_id(s),
                "refund_date": txn_by_seed[s].when[:10],
                "type": "PAYPAL",
                "method": "PAYPAL",
                "amount": money(-Decimal(txn_by_seed[s].amount), i.ccy),
            } for s in i.refunded_by],
        }
    return inv


def cases() -> dict:
    T = txn_id
    clean = [f"NW-10{n:02d}" for n in range(1, 13)]
    return {
        "clean_match": {
            "description": "Payment clearly belongs to one open invoice. NW-1001..1010 carry the invoice number "
                           "(rule 1); NW-1011..1012 match on exact amount plus payer email (rule 2).",
            "invoices": clean, "transactions": [T(n) for n in clean],
            "expected": {T(n): "matched" for n in clean},
        },
        "partial_payment": {
            "description": "Half of a 1200.00 invoice paid; 600.00 still due.",
            "invoices": ["NW-1013"], "transactions": [T("NW-1013")],
            "expected": {T("NW-1013"): "partial"},
        },
        "overpayment": {
            "description": "495.00 received for a 450.00 invoice (rush tip in the note).",
            "invoices": ["NW-1014"], "transactions": [T("NW-1014")],
            "expected": {T("NW-1014"): "overpaid"},
        },
        "duplicate_payment": {
            "description": "Double checkout: two 800.00 captures 98 seconds apart for one invoice. "
                           "The second is the refund candidate.",
            "invoices": ["NW-1015"], "transactions": [T("NW-1015"), T("NW-1015-dup")],
            "expected": {T("NW-1015"): "matched", T("NW-1015-dup"): "duplicate"},
            "proposed_action": {"type": "refund_capture", "transaction": T("NW-1015-dup")},
        },
        "payment_without_invoice": {
            "description": "Money received with no invoice behind it (a deposit and a small licence sale).",
            "invoices": [], "transactions": [T("unbilled-ravi"), T("unbilled-ella")],
            "expected": {T("unbilled-ravi"): "unmatched", T("unbilled-ella"): "unmatched"},
            "proposed_action": {"type": "create_draft_invoice", "transaction": T("unbilled-ravi")},
        },
        "overdue_unpaid": {
            "description": "Sent invoices past their due date (as of 2026-11-07) with no payment.",
            "invoices": ["NW-1016", "NW-1017"], "transactions": [],
            "expected": {"NW-1016": "overdue", "NW-1017": "overdue"},
            "proposed_action": {"type": "send_reminder", "invoice": "NW-1016"},
        },
        "fee_only_difference": {
            "description": "Payer sent 500.00 minus PayPal's fee (482.06); difference equals the fee.",
            "invoices": ["NW-1020"], "transactions": [T("NW-1020")],
            "expected": {T("NW-1020"): "fee_mismatch"},
        },
        "payer_email_differs": {
            "description": "Invoice went to ap@lumen-bakery; the owner paid from a personal address. "
                           "Only the memo ('Lumen Bakery - October website retainer') links them, so the AI resolves it.",
            "invoices": ["NW-1021"], "transactions": [T("NW-1021")],
            "expected": {T("NW-1021"): "matched"},
            "resolved_by": "ai",
        },
        "different_currency": {
            "description": "NW-1022 is a clean EUR match. NW-1023 is a USD invoice paid in EUR (920.00 EUR "
                           "for 1000.00 USD), so it is flagged rather than matched.",
            "invoices": ["NW-1022", "NW-1023"], "transactions": [T("NW-1022"), T("NW-1023")],
            "expected": {T("NW-1022"): "matched", T("NW-1023"): "currency_mismatch"},
        },
        "refunded_transaction": {
            "description": "Paid, then fully refunded (T1107 row points back to the original; original status V).",
            "invoices": ["NW-1024"], "transactions": [T("NW-1024"), T("NW-1024-refund")],
            "expected": {T("NW-1024"): "refunded", T("NW-1024-refund"): "refund"},
        },
        "pending_transaction": {
            "description": "eCheck not cleared yet (status P); not counted as paid.",
            "invoices": ["NW-1025"], "transactions": [T("NW-1025")],
            "expected": {T("NW-1025"): "pending"},
        },
        "open_not_due": {
            "description": "Sent invoices that are not due yet; no action.",
            "invoices": ["NW-1018", "NW-1019"], "transactions": [],
            "expected": {"NW-1018": "open", "NW-1019": "open"},
        },
        "draft_invoice": {
            "description": "Never sent; ignored by matching and collections.",
            "invoices": ["NW-1026"], "transactions": [],
            "expected": {"NW-1026": "draft"},
        },
        "non_sales_transaction": {
            "description": "Withdrawal to the bank (T0400); not a customer payment, ignored.",
            "invoices": [], "transactions": [T("withdrawal-1")],
            "expected": {T("withdrawal-1"): "ignored"},
        },
    }


def readme(case_map: dict) -> str:
    lines = [
        "# Fixtures: Northwind Studio (fictional)",
        "",
        "Northwind Studio is a made-up design agency paid through PayPal. Every person, business and",
        "email here is fictional (`@example.com`). Generated by `python tools/make_fixtures.py`; do not",
        "edit the JSON by hand (a test checks the generator reproduces it byte for byte).",
        "",
        "| file | real PayPal call it stands in for |",
        "|---|---|",
        "| `invoices.json` | `GET /v2/invoicing/invoices?fields=all&total_required=true` (`invoices` schema) |",
        "| `transactions.json` | `GET /v1/reporting/transactions?fields=all` (`search_response` schema), 2026-10-08 to 2026-11-07 |",
        "| `cases.json` | test oracle: which rows test which case and the expected outcome |",
        "",
        "Shapes copy PayPal's official OpenAPI specs field for field; `tests/test_fixtures.py` walks every",
        "key against `tests/spec_keys.json` (extracted from the specs by `tools/extract_spec_keys.py`).",
        "Amounts are strings; fees are negative; \"today\" is 2026-11-07.",
        "",
        "## Which row tests which case",
        "",
        "| case | invoices | transactions | expected |",
        "|---|---|---|---|",
    ]
    for name, c in case_map.items():
        inv = ", ".join(c["invoices"]) or "-"
        tx = ", ".join(c["transactions"]) or "-"
        exp = ", ".join(sorted(set(c["expected"].values())))
        lines.append(f"| `{name}` | {inv} | {tx} | {exp} |")
    lines += ["", "## What each case means", ""]
    for name, c in case_map.items():
        extra = ""
        if "proposed_action" in c:
            extra = f" Expected agent proposal: `{c['proposed_action']['type']}`."
        if c.get("resolved_by") == "ai":
            extra += " Deterministic rules leave it unmatched; the AI layer resolves it."
        lines.append(f"- `{name}`: {c['description']}{extra}")
    return "\n".join(lines) + "\n"


def write_all(dest: Path) -> None:
    invs, txns = build()
    by_seed = {t.seed: t for t in txns}
    txns_sorted = sorted(txns, key=lambda t: t.when)
    invoices = {
        "total_pages": 1,
        "total_items": len(invs),
        "items": [_inv_json(i, by_seed) for i in invs],
        "links": [{"href": f"{API}/v2/invoicing/invoices?page=1&page_size=100&total_required=true",
                   "rel": "self", "method": "GET"}],
    }
    search = {
        "transaction_details": [_txn_json(t) for t in txns_sorted],
        "account_number": MERCHANT_ACCOUNT,
        "start_date": WINDOW_START,
        "end_date": WINDOW_END,
        "last_refreshed_datetime": "2026-11-06T23:59:59+0000",
        "page": 1,
        "total_items": len(txns),
        "total_pages": 1,
        "links": [{"href": f"{API}/v1/reporting/transactions?start_date={WINDOW_START}&end_date={WINDOW_END}"
                           "&fields=all&page_size=100&page=1", "rel": "self", "method": "GET"}],
    }
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in [("invoices.json", invoices), ("transactions.json", search), ("cases.json", cases())]:
        (dest / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    (dest / "README.md").write_text(readme(cases()))


if __name__ == "__main__":
    write_all(ROOT / "fixtures")
    print("fixtures written to", ROOT / "fixtures")
