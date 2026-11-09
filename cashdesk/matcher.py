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
    status: str = ""  # transaction_status: with id and event code, identifies the row
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
    """An invoice that itself lists this transaction: as `payments.transactions[].payment_id` (PayPal's
    id of the payment) or `refunds.transactions[].refund_id`, or a recorded payment whose note names
    this PayPal transaction id as a whole token (e.g. Cash Desk's record_payment; the invoice is then
    MARKED_AS_PAID)."""
    refund = t.event_code in REFUND_CODES
    block, id_key = ("refunds", "refund_id") if refund else ("payments", "payment_id")
    by_id = [inv for inv in invoices
             if any(isinstance(e, dict) and e.get(id_key) == t.id
                    for e in ((inv.raw.get(block) or {}).get("transactions") or []))]
    if len(by_id) == 1:
        inv = by_id[0]
        return inv, {f"invoice.{block}.transactions[].{id_key}": t.id, "invoice.detail.invoice_number": inv.number}
    if refund:
        return None, {}
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(t.id)}(?![A-Za-z0-9])")
    hits = []
    for inv in invoices:
        for p in (inv.raw.get("payments") or {}).get("transactions", []) or []:
            note = (p.get("note") or "") if isinstance(p, dict) else ""
            if pat.search(note):
                hits.append((inv, note))
    if len(hits) == 1:
        inv, note = hits[0]
        return inv, {NOTE_KEY: note, "invoice.detail.invoice_number": inv.number}
    return None, {}


def _find_by_reference(t: Transaction, invoices: list[Invoice]) -> tuple[Invoice | None, dict]:
    by_id = {i.id: i for i in invoices}
    by_num = {i.number: i for i in invoices if i.number}
    inv, ev = _recorded_on(t, invoices)
    if inv:  # also cite the transaction's own reference when it names the same invoice
        if t.paypal_invoice_id == inv.id:
            ev = {"cart_info.paypal_invoice_id": t.paypal_invoice_id, "invoice.id": inv.id} | ev
        if t.invoice_ref and (by_id.get(t.invoice_ref) or by_num.get(t.invoice_ref)) is inv:
            ev = {"transaction_info.invoice_id": t.invoice_ref} | ev
        return inv, ev
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


@dataclass
class _History:
    """What PayPal's invoice record shows that this transaction set doesn't."""
    pay_unseen: Decimal = Decimal("0")   # listed payments with no local payment row
    pay_unlisted: Decimal = Decimal("0")  # paid_amount beyond the listed payments: may overlap local rows
    ref_unseen: Decimal = Decimal("0")   # listed refunds with no local refund row
    ref_unlisted: Decimal = Decimal("0")  # refund_amount beyond the listed refunds: may overlap local rows


def _history(inv: Invoice, local_pay: set[str], local_ref: set[str]) -> _History:
    """An entry is seen locally when its id is a local row of the same kind (a payment id must be a
    payment row, not a conversion row sharing the id), or, for a recorded payment, its note names a
    local payment. An amount PayPal reports without listing it is ambiguous: it is only counted
    beyond what local rows already explain (see match()), so it can never create a refundable excess."""
    ccy = inv.total.currency

    def amt(m) -> Decimal:
        m = m if isinstance(m, dict) else {}
        if m.get("currency_code") not in (None, ccy):
            return Decimal("0")
        try:
            return Decimal(str(m.get("value") or "0"))
        except ArithmeticError:
            return Decimal("0")

    def split(block, total_key: str, id_key: str, local: set[str], by_note: bool) -> tuple[Decimal, Decimal]:
        block = block if isinstance(block, dict) else {}
        listed = [e for e in (block.get("transactions") or []) if isinstance(e, dict)]

        def seen(e: dict) -> bool:
            if e.get(id_key) in local:
                return True
            note = e.get("note") or "" if by_note else ""
            return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", note) for i in local if i in note)
        listed_sum = sum((amt(e.get("amount")) for e in listed), Decimal("0"))
        unseen = sum((amt(e.get("amount")) for e in listed if not seen(e)), Decimal("0"))
        return unseen, max(amt(block.get(total_key)) - listed_sum, Decimal("0"))

    h = _History()
    h.pay_unseen, h.pay_unlisted = split(inv.raw.get("payments"), "paid_amount", "payment_id", local_pay, True)
    h.ref_unseen, h.ref_unlisted = split(inv.raw.get("refunds"), "refund_amount", "refund_id", local_ref, False)
    return h


