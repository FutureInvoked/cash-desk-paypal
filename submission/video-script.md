# Video script: Cash Desk for PayPal (v4, as rendered: 2:17, limit under 3:00)

Format: screen recordings of the board at 1440×900 in the dark theme, scaled to 1080p, with a
synthetic English voice-over (Microsoft neural voice en-US-AvaNeural via edge-tts, loudness-normalised).
Products are named in words only (PayPal, Claude, AG Grid); no brand marks and no third-party footage.
The owner approves the final cut.

How it was made (the build folder's `video/`, outside this repo):
- The opening scene is the hosted demo itself (https://cash-desk-paypal.onrender.com). The other
  sample-data scenes come from the same code in public-demo mode on a local machine, in one workspace,
  with the same banner. The sandbox scene is the live desk that ran the real refunds and reminder,
  signed in as its real Owner account, with the AI provider `codex` (gpt-6-sol on a ChatGPT plan).
- Playwright records each scene, edge-tts reads `lines.txt`, and ffmpeg cuts and joins
  (`record_v4.py`, `make_audio.sh`, `make_cards.py`, `render.py`).

## Timeline

| Time | Shot (what is on screen) | Notes |
|---|---|---|
| 0:00-0:11 | Title card: "Cash Desk for PayPal: an AI desk that matches, explains and proposes. You approve." | Text only |
| 0:11-0:23 | The hosted demo: the banner "Public demo with sample data. No PayPal calls…", the sign-in screen, **Sign in as Alex (owner)** → the board, "Alex (owner) · approver" | Recorded on cash-desk-paypal.onrender.com |
| 0:23-0:40 | Reconciliation tab: summary tiles, then the exception groups on the AG Grid Community board: Duplicate, Overpaid, Partial, Fee short, each row with its reason | Cursor runs down the group rows, then scrolls |
| 0:40-0:51 | Approvals tab → **Propose actions** → seven proposals: 2 reminders, 2 invoices, record payment, refund, overpayment refund |  |
| 0:51-1:14 | **Approve…** on "Refund duplicate" (800.00): POST https://api-m.sandbox.paypal.com/v2/payments/captures/…/refund, JSON body, PayPal-Request-Id, policy passes, two-person rule, "Approving as Alex (owner)"; **Approve (1 of 2)** → "Needs a 2nd approver"; Alex opens it again and is refused ("someone else must give the second"); **Switch to Sam (bookkeeper)** → second approval → Done, "by Alex (owner) + Sam (bookkeeper)" | The money moment |
| 1:14-1:22 | Audit log tab: green "Chain intact" banner, first approval by Alex, approval and run by Sam |  |
| 1:22-1:29 | **Export journal (CSV)** → the downloaded journal opened as a table | First 18 lines of the real export |
| 1:29-2:04 | The REAL sandbox desk (mode: sandbox, AI: codex gpt-6-sol), signed in as "Owner · approver": invoice 0015's fee shortfall with the AI's reason and the evidence (PayPal fields); invoice 0008's payment matched from its note; Approvals with the real refunds (80.00 duplicate, 4.50 tip) and the four rejected reminders; Audit log "Chain intact: 27 records" with cashdesk-q00007 / q00008 / q00009 | Live run of 7-9 Nov 2026 (docs/sandbox-evidence.md) |
| 2:04-2:17 | Closing card: "The AI does the work; you make the call." · cash-desk-paypal.onrender.com · or make demo · runs without keys · Claude, OpenAI, a ChatGPT plan or a local model · MIT | Text only |

## Voice-over

0:00
> Every small business paid through PayPal ends the month with the same questions. Which invoice was this for? Who paid twice? Who still owes us?

0:11
> Cash Desk answers them, and you can try it right now at cash desk paypal dot on render dot com. Sample data, nothing to install. Sign in with one click.

0:23
> It reconciles PayPal transactions against invoices. Most payments match on their own. The rest are exceptions, grouped on an AG Grid Community board: a duplicate, an overpayment, a partial payment, a fee shortfall.

0:40
> For each one, the agent proposes what to do: remind, invoice, record, refund. It never runs anything on its own.

0:51
> Approve shows the exact sandbox call and its idempotency key, after a policy check. This refund is over five hundred, so it needs two people. Alex approves, and nothing runs. Alex can't give the second approval. Sam signs in, approves, and only then does it run.

1:14
> Every step lands in a hash-chained audit log, with who approved it. One command proves nobody edited it.

1:22
> At month end, one click exports a balanced journal for the books.

1:29
> Now the real PayPal sandbox, with real accounts. Here the AI writes every reason and cites the PayPal fields behind it: this payment is short by exactly PayPal's fee. It matched a payment to its invoice from the note alone. Through the same gate it sent a real reminder and refunded a real duplicate and an overpaid tip, each with its own request id. It also proposed reminders to four customers who had just paid, before PayPal's report showed it. A person rejected them. That is why every action waits for you.

2:04
> Cash Desk for PayPal. The AI does the work; you make the call. Try it at cash desk paypal dot on render dot com, or run make demo. Bring any model you like.

## Shot list (capture checklist)

1. Title card and closing card, rendered as text.
2. The hosted demo's sign-in screen and banner, and the board after one click.
3. Summary tiles and exception groups (Reconciliation tab).
4. Approvals: before and after **Propose actions**.
5. Approve dialog for the refund (full URL, body and PayPal-Request-Id visible), the two-person rule, Alex refused for the second approval, Switch to Sam, Done.
6. Audit tab with the chain banner and the approvers' names.
7. Export journal (CSV) and the journal it produces.
8. The real sandbox desk as Owner: the 0015 reason with its evidence, the 0008 note match, Approvals and Audit log with the recorded request ids.

The AI in the sandbox scene is OpenAI's gpt-6-sol through the Codex CLI; Claude (`claude-sonnet-5-5`)
or any OpenAI-compatible API plugs into the same place.
