"""P1: approver accounts, sessions, sign-in routes and roles."""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.app import create_app
from cashdesk.paypal import FixturePayPalClient
from cashdesk.service import Desk
from cashdesk.users import (ABSOLUTE_TTL, IDLE_TTL, AccountError, UserStore, check_password, hash_password,
                            main as users_main)
from tests.accounts_helper import PASSWORD, sign_in

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return [T0]


@pytest.fixture
def users(tmp_path, clock):
    return UserStore(tmp_path / "cashdesk.db", clock=lambda: clock[0])


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("CASHDESK_AS_OF", raising=False)
    return Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: T0)


@pytest.fixture
def api(desk):
    return TestClient(create_app(desk))


class SandboxMode(FixturePayPalClient):
    """A fixture client that says it is the sandbox: no demo users may exist or sign in."""
    mode = "sandbox"


@pytest.fixture
def sandbox_desk(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    return Desk(client=SandboxMode(), reconciler=FallbackReconciler(), clock=lambda: T0)


# ---------- hashing ----------

def test_password_hash_is_salted_scrypt_and_checks():
    a, b = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert a != b and a.startswith("scrypt$") and "correct" not in a
    assert check_password("correct horse battery", a)
    assert not check_password("correct horse batterY", a)
    assert not check_password("anything", "!")  # an unusable hash never matches
    assert not check_password("anything", "scrypt$broken")


# ---------- the store ----------

def test_add_normalises_and_validates(users):
    u = users.add("  Alice ", "Alice Smith", "approver", PASSWORD)
    assert u.username == "alice" and u.display_name == "Alice Smith" and u.role == "approver"
    with pytest.raises(AccountError):
        users.add("ALICE", "Again", "approver", PASSWORD)          # duplicate, any case
    with pytest.raises(AccountError):
        users.add("bob", "Bob", "superuser", PASSWORD)             # unknown role
    with pytest.raises(AccountError):
        users.add("bob", "Bob", "approver", "short")               # too short
    with pytest.raises(AccountError):
        users.add("bob smith", "Bob", "approver", PASSWORD)        # not a username
    with pytest.raises(AccountError):
        users.add("bob", "​⠀ ", "approver", PASSWORD)    # a name that is only invisible characters


def test_authenticate(users):
    users.add("alice", "Alice", "approver", PASSWORD)
    assert users.authenticate("ALICE", PASSWORD).username == "alice"
    assert users.authenticate("alice", PASSWORD + "x") is None
    assert users.authenticate("nobody", PASSWORD) is None
    users.disable("alice")
    assert users.authenticate("alice", PASSWORD) is None


def test_sessions_expire_idle_and_absolute_and_end(users, clock, tmp_path):
    users.add("alice", "Alice", "approver", PASSWORD)
    tok = users.create_session("alice")
    assert users.session_user(tok).username == "alice"
    raw = (tmp_path / "cashdesk.db").read_bytes()
    assert tok.encode() not in raw  # only a hash of the token is stored
    # activity keeps it alive up to the absolute limit
    for _ in range(3):  # 23 h 57 min of steady use
        clock[0] += IDLE_TTL - timedelta(minutes=1)
        assert users.session_user(tok) is not None
    clock[0] += timedelta(minutes=4)  # 4 minutes idle, but 24 h 01 min since sign-in
    assert users.session_user(tok) is None
    # idle
    tok2 = users.create_session("alice")
    clock[0] += IDLE_TTL + timedelta(seconds=1)
    assert users.session_user(tok2) is None
    # explicit end
    tok3 = users.create_session("alice")
    users.end_session(tok3)
    assert users.session_user(tok3) is None
    assert users.session_user("not-a-token") is None and users.session_user("") is None


def test_disabling_a_user_ends_their_sessions(users):
    users.add("alice", "Alice", "approver", PASSWORD)
    tok = users.create_session("alice")
    users.disable("alice")
    assert users.session_user(tok) is None


def test_set_password(users):
    users.add("alice", "Alice", "approver", PASSWORD)
    users.set_password("alice", "another long password")
    assert users.authenticate("alice", PASSWORD) is None
    assert users.authenticate("alice", "another long password") is not None


# ---------- CLI ----------

def test_cli_add_list_disable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert users_main(["add", "alice", "--name", "Alice Smith", "--role", "approver"]) == 0
    assert users_main(["list"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "Alice Smith" in out and "scrypt" not in out and PASSWORD not in out
    assert users_main(["disable", "alice"]) == 0
    assert UserStore(tmp_path / "cashdesk.db").authenticate("alice", PASSWORD) is None


def test_cli_never_takes_a_password_in_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        users_main(["add", "alice", "--name", "Alice", "--password", PASSWORD])


def test_cli_set_password_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    UserStore(tmp_path / "cashdesk.db").add("alice", "Alice", "approver", PASSWORD)
    monkeypatch.setattr("sys.stdin", io.StringIO("a brand new password\n"))
    assert users_main(["set-password", "alice"]) == 0
    assert UserStore(tmp_path / "cashdesk.db").authenticate("alice", "a brand new password") is not None


# ---------- HTTP ----------

WRITES = [("post", "/api/sync"), ("post", "/api/proposals/refresh"),
          ("post", "/api/approvals/q00001/approve"), ("post", "/api/approvals/q00001/reject"),
          ("post", "/api/approvals/q00001/execute")]
READS = [("get", "/api/reconciliation"), ("get", "/api/approvals"), ("get", "/api/audit"),
         ("get", "/api/export/journal.csv"), ("get", "/api/me")]


def test_everything_but_health_login_and_the_page_needs_a_session(api):
    assert api.get("/api/health").status_code == 200
    assert api.get("/").status_code == 200
    assert api.get("/api/login-options").status_code == 200
    for method, path in READS + WRITES:
        r = api.post(path, json={}) if method == "post" else api.get(path)
        assert r.status_code == 401, (path, r.status_code)


def test_login_sets_a_strict_httponly_cookie_and_me_logout_work(api, desk):
    desk.users.add("alice", "Alice Smith", "approver", PASSWORD)
    r = api.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["display_name"] == "Alice Smith"
    cookie = r.headers["set-cookie"].lower()
    assert "cashdesk_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # plain http on localhost
    assert api.get("/api/me").json()["user"]["username"] == "alice"
    assert api.post("/api/logout").status_code == 200
    assert api.get("/api/me").status_code == 401


def test_cookie_is_secure_over_https(desk):
    desk.users.add("alice", "Alice", "approver", PASSWORD)
    api = TestClient(create_app(desk), base_url="https://testserver")
    r = api.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert "secure" in r.headers["set-cookie"].lower()


def test_bad_logins_all_look_the_same(api, desk):
    desk.users.add("alice", "Alice", "approver", PASSWORD)
    desk.users.add("bob", "Bob", "approver", PASSWORD)
    desk.users.disable("bob")
    answers = {(r.status_code, r.text) for r in (
        api.post("/api/login", json={"username": "alice", "password": "wrong password"}),
        api.post("/api/login", json={"username": "nobody", "password": PASSWORD}),
        api.post("/api/login", json={"username": "bob", "password": PASSWORD}))}
    assert len(answers) == 1 and answers.pop()[0] == 401


def test_login_rotates_the_session(api, desk):
    desk.users.add("alice", "Alice", "approver", PASSWORD)
    api.post("/api/login", json={"username": "alice", "password": PASSWORD})
    first = api.cookies.get("cashdesk_session")
    api.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert api.cookies.get("cashdesk_session") != first
    assert desk.users.session_user(first) is None


def test_viewer_reads_but_never_writes(api, desk):
    sign_in(api, desk, "val", role="viewer")
    for method, path in READS:
        assert getattr(api, method)(path).status_code == 200, path
    for method, path in WRITES:
        assert getattr(api, method)(path, json={}).status_code == 403, path


def test_approver_can_write(api, desk):
    sign_in(api, desk, "alice")
    assert api.post("/api/sync").status_code == 200
    assert api.post("/api/proposals/refresh").status_code == 200


def test_a_session_of_a_disabled_user_stops_working(api, desk):
    sign_in(api, desk, "alice")
    desk.users.disable("alice")
    assert api.get("/api/approvals").status_code == 401


# ---------- demo users: sample data only ----------

def test_sample_data_has_two_demo_approvers_with_one_click_sign_in(api, desk):
    opts = api.get("/api/login-options").json()
    assert [u["username"] for u in opts["demo_users"]] == ["alex", "sam"]
    r = api.post("/api/demo-login", json={"username": "sam"})
    assert r.status_code == 200 and r.json()["user"]["display_name"] == "Sam (bookkeeper)"
    assert api.get("/api/me").json()["user"]["demo"] is True
    assert api.post("/api/demo-login", json={"username": "nobody"}).status_code == 404
    desk.users.add("alice", "Alice", "approver", PASSWORD)
    assert api.post("/api/demo-login", json={"username": "alice"}).status_code == 404  # not a demo user
    # demo users have no usable password
    assert api.post("/api/login", json={"username": "alex", "password": ""}).status_code in (401, 422)


def test_sandbox_mode_has_no_demo_users_and_refuses_old_demo_sessions(tmp_path, monkeypatch, desk):
    # a database first used with sample data, then pointed at the sandbox
    tok = desk.users.create_session("alex")
    sbx = Desk(client=SandboxMode(), reconciler=FallbackReconciler(), clock=lambda: T0)
    api = TestClient(create_app(sbx))
    assert api.get("/api/login-options").json()["demo_users"] == []
    assert api.post("/api/demo-login", json={"username": "alex"}).status_code == 404
    api.cookies.set("cashdesk_session", tok)
    assert api.get("/api/approvals").status_code == 401


def test_sandbox_mode_never_seeds_demo_users(sandbox_desk):
    assert sandbox_desk.users.list() == []
    con = sqlite3.connect(sandbox_desk.settings.var_dir / "cashdesk.db")
    assert con.execute("select count(*) from users").fetchone()[0] == 0
