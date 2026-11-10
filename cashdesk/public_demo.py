"""Public-demo mode (CASHDESK_PUBLIC_DEMO=1), for the hosted board on Render.

Sample data only: the server refuses to start with any PayPal or AI credential, or in sandbox mode.
Each visitor gets a private workspace (its own SQLite file, audit log and the two demo approvers),
created by the one-click demo sign-in and found again by the `cashdesk_demo` cookie. Page loads,
health checks and bots never create one. Workspaces are capped (least recently used goes first),
dropped after 2 hours idle and on "Reset demo", and one address can only open so many an hour.
Every address is held to a request rate. Settings: CASHDESK_DEMO_MAX_WORKSPACES (50),
CASHDESK_DEMO_IDLE_MINUTES (120), CASHDESK_DEMO_NEW_PER_HOUR (10), CASHDESK_DEMO_RATE (per minute, 120).

Behind Render's proxy (CASHDESK_BEHIND_PROXY=1) the client is the rightmost X-Forwarded-For entry,
the one Render's proxy appended, so a visitor can't pick their own address; the scheme comes from
X-Forwarded-Proto. Without the flag both headers are ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

DEMO_COOKIE = "cashdesk_demo"
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,64}")
CREDENTIALS = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY",
               "CODEX_BIN")
BANNER = ("Public demo with sample data. No PayPal calls. Your changes are private to this browser "
          "and reset after 2 hours.")

# the workspace of the request being served (None: this visitor has none yet)
current_desk: ContextVar = ContextVar("cashdesk_demo_desk", default=None)


def enabled() -> bool:
    return os.environ.get("CASHDESK_PUBLIC_DEMO", "").strip() == "1"


def behind_proxy() -> bool:
    return os.environ.get("CASHDESK_BEHIND_PROXY", "").strip() == "1"


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def guardrail_errors(paypal_mode: str) -> list[str]:
    """Why the public demo may not start (empty: it may)."""
    errors = [f"{name} is set, but the public demo never holds a credential: remove it"
              for name in CREDENTIALS if os.environ.get(name)]
    if paypal_mode != "fixtures":
        errors.append(f"PAYPAL_MODE={paypal_mode}: the public demo runs on sample data only (PAYPAL_MODE=fixtures)")
    provider = (os.environ.get("AI_PROVIDER") or "none").strip().lower()
    if provider != "none":
        errors.append(f"AI_PROVIDER={provider}: the public demo runs without AI (AI_PROVIDER=none)")
    return errors


class WorkspacePool:
    def __init__(self, root: Path | str, *, clock: Callable[[], datetime] | None = None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max = _int("CASHDESK_DEMO_MAX_WORKSPACES", 50)
        self.idle = timedelta(minutes=_int("CASHDESK_DEMO_IDLE_MINUTES", 120))
        self.new_per_hour = _int("CASHDESK_DEMO_NEW_PER_HOUR", 10)
        self.rate = _int("CASHDESK_DEMO_RATE", 120)
        self._lock = threading.Lock()
        self._spaces: dict[str, list] = {}  # token -> [desk, last used]
        self._hits: dict[str, deque] = {}
        self._opened: dict[str, deque] = {}
        shutil.rmtree(self.root, ignore_errors=True)  # a new process starts with no workspaces
        self.root.mkdir(parents=True, exist_ok=True)

    def path_of(self, token: str) -> Path:
        return self.root / hashlib.sha256(token.encode()).hexdigest()[:32]

    def count(self) -> int:
        with self._lock:
            return len(self._spaces)

    def _sweep(self, now: datetime) -> None:
        for token in [t for t, (_, used) in self._spaces.items() if now - used > self.idle]:
            self._drop(token)

    def _drop(self, token: str) -> None:
        self._spaces.pop(token, None)
        shutil.rmtree(self.path_of(token), ignore_errors=True)

    def get(self, token: str | None):
        if not token or not TOKEN.fullmatch(token):
            return None
        now = self.clock()
        with self._lock:
            self._sweep(now)
            space = self._spaces.get(token)
            if space is None:
                return None
            space[1] = now
            return space[0]

    def create(self) -> tuple[str, object]:
        from .ai import FallbackReconciler
        from .paypal import FixturePayPalClient
        from .service import Desk
        now = self.clock()
        with self._lock:
            self._sweep(now)
            while len(self._spaces) >= self.max:
                oldest = min(self._spaces, key=lambda t: self._spaces[t][1])
                self._drop(oldest)
            token = secrets.token_urlsafe(24)
            desk = Desk(client=FixturePayPalClient(), var_dir=self.path_of(token),
                        reconciler=FallbackReconciler("fallback (public demo: sample data, no AI)"))
            self._spaces[token] = [desk, now]
            return token, desk

    def drop(self, token: str | None) -> bool:
        if not token or not TOKEN.fullmatch(token):
            return False
        with self._lock:
            known = token in self._spaces
            self._drop(token)
            return known

    def _within(self, book: dict, key: str, limit: int, window: timedelta) -> bool:
        now = self.clock()
        with self._lock:
            q = book.setdefault(key, deque())
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(book) > 10_000:  # forget quiet addresses
                for k in [k for k, v in book.items() if not v or now - v[-1] > window]:
                    book.pop(k, None)
            return True

    def allow_request(self, ip: str) -> bool:
        return self._within(self._hits, ip, self.rate, timedelta(minutes=1))

    def allow_new(self, ip: str) -> bool:
        return self._within(self._opened, ip, self.new_per_hour, timedelta(hours=1))


def _cookie(scope, name: str) -> str | None:
    from http.cookies import SimpleCookie
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:  # noqa: BLE001 - a malformed cookie header is no cookie
                return None
            return jar[name].value if name in jar else None
    return None


async def _reply(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class DemoGate:
    """Request rate per address; finds the visitor's workspace (or opens one on demo sign-in)."""

    def __init__(self, app, pool: WorkspacePool):
        self.app, self.pool = app, pool

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "")
        ip = (scope.get("client") or ("unknown", 0))[0]
        if path != "/api/health" and not self.pool.allow_request(ip):
            return await _reply(send, 429, "too many requests from your address; wait a minute")
        token = _cookie(scope, DEMO_COOKIE)
        desk = self.pool.get(token)
        opened = None
        if desk is None and path == "/api/demo-login" and method == "POST":
            if not self.pool.allow_new(ip):
                return await _reply(send, 429, "too many demo workspaces opened from your address; try again later")
            opened, desk = self.pool.create()
        secure = "; Secure" if scope.get("scheme") == "https" else ""

        async def send_with_cookie(message):
            if opened and message["type"] == "http.response.start":
                cookie = f"{DEMO_COOKIE}={opened}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400{secure}"
                message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie.encode())]}
            await send(message)

        mark = current_desk.set(desk)
        try:
            await self.app(scope, receive, send_with_cookie)
        finally:
            current_desk.reset(mark)


class ProxyHeaders:
    """Behind Render's proxy: client = rightmost X-Forwarded-For, scheme = X-Forwarded-Proto."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            xff = [p.strip() for p in headers.get(b"x-forwarded-for", "").split(",") if p.strip()]
            proto = (headers.get(b"x-forwarded-proto", "").split(",")[-1]).strip().lower()
            scope = dict(scope)
            if xff:
                scope["client"] = (xff[-1], 0)
            if proto in ("http", "https"):
                scope["scheme"] = proto
        await self.app(scope, receive, send)


class HostGuard:
    """The trusted-host check, except GET /api/health, which a platform's health check calls by IP."""

    def __init__(self, app, allowed_hosts: list[str]):
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        self.app = app
        self.guarded = TrustedHostMiddleware(app, allowed_hosts=allowed_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/api/health" and scope.get("method") in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        return await self.guarded(scope, receive, send)
