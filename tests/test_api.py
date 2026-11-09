"""B1: FastAPI JSON endpoints over the desk service (fixtures mode, temp var dir, no keys)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.app import create_app
from cashdesk.paypal import FixturePayPalClient
from cashdesk.service import Desk
from tools.make_fixtures import inv_id, txn_id


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("CASHDESK_AS_OF", raising=False)
    now = [datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)]
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: now[0])
    d._now = now
    return d


@pytest.fixture
def api(desk):
    return TestClient(create_app(desk))


def test_health_still_works(api):
    assert api.get("/api/health").json()["status"] == "ok"


def test_reconciliation_returns_results_reasons_and_summary(api):
    body = api.get("/api/reconciliation").json()
    assert body["as_of"] == "2026-11-07" and body["mode"] == "fixtures"
    assert body["ai_status"].startswith("fallback")
    assert len(body["transactions"]) == 26 and len(body["invoices"]) == 26
    t = next(x for x in body["transactions"] if x["txn_id"] == txn_id("NW-1015-dup"))
    assert t["outcome"] == "duplicate" and t["reason"] and t["amount"] == {"value": "800.00", "currency": "USD"}
    assert t["payer_email"] == "leo@brightside.example.com" and t["date"] == "2026-10-29"
    s = body["summary"]
    assert s["transactions"] == 26 and 0 < s["matched_pct"] <= 100
    assert s["payments"] == 24 and s["matched"] == 15 and s["matched_pct"] == 62.5
    assert s["unreconciled"]["USD"] and s["outstanding"]["USD"]
    assert s["pending_actions"] == 0


def test_refresh_proposals_enqueues_new_ones_once(api):
    first = api.post("/api/proposals/refresh").json()
    assert first["enqueued"] == 7 and first["skipped"] == 0
    again = api.post("/api/proposals/refresh").json()
    assert again["enqueued"] == 0 and again["skipped"] == 7
    items = api.get("/api/approvals").json()["items"]
    assert len(items) == 7 and {i["status"] for i in items} == {"pending"}
    assert api.get("/api/reconciliation").json()["summary"]["pending_actions"] == 7


def test_approvals_show_the_exact_api_call_and_policy(api):
    api.post("/api/proposals/refresh")
    items = api.get("/api/approvals").json()["items"]
    refund = next(i for i in items if i["proposal"]["type"] == "refund_capture")
    call = refund["proposal"]["api_calls"][0]
    assert call["method"] == "POST" and call["path"] == f"/v2/payments/captures/{txn_id('NW-1015-dup')}/refund"
    assert refund["policy"]["allowed"] is True
    rec = next(i for i in items if i["proposal"]["type"] == "record_payment")
    assert rec["invoice_due"] == {"currency_code": "USD", "value": "1500.00"} and rec["proposal"]["needs_review"]


def test_approve_then_execute(api, desk):
    api.post("/api/proposals/refresh")
    item = next(i for i in api.get("/api/approvals").json()["items"] if i["proposal"]["type"] == "refund_capture")
    r = api.post(f"/api/approvals/{item['id']}/approve", json={"by": "owner"})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_second"  # 800.00 refund: two people
    r = api.post(f"/api/approvals/{item['id']}/approve", json={"by": "bookkeeper"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = api.post(f"/api/approvals/{item['id']}/execute")
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert r.json()["request_id"] == item["request_id"]
    assert desk.client.calls[0]["op"] == "refund"


def test_execute_without_approval_is_refused(api, desk):
    api.post("/api/proposals/refresh")
    item = api.get("/api/approvals").json()["items"][0]
    r = api.post(f"/api/approvals/{item['id']}/execute")
    assert r.status_code == 409 and desk.client.calls == []


def test_reject(api, desk):
    api.post("/api/proposals/refresh")
    item = api.get("/api/approvals").json()["items"][0]
    r = api.post(f"/api/approvals/{item['id']}/reject", json={"by": "owner", "note": "spoke to client"})
    assert r.json()["status"] == "rejected" and r.json()["note"] == "spoke to client"
    assert api.post(f"/api/approvals/{item['id']}/execute").status_code == 409
    assert desk.client.calls == []


def test_expired_approval_returns_policy_violations(api, desk):
    api.post("/api/proposals/refresh")
    item = api.get("/api/approvals").json()["items"][0]
    desk._now[0] += timedelta(hours=30)
    r = api.post(f"/api/approvals/{item['id']}/approve", json={"by": "owner"})
    assert r.status_code == 422
    assert "expired" in [v["code"] for v in r.json()["detail"]["violations"]]


def test_unknown_item_is_404(api):
    assert api.post("/api/approvals/q99999/approve", json={"by": "x"}).status_code == 404


def test_approve_requires_an_approver_name(api):
    api.post("/api/proposals/refresh")
    item = api.get("/api/approvals").json()["items"][0]
    assert api.post(f"/api/approvals/{item['id']}/approve", json={}).status_code == 422


def test_audit_endpoint_returns_records_and_verification(api):
    api.post("/api/proposals/refresh")
    item = api.get("/api/approvals").json()["items"][0]
    api.post(f"/api/approvals/{item['id']}/approve", json={"by": "owner"})
    body = api.get("/api/audit").json()
    assert body["verify"]["ok"] is True and body["verify"]["count"] == 8
    assert body["verify"]["head_hash"] == body["records"][-1]["hash"]
    assert [r["event"] for r in body["records"]][-1] == "approved"


def test_sync_reloads_and_reconciles(api):
    r = api.post("/api/sync").json()
    assert r["invoices"] == 26 and r["transactions"] == 26 and r["mode"] == "fixtures"
    assert api.get("/api/reconciliation").json()["last_sync"]


def test_as_of_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("CASHDESK_AS_OF", "2026-10-30")
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler())
    body = TestClient(create_app(d)).get("/api/reconciliation").json()
    assert body["as_of"] == "2026-10-30"
    # NW-1017 (due 10-31) is not overdue yet on 10-30
    assert next(i for i in body["invoices"] if i["invoice_number"] == "NW-1017")["outcome"] == "open"


def test_board_reads_only_the_latest_sync_not_the_whole_history(desk, monkeypatch):
    # review finding #10: sync_runs grows forever (webhooks add one per event)
    desk.sync()
    desk.sync()
    def boom():
        raise AssertionError("the board should not load every sync run")
    monkeypatch.setattr(desk.store, "sync_runs", boom)
    body = desk.reconciliation()
    assert body["last_sync"] and desk.store.last_sync()["id"] == 2
