"""Accounting export: one balanced journal entry per PayPal payment or refund, as CSV.

Payments (gross G, PayPal fee F, net N = G - F):
    Dr PayPal balance N, Dr PayPal fees F, and on the credit side:
      matched / partial / fee short    Cr Accounts receivable G (a fee shortfall stays open on the invoice)
      overpaid by E                     Cr Accounts receivable G - E, Cr Customer credits E
      duplicate                         Cr Customer credits G (refund candidate)
      unbilled / wrong currency         Cr Unapplied payments G
      paid, later refunded              Cr Accounts receivable G (the refund row reverses it)
Refunds paid out (amount A, fee returned R >= 0):
    Dr Customer credits / Unapplied payments for what the original payment put there (a duplicate,
    an overpayment's excess, or a payment with no invoice), the rest Dr Accounts receivable;
    Cr PayPal fees R, Cr PayPal balance A - R.
Incoming refunds (a positive T1107/T1106: money back to the business): Dr PayPal balance, Cr
    Unapplied payments, to classify.
A negative amount (e.g. a fee larger than the payment) goes on the other side of the same account.
Dates are each row's transaction_initiation_date (PayPal's time zone offset kept), which is the
cut-off to use at month end.
Pending payments and non-sales rows (currency conversions, bank withdrawals) are not booked.
Every entry is in one currency; nothing is converted. Account names are plain, so they can be
mapped to any chart of accounts on import.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from .matcher import MatchResult
from .models import Transaction

ACCOUNTS = {
    "paypal": "PayPal balance",
    "fees": "PayPal fees",
    "receivable": "Accounts receivable",
    "credits": "Customer credits",
    "unapplied": "Unapplied payments",
}
COLUMNS = ["date", "entry_id", "account", "debit", "credit", "currency", "invoice_number",
           "paypal_transaction_id", "event_code", "memo"]
PAYMENT_OUTCOMES = {"matched", "partial", "fee_mismatch", "overpaid", "duplicate", "unmatched",
                    "currency_mismatch", "refunded"}
ZERO = Decimal("0")


def _fmt(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):.2f}"


def journal(result: MatchResult, txns: list[Transaction]) -> list[dict]:
    # a row is (id, event code, status): ids repeat across event codes, and a pending and a settled
    # snapshot of one payment can share both
    by_id: dict[tuple[str, str, str], Transaction] = {}
    for t in txns:
        by_id.setdefault((t.id, t.event_code, t.status), t)

    def row(r) -> Transaction | None:
        return by_id.get((r.txn_id, r.event_code, r.status))
    # where each payment was credited: customer credits (a duplicate in full, an overpayment its
    # excess) and unapplied payments (no invoice, or the wrong currency). A refund of that payment
    # unwinds those first, then accounts receivable.
    credit_left: dict[str, Decimal] = {}
    unapplied_left: dict[str, Decimal] = {}
    for r in result.transactions:
        if r.outcome in PAYMENT_OUTCOMES:
            t0 = row(r)
            if t0 is None:
                continue
            if r.duplicate_of:
                credit_left[r.txn_id] = t0.amount.amount
            elif "overpaid_by" in r.evidence:
                credit_left[r.txn_id] = Decimal(r.evidence["overpaid_by"].split()[0])
            elif _unapplied(r):
                unapplied_left[r.txn_id] = t0.amount.amount
    # a reversed payment (status V) is cash only alongside the row that took the money back; without
    # that row in this export (not reported yet, or outside the window) neither side is booked
    reversed_back = {t.reference_id for t in txns if t.reference_id and t.amount.amount < 0 and t.status == "S"}
    lines: list[dict] = []

    for r in result.transactions:
        t = row(r)
        if t is None:
            continue
        ccy, day = t.amount.currency, t.initiated.date().isoformat()
        entry_id = f"J-{t.id}-{t.event_code}"

        def line(account: str, debit: Decimal = ZERO, credit: Decimal = ZERO, memo: str = "") -> None:
            if debit < 0 or credit < 0:  # e.g. a fee larger than the payment: the other side, same account
                debit, credit = max(debit, ZERO) + max(-credit, ZERO), max(credit, ZERO) + max(-debit, ZERO)
            if debit == ZERO and credit == ZERO:
                return
            lines.append({"date": day, "entry_id": entry_id, "account": ACCOUNTS[account],
                          "debit": _fmt(debit) if debit else "", "credit": _fmt(credit) if credit else "",
                          "currency": ccy, "invoice_number": r.invoice_number or "",
                          "paypal_transaction_id": t.id, "event_code": t.event_code, "memo": memo})

        if r.outcome in PAYMENT_OUTCOMES and t.status == "V" and t.id not in reversed_back:
            continue
        if r.outcome in PAYMENT_OUTCOMES:
            gross = t.amount.amount
            fee = -t.fee.amount if t.fee and t.fee.amount < 0 else ZERO
            line("paypal", debit=gross - fee, memo=f"PayPal payment {t.id}, net of fee")
            line("fees", debit=fee, memo="PayPal fee")
            n = r.invoice_number
            if r.duplicate_of:
                line("credits", credit=gross, memo=f"Duplicate payment for {n} (of {r.duplicate_of}); refund candidate")
            elif "overpaid_by" in r.evidence:
                excess = Decimal(r.evidence["overpaid_by"].split()[0])
                line("receivable", credit=gross - excess, memo=f"Payment for {n}")
                line("credits", credit=excess, memo=f"Overpaid {n} by {_fmt(excess)}; refund or credit")
            elif _unapplied(r):
                why = f"names {n} but paid in {ccy}" if r.outcome == "currency_mismatch" else "no invoice found"
                line("unapplied", credit=gross, memo=f"Unapplied payment: {why}")
            else:
                memo = f"Payment for {n}"
                if r.outcome == "fee_mismatch" and r.difference is not None:
                    memo += f"; short by {_fmt(-r.difference.amount)} (PayPal's fee), still open on the invoice"
                elif r.outcome == "partial" and r.difference is not None:
                    memo += f"; {_fmt(-r.difference.amount)} still open on the invoice"
                elif r.rule in ("ai", "memo_heuristic"):
                    memo += " (suggested match; review before posting)"
                line("receivable", credit=gross, memo=memo)

        elif r.outcome == "refund" and t.amount.amount > 0:
            # money coming back to the business (T1107/T1106 can be credits): cash in, to classify
            fee = -t.fee.amount if t.fee and t.fee.amount < 0 else ZERO
            line("paypal", debit=t.amount.amount - fee, memo=f"Incoming refund {t.id}, net of fee")
            line("fees", debit=fee, memo="PayPal fee")
            line("unapplied", credit=t.amount.amount, memo="Incoming refund (money received back); classify")
        elif r.outcome == "refund":
            amount = -t.amount.amount
            returned = t.fee.amount if t.fee and t.fee.amount > 0 else ZERO
            orig = t.reference_id or ""
            from_credits = min(amount, credit_left.get(orig, ZERO))
            if orig in credit_left:
                credit_left[orig] -= from_credits
            from_unapplied = min(amount - from_credits, unapplied_left.get(orig, ZERO))
            if orig in unapplied_left:
                unapplied_left[orig] -= from_unapplied
            line("credits", debit=from_credits, memo=f"Refund of {orig or 'a payment'} from customer credits")
            line("unapplied", debit=from_unapplied, memo=f"Refund of unapplied payment {orig}")
            line("receivable", debit=amount - from_credits - from_unapplied, memo=f"Refund of {orig or 'a payment'}")
            line("fees", credit=returned, memo="PayPal fee returned on refund")
            line("paypal", credit=amount - returned, memo=f"PayPal refund {t.id}")
    return lines


def _unapplied(r) -> bool:
    """Credited to unapplied payments: no invoice (including one since refunded), or the wrong currency."""
    return r.outcome in ("unmatched", "currency_mismatch") or (r.outcome == "refunded" and not r.invoice_number)


FORMULA_START = set("=+-@\uff1d\uff0b\uff0d\uff20")  # and their fullwidth forms


def _safe(v: str) -> str:
    # spreadsheet formula injection: payer-written text could start (after spaces) with = + - @
    head = v.lstrip(" \t\r\n\xa0\u3000")
    if v and (v[0] in "\t\r\n" or (head[:1] in FORMULA_START)) and not _is_number(v):
        return "'" + v
    return v


def _is_number(v: str) -> bool:
    try:
        Decimal(v)
        return True
    except ArithmeticError:  # decimal.InvalidOperation
        return False


def to_csv(lines: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for l in lines:
        w.writerow({k: _safe(str(l.get(k, ""))) for k in COLUMNS})
    return buf.getvalue()
