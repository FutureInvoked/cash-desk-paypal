"""S2: seed the PayPal sandbox with Northwind Studio invoices so the desk has real data.

Usage (after keys are in repo/.env):
  .venv/bin/python tools/seed_sandbox.py --payer-email <your sandbox PERSONAL account email> [--dry-run]

Creates and sends ~10 invoices (idempotent: reruns skip invoices tagged [cashdesk-seed Sxx]) and writes
docs/sandbox-pay-steps.md: what the owner pays from the sandbox buyer account so every case appears
(full, partial, twice, minus the fee, from another address, overpaid, unpaid). Prints no secrets.
"""

from __future__ import annotations

import argparse
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cashdesk.models import Money  # noqa: E402

TAG = "[cashdesk-seed {key}]"

# pay: full | partial | twice | none | minus_fee | other_email | over
PLAN = [
    {"key": "S01", "business": "Harbor Lane Coffee", "name": "Dana Whitfield", "item": "Menu board redesign", "amount": "125.00", "currency": "USD", "pay": "full"},
    {"key": "S02", "business": "Pinecrest Dental", "name": "Omar Reyes", "item": "Appointment cards", "amount": "86.00", "currency": "USD", "pay": "full"},
    {"key": "S03", "business": "Oakmont Realty", "name": "Grace Kim", "item": "Listing photo edits", "amount": "120.00", "currency": "USD", "pay": "partial"},
    {"key": "S04", "business": "Brightside Tutoring", "name": "Leo Martins", "item": "Worksheet illustrations", "amount": "80.00", "currency": "USD", "pay": "twice"},
    {"key": "S05", "business": "Redwood Legal", "name": "Sam Ortiz", "item": "Report template", "amount": "240.00", "currency": "USD", "pay": "none", "email": "finance@redwood-legal.example.com"},
    {"key": "S06", "business": "Tidewater Kayaks", "name": "Jon Park", "item": "Waiver form layout", "amount": "35.00", "currency": "USD", "pay": "none", "email": "jon@tidewater.example.com"},
    {"key": "S07", "business": "Northshore Yoga", "name": "Ben Carter", "item": "Class pass artwork", "amount": "50.00", "currency": "USD", "pay": "minus_fee"},
    {"key": "S08", "business": "Lumen Bakery", "name": "Maria Lumen", "item": "Website retainer", "amount": "150.00", "currency": "USD", "pay": "other_email", "email": "ap@lumen-bakery.example.com"},
    {"key": "S09", "business": "Atelier Vogel", "name": "Klara Vogel", "item": "Catalogue cover", "amount": "90.00", "currency": "EUR", "pay": "full"},
    {"key": "S10", "business": "Juniper & Sage Florists", "name": "Priya Nair", "item": "Flyer set", "amount": "45.00", "currency": "USD", "pay": "over"},
]


def _fee(amount: Decimal) -> Decimal:
    return (amount * Decimal("0.0349") + Decimal("0.49")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def instruction(p: dict, number: str, payer_email: str) -> str:
    a, c = Decimal(p["amount"]), p["currency"]
    ref = f"invoice {number}"
    return {
        "full": f"Open the invoice email/link for {ref} as {payer_email} and pay {a} {c} in full.",
        "partial": f"Send Money {(a / 2).quantize(Decimal('0.01'))} {c} to the business account with the note \"{ref} part 1\".",
        "twice": f"Pay {ref} in full ({a} {c}), then Send Money {a} {c} again with the note \"{ref}\" (a duplicate).",
        "none": "Do nothing: leave it unpaid so it becomes overdue.",
        "minus_fee": f"Send Money {a - _fee(a)} {c} (the total minus PayPal's fee) with the note \"{p['item']}\".",
        "other_email": f"Send Money {a} {c} from {payer_email} with the note \"{p['business']} - {p['item']}\" (no invoice number).",
        "over": f"Send Money {(a * Decimal('1.1')).quantize(Decimal('0.01'))} {c} with the note \"{ref} plus a tip\".",
    }[p["pay"]]


def seed(client, *, payer_email: str, dry_run: bool = False, steps_path: Path | None = None) -> dict:
    existing = {}
    if not dry_run:
        for inv in client.list_invoices():
            note = (inv.raw.get("detail") or {}).get("note") or ""
            for p in PLAN:
                if TAG.format(key=p["key"]) in note:
                    existing[p["key"]] = inv
    created = skipped = 0
    rows = []
    for p in PLAN:
        email = p.get("email") if p["pay"] in ("none", "other_email") else payer_email
        if dry_run:
            rows.append((p, "(not created)", email))
            continue
        if p["key"] in existing:
            inv = existing[p["key"]]
            skipped += 1
        else:
            inv = client.create_draft_invoice(
                recipient_email=email, recipient_name=p["name"], item=p["item"],
                amount=Money(Decimal(p["amount"]), p["currency"]), request_id=f"cashdesk-seed-{p['key']}-create",
                note=f"Thank you, {p['business']}. {TAG.format(key=p['key'])}")
            client.send_invoice(inv.id, request_id=f"cashdesk-seed-{p['key']}-send")
            created += 1
        rows.append((p, inv.number or inv.id, email))
    if steps_path and not dry_run:
        lines = [
            "# Sandbox payment steps (owner)",
            "",
            f"Log in at https://www.sandbox.paypal.com as the sandbox PERSONAL (buyer) account `{payer_email}`",
            "(its password is under Testing Tools -> Sandbox Accounts on developer.paypal.com). Then:",
            "",
            "| key | invoice | customer | amount | to | what to do |",
            "|---|---|---|---|---|---|",
        ]
        for p, num, email in rows:
            lines.append(f"| {p['key']} | {num} | {p['business']} | {p['amount']} {p['currency']} | {email} | "
                         f"{instruction(p, num, payer_email)} |")
        lines += ["", "Transaction Search can take up to ~3 hours to show new payments; payments made through an",
                  "invoice show up sooner because Cash Desk also reads them from Invoicing.", ""]
        steps_path.write_text("\n".join(lines))
    return {"created": created, "skipped": skipped, "plan": [p["key"] for p in PLAN]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payer-email", required=True, help="your sandbox PERSONAL (buyer) account email")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    from cashdesk.config import load_dotenv
    from cashdesk.paypal.sandbox import SandboxPayPalClient
    load_dotenv()
    c = SandboxPayPalClient.from_env()
    out = seed(c, payer_email=a.payer_email, dry_run=a.dry_run, steps_path=ROOT / "docs" / "sandbox-pay-steps.md")
    print(f"created {out['created']}, skipped {out['skipped']} (already seeded); steps: docs/sandbox-pay-steps.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
