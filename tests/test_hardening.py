"""P4: sign-in throttle, health with commit, startup checks, backup and restore."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cashdesk.ai import FallbackReconciler
from cashdesk.app import create_app
from cashdesk.audit import AuditLog, verify
from cashdesk.paypal import FixturePayPalClient
from cashdesk.service import Desk
from tests.accounts_helper import PASSWORD, sign_in, user

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def desk(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL",
              "CASHDESK_AS_OF", "PAYPAL_MODE", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "CASHDESK_ALLOWED_HOSTS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path / "var"))
    now = [T0]
    d = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: now[0])
    d._now = now
    return d


def login(api, username, password):
    return api.post("/api/login", json={"username": username, "password": password})


# ---------- sign-in throttle ----------

def test_five_wrong_passwords_lock_that_username_for_15_minutes(desk):
    api = TestClient(create_app(desk))
    user(desk, "alice")
    for _ in range(5):
        assert login(api, "alice", "wrong password!").status_code == 401
    r = login(api, "alice", PASSWORD)  # even the right password waits
    assert r.status_code == 429 and "15 minutes" in r.json()["detail"]
    desk._now[0] += timedelta(minutes=15, seconds=1)
    assert login(api, "alice", PASSWORD).status_code == 200


def test_five_failures_from_one_address_lock_that_address(desk):
    api = TestClient(create_app(desk))
    user(desk, "alice")
    for name in ("bob", "carol", "dave", "erin", "frank"):
        assert login(api, name, "wrong password!").status_code == 401
    assert login(api, "alice", PASSWORD).status_code == 429


def test_a_good_sign_in_clears_that_username(desk):
    api = TestClient(create_app(desk))
    user(desk, "alice")
    for _ in range(4):
        login(api, "alice", "wrong password!")
    assert login(api, "alice", PASSWORD).status_code == 200
    users = desk.users
    assert not users.login_blocked("alice", "198.51.100.7")


def test_the_throttle_survives_a_restart(desk):
    user(desk, "alice")
    for _ in range(5):
        desk.users.login_failed("alice", "198.51.100.7")
    again = Desk(client=FixturePayPalClient(), reconciler=FallbackReconciler(), clock=lambda: desk._now[0])
    assert again.users.login_blocked("alice", "203.0.113.9")


def test_demo_sign_in_is_not_throttled(desk):
    api = TestClient(create_app(desk))
    for name in ("bob", "carol", "dave", "erin", "frank"):
        login(api, name, "wrong password!")
    assert api.post("/api/demo-login", json={"username": "alex"}).status_code == 200


# ---------- health ----------

def test_health_reports_version_and_commit(desk, monkeypatch):
    monkeypatch.setenv("CASHDESK_COMMIT", "0123456789abcdef0123")
    h = TestClient(create_app(desk)).get("/api/health").json()
    assert h["status"] == "ok" and h["version"] and h["commit"] == "0123456789ab"


def test_health_commit_comes_from_render_or_git(desk, monkeypatch):
    monkeypatch.delenv("CASHDESK_COMMIT", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "fedcba9876543210")
    assert TestClient(create_app(desk)).get("/api/health").json()["commit"] == "fedcba987654"
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    commit = TestClient(create_app(desk)).get("/api/health").json()["commit"]
    assert commit == "unknown" or (len(commit) == 12 and all(c in "0123456789abcdef" for c in commit))


# ---------- startup checks ----------

def startup(monkeypatch, **env):
    from cashdesk.config import check_startup
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return check_startup()


def test_sample_data_starts_clean(tmp_path, monkeypatch):
    errors, warnings = startup(monkeypatch, CASHDESK_VAR_DIR=str(tmp_path), PAYPAL_MODE="fixtures", AI_PROVIDER="none")
    assert errors == [] and warnings == []


def test_sandbox_without_keys_says_what_to_set(tmp_path, monkeypatch):
    errors, _ = startup(monkeypatch, CASHDESK_VAR_DIR=str(tmp_path), PAYPAL_MODE="sandbox",
                        PAYPAL_CLIENT_ID=None, PAYPAL_CLIENT_SECRET=None)
    assert any("PAYPAL_CLIENT_ID" in e and "PAYPAL_MODE=fixtures" in e for e in errors), errors


@pytest.mark.parametrize("env, needle", [
    ({"PAYPAL_MODE": "live"}, "PAYPAL_MODE"),
    ({"AI_PROVIDER": "gpt"}, "AI_PROVIDER"),
    ({"AI_PROVIDER": "openai", "AI_TIMEOUT": "soon"}, "AI_TIMEOUT"),
    ({"PAYPAL_BASE_URL": "https://api-m.paypal.com"}, "PAYPAL_BASE_URL"),
    ({"CASHDESK_ALLOWED_HOSTS": "https://desk.example.com/"}, "CASHDESK_ALLOWED_HOSTS"),
])
def test_bad_settings_are_named(tmp_path, monkeypatch, env, needle):
    errors, _ = startup(monkeypatch, CASHDESK_VAR_DIR=str(tmp_path), **{"PAYPAL_MODE": "fixtures", **env})
    assert any(needle in e for e in errors), errors


def test_an_unwritable_data_folder_is_named(tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        errors, _ = startup(monkeypatch, CASHDESK_VAR_DIR=str(ro / "var"), PAYPAL_MODE="fixtures")
        assert any("CASHDESK_VAR_DIR" in e for e in errors), errors
    finally:
        ro.chmod(0o700)


def test_sandbox_with_no_accounts_warns_how_to_add_one(tmp_path, monkeypatch):
    errors, warnings = startup(monkeypatch, CASHDESK_VAR_DIR=str(tmp_path), PAYPAL_MODE="sandbox",
                               PAYPAL_CLIENT_ID="id-for-tests", PAYPAL_CLIENT_SECRET="secret-for-tests")
    assert errors == [] and any("python -m cashdesk.users add" in w for w in warnings), warnings


def test_the_server_refuses_to_start_with_a_bad_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_MODE", "fixtures")
    monkeypatch.setenv("AI_PROVIDER", "gpt")
    with pytest.raises(RuntimeError, match="AI_PROVIDER"):
        with TestClient(create_app()):
            pass


def test_config_check_command(tmp_path, monkeypatch, capsys):
    from cashdesk.config import main
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_MODE", "fixtures")
    monkeypatch.setenv("AI_PROVIDER", "none")
    assert main([]) == 0 and "OK" in capsys.readouterr().out
    monkeypatch.setenv("AI_PROVIDER", "gpt")
    assert main([]) == 1 and "AI_PROVIDER" in capsys.readouterr().out


# ---------- backup and restore ----------

def busy_desk(desk):
    desk.refresh_proposals()
    item = next(i for i in desk.queue.list() if i.proposal.type == "send_reminder")
    desk.approve(item.id, user(desk, "alice"))
    desk.execute(item.id, user(desk, "alice"))
    return desk


def test_backup_copies_a_consistent_verified_desk(desk, tmp_path):
    from cashdesk.backup import backup, check
    busy_desk(desk)
    dest = tmp_path / "bk"
    m = backup(desk.settings.var_dir, dest)
    assert (dest / "cashdesk.db").exists() and (dest / "audit.jsonl").exists() and (dest / "audit.jsonl.head").exists()
    assert verify(dest / "audit.jsonl").ok and m["audit"]["count"] == verify(desk.audit.path).count
    con = sqlite3.connect(dest / "cashdesk.db")
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert con.execute("select count(*) from queue_items").fetchone()[0] == len(desk.queue.list())
    assert json.loads((dest / "manifest.json").read_text())["files"]["cashdesk.db"]["sha256"]
    assert check(dest) == []


def test_backup_never_overwrites(desk, tmp_path):
    from cashdesk.backup import backup
    dest = tmp_path / "bk"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        backup(desk.settings.var_dir, dest)


def test_check_finds_a_changed_backup(desk, tmp_path):
    from cashdesk.backup import backup, check
    busy_desk(desk)
    dest = tmp_path / "bk"
    backup(desk.settings.var_dir, dest)
    with open(dest / "cashdesk.db", "ab") as f:
        f.write(b"x")
    assert any("cashdesk.db" in p for p in check(dest))


def test_backup_during_writes_still_verifies(desk, tmp_path):
    from cashdesk.backup import backup, check
    busy_desk(desk)
    stop = threading.Event()

    def writer():
        log = AuditLog(desk.audit.path)
        while not stop.is_set():
            log.append({"event": "webhook_rejected", "pad": "y" * 500})
    t = threading.Thread(target=writer)
    t.start()
    try:
        for n in range(5):
            backup(desk.settings.var_dir, tmp_path / f"bk{n}")
    finally:
        stop.set()
        t.join()
    assert all(check(tmp_path / f"bk{n}") == [] for n in range(5))


def test_restore_refuses_to_overwrite_and_then_restores(desk, tmp_path):
    from cashdesk.backup import backup, restore
    busy_desk(desk)
    dest = tmp_path / "bk"
    backup(desk.settings.var_dir, dest)
    with pytest.raises(FileExistsError):
        restore(dest, desk.settings.var_dir)
    fresh = tmp_path / "restored"
    restore(dest, fresh)
    assert verify(fresh / "audit.jsonl").ok
    assert sqlite3.connect(fresh / "cashdesk.db").execute("select count(*) from queue_items").fetchone()[0] == 7


def test_make_backup_target_exists():
    mk = (REPO / "Makefile").read_text()
    assert "\nbackup:" in mk and "cashdesk.backup" in mk
