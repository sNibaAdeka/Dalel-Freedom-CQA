"""Response builders in the frontend's shapes (docs/FRONTEND_HANDOFF.md, web/js/api.js of frontend-wave1-review).

Totals, sections, critical flags and formula steps always come from ``cqa.engine`` (a1); the LLM never computes them.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from api.repo import pick_eval, eval_id
from cqa import audio as audio_mod
from cqa import engine
from cqa.ingest import public_view
from cqa.models import ChecklistVersion

EVAL_DEFAULTS = {"evidence": [], "checked_range": None, "na_reason": None, "explanation": "", "suggestion": None,
                 "confidence": 0.0, "critical_hit": False, "model": "", "tokens_in": 0, "tokens_out": 0,
                 "cost_kzt": 0.0, "latency_ms": 0, "needs_review": False, "review_reasons": []}


def current_evals(version: ChecklistVersion, evals: list[dict], state=None) -> list[dict]:
    """Evaluation shown for each criterion of the version: the newest answered one for the same wording (hash).

    A criterion renamed or moved with unchanged wording keeps its evaluation (re-mapped to the new id)."""
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for e in evals:
        by_hash[e.get("criterion_hash")].append(e)
    out = []
    profile = state.settings()['profile'] if state is not None else None
    allowed = {state.eval_mode_key(m, profile) for m in ('fast', 'accurate')} if state is not None else None
    for c in version.criteria:
        candidates = by_hash.get(c.hash, [])
        if allowed is not None:
            candidates = [e for e in candidates if e.get('mode') in allowed]
        pv = state.prompt_version_for(c) if state is not None else None
        if pv is not None:
            candidates = [e for e in candidates if e.get('prompt_version') == pv]
        ev = pick_eval(candidates)
        if ev is None:
            continue
        ev = dict(ev)
        ev["criterion_id"] = c.id
        ev["title"] = c.title
        ev["critical_hit"] = engine.is_critical_hit(c, ev.get("answer"))
        out.append(ev)
    return out


def score_of(version: ChecklistVersion, conv_id: str, evals: list[dict], decisions: list[dict]):
    return engine.score(version, evals, decisions, conv_id=conv_id, strict=False)


def eval_view(ev: dict, trace: bool = False) -> dict:
    out = {**EVAL_DEFAULTS, **{k: v for k, v in ev.items() if trace or k != "raw"}}
    out["from_cache"] = bool(ev.get("from_cache") or ev.get("cache_hit"))
    out["evaluation_id"] = ev.get("evaluation_id") or eval_id(ev)
    if out.get("checked_range") is not None:
        out["checked_range"] = list(out["checked_range"])
    return out


def _appeals_with_decisions(appeals: list[dict], decisions: list[dict]) -> list[dict]:
    by_appeal = {d.get("appeal_id"): d for d in decisions}
    out = []
    for a in appeals:
        a = dict(a)
        d = by_appeal.get(a["id"])
        if d is not None:
            a["decision"] = d
            a["decision_id"] = d.get("id")
        out.append(a)
    return out


def detail(state, conv: dict, version: Optional[ChecklistVersion] = None, trace: bool = False) -> dict:
    version = version or state.checklists.active()
    repo = state.repo
    evals = current_evals(version, repo.evals_for(conv["id"]), state)
    decisions = repo.list_decisions(conv["id"])
    result = score_of(version, conv["id"], evals, decisions)
    row = engine.summarize(conv, result if evals else None, evals).model_dump(mode="json")
    shown = conv
    if conv.get("channel") == "call":
        lead, method, _ = audio_mod.lead_for(conv, state.calibration)
        shown = audio_mod.with_playback_offsets(conv, lead)
        shown = {**shown, "meta": {**shown.get("meta", {}), "audio_lead_sec": lead, "audio_offset_method": method}}
    out = public_view(shown)
    out.update({
        "evals": [eval_view(e, trace) for e in evals],
        "score": result.model_dump(mode="json"),
        "checklist": {**version.model_dump(mode="json"), "active": version.version == state.checklists.active_version()},
        "appeals": _appeals_with_decisions(repo.list_appeals(conv_id=conv["id"]), decisions),
        "decisions": decisions,
        "audit": repo.audit(conv["id"]),
        "status": row["status"], "needs_review": row["needs_review"],
        "latency_ms": sum(int(e.get("latency_ms") or 0) for e in evals),
        "cost_kzt": round(sum(float(e.get("cost_kzt") or 0) for e in evals), 4),
        "tokens_in": sum(int(e.get("tokens_in") or 0) for e in evals),
        "tokens_out": sum(int(e.get("tokens_out") or 0) for e in evals),
        "from_cache": sum(1 for e in evals if e.get("from_cache") or e.get("cache_hit")),
        "models": sorted({e.get("model") for e in evals if e.get("model")}),
    })
    return out


def rows(state, group_id: Optional[str] = None, lang: Optional[str] = None, channel: Optional[str] = None,
         status: Optional[str] = None) -> list[dict]:
    version = state.checklists.active()
    repo = state.repo
    evals_by_conv: dict[str, list[dict]] = defaultdict(list)
    for e in repo.all_evals():
        evals_by_conv[e["conv_id"]].append(e)
    decisions_by_conv: dict[str, list[dict]] = defaultdict(list)
    for d in repo.list_decisions():
        decisions_by_conv[d.get("conv_id")].append(d)
    appealed = {a["conv_id"] for a in repo.list_appeals(status="open")}
    running = {}
    if state.runs is not None and hasattr(state.runs, "running_conversations"):
        running = state.runs.running_conversations()
    out = []
    for conv in repo.conversations(group_id, (lang or "").upper() or None, channel or None):
        evals = current_evals(version, evals_by_conv.get(conv["id"], []), state)
        result = score_of(version, conv["id"], evals, decisions_by_conv.get(conv["id"], [])) if evals else None
        row = engine.summarize(conv, result, evals).model_dump(mode="json")
        if result is not None:
            lines = {ln.criterion_id: ln.model_dump(mode="json") for ln in result.lines}
            row["cells"] = [{**lines.get(cell["criterion_id"], {}), **cell} for cell in row["cells"]]
        if result is not None and result.status == "no_applicable":
            row["status"] = "no_applicable"
        if conv["id"] in running:
            row["status"] = running[conv["id"]]
        row["appealed"] = conv["id"] in appealed
        row["checklist_version"] = version.version
        row["n_messages"] = len(conv.get("messages") or [])
        out.append(row)
    if status:
        keep = {"needs_review": lambda r: r["needs_review"] or r["appealed"], "critical": lambda r: r["critical"],
                "review": lambda r: r["needs_review"] or r["critical"]}.get(status, lambda r: r["status"] == status)
        out = [r for r in out if keep(r)]
    return out
