# PayPal API notes (source of truth for the client and the fixtures)

Read on 2026-11-07. Sources:

- Official OpenAPI specs: <https://github.com/paypal/paypal-rest-api-specifications/tree/main/openapi>
  - `reporting_transactions_v1.json` (Transaction Search, spec version 1.9)
  - `invoicing_v2.json` (Invoicing v2, spec version 2.6)
  - `payments_payment_v2.json` (Payments v2, spec version 2.12)
- Reference pages:
  - Authentication: <https://developer.paypal.com/api/rest/authentication/>
  - Transaction Search: <https://developer.paypal.com/docs/api/transaction-search/v1/>
  - Transaction event codes: <https://developer.paypal.com/reports/reference/t-codes>
  - Invoicing v2: <https://developer.paypal.com/docs/api/invoicing/v2/>
  - Payments v2: <https://developer.paypal.com/docs/api/payments/v2/>
  - Idempotency: <https://developer.paypal.com/reference/guidelines/idempotency/>

Base URL. Sandbox only: `https://api-m.sandbox.paypal.com`. The client refuses any other host. Live is `https://api-m.paypal.com` and is never used here.

## Money

Every amount is a `money` object: `{"currency_code": "USD", "value": "125.00"}`. Both fields are required. `value` is a **string** matching `^((-?[0-9]+)|(-?([0-9]+)?[.][0-9]+))$`, and it can be negative: in Transaction Search, debits and fees are negative. We parse it with `Decimal(value)` and never use float.

## 1. OAuth2 token

`POST /v1/oauth2/token`

- Auth: HTTP Basic, with `client_id:client_secret` Base64-encoded.
- Header: `Content-Type: application/x-www-form-urlencoded`.
- Body: `grant_type=client_credentials`.

Response (sample from the docs):

```json
{
  "scope": "https://uri.paypal.com/services/invoicing ... https://uri.paypal.com/services/reporting/search/read ...",
  "access_token": "<redacted>",
  "token_type": "Bearer",
  "app_id": "APP-…",
  "expires_in": 31668,
  "nonce": "…"
}
```

- Send `Authorization: Bearer <access_token>` on every call.
- Cache the token until `expires_in` minus a margin.
- Never log the token. Our client redacts it from errors and reprs.
- Transaction Search needs the scope `https://uri.paypal.com/services/reporting/search/read`. If it is missing from `scope`, the app doesn't have Transaction Search enabled, and the client raises a clear error that says so.

## 2. Transaction Search

`GET /v1/reporting/transactions` (operationId `search.get`)

Query parameters:

| param | required | notes |
|---|---|---|
| `start_date` | yes | RFC 3339 with seconds, e.g. `2026-10-08T00:00:00-0000` / `…Z` |
| `end_date` | yes | **maximum range 31 days**, so we chunk longer windows |
| `fields` | no | we send `all` (default is `transaction_info` only) |
| `page_size` | no | 1 to 500, default 100; we use 100 |
| `page` | no | min 1, default 1 (`page=1&page_size=20` = first 20) |
| `transaction_status` | no | `D` denied, `P` pending, `S` success, `V` reversed/refunded |
| `transaction_type` | no | T-code filter, e.g. `T0006` |
| `transaction_id`, `transaction_amount`, `transaction_currency`, `balance_affecting_records_only` (`Y` default / `N`), `payment_instrument_type`, `store_id`, `terminal_id` | no | |

Response `search_response`:

```text
transaction_details: [transaction_detail]
account_number, start_date, end_date, last_refreshed_datetime
page, total_items, total_pages, links
```

`transaction_detail` = `{transaction_info, payer_info, shipping_info, cart_info, store_info, auction_info, incentive_info}`.

The `transaction_info` fields we use:

- `transaction_id` (max 24; NOT unique in reporting, because a balance-affecting row and a non-balance row can share an ID)
- `paypal_reference_id`, `paypal_reference_id_type` (`ODR`, `TXN`, `SUB`, `PAP`). A refund row points to the original via this ID with type `TXN`.
- `transaction_event_code`. T-codes:
  - `T0006` Express Checkout payment
  - `T0007` standard web checkout payment
  - `T0000` general payment
  - `T0011` mobile payment
  - `T1107` payment refund
  - `T1106` payment reversal
