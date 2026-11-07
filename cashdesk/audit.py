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


def verify(path: Path | str | None = None) -> VerifyResult:
    path = Path(path or default_path())
    head_file = _head_path(path)
    if not path.exists():
        if head_file.exists():
            return VerifyResult(False, 0, error="log missing but head pointer exists")
        return VerifyResult(True, 0)
    prev, count = GENESIS, 0
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return VerifyResult(False, count, first_bad_seq=n, error=f"line {n} is not valid JSON")
        if rec.get("seq") != n:
            return VerifyResult(False, count, first_bad_seq=n, error=f"line {n} has seq {rec.get('seq')}")
        if rec.get("prev_hash") != prev:
            return VerifyResult(False, count, first_bad_seq=n, error=f"seq {n}: prev_hash does not link to seq {n - 1}")
        if rec.get("hash") != _hash(rec):
            return VerifyResult(False, count, first_bad_seq=n, error=f"seq {n}: hash does not match its content")
        prev, count = rec["hash"], n
    if head_file.exists():
        head = json.loads(head_file.read_text())
        if head.get("seq") != count or head.get("hash") != (prev if count else None):
            return VerifyResult(False, count, head_hash=prev, first_bad_seq=count + 1,
                                error=f"head pointer says seq {head.get('seq')}, log ends at {count} (truncated?)")
    elif count:
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
                state = verify(self.path)
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
                _head_path(self.path).write_text(json.dumps({"seq": rec["seq"], "hash": rec["hash"]}))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return rec

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]


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
