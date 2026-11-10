# Cash Desk for PayPal: Devpost submission text

**Tagline:** An AI reconciliation and collections desk for small businesses paid through PayPal: it matches, explains and proposes, and a human approves every action.

**Try it now:** https://cash-desk-paypal.onrender.com (sample data). Sign in as Alex with one click, approve the 800.00 refund, then switch to Sam to approve it again. A first visit after idle can take a minute.

## Inspiration

Every small business that takes PayPal ends the month with the same questions:
- Which invoice was this payment for?
- Why is this one $17.94 short?
- Did this customer pay twice?
- Who still owes us, and how late are they?
- Who sent us $275 with no invoice behind it?

PayPal holds the answers, split between Transaction Search and Invoicing, so finding them is a
monthly spreadsheet job. Fixing them means clicking through screens: chasing a late payer,
refunding a double payment, invoicing money that arrived without an invoice.

Agents can do this work, but nobody wants an AI with direct access to the business's money. So we
built the opposite. It does all the reading, matching and explaining. It
prepares every fix down to the exact API call. Then it stops and asks.

## What it does

Cash Desk pulls PayPal transactions (Transaction Search) and invoices (Invoicing v2) and reconciles them.

- **Matches** every payment to an invoice. Deterministic rules run first: invoice reference,
  exact amount plus payer email, then amount within PayPal's fee. Running totals per invoice
  sort payments into matched, partial, overpaid and duplicate. Separate classes cover currency
  mismatches, refunds and pending payments.
- **Explains** every match and exception in one sentence that cites the PayPal fields it relies on,
  for example "Short by 17.94 on NW-1020, which equals PayPal's fee…".
- **Resolves** what rules can't. Example: a payment from a personal email whose memo "Lumen
  Bakery - October retainer" is the only link to its invoice. The AI picks only among the candidate
  invoices it is shown, and the match is flagged for review.
- **Proposes actions:**
  - send a payment reminder for an overdue invoice;
  - create and send an invoice for an unbilled payment;
  - record a payment the AI matched;
  - refund a duplicate payment;
  - refund only the overpaid part of a payment (a partial refund capped at the excess).
- **Gates every action.** A policy engine checks:
  - the action allowlist;
  - amount caps (refunds up to 1,000; recorded payments never more than the amount still owed;
    overpayment refunds never more than the amount overpaid);
  - call integrity;
  - a rate cap of 10 per hour;
  - duplicate intent;
  - expiry.

  A human then sees the exact sandbox request (method, URL, JSON body, `PayPal-Request-Id`) and
  approves or rejects it. Refunds above 500 need **two different signed-in people**: approvers are
  real accounts, the first approval is only recorded, and the executor refuses a large refund
  without two distinct accounts. The
  policy runs again just before the call. Rejected, expired or blocked actions never run.
- **Listens to PayPal (optional webhooks).** A separate, webhook-only server takes PayPal events,
  checks each one with PayPal's `verify-webhook-signature` API, ignores repeats, and re-syncs. An
  event never proposes or runs an action, and amounts always come from PayPal's APIs, not the event body.
- **Exports the books.** One click downloads a CSV journal with one balanced entry per payment
  and refund: net to the PayPal balance, fee to PayPal fees, gross to receivables, customer
  credits or unapplied payments.
- **Proves it.** Every proposal, approval, rejection and PayPal call goes into an append-only,
  hash-chained audit log with a one-command verifier, shown on the board.

The board is one page built on **AG Grid Community** (no licence key): exception-first status
groups from full-width rows and the external filter, status pills, search, wrapped AI reasons with
an evidence panel, and approvals and audit grids.

**Who it's for:** small businesses and their bookkeepers who invoice through PayPal, from
freelancers to small agencies.

**Where it runs today:** out of the box it runs on **Northwind Studio**, a fictional design agency
with 26 invoices and 26 PayPal transactions. The sample data copies PayPal's response schemas field
for field, so judges can try everything with no keys: on the hosted demo, where every visitor gets
a private copy, or locally with `make demo`.

With sandbox keys in `.env`, the same code runs against the real PayPal sandbox, and we did exactly
that on 7 and 8 November 2026:
- **The setup:** 10 real sandbox invoices. A buyer account paid them by invoice link or Send Money: a part payment, a duplicate, a payment short by the fee, and a
  payment with a tip.
