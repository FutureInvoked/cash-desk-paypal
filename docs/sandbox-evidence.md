# Sandbox evidence

Every run here is against `https://api-m.sandbox.paypal.com`. Only counts and statuses are recorded: no keys, tokens or emails.

## S1b live smoke test (2026-11-07)

- OAuth2 client-credentials token: ok. The app's scopes include Invoicing, Transaction Search and Payments refund.
- `GET /v2/invoicing/invoices`: ok, 0 invoices before seeding.
- `GET /v1/reporting/transactions` (last 30 days): ok, 0 transactions.

Lesson for the README: an app whose client ID starts with `B` was created as a newer app type. It was
not granted Invoicing or Transaction Search even with those features ticked. The account's classic
"Default Application" (client ID starting with `A`) had both.

## S2b seed (2026-11-07)

- `tools/seed_sandbox.py` created and sent 10 invoices (numbers 0001-0010): 9 in USD, 1 in EUR. All are `SENT`.
- The desk in `PAYPAL_MODE=sandbox` read 10 invoices, all `open`, with 0 transactions and 0 proposals.
  That's correct before any payment.

## S3 gated actions

Pending. This needs the owner's sandbox payments (see `docs/sandbox-pay-steps.md`, which is generated
and gitignored). Once those are in, Transaction Search can take up to about 3 hours to report them.
