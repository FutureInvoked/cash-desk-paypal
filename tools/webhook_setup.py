"""Register, list or delete the Cash Desk webhook on your PayPal sandbox app.

    python -m tools.webhook_setup create https://<public-host>/webhooks/paypal
    python -m tools.webhook_setup list
    python -m tools.webhook_setup delete <webhook_id>

Uses the app's own credentials from repo/.env (sandbox only). `create` prints the webhook id to
put in .env as PAYPAL_WEBHOOK_ID. Endpoints: Webhooks Management v1 (POST/GET/DELETE
/v1/notifications/webhooks).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cashdesk.webhooks import EVENT_TYPES  # noqa: E402


def create(client, url: str) -> dict:
    if not url.startswith("https://") or not url.endswith("/webhooks/paypal"):
        raise ValueError("the URL must be https and end with /webhooks/paypal")
    body = {"url": url, "event_types": [{"name": n} for n in EVENT_TYPES]}
    return client._call("POST", "/v1/notifications/webhooks", json=body).json()


def list_hooks(client) -> list[dict]:
    return client._call("GET", "/v1/notifications/webhooks").json().get("webhooks", [])


def delete(client, webhook_id: str) -> None:
    client._call("DELETE", f"/v1/notifications/webhooks/{webhook_id}")


def main(argv: list[str]) -> int:
    from cashdesk.config import load_settings
    from cashdesk.paypal.sandbox import SandboxPayPalClient
    load_settings()
    c = SandboxPayPalClient.from_env()
    if argv[:1] == ["create"] and len(argv) == 2:
        h = create(c, argv[1])
        print(f"created webhook {h['id']} -> {h['url']}\nadd to repo/.env:  PAYPAL_WEBHOOK_ID={h['id']}")
    elif argv[:1] == ["list"]:
        for h in list_hooks(c):
            print(h["id"], h["url"], ",".join(e["name"] for e in h.get("event_types", [])))
    elif argv[:1] == ["delete"] and len(argv) == 2:
        delete(c, argv[1])
        print(f"deleted webhook {argv[1]}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
