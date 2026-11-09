"""Append-only, hash-chained audit log (var/audit.jsonl) with a verifier.

Each line is one JSON record: {seq, at, event, ..., prev_hash, hash}, where
hash = sha256(canonical JSON of the record without `hash`) and prev_hash links to the line
before (GENESIS for the first). A head file (audit.jsonl.head) stores the last seq and hash,
so deleting lines from the end is detected too. Editing, deleting, reordering or inserting
any line breaks the chain from that point on.

Limit: someone who can rewrite the whole file *and* the head file can forge a new chain.
Publish or copy the head hash elsewhere (the board shows it) to anchor it.

  python -m cashdesk.audit verify [--path var/audit.jsonl]      (make audit-verify)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings

GENESIS = "0" * 64
SECRET_KEY = re.compile(r"(token|secret|authorization|password|api[_-]?key|client_id)", re.I)


def _canonical(rec: dict) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash(rec: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in rec.items() if k != "hash"})).hexdigest()


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def default_path() -> Path:
    return load_settings().var_dir / "audit.jsonl"


@dataclass
class VerifyResult:
    ok: bool
    count: int
    head_hash: str | None = None
    first_bad_seq: int | None = None
    error: str | None = None


def _head_path(path: Path) -> Path:
    return path.with_name(path.name + ".head")


def _read_lines(path: Path) -> list[str]:
    """The log's lines, read under a shared lock so a write in progress is never seen half done."""
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            text = f.read()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return text.split("\n")


def _read_head(head_file: Path) -> dict | None:
    try:
        head = json.loads(head_file.read_text())
    except (OSError, ValueError):
        return None
    return head if isinstance(head, dict) else None


def verify(path: Path | str | None = None) -> VerifyResult:
    """Check the chain. Two states a crash mid-append can leave are not tampering and verify as OK:
    a last line cut off before its newline (the record was never completed; the next append drops
    it), and a head pointer one record behind a last record that chains correctly (the crash came
    between writing the line and the head; the next append moves the head)."""
    path = Path(path or default_path())
    head_file = _head_path(path)
    if not path.exists():
        if head_file.exists():
            return VerifyResult(False, 0, error="log missing but head pointer exists")
        return VerifyResult(True, 0)
    return _verify_text("\n".join(_read_lines(path)), head_file)


def _verify_text(text: str, head_file: Path) -> VerifyResult:
    lines = text.split("\n")[:-1]  # the last piece is "" for a complete log, or a torn line
    prev, count, prev_before = GENESIS, 0, None
    for n, line in enumerate(lines, start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return VerifyResult(False, count, first_bad_seq=n, error=f"line {n} is not valid JSON")
        if not isinstance(rec, dict) or rec.get("seq") != n:
            return VerifyResult(False, count, first_bad_seq=n, error=f"line {n} has seq {rec.get('seq') if isinstance(rec, dict) else '?'}")
        if rec.get("prev_hash") != prev:
            return VerifyResult(False, count, first_bad_seq=n, error=f"seq {n}: prev_hash does not link to seq {n - 1}")
        if rec.get("hash") != _hash(rec):
            return VerifyResult(False, count, first_bad_seq=n, error=f"seq {n}: hash does not match its content")
        prev_before, prev, count = prev, rec["hash"], n
    if head_file.exists():
        head = _read_head(head_file)
        if head is None:
            return VerifyResult(False, count, head_hash=prev, error="head pointer is unreadable")
        at_end = head.get("seq") == count and head.get("hash") == (prev if count else None)
        one_behind = count >= 1 and head.get("seq") == count - 1 and head.get("hash") == (prev_before if count > 1 else None)
        if not (at_end or one_behind):
            return VerifyResult(False, count, head_hash=prev, first_bad_seq=count + 1,
                                error=f"head pointer says seq {head.get('seq')}, log ends at {count} (truncated?)")
    elif count > 1:
        return VerifyResult(False, count, head_hash=prev, error="head pointer missing")
    return VerifyResult(True, count, head_hash=prev if count else None)


class AuditLog:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> dict:
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                state = self._repair_and_verify(f)
                if not state.ok:
                    raise ValueError(f"refusing to append to a broken audit log: {state.error}")
                rec = redact(dict(event))
                rec.setdefault("at", datetime.now(timezone.utc).isoformat())
                rec["seq"] = state.count + 1
                rec["prev_hash"] = state.head_hash or GENESIS
                rec.pop("hash", None)
                rec["hash"] = _hash(rec)
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
                self._write_head(rec["seq"], rec["hash"])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return rec

    def _write_head(self, seq: int, h: str) -> None:
        """Atomically: a crash leaves the old head or the new one, never an empty file."""
        head = _head_path(self.path)
        tmp = head.with_name(head.name + ".tmp")
        with open(tmp, "w") as hf:
            hf.write(json.dumps({"seq": seq, "hash": h}))
            hf.flush()
            os.fsync(hf.fileno())
        os.replace(tmp, head)

    def _repair_and_verify(self, f) -> VerifyResult:
        """Under the exclusive lock: drop a torn last line, then catch the head up if it lags by one."""
        f.seek(0)
        text = f.read()
        if text and not text.endswith("\n"):
            f.truncate(text.rfind("\n") + 1)
            f.flush()
            os.fsync(f.fileno())
        f.seek(0, os.SEEK_END)
        f.seek(0)
        state = _verify_text(f.read(), _head_path(self.path))
        f.seek(0, os.SEEK_END)
        if state.ok and state.count:
            head = _read_head(_head_path(self.path)) or {}
            if head.get("seq") != state.count:
                self._write_head(state.count, state.head_hash)
        return state

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for x in _read_lines(self.path)[:-1]:  # a torn last line (no newline) isn't a record yet
            if x.strip():
                out.append(json.loads(x))
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cashdesk.audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify the hash chain")
    v.add_argument("--path", default=None)
    args = ap.parse_args(argv)
    res = verify(args.path)
    target = args.path or default_path()
    if res.ok:
        print(f"OK: {res.count} records, chain intact, head {res.head_hash or '-'} ({target})")
        return 0
    print(f"FAIL: {res.error} (first bad seq: {res.first_bad_seq}; {res.count} good records before it) ({target})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
