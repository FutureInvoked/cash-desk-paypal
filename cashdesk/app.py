"""FastAPI app: JSON API for the board plus the single-page UI (static/index.html)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .config import REPO_ROOT
from .queue import NotExecutable, PolicyBlocked

STATIC_DIR = REPO_ROOT / "static"


class Decision(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    # the fingerprint of the proposal the approver was shown (the board sends it)
    proposal_hash: str | None = Field(default=None, max_length=64)


LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"]


def extra_hosts() -> list[str]:
    import os
    return [h.strip() for h in os.environ.get("CASHDESK_ALLOWED_HOSTS", "").split(",") if h.strip()]


def allowed_hosts() -> list[str]:
    import os
    test = ["testserver"] if os.environ.get("CASHDESK_TESTING") else []  # Starlette's TestClient only
    return LOCAL_HOSTS + test + extra_hosts()


def _host_matches(host: str, patterns: list[str]) -> bool:
    return any(host == p or (p.startswith("*.") and host.endswith(p[1:])) for p in patterns)


def create_app(desk=None) -> FastAPI:
    from urllib.parse import urlsplit

    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    # no interactive API docs: the board is the only client
    app = FastAPI(title="Cash Desk for PayPal", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    hosts = allowed_hosts()

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # DNS rebinding / cross-site: a write must come from this exact origin (scheme-less host AND
        # port as the browser addressed the board), or from a host named in CASHDESK_ALLOWED_HOSTS.
        # Another local port (http://127.0.0.1:9999) is another site.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None:
                o = urlsplit(origin)
                same = (o.netloc and o.netloc.lower() == (request.headers.get("host") or "").lower()
                        and o.scheme == request.url.scheme)
                if not same and not _host_matches((o.hostname or "").lower(), extra_hosts()):
                    return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
            ctype = request.headers.get("content-type", "")
            if request.headers.get("content-length", "0") not in ("", "0") and not ctype.startswith("application/json"):
                return JSONResponse({"detail": "writes take application/json"}, status_code=415)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # a page on another hostname that resolves to 127.0.0.1 is refused before any route runs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    state = {"desk": desk}

    def d():
        if state["desk"] is None:
            from .service import Desk
            state["desk"] = Desk()
        return state["desk"]

    def paypal_failed(e: Exception) -> HTTPException:
        from .ai import clean_text
        return HTTPException(502, f"PayPal call failed: {clean_text(e)[:400]}")

    def guarded(fn, item_id, *a):
        from .paypal.sandbox import PayPalError
        try:
            d().queue.get(item_id)
        except KeyError:
            raise HTTPException(404, "no such approval item")
        try:
            return fn(item_id, *a)
        except PolicyBlocked as e:
            raise HTTPException(422, {"message": str(e), "violations": e.violations, "status": e.item.status})
        except NotExecutable as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except PayPalError as e:
            raise paypal_failed(e)
        except Exception as e:  # noqa: BLE001 - the call failed; the item records why (status failed)
            raise HTTPException(502, f"the PayPal call failed: {type(e).__name__}; see the item's error")

    def paypal_read(fn):
        from .paypal.sandbox import PayPalError
        try:
            return fn()
        except PayPalError as e:
            raise paypal_failed(e)

    @app.get("/api/health")
    def health() -> dict:
        from .config import load_settings
        return {"status": "ok", "paypal_mode": load_settings().paypal_mode, "version": __version__}

    @app.get("/api/reconciliation")
    def reconciliation() -> dict:
        return paypal_read(lambda: d().reconciliation())

    @app.post("/api/sync")
    def sync() -> dict:
        return paypal_read(lambda: d().sync())

    @app.post("/api/proposals/refresh")
    def refresh() -> dict:
        return paypal_read(lambda: d().refresh_proposals())

    @app.get("/api/approvals")
    def approvals() -> dict:
        return {"items": d().approvals()}

    @app.post("/api/approvals/{item_id}/approve")
    def approve(item_id: str, body: Decision) -> dict:
        def approve_shown(iid, by, h):
            if not h:  # approve what you were shown: the fingerprint comes from GET /api/approvals
                raise ValueError("proposal_hash is required: send the fingerprint of the proposal you reviewed")
            return d().approve(iid, by, h)
        return guarded(approve_shown, item_id, body.by, body.proposal_hash)

    @app.post("/api/approvals/{item_id}/reject")
    def reject(item_id: str, body: Decision) -> dict:
        return guarded(d().reject, item_id, body.by, body.note)

    @app.post("/api/approvals/{item_id}/execute")
    def execute(item_id: str) -> dict:
        return guarded(d().execute, item_id)

    @app.get("/api/export/journal.csv")
    def journal_csv() -> Response:
        desk = d()
        return Response(desk.journal_csv(), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="cashdesk-journal-{desk.as_of.isoformat()}.csv"'})

    @app.get("/api/audit")
    def audit() -> dict:
        return d().audit_view()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
