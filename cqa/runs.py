"""Evaluation runs: planner, background execution with progress in the DB, preview (табло пересчёта, контроль).

Based on a3-api's draft (w-api, 14:41), moved onto cqa.db / cqa.checklist_store / typed models.

Plan = (conversations × criteria of a checklist version) − answered evaluations with the same criterion hash,
an acceptable mode and the same prompt version. Consequences:
- a new conversation has no evaluations → always evaluated live, all criteria;
- a weight / section change keeps every hash → 0 LLM calls, totals are just recomputed by the engine;
- replacing one criterion changes one hash → only that criterion is evaluated, on every conversation.

    mgr = RunManager(db, store, evaluate=cqa.evaluator.evaluate, workers=8)
    st = mgr.start_run(version=2, conv_ids=None, mode="fast", preview=False, activate=False, live=False)
    mgr.status(st["run_id"])   # {run_id, version, status, done, total, from_cache, llm_calls, cost_kzt, elapsed,
                               #  per_conv: [{id, old_total, new_total, status, from_cache, evaluated, error?}], ...}

live=True: evaluated pairs bypass the LLM response cache (no_cache=True); stored evaluations of unchanged criteria are
still reused. force=True: nothing stored is reused (all pairs evaluated).

`evaluate` is injected (a2: cqa.evaluator.evaluate; tests: a fake) and called as
`evaluate(conversation: Conversation, checklist: ChecklistVersion, mode=..., only_criteria=[ids], **kw)`; kw has
`no_cache=True` for live runs and `profile=` when given. It returns CriterionEval models or dicts.
`done` / `total` count conversations; `from_cache` counts reused (conversation, criterion) pairs; `llm_calls` and
`cost_kzt` come from the evaluations produced by this run (raw.calls − raw.cache_hits, cost_kzt).
Mock-provider evaluations contribute to `simulated_evaluations` (fresh criterion evaluations), never `llm_calls`.
"""
from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional, Union

from . import engine
from .checklist_store import ChecklistStore
from .db import Database, get_db
from .models import ChecklistVersion, Conversation, Criterion, CriterionEval, Score, now_iso

__all__ = ["RunManager", "plan", "pick_preview", "current_evals", "conversation_score", "PREVIEW_SIZE",
           "DEFAULT_WORKERS", "REUSABLE_MODES", "MODE_PREFERENCE"]

PREVIEW_SIZE = 5
DEFAULT_WORKERS = 8
# modes whose evaluations a run in the given mode may reuse (an accurate run never reuses fast answers)
REUSABLE_MODES = {"fast": ["fast", "accurate"], "accurate": ["accurate"]}
# Legacy export only; current_evals treats modes as an allowlist and always chooses the newest.
MODE_PREFERENCE = ["accurate", "fast"]

PromptVersion = Union[str, Callable[[Criterion], Optional[str]], None]


def _pv_for(prompt_version: PromptVersion, criterion: Criterion) -> Optional[str]:
    if callable(prompt_version):
        return prompt_version(criterion)
    return prompt_version


def _reusable_modes(mode: str) -> list[str]:
    base, sep, profile = mode.partition(":")
    suffix = f":{profile}" if sep else ""
    return [m + suffix for m in REUSABLE_MODES.get(base, [base])]


def _review_fields(ev: Optional[CriterionEval], status_of: Callable[[Optional[CriterionEval]], str]) -> dict:
    """needs_review / evidence_valid / validation_status of the model answer behind one score line (CQA-02)."""
    if ev is None:
        return {"needs_review": True, "evidence_valid": None, "validation_status": "missing"}
    return {"needs_review": bool(ev.needs_review),
            "evidence_valid": all(x.valid for x in ev.evidence) if ev.evidence else None,
            "validation_status": status_of(ev)}


def _profile_modes(mode: str) -> list[str]:
    """Both scoring modes in the same profile, independent of the requested evaluation mode."""
    _, sep, profile = mode.partition(":")
    return _reusable_modes(f"fast:{profile}" if sep else "fast")


def _resolve_mode(mode: str, profile: Optional[str]) -> tuple[str, str, str]:
    """Freeze the effective profile once and use the evaluator's canonical storage key."""
    from .evaluator import mode_key
    from .llm import active_profile

    base, sep, suffix = mode.partition(":")
    if base not in REUSABLE_MODES:
        raise ValueError("mode: fast или accurate")
    if sep and (not suffix or (profile and profile != suffix)):
        raise ValueError("Режим и профиль прогона не совпадают")
    profile = profile or suffix or active_profile()
    return base, mode_key(base, profile), profile


