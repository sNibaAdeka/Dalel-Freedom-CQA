"""/api/checklists — versions, diff, activation, lint (versions themselves: a1's cqa.checklist_store)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

router = APIRouter()


def _state(request: Request):
    return request.app.state.cqa


def _with_active(cl, active: int) -> dict:
    return {**cl.model_dump(mode="json"), "active": cl.version == active}


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, ValidationError):
        msgs = [f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}".strip(": ") for e in exc.errors()]
        return HTTPException(422, "; ".join(msgs[:5]) or "Некорректный чек-лист")
    return HTTPException(422, str(exc))


@router.get("/checklists")
def list_checklists(request: Request):
    store = _state(request).checklists
    active = store.active_version()
    return [_with_active(cl, active) for cl in store.versions()]


@router.post("/checklists")
def create_checklist(request: Request, body: dict = Body(...)):
    """New version from a full checklist body (editor), or from edit operations: {"changes": [...]}.
    Optional: base_version, change_note, activate."""
    store = _state(request).checklists
    activate = bool(body.pop("activate", False)) if isinstance(body, dict) else False
    try:
        if isinstance(body.get("changes"), list):
            res = store.apply(body["changes"], base_version=body.get("base_version"),
                              change_note=body.get("change_note") or "", activate=activate)
        else:
            res = store.create(body, base_version=body.pop("base_version", None), activate=activate)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from None
    except (ValueError, ValidationError) as exc:
        raise _err(exc) from None
    out = res.response()
    out["active"] = res.checklist.version == store.active_version()
    return out


@router.post("/checklists/lint")
def lint_criterion(request: Request, body: Any = Body(...)):
    """Wording check of one criterion: deterministic checks + (when a model is available) one LLM call."""
    from cqa.checklist_store import validate_body

    state = _state(request)
    crit = body.get("criterion", body) if isinstance(body, dict) else {}
    warnings: list[str] = []
    if not str(crit.get("description") or "").strip():
        warnings.append("Добавьте точное условие «Да» в описании критерия.")
    if crit.get("na_allowed") and not str(crit.get("na_rule") or "").strip():
        warnings.append("N/A разрешён, но условие неприменимости не описано.")
    if crit.get("answer_type") in ("scale", "numeric") and not crit.get("scale"):
        warnings.append("Для шкального критерия задайте ступени шкалы.")
    try:
        cl = state.checklists.active()
        draft = cl.model_dump()
        draft["criteria"] = [c for c in draft["criteria"] if c["id"] != crit.get("id")] + [
            {"id": crit.get("id") or "LINT", "title": crit.get("title") or "?", **crit}]
        _, more = validate_body({**draft, "version": 0})
        warnings += [w for w in more if (crit.get("id") or "LINT") in w]
    except Exception as exc:
        warnings.append(str(exc)[:300])
    llm_call = state.llm_call()
    source = "rules"
    if llm_call is not None and crit.get("title"):
        try:
            data, _ = llm_call(
                "Ты методист контроля качества. Проверь формулировку критерия оценки оператора: двусмысленность, "
                "пересечение с другими критериями, нет условия N/A, нельзя проверить по тексту диалога. Ответ — JSON "
                "{\"warnings\": [до 3 коротких предупреждений по-русски]}; если всё хорошо — пустой список.",
                f"Критерий: {crit}\nДругие критерии: {[c.title for c in state.checklists.active().criteria]}")
            warnings += [str(w) for w in (data or {}).get("warnings", [])[:3]]
            source = "llm"
        except Exception:
            pass
    return {"warnings": list(dict.fromkeys(warnings)), "source": source}


@router.get("/checklists/{version}")
def get_checklist(request: Request, version: int):
    store = _state(request).checklists
    try:
        return _with_active(store.get(version), store.active_version())
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from None


@router.get("/checklists/{a}/diff/{b}")
def diff_checklists(request: Request, a: int, b: int):
    store = _state(request).checklists
    try:
        d = store.diff(a, b).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from None
    d.setdefault("from", a)
    d.setdefault("to", b)
    return d


@router.post("/checklists/{version}/activate")
def activate_checklist(request: Request, version: int):
    store = _state(request).checklists
    try:
        cl = store.activate(version)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from None
    return {"version": cl.version, "active": True}
