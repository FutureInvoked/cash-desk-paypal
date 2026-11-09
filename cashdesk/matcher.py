"""Deterministic reconciliation: pair PayPal transactions with invoices.

Rules, in order (the first that applies wins):
  1. invoice_ref     the transaction names the invoice: cart_info.paypal_invoice_id equals the
                     invoice id, transaction_info.invoice_id equals its id or number, or the
                     invoice number appears in the subject or note.
  2. amount_email    exact amount and currency, and the payer email equals the invoice recipient,
                     against exactly one open invoice.
  3. fee_tolerance   payer email equals the recipient and the shortfall is no bigger than PayPal's
                     fee on the invoice total (the payer deducted the fee), against exactly one open invoice.
Anything else stays `unmatched`, with its candidate invoices, for the AI layer (E2).

Every result carries `evidence`: the PayPal field paths and values the decision used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .models import Invoice, Money, Transaction

OUTCOMES = {
    # transactions
    "matched", "partial", "overpaid", "duplicate", "unmatched", "fee_mismatch",
    "currency_mismatch", "refunded", "refund", "pending", "ignored",
    # invoices
    "paid", "overdue", "open", "draft", "cancelled",
}
EXCEPTIONS = {"partial", "overpaid", "duplicate", "unmatched", "fee_mismatch", "currency_mismatch", "overdue"}

OPEN_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING", "SCHEDULED"}
PAID_STATUSES = {"PAID", "MARKED_AS_PAID", "PAID_EXTERNAL"}
REFUND_CODES = {"T1107", "T1106"}  # refund, payment reversal (docs/paypal-api-notes.md)
FEE_RATE = {"USD": (Decimal("0.0349"), Decimal("0.49")), "EUR": (Decimal("0.0349"), Decimal("0.39"))}
FEE_SLACK = Decimal("0.05")
DUPLICATE_WINDOW = timedelta(hours=24)

R = "invoice.primary_recipients[0].billing_info.email_address"


def paypal_fee(amount: Money) -> Money:
    pct, fixed = FEE_RATE.get(amount.currency, FEE_RATE["USD"])
    return Money((amount.amount * pct + fixed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), amount.currency)


def is_payment(t: Transaction) -> bool:
    """Customer payment received: a T00xx event with a positive gross."""
    return t.event_code.startswith("T00") and t.amount.amount > 0


@dataclass(frozen=True)
class Link:
    """A suggested payment->invoice link from outside the rules (the AI layer)."""
    invoice_number: str
    rule: str  # "ai" or "memo_heuristic"
    resolved_by: str  # "ai" or "fallback"
    confidence: float
    evidence: dict


@dataclass
class TxnResult:
    txn_id: str
    event_code: str
    outcome: str
    rule: str | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    difference: Money | None = None  # payment minus what was due (negative = short)
    duplicate_of: str | None = None
    candidates: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    # filled by the AI layer (cashdesk.ai)
    resolved_by: str = "rules"  # rules | ai | fallback
    confidence: float = 1.0
    needs_review: bool = False
    reason: str = ""
    reason_source: str = ""  # ai | rules


@dataclass
class InvoiceResult:
    invoice_id: str
    invoice_number: str
    outcome: str
    status: str
    total: Money
    received: Money
    outstanding: Money
    days_overdue: int = 0
    transactions: list[str] = field(default_factory=list)
    last_sent: str | None = None
    reason: str = ""
    reason_source: str = ""


@dataclass
class MatchResult:
    transactions: list[TxnResult]
    invoices: list[InvoiceResult]
    ai_status: str = "not run"

    def txn(self, txn_id: str, event_code: str | None = None) -> TxnResult:
        for r in self.transactions:
            if r.txn_id == txn_id and (event_code is None or r.event_code == event_code):
                return r
        raise KeyError(txn_id)

    def invoice_by_number(self, number: str) -> InvoiceResult:
        return next(r for r in self.invoices if r.invoice_number == number)

    def summary(self) -> dict:
        by: dict[str, int] = {}
        for r in self.transactions:
            by[r.outcome] = by.get(r.outcome, 0) + 1
        inv_by: dict[str, int] = {}
        for r in self.invoices:
            inv_by[r.outcome] = inv_by.get(r.outcome, 0) + 1
        exceptions = sum(1 for r in self.transactions + self.invoices if r.outcome in EXCEPTIONS)
        return {"transactions": len(self.transactions), "invoices": len(self.invoices),
                "by_outcome": by, "invoices_by_outcome": inv_by, "exceptions": exceptions}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


NOTE_KEY = "invoice.payments.transactions[].note"


def _recorded_on(t: Transaction, invoices: list[Invoice]) -> tuple[Invoice | None, dict]:
    """A payment recorded on an invoice (e.g. by Cash Desk's record_payment) whose note names this
    PayPal transaction id as a whole token. Found on the sandbox: the invoice is then MARKED_AS_PAID."""
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(t.id)}(?![A-Za-z0-9])")
    hits = []
    for inv in invoices:
        for p in (inv.raw.get("payments") or {}).get("transactions", []) or []:
            note = p.get("note") or ""
            if pat.search(note):
                hits.append((inv, note))
    if len(hits) == 1:
        inv, note = hits[0]
        return inv, {NOTE_KEY: note, "invoice.detail.invoice_number": inv.number}
    return None, {}


def _find_by_reference(t: Transaction, invoices: list[Invoice]) -> tuple[Invoice | None, dict]:
    inv, ev = _recorded_on(t, invoices)
    if inv:
        return inv, ev
    by_id = {i.id: i for i in invoices}
    by_num = {i.number: i for i in invoices if i.number}
    if t.paypal_invoice_id and t.paypal_invoice_id in by_id:
        inv = by_id[t.paypal_invoice_id]
        ev = {"cart_info.paypal_invoice_id": t.paypal_invoice_id, "invoice.id": inv.id}
        if t.invoice_ref:
            ev["transaction_info.invoice_id"] = t.invoice_ref
        return inv, ev | {"invoice.detail.invoice_number": inv.number}
    if t.invoice_ref:
        inv = by_id.get(t.invoice_ref) or by_num.get(t.invoice_ref)
        if inv:
            return inv, {"transaction_info.invoice_id": t.invoice_ref, "invoice.detail.invoice_number": inv.number}
    for path, text in (("transaction_info.transaction_subject", t.subject), ("transaction_info.transaction_note", t.note)):
        if not text:
            continue
        hits = [i for n, i in by_num.items() if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![0-9])", text, re.I)]
        if len(hits) == 1:
            return hits[0], {path: text, "invoice.detail.invoice_number": hits[0].number}
    return None, {}


def _earlier(inv: Invoice, local_ids: set[str]) -> Decimal:
    """What PayPal says was paid on the invoice that this transaction set doesn't show (a payment
    older than the search window, or one recorded outside PayPal), net of refunds likewise unseen.
    An entry is seen when its payment/refund id is a local transaction id or its note names one."""
    def seen(entry: dict, id_key: str) -> bool:
        if entry.get(id_key) in local_ids:
            return True
        note = entry.get("note") or ""
        return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", note) for i in local_ids if i in note)

    def unseen(block: dict | None, total_key: str, id_key: str) -> Decimal:
        block = block if isinstance(block, dict) else {}
        listed = [e for e in (block.get("transactions") or []) if isinstance(e, dict)]
        amt = lambda m: Decimal(str((m or {}).get("value") or "0")) if (m or {}).get("currency_code") in (None, inv.total.currency) else Decimal("0")
        total = amt(block.get(total_key))
        listed_sum = sum((amt(e.get("amount")) for e in listed), Decimal("0"))
        # an amount PayPal reports without listing it, plus listed entries we don't have locally
        return max(total - listed_sum, Decimal("0")) + sum((amt(e.get("amount")) for e in listed if not seen(e, id_key)),
                                                           Decimal("0"))
    try:
        return max(unseen(inv.raw.get("payments"), "paid_amount", "payment_id")
                   - unseen(inv.raw.get("refunds"), "refund_amount", "refund_id"), Decimal("0"))
    except ArithmeticError:
        return Decimal("0")


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def match(invoices: list[Invoice], transactions: list[Transaction], *, as_of: date,
          links: dict[str, Link] | None = None) -> MatchResult:
    """Run the rules. `links` are AI/heuristic suggestions applied only to payments the rules
    left unmatched, and only to open invoices in the same currency."""
    links = links or {}
    txns = sorted(transactions, key=lambda t: (t.initiated, t.id))
    results: dict[tuple, TxnResult] = {}
    # payments that count toward an invoice, in time order: invoice id -> [(txn, result)]
    applied: dict[str, list[tuple[Transaction, TxnResult]]] = {}
    pending_for: dict[str, list[str]] = {}
    refunded_for: dict[str, list[str]] = {}
    by_id = {i.id: i for i in invoices}
    local_ids = {t.id for t in transactions}
    earlier = {i.id: _earlier(i, local_ids) for i in invoices}  # paid before this window, per PayPal

    def res(t: Transaction, outcome: str, **kw) -> TxnResult:
        r = TxnResult(t.id, t.event_code, outcome, **kw)
        results[t.key] = r
        return r

    def base_ev(t: Transaction) -> dict:
        ev = {"transaction_info.transaction_id": t.id, "transaction_info.transaction_amount": str(t.amount),
              "transaction_info.transaction_status": t.status,
              "transaction_info.transaction_event_code": t.event_code}
        if t.payer_email:
            ev["payer_info.email_address"] = t.payer_email
        return ev

    # ---- pass 1: non-payments, refunds, pending, reversed, and rule 1 ----
    unresolved: list[Transaction] = []
    for t in txns:
        ev = base_ev(t)
        if t.event_code in REFUND_CODES:
            inv, rev = _find_by_reference(t, invoices)
            kind = {"S": "refund", "P": "pending"}.get(t.status, "ignored")  # denied/reversed refunds moved no money
            r = res(t, kind, rule="invoice_ref" if inv else None, evidence=ev | rev |
                    ({"transaction_info.paypal_reference_id": t.reference_id} if t.reference_id else {}))
            if inv:
                r.invoice_id, r.invoice_number = inv.id, inv.number
            continue
        if not is_payment(t) or t.status == "D":
            res(t, "ignored", evidence=ev)
            continue
        inv, rev = _find_by_reference(t, invoices)
        if t.status in ("P", "V"):
            outcome = "pending" if t.status == "P" else "refunded"
            r = res(t, outcome, rule="invoice_ref" if inv else None, evidence=ev | rev)
            if inv:
                r.invoice_id, r.invoice_number = inv.id, inv.number
                (pending_for if t.status == "P" else refunded_for).setdefault(inv.id, []).append(t.id)
            continue
        if inv is None:
            unresolved.append(t)
            continue
        r = res(t, "matched", rule="invoice_ref", invoice_id=inv.id, invoice_number=inv.number,
                evidence=ev | rev | {"invoice.amount": str(inv.total)})
        if t.amount.currency != inv.total.currency:
            r.outcome = "currency_mismatch"
            r.evidence["invoice.detail.currency_code"] = inv.total.currency
            continue
        applied.setdefault(inv.id, []).append((t, r))

    # ---- pass 2: rules 2 and 3 against open invoices not yet covered ----
    def covered(inv: Invoice) -> bool:
        got = earlier[inv.id] + sum((x.amount.amount for x, _ in applied.get(inv.id, [])), Decimal("0"))
        return got >= inv.total.amount

    def open_for_rules(inv: Invoice) -> bool:
        return inv.status in OPEN_STATUSES and not covered(inv)

    for t in unresolved:
        ev = base_ev(t)
        link = links.get(t.id)
        if link:
            inv = next((i for i in invoices if i.number == link.invoice_number), None)
            if inv and open_for_rules(inv) and inv.total.currency == t.amount.currency:
                r = res(t, "matched", rule=link.rule, invoice_id=inv.id, invoice_number=inv.number,
                        evidence=ev | link.evidence | {"invoice.detail.invoice_number": inv.number,
                                                       "invoice.amount": str(inv.total)},
                        resolved_by=link.resolved_by, confidence=link.confidence,
                        needs_review=link.confidence < 0.8)
                applied.setdefault(inv.id, []).append((t, r))
                continue
        same_payer = [i for i in invoices if open_for_rules(i) and _same_email(t.payer_email, i.recipient_email)
                      and i.total.currency == t.amount.currency]
        exact = [i for i in same_payer if (i.total - Money(earlier[i.id] + sum((x.amount.amount for x, _ in applied.get(i.id, [])),
                                                                              Decimal("0")), i.total.currency)) == t.amount]
        if len(exact) == 1:
            inv = exact[0]
            r = res(t, "matched", rule="amount_email", invoice_id=inv.id, invoice_number=inv.number,
                    evidence=ev | {R: inv.recipient_email, "invoice.amount": str(inv.total),
                                   "invoice.detail.invoice_number": inv.number})
            applied.setdefault(inv.id, []).append((t, r))
            continue
        if len(exact) > 1:
            res(t, "unmatched", candidates=sorted(i.number for i in exact), evidence=ev)
            continue
        within = [i for i in same_payer
                  if Decimal("0") < (i.total.amount - t.amount.amount) <= paypal_fee(i.total).amount + FEE_SLACK]
        if len(within) == 1:
            inv = within[0]
            r = res(t, "fee_mismatch", rule="fee_tolerance", invoice_id=inv.id, invoice_number=inv.number,
                    evidence=ev | {R: inv.recipient_email, "invoice.amount": str(inv.total),
                                   "invoice.detail.invoice_number": inv.number,
                                   "expected_paypal_fee": str(paypal_fee(inv.total))})
            applied.setdefault(inv.id, []).append((t, r))
            continue
        twins = [(i, x) for i in invoices if i.total.currency == t.amount.currency
                 for x, _ in applied.get(i.id, []) if x.amount == t.amount
                 and _same_email(x.payer_email, t.payer_email) and abs(x.initiated - t.initiated) <= DUPLICATE_WINDOW]
        if len({i.id for i, _ in twins}) == 1:  # same payer paid the same amount for one invoice: a duplicate pair
            inv, twin = twins[0]
            r = res(t, "matched", rule="amount_email", invoice_id=inv.id, invoice_number=inv.number,
                    evidence=ev | {"same_payer_payment.transaction_id": twin.id, "invoice.amount": str(inv.total),
                                   "invoice.detail.invoice_number": inv.number})
            applied[inv.id].append((t, r))
            continue
        res(t, "unmatched", candidates=sorted(i.number for i in within or same_payer), evidence=ev)

    # ---- refunds that point back at a payment (transaction_info.paypal_reference_id) ----
    # Applied before duplicates are decided: a refunded first payment no longer covers the invoice.
    refunded_amt: dict[str, Decimal] = {}
    refund_ids: dict[str, list[str]] = {}
    for t in txns:
        if t.event_code in REFUND_CODES and t.status == "S" and t.reference_id:
            refunded_amt[t.reference_id] = refunded_amt.get(t.reference_id, Decimal("0")) + abs(t.amount.amount)
            refund_ids.setdefault(t.reference_id, []).append(t.id)

    # ---- pass 3: per-invoice running totals, net of refunds, in time order ----
    net_less: dict[str, Decimal] = {}     # what a refund took back from a payment
    refunded_invoice: set[str] = set()    # invoices with a fully refunded payment
    for inv_id, pays in applied.items():
        pays.sort(key=lambda p: (p[0].initiated, p[0].id))
        inv = by_id[inv_id]
        total, ccy = inv.total.amount, inv.total.currency
        running = earlier[inv_id]
        for t, r in pays:
            back = min(refunded_amt.get(t.id, Decimal("0")), t.amount.amount)
            if back:
                r.evidence["refunded_by.transaction_id"] = refund_ids[t.id][-1]
            earlier_same = [x for x, xr in pays if x is not t and (x.initiated, x.id) < (t.initiated, t.id)
                            and xr.outcome in ("matched", "partial", "overpaid") and x.amount == t.amount
                            and _same_email(x.payer_email, t.payer_email)
                            and t.initiated - x.initiated <= DUPLICATE_WINDOW]
            if running >= total and earlier_same:
                r.duplicate_of = earlier_same[-1].id
                r.evidence["duplicate_of.transaction_id"] = earlier_same[-1].id
                if back >= t.amount.amount:
                    r.outcome, r.difference = "refunded", Money(Decimal("0"), ccy)
                else:
                    r.outcome, r.difference = "duplicate", Money(t.amount.amount - back, ccy)
                    if back:
                        r.evidence["refunded_amount"] = str(Money(back, ccy))
                continue
            gross_excess = min(running + t.amount.amount - total, t.amount.amount)
            if gross_excess > 0:
                r.evidence["overpaid_by"] = str(Money(gross_excess, ccy))  # the books keep the original split
            if back >= t.amount.amount:
                r.outcome, r.difference = "refunded", None
                net_less[t.id] = t.amount.amount
                refunded_invoice.add(inv_id)
                continue
            if back:
                net_less[t.id] = back
            fee_rule = r.outcome == "fee_mismatch"
            before = running
            running += t.amount.amount - back
            r.difference = Money(running - total, ccy)
            if back and gross_excess > 0 and back == gross_excess:
                r.outcome = "matched"
                r.evidence["refunded_excess"] = str(Money(back, ccy))
                continue
            if back:
                r.evidence["refunded_amount"] = str(Money(back, ccy))
            if fee_rule and not back:
                continue  # matched by the fee rule (rule 3): stays fee_mismatch
            if running == total:
                r.outcome = "matched"
            elif running < total:
                short = total - running
                r.outcome = "fee_mismatch" if before == 0 and not back and short <= paypal_fee(inv.total).amount + FEE_SLACK \
                    and not inv.raw.get("configuration", {}).get("partial_payment", {}).get("allow_partial_payment") \
                    else "partial"
            else:
                r.outcome = "overpaid"
                # this payment's own excess: an earlier overpayment's excess is not refundable from it
                r.difference = Money(min(running - total, t.amount.amount - back), ccy)

    # ---- invoices ----
    inv_results: list[InvoiceResult] = []
    for inv in sorted(invoices, key=lambda i: i.number):
        pays = [(t, r) for t, r in applied.get(inv.id, []) if r.outcome not in ("duplicate", "refunded")]
        received = Money(earlier[inv.id] + sum((t.amount.amount - net_less.get(t.id, Decimal("0")) for t, _ in pays),
                                               Decimal("0")), inv.total.currency)
        outstanding = Money(max(inv.total.amount - received.amount, Decimal("0")), inv.total.currency)
        days = (as_of - inv.due_date).days if inv.due_date else 0
        if inv.status == "DRAFT":
            outcome = "draft"
        elif inv.status in {"CANCELLED", "AUTO_CANCELLED"}:
            outcome = "cancelled"
        elif refunded_for.get(inv.id) or inv.status in {"REFUNDED", "MARKED_AS_REFUNDED"} \
                or (inv.id in refunded_invoice and not pays):
            outcome = "refunded"
        elif pays and received.amount > inv.total.amount:
            outcome = "overpaid"
        elif pays and any(r.outcome == "fee_mismatch" for _, r in pays):
            outcome = "fee_mismatch"
        elif pays and received.amount >= inv.total.amount:
            outcome = "paid"
        elif pays:
            outcome = "partial"
        elif earlier[inv.id] >= inv.total.amount and inv.status not in OPEN_STATUSES | {"DRAFT"}:
            outcome = "paid"  # paid per PayPal, payment outside this search window
        elif pending_for.get(inv.id):
            outcome = "pending"
        elif inv.status in PAID_STATUSES:
            outcome = "paid"  # paid per PayPal, payment outside this search window
        elif earlier[inv.id] > 0 and inv.status in OPEN_STATUSES:
            outcome = "partial"  # part paid per PayPal; the rest is still due
        elif inv.due_date and days > 0:
            outcome = "overdue"
        else:
            outcome = "open"
        inv_results.append(InvoiceResult(
            inv.id, inv.number, outcome, inv.status, inv.total, received, outstanding,
            days_overdue=max(days, 0) if outcome in ("overdue", "partial") else 0,
            transactions=[t.id for t, _ in applied.get(inv.id, [])],
            last_sent=inv.last_sent.date().isoformat() if inv.last_sent else None,
        ))

    ordered = [results[t.key] for t in txns]
    return MatchResult(ordered, inv_results)
