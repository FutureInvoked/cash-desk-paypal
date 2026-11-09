"""E6: hash-chained append-only audit log, verifier, and tamper detection."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.audit import GENESIS, AuditLog, verify
from cashdesk.paypal import FixturePayPalClient
from cashdesk.queue import ApprovalQueue

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def log(tmp_path):
    a = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        a.append({"event": "test", "n": i})
    return a


def lines(path):
    return path.read_text().splitlines()


def write(path, ls):
    path.write_text("".join(x + "\n" for x in ls))


def test_records_are_chained(log):
    recs = [json.loads(x) for x in lines(log.path)]
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert recs[0]["prev_hash"] == GENESIS
    for a, b in zip(recs, recs[1:]):
        assert b["prev_hash"] == a["hash"]
    assert all(len(r["hash"]) == 64 and r["at"] for r in recs)


def test_clean_log_verifies(log):
    res = verify(log.path)
    assert res.ok and res.count == 5 and res.head_hash == json.loads(lines(log.path)[-1])["hash"]


def test_empty_log_verifies(tmp_path):
    assert verify(tmp_path / "none.jsonl").ok


def test_edited_field_is_detected(log):
    ls = lines(log.path)
    r = json.loads(ls[2])
    r["n"] = 99
    ls[2] = json.dumps(r, sort_keys=True)
    write(log.path, ls)
    res = verify(log.path)
    assert not res.ok and res.first_bad_seq == 3


def test_edit_with_recomputed_hash_breaks_the_next_link(log):
    import hashlib
    ls = lines(log.path)
    r = json.loads(ls[1])
    r["n"] = 42
    body = {k: v for k, v in r.items() if k != "hash"}
    r["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ls[1] = json.dumps(r, sort_keys=True)
    write(log.path, ls)
    res = verify(log.path)
    assert not res.ok and res.first_bad_seq == 3


def test_deleted_line_is_detected(log):
    ls = lines(log.path)
    del ls[2]
    write(log.path, ls)
    assert not verify(log.path).ok


def test_reordered_lines_are_detected(log):
    ls = lines(log.path)
    ls[1], ls[2] = ls[2], ls[1]
    write(log.path, ls)
    assert not verify(log.path).ok


def test_truncated_tail_is_detected_via_the_head_pointer(log):
    write(log.path, lines(log.path)[:3])
    res = verify(log.path)
    assert not res.ok and "head" in res.error


def test_garbage_line_is_detected(log):
    with log.path.open("a") as f:
        f.write("not json\n")
    assert not verify(log.path).ok


def test_append_continues_an_existing_chain(log):
    again = AuditLog(log.path)
    again.append({"event": "later"})
    assert verify(log.path).ok and verify(log.path).count == 6


def test_append_refuses_to_extend_a_broken_chain(log):
    ls = lines(log.path)
    del ls[1]
    write(log.path, ls)
    with pytest.raises(ValueError):
        AuditLog(log.path).append({"event": "x"})


def test_secrets_are_redacted(tmp_path):
    a = AuditLog(tmp_path / "a.jsonl")
    a.append({"event": "x", "access_token": "A21AA-real", "nested": {"Authorization": "Bearer x",
              "client_secret": "s", "ok": "keep"}, "list": [{"api_key": "k"}]})
    text = (tmp_path / "a.jsonl").read_text()
    for secret in ("A21AA-real", "Bearer x", '"s"', '"k"'):
        assert secret not in text
    assert "keep" in text and verify(tmp_path / "a.jsonl").ok


def test_queue_events_land_in_the_audit_log(tmp_path):
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=date(2026, 11, 7), reconciler=FallbackReconciler())
    p = next(x for x in propose(r, invs, txns, as_of=date(2026, 11, 7)) if x.type == "refund_capture")
    audit = AuditLog(tmp_path / "audit.jsonl")
    q = ApprovalQueue(tmp_path / "q.db", on_event=audit.append)
    item = q.enqueue(p)
    q.approve(item.id, by="owner")
    q.approve(item.id, by="bookkeeper")  # 800.00 refund: second approver
    q.execute(item.id, c)
    recs = [json.loads(x) for x in lines(audit.path)]
    assert [x["event"] for x in recs] == ["enqueued", "first_approval", "approved", "executing", "executed"]
    assert recs[1]["by"] == "owner" and recs[2]["by"] == "bookkeeper"
    assert recs[3]["request_id"] == f"cashdesk-{item.id}" and recs[3]["api_calls"][0]["path"].endswith("/refund")
    assert verify(audit.path).ok


def test_cli_verify_exit_codes(log):
    ok = subprocess.run([sys.executable, "-m", "cashdesk.audit", "verify", "--path", str(log.path)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0 and "OK" in ok.stdout
    write(log.path, lines(log.path)[:-1])
    bad = subprocess.run([sys.executable, "-m", "cashdesk.audit", "verify", "--path", str(log.path)],
                         cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode == 1 and "FAIL" in bad.stdout
