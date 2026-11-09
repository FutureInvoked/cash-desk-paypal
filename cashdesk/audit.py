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
    """Check the chain and the head pointer.

    An append writes the head first as {seq, hash, next: <hash of the record about to be written>},
    then the line, then the head at the new record. So a crash mid-append leaves one of these, and
    only these verify as OK (the next append repairs them):
      - a cut-off last line, while the head says a record was in flight;
      - the in-flight record written (with or without its newline), the head still one behind and
        naming exactly that record as `next`.
    A record appended by hand is not `next` in the head, so it fails verification."""
    path = Path(path or default_path())
    head_file = _head_path(path)
    if not path.exists():
        if head_file.exists():
            return VerifyResult(False, 0, error="log missing but head pointer exists")
        return VerifyResult(True, 0)
    return _verify_text("\n".join(_read_lines(path)), head_file)


@dataclass
class _Chain:
    count: int = 0
    prev: str = GENESIS
    prev_before: str | None = None


def _verify_text(text: str, head_file: Path, start: _Chain | None = None, offset_line: int = 0) -> VerifyResult:
    res, _ = _verify_state(text, head_file, start)
    return res


def _verify_state(text: str, head_file: Path, start: _Chain | None = None) -> tuple[VerifyResult, dict]:
    """`start`: the chain state the text continues from (an append that already verified the start
    of the file reads only what was added since). Returns the result and what a repair would do."""
    ch = _Chain(**vars(start)) if start else _Chain()
    pieces = text.split("\n")
    lines, tail = pieces[:-1], pieces[-1]
    fix = {"truncate_tail": False, "terminate_tail": False}

    def check(n: int, line: str) -> str | None:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return f"line {n} is not valid JSON"
        if not isinstance(rec, dict) or rec.get("seq") != n:
            return f"line {n} has seq {rec.get('seq') if isinstance(rec, dict) else '?'}"
        if rec.get("prev_hash") != ch.prev:
            return f"seq {n}: prev_hash does not link to seq {n - 1}"
        if rec.get("hash") != _hash(rec):
            return f"seq {n}: hash does not match its content"
        ch.prev_before, ch.prev, ch.count = ch.prev, rec["hash"], n
        return None

    for line in lines:
        err = check(ch.count + 1, line)
        if err:
            return VerifyResult(False, ch.count, first_bad_seq=ch.count + 1, error=err), fix
    head = _read_head(head_file) if head_file.exists() else None
    if head_file.exists() and head is None:
        return VerifyResult(False, ch.count, head_hash=ch.prev, error="head pointer is unreadable"), fix
    in_flight = (head or {}).get("next")
    if tail:
        before = _Chain(**vars(ch))
        if check(ch.count + 1, tail) is None and in_flight == ch.prev:
            fix["terminate_tail"] = True  # the in-flight record, written but for its newline
        else:
            ch = before
            if not in_flight:
                return VerifyResult(False, ch.count, head_hash=ch.prev, first_bad_seq=ch.count + 1,
                                    error=f"bytes after record {ch.count} that are not a record"), fix
            fix["truncate_tail"] = True  # a record cut off mid-write
    if head is not None:
        at_end = head.get("seq") == ch.count and head.get("hash") == (ch.prev if ch.count else None)
        behind = (ch.count >= 1 and head.get("seq") == ch.count - 1 and in_flight == ch.prev
                  and head.get("hash") == (ch.prev_before if ch.count > 1 else None))
        if not (at_end or behind):
            return VerifyResult(False, ch.count, head_hash=ch.prev, first_bad_seq=ch.count + 1,
                                error=f"head pointer says seq {head.get('seq')}, log ends at {ch.count} "
                                      f"(truncated, or a record added by hand?)"), fix
    elif ch.count > 1 or (ch.count == 1 and start is not None):
        return VerifyResult(False, ch.count, head_hash=ch.prev, error="head pointer missing"), fix
    fix["chain"] = ch
    return VerifyResult(True, ch.count, head_hash=ch.prev if ch.count else None), fix


class AuditLog:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # (inode, verified byte size, chain state): an append reads only what was added since
        self._verified: tuple[int, int, _Chain] | None = None

    def append(self, event: dict) -> dict:
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                state, ch = self._repair_and_verify(f)
                if not state.ok:
                    raise ValueError(f"refusing to append to a broken audit log: {state.error}")
                rec = redact(dict(event))
                rec.setdefault("at", datetime.now(timezone.utc).isoformat())
                rec["seq"] = state.count + 1
                rec["prev_hash"] = state.head_hash or GENESIS
                rec.pop("hash", None)
                rec["hash"] = _hash(rec)
                # 1. announce the record in the head, 2. write it, 3. move the head onto it
                self._write_head(state.count, state.head_hash, nxt=rec["hash"])
                f.seek(0, os.SEEK_END)
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
                self._write_head(rec["seq"], rec["hash"])
                st = os.fstat(f.fileno())
                self._verified = (st.st_ino, st.st_size, _Chain(rec["seq"], rec["hash"], ch.prev))
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return rec

    def _write_head(self, seq: int, h: str | None, *, nxt: str | None = None) -> None:
        """Atomically: a crash leaves the old head or the new one, never an empty file."""
        head = _head_path(self.path)
        tmp = head.with_name(head.name + ".tmp")
        body = {"seq": seq, "hash": h} | ({"next": nxt} if nxt else {})
        with open(tmp, "w") as hf:
            hf.write(json.dumps(body))
            hf.flush()
            os.fsync(hf.fileno())
        os.replace(tmp, head)

    def _repair_and_verify(self, f) -> tuple[VerifyResult, _Chain]:
        """Under the exclusive lock. Verify first (from the last verified point when the file only
        grew); repair a crash leftover only once the log is known to be sound."""
        st = os.fstat(f.fileno())
        start, offset = None, 0
        if self._verified and self._verified[0] == st.st_ino and self._verified[1] <= st.st_size:
            offset, start = self._verified[1], self._verified[2]
        f.seek(offset)
        state, fix = _verify_state(f.read(), _head_path(self.path), start)
        if not state.ok and start is not None:  # something changed before our mark: check it all
            self._verified = None
            return self._repair_and_verify(f)
        if not state.ok:
            return state, _Chain()
        ch: _Chain = fix["chain"]
        if fix["truncate_tail"] or fix["terminate_tail"]:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if fix["terminate_tail"]:
                f.write("\n")
            else:
                f.seek(offset)
                text = f.read()
                f.truncate(offset + len(text.encode()) - len(text.split("\n")[-1].encode()) if text else end)
            f.flush()
            os.fsync(f.fileno())
        head = _read_head(_head_path(self.path)) or {}
        if ch.count and (head.get("seq") != ch.count or head.get("next")):
            self._write_head(ch.count, ch.prev)
        return state, ch

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = _read_lines(self.path)
        out = [json.loads(x) for x in lines[:-1] if x.strip()]
        if lines[-1].strip():  # the in-flight record written but for its newline counts once it verifies
            try:
                rec = json.loads(lines[-1])
                if isinstance(rec, dict) and rec.get("seq") == len(out) + 1 and rec.get("hash") == _hash(rec):
                    out.append(rec)
            except json.JSONDecodeError:
                pass
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
