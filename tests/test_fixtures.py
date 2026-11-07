"""F3: the Northwind Studio fixtures copy PayPal's real response shapes and cover every case."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
SPEC = json.loads((ROOT / "tests" / "spec_keys.json").read_text())

REQUIRED_CASES = {
    "clean_match", "partial_payment", "overpayment", "duplicate_payment",
    "payment_without_invoice", "overdue_unpaid", "fee_only_difference",
    "payer_email_differs", "different_currency", "refunded_transaction",
    "pending_transaction",
}


def load(name):
    return json.loads((FIX / name).read_text())


def invoices():
    return load("invoices.json")["items"]


def transactions():
    return load("transactions.json")["transaction_details"]


def walk(obj, schema_key, path="$"):
    """Yield every key in obj that the official schema doesn't define."""
    spec_file, name = schema_key.split(":")
    props = SPEC.get(schema_key)
    if props is None:  # leaf schema (string/enum)
        return
    if isinstance(obj, list):
        item = props.get("__items__")
        for i, x in enumerate(obj):
            if item:
                yield from walk(x, f"{spec_file}:{item}", f"{path}[{i}]")
        return
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        if k not in props:
            yield f"{path}.{k} (not in {name})"
        elif props[k]:
            yield from walk(v, f"{spec_file}:{props[k]}", f"{path}.{k}")


def test_invoices_match_official_schema_field_for_field():
    bad = list(walk(load("invoices.json"), "invoicing_v2:invoices"))
    assert not bad, bad[:10]


def test_transactions_match_official_schema_field_for_field():
    bad = list(walk(load("transactions.json"), "reporting_transactions_v1:search_response"))
    assert not bad, bad[:10]


def test_sizes_are_realistic():
    assert 22 <= len(invoices()) <= 30
    assert 26 <= len(transactions()) <= 35


def money_objects(obj):
    if isinstance(obj, dict):
        if set(obj) == {"currency_code", "value"}:
            yield obj
        for v in obj.values():
            yield from money_objects(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from money_objects(v)


@pytest.mark.parametrize("name", ["invoices.json", "transactions.json"])
def test_money_is_string_decimal_with_currency(name):
    found = list(money_objects(load(name)))
    assert found
    for m in found:
        assert isinstance(m["value"], str)
        assert re.fullmatch(r"-?\d+\.\d{2}", m["value"]), m
        Decimal(m["value"])
        assert m["currency_code"] in {"USD", "EUR"}


def test_all_people_are_fictional_example_emails():
    text = (FIX / "invoices.json").read_text() + (FIX / "transactions.json").read_text()
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+", text))
    assert emails
    assert all(e.endswith("example.com") for e in emails), emails


def test_transaction_window_fits_one_search_call():
    s = load("transactions.json")
    start = datetime.fromisoformat(s["start_date"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(s["end_date"].replace("Z", "+00:00"))
    assert end - start <= timedelta(days=31)
    for t in transactions():
        d = datetime.fromisoformat(t["transaction_info"]["transaction_initiation_date"].replace("Z", "+00:00"))
        assert start <= d <= end


def test_transaction_keys_are_unique_and_codes_valid():
    keys = [(t["transaction_info"]["transaction_id"], t["transaction_info"]["transaction_event_code"],
             t["transaction_info"]["transaction_status"]) for t in transactions()]
    assert len(keys) == len(set(keys))
    for tid, code, status in keys:
        assert re.fullmatch(r"[A-Z0-9]{17}", tid)
        assert re.fullmatch(r"T\d{4}", code)
        assert status in {"D", "P", "S", "V"}


def test_invoice_ids_and_statuses_look_real():
    for inv in invoices():
        assert re.fullmatch(r"INV2(-[A-Z0-9]{4}){4}", inv["id"])
        assert inv["status"] in {"DRAFT", "SENT", "UNPAID", "PAID", "PARTIALLY_PAID",
                                 "PAYMENT_PENDING", "REFUNDED", "MARKED_AS_PAID"}
        assert inv["detail"]["currency_code"] == inv["amount"]["currency_code"]


def test_every_case_is_covered_and_points_at_real_rows():
    cases = load("cases.json")
    assert REQUIRED_CASES <= set(cases), REQUIRED_CASES - set(cases)
    inv_numbers = {i["detail"]["invoice_number"] for i in invoices()}
    txn_ids = {t["transaction_info"]["transaction_id"] for t in transactions()}
    for name, case in cases.items():
        assert case["invoices"] or case["transactions"], name
        assert set(case["invoices"]) <= inv_numbers, (name, case["invoices"])
        assert set(case["transactions"]) <= txn_ids, (name, case["transactions"])
        assert case["expected"], name


def test_readme_documents_every_case():
    readme = (FIX / "README.md").read_text()
    for name in load("cases.json"):
        assert f"`{name}`" in readme, name


def test_generator_is_deterministic(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_fixtures_under_test", ROOT / "tools" / "make_fixtures.py")
    gen = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["make_fixtures_under_test"] = gen
    spec.loader.exec_module(gen)
    gen.write_all(tmp_path)
    for name in ["invoices.json", "transactions.json", "cases.json", "README.md"]:
        assert (tmp_path / name).read_text() == (FIX / name).read_text(), name
