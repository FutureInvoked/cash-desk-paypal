#!/usr/bin/env bash
# R4: prove a judge can clone the repo and run it with no keys.
# Clones HEAD into a temp folder, installs, runs the full test suite and an end-to-end smoke
# (health, reconciliation, propose, approve+execute a refund, audit verify), then deletes the clone.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
TMP="${TMPDIR:-/tmp}/cashdesk-fresh-$$"
trap 'rm -rf "$TMP"' EXIT
git clone -q "$SRC" "$TMP"
cd "$TMP"
test ! -e .env || { echo "FAIL: .env is tracked"; exit 1; }
# a judge has no keys: strip every credential from the environment
CLEAN="env -u PAYPAL_CLIENT_ID -u PAYPAL_CLIENT_SECRET -u PAYPAL_MODE -u ANTHROPIC_API_KEY -u CASHDESK_VAR_DIR -u CASHDESK_AS_OF"
$CLEAN make test
make -n demo >/dev/null   # the demo target resolves
$CLEAN .venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from cashdesk.app import create_app
c = TestClient(create_app())
h = c.get("/api/health").json(); assert h == {"status": "ok", "paypal_mode": "fixtures", "version": h["version"]}, h
r = c.get("/api/reconciliation").json()
assert len(r["transactions"]) == 26 and len(r["invoices"]) == 26 and r["ai_status"].startswith("fallback"), r["ai_status"]
assert c.post("/api/proposals/refresh").json()["enqueued"] == 6
item = next(i for i in c.get("/api/approvals").json()["items"] if i["proposal"]["type"] == "refund_capture")
assert c.post(f"/api/approvals/{item['id']}/approve", json={"by": "judge"}).json()["status"] == "approved"
assert c.post(f"/api/approvals/{item['id']}/execute").json()["status"] == "executed"
a = c.get("/api/audit").json(); assert a["verify"]["ok"] and a["verify"]["count"] == 9, a["verify"]
assert "Cash Desk for PayPal" in c.get("/").text
print("SMOKE OK: health, 26/26 rows, 6 proposals, refund executed, audit chain of 9 verified")
PY
$CLEAN make audit-verify
echo "FRESH CLONE OK ($TMP, removed on exit)"
