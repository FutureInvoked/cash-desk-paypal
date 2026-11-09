"""Webhooks: a verified PayPal event triggers a re-sync, nothing else.

Every event is checked with PayPal's own verify-webhook-signature API (fail closed), repeated event
ids are ignored, and an event never proposes or runs an action. The webhook server is separate from
the board, so a public tunnel exposes only POST /webhooks/paypal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.paypal import FixturePayPalClient
from cashdesk.paypal.sandbox import PayPalError, SandboxPayPalClient
from cashdesk.webhooks import EVENT_TYPES, MAX_BODY

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
HEADERS = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
           "PAYPAL-TRANSMISSION-ID": "tx-1", "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJl",
           "PAYPAL-TRANSMISSION-TIME": "2026-11-09T19:00:00Z"}


def event(eid="WH-1", etype="INVOICING.INVOICE.PAID", rid="INV2-AAAA-BBBB-CCCC-DDDD"):
    # key order and spacing as PayPal sends them; the verify call must pass this text through unchanged
    return ('{"id":"%s","event_version":"1.0","create_time":"2026-11-09T19:00:00.000Z","resource_type":"invoices",'
            '"event_type":"%s","summary":"An invoice was paid","resource":{"invoice":{"id":"%s"}},"links":[]}'
            % (eid, etype, rid))


class Verifier(FixturePayPalClient):
    def __init__(self, status="SUCCESS", exc=None):
        super().__init__()
        self.status, self.exc, self.verify_calls = status, exc, []

    def verify_webhook_signature(self, raw_event: str, headers, *, webhook_id: str) -> str:
        self.verify_calls.append({"raw": raw_event, "headers": dict(headers), "webhook_id": webhook_id})
        if self.exc:
            raise self.exc
        return self.status


@pytest.fixture
def make(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-ID-123")

    def _make(client=None):
        from cashdesk.service import Desk
        from cashdesk.webhook_app import create_webhook_app
        desk = Desk(client=client or Verifier(), reconciler=FallbackReconciler(), clock=lambda: T0)
        return desk, TestClient(create_webhook_app(desk))
    return _make


def post(api, body, headers=HEADERS):
    return api.post("/webhooks/paypal", content=body.encode() if isinstance(body, str) else body,
                    headers={**headers, "Content-Type": "application/json"})


def audit_events(desk):
    return [r["event"] for r in desk.audit.records()]


# ---------- the sandbox client's verify call ----------

def test_sandbox_client_posts_the_raw_event_to_verify_webhook_signature():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AAtok", "expires_in": 3600, "scope": ""})
        seen["path"], seen["body"] = req.url.path, req.content.decode()
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    c = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    raw = event()
    assert c.verify_webhook_signature(raw, HEADERS, webhook_id="WH-ID-123") == "SUCCESS"
    assert seen["path"] == "/v1/notifications/verify-webhook-signature"
    body = json.loads(seen["body"])
    assert body == {"auth_algo": "SHA256withRSA", "cert_url": HEADERS["PAYPAL-CERT-URL"], "transmission_id": "tx-1",
                    "transmission_sig": "c2lnbmF0dXJl", "transmission_time": "2026-11-09T19:00:00Z",
                    "webhook_id": "WH-ID-123", "webhook_event": json.loads(raw)}
    assert raw in seen["body"]  # passed through byte for byte, not re-serialised


def test_fixture_client_never_verifies_anything():
    assert FixturePayPalClient().verify_webhook_signature(event(), HEADERS, webhook_id="x") == "FAILURE"


# ---------- the handler ----------

def test_verified_event_triggers_one_resync_and_is_audited(make):
    desk, api = make()
    runs = len(desk.store.sync_runs())
    r = post(api, event())
    assert r.status_code == 200 and r.json()["status"] == "synced"
    assert len(desk.store.sync_runs()) == runs + 1
    rec = [x for x in desk.audit.records() if x["event"] == "webhook_received"][-1]
    assert rec["event_id"] == "WH-1" and rec["event_type"] == "INVOICING.INVOICE.PAID"
    assert rec["verification"] == "SUCCESS" and rec["action"] == "resync" and rec["transmission_id"] == "tx-1"
    assert desk.client.verify_calls[0]["raw"] == event() and desk.client.verify_calls[0]["webhook_id"] == "WH-ID-123"


def test_repeated_event_id_is_ignored(make):
    desk, api = make()
    post(api, event())
    runs = len(desk.store.sync_runs())
    r = post(api, event())
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(desk.store.sync_runs()) == runs


def test_unverified_event_is_rejected_and_changes_nothing(make):
    desk, api = make(Verifier("FAILURE"))
    runs = len(desk.store.sync_runs())
    r = post(api, event())
    assert r.status_code == 400
    assert len(desk.store.sync_runs()) == runs
    assert audit_events(desk)[-1] == "webhook_rejected"
    # a rejected id is not remembered: a genuine retry with the same id must still be processed
    desk.client.status = "SUCCESS"
    assert post(api, event()).json()["status"] == "synced"


def test_without_a_configured_webhook_id_nothing_is_accepted(make, monkeypatch):
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID")
    desk, api = make()
    assert post(api, event()).status_code == 503
    assert desk.client.verify_calls == []


@pytest.mark.parametrize("drop", ["PAYPAL-TRANSMISSION-SIG", "PAYPAL-CERT-URL", "PAYPAL-AUTH-ALGO"])
def test_missing_signature_headers_are_rejected_before_calling_paypal(make, drop):
    desk, api = make()
    r = post(api, event(), {k: v for k, v in HEADERS.items() if k != drop})
    assert r.status_code == 400 and desk.client.verify_calls == []


def test_bad_json_and_oversized_bodies_are_rejected(make):
    desk, api = make()
    assert post(api, "{not json").status_code == 400
    assert post(api, '{"id": "x"}').status_code == 400  # no event_type
    assert post(api, "x" * (MAX_BODY + 1)).status_code == 413
    assert desk.client.verify_calls == []


def test_verified_but_unsubscribed_event_is_recorded_without_a_resync(make):
    desk, api = make()
    runs = len(desk.store.sync_runs())
    r = post(api, event(eid="WH-9", etype="CUSTOMER.DISPUTE.CREATED"))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert len(desk.store.sync_runs()) == runs


def test_paypal_verify_outage_returns_502_so_paypal_retries(make):
    desk, api = make(Verifier(exc=PayPalError(503, "SERVICE_UNAVAILABLE", "down", "", "/v1/notifications/verify-webhook-signature")))
    assert post(api, event()).status_code == 502
    desk.client.exc = None
    assert post(api, event()).json()["status"] == "synced"  # the retry is processed


def test_a_webhook_never_proposes_or_runs_an_action(make):
    desk, api = make()
    post(api, event(etype="PAYMENT.CAPTURE.COMPLETED"))
    post(api, event(eid="WH-2", etype="PAYMENT.CAPTURE.REFUNDED"))
    assert desk.queue.list() == [] and desk.client.calls == []


def test_subscribed_event_types():
    assert {"PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED", "INVOICING.INVOICE.PAID"} <= set(EVENT_TYPES)


# ---------- the separate server ----------

def test_webhook_server_exposes_only_the_webhook(make):
    desk, api = make()
    paths = {r.path for r in api.app.routes}
    assert {"/webhooks/paypal", "/webhooks/health"} <= paths
    assert not [p for p in paths if p.startswith("/api") or p == "/"]
    assert api.get("/api/approvals").status_code == 404
    assert api.get("/webhooks/health").json() == {"status": "ok", "webhook_id_set": True}


def test_board_sees_a_resync_done_by_the_webhook_server(make):
    board, _ = make(FixturePayPalClient())
    board.reconciliation()
    first = board._result_run
    hooks, api = make()  # second process, same var dir
    post(api, event())
    board.reconciliation()
    assert board._result_run != first and board._result_run == hooks.store.sync_runs()[-1]["id"]


def test_setup_tool_registers_exactly_the_subscribed_events():
    from tools.webhook_setup import create
    seen = {}

    def handler(req: httpx.Request):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AAtok", "expires_in": 3600, "scope": ""})
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(201, json={"id": "WH-NEW", "url": seen["body"]["url"]})

    c = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    assert create(c, "https://x.trycloudflare.com/webhooks/paypal")["id"] == "WH-NEW"
    assert seen["path"] == "/v1/notifications/webhooks"
    assert seen["body"]["event_types"] == [{"name": n} for n in EVENT_TYPES]
    with pytest.raises(ValueError):
        create(c, "http://x.example.com/webhooks/paypal")


# ---------- review findings #2, #4, #6, #7 ----------

def test_chunked_body_over_the_limit_is_cut_off_without_calling_paypal(make):
    desk, api = make()

    def chunks():  # no Content-Length: the request is sent chunked
        for _ in range(MAX_BODY // 4096 + 4):
            yield b"x" * 4096
    r = api.post("/webhooks/paypal", content=chunks(), headers={**HEADERS, "Content-Type": "application/json"})
    assert r.status_code == 413 and desk.client.verify_calls == []


@pytest.mark.parametrize("field", ["id", "event_type", "transmission"])
def test_oversized_fields_are_rejected_before_calling_paypal(make, field):
    desk, api = make()
    body, headers = event(), dict(HEADERS)
    if field == "id":
        body = event(eid="W" * 300)
    elif field == "event_type":
        body = event(etype="X" * 300)
    else:
        headers["PAYPAL-TRANSMISSION-ID"] = "t" * 300
    assert post(api, body, headers).status_code == 400
    assert desk.client.verify_calls == [] and audit_events(desk) == []


def test_a_flood_of_forged_events_is_rate_limited(make):
    from cashdesk.webhooks import REJECT_LIMIT
    desk, api = make(Verifier("FAILURE"))
    codes = [post(api, event(eid=f"WH-F{i}")).status_code for i in range(REJECT_LIMIT + 5)]
    assert codes[:REJECT_LIMIT] == [400] * REJECT_LIMIT and set(codes[REJECT_LIMIT:]) == {429}
    assert len(desk.client.verify_calls) == REJECT_LIMIT  # no PayPal call once limited
    assert audit_events(desk).count("webhook_rejected") == REJECT_LIMIT


def test_simultaneous_deliveries_of_one_event_sync_once(make):
    import threading
    import time as _t
    from cashdesk.webhooks import handle

    class Slow(Verifier):
        def verify_webhook_signature(self, raw_event, headers, *, webhook_id):
            _t.sleep(0.2)  # both deliveries pass verification at the same time
            return super().verify_webhook_signature(raw_event, headers, webhook_id=webhook_id)

    desk, _ = make(Slow())
    runs = len(desk.store.sync_runs())
    out = []
    ts = [threading.Thread(target=lambda: out.append(handle(desk, event().encode(), HEADERS))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # the one that lost the race isn't acknowledged while the other is still working: PayPal retries it,
    # and the retry is then a duplicate
    assert sorted((c, b["status"]) for c, b in out) in ([(200, "duplicate"), (200, "synced")],
                                                       [(200, "synced"), (409, "retry")])
    assert len(desk.store.sync_runs()) == runs + 1


def test_a_failed_sync_lets_paypals_retry_through(make):
    desk, api = make()
    real = desk.sync
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("PayPal down")
        return real()
    desk.sync = flaky
    assert post(api, event()).status_code == 502
    assert post(api, event()).json()["status"] == "synced"


def test_webhook_work_runs_off_the_event_loop(make, monkeypatch):
    import asyncio
    import cashdesk.webhook_app as wa
    desk, api = make()
    seen = {}

    def fake_handle(d, raw, headers, **kw):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False  # a worker thread: verify and sync can't block other requests
        return 200, {"status": "ok"}
    monkeypatch.setattr(wa, "handle", fake_handle)
    assert post(api, event()).status_code == 200 and seen == {"on_loop": False}


def test_verify_refreshes_a_stale_token_and_retries_429():
    state = {"verify": 0, "tokens": 0}

    def handler(req: httpx.Request):
        if req.url.path == "/v1/oauth2/token":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"A21AAtok{state['tokens']}", "expires_in": 3600, "scope": ""})
        state["verify"] += 1
        if state["verify"] == 1:
            return httpx.Response(401, json={"name": "INVALID_TOKEN"})
        if state["verify"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"name": "RATE_LIMIT_REACHED"})
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    c = SandboxPayPalClient("id", "secret", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.verify_webhook_signature(event(), HEADERS, webhook_id="WH-ID-123") == "SUCCESS"
    assert state["tokens"] == 2 and state["verify"] == 3
