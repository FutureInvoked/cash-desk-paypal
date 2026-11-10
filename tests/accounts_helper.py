"""Signing a test client in (every board route needs a session since P1)."""
from __future__ import annotations

PASSWORD = "correct horse battery staple"


def sign_in(client, desk, username: str = "owner", *, role: str = "approver", name: str | None = None):
    """Create the user if needed and sign the client in with a real password login."""
    if desk.users.get(username) is None:
        desk.users.add(username, name or username.title(), role, PASSWORD)
    r = client.post("/api/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def demo_sign_in(client, username: str = "alex"):
    """Sample-data mode: the one-click sign-in the board offers."""
    r = client.post("/api/demo-login", json={"username": username})
    assert r.status_code == 200, r.text
    return client
