"""AI layer: Claude resolves the payments the rules can't place and explains every result.

- Model: claude-sonnet-5-5 through the official Anthropic SDK, one request per reconciliation,
  structured JSON output (output_config.format), effort "low", server-side refusal fallback
  (`fallbacks: "default"`, beta server-side-fallback-2026-07-01).
- Without ANTHROPIC_API_KEY (or on any API error, refusal or invalid output) the deterministic
  FallbackReconciler runs instead, so the demo works with no keys.
- The AI only suggests. Its links are validated (candidate invoice, same currency, confidence
  >= MIN_CONFIDENCE) and fed back through the deterministic matcher; its reasons are kept only if
  they cite evidence fields that exist. Nothing here can execute an action.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .matcher import InvoiceResult, Link, MatchResult, TxnResult, match, paypal_fee
from .models import Invoice, Transaction

MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MIN_CONFIDENCE = 0.6
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "reconcile_system.md").read_text()

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "invoice_number": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "cited_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["transaction_id", "invoice_number", "confidence", "reason", "cited_fields"],
                "additionalProperties": False,
            },
        },
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                    "cited_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "reason", "cited_fields"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["resolutions", "reasons"],
    "additionalProperties": False,
}


@dataclass
class Suggestions:
    links: dict[str, Link]
    reasons: dict[str, str]  # result id (txn id or invoice number) -> reason
    status: str


# ---------------------------------------------------------------- candidates & payload

def candidates(result: MatchResult, invoices: list[Invoice], t: Transaction) -> list[Invoice]:
    open_numbers = {r.invoice_number for r in result.invoices if r.outcome in ("open", "overdue", "partial")}
    return [i for i in invoices if i.number in open_numbers and i.total.currency == t.amount.currency]


def _unmatched(result: MatchResult, txns: list[Transaction]) -> list[Transaction]:
    ids = {r.txn_id for r in result.transactions if r.outcome == "unmatched"}
    return [t for t in txns if t.id in ids]


def _inv_brief(i: Invoice, r: InvoiceResult | None) -> dict:
    return {"invoice_number": i.number, "recipient_business": i.recipient_business,
            "recipient_name": i.recipient_name, "recipient_email": i.recipient_email,
            "amount": str(i.total), "still_due": str(r.outstanding) if r else str(i.due),
            "items": list(i.items), "due_date": i.due_date.isoformat() if i.due_date else None}


def build_payload(result: MatchResult, invoices: list[Invoice], txns: list[Transaction]) -> dict:
    inv_r = {r.invoice_number: r for r in result.invoices}
    unmatched = []
    for t in _unmatched(result, txns):
        unmatched.append({
            "transaction_id": t.id,
            "evidence": {"transaction_info.transaction_amount": str(t.amount),
                         "payer_info.email_address": t.payer_email,
                         "payer_info.payer_name": t.payer_name,
                         "transaction_info.transaction_subject": t.subject,
                         "transaction_info.transaction_note": t.note,
                         "transaction_info.transaction_initiation_date": t.initiated.isoformat()},
            "candidate_invoices": [_inv_brief(i, inv_r.get(i.number)) for i in candidates(result, invoices, t)],
        })
    results = [{"id": r.txn_id, "kind": "transaction", "outcome": r.outcome, "invoice_number": r.invoice_number,
                "difference": str(r.difference) if r.difference else None, "evidence": r.evidence}
               for r in result.transactions]
    results += [{"id": r.invoice_number, "kind": "invoice", "outcome": r.outcome, "status": r.status,
                 "evidence": {"invoice.detail.invoice_number": r.invoice_number, "invoice.amount": str(r.total),
                              "received": str(r.received), "invoice.due_amount": str(r.outstanding),
                              "invoice.detail.payment_term.due_date": next(
                                  (i.due_date.isoformat() for i in invoices if i.number == r.invoice_number
                                   and i.due_date), None),
                              "days_overdue": r.days_overdue}}
                for r in result.invoices]
    return {"unmatched_payments": unmatched, "results": results}


# ---------------------------------------------------------------- reconcilers

class FallbackReconciler:
    """No-key, deterministic stand-in: links a payment when the memo names an open candidate's
    business (or the payer shares the recipient's surname) and the amount equals what is due."""

    name = "fallback"

    def suggest(self, result: MatchResult, invoices: list[Invoice], txns: list[Transaction]) -> Suggestions:
        inv_r = {r.invoice_number: r for r in result.invoices}
        links: dict[str, Link] = {}
        for t in _unmatched(result, txns):
            text = " ".join(x for x in (t.subject, t.note) if x).lower()
            hits = []
            for i in candidates(result, invoices, t):
                due = inv_r[i.number].outstanding if i.number in inv_r else i.due
                if due != t.amount:
                    continue
                biz = (i.recipient_business or "").lower()
                surname = (i.recipient_name or "").split(" ")[-1].lower()
                payer_surname = (t.payer_name or "").split(" ")[-1].lower()
                if biz and biz in text:
                    hits.append((i, {"transaction_info.transaction_note": t.note or t.subject,
                                     "invoice.primary_recipients[0].billing_info.business_name": i.recipient_business}))
                elif surname and surname == payer_surname:
                    hits.append((i, {"payer_info.payer_name": t.payer_name,
                                     "invoice.primary_recipients[0].billing_info.name": i.recipient_name}))
            if len(hits) == 1:
                i, ev = hits[0]
                links[t.id] = Link(i.number, "memo_heuristic", "fallback", 0.6, ev)
        return Suggestions(links, {}, "fallback (no ANTHROPIC_API_KEY)")


class ClaudeReconciler:
    name = "claude"

    def __init__(self, client=None, model: str = MODEL):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client, self.model = client, model

    def suggest(self, result: MatchResult, invoices: list[Invoice], txns: list[Transaction]) -> Suggestions:
        import anthropic

        payload = build_payload(result, invoices, txns)
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
        except anthropic.APIError as e:  # connection, status and timeout errors
            return self._fallback(result, invoices, txns, f"fallback (API error: {type(e).__name__})")
        if resp.stop_reason == "refusal":
            return self._fallback(result, invoices, txns, "fallback (model declined)")
        if resp.stop_reason == "max_tokens":
            return self._fallback(result, invoices, txns, "fallback (output truncated)")
        try:
            text = next(b.text for b in resp.content if b.type == "text")
            out = json.loads(text)
        except (StopIteration, json.JSONDecodeError):
            return self._fallback(result, invoices, txns, "fallback (invalid AI output)")
        return self._validate(out, result, invoices, txns, payload)

    def _fallback(self, result, invoices, txns, status):
        s = FallbackReconciler().suggest(result, invoices, txns)
        return Suggestions(s.links, {}, status)

    def _validate(self, out, result, invoices, txns, payload) -> Suggestions:
        by_txn = {t.id: t for t in _unmatched(result, txns)}
        links: dict[str, Link] = {}
        for res in out.get("resolutions", []):
            t = by_txn.get(res.get("transaction_id"))
            num = res.get("invoice_number")
            conf = float(res.get("confidence") or 0)
            if not t or not num or conf < MIN_CONFIDENCE:
                continue
            if num not in {i.number for i in candidates(result, invoices, t)}:
                continue  # the AI may only pick from the candidates it was shown
            ev = {"ai.reason": str(res.get("reason", ""))[:300]}
            ev.update({f: payload_value(payload, t.id, f) for f in res.get("cited_fields", [])
                       if payload_value(payload, t.id, f) is not None})
            links[t.id] = Link(num, "ai", "ai", conf, ev)
        evidence_keys = {r["id"]: set(r["evidence"]) for r in payload["results"]}
        reasons = {}
        for r in out.get("reasons", []):
            rid, text, cited = r.get("id"), _one_line(r.get("reason", "")), r.get("cited_fields", [])
            if rid in evidence_keys and text and cited and set(cited) <= evidence_keys[rid]:
                reasons[rid] = text
        return Suggestions(links, reasons, f"ai ({self.model})")


def payload_value(payload: dict, txn_id: str, field: str):
    for u in payload["unmatched_payments"]:
        if u["transaction_id"] == txn_id:
            return u["evidence"].get(field)
    return None


def make_reconciler():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeReconciler()
    return FallbackReconciler()


# ---------------------------------------------------------------- reasons (deterministic)

def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:280]


