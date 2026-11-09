"""PayPal webhooks: a verified event makes the desk re-sync. Nothing else.

Flow for POST /webhooks/paypal (served by cashdesk.webhook_app, separate from the board):
  1. size, JSON and header checks           400 / 413, PayPal is not called
  2. PAYPAL_WEBHOOK_ID must be configured   503 otherwise (fail closed)
  3. PayPal verify-webhook-signature        FAILURE -> 400 and an audit record; outage -> 502 so PayPal retries
  4. event id seen before                   200 "duplicate", no second sync
  5. subscribed event type                  re-sync from PayPal's APIs, audit "webhook_received", 200 "synced"
     other verified types                   recorded, 200 "ignored"
The event body is never trusted for amounts or matching: the re-sync reads the truth from PayPal.
An event never proposes, approves or runs an action.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

EVENT_TYPES = (
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.CAPTURE.REVERSED",
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.UPDATED",
    "INVOICING.INVOICE.REFUNDED",
)
REQUIRED_HEADERS = ("PAYPAL-AUTH-ALGO", "PAYPAL-CERT-URL", "PAYPAL-TRANSMISSION-ID",
                    "PAYPAL-TRANSMISSION-SIG", "PAYPAL-TRANSMISSION-TIME")
MAX_BODY = 256 * 1024

SCHEMA = """
create table if not exists webhook_events (
    event_id text primary key,
    event_type text not null,
    resource_id text,
    received_at text not null,
    action text not null
);
"""


def _resource_id(ev: dict) -> str | None:
    r = ev.get("resource") or {}
    return r.get("id") or (r.get("invoice") or {}).get("id")


class WebhookInbox:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(self.path) as con:
            con.executescript(SCHEMA)

    def seen(self, event_id: str) -> bool:
        with sqlite3.connect(self.path) as con:
            return con.execute("select 1 from webhook_events where event_id = ?", (event_id,)).fetchone() is not None

    def record(self, ev: dict, action: str) -> None:
        with sqlite3.connect(self.path) as con:
            con.execute("insert or ignore into webhook_events values (?,?,?,?,?)",
                        (ev["id"], ev["event_type"], _resource_id(ev), datetime.now(timezone.utc).isoformat(), action))


def handle(desk, raw: bytes, headers) -> tuple[int, dict]:
    from .paypal.sandbox import PayPalError

    if len(raw) > MAX_BODY:
        return 413, {"status": "rejected", "error": "body too large"}
    try:
        text = raw.decode("utf-8")
        ev = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return 400, {"status": "rejected", "error": "not JSON"}
    if not isinstance(ev, dict) or not isinstance(ev.get("id"), str) or not isinstance(ev.get("event_type"), str):
        return 400, {"status": "rejected", "error": "not a PayPal event"}
    h = {k.upper(): v for k, v in dict(headers).items()}
    missing = [k for k in REQUIRED_HEADERS if not h.get(k)]
    if missing:
        return 400, {"status": "rejected", "error": f"missing {', '.join(missing)}"}
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return 503, {"status": "rejected", "error": "PAYPAL_WEBHOOK_ID is not configured"}

    base = {"event_id": ev["id"], "event_type": ev["event_type"], "resource_id": _resource_id(ev),
            "transmission_id": h["PAYPAL-TRANSMISSION-ID"]}
    try:
        status = desk.client.verify_webhook_signature(text, h, webhook_id=webhook_id)
    except PayPalError as e:
        return 502, {"status": "retry", "error": f"PayPal verify failed: HTTP {e.status}"}
    if status != "SUCCESS":
        desk.audit.append({"event": "webhook_rejected", **base, "verification": status})
        return 400, {"status": "rejected", "error": "signature not verified by PayPal"}

    inbox = WebhookInbox(desk.settings.var_dir / "cashdesk.db")
    if inbox.seen(ev["id"]):
        return 200, {"status": "duplicate", "event_id": ev["id"]}
    if ev["event_type"] not in EVENT_TYPES:
        inbox.record(ev, "ignored")
        desk.audit.append({"event": "webhook_received", **base, "verification": status, "action": "ignored"})
        return 200, {"status": "ignored", "event_id": ev["id"]}
    res = desk.sync()
    inbox.record(ev, "resync")
    desk.audit.append({"event": "webhook_received", **base, "verification": status, "action": "resync",
                       "synced": res})
    return 200, {"status": "synced", "event_id": ev["id"], "synced": res}
