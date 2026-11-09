"""SQLite store for ingested PayPal data (var/cashdesk.db, gitignored).

Money is stored as TEXT (exact decimal strings) and models are rebuilt from the raw
PayPal JSON with the same parser the clients use, so nothing is lost in storage.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .models import Invoice, Transaction
from .paypal.base import PayPalClient
from .paypal.parse import parse_invoice, parse_transaction

SCHEMA = """
create table if not exists invoices (
    id text primary key,
    number text not null,
    status text not null,
    currency text not null,
    total text not null,
    due text not null,
    due_date text,
    recipient_email text,
    raw text not null,
    synced_at text not null
);
create table if not exists transactions (
    id text not null,
    event_code text not null,
    status text not null,
    initiated text not null,
    currency text not null,
    amount text not null,
    payer_email text,
    raw text not null,
    synced_at text not null,
    primary key (id, event_code, status)
);
create index if not exists transactions_initiated on transactions(initiated);
create table if not exists sync_runs (
    id integer primary key autoincrement,
    at text not null,
    mode text not null,
    window_start text not null,
    window_end text not null,
    invoices integer not null,
    transactions integer not null
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            path = load_settings().var_dir / "cashdesk.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- ingest ----
    def sync(self, client: PayPalClient, start: datetime, end: datetime) -> dict:
        invoices = client.list_invoices()
        txns = client.search_transactions(start, end)
        now = _iso(datetime.now(timezone.utc))
        with self._conn() as con:
            for inv in invoices:
                con.execute(
                    """insert into invoices (id, number, status, currency, total, due, due_date,
                                             recipient_email, raw, synced_at)
                       values (?,?,?,?,?,?,?,?,?,?)
                       on conflict(id) do update set number=excluded.number, status=excluded.status,
                         currency=excluded.currency, total=excluded.total, due=excluded.due,
                         due_date=excluded.due_date, recipient_email=excluded.recipient_email,
                         raw=excluded.raw, synced_at=excluded.synced_at""",
                    (inv.id, inv.number, inv.status, inv.total.currency, str(inv.total.amount),
                     str(inv.due.amount), inv.due_date.isoformat() if inv.due_date else None,
                     inv.recipient_email, json.dumps(inv.raw), now),
                )
            # The search result is authoritative for its window: replace it, so a status change
            # upstream (P -> S changes the key) never leaves a stale row behind.
            con.execute("delete from transactions where initiated >= ? and initiated <= ?",
                        (_iso(start), _iso(end)))
            for t in txns:
                con.execute(
                    """insert or replace into transactions (id, event_code, status, initiated, currency,
                                                           amount, payer_email, raw, synced_at)
                       values (?,?,?,?,?,?,?,?,?)""",
                    (t.id, t.event_code, t.status, _iso(t.initiated), t.amount.currency,
                     str(t.amount.amount), t.payer_email, json.dumps(t.raw), now),
                )
            con.execute(
                """insert into sync_runs (at, mode, window_start, window_end, invoices, transactions)
                   values (?,?,?,?,?,?)""",
                (now, client.mode, _iso(start), _iso(end), len(invoices), len(txns)),
            )
        return {"invoices": len(invoices), "transactions": len(txns), "mode": client.mode}

    # ---- reads ----
    def invoices(self) -> list[Invoice]:
        with self._conn() as con:
            return [parse_invoice(json.loads(r["raw"])) for r in con.execute("select raw from invoices order by number")]

    def invoice(self, invoice_id: str) -> Invoice | None:
        with self._conn() as con:
            r = con.execute("select raw from invoices where id = ?", (invoice_id,)).fetchone()
        return parse_invoice(json.loads(r["raw"])) if r else None

    def transactions(self) -> list[Transaction]:
        with self._conn() as con:
            rows = con.execute("select raw from transactions order by initiated, id, event_code").fetchall()
        return [parse_transaction(json.loads(r["raw"])) for r in rows]

    def transaction(self, txn_id: str) -> Transaction | None:
        with self._conn() as con:
            r = con.execute("select raw from transactions where id = ? order by initiated limit 1",
                            (txn_id,)).fetchone()
        return parse_transaction(json.loads(r["raw"])) if r else None

    def last_sync(self) -> dict | None:
        with self._conn() as con:
            r = con.execute("select * from sync_runs order by id desc limit 1").fetchone()
        return dict(r) if r else None

    def sync_runs(self) -> list[dict]:
        with self._conn() as con:
            return [dict(r) for r in con.execute("select * from sync_runs order by id")]
