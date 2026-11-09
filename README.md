# Cash Desk for PayPal

**An AI reconciliation and collections desk for small businesses that get paid through PayPal.**
It pulls PayPal transactions and invoices, matches every payment to an invoice, explains every
match and every exception in plain words, and proposes the next actions: payment reminders, draft
invoices for money nobody billed, recording a payment, refunding a duplicate or just the overpaid
part of a payment. Nothing moves money on its own. Every action passes a policy check and a human
approval (two different people for refunds above 500), runs with a PayPal idempotency key, and lands
in a hash-chained audit log you can verify. One click exports a balanced journal for your accounting system.

![Reconciliation grid](docs/screenshots/b2-reconciliation-grid.jpg)

## Try it in one command (no keys needed)

```bash
make demo          # installs into .venv, serves http://localhost:8000 with sample data
```

- Port 8000 busy? `make demo PORT=8010`.
- Needs Python 3.11+. It uses `uv` when installed, otherwise `venv` + `pip`.
- The demo runs on **Northwind Studio**, a fictional design agency with 26 invoices and 26 PayPal
  transactions that cover every case a small business hits (see [fixtures/README.md](fixtures/README.md)).

Other commands:

```bash
make test          # 310+ tests: matcher, AI layer (mocked), policy, queue, audit, API, UI contract
make audit-verify  # verifies the hash chain of var/audit.jsonl
make eval          # scores reconciliation against the expected outcomes in evals/match_eval.json
```

### A two-minute tour

1. **Reconciliation** groups every payment and invoice by status, with exceptions first:
   duplicate, overpaid, partial, fee short, currency mismatch, unmatched, overdue and suggested match.
   Each row has a one-line reason that cites the PayPal fields it relies on. Click a row to see the evidence.
2. **Approvals → Propose actions.** The agent proposes seven actions. **Approve…** shows exactly
   which sandbox call will run: method, full URL, JSON body and `PayPal-Request-Id`. The action
   runs only after you confirm. **Reject…** takes a note.
   - The 800.00 duplicate refund is above the two-person threshold: the first approval is only
     recorded ("Needs a 2nd approver"), and it runs after a different person approves.
   - The 45.00 overpayment on NW-1014 gets a partial refund of exactly the overpaid amount.
3. **Audit log** shows every proposal, approval, rejection and PayPal call, plus a banner that
   verifies the hash chain and the head hash to copy elsewhere.