- **Reconciliation:** the desk sorted every one correctly. Matched, partial, duplicate, fee short
  (exactly PayPal's 2.24) and overpaid, including a EUR invoice.
- **The refunds:** the duplicate was refunded through the approval gate (PayPal refund
  `86940605T47984402`, 80.00 USD, `PayPal-Request-Id: cashdesk-q00007`; a retry did not refund
  twice). Later the owner approved a partial refund of a 4.50 tip (`8FU36371TX504383F`).
- **The reminder:** an overdue invoice got a real reminder (`cashdesk-q00001`, HTTP 204).
- **The memo match:** a 150.00 Send Money with no invoice number was suggested for invoice 0008 from
  its note alone ("Lumen Bakery - Website retainer"), marked for review, with a record-payment
  proposal waiting for approval.
- **The webhook:** approving that record-payment made PayPal send a real `INVOICING.INVOICE.PAID`
  event 43 seconds later. PayPal verified the signature, the desk re-synced, and a second delivery
  of the same event was ignored as a duplicate.
- **What the gate caught:** the agent also proposed reminders for four invoices the buyer had just
  paid by Send Money. Transaction Search hadn't reported those payments yet; it runs about 2 hours
  behind. The human rejected them with a note. That moment is why every action waits for a person.

Ids and audit hashes: `docs/sandbox-evidence.md`.

## What's new (built during the hackathon)

All of it: the repository was started on 7 November 2026 for this hackathon. After submitting we
added real approver accounts and the hosted demo.

## How we built it

- **Backend:** Python 3.11, FastAPI, `httpx`, SQLite. One `PayPalClient` interface with a
  fixture client and a sandbox client. Money is always `Decimal` with a currency, and floats are rejected.
- **PayPal integration:** endpoints and shapes come straight from PayPal's official OpenAPI specs.
  A test walks every key in the sample data against the official schemas. The app refuses any base
  URL other than `api-m.sandbox.paypal.com`.
- **AI, your choice of model:** Claude (`claude-sonnet-5-5`) through the Anthropic SDK, with a
  server-side refusal fallback, any OpenAI-compatible API, or an OpenAI model through the Codex CLI
  signed in with a ChatGPT plan (no API key). Checked live: 31/31 on the eval, Lumen at 0.99. Every provider gets one structured-output
  request with the same prompt and schema. The model's links are validated and
  re-run through the deterministic matcher. Payer-written notes are treated as untrusted data.
  Without an API key, a deterministic fallback keeps the whole demo working. An eval file with
  31 expected outcomes (`make eval`) keeps both paths honest.
- **Safety:** a policy engine, an approval queue and an executor with idempotency keys
  (`cashdesk-<item>-<random>`, reused on every retry), plus a sha256 hash-chained audit log.
  Approvers sign in (salted scrypt, HttpOnly session cookies, a sign-in throttle).
- **Hosting:** Render's free plan, in a public-demo mode that refuses to start with any credential.
- **Frontend:** a single HTML page with AG Grid Community 36.2.0 from a CDN. There is no build step,
  and light and dark themes are supported.
- **Process:** built test-first. About 600 tests cover every reconciliation case, every policy rule
  (including attempts to bypass it), the queue lifecycle and audit tampering.

## Challenges we ran into

- **Transaction IDs aren't unique in PayPal reporting,** and new transactions can take hours to
  appear in Transaction Search. We key rows on ID, event code and status, and replace a whole
  date window on each sync so status changes never leave stale rows.
- **Making "the AI only suggests" real, not a slogan.** The AI cannot move money because no code
  path lets it. Its output is validated, re-matched and shown as "needs review". Every write goes
  through a policy check that also re-reads the stored proposal, so editing it after approval is caught.
- **Grouping without Enterprise.** Row grouping is Enterprise-only, so status groups use
  full-width rows plus the external filter.

## Accomplishments that we're proud of

- An approval dialog that shows exactly which PayPal request will run before anything runs.
- A tamper-evident audit trail. Edit, delete, reorder or truncate a line, and `make audit-verify`
  names the first bad record.
- A zero-key demo that is honest about which parts are AI and which are rules.
- Seven independent adversarial reviews. Every finding became a failing test first, then a fix.

## What we learned

The most useful thing an agent can do for a small business is often to do 95% of the work and
leave the last click to a person, with enough evidence that the click takes two seconds.

## What's next

- Hosting the webhook server permanently instead of a temporary tunnel.
- Write-off suggestions for fee shortfalls.
- Direct export to accounting systems (today it's a CSV journal).

## Built with

PayPal REST APIs (OAuth2, Transaction Search, Invoicing v2, Payments v2, Webhooks v1) · Claude (Anthropic SDK) or any OpenAI-compatible LLM API ·
AG Grid Community · Python · FastAPI · SQLite · httpx · pytest

**Licence:** MIT. **Repository:** https://github.com/FutureInvoked/cash-desk-paypal · **Video:** https://www.youtube.com/watch?v=5Sc75bXU300
