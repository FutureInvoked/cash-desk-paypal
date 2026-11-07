"""S2: sandbox seed script, run against a fake PayPal. Creates ~10 Northwind invoices, idempotently."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from cashdesk.paypal.sandbox import SandboxPayPalClient
from tools.seed_sandbox import PLAN, seed


class FakeInvoicing:
    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None, str | None]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        body = json.loads(req.content) if req.content and p != "/v1/oauth2/token" else None
        self.calls.append((req.method, p, body, req.headers.get("paypal-request-id")))
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600, "scope": ""})
        if p == "/v2/invoicing/invoices" and req.method == "GET":
            return httpx.Response(200, json={"items": list(self.invoices.values()), "total_pages": 1})
        if p == "/v2/invoicing/invoices" and req.method == "POST":
            iid = f"INV2-SEED-{len(self.invoices) + 1:04d}-AAAA-BBBB"
            inv = {"id": iid, "status": "DRAFT", **body,
                   "amount": {"currency_code": body["detail"]["currency_code"],
                              "value": body["items"][0]["unit_amount"]["value"]}}
            inv["detail"] = {**body["detail"], "invoice_number": f"{1000 + len(self.invoices)}"}
            self.invoices[iid] = inv
            return httpx.Response(201, json=inv)
        if p.endswith("/send"):
            self.invoices[p.split("/")[-2]]["status"] = "SENT"
            return httpx.Response(202, json={"href": "x"})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})


def client(fake):
    return SandboxPayPalClient("CID", "S", transport=httpx.MockTransport(fake), sleep=lambda s: None)


def test_plan_covers_the_cases_we_want_to_show():
    assert 8 <= len(PLAN) <= 12
    assert {p["pay"] for p in PLAN} >= {"full", "partial", "twice", "none", "minus_fee", "other_email"}
    assert any(p["currency"] == "EUR" for p in PLAN)
    assert all(Decimal(p["amount"]) > 0 for p in PLAN)


def test_dry_run_makes_no_calls(capsys):
    fake = FakeInvoicing()
    out = seed(client(fake), payer_email="buyer@example.com", dry_run=True)
    assert out["created"] == 0 and len(out["plan"]) == len(PLAN)
    assert [c for c in fake.calls if c[1] != "/v1/oauth2/token"] == []


def test_seed_creates_and_sends_each_invoice_once():
    fake = FakeInvoicing()
    out = seed(client(fake), payer_email="buyer@example.com")
    assert out["created"] == len(PLAN) and out["skipped"] == 0
    creates = [c for c in fake.calls if c[0] == "POST" and c[1] == "/v2/invoicing/invoices"]
    sends = [c for c in fake.calls if c[1].endswith("/send")]
    assert len(creates) == len(sends) == len(PLAN)
    assert all(c[3] and c[3].startswith("cashdesk-seed-") for c in creates + sends)
    # invoices the owner will pay go to the sandbox buyer; the rest to fictional example.com customers
    to_buyer = [c for c in creates if c[2]["primary_recipients"][0]["billing_info"]["email_address"] == "buyer@example.com"]
    # "other_email": the invoice goes to the business address, the owner pays from the buyer account
    assert len(to_buyer) == sum(1 for p in PLAN if p["pay"] not in ("none", "other_email"))
    # the seed tag makes reruns idempotent
    assert all("[cashdesk-seed " in c[2]["detail"]["note"] for c in creates)


def test_rerun_skips_existing_invoices():
    fake = FakeInvoicing()
    seed(client(fake), payer_email="buyer@example.com")
    again = seed(client(fake), payer_email="buyer@example.com")
    assert again["created"] == 0 and again["skipped"] == len(PLAN)


def test_owner_payment_steps_are_written(tmp_path):
    fake = FakeInvoicing()
    out = seed(client(fake), payer_email="buyer@example.com", steps_path=tmp_path / "steps.md")
    text = (tmp_path / "steps.md").read_text()
    assert "sandbox.paypal.com" in text and "buyer@example.com" in text
    for p in PLAN:
        assert p["key"] in text
    assert "CLIENT_SECRET" not in text
