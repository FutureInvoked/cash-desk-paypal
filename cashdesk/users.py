"""Approver accounts and board sessions, in the desk's SQLite file (var/cashdesk.db).

Passwords: stdlib scrypt with a per-user salt, compared in constant time. Sessions: a random token
in an HttpOnly cookie; only its sha256 is stored. Sessions end after 8 hours idle or 24 hours in all.

Command line (passwords are read from a prompt, or one line on stdin for scripts; never from argv):

    python -m cashdesk.users add alice --name "Alice Smith" --role approver
    python -m cashdesk.users list
    python -m cashdesk.users disable alice
    python -m cashdesk.users set-password alice
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROLES = ("admin", "approver", "viewer")
WRITE_ROLES = ("admin", "approver")
IDLE_TTL = timedelta(hours=8)
ABSOLUTE_TTL = timedelta(hours=24)
# five wrong passwords for one username, or from one address, wait 15 minutes (stored in SQLite, so
# a restart or a second process doesn't reset it)
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)
MIN_PASSWORD = 10
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
UNUSABLE = "!"  # demo users sign in with one click (sample data only), never with a password

# two approvers so the two-person rule can be shown on sample data; never created outside it
DEMO_USERS = (("alex", "Alex (owner)"), ("sam", "Sam (bookkeeper)"))

SCHEMA = """
create table if not exists users (
    username text primary key,
    display_name text not null,
    role text not null,
    password_hash text not null,
    demo integer not null default 0,
    disabled integer not null default 0,
    created_at text not null
);
create table if not exists sessions (
    token_hash text primary key,
    username text not null references users(username),
    created_at text not null,
    last_seen text not null
);
create table if not exists login_failures (
    key text not null,
    at text not null
);
create index if not exists login_failures_key on login_failures (key, at);
"""


class AccountError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    role: str
    demo: bool = False
    disabled: bool = False

    @property
    def identity(self) -> str:
        """Who approved, as the queue and the audit log record it."""
        return f"user:{self.username}"

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES and not self.disabled

    def to_dict(self) -> dict:
        return {"username": self.username, "display_name": self.display_name, "role": self.role,
                "demo": self.demo, "can_write": self.can_write}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


_DUMMY = hash_password(secrets.token_hex(16))  # an unknown user costs the same as a wrong password


def clean_name(name: str) -> str:
    """One line, no control or invisible format characters, at most 80 characters."""
    n = unicodedata.normalize("NFKC", str(name or ""))
    n = "".join(ch for ch in n if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    n = "".join(ch for ch in n if ch not in "ᅟᅠㅤﾠ⠀")
    return re.sub(r"\s+", " ", n).strip()[:80]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class UserStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._conn() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _user(row) -> User:
        return User(row["username"], row["display_name"], row["role"], bool(row["demo"]), bool(row["disabled"]))

    # ---- accounts ----
    def add(self, username: str, display_name: str, role: str, password: str) -> User:
        username = (username or "").strip().lower()
        if not USERNAME.match(username):
            raise AccountError("username: 2-32 characters, letters, digits, '.', '_' or '-'")
        name = clean_name(display_name)
        if not name:
            raise AccountError("display name is empty")
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"password must be at least {MIN_PASSWORD} characters")
        return self._insert(username, name, role, hash_password(password), demo=False)

    def _insert(self, username: str, name: str, role: str, pw_hash: str, *, demo: bool) -> User:
        try:
            with self._conn() as con:
                con.execute("insert into users (username, display_name, role, password_hash, demo, created_at)"
                            " values (?,?,?,?,?,?)", (username, name, role, pw_hash, int(demo), _iso(self.clock())))
        except sqlite3.IntegrityError:
            raise AccountError(f"user {username!r} already exists") from None
        return User(username, name, role, demo)

    def ensure_demo_users(self) -> None:
        for username, name in DEMO_USERS:
            if self.get(username) is None:
                try:
                    self._insert(username, name, "approver", UNUSABLE, demo=True)
                except AccountError:
                    pass  # another process made it first

    def get(self, username: str) -> User | None:
        with self._conn() as con:
            row = con.execute("select * from users where username = ?", ((username or "").strip().lower(),)).fetchone()
        return self._user(row) if row else None

    def list(self) -> list[User]:
        with self._conn() as con:
            return [self._user(r) for r in con.execute("select * from users order by username")]

    def demo_users(self) -> list[User]:
        return [u for u in self.list() if u.demo and not u.disabled]

    def disable(self, username: str) -> None:
        with self._conn() as con:
            n = con.execute("update users set disabled = 1 where username = ?", (username.strip().lower(),)).rowcount
            con.execute("delete from sessions where username = ?", (username.strip().lower(),))
        if not n:
            raise AccountError(f"no user {username!r}")

    def set_password(self, username: str, password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"password must be at least {MIN_PASSWORD} characters")
        u = username.strip().lower()
        with self._conn() as con:
            n = con.execute("update users set password_hash = ? where username = ? and demo = 0",
                            (hash_password(password), u)).rowcount
            con.execute("delete from sessions where username = ?", (u,))
        if not n:
            raise AccountError(f"no user {username!r} with a password")

    def authenticate(self, username: str, password: str) -> User | None:
        with self._conn() as con:
            row = con.execute("select * from users where username = ?", ((username or "").strip().lower(),)).fetchone()
        ok = check_password(password or "", row["password_hash"] if row else _DUMMY)
        if not row or not ok or row["disabled"] or row["demo"]:
            return None
        return self._user(row)

    # ---- sign-in throttle ----
    @staticmethod
    def _keys(username: str, ip: str | None) -> list[str]:
        keys = [f"user:{(username or '').strip().lower()[:64]}"]
        if ip:
            keys.append(f"ip:{ip}")
        return keys

    def login_blocked(self, username: str, ip: str | None) -> bool:
        since = _iso(self.clock() - FAILURE_WINDOW)
        with self._conn() as con:
            return any(con.execute("select count(*) from login_failures where key = ? and at > ?", (k, since)).fetchone()[0]
                       >= MAX_FAILURES for k in self._keys(username, ip))

    def login_failed(self, username: str, ip: str | None) -> None:
        now = self.clock()
        with self._conn() as con:
            con.execute("delete from login_failures where at <= ?", (_iso(now - FAILURE_WINDOW),))
            con.executemany("insert into login_failures (key, at) values (?, ?)",
                            [(k, _iso(now)) for k in self._keys(username, ip)])

    def login_succeeded(self, username: str) -> None:
        with self._conn() as con:
            con.execute("delete from login_failures where key = ?", (self._keys(username, None)[0],))

    def begin_attempt(self, username: str, ip: str | None) -> list[int] | None:
        """Check the throttle and record this attempt as a failure in ONE write transaction, before the
        password is checked: concurrent guesses see each other, so at most five are ever checked.
        None: throttled. Otherwise the ids of the provisional failure rows (end_attempt clears them)."""
        now = self.clock()
        since = _iso(now - FAILURE_WINDOW)
        keys = self._keys(username, ip)
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            con.execute("begin immediate")
            try:
                if any(con.execute("select count(*) from login_failures where key = ? and at > ?", (k, since)).fetchone()[0]
                       >= MAX_FAILURES for k in keys):
                    con.execute("rollback")
                    return None
                con.execute("delete from login_failures where at <= ?", (since,))
                ids = [con.execute("insert into login_failures (key, at) values (?, ?)", (k, _iso(now))).lastrowid
                       for k in keys]
                con.execute("commit")
                return ids
            except BaseException:
                con.execute("rollback")
                raise
        finally:
            con.close()

    def end_attempt(self, ids: list[int], user: "User | None") -> None:
        """A good password removes this attempt's rows and the username's earlier failures."""
        if user is None:
            return
        with self._conn() as con:
            con.executemany("delete from login_failures where rowid = ?", [(i,) for i in ids])
            con.execute("delete from login_failures where key = ?", (self._keys(user.username, None)[0],))

    # ---- sessions ----
    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _iso(self.clock())
        with self._conn() as con:
            con.execute("insert into sessions (token_hash, username, created_at, last_seen) values (?,?,?,?)",
                        (_token_hash(token), username, now, now))
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token:
            return None
        now = self.clock()
        th = _token_hash(token)
        with self._conn() as con:
            row = con.execute("select s.created_at, s.last_seen, u.* from sessions s join users u using (username)"
                              " where s.token_hash = ?", (th,)).fetchone()
            if row is None:
                return None
            created, seen = datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["last_seen"])
            if row["disabled"] or now - seen > IDLE_TTL or now - created > ABSOLUTE_TTL:
                con.execute("delete from sessions where token_hash = ?", (th,))
                return None
            con.execute("update sessions set last_seen = ? where token_hash = ?", (_iso(now), th))
        return self._user(row)

    def end_session(self, token: str | None) -> None:
        if token:
            with self._conn() as con:
                con.execute("delete from sessions where token_hash = ?", (_token_hash(token),))


# ---- command line ----

class _NoArgvPasswords(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        ns, extra = self.parse_known_args(args, namespace)
        if extra:  # name the refused options, never their values
            self.error("unexpected arguments (passwords are never taken from the command line): "
                       + " ".join(a.split("=", 1)[0] for a in extra if a.startswith("-")))
        return ns


def _read_password(prompt: str) -> str:
    if sys.stdin.isatty():
        import getpass
        first = getpass.getpass(prompt)
        if getpass.getpass("Again: ") != first:
            raise AccountError("the two passwords differ")
        return first
    return sys.stdin.readline().rstrip("\n")


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings

    p = _NoArgvPasswords(prog="python -m cashdesk.users", description="Manage Cash Desk approver accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a user (password from a prompt or stdin)")
    a.add_argument("username")
    a.add_argument("--name", required=True, help="the name shown on approvals")
    a.add_argument("--role", default="approver", choices=ROLES)
    sub.add_parser("list", help="list users")
    d = sub.add_parser("disable", help="disable a user and end their sessions")
    d.add_argument("username")
    s = sub.add_parser("set-password", help="set a new password (prompt or stdin)")
    s.add_argument("username")
    args = p.parse_args(argv)

    store = UserStore(load_settings().var_dir / "cashdesk.db")
    try:
        if args.cmd == "add":
            u = store.add(args.username, args.name, args.role, _read_password("Password: "))
            print(f"added {u.username} ({u.display_name}, {u.role})")
        elif args.cmd == "list":
            for u in store.list():
                flags = ", ".join(f for f, on in (("demo", u.demo), ("disabled", u.disabled)) if on)
                print(f"{u.username}\t{u.display_name}\t{u.role}" + (f"\t({flags})" if flags else ""))
        elif args.cmd == "disable":
            store.disable(args.username)
            print(f"disabled {args.username}")
        elif args.cmd == "set-password":
            store.set_password(args.username, _read_password("New password: "))
            print(f"password changed for {args.username}; their sessions were ended")
    except AccountError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
