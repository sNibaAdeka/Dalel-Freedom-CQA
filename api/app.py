"""FastAPI application: all endpoints under /api + the static frontend (web/, ES modules with correct MIME types)."""
from __future__ import annotations

import mimetypes
import os
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import audio, checklists, conversations, exports, labeling, metrics, review, runs
from api.state import AppState


class ProductStaticFiles(StaticFiles):
    """Generated demo fixtures can contain DEV transcripts; opt in explicitly to serve them."""

    async def get_response(self, path, scope):
        parts = path.replace("\\", "/").split("/")
        if any(part.startswith(".") for part in parts if part not in ("", ".")) or (
            "mocks" in parts and os.environ.get("CQA_SERVE_DEMO_FIXTURES") != "1"
        ):
            raise HTTPException(404)
        return await super().get_response(path, scope)

for _ext, _type in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                    (".json", "application/json"), (".svg", "image/svg+xml"), (".html", "text/html"),
                    (".woff2", "font/woff2"), (".mp3", "audio/mpeg")):
    mimetypes.add_type(_type, _ext)


def create_app(state: Optional[AppState] = None) -> FastAPI:
    state = state or AppState()
    app = FastAPI(title="Дәлел — Conversation Quality Analytics API", version="1.0")
    app.state.cqa = state
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["GET", "POST"], allow_headers=["*"])
    api = APIRouter(prefix="/api")
    for module in (conversations, checklists, runs, exports, review, audio, metrics, labeling):
        api.include_router(module.router)

    @api.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
    def not_found(rest: str):
        raise HTTPException(404, f"Нет такого метода API: /api/{rest}")

    app.include_router(api)
    dynamics_dir = state.web_dir.parent / "dynamics"
    if dynamics_dir.is_dir():
        app.mount("/dynamics", ProductStaticFiles(directory=dynamics_dir, html=True), name="dynamics")
    if state.web_dir.is_dir():  # after the API routes: the frontend is the root fallback
        app.mount("/", ProductStaticFiles(directory=state.web_dir, html=True), name="web")
    return app