- `transaction_initiation_date`, `transaction_updated_date`
- `transaction_amount` (money, gross; negative for debits/refunds)
- `fee_amount` (money, negative for a charged fee)
- `transaction_status` (`D`, `P`, `S`, `V`)
- `transaction_subject` (max 256), `transaction_note` (max 4000). The payer's memo lives here.
- `invoice_id` (max 127). For Invoicing payments this carries the invoice ID or number.
- `custom_field`, `protection_eligibility`, `instrument_type`, `instrument_sub_type`

The `payer_info` fields:

- `account_id`
- `email_address`
- `payer_name` (`given_name`, `surname`, `alternate_full_name`)
- `country_code`
- `address_status`, `payer_status` (`Y`/`N`)

The `cart_info` fields:

- `item_details[]` (`item_name`, `item_amount`, `invoice_number`, …)
- `tax_inclusive`
- `paypal_invoice_id`. This is the link to an Invoicing v2 invoice when the payment came through one.

Errors use the shared shape `{name, message, debug_id, details:[{field, value, location, issue, description}], links}`.

## 3. Invoicing v2

All paths are under `/v2/invoicing`. The invoicing spec documents no `PayPal-Request-Id` header for send, remind or record payment. We still send one (harmless), **and** we dedupe on our side: the policy engine refuses a second intent for the same invoice and action, and a retry first reads the invoice: a payment whose note is already on it isn't recorded again, a created invoice's id is saved before it is sent (a retry sends that one), and an invoice no longer in DRAFT isn't sent again.

| call | method + path | request | success |
|---|---|---|---|
| list | `GET /invoices?page=&page_size=&total_required=true&fields=all` | – | 200 `invoices` |
| search | `POST /search-invoices?page=&page_size=&total_required=` | `search_data` (status[], due_date_range, recipient_email, …) | 200 `invoices` |
| get | `GET /invoices/{invoice_id}` | – | 200 `invoice` |
| create draft | `POST /invoices` | `invoice` | 201 `invoice` (or a link with Prefer minimal) |
| send | `POST /invoices/{invoice_id}/send` | `notification` | 200/202 `link_description` |
| remind | `POST /invoices/{invoice_id}/remind` | `notification` | **204 No Content** |
| record payment | `POST /invoices/{invoice_id}/payments` | `payment_detail` | 200 `{payment_id}` |
| next number | `POST /generate-next-invoice-number` | – | 200 `{invoice_number}` |

Paging limits:

- `page` runs 1 to 1000.
- `page_size` runs 1 to 100 (default 20).
- `total_required=true` adds `total_pages` and `total_items` to the response.

`invoices` = `{total_pages, total_items, items:[invoice], links}`.

`invoice`:

```text
id (RO, max 30), parent_id, status (RO)
detail*: { invoice_number (max 25), invoice_date (YYYY-MM-DD), payment_term:{term_type, due_date},
           currency_code*, reference (max 120), note, terms_and_conditions, memo,
           metadata (RO): {create_time, first_sent_time, last_sent_time, recipient_view_url, invoicer_view_url, …} }
invoicer: { name, address, email_address, phones, website, tax_id, additional_notes, logo_url }
primary_recipients: [ { billing_info: { name:{given_name, surname, full_name}, business_name?, email_address, language },
                        shipping_info } ]
items: [ { name*, quantity* (string), unit_amount* money, description, tax, discount } ]
configuration: { partial_payment:{allow_partial_payment, minimum_amount_due}, allow_tip, tax_inclusive, template_id }
amount: { currency_code, value, breakdown:{item_total, discount, tax_total, shipping, custom} }
due_amount (RO money), gratuity (RO money)
payments: { paid_amount (RO money), transactions:[payment_detail] }
refunds:  { refund_amount (RO money), transactions:[refund_detail] }
links
```

`invoice_status` values:

