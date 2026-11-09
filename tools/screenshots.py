"""Re-shoot the README screenshots from a running sample-data board.

Needs Playwright (not a project dependency): pip install playwright && playwright install chromium.
Start a fresh board with no keys and no AI, then run this against it:

    rm -rf var/shots && PAYPAL_MODE=fixtures AI_PROVIDER=none CASHDESK_VAR_DIR=var/shots make demo
    python tools/screenshots.py http://127.0.0.1:8000 docs/screenshots

It proposes actions, opens the approve dialog of the 800.00 duplicate refund (two approvers), runs
one reminder so the audit log has a full approve-and-run trail, and saves five JPEGs.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", device_scale_factor=1)
        page = ctx.new_page()
        page.goto(base)
        page.wait_for_selector("#grid .ag-row", timeout=60000)
        time.sleep(1.0)
        page.screenshot(path=str(out / "b2-reconciliation-grid.jpg"), quality=85, type="jpeg")

        page.click("nav.tabs button[data-tab='approvals']")
        page.click("#propose")
        page.wait_for_selector("#approvals-grid .ag-row", timeout=20000)
        time.sleep(1.0)
        page.screenshot(path=str(out / "b3-approvals-grid.jpg"), quality=85, type="jpeg")

        items = page.evaluate("async () => (await (await fetch('/api/approvals')).json()).items")
        refund = next(i for i in items if i["proposal"]["type"] == "refund_capture")
        page.click(f"#approvals-grid .ag-row[row-id='{refund['id']}'] [data-act='approve']")
        page.wait_for_selector("dialog#confirm[open]")
        page.fill("#confirm-by", "Owner")
        time.sleep(0.6)
        page.screenshot(path=str(out / "b3-approve-dialog.jpg"), quality=85, type="jpeg")
        page.keyboard.press("Escape")

        # one reminder approved and run, so the audit tab shows the whole trail
        reminder = next(i for i in items if i["proposal"]["type"] == "send_reminder")
        page.click(f"#approvals-grid .ag-row[row-id='{reminder['id']}'] [data-act='approve']")
        page.wait_for_selector("dialog#confirm[open]")
        page.fill("#confirm-by", "Owner")
        page.click("#confirm-run")
        page.wait_for_function("!document.querySelector('dialog#confirm').open", timeout=20000)
        time.sleep(0.8)
        page.click("nav.tabs button[data-tab='audit']")
        page.wait_for_selector("#audit-grid .ag-row", timeout=20000)
        time.sleep(1.0)
        page.screenshot(path=str(out / "b4-audit-and-summary.jpg"), quality=85, type="jpeg")

        light = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme="light")
        lp = light.new_page()
        lp.goto(base)
        lp.wait_for_selector("#grid .ag-row", timeout=60000)
        time.sleep(1.0)
        lp.screenshot(path=str(out / "b5-light-1280.jpg"), quality=85, type="jpeg")
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
