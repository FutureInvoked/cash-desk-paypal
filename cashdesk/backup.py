"""Back up and restore the desk's data folder (var/): the SQLite database and the audit log.

    make backup                                   # = python -m cashdesk.backup create
    python -m cashdesk.backup create [DEST]       # default var/backups/<UTC time>
    python -m cashdesk.backup check DEST          # hashes, SQLite integrity, audit chain
    python -m cashdesk.backup restore SRC [VAR]   # into an empty data folder only

A backup is taken while the board runs. It holds the database's write lock (no approval, reject or
run can change state) while it copies the database with SQLite's online backup and then reads the
audit log and its head under the log's own lock. So the copied log is never behind the copied queue:
a restored desk can't run anything the log doesn't back, and never repeats a run the log shows.
Every copy is verified before the command says OK, and a manifest records each file's sha256.

Restore: stop the board, move the old data folder aside, restore into an empty folder, start the
board, and check the audit tab (or `make audit-verify`). A restore ends every session and withdraws
approvals that never ran (each with an audit record): PayPal may have moved on since the backup, so
they are proposed again on the next sync and need a fresh approval. Check the user list it prints.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .audit import _snapshot, verify

DB = "cashdesk.db"
LOG = "audit.jsonl"
HEAD = "audit.jsonl.head"
FILES = (DB, LOG, HEAD)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(var_dir: Path | str, dest: Path | str) -> dict:
    var_dir, dest = Path(var_dir), Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; a backup never overwrites")
    if not (var_dir / DB).exists():
        raise FileNotFoundError(f"no {DB} in {var_dir}")
    dest.mkdir(parents=True)
    hold = sqlite3.connect(var_dir / DB, timeout=30, isolation_level=None)
    try:
        hold.execute("begin immediate")  # queue transitions (and their audit records) wait for us
        src, dst = sqlite3.connect(var_dir / DB, timeout=30), sqlite3.connect(dest / DB)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        if (var_dir / LOG).exists():
            text, (head_exists, head) = _snapshot(var_dir / LOG)
            (dest / LOG).write_text(text)
            if head_exists:
                (dest / HEAD).write_text(json.dumps(head) if head is not None else "")
    finally:
        hold.execute("rollback")
        hold.close()
    v = verify(dest / LOG)
    if not v.ok:
        raise RuntimeError(f"the copied audit log does not verify ({v.error}); the live log needs checking: "
                           f"make audit-verify")
    con = sqlite3.connect(dest / DB)
    try:
        ok = con.execute("pragma integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute("select name from sqlite_master where type = 'table' order by name")]
        rows = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()
    if ok != "ok":
        raise RuntimeError(f"the copied database fails SQLite's integrity check: {ok}")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cashdesk_version": __version__,
        "source": str(var_dir.resolve()),
        "files": {f: {"sha256": _sha256(dest / f), "bytes": (dest / f).stat().st_size} for f in FILES if (dest / f).exists()},
        "audit": {"count": v.count, "head_hash": v.head_hash},
        "rows": rows,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def check(dest: Path | str) -> list[str]:
    """Problems with a backup folder (empty list: it is sound). Never writes to it."""
    dest = Path(dest)
    try:
        manifest = json.loads((dest / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        return [f"manifest.json unreadable: {e}"]
    files = manifest.get("files") or {}
    audit = manifest.get("audit") or {}
    problems = []
    if DB not in files:
        problems.append(f"the manifest lists no {DB}")
    if (audit.get("count") or 0) > 0 and LOG not in files:
        problems.append(f"the manifest counts {audit.get('count')} audit records but lists no {LOG}")
    for name, info in files.items():
        p = dest / name
        if not p.exists():
            problems.append(f"{name} is missing")
        elif _sha256(p) != info.get("sha256"):
            problems.append(f"{name} changed since the backup (sha256 differs)")
    if problems:
        return problems
    try:
        con = sqlite3.connect(f"file:{(dest / DB).resolve()}?mode=ro", uri=True)  # read only: never creates
        try:
            ok = con.execute("pragma integrity_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        ok = str(e)
    if ok != "ok":
        problems.append(f"{DB} fails SQLite's integrity check: {ok}")
    if (dest / LOG).exists():
        v = verify(dest / LOG)
        if not v.ok:
            problems.append(f"{LOG} does not verify: {v.error}")
        elif (v.count, v.head_hash) != (audit.get("count"), audit.get("head_hash")):
            problems.append(f"the audit log has {v.count} records, the manifest says {audit.get('count')}")
    return problems


def restore(src: Path | str, var_dir: Path | str) -> dict:
    src, var_dir = Path(src), Path(var_dir)
    problems = check(src)
    if problems:
        raise ValueError("not restoring a backup that fails its check: " + "; ".join(problems))
    clash = [f for f in FILES if (var_dir / f).exists()]
    if clash:
        raise FileExistsError(f"{var_dir} already holds {', '.join(clash)}: stop the board and move that folder "
                              f"aside first (restore only fills an empty data folder)")
    var_dir.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if (src / f).exists():
            shutil.copy2(src / f, var_dir / f)
    from .audit import AuditLog
    from .queue import ApprovalQueue
    from .users import UserStore
    users = UserStore(var_dir / DB)
    with users._conn() as con:
        ended = con.execute("delete from sessions").rowcount  # everyone signs in again
    withdrawn = ApprovalQueue(var_dir / DB, on_event=AuditLog(var_dir / LOG).append).withdraw_unstarted(
        "restored from a backup: approve it again after a sync")
    v = verify(var_dir / LOG)
    if not v.ok:
        raise RuntimeError(f"restored, but the audit log does not verify: {v.error}")
    return {"sessions_ended": ended, "withdrawn": withdrawn,
            "users": [(u.username, u.display_name, u.role, u.disabled) for u in users.list()]}


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import load_settings

    p = argparse.ArgumentParser(prog="python -m cashdesk.backup", description="Back up and restore var/.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="back up the data folder")
    c.add_argument("dest", nargs="?")
    k = sub.add_parser("check", help="check a backup folder")
    k.add_argument("dest")
    r = sub.add_parser("restore", help="restore into an empty data folder")
    r.add_argument("src")
    r.add_argument("var_dir", nargs="?")
    args = p.parse_args(argv)
    var_dir = load_settings().var_dir
    try:
        if args.cmd == "create":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = Path(args.dest) if args.dest else var_dir / "backups" / stamp
            m = backup(var_dir, dest)
            print(f"OK: backup in {dest} ({m['audit']['count']} audit records, chain verified; "
                  f"{sum(m['rows'].values())} database rows)")
        elif args.cmd == "check":
            problems = check(args.dest)
            for x in problems:
                print(f"problem: {x}")
            if problems:
                return 1
            print(f"OK: {args.dest} is intact")
        else:
            target = Path(args.var_dir) if args.var_dir else var_dir
            out = restore(args.src, target)
            print(f"OK: restored into {target}; {out['sessions_ended']} session(s) ended; "
                  f"{len(out['withdrawn'])} approval(s) that never ran withdrawn (approve them again after a sync)")
            print("Accounts in the restored data (check them; disable any that should no longer approve):")
            for username, name, role, disabled in out["users"]:
                print(f"  {username}\t{name}\t{role}" + ("\t(disabled)" if disabled else ""))
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
