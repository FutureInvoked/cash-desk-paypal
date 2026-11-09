"""Webhook-only server: POST /webhooks/paypal and GET /webhooks/health, nothing else.

Run it on its own port (make webhooks) and point a tunnel at it, so the public internet can reach
the webhook and never the board, its approvals or its API. Logic: cashdesk/webhooks.py.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .webhooks import MAX_BODY, handle


def create_webhook_app(desk=None) -> FastAPI:
    app = FastAPI(title="Cash Desk webhooks", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    state = {"desk": desk}

    def d():
        if state["desk"] is None:
            from .service import Desk
            state["desk"] = Desk()
        return state["desk"]

    @app.post("/webhooks/paypal")
    async def paypal(request: Request) -> JSONResponse:
        # read at most MAX_BODY + 1 bytes, whatever Content-Length says (chunked bodies have none)
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"status": "rejected", "error": "body too large"}, status_code=413)
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > MAX_BODY:
                return JSONResponse({"status": "rejected", "error": "body too large"}, status_code=413)
        # PayPal verify and the re-sync are blocking network calls: keep them off the event loop
        status, body = await run_in_threadpool(handle, d(), bytes(buf), dict(request.headers))
        return JSONResponse(body, status_code=status)

    @app.get("/webhooks/health")
    def health() -> dict:
        return {"status": "ok", "webhook_id_set": bool(os.environ.get("PAYPAL_WEBHOOK_ID"))}

    return app


app = create_webhook_app()
