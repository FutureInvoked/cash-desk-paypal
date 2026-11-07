"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import __version__
from .config import REPO_ROOT, load_settings

STATIC_DIR = REPO_ROOT / "static"


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Cash Desk for PayPal", version=__version__)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "paypal_mode": settings.paypal_mode, "version": __version__}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
