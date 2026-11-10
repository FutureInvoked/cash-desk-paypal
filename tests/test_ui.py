"""B2: the board page contract. The browser check (screenshot, console) is done in the loop's verify step."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from cashdesk.app import create_app

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
AG_VERSION = "36.2.0"  # published 2026-09-16 on npm; pinned exactly


def test_ag_grid_community_is_pinned_exactly_from_jsdelivr():
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', HTML)
    ag = [s for s in srcs if "ag-grid" in s]
    assert ag == [f"https://cdn.jsdelivr.net/npm/ag-grid-community@{AG_VERSION}/dist/ag-grid-community.min.js"]
    for s in srcs:
        assert s.startswith("https://cdn.jsdelivr.net/npm/") and re.search(r"@\d+\.\d+\.\d+/", s), s


def test_no_enterprise_features_or_licence():
    low = HTML.lower()
    assert "enterprise" not in low
    assert "licensekey" not in low and "setlicensekey" not in low
    for enterprise_only in ("rowGroup", "masterDetail", "pivot", "sideBar", "groupDisplayType", "aggFunc"):
        assert enterprise_only not in HTML, enterprise_only


def test_no_logos_or_external_images():
    assert "<img" not in HTML.lower()
    assert not re.search(r"\blogos?\b", HTML.lower())  # (whole word: "/api/logout" is fine)


def test_reconciliation_grid_columns_and_features():
    for field in ('"outcome"', '"date"', '"invoice_number"', '"payer"', '"amount"', '"fee"', '"difference"',
                  '"reason"', '"rule"'):
        assert f"field: {field}" in HTML, field
    for feature in ("isFullWidthRow", "fullWidthCellRenderer", "isExternalFilterPresent", "doesExternalFilterPass",
                    "quickFilterText", "getRowClass", "cellRenderer", "wrapText", "autoHeight"):
        assert feature in HTML, feature
    assert "/api/reconciliation" in HTML
    assert 'id="exceptions-only"' in HTML and 'id="search"' in HTML and 'id="detail"' in HTML


def test_page_is_served_with_a_title():
    r = TestClient(create_app(desk=object())).get("/")
    assert r.status_code == 200 and "<title>Cash Desk for PayPal</title>" in r.text


def test_approvals_tab_contract():
    assert 'id="approvals-grid"' in HTML and 'id="propose"' in HTML
    assert "/api/approvals" in HTML and "/api/proposals/refresh" in HTML
    # approve/reject/execute endpoints are all wired
    for path in ("/approve", "/reject", "/execute"):
        assert path in HTML, path
    # the confirm dialog shows the exact sandbox call before anything runs
    assert '<dialog id="confirm"' in HTML
    assert "https://api-m.sandbox.paypal.com" in HTML
    assert "PayPal-Request-Id" in HTML and "cashdesk-" in HTML
    # errors from the policy are surfaced
    assert "violations" in HTML


def test_approve_only_runs_after_explicit_confirmation():
    # the approve endpoint is only called from the dialog's confirm handler
    i = HTML.index('id="confirm-run"')
    assert HTML.count("/approve`") == 1
    handler = HTML[HTML.index("async function confirmAndRun"):]
    handler = handler[:handler.index("\n}\n")]
    assert "/approve`" in handler and "/execute`" in handler
    assert i > 0


def test_audit_tab_contract():
    assert 'id="audit-grid"' in HTML and 'id="chain-status"' in HTML and 'id="head-hash"' in HTML
    assert "/api/audit" in HTML
    for field in ('"seq"', '"at"', '"event"', '"request_id"', '"hash"'):
        assert f"field: {field}" in HTML, field
    assert "Copy" in HTML  # the head hash can be copied to anchor the chain elsewhere


def test_summary_header_contract():
    for tile in ("kpi-matched", "kpi-unreconciled", "kpi-outstanding", "kpi-pending"):
        assert f'id="{tile}"' in HTML, tile
    assert "matched_pct" in HTML and "pending_actions" in HTML
    # summary refreshes after approvals change
    run = HTML[HTML.index("async function confirmAndRun"):]
    run = run[:run.index("\n}\n")]
    assert "loadSummary" in run or "load()" in run


def test_loading_is_cleared_before_rows_are_set():
    # AG Grid warning #99: setting rowData while `loading` is true triggers an internal hideOverlay()
    body = HTML[HTML.index("async function load()"):]
    body = body[:body.index("\n}\n")]
    assert body.index('setGridOption("loading", false)') < body.index('setGridOption("rowData", groupedRows(rows))')


def test_error_states_for_every_tab():
    assert HTML.count("Could not load") >= 3  # reconciliation, approvals, audit
    assert 'id="banner"' in HTML  # page-level connection banner
    assert "server" in HTML.lower()


def test_dialog_keyboard_and_focus():
    assert 'addEventListener("close"' in HTML  # focus returns to the opener when the dialog closes
    assert "lastFocus" in HTML


def test_no_horizontal_page_scroll_rules():
    assert "overflow-x: hidden" in HTML or "max-width: 100%" in HTML


def test_resync_handles_a_dead_server():
    h = HTML[HTML.index('getElementById("sync").onclick'):]
    h = h[:h.index("};")]
    assert "catch" in h and "showBanner" in h


def test_two_person_approval_in_the_dialog():
    # a first approval of a large refund is recorded and does NOT run; only the second runs it
    handler = HTML[HTML.index("async function confirmAndRun"):]
    handler = handler[:handler.index("\n}\n")]
    assert 'state !== "awaiting_second"' in handler
    assert "awaiting_second" in HTML and "Needs a 2nd approver" in HTML and "approvals_needed" in HTML
    assert "first_by" in HTML and "Two-person rule" in HTML


def test_overpayment_refund_and_journal_export_are_on_the_board():
    assert 'refund_overpayment: "Refund overpayment"' in HTML
    assert 'id="export-journal"' in HTML and 'href="/api/export/journal.csv"' in HTML



def test_dialog_shows_the_stored_request_id():
    # second review (H3) + browser check: the dialog must show the id the server will send
    calls = HTML[HTML.index("function callsFor"):]
    calls = calls[:calls.index("\n}\n")]
    assert "it.request_id" in calls


# ---------- P3: sign-in on the board ----------

def _fn(name: str) -> str:
    body = HTML[HTML.index(f"function {name}"):]
    return body[:body.index("\n}\n")]


def test_sign_in_screen_with_demo_buttons_and_a_password_form():
    assert 'id="signin"' in HTML and 'id="signin-form"' in HTML
    assert 'id="signin-user"' in HTML and 'type="password"' in HTML and 'autocomplete="current-password"' in HTML
    for path in ("/api/me", "/api/login", "/api/demo-login", "/api/login-options", "/api/logout"):
        assert path in HTML, path
    assert 'id="demo-users"' in HTML and "Sign in as" in HTML
    assert "python -m cashdesk.users add" in HTML  # how to get an account outside sample data


def test_the_board_stays_hidden_until_signed_in():
    assert '<div id="app" hidden>' in HTML
    boot = _fn("boot")
    assert "/api/me" in boot and "showSignin" in boot and "startBoard" in boot


def test_who_is_signed_in_switch_user_and_sign_out():
    assert 'id="who"' in HTML and 'id="switch-user"' in HTML and 'id="signout"' in HTML
    assert "me.demo" in HTML and "me.can_write" in HTML  # switch for demo approvers; viewers see no write buttons


def test_no_typed_approver_name_anywhere():
    assert 'id="confirm-by"' not in HTML and "Approved by<input" not in HTML
    handler = _fn("confirmAndRun")
    assert "by," not in handler and "{ by" not in handler and "by:" not in handler
    assert 'id="confirm-who"' in HTML and '"Approving"' in HTML and "as <strong>${esc(me.display_name)}</strong>" in HTML


def test_second_approval_names_the_first_approver_and_stops_the_same_person():
    oc = _fn("openConfirm")
    assert "first_by_name" in oc and "me.username" in oc and "run.disabled = true" in oc


def test_names_shown_are_display_names_and_escaped():
    assert "first_by_name" in HTML and "decided_by_name" in HTML and "by_name" in HTML
    assert "esc(me.display_name)" in HTML


def test_every_data_call_goes_through_the_401_aware_fetch():
    api_fetch = _fn("apiFetch")
    assert "401" in api_fetch and "showSignin" in api_fetch
    raw = re.findall(r'(?<![A-Za-z])fetch\("/api/(reconciliation|approvals|audit|sync|proposals|export)', HTML)
    assert raw == [], raw
    assert "apiFetch(url" in _fn("post")


# ---------- seventh review: L11 ----------

def test_the_dialog_rechecks_who_is_signed_in_before_it_names_the_approver():
    oc = _fn("openConfirm")
    assert oc.startswith("async function openConfirm") or "async function openConfirm" in HTML
    assert 'apiFetch("/api/me")' in oc and "renderWho" in oc


def test_finished_rows_show_who_even_to_viewers():
    r = HTML[HTML.index("ActionsRenderer.prototype.init"):]
    r = r[:r.index("};")]
    assert r.index("first_by_name") < r.index("view only")  # who acted is shown before the role check
