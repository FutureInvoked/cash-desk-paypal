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
  is optional throughout. (A full `GET /v2/invoicing/invoices/{id}` does include `first_sent_time` and
  `last_sent_time`; checked 2026-11-08.)
- Transaction-side parity waits for Transaction Search to report the owner's payments.

## S3 gated actions

### Reminder (2026-11-08 01:1x CET), live sandbox through the API gate

Run: `POST /api/sync` -> `POST /api/proposals/refresh` -> `POST /api/approvals/{id}/approve` ->
`POST /api/approvals/{id}/execute`, with the same FastAPI app the board uses and keys from `.env`.

Invoices 0005 and 0006 were due on receipt on 2026-11-07, so on 8 Nov the desk saw both as overdue.
It proposed six reminders. Four of them (0008, 0013, 0015, 0017) were for invoices the owner had
already paid by Send Money that night. Transaction Search had not reported those payments yet
(it was refreshed only up to 22:29:59Z), so the desk could not know.
**That is the case the human gate exists for.**

| queue item | invoice | action | decision | PayPal result |
|---|---|---|---|---|
| q00001 | 0005 (240.00 USD) | `POST /v2/invoicing/invoices/INV2-FQBD-KUDF-YRSW-8GWA/remind` | approved, executed | **HTTP 204**, `PayPal-Request-Id: cashdesk-q00001` |
| q00002 | 0006 (35.00 USD) | reminder | rejected: "Small amount, one day late: wait a week before reminding." | not sent |
| q00003-q00006 | 0008, 0013, 0015, 0017 | reminder | rejected: "Customer already paid by Send Money on 2026-11-08; Transaction Search has not reported it yet. Do not chase." | not sent |

- A second `execute` on q00001 returned the stored result with the same request id. Nothing was sent twice.
- Audit chain: 14 records, verify OK. Head `5226c1b27cee41b222a94a0b79b3f0cbb45246c3cb251eb30d47d40ac370cc0b`.
- PayPal does not change the invoice's `last_sent_time` on a reminder (still 2026-11-07T08:49:11Z
  afterwards). The 204 response and the request id are the record.
- Bug found and fixed in this run: the reminder subject said "is 1 days overdue". It now says
  "1 day" (test added).

### Live reconciliation of the owner's payments (2026-11-08 02:58 CET)

The Send Money payments reached Transaction Search at the 23:59:59Z refresh, about 2 hours after
they were made. Synced through `POST /api/sync`: 24 rows (8 payments; 16 rows ignored, mostly CZK
conversions). Every case came out as designed on real data, with the fallback reconciler (no Anthropic key):

