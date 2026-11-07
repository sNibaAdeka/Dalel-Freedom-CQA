"""/api/health, /api/config, /api/metrics."""
from __future__ import annotations

import os
from collections import defaultdict
from statistics import mean
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request

from api import views
from api.state import LOCATIONS, MODES

router = APIRouter()


def _state(request: Request):
    return request.app.state.cqa


def _per_conversation(state) -> list[dict]:
    """Current evaluations of the active version grouped per conversation (complete sets only)."""
    version = state.checklists.active()
    n = len(version.criteria)
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for e in state.repo.all_evals():
        by_conv[e["conv_id"]].append(e)
    out = []
    for conv_id, evals in by_conv.items():
        cur = views.current_evals(version, evals, state)
        if len(cur) == n:
            out.append({"conv_id": conv_id, "evals": cur,
                        "cost_kzt": sum(float(e.get("cost_kzt_full", e.get("cost_kzt")) or 0) for e in cur),
                        "cost_usd": sum(float(e.get("cost_usd") or 0) for e in cur),
                        "latency_ms": sum(int(e.get("latency_ms") or 0) for e in cur),
                        "models": sorted({e.get("model") for e in cur if e.get("model")})})
    return out


def cost_per_eval(state) -> dict:
    convs = _per_conversation(state)
    if not convs:
        return {"cost_per_eval": None, "cost_per_eval_usd": None, "latency_per_eval_ms": None, "cost_basis": 0}
    models = sorted({m for c in convs for m in c["models"]})
    return {"cost_per_eval": round(mean(c["cost_kzt"] for c in convs), 4),
            "cost_per_eval_usd": round(mean(c["cost_usd"] for c in convs), 6),
            "latency_per_eval_ms": round(mean(c["latency_ms"] for c in convs)),
            "cost_basis": len(convs), "cost_models": models}


@router.get("/health")
def health(request: Request):
    state = _state(request)
    s = state.settings()
    info = state.model_info(s["mode"], s["profile"])
    return {"ok": True, "provider": info.get("provider"), "model": info.get("model"), "mode": s["mode"],
            "location": s["location"], "profile": s["profile"], "votes": info.get("votes"), "arbiter": info.get("arbiter"),
            "offline": os.environ.get("CQA_OFFLINE") == "1", **cost_per_eval(state),
            "evaluator": state.evaluator_ready, "runs": state.runs is not None, "review": state.review is not None,
            "prompt_version": state.prompt_version(), "active_version": state.checklists.active_version(),
            "conversations": len(state.repo.conversations())}


@router.get("/config")
def get_config(request: Request):
    state = _state(request)
    s = state.settings()
    return {**s, "modes": list(MODES), "locations": list(LOCATIONS), "profiles": state.profiles(),
            "model": state.model_info(s["mode"], s["profile"])}


@router.post("/config")
def set_config(request: Request, body: dict = Body(...)):
    state = _state(request)
    try:
        state.update_settings(mode=body.get("mode"), location=body.get("location"), profile=body.get("profile"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return get_config(request)


@router.get("/metrics")
def metrics(request: Request, group_id: Optional[str] = None):
    state = _state(request)
    rows = views.rows(state, group_id=group_id)
    evaluated = [r for r in rows if r["status"] not in ("pending", "running", "incomplete")]
    llm = state.repo.llm_stats()
    convs = _per_conversation(state)
    by_model: dict[str, dict] = defaultdict(lambda: {"evals": 0, "cost_kzt": 0.0})
    for c in convs:
        for e in c["evals"]:
            m = by_model[e.get("model") or "?"]
            m["evals"] += 1
            m["cost_kzt"] = round(m["cost_kzt"] + float(e.get("cost_kzt") or 0), 4)
    appeals = state.repo.list_appeals()
    return {"conversations": len(rows), "evaluated": len(evaluated),
            "pending": sum(r["status"] == "pending" for r in rows),
            "needs_review": sum(bool(r["needs_review"]) for r in rows), "critical": sum(bool(r["critical"]) for r in rows),
            "escalation_rate": round(sum(bool(r["needs_review"]) for r in evaluated) / len(evaluated), 4) if evaluated else None,
            "appeals_open": sum(a["status"] == "open" for a in appeals), "appeals_total": len(appeals),
            "decisions": len(state.repo.list_decisions()),
            "cost_kzt": round(sum(c["cost_kzt"] for c in convs), 4), "llm_calls": llm["calls"],
            "cache_hits": llm["cache_hits"], "llm_errors": llm["errors"], "avg_latency_ms": llm["avg_latency_ms"],
            **cost_per_eval(state), "by_model": dict(by_model)}
