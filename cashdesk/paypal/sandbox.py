"""SandboxPayPalClient: the real PayPal REST API, sandbox only.

Endpoints, headers and shapes: docs/paypal-api-notes.md (from PayPal's official OpenAPI specs).
- OAuth2 client credentials (POST /v1/oauth2/token), token cached until shortly before expiry.
- Transaction Search (GET /v1/reporting/transactions, fields=all, paged, <= 31-day window), plus
  PayPal payments recorded on invoices (Invoicing) to cover Transaction Search's reporting lag.
- Invoicing v2 list/create/send/remind/record payment; Payments v2 capture refund.
Every write sends PayPal-Request-Id. HTTP 429 is retried with Retry-After. Secrets and tokens
never appear in errors, logs or reprs.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Callable

import httpx

from ..config import sandbox_base_url
from ..models import Invoice, Money, Transaction
from .base import PayPalClient, check_window
from .parse import parse_invoice, parse_transaction

REPORTING_SCOPE = "https://uri.paypal.com/services/reporting/search/read"
MAX_RETRIES = 3


class PayPalError(Exception):
    def __init__(self, status: int, name: str = "", message: str = "", debug_id: str = "", path: str = ""):
        self.status, self.name, self.debug_id, self.path = status, name, debug_id, path
        super().__init__(f"PayPal {status} {name} on {path}: {message} (debug_id {debug_id or '-'})")


class SandboxPayPalClient(PayPalClient):
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        self.base_url = sandbox_base_url()  # refuses anything but api-m.sandbox.paypal.com
        self._auth = (client_id, client_secret)
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)
        self._sleep = sleep
        self._token: str | None = None
        self._token_exp = 0.0
        self.scope = ""

    def __repr__(self) -> str:  # never show credentials or tokens
        return f"SandboxPayPalClient(base_url={self.base_url!r})"

    @classmethod
    def from_env(cls) -> "SandboxPayPalClient":
        import os
        cid, sec = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if not cid or not sec:
            raise RuntimeError("sandbox mode needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in repo/.env")
        return cls(cid, sec)

    # ------------------------------------------------------------------ HTTP
    def _bearer(self) -> str:
        if self._token and time.monotonic() < self._token_exp:
            return self._token
        r = self._http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth,
                            headers={"Accept": "application/json"})
        if r.status_code != 200:
            self._raise(r, "/v1/oauth2/token")
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.monotonic() + max(int(body.get("expires_in", 0)) - 120, 30)
        self.scope = body.get("scope", "")
        return self._token

    def _raise(self, r: httpx.Response, path: str):
        try:
            b = r.json() if r.content else {}
        except ValueError:
            b = {}
        msg = b.get("message") or b.get("error_description") or r.reason_phrase
        detail = "; ".join(d.get("issue", "") for d in b.get("details", []) if isinstance(d, dict))
        raise PayPalError(r.status_code, b.get("name") or b.get("error", ""), f"{msg} {detail}".strip(),
                          b.get("debug_id", ""), path)

    def _call(self, method: str, path: str, *, params=None, json=None, request_id: str | None = None,
              prefer: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer:
            headers["Prefer"] = prefer
        for attempt in range(MAX_RETRIES + 1):
            r = self._http.request(method, path, params=params, json=json, headers=headers)
            if r.status_code == 429 and attempt < MAX_RETRIES:
                self._sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code == 401 and attempt == 0:  # token expired early: refresh once
                self._token = None
                headers["Authorization"] = f"Bearer {self._bearer()}"
                continue
            if r.status_code >= 400:
                self._raise(r, path)
            return r
        self._raise(r, path)

    # ------------------------------------------------------------------ reads
    def list_invoices(self) -> list[Invoice]:
        out, page = [], 1
        while True:
            r = self._call("GET", "/v2/invoicing/invoices",
                           params={"page": page, "page_size": 100, "total_required": "true", "fields": "all"})
            b = r.json()
            out += [parse_invoice(d) for d in b.get("items", [])]
            if page >= int(b.get("total_pages") or 1) or not b.get("items"):
                return out
            page += 1

    def search_transactions(self, start: datetime, end: datetime, *, merge_invoice_payments: bool = True
                            ) -> list[Transaction]:
        check_window(start, end)
        self._bearer()
        if REPORTING_SCOPE not in self.scope.split():
            raise PayPalError(403, "NOT_AUTHORIZED", "this REST app has no Transaction search permission: enable "
                              "'Transaction search' in the app settings on developer.paypal.com", "",
                              "/v1/reporting/transactions")
        fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, page = [], 1
        while True:
            r = self._call("GET", "/v1/reporting/transactions",
                           params={"start_date": fmt(start), "end_date": fmt(end), "fields": "all",
                                   "page_size": 100, "page": page})
            b = r.json()
            out += [parse_transaction(d) for d in b.get("transaction_details", [])]
            if page >= int(b.get("total_pages") or 1) or not b.get("transaction_details"):
                break
            page += 1
        if merge_invoice_payments:
            out += self._invoice_payment_gaps(out, start, end)
        return out

    def _invoice_payment_gaps(self, txns: list[Transaction], start: datetime, end: datetime) -> list[Transaction]:
        """PayPal payments recorded on invoices that Transaction Search hasn't reported yet (~3 h lag)."""
        seen = {t.id for t in txns}
        extra = []
        for inv in self.list_invoices():
            for p in (inv.raw.get("payments") or {}).get("transactions", []):
                pid, when = p.get("payment_id"), p.get("payment_date")
                if p.get("type") != "PAYPAL" or not pid or pid in seen or not when:
                    continue
                if not (start.date() <= date.fromisoformat(when[:10]) <= end.date()):
                    continue
                given, _, surname = (inv.recipient_name or "").partition(" ")
                extra.append(parse_transaction({
                    "cashdesk_source": "invoicing",
                    "transaction_info": {
                        "transaction_id": pid, "transaction_event_code": "T0006", "transaction_status": "S",
                        "transaction_initiation_date": f"{when[:10]}T00:00:00+0000",
                        "transaction_amount": p["amount"], "invoice_id": inv.number,
                    },
                    "payer_info": {"email_address": inv.recipient_email,
                                   "payer_name": {"given_name": given, "surname": surname}},
                    "cart_info": {"paypal_invoice_id": inv.id},
                }))
                seen.add(pid)
        return extra

    # ------------------------------------------------------------------ writes
    def send_reminder(self, invoice_id, *, note, request_id, subject=None):
        body = {"note": note, "send_to_invoicer": True, "send_to_recipient": True}
        if subject:
            body["subject"] = subject
        r = self._call("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json=body, request_id=request_id)
        return {"status": r.status_code}

    def create_draft_invoice(self, *, recipient_email, recipient_name, item, amount, request_id, note=None):
        given, _, surname = recipient_name.partition(" ")
        body = {
            "detail": {"currency_code": amount.currency, "invoice_date": date.today().isoformat(),
                       "note": note or "", "payment_term": {"term_type": "DUE_ON_RECEIPT"}},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname},
                                                     "email_address": recipient_email}}],
            "items": [{"name": item[:200], "quantity": "1", "unit_amount": amount.to_paypal()}],
        }
        r = self._call("POST", "/v2/invoicing/invoices", json=body, request_id=request_id,
                       prefer="return=representation")
        b = r.json()
        if "id" not in b and b.get("href"):  # minimal response: fetch the created invoice
            b = self._call("GET", httpx.URL(b["href"]).path).json()
        return parse_invoice(b)

    def send_invoice(self, invoice_id, *, request_id):
        r = self._call("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                       json={"send_to_recipient": True, "send_to_invoicer": True}, request_id=request_id)
        return {"status": r.status_code, **(r.json() if r.content else {})}

    def record_payment(self, invoice_id, amount, *, payment_date, method, note, request_id):
        body = {"method": method, "payment_date": payment_date.isoformat(), "note": note, "amount": amount.to_paypal()}
        return self._call("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", json=body,
                          request_id=request_id).json()

    def refund_capture(self, capture_id, amount, *, request_id, note_to_payer):
        body = {"amount": amount.to_paypal(), "note_to_payer": note_to_payer[:255]}
        return self._call("POST", f"/v2/payments/captures/{capture_id}/refund", json=body, request_id=request_id,
                          prefer="return=representation").json()
