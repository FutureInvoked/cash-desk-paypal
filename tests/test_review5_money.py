"""Review A (money invariants, commit e0c359e): the reviewer's shrunken regression cases, kept as
written (the "actual" comments describe e0c359e). Helpers in tests/review_a_common.py build raw PayPal
dictionaries and run the production parsers.
"""
import pytest
from tests.review_a_common import *  # noqa: F403


def test_aggregate_paid_amount_is_not_a_second_receipt():
    invs = [invoice(1, status="PAID", paid=1)]  # no payments.transactions
    r, p, j = run(invs, [txn("PAYMENT", 1)])
    assert r.invoices[0].received.amount == D("0.01")  # actual 0.02
    assert not [x for x in p if x.type.startswith("refund")]  # actual refund 0.01


def test_invoice_referenced_refund_reduces_the_overpayment():
    ts = [txn("PAYMENT", 2), txn("REFUND", -1, code="T1107", hour=1)]  # no capture reference
    r, p, j = run([invoice(1)], ts)
    assert not [x for x in p if x.type.startswith("refund")]  # actual refund 0.01
    assert r.invoices[0].received.amount == D("0.01")  # actual 0.02


def test_local_refund_of_an_old_capture_reduces_received():
    pays = [{"payment_id": "OLDPAY", "amount": money(1), "method": "PAYPAL"}]
    refs = {"refund_amount": money(1), "transactions": [
        {"refund_id": "REFUND", "amount": money(1), "method": "PAYPAL"}]}
    inv = invoice(1, paid=1, payments=pays, refunds=refs, due=1)
    r, p, j = run([inv], [txn("REFUND", -1, code="T1107", ref="OLDPAY")])
    assert r.invoices[0].received.amount == D("0.00")  # actual 0.01


def test_invoice_only_refund_of_a_local_capture_reduces_received():
    pays = [{"payment_id": "PAYMENT", "amount": money(1), "method": "PAYPAL"}]
    refs = {"refund_amount": money(1), "transactions": [
        {"refund_id": "OLDREFUND", "amount": money(1), "method": "PAYPAL"}]}
    inv = invoice(1, paid=1, payments=pays, refunds=refs, due=1)
    r, p, j = run([inv], [txn("PAYMENT", 1)])
    assert r.invoices[0].received.amount == D("0.00")  # actual 0.01


@pytest.mark.parametrize("code", ["T1107", "T1106"])
def test_credit_refund_increases_cash(code):
    r, p, j = run([], [txn("REFUND", 1, code=code, invoice_ref=None)])
    assert balance(j, "PayPal balance") == D("0.01")  # actual -0.01


def test_unapplied_refund_clears_unapplied():
    ts = [txn("PAYMENT", 1, invoice_ref=None),
          txn("REFUND", -1, code="T1107", hour=1, ref="PAYMENT", invoice_ref=None)]
    r, p, j = run([], ts)
    assert balance(j, "Unapplied payments") == D(0)  # actual -0.01
    assert balance(j, "Accounts receivable") == D(0)  # actual +0.01


def test_partially_refunded_duplicate_proposes_only_the_remainder():
    ts = [txn("FIRST", 2), txn("SECOND", 2, hour=1),
          txn("REFUND", -1, code="T1107", hour=2, ref="SECOND", invoice_ref=None)]
    r, p, j = run([invoice(2)], ts)
    (refund,) = [x for x in p if x.type == "refund_capture"]
    assert refund.amount.amount == D("0.01")  # actual 0.02; Desk blocks it
    assert refund.api_calls[0]["body"]["amount"] == money(1)


def test_recorded_payment_id_is_a_match_without_a_note():
    pays = [{"payment_id": "PAYMENT", "amount": money(1), "method": "PAYPAL"}]
    inv = invoice(1, status="PAID", paid=1, payments=pays)
    r, p, j = run([inv], [txn("PAYMENT", 1, invoice_ref=None)])
    assert r.invoices[0].received.amount == D("0.01")  # actual 0
    assert r.txn("PAYMENT").invoice_number == "INV-1"  # actual unmatched


def test_status_rows_do_not_change_cash_when_reordered():
    ts = [txn("PAYMENT", 1, status="P"), txn("PAYMENT", 1, status="S", fee=-1)]
    forward = run([invoice(1)], ts)
    backward = run([invoice(1)], ts[::-1])
    assert balance(forward[2], "PayPal balance") == balance(backward[2], "PayPal balance")
    # actual forward 0.01, backward 0.00; only the settled row carries the fee


