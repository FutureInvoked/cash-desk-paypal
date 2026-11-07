# Video script: Cash Desk for PayPal (target 2:45, hard limit under 3:00)

Format: screen recording of the board at 1440×900, dark theme, with a calm voice-over in English.
Use names only (PayPal, Claude, AG Grid): no brand marks, no third-party footage. The owner approves the final cut.

Prep before recording:
- Start from a clean state: `rm -f var/cashdesk.db var/audit.jsonl var/audit.jsonl.head`, then `make demo`.
- Browser zoom 100%, Reconciliation tab open, mouse cursor visible.
- For the sandbox take (after S3): run with sandbox keys and show the real request id in the Audit tab.

## Timeline

| Time | Shot (what is on screen) | Notes |
|---|---|---|
| 0:00-0:12 | Title card: "Cash Desk for PayPal: an AI desk that matches, explains and proposes; you approve" | Static text over the dark board, blurred |
| 0:12-0:30 | Reconciliation tab, summary tiles at the top: 62.5% matched, unreconciled, still owed, 0 waiting | Slow zoom on the tiles |
| 0:30-0:52 | The grid's exception groups: Duplicate, Overpaid, Partial, Fee short, Currency mismatch. Hover the AI reason column | Cursor runs down the status pills |
| 0:52-1:10 | Click the Fee short row (NW-1020): detail panel shows the reason and the evidence fields | Hold on "equals PayPal's fee (17.94 USD)" |
| 1:10-1:25 | Search "Lumen": the suggested match (60%, needs review) | Point at "payer email differs" |
| 1:25-1:40 | Approvals tab → press **Propose actions** → six proposals appear: 2 reminders, 2 invoices, record payment, refund | |
| 1:40-2:05 | **Approve…** on "Refund duplicate": the dialog shows POST https://api-m.sandbox.paypal.com/v2/payments/captures/…/refund, the JSON body and PayPal-Request-Id: cashdesk-q00004; policy passes; press **Approve and run** → status Done | The money moment: slow down |
| 2:05-2:15 | **Reject…** on one reminder with the note "Customer promised to pay Friday" | |
| 2:15-2:35 | Audit log tab: green "Chain intact" banner, head hash, Copy; rows enqueued → approved → executing → executed → rejected | Cut to a terminal: `make audit-verify` → OK |
| 2:35-2:45 | Closing card: "make demo · runs without keys · MIT" + repo URL | Text only |

## Voice-over

0:00
> Every small business paid through PayPal ends the month with the same questions: which invoice was this for, who paid twice, who still owes us?

0:12
> Cash Desk pulls PayPal transactions and invoices and reconciles them. Here, about sixty percent matched on their own.

0:30
> Everything else is an exception, grouped on an AG Grid Community board: a duplicate, an overpayment, a partial, a fee shortfall, a wrong currency.

0:52
> Claude explains each one in a sentence and cites the PayPal fields behind it. This customer paid the invoice minus PayPal's fee.

1:10
> Where rules give up, Claude suggests a match from the memo, and marks it for review.

1:25
> Then the agent proposes what to do: remind, invoice, record, refund. It never runs anything itself.

1:40
> Approve shows exactly which sandbox call will run, with an idempotency key, after a policy check on caps, rate and duplicates. Only now does it run.

2:05
> Or reject it, with a note.

2:15
> Every step lands in a hash-chained audit log. One command proves nobody edited it.

2:35
> Cash Desk for PayPal. The AI does the work; you make the call. Try it with make demo, no keys needed.

## Shot list (capture checklist)

1. Title card and closing card, rendered as text.
2. Summary tiles (Reconciliation tab, top).
3. Grid with exception groups expanded and the matched group collapsed.
4. NW-1020 detail panel (evidence list).
5. Search "Lumen" → suggested match row.
6. Approvals: before and after **Propose actions**.
7. Approve dialog for the refund (full URL, body, PayPal-Request-Id visible).
8. Approvals after running: Done and Rejected rows.
9. Audit tab with the chain banner and head hash.
10. Terminal: `make audit-verify` printing OK.

Optional B-roll: `make test` scrolling to "passed"; the policy table in the README.
