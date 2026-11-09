"""Findings from the 10 Nov soak (40 normal runs green, 4 of 10 runs under full CPU load red)."""
from __future__ import annotations

import threading

import cashdesk.audit as audit
from cashdesk.audit import AuditLog, verify


def test_verify_reads_the_log_and_its_head_as_one_snapshot(tmp_path, monkeypatch):
    """verify() read the log under the shared lock, then read the .head file after releasing it. An
    append landing in between moved the head past the text it had read, so a sound log reported
    'head pointer says seq N+1'. Here a writer is let in exactly at the head read: it must wait."""
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append({"event": "x", "i": i})
    real = audit._read_head
    writer: list[threading.Thread] = []

    def head_read_with_a_writer_in_between(head_file):
        if not writer:
            t = threading.Thread(target=lambda: AuditLog(log.path).append({"event": "late"}))
            writer.append(t)
            t.start()
            t.join(timeout=0.5)  # with no lock held the append finishes inside this window
        return real(head_file)

    monkeypatch.setattr(audit, "_read_head", head_read_with_a_writer_in_between)
    res = verify(log.path)
    writer[0].join(timeout=5)
    assert res.ok, res.error
    assert res.count in (3, 4)
    assert verify(log.path).ok and len(AuditLog(log.path).records()) == 4
