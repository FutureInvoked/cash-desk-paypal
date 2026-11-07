"""E4: policy engine. Every rule has a pass test, a block test and bypass attempts."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from cashdesk.actions import propose
from cashdesk.ai import FallbackReconciler, reconcile
from cashdesk.models import Money
from cashdesk.paypal import FixturePayPalClient
from cashdesk.policy import HistoryEntry, Policy, PolicyConfig
from tools.make_fixtures import inv_id, txn_id

AS_OF = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CREATED = NOW - timedelta(minutes=10)


@pytest.fixture(scope="module")
def props():
    c = FixturePayPalClient()
    txns = c.search_transactions(datetime(2026, 10, 8, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc))
    invs = c.list_invoices()
    r = reconcile(invs, txns, as_of=AS_OF, reconciler=FallbackReconciler())
    return {p.type + ":" + (p.invoice_number or p.target): p for p in propose(r, invs, txns, as_of=AS_OF)}


def get(props, key):
    return copy.deepcopy(props[key])


REFUND = "refund_capture:NW-1015"
REMIND = "send_reminder:NW-1016"
RECORD = "record_payment:NW-1021"
DRAFT = f"create_draft_invoice:{txn_id('unbilled-ravi')}"


def check(p, history=(), policy=None, now=NOW, created=CREATED, **kw):
    return (policy or Policy()).check(p, now=now, created_at=created, history=list(history), **kw)


# ---------- baseline ----------

def test_all_fixture_proposals_pass_by_default(props):
    for key, p in props.items():
        d = check(p, invoice_due=Money(Decimal("1500.00"), "USD") if p.type == "record_payment" else None)
        assert d.allowed, (key, d.violations)


# ---------- allowlist ----------

def test_unknown_action_type_is_blocked(props):
    p = get(props, REFUND)
    p.type = "payout"
    d = check(p)
    assert not d.allowed and "allowlist" in d.codes


def test_disabled_action_type_is_blocked(props):
    pol = Policy(PolicyConfig(enabled_types=frozenset({"send_reminder"})))
    assert not check(get(props, REFUND), policy=pol).allowed
    assert check(get(props, REMIND), policy=pol).allowed


# ---------- amount cap ----------

def test_refund_over_cap_is_blocked(props):
    pol = Policy(PolicyConfig(caps={"refund_capture": Decimal("500")}))
    d = check(get(props, REFUND), policy=pol)
    assert not d.allowed and "amount_cap" in d.codes


def test_record_payment_cannot_exceed_what_is_due(props):
    d = check(get(props, RECORD), invoice_due=Money(Decimal("1000.00"), "USD"))
    assert not d.allowed and "amount_cap" in d.codes


def test_record_payment_requires_the_due_amount(props):
    d = check(get(props, RECORD))
    assert not d.allowed and "amount_cap" in d.codes


@pytest.mark.parametrize("value", ["0.00", "-800.00"])
def test_zero_or_negative_amounts_are_blocked(props, value):
    p = get(props, REFUND)
    p.amount = Money(Decimal(value), "USD")
    p.api_calls[0]["body"]["amount"]["value"] = value
    d = check(p)
    assert not d.allowed and "amount" in d.codes


def test_unsupported_currency_is_blocked(props):
    p = get(props, REFUND)
    p.amount = Money(Decimal("800.00"), "JPY")
    p.api_calls[0]["body"]["amount"]["currency_code"] = "JPY"
    assert "currency" in check(p).codes


# ---------- integrity: the call must be exactly what the proposal says ----------

def test_edited_path_is_blocked(props):
    p = get(props, REFUND)
    p.api_calls[0]["path"] = f"/v2/payments/captures/{txn_id('NW-1001')}/refund"
    d = check(p)
    assert not d.allowed and "integrity" in d.codes


def test_refund_body_amount_must_equal_proposal_amount(props):
    p = get(props, REFUND)
    p.api_calls[0]["body"]["amount"]["value"] = "8000.00"
    assert "integrity" in check(p).codes


def test_currency_switch_in_body_is_blocked(props):
    p = get(props, REFUND)
    p.api_calls[0]["body"]["amount"]["currency_code"] = "EUR"
    assert "integrity" in check(p).codes


def test_extra_smuggled_call_is_blocked(props):
    p = get(props, REMIND)
    p.api_calls.append({"method": "POST", "path": f"/v2/payments/captures/{txn_id('NW-1001')}/refund",
                        "body": {}, "headers": {}})
    assert "integrity" in check(p).codes


def test_wrong_method_is_blocked(props):
    p = get(props, REMIND)
    p.api_calls[0]["method"] = "DELETE"
    assert "integrity" in check(p).codes


def test_path_traversal_or_host_injection_is_blocked(props):
    p = get(props, REMIND)
    p.api_calls[0]["path"] = "https://evil.example.com/v2/invoicing/invoices/x/remind"
    assert "integrity" in check(p).codes
    p.target = "../../v1/oauth2/token"
    p.api_calls[0]["path"] = f"/v2/invoicing/invoices/{p.target}/remind"
    assert "integrity" in check(p).codes


def test_draft_invoice_items_must_add_up_to_the_amount(props):
    p = get(props, DRAFT)
    p.api_calls[0]["body"]["items"][0]["unit_amount"]["value"] = "2750.00"
    assert "integrity" in check(p).codes


def test_record_payment_body_amount_must_match(props):
    p = get(props, RECORD)
    p.api_calls[0]["body"]["amount"]["value"] = "15000.00"
    assert "integrity" in check(p, invoice_due=Money(Decimal("1500.00"), "USD")).codes


# ---------- rate cap ----------

def executed(key, minutes_ago, rid=None, status="executed"):
    return HistoryEntry(intent_key=key, status=status, at=NOW - timedelta(minutes=minutes_ago),
                        request_id=rid or f"r-{key}-{minutes_ago}", proposal_id=f"p-{key}-{minutes_ago}")


def test_rate_cap_blocks_after_n_actions_in_an_hour(props):
    pol = Policy(PolicyConfig(max_per_hour=3))
    hist = [executed(f"send_reminder:x{i}", 5 + i) for i in range(3)]
    d = check(get(props, REMIND), history=hist, policy=pol)
    assert not d.allowed and "rate_cap" in d.codes


def test_rate_cap_window_rolls(props):
    pol = Policy(PolicyConfig(max_per_hour=3))
    hist = [executed(f"send_reminder:x{i}", 61 + i) for i in range(3)]
    assert check(get(props, REMIND), history=hist, policy=pol).allowed


def test_rejected_items_do_not_count_toward_the_rate_cap(props):
    pol = Policy(PolicyConfig(max_per_hour=1))
    hist = [executed("send_reminder:x", 5, status="rejected")]
    assert check(get(props, REMIND), history=hist, policy=pol).allowed


# ---------- duplicate intent ----------

@pytest.mark.parametrize("status", ["pending", "approved", "executed"])
def test_same_intent_twice_is_blocked(props, status):
    p = get(props, REFUND)
    d = check(p, history=[executed(p.intent_key, 30, status=status)])
    assert not d.allowed and "duplicate_intent" in d.codes


def test_same_intent_after_a_rejection_is_allowed(props):
    p = get(props, REFUND)
    assert check(p, history=[executed(p.intent_key, 30, status="rejected")]).allowed


def test_the_items_own_history_entry_does_not_block_it(props):
    p = get(props, REFUND)
    own = HistoryEntry(p.intent_key, "pending", NOW, "rid", p.id)
    assert check(p, history=[own]).allowed


def test_retargeted_duplicate_is_still_caught_by_target(props):
    # Same capture, forged proposal id: still one refund per capture.
    p = get(props, REFUND)
    p.id = "p_forged"
    d = check(p, history=[HistoryEntry(p.intent_key, "executed", NOW, "rid", "p_original")])
    assert "duplicate_intent" in d.codes


# ---------- request id reuse ----------

def test_request_id_reused_for_another_intent_is_blocked(props):
    p = get(props, REFUND)
    hist = [HistoryEntry("send_reminder:other", "executed", NOW - timedelta(days=2), "cashdesk-abc", "p_other")]
    d = check(p, history=hist, request_id="cashdesk-abc")
    assert not d.allowed and "request_id" in d.codes


# ---------- expiry ----------

def test_expired_proposal_is_blocked(props):
    d = check(get(props, REMIND), created=NOW - timedelta(hours=25))
    assert not d.allowed and "expired" in d.codes


def test_future_created_at_beyond_skew_is_blocked(props):
    d = check(get(props, REMIND), created=NOW + timedelta(hours=2))
    assert not d.allowed and "clock_skew" in d.codes


def test_small_clock_skew_is_tolerated(props):
    assert check(get(props, REMIND), created=NOW + timedelta(seconds=30)).allowed


def test_naive_datetimes_are_rejected(props):
    with pytest.raises(ValueError):
        check(get(props, REMIND), now=NOW.replace(tzinfo=None))


# ---------- decision shape ----------

def test_decision_lists_every_violation_with_a_message(props):
    p = get(props, REFUND)
    p.type = "payout"
    d = check(p, created=NOW - timedelta(days=3))
    assert {"allowlist", "expired"} <= set(d.codes)
    assert all(v["message"] for v in d.violations)
    assert d.to_dict()["allowed"] is False
