"""PayPal webhooks: a verified event makes the desk re-sync. Nothing else.

Flow for POST /webhooks/paypal (served by cashdesk.webhook_app, separate from the board):
  1. size, JSON and header checks           400 / 413, PayPal is not called
  2. PAYPAL_WEBHOOK_ID must be configured   503 otherwise (fail closed)
  3. PayPal verify-webhook-signature        FAILURE -> 400 and an audit record; outage -> 502 so PayPal retries
  0. more than REJECT_LIMIT unverified events in REJECT_WINDOW -> 429, PayPal is not called
  4. event id already claimed (atomically)  200 "duplicate", no second sync; a failed sync gives it back
  5. subscribed event type                  re-sync from PayPal's APIs, audit "webhook_received", 200 "synced"
     other verified types                   recorded, 200 "ignored"
The event body is never trusted for amounts or matching: the re-sync reads the truth from PayPal.
An event never proposes, approves or runs an action.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
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
MAX_ID = 128          # event id and transmission id (PayPal's are ~40 characters)
MAX_TYPE = 100        # event type names are ~40 characters
REJECT_LIMIT = 20     # unverified events allowed per window before answering 429 without calling PayPal
REJECT_WINDOW = 600.0

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
    r = ev.get("resource")
    if not isinstance(r, dict):
        return None
    inv = r.get("invoice")
    rid = r.get("id") or (inv.get("id") if isinstance(inv, dict) else None)
    return rid[:MAX_ID] if isinstance(rid, str) else None


class WebhookInbox:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(self.path) as con:
            con.executescript(SCHEMA)

    def _con(self):
        return closing(sqlite3.connect(self.path, timeout=30))

    def claim(self, ev: dict) -> bool:
        """Atomically take an event id; False if another delivery already has it."""
        with self._con() as con, con:
            cur = con.execute("insert or ignore into webhook_events values (?,?,?,?,?)",
                              (ev["id"], ev["event_type"], _resource_id(ev),
                               datetime.now(timezone.utc).isoformat(), "processing"))
            return cur.rowcount == 1

    def finish(self, event_id: str, action: str) -> None:
        with self._con() as con, con:
            con.execute("update webhook_events set action = ? where event_id = ?", (action, event_id))

    def release(self, event_id: str) -> None:
        """Forget a claim whose work failed, so PayPal's retry is processed."""
        with self._con() as con, con:
            con.execute("delete from webhook_events where event_id = ?", (event_id,))


class RejectLimiter:
    """Counts unverified deliveries per source; past the limit, that source gets 429 without a PayPal
    call or an audit write. Per source, so a flood from one address can't lock out PayPal's own."""

    def __init__(self, limit: int = REJECT_LIMIT, window: float = REJECT_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, source: str = "-") -> bool:
        with self.lock:
            now = self.clock()
            q = self.hits.get(source)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(self.hits) > 10_000:  # bound memory: forget sources with nothing recent
                self.hits = {k: v for k, v in self.hits.items() if v}
            return bool(q) and len(q) >= self.limit

    def add(self, source: str = "-") -> None:
        with self.lock:
            self.hits.setdefault(source, deque()).append(self.clock())


def handle(desk, raw: bytes, headers, limiter: RejectLimiter | None = None, source: str = "-") -> tuple[int, dict]:
    from .paypal.sandbox import PayPalError

    if limiter is None:
        limiter = getattr(desk, "_webhook_limiter", None) or RejectLimiter()
        desk._webhook_limiter = limiter

    if len(raw) > MAX_BODY:
        return 413, {"status": "rejected", "error": "body too large"}
    try:
        text = raw.decode("utf-8")
        ev = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return 400, {"status": "rejected", "error": "not JSON"}
    if not isinstance(ev, dict) or not isinstance(ev.get("id"), str) or not isinstance(ev.get("event_type"), str):
        return 400, {"status": "rejected", "error": "not a PayPal event"}
    if len(ev["id"]) > MAX_ID or len(ev["event_type"]) > MAX_TYPE:
        return 400, {"status": "rejected", "error": "field too long"}
    h = {k.upper(): v for k, v in dict(headers).items()}
    missing = [k for k in REQUIRED_HEADERS if not h.get(k)]
    if missing:
        return 400, {"status": "rejected", "error": f"missing {', '.join(missing)}"}
    if any(len(h[k]) > 2048 for k in REQUIRED_HEADERS) or len(h["PAYPAL-TRANSMISSION-ID"]) > MAX_ID:
        return 400, {"status": "rejected", "error": "header too long"}
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return 503, {"status": "rejected", "error": "PAYPAL_WEBHOOK_ID is not configured"}

    if limiter.blocked(source):
        return 429, {"status": "rejected", "error": "too many unverified events; try later"}

    base = {"event_id": ev["id"], "event_type": ev["event_type"], "resource_id": _resource_id(ev),
            "transmission_id": h["PAYPAL-TRANSMISSION-ID"]}
    try:
        status = desk.client.verify_webhook_signature(text, h, webhook_id=webhook_id)
    except PayPalError as e:
        return 502, {"status": "retry", "error": f"PayPal verify failed: HTTP {e.status}"}
    if status != "SUCCESS":
        limiter.add(source)
        desk.audit.append({"event": "webhook_rejected", **base, "verification": status})
        return 400, {"status": "rejected", "error": "signature not verified by PayPal"}

    inbox = WebhookInbox(desk.settings.var_dir / "cashdesk.db")
    if not inbox.claim(ev):  # atomic: of two simultaneous deliveries, exactly one goes on
        return 200, {"status": "duplicate", "event_id": ev["id"]}
    if ev["event_type"] not in EVENT_TYPES:
        inbox.finish(ev["id"], "ignored")
        desk.audit.append({"event": "webhook_received", **base, "verification": status, "action": "ignored"})
        return 200, {"status": "ignored", "event_id": ev["id"]}
    try:
        res = desk.sync()
    except Exception as e:  # PayPal down mid-sync: give the id back so PayPal's retry is processed
        inbox.release(ev["id"])
        return 502, {"status": "retry", "error": f"sync failed: {type(e).__name__}"}
    inbox.finish(ev["id"], "resync")
    desk.audit.append({"event": "webhook_received", **base, "verification": status, "action": "resync",
                       "synced": res})
    return 200, {"status": "synced", "event_id": ev["id"], "synced": res}
