"""Accounting export: one balanced journal entry per PayPal payment or refund, as CSV."""

from __future__ import annotations

import csv
import dataclasses
import io
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.journal import ACCOUNTS, COLUMNS, journal, to_csv
from cashdesk.paypal import FixturePayPalClient
from tools.make_fixtures import txn_id
from tests.accounts_helper import sign_in

AS_OF = date(2026, 11, 7)


def data(extra=()):
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    txns = list(txns) + list(extra)
    r = reconcile(c.list_invoices(), txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return r, txns


@pytest.fixture(scope="module")
def lines():
    r, txns = data()
    return journal(r, txns)


def entry(lines, tid, code=None):
    return [l for l in lines if l["paypal_transaction_id"] == tid and (code is None or l["event_code"] == code)]


def amounts(rows):
    out = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for l in rows:
        out[l["account"]][0] += Decimal(l["debit"] or "0")
        out[l["account"]][1] += Decimal(l["credit"] or "0")
    return {k: (str(v[0]), str(v[1])) for k, v in out.items()}


def test_every_entry_balances_in_its_own_currency(lines):
    by = defaultdict(lambda: Decimal("0"))
    ccy = defaultdict(set)
    for l in lines:
        by[l["entry_id"]] += Decimal(l["debit"] or "0") - Decimal(l["credit"] or "0")
        ccy[l["entry_id"]].add(l["currency"])
    assert by and all(v == 0 for v in by.values()), {k: v for k, v in by.items() if v}
    assert all(len(c) == 1 for c in ccy.values())


def test_columns_and_accounts(lines):
    assert COLUMNS == ["date", "entry_id", "account", "debit", "credit", "currency", "invoice_number",
                       "paypal_transaction_id", "event_code", "memo"]
    assert {l["account"] for l in lines} <= set(ACCOUNTS.values())
    for l in lines:
        assert bool(l["debit"]) != bool(l["credit"])  # exactly one side per line


def test_clean_payment_splits_net_and_fee_against_receivables(lines):
    rows = entry(lines, txn_id("NW-1001"))
    assert amounts(rows) == {ACCOUNTS["paypal"]: ("1205.88", "0"), ACCOUNTS["fees"]: ("44.12", "0"),
                             ACCOUNTS["receivable"]: ("0", "1250.00")}
    assert {l["invoice_number"] for l in rows} == {"NW-1001"} and rows[0]["date"] == rows[0]["date"][:10]


def test_overpayment_credits_the_excess_to_customer_credits(lines):
    rows = entry(lines, txn_id("NW-1014"))
    a = amounts(rows)
    assert a[ACCOUNTS["receivable"]] == ("0", "450.00") and a[ACCOUNTS["credits"]] == ("0", "45.00")


def test_duplicate_goes_to_customer_credits_not_receivables(lines):
    a = amounts(entry(lines, txn_id("NW-1015-dup")))
    assert a[ACCOUNTS["credits"]] == ("0", "800.00") and ACCOUNTS["receivable"] not in a


def test_unbilled_and_wrong_currency_payments_go_to_unapplied(lines):
    assert amounts(entry(lines, txn_id("unbilled-ravi")))[ACCOUNTS["unapplied"]] == ("0", "275.00")
    rows = entry(lines, txn_id("NW-1023"))
    assert amounts(rows)[ACCOUNTS["unapplied"]] == ("0", "920.00") and rows[0]["currency"] == "EUR"


def test_fee_shortfall_leaves_the_rest_open_and_says_so(lines):
    rows = entry(lines, txn_id("NW-1020"))
    assert amounts(rows)[ACCOUNTS["receivable"]] == ("0", "482.06")
    assert any("17.94" in l["memo"] and "still open" in l["memo"] for l in rows)


def test_refund_of_a_paid_invoice_reverses_the_receivable(lines):
    (refund,) = [l for l in lines if l["event_code"] == "T1107" and l["account"] == ACCOUNTS["receivable"]]
    assert refund["debit"] == "300.00" and refund["invoice_number"] == "NW-1024"
    rid = refund["entry_id"]
    assert amounts([l for l in lines if l["entry_id"] == rid])[ACCOUNTS["paypal"]] == ("0", "300.00")


def test_refund_of_a_duplicate_comes_out_of_customer_credits():
    r0, txns0 = data()
    tmpl = next(t for t in txns0 if t.event_code == "T1107")
    dup = next(t for t in txns0 if t.id == txn_id("NW-1015-dup"))
    refund = dataclasses.replace(tmpl, id="REFUNDDUP0000001", reference_id=dup.id, amount=-dup.amount,
                                 invoice_ref="NW-1015", initiated=dup.initiated)
    r, txns = data([refund])
    a = amounts(entry(journal(r, txns), "REFUNDDUP0000001"))
    assert a[ACCOUNTS["credits"]] == ("800.00", "0") and a[ACCOUNTS["paypal"]] == ("0", "800.00")


def test_pending_and_non_sales_rows_are_not_booked(lines):
    assert entry(lines, txn_id("NW-1025")) == []  # pending
    assert not [l for l in lines if l["event_code"].startswith("T04")]  # bank withdrawal


def test_csv_round_trip(lines):
    text = to_csv(lines)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == COLUMNS and len(rows) == len(lines)


def test_csv_neutralises_spreadsheet_formulas():
    text = to_csv([{c: "" for c in COLUMNS} | {"memo": "=HYPERLINK(\"http://x\")", "debit": "1.00"}])
    assert "'=HYPERLINK" in text


def test_export_endpoint(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from cashdesk.app import create_app
    from cashdesk.service import Desk
    T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    api = sign_in(TestClient(create_app(d)), d)
    r = api.get("/api/export/journal.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="cashdesk-journal-2026-11-07.csv"' == r.headers["content-disposition"]
    assert r.text.splitlines()[0] == ",".join(COLUMNS)
