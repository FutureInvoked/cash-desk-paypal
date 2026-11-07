# Cash Desk for PayPal

An AI reconciliation and collections desk for small businesses paid through PayPal.
It matches PayPal payments to invoices, explains every match and exception, and proposes
actions (reminders, draft invoices, refunds of duplicates) that only run after a policy
check and a human approval, with a hash-chained audit log.

Status: under construction for the PayPal AI Hackathon (Nov 2026).

## Quick start (no keys needed)

```bash
make demo   # installs into .venv, serves http://localhost:8000 using fixtures
make test
```

Requires Python 3.11+ (uses `uv` if installed, else `venv` + `pip`).

## Licence

MIT, see [LICENSE](LICENSE).
