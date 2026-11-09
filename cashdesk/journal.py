"""Accounting export: one balanced journal entry per PayPal payment or refund, as CSV.

Payments (gross G, PayPal fee F, net N = G - F):
    Dr PayPal balance N, Dr PayPal fees F, and on the credit side:
      matched / partial / fee short    Cr Accounts receivable G (a fee shortfall stays open on the invoice)
      overpaid by E                     Cr Accounts receivable G - E, Cr Customer credits E
      duplicate                         Cr Customer credits G (refund candidate)
      unbilled / wrong currency         Cr Unapplied payments G
      paid, later refunded              Cr Accounts receivable G (the refund row reverses it)
Refunds (amount A, fee returned R >= 0):
    Dr Customer credits A when the original was a duplicate or an overpayment, else Dr Accounts
    receivable A; Cr PayPal fees R, Cr PayPal balance A - R.
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
    by_id: dict[tuple[str, str], Transaction] = {}
    for t in txns:  # transaction ids repeat across event codes; one row per (id, event code) is enough
        by_id.setdefault((t.id, t.event_code), t)
    # how much of each payment went to customer credits (a duplicate in full, an overpayment its excess);
    # refunds of that payment take from those credits first, then from receivables
    credit_left: dict[str, Decimal] = {}
    for r in result.transactions:
        if r.outcome in PAYMENT_OUTCOMES:
            t0 = by_id.get((r.txn_id, r.event_code))
            if t0 is None:
                continue
            if r.duplicate_of:
                credit_left[r.txn_id] = t0.amount.amount
            elif "overpaid_by" in r.evidence:
                credit_left[r.txn_id] = Decimal(r.evidence["overpaid_by"].split()[0])
    lines: list[dict] = []

    for r in result.transactions:
        t = by_id.get((r.txn_id, r.event_code))
        if t is None:
            continue
        ccy, day = t.amount.currency, t.initiated.date().isoformat()
        entry_id = f"J-{t.id}-{t.event_code}"

        def line(account: str, debit: Decimal = ZERO, credit: Decimal = ZERO, memo: str = "") -> None:
            if debit == ZERO and credit == ZERO:
                return
            lines.append({"date": day, "entry_id": entry_id, "account": ACCOUNTS[account],
                          "debit": _fmt(debit) if debit else "", "credit": _fmt(credit) if credit else "",
                          "currency": ccy, "invoice_number": r.invoice_number or "",
                          "paypal_transaction_id": t.id, "event_code": t.event_code, "memo": memo})

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
            elif r.outcome in ("unmatched", "currency_mismatch"):
                why = "no invoice found" if r.outcome == "unmatched" else f"names {n} but paid in {ccy}"
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

        elif r.outcome == "refund":
            amount = -t.amount.amount if t.amount.amount < 0 else t.amount.amount
            returned = t.fee.amount if t.fee and t.fee.amount > 0 else ZERO
            orig = t.reference_id or ""
            from_credits = min(amount, credit_left.get(orig, ZERO))
            if orig in credit_left:
                credit_left[orig] -= from_credits
            line("credits", debit=from_credits, memo=f"Refund of {orig or 'a payment'} from customer credits")
            line("receivable", debit=amount - from_credits, memo=f"Refund of {orig or 'a payment'}")
            line("fees", credit=returned, memo="PayPal fee returned on refund")
            line("paypal", credit=amount - returned, memo=f"PayPal refund {t.id}")
    return lines


def _safe(v: str) -> str:
    # spreadsheet formula injection: payer-written text could start with = + - @
    return "'" + v if v and v[0] in "=+-@\t\r" and not _is_number(v) else v


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
