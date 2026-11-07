"""E1: deterministic matcher. One acceptance test per fixture case (oracle: fixtures/cases.json)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cashdesk.matcher import OUTCOMES, match
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from tools.make_fixtures import txn_id

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "fixtures" / "cases.json").read_text())
AS_OF = date(2026, 11, 7)


@pytest.fixture(scope="module")
def result():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    return match(c.list_invoices(), txns, as_of=AS_OF)


def deterministic_expectation(case: dict, outcome: str) -> str:
    # Cases the AI resolves stay unmatched for the rules alone (E2 turns them into matches).
    return "unmatched" if case.get("resolved_by") == "ai" else outcome


@pytest.mark.parametrize("name", sorted(CASES))
def test_case(result, name):
    case = CASES[name]
    for key, outcome in case["expected"].items():
        want = deterministic_expectation(case, outcome)
        if key.startswith("NW-"):
            got = result.invoice_by_number(key)
            assert got.outcome == want, (name, key, got)
        else:
            got = result.txn(key)
            assert got.outcome == want, (name, key, got)
            if want not in ("unmatched", "ignored") and case["invoices"]:
                assert got.invoice_number in case["invoices"], (name, key, got)


def test_every_transaction_and_invoice_gets_exactly_one_known_outcome(result):
    assert len(result.transactions) == 26
    assert len(result.invoices) == 26
    for r in result.transactions + result.invoices:
        assert r.outcome in OUTCOMES


def test_rules_are_reported(result):
    assert result.txn(txn_id("NW-1001")).rule == "invoice_ref"
    assert result.txn(txn_id("NW-1011")).rule == "amount_email"
    assert result.txn(txn_id("NW-1020")).rule == "fee_tolerance"
    assert result.txn(txn_id("unbilled-ravi")).rule is None


def test_evidence_cites_the_fields_used(result):
    ev = result.txn(txn_id("NW-1001")).evidence
    assert ev["transaction_info.invoice_id"] == "NW-1001"
    assert ev["invoice.detail.invoice_number"] == "NW-1001"
    ev = result.txn(txn_id("NW-1011")).evidence
    assert ev["payer_info.email_address"] == ev["invoice.primary_recipients[0].billing_info.email_address"]
    ev = result.txn(txn_id("NW-1014")).evidence
    assert "transaction_info.transaction_note" in ev


def test_differences_are_exact_decimal(result):
    assert result.txn(txn_id("NW-1014")).difference == Money(Decimal("45.00"), "USD")
    assert result.txn(txn_id("NW-1013")).difference == Money(Decimal("-600.00"), "USD")
    assert result.txn(txn_id("NW-1020")).difference == Money(Decimal("-17.94"), "USD")
    assert result.txn(txn_id("NW-1001")).difference == Money(Decimal("0.00"), "USD")


def test_invoice_rollups(result):
    inv = result.invoice_by_number("NW-1013")
    assert inv.outcome == "partial" and inv.outstanding == Money(Decimal("600.00"), "USD")
    assert result.invoice_by_number("NW-1001").outcome == "paid"
    assert result.invoice_by_number("NW-1014").outcome == "overpaid"
    assert result.invoice_by_number("NW-1015").outcome == "paid"
    assert result.invoice_by_number("NW-1016").days_overdue == 13


def test_duplicate_points_at_the_original(result):
    dup = result.txn(txn_id("NW-1015-dup"))
    assert dup.duplicate_of == txn_id("NW-1015")


def test_ambiguous_amount_email_match_is_left_for_the_ai():
    c = FixturePayPalClient()
    invs = c.list_invoices()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    # Clone NW-1011 into a second open invoice with the same payer and amount: rule 2 must not guess.
    import copy
    from cashdesk.paypal.parse import parse_invoice
    raw = copy.deepcopy(next(i for i in invs if i.number == "NW-1011").raw)
    raw["id"], raw["detail"]["invoice_number"] = "INV2-AAAA-BBBB-CCCC-DDDD", "NW-1099"
    r = match(invs + [parse_invoice(raw)], txns, as_of=AS_OF)
    t = r.txn(txn_id("NW-1011"))
    assert t.outcome == "unmatched" and t.candidates == ["NW-1011", "NW-1099"]


def test_summary(result):
    s = result.summary()
    assert s["transactions"] == 26
    assert s["by_outcome"]["matched"] == 14  # 12 clean + duplicate's original + EUR
    assert s["exceptions"] >= 8
