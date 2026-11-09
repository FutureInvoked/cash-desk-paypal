"""FixturePayPalClient: reads recorded PayPal responses from fixtures/; writes are simulated.

Writes never touch the files on disk. They are recorded in `self.calls` with the exact path
and headers the sandbox client would send, and are idempotent on request_id like PayPal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from ..config import load_settings
from ..models import Invoice, Money, Transaction
from .base import PayPalClient, check_window
from .parse import parse_invoice, parse_transaction


def _fake_id(prefix: str, seed: str, n: int = 17) -> str:
    return prefix + hashlib.sha256(seed.encode()).hexdigest().upper()[:n]


class FixturePayPalClient(PayPalClient):
    mode = "fixtures"

    def __init__(self, fixtures_dir: Path | None = None):
        self.dir = Path(fixtures_dir or load_settings().fixtures_dir)
        self._invoices = json.loads((self.dir / "invoices.json").read_text())["items"]
        self._txns = json.loads((self.dir / "transactions.json").read_text())["transaction_details"]
        self._drafts: dict[str, dict] = {}
        self._results: dict[str, dict] = {}
        self.calls: list[dict] = []

    # ---- reads ----
    def list_invoices(self) -> list[Invoice]:
        return [parse_invoice(d) for d in self._invoices] + [parse_invoice(d) for d in self._drafts.values()]

    def search_transactions(self, start: datetime, end: datetime) -> list[Transaction]:
        check_window(start, end)
        out = [parse_transaction(d) for d in self._txns]
        return [t for t in out if start <= t.initiated <= end]

    # ---- writes (simulated) ----
    def _known_invoice(self, invoice_id: str) -> None:
        if invoice_id not in self._drafts and not any(d["id"] == invoice_id for d in self._invoices):
            raise KeyError(f"no invoice {invoice_id}")

    def _once(self, op: str, method: str, path: str, request_id: str, body: dict | None, result) -> dict:
        if request_id in self._results:
            return self._results[request_id]
        res = result() if callable(result) else result
        self.calls.append({"op": op, "method": method, "path": path, "body": body,
                           "headers": {"PayPal-Request-Id": request_id}})
        self._results[request_id] = res
        return res

    def execute_call(self, method, path, body, *, request_id):
        import re
        if method == "POST" and path == "/v2/invoicing/invoices":
            def created():
                iid = "INV2-" + "-".join(_fake_id("", request_id, 16)[i:i + 4] for i in range(0, 16, 4))
                doc = json.loads(json.dumps(body))
                doc.update(id=iid, status="DRAFT")
                doc.setdefault("detail", {})["invoice_number"] = f"NW-D{len(self._drafts) + 1:03d}"
                self._drafts[iid] = doc
                return {"id": iid, "status": "DRAFT", "detail": doc["detail"]}
            return self._once("create_invoice", method, path, request_id, body, created)
        m = re.fullmatch(r"/v2/invoicing/invoices/([^/]+)", path)
        if method == "GET" and m:
            iid = m.group(1)
            self._known_invoice(iid)
            doc = self._drafts.get(iid) or next(d for d in self._invoices if d["id"] == iid)
            return json.loads(json.dumps(doc))
        m = re.fullmatch(r"/v2/invoicing/invoices/([^/]+)/(send|remind|payments)", path)
        if method == "POST" and m:
            iid, what = m.groups()
            self._known_invoice(iid)
            if what == "send":
                def sent():
                    if iid in self._drafts:
                        self._drafts[iid]["status"] = "SENT"
                    return {"status": 202, "href": f"https://www.sandbox.paypal.com/invoice/p/#{iid}"}
                return self._once("send_invoice", method, path, request_id, body, sent)
            if what == "remind":
                return self._once("remind", method, path, request_id, body, {"status": 204})
            def recorded():
                pid = _fake_id("EXTR-", request_id, 16)
                doc = self._drafts.get(iid) or next(d for d in self._invoices if d["id"] == iid)
                doc.setdefault("payments", {}).setdefault("transactions", []).append(
                    {"payment_id": pid, "type": "EXTERNAL", **(body or {})})
                return {"payment_id": pid}
            return self._once("record_payment", method, path, request_id, body, recorded)
        m = re.fullmatch(r"/v2/payments/captures/([^/]+)/refund", path)
        if method == "POST" and m:
            cid = m.group(1)
            if not any(d["transaction_info"]["transaction_id"] == cid for d in self._txns):
                raise KeyError(f"no capture {cid}")
            return self._once("refund", method, path, request_id, body,
                              {"id": _fake_id("", "refund:" + request_id), "status": "COMPLETED",
                               "amount": (body or {}).get("amount")})
        raise KeyError(f"no simulated endpoint for {method} {path}")

    def send_reminder(self, invoice_id, *, note, request_id, subject=None):
        self._known_invoice(invoice_id)
        body = {"note": note, "send_to_invoicer": True, "send_to_recipient": True}
        if subject:
            body["subject"] = subject
        return self._once("remind", "POST", f"/v2/invoicing/invoices/{invoice_id}/remind", request_id, body,
                          {"status": 204})

    def create_draft_invoice(self, *, recipient_email, recipient_name, item, amount, request_id, note=None):
        if request_id in self._results:
            return parse_invoice(self._drafts[self._results[request_id]["id"]])
        iid = "INV2-" + "-".join(_fake_id("", request_id, 16)[i:i + 4] for i in range(0, 16, 4))
        given, _, surname = recipient_name.partition(" ")
        doc = {
            "id": iid,
            "status": "DRAFT",
            "detail": {"invoice_number": f"NW-D{len(self._drafts) + 1:03d}", "currency_code": amount.currency,
                       "invoice_date": date.today().isoformat(), "note": note or "",
                       "payment_term": {"term_type": "DUE_ON_RECEIPT"}},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname},
                                                     "email_address": recipient_email}}],
            "items": [{"name": item, "quantity": "1", "unit_amount": amount.to_paypal()}],
            "amount": {"currency_code": amount.currency, "value": amount.to_paypal()["value"]},
            "due_amount": amount.to_paypal(),
        }
        self._drafts[iid] = doc
        self._once("create_invoice", "POST", "/v2/invoicing/invoices", request_id,
                   {k: doc[k] for k in ("detail", "primary_recipients", "items")}, {"id": iid})
        return parse_invoice(doc)

    def send_invoice(self, invoice_id, *, request_id):
        self._known_invoice(invoice_id)

        def result():
            if invoice_id in self._drafts:
                self._drafts[invoice_id]["status"] = "SENT"
            return {"status": 202, "href": f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"}

        return self._once("send_invoice", "POST", f"/v2/invoicing/invoices/{invoice_id}/send", request_id,
                          {"send_to_recipient": True}, result)

    def record_payment(self, invoice_id, amount, *, payment_date, method, note, request_id):
        self._known_invoice(invoice_id)
        body = {"method": method, "payment_date": payment_date.isoformat(), "note": note, "amount": amount.to_paypal()}
        return self._once("record_payment", "POST", f"/v2/invoicing/invoices/{invoice_id}/payments", request_id,
                          body, {"payment_id": _fake_id("EXTR-", request_id, 16)})

    def refund_capture(self, capture_id, amount, *, request_id, note_to_payer):
        if not any(d["transaction_info"]["transaction_id"] == capture_id for d in self._txns):
            raise KeyError(f"no capture {capture_id}")
        body = {"amount": amount.to_paypal(), "note_to_payer": note_to_payer}
        return self._once("refund", "POST", f"/v2/payments/captures/{capture_id}/refund", request_id, body,
                          {"id": _fake_id("", "refund:" + request_id), "status": "COMPLETED",
                           "amount": amount.to_paypal()})
