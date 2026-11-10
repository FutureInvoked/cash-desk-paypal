"""Seventh review (independent, fresh context) of 24abfec..564c918: accounts, public demo, hardening.
The reviewer's failing tests, kept here (scenarios unchanged; the eviction test now checks the fix)."""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.paypal import FixturePayPalClient
from cashdesk.queue import NotExecutable
from cashdesk.service import Desk
from cashdesk.users import MAX_FAILURES
from tests.review7_helpers import PASSWORD, T0, account, h, pending, refund_calls


class SandboxMode(FixturePayPalClient):
    """Simulated PayPal that reports itself as the sandbox (no network)."""
    mode = "sandbox"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL",
              "CASHDESK_AS_OF", "CASHDESK_PUBLIC_DEMO", "CASHDESK_BEHIND_PROXY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)
    d.refresh_proposals()
    return d


def as_old_version(desk, fn):
    """24abfec took any typed `by` and its queue had no account mode (as tests/test_identities.py::typed)."""
    desk.queue.require_users = False
    try:
        return fn()
    finally:
        desk.queue.require_users = True


# ---------- M2: a typed name shaped like an account ----------

def test_legacy_typed_name_shaped_like_an_account_counts_as_a_person(desk):
    item = pending(desk, "refund_capture")
    assert item.approvals_needed == 2
    as_old_version(desk, lambda: desk.queue.approve(item.id, by="user:dana", expected_hash=h(item)))
    assert desk.users.get("dana") is None
    mallory = account(desk, "mallory")
    out = None
    try:
        out = desk.approve(item.id, mallory, h(item))
    except NotExecutable:
        pass
    if out and out["status"] == "approved":
        desk.execute(item.id, mallory)
    assert refund_calls(desk.client) == [], "one person ran a refund above 500.00 with a typed 'user:dana'"


def test_a_typed_account_shaped_name_for_an_existing_account_still_is_not_that_account(desk):
    """Even when an account 'dana' exists, a 'user:dana' typed into the older version was not Dana."""
    item = pending(desk, "refund_capture")
    account(desk, "dana")
    as_old_version(desk, lambda: desk.queue.approve(item.id, by="user:dana", expected_hash=h(item)))
    mallory = account(desk, "mallory")
    try:
        if desk.approve(item.id, mallory, h(item))["status"] == "approved":
            desk.execute(item.id, mallory)
    except NotExecutable:
        pass
    assert refund_calls(desk.client) == []


def test_a_typed_account_shaped_single_approval_never_runs(desk):
    item = pending(desk, "send_reminder")
    as_old_version(desk, lambda: desk.queue.approve(item.id, by="user:dana", expected_hash=h(item)))
    with pytest.raises(NotExecutable):
        desk.execute(item.id, account(desk, "mallory"))
    assert desk.reject(item.id, account(desk, "mallory"), None)["status"] == "withdrawn"


# ---------- L3 / L4: a first approval by someone who can no longer approve ----------

def test_a_demo_first_approval_still_counts_once_the_database_points_at_the_sandbox(desk):
    item = pending(desk, "refund_capture")
    alex = desk.users.get("alex")
    assert desk.approve(item.id, alex, h(item))["status"] == "awaiting_second"
    sbx = Desk(client=SandboxMode(), reconciler=FallbackReconciler(), clock=lambda: T0)
    bob = account(sbx, "bob")
    try:
        if sbx.approve(item.id, bob, h(item))["status"] == "approved":
            sbx.execute(item.id, bob)
    except (NotExecutable, PermissionError):
        pass
    assert refund_calls(sbx.client) == [], "a password-less demo approval + one real account ran 800.00"


def test_a_disabled_first_approver_no_longer_counts(desk):
    item = pending(desk, "refund_capture")
    alice, bob = account(desk, "alice"), account(desk, "bob")
    desk.approve(item.id, alice, h(item))
    desk.users.disable("alice")
    try:
        if desk.approve(item.id, bob, h(item))["status"] == "approved":
            desk.execute(item.id, bob)
    except (NotExecutable, PermissionError):
        pass
    assert refund_calls(desk.client) == [], "a disabled account's first approval was one of the two"


