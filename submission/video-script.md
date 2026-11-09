# Video script: Cash Desk for PayPal (as rendered: 2:33, limit under 3:00)

Format: screen recordings of the board at 1440×900 in the dark theme, scaled to 1080p, with a
synthetic English voice-over (Microsoft neural voice en-US-AvaNeural via edge-tts, loudness-normalised).
Products are named in words only (PayPal, Claude, AG Grid); no brand marks and no third-party footage.
The owner approves the final cut.

How it was made (the build folder's `video/`, outside this repo):
- Both boards ran with `AI_PROVIDER=codex` (gpt-6-sol on a ChatGPT plan). The sample-data scenes come
  from `make demo` on a clean state. The sandbox scene comes from the
  live desk that ran the real refund and reminder.
- Playwright records each scene, edge-tts reads `lines.txt`, and ffmpeg cuts and joins
  (`record.py`, `make_audio.sh`, `make_cards.py`, `render.py`).

## Timeline

| Time | Shot (what is on screen) | Notes |
|---|---|---|
| 0:00-0:11 | Title card: "Cash Desk for PayPal: an AI desk that matches, explains and proposes. You approve." | Text only |
| 0:11-0:22 | Reconciliation tab, sample data: summary tiles; header badge `AI: ai (codex: gpt-6-sol)` | The real model ran (Claude or any OpenAI-compatible API would show here too) |
| 0:22-0:35 | Exception groups on the AG Grid Community board: Duplicate, Overpaid, Partial, Fee short, Currency mismatch, Unmatched, Overdue | Cursor runs down the group rows, then scrolls |
| 0:35-0:46 | Search "NW-1020", open the Fee short row: the AI-written reason plus the evidence list (PayPal fields) | Every AI reason must cite fields that exist |
| 0:46-1:00 | Search "Lumen": the AI's suggested match to NW-1021 at 99%, with the memo as evidence | gpt-6-sol through the Codex CLI on a ChatGPT plan |
| 1:00-1:10 | Approvals tab → **Propose actions** → seven proposals: 2 reminders, 2 invoices, record payment, refund, overpayment refund |  |
| 1:10-1:29 | **Approve…** on "Refund duplicate" (800.00): POST https://api-m.sandbox.paypal.com/v2/payments/captures/…/refund, JSON body, PayPal-Request-Id, policy passes, two-person rule; **Approve (1 of 2)** as owner → "Needs a 2nd approver" → second approval as bookkeeper → Done | The money moment |
| 1:29-1:35 | **Reject…** on a reminder with the note "Customer promised to pay Friday" |  |
| 1:35-1:42 | Audit log tab: green "Chain intact" banner, head hash, enqueued → first approval → approved → executing → executed → rejected |  |
| 1:42-1:54 | **Export journal (CSV)** → the downloaded journal opened as a table: PayPal balance, fees, receivables, customer credits | First 18 lines of the real export |
| 1:54-2:22 | The REAL sandbox desk (mode: sandbox, same AI): search 0008 → the owner's 150.00 Send Money matched to invoice 0008; Approvals with the real refunds (80.00 duplicate, 4.50 tip) Done and the four rejected reminders; Audit log with the verified webhook and cashdesk-q00007 / q00008 / q00009 | Live run of 7-9 Nov 2026 (docs/sandbox-evidence.md) |
| 2:22-2:33 | Closing card: "The AI does the work; you make the call." · make demo · runs without keys · Claude, OpenAI, a ChatGPT plan or a local model · MIT | Text only |

## Voice-over

0:00
> Every small business paid through PayPal ends the month with the same questions. Which invoice was this for? Who paid twice? Who still owes us?

0:11
> Cash Desk pulls PayPal transactions and invoices, and reconciles them. Here, about sixty percent of payments matched on their own.

0:22
> Everything else is an exception, grouped on an AG Grid Community board: a duplicate, an overpayment, a partial payment, a fee shortfall, a wrong currency.

0:35
> Each one gets a one-line reason, written by the AI, that cites the PayPal fields behind it. This customer paid the invoice, minus PayPal's fee.

0:46
> Where the rules give up, the AI steps in. Here an OpenAI model, signed in with a ChatGPT plan, matches a payment from its note alone, at ninety-nine percent. Every link it makes is checked.

1:00
> Then the agent proposes what to do: remind, invoice, record, refund. It never runs anything on its own.

1:10
> Approve shows the exact sandbox call, with an idempotency key, after a policy check on caps, rate and duplicates. This refund is over five hundred, so one approval is not enough. A second person approves, and only then does it run.

1:29
> Or reject it, with a note.

1:35
> Every step lands in a hash-chained audit log. One command proves nobody edited it.

1:42
> At month end, one click exports a balanced journal: the net payment, PayPal's fee, receivables and customer credits, ready for the books.

1:54
> And this is the real PayPal sandbox: ten real invoices, paid by a test buyer. The desk refunded a real duplicate and a real overpaid tip, and matched a payment from its note alone. Verified PayPal webhooks keep it in sync. It also proposed reminders to four customers who had just paid, before PayPal's report showed it. A person rejected them. That is why every action waits for you.

2:22
> Cash Desk for PayPal. The AI does the work; you make the call. Try it with make demo: no keys needed, and bring any model you like.

## Shot list (capture checklist)

1. Title card and closing card, rendered as text.
2. Summary tiles (Reconciliation tab, top).
3. Exception groups.
4. NW-1020 detail panel with the evidence list.
5. Search "Lumen" → suggested match row.
6. Approvals: before and after **Propose actions**.
7. Approve dialog for the refund (full URL, body and PayPal-Request-Id visible), the two-person rule, then Done.
8. Reject with a note.
9. Audit tab with the chain banner and head hash.
10. Export journal (CSV) and the journal it produces.
11. The real sandbox desk: the 0008 memo match, Approvals and Audit log with the recorded request ids.
