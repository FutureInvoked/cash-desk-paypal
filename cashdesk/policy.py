"""Policy engine: the first gate every proposed action passes before a human sees Approve.

It runs again just before execution (E5), so a proposal edited after approval, a replay or a
burst still can't get through. Rules:
  allowlist         only the five action types, and only those enabled in config
  amount            a positive amount in an allowed currency
  amount_cap        per-type caps; record_payment may not exceed what the invoice still owes, and
                    refund_overpayment may not exceed what the customer overpaid
  integrity         the API calls are exactly the ones this type and target produce: method,
                    path (no host, no traversal), call count, and body amounts equal the proposal
  rate_cap          at most N approved/executed actions in a rolling hour
  duplicate_intent  one live intent per (type, target): pending, approved or executed blocks
  request_id        a PayPal-Request-Id may never be reused for a different intent
  expired           proposals older than the expiry can't be approved or run
  clock_skew        proposals dated in the future (beyond a small tolerance) are refused
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from .actions import ACTION_TYPES, Proposal
from .models import Money

ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,39}$")
LIVE_STATUSES = {"pending", "approved", "executed"}
COUNTED_STATUSES = {"approved", "executed"}


@dataclass(frozen=True)
class PolicyConfig:
    enabled_types: frozenset = frozenset(ACTION_TYPES)
    caps: dict = field(default_factory=lambda: {
        "refund_capture": Decimal("1000.00"),
        "refund_overpayment": Decimal("1000.00"),
        "create_draft_invoice": Decimal("5000.00"),
        "send_reminder": Decimal("50000.00"),
        "record_payment": Decimal("50000.00"),
    })
    currencies: frozenset = frozenset({"USD", "EUR"})
    max_per_hour: int = 10
    expiry: timedelta = timedelta(hours=24)
    skew: timedelta = timedelta(minutes=5)
    # two-person rule: these action types above this amount need two different approvers (None = off)
    second_approval_over: Decimal | None = Decimal("500.00")
    second_approval_types: frozenset = frozenset({"refund_capture", "refund_overpayment"})

    def approvals_needed(self, p: Proposal) -> int:
        if (self.second_approval_over is not None and p.type in self.second_approval_types
                and p.amount is not None and p.amount.amount > self.second_approval_over):
            return 2
        return 1


@dataclass(frozen=True)
class HistoryEntry:
    intent_key: str
    status: str  # pending | approved | executed | rejected | expired | failed
    at: datetime
    request_id: str | None
    proposal_id: str


@dataclass
class Decision:
    allowed: bool
    violations: list[dict]

    @property
    def codes(self) -> list[str]:
        return [v["code"] for v in self.violations]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "violations": self.violations}


def _money(m: dict | None) -> Money | None:
    try:
        return Money(Decimal(m["value"]), m["currency_code"])
    except (TypeError, KeyError, ArithmeticError, ValueError):
        return None


def expected_paths(p: Proposal) -> list[tuple[str, str]]:
    t = p.target
    return {
        "send_reminder": [("POST", f"/v2/invoicing/invoices/{t}/remind")],
        "record_payment": [("POST", f"/v2/invoicing/invoices/{t}/payments")],
        "refund_capture": [("POST", f"/v2/payments/captures/{t}/refund")],
        "refund_overpayment": [("POST", f"/v2/payments/captures/{t}/refund")],
        "create_draft_invoice": [("POST", "/v2/invoicing/invoices"),
                                 ("POST", "/v2/invoicing/invoices/{new_invoice_id}/send")],
    }.get(p.type, [])


class Policy:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()

    def check(self, p: Proposal, *, now: datetime, created_at: datetime, history: list[HistoryEntry],
              invoice_due: Money | None = None, request_id: str | None = None) -> Decision:
        if now.tzinfo is None or created_at.tzinfo is None:
            raise ValueError("policy times must be timezone-aware")
        cfg = self.config
        v: list[dict] = []

        def bad(code: str, message: str) -> None:
            v.append({"code": code, "message": message})

        # allowlist
        if p.type not in ACTION_TYPES:
            bad("allowlist", f"action type {p.type!r} is not on the allowlist")
        elif p.type not in cfg.enabled_types:
            bad("allowlist", f"action type {p.type!r} is disabled in policy")

        # amount and currency
        amt = p.amount
        if amt is None or amt.amount <= 0:
            bad("amount", "amount must be positive")
        if amt is not None and amt.currency not in cfg.currencies:
            bad("currency", f"currency {amt.currency} is not allowed")

        # caps
        if amt is not None and amt.amount > 0:
            cap = cfg.caps.get(p.type)
            if cap is not None and amt.amount > cap:
                bad("amount_cap", f"{amt} exceeds the {p.type} cap of {cap:.2f}")
            if p.type == "record_payment":
                if invoice_due is None:
                    bad("amount_cap", "record_payment needs the invoice's outstanding amount to check against")
                elif invoice_due.currency != amt.currency or amt.amount > invoice_due.amount:
                    bad("amount_cap", f"{amt} exceeds what the invoice still owes ({invoice_due})")
            if p.type == "refund_overpayment":
                # invoice_due carries the overpaid excess for this type, stored outside the proposal
                if invoice_due is None:
                    bad("amount_cap", "refund_overpayment needs the overpaid amount to check against")
                elif invoice_due.currency != amt.currency or amt.amount > invoice_due.amount:
                    bad("amount_cap", f"{amt} exceeds what the customer overpaid ({invoice_due})")

            if p.type == "refund_capture" and invoice_due is not None:
                # the duplicate's amount still unrefunded, re-read from the latest sync
                if invoice_due.currency != amt.currency or amt.amount > invoice_due.amount:
                    bad("amount_cap", f"{amt} exceeds what is still refundable on this duplicate ({invoice_due})")

        # integrity
        for msg in self._integrity(p):
            bad("integrity", msg)

        # rate cap
        # a run dated in the future (the clock stepped back) still counts
        window = [h for h in history if h.status in COUNTED_STATUSES and now - h.at <= timedelta(hours=1)
                  and h.proposal_id != p.id]
        if len(window) >= cfg.max_per_hour:
            bad("rate_cap", f"{len(window)} actions in the last hour; the limit is {cfg.max_per_hour}")

        # duplicate intent
        dupes = [h for h in history if h.intent_key == p.intent_key and h.status in LIVE_STATUSES
                 and h.proposal_id != p.id]
        if dupes:
            bad("duplicate_intent", f"{p.intent_key} is already {dupes[0].status} ({dupes[0].proposal_id})")

        # request id reuse
        if request_id:
            other = [h for h in history if h.request_id == request_id and h.intent_key != p.intent_key]
            if other:
                bad("request_id", f"PayPal-Request-Id {request_id} was already used for {other[0].intent_key}")

        # expiry and skew
        if created_at - now > cfg.skew:
            bad("clock_skew", "proposal is dated in the future")
        elif now - created_at > cfg.expiry:
            bad("expired", f"proposal is older than {cfg.expiry}")

        return Decision(not v, v)

    def _integrity(self, p: Proposal) -> list[str]:
        errs: list[str] = []
        if not ID_RE.match(p.target or ""):
            errs.append(f"target {p.target!r} is not a valid PayPal id")
        want = expected_paths(p)
        got = [(c.get("method"), c.get("path")) for c in p.api_calls]
        if got != want:
            errs.append(f"API calls {got} do not match what {p.type} on {p.target} may run")
            return errs
        if p.amount is None:
            return errs
        body = p.api_calls[0].get("body") or {}
        if p.type in ("refund_capture", "refund_overpayment", "record_payment"):
            if _money(body.get("amount")) != p.amount:
                errs.append(f"body amount {body.get('amount')} differs from the proposal amount {p.amount}")
        if p.type == "create_draft_invoice":
            items = body.get("items") or []
            totals = []
            for it in items:
                m = _money(it.get("unit_amount"))
                try:
                    q = Decimal(str(it.get("quantity", "1")))
                except ArithmeticError:
                    q = None
                if m is None or q is None:
                    errs.append("invoice item has no valid amount")
                    return errs
                totals.append(Money(m.amount * q, m.currency))
            ccys = {m.currency for m in totals} | {(body.get("detail") or {}).get("currency_code")}
            if ccys != {p.amount.currency}:
                errs.append(f"invoice currency {sorted(c for c in ccys if c)} differs from {p.amount.currency}")
            elif sum((m.amount for m in totals), Decimal("0")) != p.amount.amount:
                errs.append(f"invoice items don't add up to {p.amount}")
        return errs