| invoice | what the owner did | desk outcome | rule |
|---|---|---|---|
| 0011, 0012 | paid the invoice link | matched / paid | invoice_ref |
| 0013 (120.00) | Send Money 60.00, note "invoice 0013 part 1" | partial, 60.00 still due | invoice_ref (from the note) |
| 0014 (80.00) | paid the invoice, then Send Money 80.00 again a minute later | matched + **duplicate** (refund candidate) | invoice_ref |
| 0015 (50.00) | Send Money 47.76 | fee short by 2.24 (= PayPal's fee) | fee_tolerance |
| 0016 (90.00 EUR) | paid the invoice link | matched / paid | invoice_ref |
| 0017 (45.00) | Send Money 49.50, note "invoice 0017 plus a tip" | overpaid by 4.50 | invoice_ref |
| 0008 (150.00) | Send Money 150.00, memo only | not yet reported by Transaction Search at this refresh | - |
| 0005, 0006 | nothing | overdue | - |

### Refund of the 0014 duplicate (2026-11-08 02:58 CET)

After the sync, `POST /api/proposals/refresh` enqueued one new item, q00007. The reminders already
decided were skipped. q00007 was a refund with these details:
`POST /v2/payments/captures/6FK36409J7161034V/refund`, body amount 80.00 USD, note to the payer and
`invoice_id: 0014`. The policy check passed.

| queue item | action | decision | PayPal result |
|---|---|---|---|
| q00007 | refund 80.00 USD of transaction 6FK36409J7161034V (the duplicate) | approved, executed | **refund 86940605T47984402, status COMPLETED, 80.00 USD**, `PayPal-Request-Id: cashdesk-q00007` |

- `GET /v2/payments/refunds/86940605T47984402` confirms COMPLETED, 80.00 USD.
- A second `execute` returned the stored result with the same request id. Nothing was refunded twice.
- Audit chain after the run: 18 records, verify OK. Head `3baa82c0a8c718eab29445f5cce3c8729fbbc2d247a7840de3c22027ec51be59`.
- Findings:
  - A Send Money payment (`T0000`) can be refunded through the Payments v2 capture refund endpoint,
    using its transaction id.
  - PayPal accepted `invoice_id` in the refund body but does not echo it in the refund resource.
  - The invoice-level reason also said "1 days overdue". Fixed (test added).

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

## S5: a memo-only match and a real overpayment refund proposal (2026-11-09)

The owner sent 150.00 USD by Send Money from the buyer account to the business account, with the
note "Lumen Bakery - Website retainer" and no invoice number. (A first attempt that day went to the
fictional customer's address instead of the business account and never reached the desk.)

- Transaction Search reported it as `6AW69090TU505332W` (`T0000`) about 2.5 hours later.
- **Found on real data:** invoice 0008 on the sandbox has only a contact ("Maria Lumen") and no
  business name, so the keyless fallback first left the payment unmatched. The fallback now also
  accepts the contact's surname as a whole word in the note, still with an exact amount and exactly
  one candidate (tests in `tests/test_ai.py`). After a re-sync, the desk suggested **0008** at 60%
  confidence, marked for review, citing `transaction_info.transaction_note` and
  `invoice.primary_recipients[0].billing_info.name`. A live AI model is the intended path for this case.
- **Proposals, waiting for a person (not executed):** `record_payment` of 150.00 USD on 0008, and
  `refund_overpayment` of 4.50 USD on 0017 (the tip), a partial refund capped at the overpaid excess.
  Both pass the policy.
- Audit chain after the proposals: 20 records, verify OK. Head `4745ce42e88feba02d90209a84a72be00e2fb4959a07f4a625fbaa3ece27f9d4`.

## S6: a real PayPal webhook, verified (2026-11-09, 18:59 UTC)

Set-up, all temporary: the webhook-only server (`cashdesk.webhook_app`, port 8001) behind a
Cloudflare quick tunnel, and a webhook registered on the sandbox app with `tools/webhook_setup.py`
for the six subscribed event types.

| Time (UTC) | What happened |
|---|---|
| 18:58:58 | `record_payment` 150.00 USD on invoice 0008 (the memo match from S5) approved through the gate |
| 18:59:01 | executed, `PayPal-Request-Id: cashdesk-q00008`, PayPal payment `EXTR-63L72930K2468451K`; invoice status `MARKED_AS_PAID` |
| 18:59:17 | PayPal created event `WH-7PT40345H3816905Y-67K33572BX261623N`, `INVOICING.INVOICE.PAID`, resource `INV2-UWZS-TSN9-PB3N-PR9J` |
| 18:59:44 | delivered to `POST /webhooks/paypal`; PayPal's `verify-webhook-signature` answered `SUCCESS`; the desk re-synced (17 invoices, 30 transactions); audit `webhook_received` |

- PayPal delivered the event **twice** (from 173.0.80.116 and 173.0.80.117). The second copy was
  answered `duplicate` and caused no second sync; the inbox holds one row for the event id.
- Transmission id `adaa3718-dc6f-11f1-ae6a-d1526f469e1d`. Audit chain after the run: 24 records,
  verify OK, head `1d4d33f44150d12fc93b420c57eb34ed104ad090b4342d83f28ace11636b77d1`.
- **Found on the way:** this Mac had a cloudflared `config.yml` with ingress rules for another
  hostname, which made the quick tunnel answer 404. Giving the quick tunnel its own empty config with
  `--config` fixed it (now in the README).
- Clean-up: webhook `4MC82235WV014724F` deleted from the sandbox app, tunnel closed, server stopped.

## S7: two gaps found on real data, fixed; the owner's own partial refund (2026-11-09, ~19:15 UTC)

After the S6 webhook run, the live board showed **230.00 USD unreconciled** that should have been 0:

1. **A recorded payment lost its match.** Recording the 150.00 on invoice 0008 made PayPal mark it
   `MARKED_AS_PAID`, so it was no longer open, and the 150.00 Send Money payment fell back to
   `unmatched`. PayPal keeps the recorded payment on the invoice as an `EXTERNAL` payment whose note
   says "PayPal transaction 6AW69090TU505332W". The matcher now links a payment to the one invoice
   whose recorded payment names it as a whole token (`invoice.payments.transactions[].note`).
2. **A refunded duplicate stayed an exception.** The 80.00 duplicate on 0014 was refunded on 8 Nov,
   and its refund row points back at it through `transaction_info.paypal_reference_id`. Refunds are
   now linked to their originals: a duplicate refunded in full becomes `refunded`, and an overpayment
   whose excess was refunded becomes a clean `matched`. The journal books both through customer credits.

After a re-sync on the fixed code: the 150.00 is matched to 0008 (paid 150.00), the duplicate reads
"already refunded", and **Unreconciled is empty**. Tests: `tests/test_matcher_followups.py`; eval 31/31.

**The owner's own approval.** At 19:13 UTC the owner approved the pending `refund_overpayment` on 0017
in the board (q00009). PayPal refund **`8FU36371TX504383F`**, 4.50 USD, `PayPal-Request-Id:
cashdesk-q00009`: a real partial refund of the tip, through the gate. Until Transaction Search
reports the refund row (about 2 hours), 0017 still shows as overpaid; fix 2 then turns it into a clean match.

Audit chain: 27 records, verify OK. Head `28f02c6b343265e71c696420a8affeba8b218cf1e9a1ab60b3f42a3405a8d04b`.

## S8: a hosted AI model on real data, through a ChatGPT sign-in (2026-11-09, ~19:45 UTC)

`AI_PROVIDER=codex`: an OpenAI model (`gpt-6-sol`) through the local Codex CLI 0.157.0, signed in with
the owner's ChatGPT plan (no API key). One `codex exec` per reconciliation, read-only sandbox, empty
temporary folder, MCP servers off, `--output-schema` = the same JSON schema as the other providers.

| Data | Result | Time |
|---|---|---|
| `make eval` (31 expected outcomes, sample data) | **31/31**, status `ai (codex: gpt-6-sol)` | 74 s |
| Sample data, the memo-only case (NW-1021) | linked by the AI at **0.99**: "the note names Lumen Bakery's October website retainer, and 1500.00 USD equals the balance due" | 71 s |
| Sample data, reasons | 26/26 payment and 25/26 invoice reasons passed the cited-field check | |
| Live sandbox data (17 invoices, 30 rows) | 30/30 payment and 17/17 invoice reasons passed; the 0008 match and the refunded 0014 duplicate explained from the real fields | 58 s |

The configured default model (`gpt-6.1-sol`) is refused for Codex with a ChatGPT account
("not supported when using Codex with a ChatGPT account"), so `AI_MODEL` defaults to `gpt-6-sol`.

Then on the owner's own configuration (`AI_PROVIDER=codex` in `.env`, 2026-11-09 ~20:05 UTC): the live
board's badge reads `AI: ai (codex: gpt-6-sol)`, all 32 rows on the Reconciliation grid carry an AI
reason with the AI tag, and the first load after a sync takes about 60 s while the model runs.
