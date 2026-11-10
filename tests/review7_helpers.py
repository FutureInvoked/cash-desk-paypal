"""Helpers for tests/test_review7.py (the seventh, independent review of 24abfec..564c918)."""
from __future__ import annotations

from datetime import datetime, timezone

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


def account(desk, username, role="approver", name=None):
    u = desk.users.get(username)
    return u if u is not None else desk.users.add(username, name or username.title(), role, PASSWORD)


def pending(desk, ptype):
    return next(i for i in desk.queue.list() if i.proposal.type == ptype and i.status == "pending")


def h(item):
    from cashdesk.queue import proposal_hash
    return proposal_hash(item.proposal)


def refund_calls(client):
    return [c for c in client.calls if "/refund" in c["path"]]