- `DRAFT`, `SENT`, `SCHEDULED`, `UNPAID`, `PAYMENT_PENDING`
- `PAID`, `MARKED_AS_PAID`, `PARTIALLY_PAID`, `PAID_EXTERNAL`
- `CANCELLED`, `AUTO_CANCELLED`
- `REFUNDED`, `PARTIALLY_REFUNDED`, `MARKED_AS_REFUNDED`, `REFUNDED_EXTERNAL`
- `SHARED`

The "open" invoices we chase are `SENT`, `UNPAID`, `PARTIALLY_PAID` and `PAYMENT_PENDING`.

`payment_detail`:

- `type` (RO: `PAYPAL` / `EXTERNAL`)
- `payment_id` (max 22)
- `payment_date` (YYYY-MM-DD)
- `method`*: one of `BANK_TRANSFER`, `CASH`, `CHECK`, `CREDIT_CARD`, `DEBIT_CARD`, `PAYPAL`, `WIRE_TRANSFER`, `OTHER`
- `note` (max 2000)
- `amount` (money)
- `shipping_info`

`notification` (body for send and remind):

- `subject`, `note`
- `send_to_invoicer`, `send_to_recipient` (booleans)
- `additional_recipients` (array of emails)

## 4. Payments v2: refund a capture

`POST /v2/payments/captures/{capture_id}/refund` (operationId `captures.refund`)

Headers:

- `Authorization: Bearer …`
- `Content-Type: application/json`
- `PayPal-Request-Id` (idempotency, **we always send it**)
- `Prefer: return=minimal` (default) or `return=representation`

Body `refund_request` (empty body = full refund):

```json
{ "amount": {"currency_code": "USD", "value": "125.00"}, "invoice_id": "…", "note_to_payer": "Duplicate payment refunded", "custom_id": "…" }
```

- Response: 200 or 201 `refund` = `{id, status, status_details:{reason}, amount, invoice_id, custom_id, note_to_payer, seller_payable_breakdown, payer, links}`.
- Refund `status` is one of `CANCELLED`, `FAILED`, `PENDING` or `COMPLETED`.
- Errors: 400, 409 and 422 return the `error` shape (`name`, `message`, `debug_id`, `details[{issue, description}]`). The spec names `REFUND_AMOUNT_EXCEEDED` as a 422 issue.

Capture status values:

- `COMPLETED`, `DECLINED`, `PARTIALLY_REFUNDED`, `PENDING`, `REFUNDED`, `FAILED`
- `GET /v2/payments/captures/{capture_id}` returns the capture.

## 5. Idempotency

From the guideline page:

- `PayPal-Request-Id` is a unique, client-generated ID that the server stores for a period of time.
- Re-sending a request with the same ID returns the latest status of the original request instead of repeating it.
- Omitting the header can create a duplicate.

Our executor fixes the ID when the action is queued (`cashdesk-<item_id>-<random>`, so a reinstall or a second desk never reuses one). A run that has started is only ever retried with that same ID: it never expires, and limits, the hourly cap and later syncs don't re-check it, so a retry after a lost reply or a crash returns PayPal's first result instead of refunding twice. If PayPal reports the refund as FAILED or CANCELLED, the item becomes `declined` and may be proposed again.

## 6. Sandbox gotchas (handled in code)

1. **Transaction Search must be enabled** on the sandbox REST app (app settings, Features, "Transaction search"). Without it, the token lacks the reporting scope and the call returns 403 `NOT_AUTHORIZED`.
2. **Reporting delay.** New transactions can take up to about 3 hours to appear in Transaction Search. Sandbox mode therefore also reads `invoice.payments.transactions[]` from Invoicing as a second source of payment evidence. The board shows `last_refreshed_datetime`.
3. **31-day window.** `end_date - start_date <= 31 days`; the desk always asks for exactly the last 31 days and refuses longer ranges (nothing is chunked).
4. **Rate limits.** HTTP 429 is retried with exponential backoff, at most 3 tries, honouring `Retry-After` in seconds or as an HTTP date; a wait over 30 s, or one that can't be read, fails at once as a PayPal error instead of holding the request open.
5. **Remind returns 204** with an empty body, so don't parse JSON there.
6. **Transaction IDs aren't unique** in reporting. The key is `(transaction_id, transaction_event_code, transaction_status)`.
7. **Amounts are strings**, and debits/fees are negative.