# --------------------------------------------------------------------------------- state of one dialogue

def current_evals(db: Database, conv_id: str, version: ChecklistVersion, modes: Optional[list[str]] = None,
                  prompt_version: PromptVersion = None) -> list[CriterionEval]:
    """Newest valid answers with matching hashes/prompts among the allowed modes (order is irrelevant).
    Callers showing a profile must supply its modes to isolate it.
    criterion_id is mapped to the version's id.
    """
    allowed = set(modes) if modes is not None else None
    by_hash = {c.hash: c for c in version.criteria}
    best: dict[str, CriterionEval] = {}
    for ev in db.list_evals(conv_id, list(by_hash)):  # oldest → newest
        if ev.answer is None:
            continue
        if allowed is not None and ev.mode not in allowed:
            continue
        pv = _pv_for(prompt_version, by_hash[ev.criterion_hash]) if prompt_version is not None else None
        if pv is not None and ev.prompt_version != pv:
            continue
        try:
            by_hash[ev.criterion_hash].check_answer(ev.answer)
        except ValueError:
            continue
        best[ev.criterion_hash] = ev
    out = []
    for c in version.criteria:
        if c.hash in best:
            ev = best[c.hash]
            out.append(ev if ev.criterion_id == c.id else ev.model_copy(update={"criterion_id": c.id}))
    return out


def conversation_score(db: Database, conv_id: str, version: ChecklistVersion,
                       evals: Optional[list[CriterionEval]] = None) -> Score:
    """Engine total for one dialogue: current evaluations + reviewer decisions (non-strict: never raises)."""
    evals = current_evals(db, conv_id, version) if evals is None else evals
    return engine.score(version, evals, db.list_decisions(conv_id=conv_id), conv_id=conv_id, strict=False)


# ---------------------------------------------------------------------------------------------- planning

def plan(db: Database, version: ChecklistVersion, conv_ids: Iterable[str], mode: str = "fast",
         prompt_version: PromptVersion = None, force: bool = False, *, profile: Optional[str] = None) -> list[dict]:
    """-> [{conv_id, evaluate: [criterion ids], reuse: [criterion ids]}] in the given order."""
    if profile is not None:
        _, mode, _ = _resolve_mode(mode, profile)
    modes = _reusable_modes(mode)
    rows = []
    for cid in conv_ids:
        have = set() if force else {e.criterion_id for e in current_evals(db, cid, version, modes, prompt_version)}
        rows.append({"conv_id": cid, "evaluate": [c.id for c in version.criteria if c.id not in have],
                     "reuse": [c.id for c in version.criteria if c.id in have]})
    return rows


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def pick_preview(convs: Iterable[Any], n: int = PREVIEW_SIZE) -> list[str]:
    """Deterministic, diverse sample: a call, KZ / EN / RU chats, then the rest in id order."""
    convs = sorted(convs, key=lambda c: _get(c, "id"))
    chosen: list[str] = []
    for channel, lang in (("call", None), ("chat", "KZ"), ("chat", "EN"), ("chat", "RU"), ("chat", "RU")):
        if len(chosen) >= n:
            break
        for c in convs:
            if _get(c, "id") not in chosen and _get(c, "channel") == channel and (lang is None or _get(c, "language") == lang):
                chosen.append(_get(c, "id"))
                break
    for c in convs:
        if len(chosen) >= n:
            break
        if _get(c, "id") not in chosen:
            chosen.append(_get(c, "id"))
    return chosen[:n]


# -------------------------------------------------------------------------------------------------- runs

