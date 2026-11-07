"""/api/ingest, /api/conversations, /api/conversations/{id}."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from api import views
from cqa import ingest

router = APIRouter()


def _state(request: Request):
    return request.app.state.cqa


@router.post("/ingest")
def ingest_dataset(request: Request, group_id: str = Query("DEV", min_length=1, max_length=120),
                   source: str = Query("auto", pattern="^(auto|file|api)$"), body: Any = Body(None)):
    """Load a dataset: request body (records / API envelope / one record) > local file > organizers' API."""
    state = _state(request)
    try:
        if body:
            payload = [body] if isinstance(body, dict) and "transcript" in body else body
            records, gid = ingest.parse_payload(payload)
            convs = ingest.normalize_records(records, group_id if group_id != "DEV" or not gid else gid)
            src = "upload"
        else:
            path = ingest.ROOT / "data/raw/dev_records.json" if group_id == "DEV" else ingest.raw_path(group_id)
            if source != "api" and path.exists():
                convs, src = ingest.ingest_file(path, group_id=group_id), "file"
            elif source == "file":
                raise HTTPException(404, f"Нет локального файла набора {group_id}")
            else:
                convs, src = ingest.fetch_group(group_id), "api"
    except ingest.IngestError as exc:
        raise HTTPException(422 if "Expected" in str(exc) or "record" in str(exc) else 502, str(exc)) from None
    if not convs:
        raise HTTPException(422, "В наборе нет диалогов")
    try:
        return state.ingest(convs, src)
    except ValueError as exc:
        raise HTTPException(getattr(exc, "status", 422), str(exc)) from None


@router.get("/conversations")
def list_conversations(request: Request, group_id: Optional[str] = None, lang: Optional[str] = None,
                       channel: Optional[str] = None, status: Optional[str] = None):
    return views.rows(_state(request), group_id or None, lang or None, channel or None, status or None)


@router.get("/conversations/{conv_id}")
def get_conversation(request: Request, conv_id: str, version: Optional[int] = None, trace: bool = False):
    state = _state(request)
    conv = state.repo.conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Диалог не найден.")
    try:
        cl = state.checklists.get(version) if version else None
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    return views.detail(state, conv, cl, trace=trace)
