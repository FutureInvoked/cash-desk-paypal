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
    assert "logo" not in HTML.lower()


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