def test_an_approval_by_an_account_disabled_before_the_run_does_not_run(desk):
    item = pending(desk, "send_reminder")
    alice, bob = account(desk, "alice"), account(desk, "bob")
    desk.approve(item.id, alice, h(item))
    desk.users.disable("alice")
    with pytest.raises(NotExecutable, match="no longer"):
        desk.execute(item.id, bob)
    assert desk.reject(item.id, bob, None)["status"] == "withdrawn"


# ---------- M1: the sign-in throttle under a concurrent burst ----------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path, monkeypatch):
    import uvicorn
    from cashdesk.app import create_app
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_MODE", "fixtures")
    monkeypatch.setenv("AI_PROVIDER", "none")
    desk = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler())
    desk.users.add("alice", "Alice", "approver", PASSWORD)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(desk), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", desk
    srv.should_exit = True
    t.join(5)


def test_concurrent_wrong_passwords_get_at_most_five_guesses(server):
    url, _ = server

    def guess(i):
        with httpx.Client(timeout=60) as c:
            return c.post(f"{url}/api/login", json={"username": "alice", "password": f"wrong-guess-{i:04d}"}).status_code

    with ThreadPoolExecutor(40) as ex:
        codes = list(ex.map(guess, range(40)))
    assert codes.count(401) <= MAX_FAILURES, f"{codes.count(401)} passwords were tried inside one window"


def test_a_correct_password_inside_a_burst_does_not_sign_in_after_five_failures(server):
    url, _ = server
    pw = [f"wrong-guess-{i:04d}" for i in range(30)]
    pw[10] = PASSWORD

    def guess(p):
        with httpx.Client(timeout=60) as c:
            return c.post(f"{url}/api/login", json={"username": "alice", "password": p}).status_code

    with ThreadPoolExecutor(30) as ex:
        codes = list(ex.map(guess, pw))
    wrong = codes[:10] + codes[11:]
    assert not (wrong.count(401) > MAX_FAILURES and codes[10] == 200)
    assert wrong.count(401) <= MAX_FAILURES


# ---------- L7 / L8: public demo ----------

@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_MODEL", "AI_BASE_URL", "CODEX_BIN",
              "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "CASHDESK_ALLOWED_HOSTS", "RENDER_EXTERNAL_HOSTNAME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("CASHDESK_PUBLIC_DEMO", "1")
    monkeypatch.setenv("CASHDESK_BEHIND_PROXY", "1")
    monkeypatch.setenv("PAYPAL_MODE", "fixtures")
    monkeypatch.setenv("AI_PROVIDER", "none")
    return monkeypatch


def visitor(app, ip):
    # https: the cookies are Secure behind the proxy, and the test client (like a browser) sends them
    # only over https
    return TestClient(app, base_url="https://127.0.0.1:8000",
                      headers={"CF-Connecting-IP": ip, "X-Forwarded-Proto": "https"})


def test_a_refused_demo_sign_in_opens_no_workspace(demo_env):
    from cashdesk.app import create_app
    app = create_app()
    for a in range(5):
        for _ in range(10):
            assert visitor(app, f"203.0.113.{a}").post("/api/demo-login", json={"username": "nobody"}).status_code == 404
    assert app.state.demo_pool.count() == 0


def test_a_visitor_in_the_middle_of_the_demo_is_never_evicted(demo_env):
    from cashdesk.app import create_app
    demo_env.setenv("CASHDESK_DEMO_MAX_WORKSPACES", "5")
    app = create_app()
    victim = visitor(app, "198.51.100.7")
    assert victim.post("/api/demo-login", json={"username": "alex"}).status_code == 200
    codes = [visitor(app, f"203.0.113.{a}").post("/api/demo-login", json={"username": "alex"}).status_code
             for a in range(8)]
    assert codes.count(200) == 4 and codes.count(503) == 4  # full of recent visitors: busy, nobody evicted
    assert victim.get("/api/approvals").status_code == 200, "the victim's workspace was evicted mid-demo"


