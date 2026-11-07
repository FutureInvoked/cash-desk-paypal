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

## S4 parity: invoices (2026-11-07)

Two real invoices are recorded in `fixtures/sandbox-recorded/`: 0011 (paid) and 0005 (unpaid).
Names and emails were scrubbed, and phones and addresses removed. `tests/test_parity.py` asserts
that every invoice field the parser and matcher read is present in the real responses, and that real
and fixture invoices parse the same way. Findings:
- The real API returns many fields beyond the published schema, for example `detail.archived`,
  `payments.transactions[].transaction_type`, and top-level `settings` and `unilateral`. The parser
  ignores unknown fields.
- The list endpoint's `detail.metadata` has only `create_time`, with no `last_sent_time`. "Last sent"
  is optional throughout.
- Transaction-side parity waits for Transaction Search to report the owner's payments.

## S3 gated actions

Pending. This needs the owner's sandbox payments (see `docs/sandbox-pay-steps.md`, which is generated
and gitignored). Once those are in, Transaction Search can take up to about 3 hours to report them.

## S4b: Transaction Search parity (2026-11-08 00:5x CET)

Recorded one real `GET /v1/reporting/transactions?fields=all` page (3-day window) into
`fixtures/sandbox-recorded/transactions_page.json`, with the personal details scrubbed (emails,
names, address, phone and account ids). Tests in `tests/test_parity.py`.

- Every field the parser reads exists in the real `T0006` payment row. The row parses to the same
  types as a sample payment: Decimal amounts, a negative fee in the same currency, and the invoice
  reference, PayPal invoice id and item name all filled in.
- **Real finding: currency conversion rows.** The sandbox business account's primary currency is CZK.
  PayPal therefore books each USD payment as one `T0006` row plus two `T0200` conversion rows (USD out,
  CZK in), and the conversion rows repeat the `invoice_id`. The matcher counts only `T00xx` rows with a
  positive gross as payments, so the conversion rows are `ignored`. Invoice 0011 stays one clean match,
  with no fake duplicate or overpayment (tested).
- **Real fees:** 4.55 on 125.00 USD and 3.22 on 86.00 USD. Both are below the standard-rate ceiling
  (3.49% + 0.49) that the fee-short rule uses, so a payer who deducts the real fee is still caught (tested).
- **Lag:** `last_refreshed_datetime` was 21:59:59Z when queried at 23:4xZ. Invoice payments made later
  were already on the board through the Invoicing second source; the Send Money payments were not yet visible.