class RunManager:
    """Background runs. One manager per process: a per-dialogue lock makes parallel runs evaluate a dialogue once."""

    def __init__(self, db: Optional[Database] = None, store: Optional[ChecklistStore] = None,
                 evaluate: Optional[Callable[..., list]] = None, *, workers: int = DEFAULT_WORKERS,
                 prompt_version: PromptVersion = None, evaluate_kwargs: Optional[dict] = None):
        self.db = db or get_db()
        self.store = store or ChecklistStore(self.db)
        self.evaluate = evaluate
        self.workers = max(1, int(workers))
        self.prompt_version = prompt_version
        self.evaluate_kwargs = dict(evaluate_kwargs or {})
        self._runs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._conv_locks: dict[str, threading.Lock] = {}

    # -- public
    def start_run(self, version: Optional[int] = None, conv_ids: Optional[list[str]] = None, *, mode: str = "fast",
                  preview: bool = False, activate: bool = False, group_id: Optional[str] = None, force: bool = False,
                  live: bool = False, profile: Optional[str] = None, wait: bool = False) -> dict:
        cl = self.store.get(version) if version is not None else self.store.active()
        baseline = self.store.active()
        base_mode, mode, profile = _resolve_mode(mode, profile or self.evaluate_kwargs.get("profile"))
        if conv_ids is not None:
            if not conv_ids:
                raise ValueError("conv_ids не должен быть пустым; передайте null для всех диалогов")
            ids = list(dict.fromkeys(conv_ids))
            unknown = [c for c in ids if self.db.get_conversation(c) is None]
            if unknown:
                raise KeyError(f"Неизвестные диалоги: {', '.join(unknown[:5])}")
            if preview:
                ids = ids[:PREVIEW_SIZE]
        else:
            convs = self.db.list_conversations(group_id=group_id)
            ids = pick_preview(convs) if preview else [c.id for c in convs]
        # live = fresh model answers for every pair that has to be evaluated (no LLM disk cache, no_cache=True).
        # It does NOT discard stored evaluations of unchanged criteria (same criterion_hash + prompt_version + mode):
        # a weight change stays 0 calls and a replaced criterion re-evaluates only its own pairs. New dialogues
        # have no stored evaluations, so they are always computed live. force=True re-evaluates everything
        # (scripts/control_run.py passes force=True and live=True explicitly).
        force = bool(force)
        # Pin prompt versions for this run too; configuration may change while it is queued.
        prompt_versions = {c.id: _pv_for(self.prompt_version, c) for c in cl.criteria}
        planned = plan(self.db, cl, ids, mode, lambda c: prompt_versions[c.id], force)
        # The "before" value is the current same-profile score, even when accurate cannot reuse fast.
        modes = _profile_modes(mode)
        decisions = {cid: self.db.list_decisions(conv_id=cid) for cid in ids}
        old_totals = {cid: engine.score(baseline, current_evals(self.db, cid, baseline, modes), decisions[cid],
                                       conv_id=cid, strict=False).total for cid in ids}
        rid = f"run-{uuid.uuid4().hex[:10]}"
        run = {"run_id": rid, "version": cl.version, "baseline_version": baseline.version, "mode": mode,
               "profile": profile, "preview": preview, "live": live, "force": force, "activate": activate,
               "activated": False, "status": "running", "done": 0, "total": len(ids), "from_cache": 0,
               "pairs_total": len(ids) * len(cl.criteria), "pairs_to_evaluate": sum(len(p["evaluate"]) for p in planned),
               "evaluated": 0, "simulated_evaluations": 0, "llm_calls": 0, "cost_kzt": 0.0,
               "elapsed": 0.0, "started_at": now_iso(),
               "finished_at": None, "errors": [],
               "per_conv": [{"id": p["conv_id"], "old_total": old_totals[p["conv_id"]], "new_total": None, "status": "pending",
                             "from_cache": len(p["reuse"]), "evaluated": [], "simulated_evaluations": 0,
                             "score": None, "answers": []}
                            for p in planned],
               "_calls": 0.0, "_t0": time.perf_counter(), "_base_mode": base_mode,
               "_prompt_versions": prompt_versions, "_decisions": decisions}
        with self._lock:
            self._runs[rid] = run
        self._persist(run)
        self.db.add_audit("run", rid, "run.started", {"version": cl.version, "mode": mode, "preview": preview,
                                                      "conversations": len(ids), "to_evaluate": run["pairs_to_evaluate"]})
        thread = threading.Thread(target=self._execute, args=(run, cl, baseline, ids), name=f"cqa-{rid}", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return self.status(rid)

    def status(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                return self._public(run)
        saved = self.db.get_run(run_id)
        if saved is None:
            raise KeyError(run_id)
        return saved

    def list_runs(self, limit: int = 20) -> list[dict]:
        return self.db.list_runs(limit)

    # -- internals
    def _public(self, run: dict) -> dict:
        out = copy.deepcopy({k: v for k, v in run.items() if not k.startswith("_")})
        out["llm_calls"] = int(round(run["_calls"]))
        out["cost_kzt"] = round(run["cost_kzt"], 4)
        out["elapsed"] = round((time.perf_counter() - run["_t0"]) if run["status"] == "running" else run["elapsed"], 2)
        return out

    def _persist(self, run: dict) -> None:
        with self._lock:
            snap = self._public(run)
            self.db.save_run(run["run_id"], snap["status"], snap, version=run["version"])

    def _conv_lock(self, conv_id: str) -> threading.Lock:
        with self._lock:
            return self._conv_locks.setdefault(conv_id, threading.Lock())

    def _execute(self, run: dict, cl: ChecklistVersion, baseline: ChecklistVersion, ids: list[str]) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f"cqa-{run['run_id']}") as pool:
                list(pool.map(lambda cid: self._one(run, cl, baseline, cid), ids))
            failed = any(r["status"] != "done" for r in run["per_conv"])
            if ids and run["activate"] and not run["preview"] and not failed:
                self.store.activate(cl.version)
                with self._lock:
                    run["activated"] = True
            with self._lock:
                run["status"] = "failed" if failed else "completed"
        except Exception as exc:  # pragma: no cover - defensive: the run must end with a status
            with self._lock:
                run["status"] = "failed"
                run["errors"].append({"conv_id": None, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            run["elapsed"] = round(time.perf_counter() - run["_t0"], 2)
            run["finished_at"] = now_iso()
            self._persist(run)
            pub = self._public(run)
            self.db.add_audit("run", run["run_id"], "run.completed", {k: pub[k] for k in (
                "status", "version", "done", "total", "from_cache", "evaluated", "simulated_evaluations",
                "llm_calls", "cost_kzt", "elapsed")})

    def _one(self, run: dict, cl: ChecklistVersion, baseline: ChecklistVersion, conv_id: str) -> None:
        row = next(r for r in run["per_conv"] if r["id"] == conv_id)
        try:
            with self._conv_lock(conv_id):  # another run may be evaluating this dialogue right now
                with self._lock:
                    row["status"] = "running"
                conv = self.db.get_conversation(conv_id)
                if conv is None:
                    raise KeyError(conv_id)
                with self._lock:
                    row.update(group_id=conv.group_id, language=conv.language, channel=conv.channel)
                # Score precisely this run's selected cache entries + freshly returned answers. Re-reading
                # global "current" evaluations could hide failures or prefer an older accurate/profile answer.
                reused = [] if run["force"] else current_evals(
                    self.db, conv_id, cl, _reusable_modes(run["mode"]), lambda c: run["_prompt_versions"][c.id])
                have = {ev.criterion_id for ev in reused}
                ids = [c.id for c in cl.criteria if c.id not in have]
                with self._lock:
                    row["from_cache"] = len(reused)
                fresh = self._evaluate(run, row, conv, cl, ids) if ids else []
                result = engine.score(cl, reused + fresh, run["_decisions"][conv_id], conv_id=conv_id, strict=False)
                if result.missing:
                    raise ValueError(f"Нет допустимого ответа по {', '.join(result.missing)}")
                # CQA-02: record the acceptance state of every model answer in the immutable run snapshot so the
                # export can tell an accepted answer from a rejected/flagged one (accepted|needs_review|rejected|missing)
                from .review import validation_status  # local import: cqa.review imports this module
                used = {ev.criterion_id: ev for ev in reused + fresh}
                with self._lock:
                    row["score"] = result.model_dump(mode="json")
                    row["answers"] = [{**line.model_dump(mode="json", include={
                        "criterion_id", "answer", "original_answer", "source", "points", "weight", "critical_hit"}),
                        **_review_fields(used.get(line.criterion_id), validation_status)}
                        for line in result.lines]
                    row["new_total"] = result.total
                    row["status"] = "done"
        except Exception as exc:  # one dialogue must not stop the run
            with self._lock:
                row["status"] = "error"
                row["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                run["errors"].append({"conv_id": conv_id, "error": row["error"]})
        finally:
            with self._lock:
                run["done"] += 1
                run["from_cache"] += row["from_cache"]
            self._persist(run)

    def _evaluate(self, run: dict, row: dict, conv: Conversation, cl: ChecklistVersion,
                  ids: list[str]) -> list[CriterionEval]:
        if self.evaluate is None:
            raise RuntimeError("Оценщик (cqa.evaluator.evaluate) не подключён")
        kw = dict(self.evaluate_kwargs)
        if run["live"]:
            kw["no_cache"] = True
        if run["profile"]:
            kw["profile"] = run["profile"]
        produced = list(self.evaluate(conv, cl, mode=run["_base_mode"], only_criteria=list(ids), **kw) or [])
        evals, calls, cost, problems = [], 0.0, 0.0, []
        simulated = 0
        seen_ids: set[str] = set()
        seen_calls = seen_real_evals = False
        for item in produced:
            try:
                ev = item if isinstance(item, CriterionEval) else CriterionEval.model_validate(item)
            except Exception as exc:
                problems.append(f"ответ оценщика не разобран: {type(exc).__name__}")
                continue
            if ev.criterion_id not in ids:
                problems.append("оценщик вернул незапрошенный критерий")
                continue
            if ev.criterion_id in seen_ids:
                problems.append(f"{ev.criterion_id}: повторный ответ оценщика")
                continue
            seen_ids.add(ev.criterion_id)
            c = cl.criterion(ev.criterion_id)
            pv = run["_prompt_versions"][c.id]
            if ((ev.conv_id and ev.conv_id != conv.id) or (ev.criterion_hash and ev.criterion_hash != c.hash)
                    or ev.mode not in (run["mode"], run["_base_mode"])
                    or (pv is not None and ev.prompt_version and ev.prompt_version != pv)):
                problems.append(f"{c.id}: ответ относится к другому диалогу, критерию, режиму или промпту")
                continue
            raw = ev.raw if isinstance(ev.raw, dict) else {}
            if ev.provider == "mock" or ev.model.startswith("mock:"):
                simulated += 1
            else:
                seen_real_evals = True
                if "calls" in raw:
                    seen_calls = True
                    usage = float(raw.get("calls") or 0) - float(raw.get("cache_hits") or 0)
                    if not math.isfinite(usage) or usage < 0:
                        problems.append(f"{c.id}: недопустимый счётчик вызовов")
                        continue
                    calls += usage
            amount = float(ev.cost_kzt or 0)
            if not math.isfinite(amount) or amount < 0:
                problems.append(f"{c.id}: недопустимая стоимость")
                continue
            cost += amount
            ev = self._sanitize(ev, conv, c, run)
            if ev.answer is None:
                prev = self.db.get_eval(conv.id, ev.criterion_hash, ev.prompt_version, ev.mode)
                if prev is not None and prev.answer is not None:
                    problems.append(f"{ev.criterion_id}: новый ответ не получен, сохранён прежний")
                    continue
            evals.append(ev)
        if seen_real_evals and not seen_calls:
            calls = 1.0  # non-mock evaluator gave no usage: count one call for this dialogue
        self.db.save_evals(evals)
        answered = sorted(e.criterion_id for e in evals if e.answer is not None)
        missing = sorted(set(ids) - set(answered))
        if missing:
            problems.append(f"Нет допустимого ответа модели по {', '.join(missing)}")
        with self._lock:
            row["evaluated"] = answered
            row["simulated_evaluations"] = simulated
            run["evaluated"] += len(answered)
            run["simulated_evaluations"] += simulated
            run["_calls"] += calls
            run["cost_kzt"] += cost
        self.db.add_audit("evaluation", conv.id, "evaluation.created",
                          {"run_id": run["run_id"], "criteria": answered, "mode": run["mode"], "version": cl.version,
                           "simulated_evaluations": simulated,
                           "failed": sorted(set(ids) - set(answered))}, conv_id=conv.id)
        if problems:
            raise ValueError("; ".join(problems[:3]))
        return evals

    def _sanitize(self, ev: CriterionEval, conv: Conversation, c: Criterion, run: dict) -> CriterionEval:
        """Pin the cache key to what the planner uses and never store an answer the checklist forbids."""
        upd: dict[str, Any] = {"conv_id": conv.id, "criterion_hash": c.hash, "mode": run["mode"]}
        pv = run["_prompt_versions"][c.id]
        if pv is not None:
            upd["prompt_version"] = pv
        elif not ev.prompt_version:
            upd["prompt_version"] = "unknown"
        if not ev.created_at:
            upd["created_at"] = now_iso()
        answer = ev.answer
        if answer is not None:
            try:
                answer = c.check_answer(answer)
            except ValueError as exc:
                upd.update(answer=None, needs_review=True,
                           review_reasons=list(ev.review_reasons) + [f"ответ модели не прошёл проверку: {exc}"])
                answer = None
            else:
                upd["answer"] = answer
        upd["critical_hit"] = engine.is_critical_hit(c, answer)
        out = ev.model_copy(update=upd)
        if hasattr(out, "__pydantic_extra__") and out.__pydantic_extra__ is not None:
            out.__pydantic_extra__["run_id"] = run["run_id"]
        return out
