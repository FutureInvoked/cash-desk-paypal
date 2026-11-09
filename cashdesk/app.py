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


LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]", "testserver"]


def allowed_hosts() -> list[str]:
    import os
    extra = [h.strip() for h in os.environ.get("CASHDESK_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return LOCAL_HOSTS + extra


def create_app(desk=None) -> FastAPI:
    from urllib.parse import urlsplit

    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app = FastAPI(title="Cash Desk for PayPal", version=__version__)
    hosts = allowed_hosts()

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # DNS rebinding / cross-site: a write must not come from another site's page
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if (urlsplit(origin).hostname or "") not in hosts:
                return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
        return await call_next(request)

    # a page on another hostname that resolves to 127.0.0.1 is refused before any route runs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    state = {"desk": desk}

    def d():
        if state["desk"] is None:
            from .service import Desk
            state["desk"] = Desk()
        return state["desk"]

    def guarded(fn, *a):
        try:
            return fn(*a)
        except KeyError:
            raise HTTPException(404, "no such approval item")
        except PolicyBlocked as e:
            raise HTTPException(422, {"message": str(e), "violations": e.violations, "status": e.item.status})
        except NotExecutable as e:
            raise HTTPException(409, str(e))

    @app.get("/api/health")
    def health() -> dict:
        from .config import load_settings
        return {"status": "ok", "paypal_mode": load_settings().paypal_mode, "version": __version__}

    @app.get("/api/reconciliation")
    def reconciliation() -> dict:
        return d().reconciliation()

    @app.post("/api/sync")
    def sync() -> dict:
        return d().sync()

    @app.post("/api/proposals/refresh")
    def refresh() -> dict:
        return d().refresh_proposals()

    @app.get("/api/approvals")
    def approvals() -> dict:
        return {"items": d().approvals()}

    @app.post("/api/approvals/{item_id}/approve")
    def approve(item_id: str, body: Decision) -> dict:
        return guarded(d().approve, item_id, body.by)

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
