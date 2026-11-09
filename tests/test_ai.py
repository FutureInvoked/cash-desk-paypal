"""E2: AI layer. Claude resolves ambiguous payments and writes cited reasons; deterministic fallback.

The SDK is always mocked here: no test touches the network or needs a key.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from cashdesk.ai import MODEL, ClaudeReconciler, FallbackReconciler, make_reconciler, reconcile
from cashdesk.paypal import FixturePayPalClient
from tools.make_fixtures import txn_id

ROOT = Path(__file__).resolve().parent.parent
AS_OF = date(2026, 11, 7)
LUMEN = txn_id("NW-1021")


def data():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return c.list_invoices(), txns


class FakeMessages:
    def __init__(self, payload=None, stop_reason="end_turn", exc=None):
        self.payload, self.stop_reason, self.exc, self.calls = payload, stop_reason, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        text = json.dumps(self.payload) if not isinstance(self.payload, str) else self.payload
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=self.stop_reason,
                               model=MODEL, stop_details=None)


def fake_client(**kw):
    msgs = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def good_payload(result_ids):
    return {
        "resolutions": [{"transaction_id": LUMEN, "invoice_number": "NW-1021", "confidence": 0.92,
                         "reason": "Memo names Lumen Bakery and the October retainer; amount equals NW-1021.",
                         "cited_fields": ["transaction_info.transaction_note", "invoice.amount"]}],
        "reasons": [{"id": i, "reason": f"AI reason for {i} citing transaction_info.transaction_id.",
                     "cited_fields": ["transaction_info.transaction_id"]} for i in result_ids],
    }


# ---------- fallback (no key) ----------

def test_fallback_is_used_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_reconciler(), FallbackReconciler)


def test_fallback_resolves_the_email_differs_case_with_lower_confidence():
    invs, txns = data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    t = r.txn(LUMEN)
    assert t.outcome == "matched" and t.invoice_number == "NW-1021"
    assert t.rule == "memo_heuristic" and t.resolved_by == "fallback"
    assert t.confidence < 0.8 and t.needs_review
    assert r.invoice_by_number("NW-1021").outcome == "paid"
    # genuinely unbilled payments stay unmatched
    assert r.txn(txn_id("unbilled-ravi")).outcome == "unmatched"


def test_every_result_gets_a_one_line_reason_citing_its_evidence():
    invs, txns = data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    for t in r.transactions:
        assert t.reason and "\n" not in t.reason, t
        assert any(path in t.reason for path in t.evidence), (t.txn_id, t.reason)
        assert t.reason_source == "rules"
    for i in r.invoices:
        assert i.reason and "\n" not in i.reason and i.reason_source == "rules"


def test_specific_reasons_are_informative():
    invs, txns = data()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    assert "NW-1015" in r.txn(txn_id("NW-1015-dup")).reason and "refund" in r.txn(txn_id("NW-1015-dup")).reason.lower()
    assert "17.94" in r.txn(txn_id("NW-1020")).reason
    assert "13 days" in r.invoice_by_number("NW-1016").reason


# ---------- Claude path (mocked) ----------

def test_claude_request_shape():
    invs, txns = data()
    client, msgs = fake_client(payload={"resolutions": [], "reasons": []})
    reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    kw = msgs.calls[0]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["effort"] in ("low", "medium")
    assert "thinking" not in kw or kw["thinking"]["type"] == "adaptive"
    assert "temperature" not in kw
    body = kw["messages"][0]["content"]
    assert LUMEN in body and "NW-1021" in body
    # payer-written text is fenced as untrusted data
    assert "untrusted" in kw["system"].lower()


def test_claude_resolution_and_reasons_are_applied():
    invs, txns = data()
    ids = [t.id for t in txns] + [i.number for i in invs]
    client, _ = fake_client(payload=good_payload(ids))
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    t = r.txn(LUMEN)
    assert t.outcome == "matched" and t.rule == "ai" and t.resolved_by == "ai"
    assert t.confidence == pytest.approx(0.92) and t.needs_review is False
    assert t.reason_source == "ai" and "Lumen Bakery" in t.reason
    assert r.txn(txn_id("NW-1001")).reason_source == "ai"


def test_ai_cannot_link_to_an_invoice_that_is_not_a_candidate():
    invs, txns = data()
    bad = good_payload([])
    bad["resolutions"][0]["invoice_number"] = "NW-1001"  # already paid: not a candidate
    client, _ = fake_client(payload=bad)
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    assert r.txn(LUMEN).outcome == "unmatched"


def test_ai_reason_citing_unknown_fields_is_replaced_by_the_rules_reason():
    invs, txns = data()
    p = {"resolutions": [], "reasons": [{"id": txn_id("NW-1001"), "reason": "trust me",
                                         "cited_fields": ["made_up.field"]}]}
    client, _ = fake_client(payload=p)
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    assert r.txn(txn_id("NW-1001")).reason_source == "rules"


def test_low_confidence_ai_link_is_not_applied():
    invs, txns = data()
    p = good_payload([])
    p["resolutions"][0]["confidence"] = 0.3
    client, _ = fake_client(payload=p)
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    assert r.txn(LUMEN).outcome == "unmatched"


@pytest.mark.parametrize("kind", ["refusal", "api_error", "bad_json"])
def test_claude_failures_fall_back_to_rules(kind):
    invs, txns = data()
    if kind == "refusal":
        client, _ = fake_client(payload={}, stop_reason="refusal")
    elif kind == "api_error":
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        client, _ = fake_client(exc=anthropic.APIConnectionError(request=req))
    else:
        client, _ = fake_client(payload="not json")
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=ClaudeReconciler(client=client))
    assert r.txn(LUMEN).outcome == "matched" and r.txn(LUMEN).resolved_by == "fallback"
    assert r.ai_status.startswith("fallback")


# ---------- eval file ----------

def test_eval_file_matches_the_fixture_oracle_and_fallback_scores_100():
    ev = json.loads((ROOT / "evals" / "match_eval.json").read_text())
    cases = json.loads((ROOT / "fixtures" / "cases.json").read_text())
    expected = {k: v for c in cases.values() for k, v in c["expected"].items()}
    assert {e["id"]: e["expected_outcome"] for e in ev["items"]} == expected
    from cashdesk.ai import run_eval
    invs, txns = data()
    score = run_eval(ev, invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    assert score["accuracy"] == 1.0, score["failures"]


def test_overdue_reason_says_1_day_not_1_days():
    # Found in the live S3 run on 2026-11-08: "0005 is 1 days overdue".
    from dataclasses import fields
    from cashdesk.ai import invoice_reason
    from cashdesk.matcher import InvoiceResult
    from cashdesk.models import Money
    from decimal import Decimal
    kw = {f.name: None for f in fields(InvoiceResult)}
    kw.update(invoice_id="I-1", invoice_number="0005", outcome="overdue", days_overdue=1,
              outstanding=Money(Decimal("240.00"), "USD"), total=Money(Decimal("240.00"), "USD"))
    r = InvoiceResult(**{k: v for k, v in kw.items() if v is not None or k not in ("transactions",)})
    assert "is 1 day overdue" in invoice_reason(r)
    r.days_overdue = 3
    assert "is 3 days overdue" in invoice_reason(r)


def test_fallback_matches_a_memo_that_names_the_contact_when_the_invoice_has_no_business():
    # Found on the live sandbox 2026-11-09: invoice 0008 has only a contact ("Maria Lumen"), and the
    # payer's note says "Lumen Bakery - Website retainer". The fallback must still suggest it.
    import dataclasses
    invs, txns = data()
    invs = [dataclasses.replace(i, recipient_business=None, recipient_name="Maria Lumen") if i.number == "NW-1021" else i
            for i in invs]
    txns = [dataclasses.replace(t, payer_name="John Doe", note="Lumen Bakery - Website retainer", subject=None)
            if t.id == LUMEN else t for t in txns]
    t = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler()).txn(LUMEN)
    assert t.outcome == "matched" and t.invoice_number == "NW-1021" and t.rule == "memo_heuristic"
    assert t.needs_review and t.evidence.get("invoice.primary_recipients[0].billing_info.name") == "Maria Lumen"
    assert "names the invoice contact" in t.reason and "business" not in t.reason


def test_fallback_contact_rule_needs_a_whole_word_and_one_candidate():
    import dataclasses
    invs, txns = data()
    invs = [dataclasses.replace(i, recipient_business=None, recipient_name="Maria Lumen") if i.number == "NW-1021" else i
            for i in invs]
    for note in ("Illumenate retainer", "retainer"):  # substring only / no name at all
        tx = [dataclasses.replace(t, payer_name="John Doe", note=note, subject=None) if t.id == LUMEN else t for t in txns]
        assert reconcile(invs, tx, as_of=AS_OF, reconciler=FallbackReconciler()).txn(LUMEN).outcome == "unmatched", note
