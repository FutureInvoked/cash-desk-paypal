"""P5: public-demo mode (hosted on Render). Sample data only, a private workspace per visitor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
CREDS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY",
         "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "CODEX_BIN", "PAYPAL_MODE", "CASHDESK_AS_OF",
         "CASHDESK_ALLOWED_HOSTS", "RENDER_EXTERNAL_HOSTNAME", "CASHDESK_BEHIND_PROXY")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for v in CREDS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CASHDESK_PUBLIC_DEMO", "1")
    monkeypatch.setenv("PAYPAL_MODE", "fixtures")
    monkeypatch.setenv("AI_PROVIDER", "none")
    return monkeypatch


def make(env, **extra):
    from cashdesk.app import create_app
    for k, v in extra.items():
        env.setenv(k, v)
    app = create_app()
    return app


def visitor(app, **kw):
    return TestClient(app, base_url=kw.pop("base_url", "http://127.0.0.1:8000"), **kw)


def alex(c, who="alex"):
    r = c.post("/api/demo-login", json={"username": who})
    assert r.status_code == 200, r.text
    return c


# ---------- guardrails ----------

@pytest.mark.parametrize("var, value", [
    ("PAYPAL_CLIENT_ID", "A-client-id"), ("PAYPAL_CLIENT_SECRET", "a-secret"), ("ANTHROPIC_API_KEY", "a-key"),
    ("OPENAI_API_KEY", "a-key"), ("AI_API_KEY", "a-key"), ("CODEX_BIN", "/usr/bin/true"),
    ("PAYPAL_MODE", "sandbox"), ("AI_PROVIDER", "anthropic"),
])
def test_public_demo_refuses_any_credential_or_real_mode(env, var, value):
    from cashdesk.config import check_startup
    env.setenv(var, value)
    errors, _ = check_startup()
    assert any(var in e and "public demo" in e.lower() for e in errors), errors


def test_public_demo_starts_clean_and_refuses_to_start_with_a_key(env):
    from cashdesk.config import check_startup
    assert check_startup() == ([], [])
    env.setenv("ANTHROPIC_API_KEY", "a-key")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        with TestClient(make(env)):
            pass


# ---------- workspaces ----------

def test_nothing_is_created_until_someone_signs_in(env):
    app = make(env)
    c = visitor(app)
    assert c.get("/").status_code == 200 and c.get("/api/health").status_code == 200
    assert c.get("/api/me").json() == {"user": None}
    opts = c.get("/api/login-options").json()
    assert opts["public_demo"] is True and [u["username"] for u in opts["demo_users"]] == ["alex", "sam"]
    assert c.get("/api/approvals").status_code == 401
    assert c.post("/api/login", json={"username": "alex", "password": "anything at all"}).status_code == 401
    assert app.state.demo_pool.count() == 0


def test_each_visitor_gets_a_private_workspace(env):
    app = make(env)
    a, b = alex(visitor(app)), alex(visitor(app))
    assert a.post("/api/proposals/refresh").json()["enqueued"] == 7
    assert len(a.get("/api/approvals").json()["items"]) == 7
    assert b.get("/api/approvals").json()["items"] == []
    assert app.state.demo_pool.count() == 2


def test_switching_demo_user_keeps_the_workspace(env):
    app = make(env)
    c = alex(visitor(app))
    c.post("/api/proposals/refresh")
    item = next(i for i in c.get("/api/approvals").json()["items"] if i["proposal"]["type"] == "refund_capture")
    assert c.post(f"/api/approvals/{item['id']}/approve", json={"proposal_hash": item["proposal_hash"]}).json()["status"] == "awaiting_second"
    alex(c, "sam")
    r = c.post(f"/api/approvals/{item['id']}/approve", json={"proposal_hash": item["proposal_hash"]})
    assert r.json()["status"] == "approved"
    assert c.post(f"/api/approvals/{item['id']}/execute").json()["status"] == "executed"
    assert app.state.demo_pool.count() == 1


def test_workspaces_are_capped_least_recently_used_goes_first(env):
    app = make(env, CASHDESK_DEMO_MAX_WORKSPACES="3")
    first = alex(visitor(app))
    others = [alex(visitor(app)) for _ in range(3)]
    assert app.state.demo_pool.count() == 3
    assert first.get("/api/me").json() == {"user": None}  # its workspace made room
    assert all(o.get("/api/me").json()["user"]["username"] == "alex" for o in others)


def test_an_idle_workspace_is_dropped_after_two_hours(env):
    app = make(env)
    pool = app.state.demo_pool
    now = [datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)]
    pool.clock = lambda: now[0]
    c = alex(visitor(app))
    path = pool.path_of(c.cookies.get("cashdesk_demo"))
    assert path.exists()
    now[0] += timedelta(hours=1, minutes=59)
    assert c.get("/api/me").json()["user"] is not None
    now[0] += timedelta(hours=2, minutes=1)
    assert c.get("/api/me").json() == {"user": None}
    assert not path.exists() and pool.count() == 0


def test_reset_demo_drops_the_workspace(env):
    app = make(env)
    c = alex(visitor(app))
    path = app.state.demo_pool.path_of(c.cookies.get("cashdesk_demo"))
    r = c.post("/api/demo-reset")
    assert r.status_code == 200 and not path.exists() and app.state.demo_pool.count() == 0
    assert c.get("/api/me").json() == {"user": None}


def test_one_address_can_only_open_so_many_workspaces(env):
    app = make(env, CASHDESK_DEMO_NEW_PER_HOUR="3")
    for _ in range(3):
        alex(visitor(app))
    r = visitor(app).post("/api/demo-login", json={"username": "alex"})
    assert r.status_code == 429 and "workspace" in r.json()["detail"]


def test_requests_per_address_are_throttled(env):
    app = make(env, CASHDESK_DEMO_RATE="5")
    c = visitor(app)
    codes = [c.get("/api/login-options").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert c.get("/api/health").status_code == 200  # health checks are never throttled


def test_cookie_token_is_not_the_folder_name_and_bad_cookies_are_ignored(env):
    app = make(env)
    c = alex(visitor(app))
    token = c.cookies.get("cashdesk_demo")
    assert token and token not in str(app.state.demo_pool.path_of(token))
    d = visitor(app)
    d.cookies.set("cashdesk_demo", "../../etc")
    assert d.get("/api/me").json() == {"user": None}


def test_a_fresh_process_starts_with_no_old_workspaces(env, tmp_path):
    app = make(env)
    alex(visitor(app))
    root = app.state.demo_pool.root
    assert any(root.iterdir())
    make(env)
    assert not root.exists() or not any(root.iterdir())


# ---------- behind Render's proxy ----------

HOST = "cash-desk-paypal.onrender.com"


def proxied(app, xff="198.51.100.20"):
    return TestClient(app, base_url=f"http://{HOST}",
                      headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": xff})


def test_behind_the_proxy_https_and_the_render_host_are_honoured(env):
    app = make(env, CASHDESK_BEHIND_PROXY="1", RENDER_EXTERNAL_HOSTNAME=HOST)
    c = proxied(app)
    r = c.post("/api/demo-login", json={"username": "alex"}, headers={"Origin": f"https://{HOST}"})
    assert r.status_code == 200
    cookies = " ".join(v.lower() for k, v in r.headers.multi_items() if k.lower() == "set-cookie")
    assert cookies.count("secure") == 2  # session and workspace cookies
    # a write from the same https origin is same-origin behind the proxy
    c.cookies.update({k: v for k, v in r.cookies.items()})


def test_the_rightmost_forwarded_address_is_the_client(env):
    app = make(env, CASHDESK_BEHIND_PROXY="1", RENDER_EXTERNAL_HOSTNAME=HOST, CASHDESK_DEMO_RATE="3")
    # a visitor can't dodge the throttle by sending their own X-Forwarded-For
    codes = [proxied(app, xff=f"10.0.0.{n}, 203.0.113.9").get("/api/login-options").status_code for n in range(4)]
    assert codes == [200, 200, 200, 429]


def test_on_render_the_client_is_cloudflares_connecting_ip(env):
    """On Render, X-Forwarded-For is "<client>, <Cloudflare edge>": the last entry is an edge shared by
    many visitors, so per-address limits would become shared limits. Cloudflare sets (and overwrites)
    CF-Connecting-IP; that is the client."""
    app = make(env, CASHDESK_BEHIND_PROXY="1", RENDER_EXTERNAL_HOSTNAME=HOST, CASHDESK_DEMO_RATE="3")
    edge = "104.22.17.40"

    def via_cloudflare(client, spoof="10.0.0.1"):
        return TestClient(app, base_url=f"http://{HOST}", headers={
            "X-Forwarded-Proto": "https", "X-Forwarded-For": f"{spoof}, {client}, {edge}",
            "CF-Connecting-IP": client})
    # five visitors behind the same edge each get their own budget
    assert [via_cloudflare(f"198.51.100.{n}").get("/api/login-options").status_code for n in range(5)] == [200] * 5
    # one visitor can't refresh their budget by changing the X-Forwarded-For they send
    codes = [via_cloudflare("203.0.113.9", spoof=f"10.0.0.{n}").get("/api/login-options").status_code for n in range(4)]
    assert codes == [200, 200, 200, 429]


def test_true_client_ip_is_used_when_cf_connecting_ip_is_missing(env):
    app = make(env, CASHDESK_BEHIND_PROXY="1", RENDER_EXTERNAL_HOSTNAME=HOST, CASHDESK_DEMO_RATE="2")
    edge = "104.22.17.40"
    c = lambda ip: TestClient(app, base_url=f"http://{HOST}", headers={  # noqa: E731
        "X-Forwarded-Proto": "https", "X-Forwarded-For": f"{ip}, {edge}", "True-Client-IP": ip})
    assert [c(f"198.51.100.{n}").get("/api/login-options").status_code for n in range(3)] == [200] * 3


def test_without_the_proxy_flag_forwarded_headers_are_ignored(env):
    app = make(env, CASHDESK_ALLOWED_HOSTS=HOST)
    r = proxied(app).post("/api/demo-login", json={"username": "alex"})
    assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()


def test_health_answers_any_host_but_nothing_else_does(env):
    app = make(env)
    c = TestClient(app, base_url="http://10.214.3.7:10000")
    assert c.get("/api/health").status_code == 200
    assert c.get("/").status_code == 400
    assert c.get("/api/login-options").status_code == 400


def test_the_render_host_is_allowed_only_when_render_names_it(env):
    assert TestClient(make(env), base_url=f"http://{HOST}").get("/").status_code == 400
    assert TestClient(make(env, RENDER_EXTERNAL_HOSTNAME=HOST), base_url=f"http://{HOST}").get("/").status_code == 200


# ---------- the board and the blueprint ----------

def test_board_shows_the_demo_banner_and_reset():
    html = (REPO / "static" / "index.html").read_text()
    assert 'id="demo-banner"' in html and "Public demo with sample data" in html
    assert "/api/demo-reset" in html and 'id="demo-reset"' in html and "public_demo" in html


def test_render_blueprint():
    y = (REPO / "render.yaml").read_text()
    for needle in ("type: web", "plan: free", "runtime: python", "healthCheckPath: /api/health", "$PORT",
                   "CASHDESK_PUBLIC_DEMO", "CASHDESK_BEHIND_PROXY", "PAYPAL_MODE", "fixtures", "AI_PROVIDER",
                   "PYTHON_VERSION", "requirements.lock"):
        assert needle in y, needle
    for secret in ("PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert secret not in y, secret
    import re
    assert not re.search(r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{16,}", y)  # no API-key-shaped string
