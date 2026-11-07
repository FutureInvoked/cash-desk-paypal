"""S1a: SandboxPayPalClient against a mocked PayPal (httpx.MockTransport). No network, no keys."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cashdesk.config import load_dotenv
from cashdesk.models import Money
from cashdesk.paypal import PayPalClient, make_client
from cashdesk.paypal.sandbox import PayPalError, SandboxPayPalClient

ROOT = Path(__file__).resolve().parent.parent
FIX_INV = json.loads((ROOT / "fixtures" / "invoices.json").read_text())
FIX_TXN = json.loads((ROOT / "fixtures" / "transactions.json").read_text())
START = datetime(2026, 10, 8, tzinfo=timezone.utc)
END = datetime(2026, 11, 7, tzinfo=timezone.utc)
SCOPE = "https://uri.paypal.com/services/invoicing https://uri.paypal.com/services/reporting/search/read"


class FakePayPal:
    """Answers like api-m.sandbox.paypal.com, recording every request."""

    def __init__(self, scope=SCOPE, page_size_inv=10, fail_first_429=False):
        self.requests: list[httpx.Request] = []
        self.scope, self.page_size_inv, self.fail_first_429 = scope, page_size_inv, fail_first_429
        self.tokens = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path
        if p == "/v1/oauth2/token":
            self.tokens += 1
            return httpx.Response(200, json={"scope": self.scope, "access_token": f"TOKEN{self.tokens}",
                                             "token_type": "Bearer", "app_id": "APP-1", "expires_in": 32400})
        if self.fail_first_429:
            self.fail_first_429 = False
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"name": "RATE_LIMIT_REACHED"})
        if p == "/v2/invoicing/invoices" and req.method == "GET":
            page = int(req.url.params["page"])
            items = FIX_INV["items"][(page - 1) * self.page_size_inv: page * self.page_size_inv]
            total_pages = -(-len(FIX_INV["items"]) // self.page_size_inv)
            return httpx.Response(200, json={"items": items, "total_items": len(FIX_INV["items"]),
                                             "total_pages": total_pages, "links": []})
        if p == "/v1/reporting/transactions":
            page = int(req.url.params["page"])
            rows = FIX_TXN["transaction_details"]
            chunk = rows[(page - 1) * 20: page * 20]
            return httpx.Response(200, json={**FIX_TXN, "transaction_details": chunk, "page": page,
                                             "total_pages": -(-len(rows) // 20)})
        if p.endswith("/remind"):
            return httpx.Response(204)
        if p.endswith("/send"):
            return httpx.Response(202, json={"href": "https://www.sandbox.paypal.com/invoice/p/#X", "rel": "payer-view", "method": "GET"})
        if p.endswith("/payments"):
            return httpx.Response(200, json={"payment_id": "EXTR-123"})
        if p == "/v2/invoicing/invoices" and req.method == "POST":
            body = json.loads(req.content)
            inv = {"id": "INV2-AAAA-BBBB-CCCC-DDDD", "status": "DRAFT", **body,
                   "amount": {"currency_code": "USD", "value": body["items"][0]["unit_amount"]["value"]}}
            inv["detail"] = {**body["detail"], "invoice_number": "NW-0042"}
            return httpx.Response(201, json=inv)
        if p.endswith("/refund") and "NOPE" not in p:
            return httpx.Response(201, json={"id": "REF1", "status": "COMPLETED",
                                             "amount": json.loads(req.content)["amount"]})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "debug_id": "dbg1",
                                         "message": "The specified resource does not exist."})


def client(fake=None, **kw):
    fake = fake or FakePayPal()
    c = SandboxPayPalClient("CID", "SECRET-VALUE", transport=httpx.MockTransport(fake), sleep=lambda s: None, **kw)
    return c, fake


def test_implements_the_interface():
    c, _ = client()
    assert isinstance(c, PayPalClient) and c.mode == "sandbox"


def test_token_request_shape_and_caching():
    c, fake = client()
    c.list_invoices()
    c.list_invoices()
    tok = [r for r in fake.requests if r.url.path == "/v1/oauth2/token"]
    assert len(tok) == 1  # cached
    r = tok[0]
    assert r.method == "POST" and str(r.url).startswith("https://api-m.sandbox.paypal.com/")
    assert r.headers["authorization"] == "Basic " + base64.b64encode(b"CID:SECRET-VALUE").decode()
    assert r.headers["content-type"] == "application/x-www-form-urlencoded"
    assert r.content == b"grant_type=client_credentials"
    api = [x for x in fake.requests if x.url.path != "/v1/oauth2/token"]
    assert all(x.headers["authorization"] == "Bearer TOKEN1" for x in api)


def test_list_invoices_paginates_and_parses():
    c, fake = client()
    invs = c.list_invoices()
    assert len(invs) == 26 and invs[0].number == "NW-1001"
    pages = [r.url.params["page"] for r in fake.requests if r.url.path == "/v2/invoicing/invoices"]
    assert pages == ["1", "2", "3"]
    r = next(r for r in fake.requests if r.url.path == "/v2/invoicing/invoices")
    assert r.url.params["fields"] == "all" and r.url.params["total_required"] == "true"


def test_search_transactions_params_paging_and_parse():
    c, fake = client()
    txns = c.search_transactions(START, END, merge_invoice_payments=False)
    assert len(txns) == 26
    r = next(r for r in fake.requests if r.url.path == "/v1/reporting/transactions")
    assert r.url.params["start_date"] == "2026-10-08T00:00:00Z"
    assert r.url.params["end_date"] == "2026-11-07T00:00:00Z"
    assert r.url.params["fields"] == "all"
    assert [x.url.params["page"] for x in fake.requests if x.url.path == "/v1/reporting/transactions"] == ["1", "2"]


def test_search_window_is_limited_to_31_days():
    c, _ = client()
    with pytest.raises(ValueError, match="31 days"):
        c.search_transactions(datetime(2026, 9, 1, tzinfo=timezone.utc), END)


def test_missing_reporting_scope_gives_a_clear_error():
    c, _ = client(FakePayPal(scope="https://uri.paypal.com/services/invoicing"))
    with pytest.raises(PayPalError, match="Transaction search"):
        c.search_transactions(START, END)


def test_invoice_payments_fill_gaps_while_reporting_lags():
    # Transaction Search can lag ~3 h: a PayPal payment recorded on an invoice but missing from
    # the search results is added from Invoicing so reconciliation still sees it.
    fake = FakePayPal()
    lagging = {**FIX_TXN, "transaction_details": [t for t in FIX_TXN["transaction_details"]
                                                  if t["transaction_info"].get("invoice_id") != "NW-1001"]}
    orig = fake.__call__

    def handler(req):
        if req.url.path == "/v1/reporting/transactions":
            fake.requests.append(req)
            return httpx.Response(200, json={**lagging, "page": 1, "total_pages": 1})
        return orig(req)

    c = SandboxPayPalClient("CID", "S", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    txns = c.search_transactions(START, END)
    added = [t for t in txns if t.invoice_ref == "NW-1001"]
    assert len(added) == 1 and added[0].amount == Money(Decimal("1250.00"), "USD")
    assert added[0].raw["transaction_info"]["transaction_event_code"] == "T0006"
    assert added[0].raw.get("cashdesk_source") == "invoicing"
    # no duplicates for payments already present in Transaction Search
    assert len([t for t in txns if t.invoice_ref == "NW-1002"]) == 1


def test_writes_send_exact_paths_bodies_and_idempotency_key():
    c, fake = client()
    assert c.send_reminder("INV2-X", note="Friendly reminder", subject="Reminder", request_id="cashdesk-q1") == {"status": 204}
    refund = c.refund_capture("CAP1", Money(Decimal("800.00"), "USD"), request_id="cashdesk-q2", note_to_payer="dup")
    assert refund["status"] == "COMPLETED"
    rec = c.record_payment("INV2-X", Money(Decimal("1500.00"), "USD"), payment_date=date(2026, 11, 3),
                           method="PAYPAL", note="n", request_id="cashdesk-q3")
    assert rec == {"payment_id": "EXTR-123"}
    inv = c.create_draft_invoice(recipient_email="r@example.com", recipient_name="Ravi Menon", item="Deposit",
                                 amount=Money(Decimal("275.00"), "USD"), request_id="cashdesk-q4-create")
    assert inv.status == "DRAFT" and inv.id == "INV2-AAAA-BBBB-CCCC-DDDD"
    sent = c.send_invoice(inv.id, request_id="cashdesk-q4-send")
    assert sent["status"] == 202
    w = {r.url.path: r for r in fake.requests if r.method == "POST" and r.url.path != "/v1/oauth2/token"}
    assert json.loads(w["/v2/invoicing/invoices/INV2-X/remind"].content)["note"] == "Friendly reminder"
    assert json.loads(w["/v2/payments/captures/CAP1/refund"].content)["amount"] == {"currency_code": "USD", "value": "800.00"}
    assert w["/v2/payments/captures/CAP1/refund"].headers["paypal-request-id"] == "cashdesk-q2"
    assert w["/v2/invoicing/invoices/INV2-X/payments"].headers["paypal-request-id"] == "cashdesk-q3"
    body = json.loads(w["/v2/invoicing/invoices"].content)
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "r@example.com"
    assert w["/v2/invoicing/invoices"].headers["prefer"] == "return=representation"


def test_rate_limit_is_retried():
    c, fake = client(FakePayPal(fail_first_429=True))
    assert len(c.list_invoices()) == 26


def test_errors_carry_debug_id_but_never_secrets():
    c, _ = client()
    with pytest.raises(PayPalError) as e:
        c.refund_capture("NOPE", Money(Decimal("1.00"), "USD"), request_id="r", note_to_payer="x")
    msg = str(e.value) + repr(e.value) + repr(c)
    assert "404" in msg and "dbg1" in msg
    assert "SECRET-VALUE" not in msg and "TOKEN1" not in msg


def test_refuses_non_sandbox_base_url(monkeypatch):
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    with pytest.raises(ValueError, match="only https://api-m.sandbox.paypal.com"):
        SandboxPayPalClient("CID", "S", transport=httpx.MockTransport(FakePayPal()))


def test_make_client_returns_the_sandbox_client_when_keys_exist(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "CID")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "S")
    monkeypatch.delenv("PAYPAL_MODE", raising=False)
    assert isinstance(make_client(), SandboxPayPalClient)


def test_dotenv_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nPAYPAL_CLIENT_ID=abc\nPAYPAL_CLIENT_SECRET = 'xyz'\nEXISTING=fromfile\nBAD LINE\n\n")
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("EXISTING", "fromenv")
    monkeypatch.delenv("CASHDESK_NO_DOTENV", raising=False)
    loaded = load_dotenv(env)
    import os
    assert os.environ["PAYPAL_CLIENT_ID"] == "abc" and os.environ["PAYPAL_CLIENT_SECRET"] == "xyz"
    assert os.environ["EXISTING"] == "fromenv"  # real environment wins
    assert loaded == ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"]  # names only, never values
