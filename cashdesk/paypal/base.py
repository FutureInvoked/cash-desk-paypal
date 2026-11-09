"""The one PayPal interface all app code depends on."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

from ..models import Invoice, Money, Transaction

MAX_SEARCH_WINDOW = timedelta(days=31)  # Transaction Search limit


def check_window(start: datetime, end: datetime) -> None:
    if end < start:
        raise ValueError("end before start")
    if end - start > MAX_SEARCH_WINDOW:
        raise ValueError("Transaction Search window must be at most 31 days; chunk the range")


class PayPalClient(ABC):
    """Reads (Transaction Search, Invoicing) and gated writes (remind, draft, send, record, refund).

    Every write takes a request_id that becomes the PayPal-Request-Id header.
    """

    mode: str = "abstract"

    @abstractmethod
    def list_invoices(self) -> list[Invoice]: ...

    @abstractmethod
    def search_transactions(self, start: datetime, end: datetime) -> list[Transaction]: ...

    @abstractmethod
    def send_reminder(self, invoice_id: str, *, note: str, request_id: str, subject: str | None = None) -> dict: ...

    @abstractmethod
    def create_draft_invoice(self, *, recipient_email: str, recipient_name: str, item: str, amount: Money,
                             request_id: str, note: str | None = None) -> Invoice: ...

    @abstractmethod
    def send_invoice(self, invoice_id: str, *, request_id: str) -> dict: ...

    @abstractmethod
    def record_payment(self, invoice_id: str, amount: Money, *, payment_date: date, method: str, note: str,
                       request_id: str) -> dict: ...

    @abstractmethod
    def refund_capture(self, capture_id: str, amount: Money, *, request_id: str, note_to_payer: str) -> dict: ...

    def verify_webhook_signature(self, raw_event: str, headers, *, webhook_id: str) -> str:
        """Ask PayPal whether a webhook delivery is genuine: "SUCCESS" or "FAILURE".
        Clients that can't ask PayPal (the fixture client) fail closed."""
        return "FAILURE"
