"""Domain models. Money is always Decimal plus a currency code; floats are rejected."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"Money.amount must be Decimal, got {type(self.amount).__name__}")
        if not (isinstance(self.currency, str) and len(self.currency) == 3):
            raise ValueError(f"bad currency code {self.currency!r}")

    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def to_paypal(self) -> dict:
        return {"currency_code": self.currency, "value": f"{self.amount.quantize(Decimal('0.01')):.2f}"}

    def __str__(self) -> str:
        return f"{self.amount:.2f} {self.currency}"


@dataclass(frozen=True)
class Invoice:
    id: str
    number: str
    status: str
    total: Money
    due: Money
    paid: Money
    invoice_date: date | None
    due_date: date | None
    recipient_email: str | None
    recipient_name: str | None
    recipient_business: str | None
    items: tuple[str, ...]
    memo: str | None
    payment_ids: list[str] = field(default_factory=list)
    refund_ids: list[str] = field(default_factory=list)
    last_sent: datetime | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Transaction:
    id: str
    event_code: str
    status: str  # D denied, P pending, S success, V reversed/refunded
    initiated: datetime
    amount: Money
    fee: Money | None
    payer_email: str | None
    payer_name: str | None
    invoice_ref: str | None  # transaction_info.invoice_id
    paypal_invoice_id: str | None  # cart_info.paypal_invoice_id
    subject: str | None
    note: str | None
    reference_id: str | None  # paypal_reference_id (e.g. a refund's original)
    items: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> tuple[str, str, str]:
        """Transaction IDs aren't unique in reporting; this triple is."""
        return (self.id, self.event_code, self.status)
