"""Read-only, explicitly projected inputs for independent human annotation.

Do not reuse api.views: its conversation projections contain model evaluations.
No ChecklistStore is constructed here (its constructor can seed/write the DB).
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict

from cqa.db import Database
from cqa.models import AnswerType, Channel, ChecklistVersion, Conversation, Language, Speaker

router = APIRouter(prefix="/labeling", tags=["blind labeling"])


def get_db(request: Request) -> Database:
    """Use the app's existing Database, including custom/test paths.

    cqa.db.get_db is a storage factory, not a Request dependency; calling it
    here would silently use a different database when AppState has a custom path.
    """
    return request.app.state.cqa.db


DB = Annotated[Database, Depends(get_db)]


class BlindModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationMeta(BlindModel):
    id: str
    language: Language
    channel: Channel
    group_id: str
    message_count: int


class TranscriptMessage(BlindModel):
    id: str
    idx: int
    speaker: Speaker
    text: str
    ts_raw: str
    t_sec: float | None


class BlindConversation(ConversationMeta):
    messages: list[TranscriptMessage]
    transcript_hash: str


class BlindCriterion(BlindModel):
    id: str
    hash: str
    title: str
    section: str | None
    description: str
    answer_type: AnswerType
    scale: list[float] | None
    scale_labels: dict[str, str] | None
    na_allowed: bool
    na_rule: str
    guidance: list[str]


class ChecklistMeta(BlindModel):
    checklist_id: str
    version: int
    name: str
    criterion_count: int
    checklist_hash: str


class BlindChecklist(ChecklistMeta):
    criteria: list[BlindCriterion]


def _digest(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _meta(conv: Conversation) -> ConversationMeta:
    return ConversationMeta(id=conv.id, language=conv.language, channel=conv.channel,
                            group_id=conv.group_id, message_count=len(conv.messages))


def _conversation(conv: Conversation) -> BlindConversation:
    # Model contracts allow extras: even message.meta / translation_ru can carry
    # generated content. Select original fields explicitly, never dump+delete.
    messages = [TranscriptMessage(id=m.id, idx=m.idx, speaker=m.speaker, text=m.text,
                                  ts_raw=m.ts_raw, t_sec=m.t_sec) for m in conv.messages]
    payload = {**_meta(conv).model_dump(), "messages": [m.model_dump() for m in messages]}
    # Group membership is selection metadata, not an annotation input.
    content = {k: v for k, v in payload.items() if k not in ("group_id", "message_count")}
    return BlindConversation(**payload, transcript_hash=_digest(content))


def _checklist(cl: ChecklistVersion) -> BlindChecklist:
    criteria = [BlindCriterion(id=c.id, hash=c.hash, title=c.title, section=c.section,
                               description=c.description, answer_type=c.answer_type,
                               scale=c.scale, scale_labels=c.scale_labels, na_allowed=c.na_allowed,
                               na_rule=c.na_rule, guidance=c.guidance) for c in cl.criteria]
    payload = {"checklist_id": cl.checklist_id, "version": cl.version, "name": cl.name,
               "criteria": [c.model_dump() for c in criteria]}
    return BlindChecklist(**payload, criterion_count=len(criteria), checklist_hash=_digest(payload))


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/conversations", response_model=list[ConversationMeta])
def conversations(db: DB, response: Response, group_id: str | None = None,
                  language: Language | None = None, channel: Channel | None = None):
    _private(response)
    return [_meta(c) for c in db.list_conversations(group_id=group_id, language=language, channel=channel)]


@router.get("/conversations/{conv_id}", response_model=BlindConversation)
def conversation(conv_id: str, db: DB, response: Response):
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Диалог не найден")
    _private(response)
    return _conversation(conv)


@router.get("/checklists", response_model=list[ChecklistMeta])
def checklists(db: DB, response: Response):
    _private(response)
    fields = set(ChecklistMeta.model_fields)
    return [ChecklistMeta(**_checklist(cl).model_dump(include=fields)) for cl in db.list_checklist_versions()]


@router.get("/checklists/{version}", response_model=BlindChecklist)
def checklist(version: Annotated[int, Path(ge=1)], db: DB, response: Response):
    cl = db.get_checklist_version(version)
    if cl is None:
        raise HTTPException(404, "Версия чек-листа не найдена")
    _private(response)
    return _checklist(cl)