4. **Export journal (CSV)**, top right, downloads one balanced journal entry per payment and refund
   (see [Accounting export](#accounting-export)).

![Approve dialog](docs/screenshots/b3-approve-dialog.jpg)

## The problem

A small business paid through PayPal ends every month with a pile of questions:
- Which invoice was this payment for?
- Why is this one $17.94 short?
- Did this customer pay twice?
- Who still owes us, and how late are they?
- Who sent this $275 with no invoice behind it?

PayPal has all the data, split across Transaction Search and Invoicing. Answering those questions is
manual spreadsheet work, and fixing the problems (reminders, refunds, missing invoices) means
clicking through several screens. Cash Desk does the matching and the explaining. It also queues
the fixes for a person to approve.

## How it works

```text
PayPal (sandbox)                Cash Desk                                         Board (AG Grid)
-----------------    ----------------------------------------------------------   ----------------
Transaction Search -> ingest -> SQLite -> rules matcher -> AI layer (LLM)    --->  Reconciliation
Invoicing v2       ->                     (evidence)       resolve + explain       grid + detail
                                                 |
                                                 v
                                         action proposer -> policy check -> approval queue
                                                                                   |  human approves
                                                                                   v
                     PayPal write  <- executor (PayPal-Request-Id) <- policy re-check
                                                 |
                                                 v
                                     hash-chained audit log (var/audit.jsonl)  ->  Audit tab
```

**Matching** (`cashdesk/matcher.py`) is deterministic and tries rules in this order:
1. **Invoice reference.** `cart_info.paypal_invoice_id`, `transaction_info.invoice_id`, or the invoice number in the payer's note.
2. **Exact amount plus payer email.** It must point to exactly one open invoice.
3. **Fee tolerance.** The payer deducted PayPal's fee.

Running totals per invoice then classify each payment as matched, partial, overpaid or duplicate.
A duplicate is the same payer paying the same amount within 24 hours of a payment that already
covered the invoice. Currency mismatches, refunds, pending payments and bank withdrawals are
classified separately. When the rules aren't sure, they don't guess: an ambiguous match stays
unmatched, with its candidate invoices, for the AI.

## What the AI does

The AI layer is in `cashdesk/ai.py`, with the prompt in `cashdesk/prompts/reconcile_system.md`.

- **Models: Claude or any OpenAI-compatible API.** You pick the provider in `.env`:
  - **Claude** (`claude-sonnet-5-5`) through the official Anthropic SDK: one request per
    reconciliation with structured JSON output and low effort. Server-side refusal fallback is on
    (`fallbacks: "default"`), so a declined request is retried on another model inside the same call.
  - **Any OpenAI-compatible chat-completions API:** OpenAI, Mistral, Groq, OpenRouter, or a local
    model through Ollama, llama-server or LM Studio. One request with a strict JSON schema
    (`response_format`). Set `AI_MODEL`, plus `AI_BASE_URL` and `AI_API_KEY` as your endpoint needs.
  - **An OpenAI model through the Codex CLI, signed in with a ChatGPT plan** (no API key):
    `AI_PROVIDER=codex`, optionally `AI_MODEL` (default `gpt-6-sol`) and `CODEX_BIN`. One
    `codex exec` per reconciliation, in a read-only sandbox inside an empty temporary folder, with
    every Codex tool switched off (shell, file viewing, browser, apps, plugins), so text a payer wrote
    can't make it read your files. `--output-schema` forces the same JSON. Run `codex login` once.
  - All of them go through the same prompt, schema and guard rails below.
  - Checked with a real hosted model on 2026-11-09: `gpt-6-sol` through Codex scored 31/31 on
    `make eval` (74 s), resolved the Lumen Bakery memo case at 0.99 confidence, and every payment
    reason it wrote on the live sandbox data passed the evidence check.
  - Checked against a real local model on 2026-11-09: a 27B model behind llama-server, through
    the OpenAI-compatible path, linked the Lumen Bakery payment to its invoice from the memo
    (confidence 0.98) and wrote cited reasons that passed the guard rails. It needed 343 seconds
    for 3 payments and 5 invoices on a laptop, so use a hosted model for real volumes, or raise
    `AI_TIMEOUT` for a local one.
- **Job 1, resolve.** For each payment the rules couldn't place, the model may pick one invoice
  from that payment's candidate list. Example: the owner of Lumen Bakery paid from a personal address,
  and only the memo links the payment to the invoice.
- **Job 2, explain.** The model writes a one-line reason for every result, citing the PayPal evidence fields.
- **Guard rails:**
  - The model only suggests.
  - A link is kept only if the invoice was one of the candidates, the currency matches and the
    confidence is at least 0.6. It then goes back through the deterministic matcher.
  - Suggested matches are marked "needs review".
  - A reason is kept only if every field it cites exists in the evidence of that exact row, and it
    is dropped if it carries data the model was never shown (a key block or a long unknown token).
  - Text written by payers (notes, subjects) is passed to the model as untrusted data.
  - The AI layer has no path to any PayPal write.
- **No key? It still works.** With no AI provider set, or on an API error, a refusal or
  invalid output, a deterministic fallback takes over. It is a memo heuristic plus templated reasons.
  The board shows which one ran (`AI: fallback …` or `AI: ai (<model>)`).
- `make eval` scores 31 expected outcomes. The fallback scores 31/31 on the sample data. Set an AI
  provider in `.env` to score that model. That makes one real API call.

## Safety design

| Layer | What it does | Code |
|---|---|---|
| Proposals only | The agent writes proposals that carry the exact PayPal calls; it can't execute | `cashdesk/actions.py` |
| Policy | Checks the allowlist (five actions only), positive amounts in USD or EUR, per-action caps (refunds ≤ 1,000; record payment ≤ amount still owed; an overpayment refund ≤ the amount overpaid, a limit stored apart from the proposal), call integrity (method, path, body amounts must equal the proposal; no host or path tricks), a rate cap (10 per hour), one live action per target, no reuse of a `PayPal-Request-Id`, and 24-hour expiry | `cashdesk/policy.py` |
| Approval queue | A human approves or rejects each action. The policy runs again at approval and again right before the PayPal call, against the proposal as stored. Rejected, expired, blocked or unapproved items never execute (tested). | `cashdesk/queue.py` |
| Two-person rule | Refunds above 500.00 need two different approvers. The first approval is only recorded; the second must come from someone else, and the executor refuses to run a large refund that lacks two distinct approvers even if the database is edited. The threshold is set in `PolicyConfig` (`None` turns it off). The board has no logins yet, so approver names are typed in; in production they would come from user accounts. | `cashdesk/policy.py`, `cashdesk/queue.py` |
| Idempotency | Every call carries `PayPal-Request-Id: cashdesk-<item>`. A retry never double-refunds, and an executed item returns its stored result. | `cashdesk/queue.py` |
| Audit | Append-only JSONL. Each record holds the sha256 of its canonical JSON and the previous hash, and a head pointer catches truncation. `make audit-verify` reports the first bad record. Secret-looking fields are redacted. | `cashdesk/audit.py` |
| Sandbox only | The client refuses any base URL other than `api-m.sandbox.paypal.com` | `cashdesk/config.py` |

## Accounting export

`GET /api/export/journal.csv` (the **Export journal (CSV)** button) turns the reconciliation into
journal entries a bookkeeper can import. Each PayPal payment or refund becomes one entry that
balances in its own currency, with columns `date, entry_id, account, debit, credit, currency,
invoice_number, paypal_transaction_id, event_code, memo`.

| PayPal row | Debit | Credit |
|---|---|---|
| Payment matched to an invoice (also partial or short by the fee) | PayPal balance (net), PayPal fees | Accounts receivable (gross); the memo says what stays open |
| Overpayment | PayPal balance, PayPal fees | Accounts receivable (invoice part), Customer credits (excess) |
| Duplicate payment | PayPal balance, PayPal fees | Customer credits |
| Payment with no invoice, or in the wrong currency | PayPal balance, PayPal fees | Unapplied payments |
| Refund | Customer credits (duplicate or overpayment) or Accounts receivable | PayPal balance, PayPal fees (fee returned) |

Pending payments, currency conversions and bank withdrawals are not booked. Account names are plain
so they can be mapped to any chart of accounts. Text that payers wrote is escaped so a spreadsheet
can't run it as a formula.

## Webhooks (optional)

Polling is the default. With webhooks, PayPal tells the desk when something changes, and the desk
re-syncs within seconds instead of on the next poll. Send Money payments still appear only through
Transaction Search, which runs about 2 hours behind, so webhooks don't remove that lag.

- **A separate server** (`make webhooks`, port 8001) serves only `POST /webhooks/paypal` and
  `GET /webhooks/health`. Point a tunnel or reverse proxy at this port, never at the board.
- **Every event is verified with PayPal** (`POST /v1/notifications/verify-webhook-signature`, with
  the raw body passed through unchanged). Unverified events are rejected and audited; after 20 in
  10 minutes the server answers 429 without asking PayPal. Bodies are capped at 256 KB while they
  stream in, and the PayPal calls run off the event loop. Without `PAYPAL_WEBHOOK_ID` in `.env`,
  nothing is accepted.
- **An event only triggers a re-sync.** Amounts and matches always come from PayPal's APIs, never
  from the event body, and an event can't propose, approve or run an action. Repeated event ids are
  ignored. Each event lands in the audit log (`webhook_received` / `webhook_rejected`).
- **Subscribed events:** `PAYMENT.CAPTURE.COMPLETED`, `.REFUNDED`, `.REVERSED`,
  `INVOICING.INVOICE.PAID`, `.UPDATED`, `.REFUNDED`.

Set it up against your sandbox app:

```bash
make webhooks                                   # terminal 1: the webhook-only server
cloudflared tunnel --url http://127.0.0.1:8001  # terminal 2: a public HTTPS address for it
.venv/bin/python -m tools.webhook_setup create https://<your-tunnel-host>/webhooks/paypal
# put the printed PAYPAL_WEBHOOK_ID=... in .env and restart `make webhooks`
.venv/bin/python -m tools.webhook_setup delete <webhook_id>   # when you're done
```

If you already have a cloudflared `config.yml` with ingress rules (for example in
`/usr/local/etc/cloudflared/`), give the quick tunnel its own empty config with `--config`;
otherwise those rules answer 404 for the tunnel's address.

## PayPal APIs used

All endpoints and response shapes are taken from PayPal's official OpenAPI specs and reference pages.
See [docs/paypal-api-notes.md](docs/paypal-api-notes.md). The sample data matches those schemas
field for field, and a test walks every key.

| API | Endpoint | Used for |
|---|---|---|
| OAuth2 | `POST /v1/oauth2/token` | client-credentials token |
| Transaction Search | `GET /v1/reporting/transactions` | payments, fees, refunds (31-day windows, `fields=all`) |
| Invoicing v2 | `GET /v2/invoicing/invoices` | invoices, amounts due, recorded payments |
| Invoicing v2 | `POST /v2/invoicing/invoices` | draft an invoice for an unbilled payment |
| Invoicing v2 | `POST /v2/invoicing/invoices/{invoice_id}/send` | send that invoice |
| Invoicing v2 | `POST /v2/invoicing/invoices/{invoice_id}/remind` | payment reminder for an overdue invoice |
| Invoicing v2 | `POST /v2/invoicing/invoices/{invoice_id}/payments` | record a payment the AI matched |
| Payments v2 | `POST /v2/payments/captures/{capture_id}/refund` | refund a duplicate payment, or only the overpaid part (partial refund) |
| Webhooks v1 | `POST /v1/notifications/verify-webhook-signature` | check that a webhook delivery really came from PayPal |
| Webhooks v1 | `POST`/`GET`/`DELETE /v1/notifications/webhooks` | register, list and remove the webhook (`tools/webhook_setup.py`) |

## Run it against the PayPal sandbox

1. Sign in at <https://developer.paypal.com>, open **Apps & Credentials** (Sandbox), and use the
   classic REST app: the **Default Application**, or any app whose client ID starts with `A`.
   Newer app types (client ID starting with `B`) were not granted Invoicing or Transaction Search in our tests.
2. In the app's settings, enable **Invoicing** and **Transaction search**. Without them the token lacks those scopes, and Cash Desk says so.
3. Copy `.env.example` to `.env` and fill in `PAYPAL_CLIENT_ID=` and `PAYPAL_CLIENT_SECRET=`.
   `.env` is gitignored, so never commit it.
4. Run `make demo`. With keys present the mode defaults to `PAYPAL_MODE=sandbox`. Force sample data
   with `PAYPAL_MODE=fixtures make demo`.
5. Optional, for live AI matching and reasons, add one of these:
   - `ANTHROPIC_API_KEY=` for Claude;
   - `AI_PROVIDER=codex` to use your ChatGPT plan through the Codex CLI (after `codex login`);
   - `AI_MODEL=`, `AI_API_KEY=` and, if it isn't OpenAI, `AI_BASE_URL=` for any OpenAI-compatible
     API. For a local Ollama model: `AI_PROVIDER=openai`, `AI_BASE_URL=http://localhost:11434/v1`,
     `AI_MODEL=<model>`, no key.

   See `.env.example`.

Sandbox notes:
- New transactions can take up to about 3 hours to appear in Transaction Search, so payments
  recorded on invoices are read from Invoicing as a second source.
- Amounts are strings handled as `Decimal`, never floats.

## What's new (hackathon period)

Everything. This repository was started on **2026-11-07** for the PayPal AI Hackathon. The ideas
come from the author's earlier Cash Desk work: matching first, AI only suggests, a human approves.
The code was written fresh here, along with the PayPal integration, the policy-gated agent, the
audit chain and the AG Grid board.

## Project layout

```text
cashdesk/            app code: models, paypal/ (interface, parser, fixture client), store, matcher,
                     ai, actions, policy, queue, audit, service, app (FastAPI)
static/index.html    the board: AG Grid Community 36.2.0 from jsdelivr, no build step
fixtures/            Northwind Studio sample data in PayPal's response shapes, plus cases.json
evals/               expected outcomes for make eval
tools/               fixture generator, spec key extractor, eval runner
docs/                PayPal API notes, screenshots
tests/               pytest suite
```

More screenshots: [approvals](docs/screenshots/b3-approvals-grid.jpg) ·
[audit + summary](docs/screenshots/b4-audit-and-summary.jpg) ·
[light mode](docs/screenshots/b5-light-1280-server-down.jpg)

![Audit log and summary](docs/screenshots/b4-audit-and-summary.jpg)

## Licence

MIT, see [LICENSE](LICENSE). PayPal, AG Grid and Claude are named only as the integrations they
are; no brand marks are used.
