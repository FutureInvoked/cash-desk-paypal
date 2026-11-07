"""FastAPI app: JSON API for the board plus the single-page UI (static/index.html)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import REPO_ROOT
from .queue import NotExecutable, PolicyBlocked

STATIC_DIR = REPO_ROOT / "static"


class Decision(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)


def create_app(desk=None) -> FastAPI:
    app = FastAPI(title="Cash Desk for PayPal", version=__version__)
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

    @app.get("/api/audit")
    def audit() -> dict:
        return d().audit_view()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
