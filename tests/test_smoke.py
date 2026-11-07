from fastapi.testclient import TestClient

from cashdesk.app import create_app


def test_health_reports_ok_and_mode():
    client = TestClient(create_app())
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["paypal_mode"] in {"fixtures", "sandbox"}


def test_index_page_is_served():
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Cash Desk for PayPal" in resp.text