def _first_ref(ev: dict) -> str:
    for k in ("transaction_info.invoice_id", "cart_info.paypal_invoice_id",
              "transaction_info.transaction_subject", "transaction_info.transaction_note"):
        if k in ev:
            return f"{k} = {ev[k]!s}"
    return "transaction_info.transaction_id"


def txn_reason(r: TxnResult, txns: dict[str, Transaction]) -> str:
    t = txns.get(r.txn_id)
    amt = str(t.amount) if t else "?"
    ev = r.evidence
    email = ev.get("payer_info.email_address", "unknown payer")
    n = r.invoice_number
    if r.outcome == "matched" and r.rule == "invoice_ref":
        return f"Matched to {n}: {_first_ref(ev)}; transaction_info.transaction_amount {amt} equals the invoice total."
    if r.outcome == "matched" and r.rule == "amount_email":
        return (f"Matched to {n}: transaction_info.transaction_amount {amt} and payer_info.email_address {email} "
                f"equal the invoice total and recipient.")
    if r.outcome == "matched" and r.rule in ("memo_heuristic", "ai"):
        why = ev.get("ai.reason") or ("transaction_info.transaction_note names the customer's business"
                                      if "transaction_info.transaction_note" in ev
                                      else "payer_info.payer_name matches the recipient")
        who = "AI" if r.rule == "ai" else "heuristic"
        return (f"Suggested match to {n} ({who}, confidence {r.confidence:.0%}, review): {why}; "
                f"payer_info.email_address {email} differs from the invoice recipient.")
    if r.outcome == "fee_mismatch":
        return (f"Short by {-r.difference.amount:.2f} on {n}, which equals PayPal's fee ({ev.get('expected_paypal_fee')}): "
                f"payer_info.email_address {email} matches; the payer likely deducted the fee.")
    if r.outcome == "partial":
        return (f"Partial payment on {n}: transaction_info.transaction_amount {amt} received, "
                f"{-r.difference.amount:.2f} {r.difference.currency} still due ({_first_ref(ev)}).")
    if r.outcome == "overpaid":
        return f"Overpaid {n} by {r.difference}: {_first_ref(ev)}; offer a refund or credit."
    if r.outcome == "duplicate":
        orig = txns.get(r.duplicate_of)
        mins = int((t.initiated - orig.initiated).total_seconds() // 60) if t and orig else 0
        return (f"Duplicate of {r.duplicate_of} for {n}: same payer_info.email_address, same amount {amt}, "
                f"{mins} min apart, invoice already covered; refund candidate.")
    if r.outcome == "currency_mismatch":
        return (f"Names {n} ({_first_ref(ev)}) but paid in {t.amount.currency if t else '?'}; "
                f"invoice.detail.currency_code is {ev.get('invoice.detail.currency_code')}; review manually.")
    if r.outcome == "refunded":
        return f"Paid {n}, then fully refunded (transaction_info.transaction_status V)."
    if r.outcome == "refund":
        return f"Refund row for {n} pointing at transaction_info.paypal_reference_id {ev.get('transaction_info.paypal_reference_id')}."
    if r.outcome == "pending":
        return f"Pending for {n} (transaction_info.transaction_status P); not counted as paid yet."
    if r.outcome == "ignored":
        return f"Not a customer payment (transaction_info.transaction_event_code {r.event_code}); ignored."
    if r.outcome == "unmatched":
        extra = f"; candidates: {', '.join(r.candidates)}" if r.candidates else ""
        return (f"No invoice found for {amt} from payer_info.email_address {email}: no invoice reference and no "
                f"open invoice at that amount{extra}. Consider a draft invoice.")
    return f"{r.outcome} (transaction_info.transaction_id {r.txn_id})."


def invoice_reason(r: InvoiceResult) -> str:
    n = r.invoice_number
    if r.outcome == "paid":
        return f"{n} paid in full: {r.received} received against invoice.amount {r.total}."
    if r.outcome == "partial":
        return f"{n} partly paid: {r.received} of {r.total}; {r.outstanding} still due."
    if r.outcome == "overpaid":
        return f"{n} overpaid: {r.received} received against invoice.amount {r.total}."
    if r.outcome == "fee_mismatch":
        return f"{n} paid short by about PayPal's fee ({paypal_fee(r.total)}); decide whether to write it off."
    if r.outcome == "overdue":
        sent = f"; last sent {r.last_sent}" if r.last_sent else ""
        return f"{n} is {r.days_overdue} days overdue (invoice.detail.payment_term.due_date passed), {r.outstanding} unpaid{sent}; send a reminder."
    if r.outcome == "open":
        return f"{n} open, not yet due; {r.outstanding} outstanding."
    if r.outcome == "pending":
        return f"{n} has a pending payment; wait for it to clear."
    if r.outcome == "refunded":
        return f"{n} was paid and then refunded."
    if r.outcome == "draft":
        return f"{n} is a draft that was never sent."
    return f"{n}: {r.outcome}."


# ---------------------------------------------------------------- entry points

def reconcile(invoices: list[Invoice], txns: list[Transaction], *, as_of: date, reconciler=None) -> MatchResult:
    reconciler = reconciler or make_reconciler()
    first = match(invoices, txns, as_of=as_of)
    sug = reconciler.suggest(first, invoices, txns)
    final = match(invoices, txns, as_of=as_of, links=sug.links) if sug.links else first
    by_id = {t.id: t for t in txns}
    for r in final.transactions:
        if r.txn_id in sug.reasons and r.outcome == first.txn(r.txn_id, r.event_code).outcome:
            r.reason, r.reason_source = sug.reasons[r.txn_id], "ai"
        else:
            r.reason, r.reason_source = txn_reason(r, by_id), "rules"
    for r in final.invoices:
        if r.invoice_number in sug.reasons and r.outcome == first.invoice_by_number(r.invoice_number).outcome:
            r.reason, r.reason_source = sug.reasons[r.invoice_number], "ai"
        else:
            r.reason, r.reason_source = invoice_reason(r), "rules"
    # an AI-resolved link always explains itself with the AI's own words
    for r in final.transactions:
        if r.rule == "ai":
            r.reason = txn_reason(r, by_id) if not r.evidence.get("ai.reason") else (
                f"Suggested match to {r.invoice_number} (AI, confidence {r.confidence:.0%}): {r.evidence['ai.reason']}")
            r.reason_source = "ai"
    final.ai_status = sug.status
    return final


def run_eval(ev: dict, invoices, txns, *, as_of: date, reconciler=None) -> dict:
    r = reconcile(invoices, txns, as_of=as_of, reconciler=reconciler)
    failures = []
    for item in ev["items"]:
        got = (r.invoice_by_number(item["id"]).outcome if item["kind"] == "invoice"
               else r.txn(item["id"]).outcome)
        if got != item["expected_outcome"]:
            failures.append({"id": item["id"], "case": item["case"], "expected": item["expected_outcome"], "got": got})
    n = len(ev["items"])
    return {"items": n, "correct": n - len(failures), "accuracy": (n - len(failures)) / n,
            "failures": failures, "ai_status": r.ai_status}
