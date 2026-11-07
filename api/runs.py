"""/api/runs — evaluation runs (planner and execution: a1's cqa.runs; evaluator: a2 via AppState.evaluate)."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from api.state import MODES

router = APIRouter()


def _state(request: Request):
    return request.app.state.cqa


def _manager(state):
    if state.runs is None:
        raise HTTPException(503, "Модуль прогонов (cqa.runs) ещё не подключён")
    return state.runs


@router.post("/runs")
def create_run(request: Request, body: dict = Body(default_factory=dict)):
    """{version?, conv_ids?, preview?, mode?, live?, force?, activate?, group_id?, wait?} -> {run_id, ...progress}.

    preview: 5 diverse conversations, the version is not activated. live: no LLM response cache for the pairs that
    are evaluated; stored evaluations of unchanged criteria (same hash + prompt version + mode) are still reused, so
    applying a weight change is 0 calls and a replaced criterion re-evaluates only its pairs. force: re-evaluate all.
    """
    state = _state(request)
    mgr = _manager(state)
    s = state.settings()
    mode = body.get("mode") or s["mode"]
    if mode not in MODES:
        raise HTTPException(422, "mode: fast или accurate")
    profile = body.get("profile") or s["profile"]
    try:
        st = mgr.start_run(version=body.get("version"), conv_ids=body.get("conv_ids"),
                       preview=bool(body.get("preview")), mode=state.eval_mode_key(mode, profile), profile=profile,
                       live=bool(body.get("live")), force=bool(body.get("force")), activate=bool(body.get("activate")),
                       group_id=body.get("group_id") or None, wait=bool(body.get("wait")))
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _shape(st)


@router.get("/runs")
def list_runs(request: Request, limit: int = 20):
    state = _state(request)
    return state.repo.list_runs(limit)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str):
    state = _state(request)
    if state.runs is not None:
        try:
            return _shape(state.runs.status(run_id))
        except KeyError:
            pass
    saved = state.repo.get_run(run_id)
    if saved is None:
        raise HTTPException(404, "Прогон не найден.")
    return _shape(saved)


def _shape(st: dict) -> dict:
    """Frontend run shape (web/js/api.js, run_progress fixtures): + cache_total = (conversation, criterion) pairs."""
    st = dict(st)
    st.setdefault("cache_total", st.get("pairs_total"))
    st.setdefault("simulated_evaluations", 0)
    return st