def _is_customer_refund(t: Transaction) -> bool:
    """A settled refund paid out (T1107/T1106 are debit or credit events; a positive one is money
    coming back to the business, not a refund to a customer)."""
    return t.event_code in REFUND_CODES and t.status == "S" and t.amount.amount < 0


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def match(invoices: list[Invoice], transactions: list[Transaction], *, as_of: date,
          links: dict[str, Link] | None = None) -> MatchResult:
    """Run the rules. `links` are AI/heuristic suggestions applied only to payments the rules
    left unmatched, and only to open invoices in the same currency.

    Money per invoice comes from one net ledger: local payments (net of refunds that point back at
    them), what PayPal's invoice lists that this window doesn't show, refunds that don't point at a
    local payment, and PayPal's unlisted lump sums read conservatively. Coverage, matching, each
    payment's excess and the invoice's received all use it."""
    links = links or {}
    txns = sorted(transactions, key=lambda t: (t.initiated, t.id, t.event_code, t.status))
    results: dict[tuple, TxnResult] = {}
    # payments that count toward an invoice, in time order: invoice id -> [(txn, result)]
    applied: dict[str, list[tuple[Transaction, TxnResult]]] = {}
    pending_for: dict[str, list[str]] = {}
    refunded_for: dict[str, list[str]] = {}
    by_id = {i.id: i for i in invoices}
    local_pay = {t.id for t in transactions if is_payment(t)}
    local_ref = {t.id for t in transactions if t.event_code in REFUND_CODES}
    hist = {i.id: _history(i, local_pay, local_ref) for i in invoices}

    def res(t: Transaction, outcome: str, **kw) -> TxnResult:
        r = TxnResult(t.id, t.event_code, outcome, status=t.status, **kw)
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
            if t.amount.amount > 0 and kind == "refund":
                ev["incoming_refund"] = True  # money back to the business, not a refund to a customer
                inv, rev = None, {}
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

    # ---- refunds paid out to customers ----
    # pointing back at a local payment (transaction_info.paypal_reference_id): taken off that payment;
    # otherwise ("unattributed"): taken off the invoice the refund names, or whose payment list holds
    # the refunded capture. Known before rules 2-3, so a partly refunded invoice can be topped up.
    refunded_amt: dict[str, Decimal] = {}
    refund_ids: dict[str, list[str]] = {}
    unattributed: dict[str, Decimal] = {}
    for t in txns:
        if not _is_customer_refund(t):
            continue
        if t.reference_id and t.reference_id in local_pay:
            refunded_amt[t.reference_id] = refunded_amt.get(t.reference_id, Decimal("0")) + abs(t.amount.amount)
            refund_ids.setdefault(t.reference_id, []).append(t.id)
            continue
        r = results[t.key]
        inv = by_id.get(r.invoice_id) if r.invoice_id else None
        if inv is None and t.reference_id:
            owners = [i for i in invoices if any(isinstance(e, dict) and e.get("payment_id") == t.reference_id
                                                 for e in ((i.raw.get("payments") or {}).get("transactions") or []))]
            inv = owners[0] if len(owners) == 1 else None
            if inv:
                r.invoice_id, r.invoice_number = inv.id, inv.number
                r.evidence["invoice.payments.transactions[].payment_id"] = t.reference_id
        if inv is not None and inv.total.currency == t.amount.currency:
            unattributed[inv.id] = unattributed.get(inv.id, Decimal("0")) + abs(t.amount.amount)

    def net(t: Transaction) -> Decimal:
        return t.amount.amount - min(refunded_amt.get(t.id, Decimal("0")), t.amount.amount)

    def opening(inv: Invoice, local_net: Decimal) -> Decimal:
        """What the invoice had received before the local payments: PayPal's history (an unlisted lump
        sum only beyond what local rows explain), less refunds not tied to a local payment."""
        h = hist[inv.id]
        local_refunds = sum((min(refunded_amt.get(x.id, Decimal("0")), x.amount.amount)
                             for x, _ in applied.get(inv.id, [])), Decimal("0")) + unattributed.get(inv.id, Decimal("0"))
        return (h.pay_unseen + max(h.pay_unlisted - local_net, Decimal("0"))
                - h.ref_unseen - max(h.ref_unlisted - local_refunds, Decimal("0"))
                - unattributed.get(inv.id, Decimal("0")))

    def received_now(inv: Invoice) -> Decimal:
        local_net = sum((net(x) for x, _ in applied.get(inv.id, [])), Decimal("0"))
        return opening(inv, local_net) + local_net

    # ---- pass 2: rules 2 and 3 against open invoices not yet covered ----
    def covered(inv: Invoice) -> bool:
        return received_now(inv) >= inv.total.amount

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
        exact = [i for i in same_payer if i.total.amount - received_now(i) == t.amount.amount]
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
                  if Decimal("0") < (i.total.amount - t.amount.amount) <= paypal_fee(i.total).amount + FEE_SLACK
                  and received_now(i) == 0]
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
        r = res(t, "unmatched", candidates=sorted(i.number for i in within or same_payer), evidence=ev)
        back = min(refunded_amt.get(t.id, Decimal("0")), t.amount.amount)
        if back:
            r.evidence["refunded_by.transaction_id"] = refund_ids[t.id][-1]
            if back >= t.amount.amount:  # money with no invoice, all of it already given back
                r.outcome = "refunded"
            else:
                r.evidence["refunded_amount"] = str(Money(back, t.amount.currency))

    # ---- pass 3: per-invoice running totals, net of refunds, in time order ----
    net_less: dict[str, Decimal] = {}     # what a refund took back from a payment
    refunded_invoice: set[str] = set()    # invoices with a fully refunded payment
    start: dict[str, Decimal] = {}
    for inv_id, pays in applied.items():
        pays.sort(key=lambda p: (p[0].initiated, p[0].id))
        inv = by_id[inv_id]
        total, ccy = inv.total.amount, inv.total.currency
        running = start[inv_id] = opening(inv, sum((net(x) for x, _ in pays), Decimal("0")))
        first = True
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
            was_first, first = first, False
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
                r.outcome = "fee_mismatch" if was_first and start[inv_id] == 0 and not back \
                    and short <= paypal_fee(inv.total).amount + FEE_SLACK \
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
        local = sum((t.amount.amount - net_less.get(t.id, Decimal("0")) for t, _ in pays), Decimal("0"))
        before = start.get(inv.id, opening(inv, Decimal("0")))
        received = Money(max(before + local, Decimal("0")), inv.total.currency)
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
        elif pays and received.amount >= inv.total.amount:
            outcome = "paid"  # fully covered, even if an earlier payment was short by PayPal's fee
        elif pays and any(r.outcome == "fee_mismatch" for _, r in pays):
            outcome = "fee_mismatch"
        elif pays:
            outcome = "partial"
        elif received.amount >= inv.total.amount and inv.status not in OPEN_STATUSES | {"DRAFT"}:
            outcome = "paid"  # paid per PayPal, payment outside this search window
        elif pending_for.get(inv.id):
            outcome = "pending"
        elif inv.status in PAID_STATUSES and received.amount >= inv.total.amount:
            outcome = "paid"
        elif received.amount > 0 and inv.status in OPEN_STATUSES | PAID_STATUSES:
            outcome = "partial"  # part paid per PayPal; the rest is still due
        elif inv.status in PAID_STATUSES:
            outcome = "paid"  # paid per PayPal with no amounts to go on
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