def test_conversion_row_does_not_hide_an_earlier_payment():
    pays = [{"payment_id": "SAMEID", "amount": money(1), "method": "PAYPAL"}]
    inv = invoice(1, status="PAID", paid=1, payments=pays)
    r, p, j = run([inv], [txn("SAMEID", -1, code="T0200", invoice_ref=None)])
    assert r.invoices[0].received.amount == D("0.01")  # actual 0


def test_fee_exceeding_gross_never_produces_negative_debit():
    r, p, j = run([invoice(1)], [txn("PAYMENT", 1, fee=-2)])
    assert all(D(l[s] or 0) >= 0 for l in j for s in ("debit", "credit"))
    # actual PayPal debit -0.01. This is parser-valid; upstream occurrence is unconfirmed.


def test_partial_refund_topup_matches_the_remaining_amount():
    ts = [txn("FIRST", 2), txn("REFUND", -1, code="T1107", hour=1, ref="FIRST", invoice_ref=None),
          txn("TOPUP", 1, hour=2, invoice_ref=None)]
    r, p, j = run([invoice(2)], ts)
    assert r.invoices[0].received.amount == D("0.02")  # actual 0.01
    assert r.txn("TOPUP").outcome == "matched"  # actual unmatched; proposes another invoice


def test_fee_shortfall_topup_clears_the_invoice_exception():
    ts = [txn("FIRST", 1, ccy="EUR", invoice_ref=None),
          txn("TOPUP", 40, ccy="EUR", hour=1, invoice_ref=None)]
    r, p, j = run([invoice(41, ccy="EUR")], ts)
    assert r.invoices[0].received.amount == D("0.41") and r.invoices[0].outstanding.amount == 0
    assert r.invoices[0].outcome == "paid"  # actual fee_mismatch, still says a fee is missing


# ---------- the same shapes through the Desk: no unsafe refund is proposed ----------

def desk_for(monkeypatch, tmp_path, invs, ts):
    from cashdesk.paypal import FixturePayPalClient
    from cashdesk.service import Desk
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AI_API_KEY", "AI_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CASHDESK_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("CASHDESK_AS_OF", AS_OF.isoformat())
    c = FixturePayPalClient()
    c._invoices = [i.raw for i in invs]
    c._txns = [t.raw for t in ts]
    return Desk(client=c, reconciler=FallbackReconciler(), clock=lambda: T0), c


@pytest.mark.parametrize("shape", ["aggregate", "no_ref", "old_capture"])
def test_no_unsupported_refund_reaches_the_queue(monkeypatch, tmp_path, shape):
    if shape == "aggregate":
        invs, ts = [invoice(1, status="PAID", paid=1)], [txn("PAYMENT", 1)]
    elif shape == "no_ref":
        invs, ts = [invoice(1)], [txn("PAYMENT", 2), txn("REFUND", -1, code="T1107", hour=1)]
    else:
        pays = [{"payment_id": "OLDPAY", "amount": money(1), "method": "PAYPAL"}]
        refs = {"refund_amount": money(1), "transactions": [{"refund_id": "REFUND", "amount": money(1), "method": "PAYPAL"}]}
        invs = [invoice(1, paid=1, payments=pays, refunds=refs, due=1)]
        ts = [txn("REFUND", -1, code="T1107", ref="OLDPAY"), txn("PAYMENT", 1, hour=1)]
    d, c = desk_for(monkeypatch, tmp_path, invs, ts)
    d.refresh_proposals()
    assert not [x for x in d.queue.list() if x.proposal.type.startswith("refund")]
    assert c.calls == []


def test_partially_refunded_duplicate_queues_only_the_remainder(monkeypatch, tmp_path):
    ts = [txn("FIRST", 2), txn("SECOND", 2, hour=1),
          txn("REFUND", -1, code="T1107", hour=2, ref="SECOND", invoice_ref=None)]
    d, c = desk_for(monkeypatch, tmp_path, [invoice(2)], ts)
    d.refresh_proposals()
    (item,) = [x for x in d.queue.list() if x.proposal.type == "refund_capture"]
    assert item.status == "pending" and item.proposal.amount.amount == D("0.01")
    d.approve(item.id, "owner")
    assert d.execute(item.id)["status"] == "executed"
    assert c.calls[0]["body"]["amount"] == money(1)