def test_workspaces_idle_past_the_keep_window_make_room(demo_env):
    from datetime import timedelta
    from cashdesk.app import create_app
    demo_env.setenv("CASHDESK_DEMO_MAX_WORKSPACES", "2")
    app = create_app()
    pool = app.state.demo_pool
    now = [pool.clock()]
    pool.clock = lambda: now[0]
    first = visitor(app, "198.51.100.1")
    assert first.post("/api/demo-login", json={"username": "alex"}).status_code == 200
    assert visitor(app, "198.51.100.2").post("/api/demo-login", json={"username": "alex"}).status_code == 200
    assert visitor(app, "198.51.100.3").post("/api/demo-login", json={"username": "alex"}).status_code == 503
    now[0] += timedelta(minutes=31)
    assert visitor(app, "198.51.100.3").post("/api/demo-login", json={"username": "alex"}).status_code == 200
    assert pool.count() == 2


def test_one_unrelated_malformed_cookie_does_not_hide_the_workspace(demo_env):
    from cashdesk.app import create_app
    demo_env.delenv("CASHDESK_BEHIND_PROXY")
    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    assert c.post("/api/demo-login", json={"username": "alex"}).status_code == 200
    jar = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    raw = TestClient(app, base_url="http://127.0.0.1:8000")
    assert raw.get("/api/approvals", headers={"cookie": jar}).status_code == 200
    browser = 'prefs={"theme":"dark"}; ' + jar
    assert raw.get("/api/approvals", headers={"cookie": browser}).status_code == 200


# ---------- L9 / L10 / L5 / L6: CLI and backup ----------

def test_cli_refuses_a_password_flag_without_echoing_it(tmp_path, monkeypatch, capsys):
    from cashdesk.users import main
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        main(["add", "alice", "--name", "Alice", "--password=Hunter2-not-a-real-one"])
    assert "Hunter2-not-a-real-one" not in capsys.readouterr().err


def test_check_refuses_a_backup_folder_with_nothing_in_it_but_a_manifest(tmp_path):
    from cashdesk.backup import check
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "manifest.json").write_text('{"files": {}}')
    assert check(bk) and not (bk / "cashdesk.db").exists()


def test_check_compares_the_manifest_with_the_audit_log(desk, tmp_path):
    import json
    from cashdesk.backup import backup, check
    backup(desk.settings.var_dir, tmp_path / "bk")
    m = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    m["audit"]["count"] += 1
    (tmp_path / "bk" / "manifest.json").write_text(json.dumps(m))
    assert any("audit" in p for p in check(tmp_path / "bk"))


def test_restore_does_not_bring_back_ended_sessions(desk, tmp_path):
    from cashdesk.backup import backup, restore
    desk.users.add("eve", "Eve", "approver", PASSWORD)
    stolen = desk.users.create_session("eve")
    backup(desk.settings.var_dir, tmp_path / "bk")
    desk.users.disable("eve")
    restore(tmp_path / "bk", tmp_path / "restored")
    d2 = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0,
              var_dir=tmp_path / "restored")
    assert d2.users.session_user(stolen) is None


SENT: list = []


class CountingPayPal(FixturePayPalClient):
    def execute_call(self, method, path, body, *, request_id):
        if method != "GET":
            SENT.append((method, path, request_id))
        return super().execute_call(method, path, body, request_id=request_id)


def test_restoring_an_older_backup_never_runs_an_approval_again(tmp_path, monkeypatch):
    from cashdesk.backup import backup, restore
    SENT.clear()
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path / "var"))
    d = Desk(client=CountingPayPal(), reconciler=FallbackReconciler(), clock=lambda: T0)
    d.refresh_proposals()
    alice = d.users.add("alice", "Alice", "approver", PASSWORD)
    item = next(i for i in d.queue.list() if i.proposal.type == "record_payment")
    assert d.approve(item.id, alice, h(item))["status"] == "approved"
    backup(d.settings.var_dir, tmp_path / "bk")
    assert d.execute(item.id, alice)["status"] == "executed"
    restore(tmp_path / "bk", tmp_path / "restored")
    d2 = Desk(client=CountingPayPal(), reconciler=FallbackReconciler(), clock=lambda: T0, var_dir=tmp_path / "restored")
    assert d2.queue.get(item.id).status == "withdrawn"  # approve it again after a fresh sync
    with pytest.raises(NotExecutable):
        d2.execute(item.id, d2.users.get("alice"))
    assert len(SENT) == 1, f"the same payment was recorded twice: {SENT}"
