"""F5: ingest PayPal data into SQLite; re-sync is idempotent; money round-trips as Decimal."""

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.store import Store
from tools.make_fixtures import inv_id, txn_id

START = datetime(2026, 10, 8, tzinfo=timezone.utc)
END = datetime(2026, 11, 7, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cashdesk.db")


def test_sync_loads_every_invoice_and_transaction(store):
    result = store.sync(FixturePayPalClient(), START, END)
    assert result == {"invoices": 26, "transactions": 26, "mode": "fixtures"}
    assert len(store.invoices()) == 26
    assert len(store.transactions()) == 26


def test_resync_is_idempotent(store):
    c = FixturePayPalClient()
    store.sync(c, START, END)
    store.sync(c, START, END)
    assert len(store.invoices()) == 26
    assert len(store.transactions()) == 26
    assert len(store.sync_runs()) == 2


def test_models_round_trip_with_decimal_money(store):
    store.sync(FixturePayPalClient(), START, END)
    inv = store.invoice(inv_id("NW-1013"))
    assert inv.number == "NW-1013" and inv.due == Money(Decimal("600.00"), "USD")
    t = store.transaction(txn_id("NW-1020"))
    assert t.amount == Money(Decimal("482.06"), "USD")
    assert isinstance(t.amount.amount, Decimal)


def test_money_is_stored_as_text_not_real(store):
    store.sync(FixturePayPalClient(), START, END)
    con = sqlite3.connect(store.path)
    types = {r[0] for r in con.execute("select typeof(amount) from transactions")}
    assert types == {"text"}
    types = {r[0] for r in con.execute("select typeof(total) from invoices")}
    assert types == {"text"}


def test_status_change_updates_rather_than_duplicates(store, tmp_path):
    c = FixturePayPalClient()
    store.sync(c, START, END)
    # PayPal later reports the pending eCheck as completed (new status => new key upstream)
    for d in c._txns:
        if d["transaction_info"]["transaction_id"] == txn_id("NW-1025"):
            d["transaction_info"]["transaction_status"] = "S"
    for d in c._invoices:
        if d["detail"]["invoice_number"] == "NW-1025":
            d["status"] = "PAID"
    store.sync(c, START, END)
    rows = [t for t in store.transactions() if t.id == txn_id("NW-1025")]
    assert [t.status for t in rows] == ["S"]
    assert store.invoice(inv_id("NW-1025")).status == "PAID"


def test_sync_only_replaces_transactions_inside_the_window(store):
    c = FixturePayPalClient()
    store.sync(c, START, END)
    store.sync(c, datetime(2026, 11, 1, tzinfo=timezone.utc), END)
    assert len(store.transactions()) == 26  # older rows outside the window are kept


def test_same_transaction_id_with_two_event_codes_is_kept_twice(store):
    c = FixturePayPalClient()
    dup = copy.deepcopy(c._txns[0])
    dup["transaction_info"]["transaction_event_code"] = "T1105"  # e.g. a non-balance hold row
    c._txns.append(dup)
    store.sync(c, START, END)
    assert len(store.transactions()) == 27


def test_raw_json_is_kept(store):
    store.sync(FixturePayPalClient(), START, END)
    assert store.invoice(inv_id("NW-1001")).raw["detail"]["invoice_number"] == "NW-1001"
    assert store.transaction(txn_id("NW-1001")).raw["transaction_info"]["invoice_id"] == "NW-1001"


def test_default_db_lives_under_var(monkeypatch, tmp_path):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path / "v"))
    s = Store()
    assert s.path == tmp_path / "v" / "cashdesk.db"
    assert s.path.exists()
