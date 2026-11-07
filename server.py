"""«Дәлел» — Conversation Quality Analytics server (Freedom Broker CQA).

    uvicorn server:app --port 8000        # API under /api + the frontend from web/ on http://localhost:8000

Env: CQA_DB (SQLite path, default results/cqa.sqlite), CQA_WEB_DIR (frontend dir, default web/), CQA_MODE (fast|accurate),
CQA_PROFILE (cloud|claude|local|mock, a2's config/models.json), CQA_RUN_WORKERS (parallel conversations per run),
CQA_DATA_API (organizers' GET link, in .env). On first start the DEV set (data/raw/dev_records.json) is loaded if present.
"""
from __future__ import annotations

import os

from api.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
