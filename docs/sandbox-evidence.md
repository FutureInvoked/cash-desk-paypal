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

### Re-seed (2026-11-07)

The first buyer email was the sandbox **business** account, which can't pay its own invoices. We
cancelled the 7 invoices addressed to it (0001-0004, 0007, 0009, 0010) with
`POST /v2/invoicing/invoices/{id}/cancel` and no emails sent. The seed then re-created them for the
**personal** (buyer) account as 0011-0017. The fix to the seed: cancelled invoices don't count as seeded,
and request ids include a hash of the payer, so PayPal's idempotency doesn't return the old invoices.
Live set: 0005, 0006, 0008 go to fictional customers and stay unpaid or are paid from another
address; 0011-0017 go to the buyer.

## First real payments (2026-11-07 ~10:20)

The owner paid invoices 0011 and 0012 from the sandbox buyer account. Real-API finding:
`GET /v2/invoicing/invoices` (even with `fields=all`) returns `payments.paid_amount` but not
`payments.transactions`. So the client now fetches `GET /v2/invoicing/invoices/{id}` for any invoice
with money on it. After that fix the desk shows 2 transactions, both **matched** by invoice reference
(0011: 125.00 USD, 0012: 86.00 USD). They came from Invoicing while Transaction Search lags.

## S3 gated actions

Pending. This needs the owner's sandbox payments (see `docs/sandbox-pay-steps.md`, which is generated
and gitignored). Once those are in, Transaction Search can take up to about 3 hours to report them.
