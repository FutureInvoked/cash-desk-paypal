"""S1b: live smoke test against the PayPal sandbox. Prints counts and statuses only, never secrets.

Usage: .venv/bin/python tools/sandbox_smoke.py      (needs PAYPAL_CLIENT_ID/SECRET in repo/.env)
Read-only: token, list invoices, transaction search for the last 30 days. Exit 0 on success.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cashdesk.config import load_dotenv  # noqa: E402
from cashdesk.paypal.sandbox import REPORTING_SCOPE, PayPalError, SandboxPayPalClient  # noqa: E402


def main() -> int:
    names = load_dotenv()
    print(f"loaded from .env: {', '.join(names) or '(nothing new)'}")
    try:
        c = SandboxPayPalClient.from_env()
    except RuntimeError as e:
        print(f"SKIP: {e}")
        return 2
    print(f"base url: {c.base_url}")
    try:
        invs = c.list_invoices()
        print(f"token: ok; reporting scope: {'yes' if REPORTING_SCOPE in c.scope.split() else 'NO (enable Transaction search)'}")
        print(f"invoices: {len(invs)} ({', '.join(sorted({i.status for i in invs})) or 'none'})")
        end = datetime.now(timezone.utc)
        txns = c.search_transactions(end - timedelta(days=30), end)
        src = sum(1 for t in txns if t.raw.get("cashdesk_source") == "invoicing")
        print(f"transactions (30 days): {len(txns)} ({src} from Invoicing while reporting lags)")
    except PayPalError as e:
        print(f"FAIL: {e}")  # status, name, path, debug_id only
        return 1
    print("SANDBOX SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
