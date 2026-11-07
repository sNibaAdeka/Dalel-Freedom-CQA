"""/api/appeals, /api/decisions — appeal, AI dossier, reviewer decision (logic: a1's cqa.review)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError
from api.repo import plain

router = APIRouter()


def _state(request: Request):
    return request.app.state.cqa


def _review(state):
    if state.review is None:
        raise HTTPException(503, "Модуль апелляций (cqa.review) ещё не подключён")
    return state.review


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors()]
        raise HTTPException(422, "; ".join(msgs[:5])) from None
    except Exception as exc:
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            raise HTTPException(status, str(exc)) from None
        if isinstance(exc, KeyError):
            raise HTTPException(404, str(exc).strip("'\"")) from None
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from None
        raise


def _with_decision(state, appeal: dict) -> dict:
    if appeal.get("status") == "decided":
        found = state.repo.list_decisions(appeal_id=appeal["id"])
        if found:
            appeal = {**appeal, "decision": found[-1], "decision_id": found[-1].get("id")}
    return appeal


@router.post("/appeals")
def create_appeal(request: Request, body: dict = Body(...)):
    state = _state(request)
    review = _review(state)
    appeal = plain(_call(review.create_appeal, body.get("conv_id"), body.get("criterion_id"),
                         body.get("comment"), body.get("message_ids") or [],
                         version=body.get("checklist_version"), author=body.get("author")))
    llm_call = state.llm_call()
    if llm_call is None:  # rules-only dossier is instant: build it now
        _call(review.build_dossier, appeal["id"])
    else:
        review.build_dossier_async(appeal["id"], llm_call)
    return appeal


@router.get("/appeals")
def list_appeals(request: Request, status: Optional[str] = None, conv_id: Optional[str] = None):
    state = _state(request)
    return [_with_decision(state, a) for a in state.repo.list_appeals(status or None, conv_id or None)]


@router.get("/appeals/{appeal_id}")
def get_appeal(request: Request, appeal_id: str):
    state = _state(request)
    appeal = state.repo.appeal(appeal_id)
    if appeal is None:
        raise HTTPException(404, "Апелляция не найдена.")
    return _with_decision(state, appeal)


@router.get("/appeals/{appeal_id}/dossier")
def get_dossier(request: Request, appeal_id: str):
    state = _state(request)
    if state.repo.appeal(appeal_id) is None:
        raise HTTPException(404, "Апелляция не найдена.")
    dossier = state.repo.dossier(appeal_id)
    if dossier is None:
        dossier = _call(_review(state).build_dossier, appeal_id)
    return dossier_view(plain(dossier))


def dossier_view(d: dict) -> dict:
    """a1's dossier + (a3) flat ``quote`` / ``message_id`` on each argument and ``for`` / ``against`` aliases.

    Additive only: ``arguments_for`` / ``arguments_against`` with ``quotes`` stay as the frontend reads them."""
    if not isinstance(d, dict):
        return d
    d = dict(d)
    for side in ("arguments_for", "arguments_against"):
        items = []
        for arg in d.get(side) or []:
            arg = dict(arg) if isinstance(arg, dict) else {"text": str(arg)}
            quotes = [q for q in arg.get("quotes") or [] if isinstance(q, dict)]
            best = next((q for q in quotes if q.get("valid")), quotes[0] if quotes else None)
            arg.setdefault("quote", best.get("quote") if best else None)
            arg.setdefault("message_id", (best or {}).get("message_id") or next(iter(arg.get("message_ids") or []), None))
            items.append(arg)
        d[side] = items
    d["for"], d["against"] = d.get("arguments_for", []), d.get("arguments_against", [])
    return d


@router.get("/appeals/{appeal_id}/history")
def get_history(request: Request, appeal_id: str):
    """Journal of one appeal: AI evaluation -> appeal -> decision -> recalculation (a1 ReviewService.history)."""
    return plain(_call(_review(_state(request)).history, appeal_id))


@router.post("/decisions")
def create_decision(request: Request, body: dict = Body(...)):
    state = _state(request)
    return plain(_call(_review(state).decide, body.get("appeal_id"), body.get("action"),
                       body.get("new_answer"), body.get("reason"), reviewer=body.get("reviewer")))
