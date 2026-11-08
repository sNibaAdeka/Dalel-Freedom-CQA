"""Appeals, reviewer dossier and decisions — Student Guide p.2, functions 05 and 06.

Based on a3-api's draft (w-api, 14:41), moved onto cqa.db / cqa.checklist_store / typed models.

Flow (journal of 4 steps): AI evaluation (run) → appeal (criterion, comment, messages) → reviewer decision
(confirm / change + mandatory reason) → the engine recomputes the total. The model's CriterionEval is never
modified: a Decision is a separate record that overrides the answer inside engine.score().
The dossier (arguments for / against with quotes + recommendation) uses one injected LLM call
`llm_call(system, user) -> (json, usage)` (a2: cqa.llm.complete_json); its answer is validated here.

    svc = ReviewService(db, store, llm_call=None)
    appeal = svc.create_appeal(conv_id, criterion_id, comment, message_ids)
    dossier = svc.build_dossier(appeal.id)                    # or build_dossier_async(appeal.id)
    result = svc.decide(appeal.id, "change", 20, "причина")   # DecisionResult: old_total, new_total, decision, score
    svc.history(appeal.id)                                    # evaluation → appeal → decision → recalculation
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from pydantic import ValidationError

from .checklist_store import ChecklistStore
from .db import AppealAlreadyDecidedError, Database, get_db
from .models import (Appeal, AppealCreate, ChecklistVersion, Conversation, Criterion, CriterionEval, Decision,
                     DecisionCreate, DecisionResult, Score, now_iso)
from .runs import conversation_score, current_evals

__all__ = ["ReviewService", "ReviewError", "DOSSIER_SYSTEM", "DOSSIER_PROMPT_VERSION", "build_dossier_prompt",
           "validate_dossier", "LLMCall"]

# -- export acceptance state (strict review CQA-02): a preliminary answer must never look final in any export
VALIDATION_STATES = ("accepted", "needs_review", "rejected", "missing")
_REJECTED_REASONS = ("ответ модели не прошёл проверку", "оценка не получена", "нет доказательства")


def validation_status(ev: Optional[CriterionEval]) -> str:
    """accepted | needs_review | rejected | missing for one stored model answer.

    rejected = the validator finally refused the answer or its evidence (or a cited quote is not verbatim);
    needs_review = valid evidence but flagged for a human (low confidence, rule disagreement, critical hit...).
    The evaluator's structured fields win: ``validation_status`` (valid / rejected / failed) and ``provisional``;
    the reason-prefix heuristics below only cover evaluations stored before those fields existed."""
    if ev is None or ev.answer is None:
        return "missing"
    declared = getattr(ev, "validation_status", None)
    if declared == "failed":
        return "missing"
    if declared == "rejected" or (getattr(ev, "provisional", False) is True and declared != "valid"):
        return "rejected"
    if declared == "valid":
        if any(not e.valid for e in ev.evidence):
            return "rejected"
        return "needs_review" if ev.needs_review else "accepted"
    explicit = (getattr(ev, "__pydantic_extra__", None) or {}).get("validation_status")
    if explicit in VALIDATION_STATES:
        return explicit
    if any(not e.valid for e in ev.evidence) or any(
            str(r).startswith(_REJECTED_REASONS) for r in ev.review_reasons or []):
        return "rejected"
    return "needs_review" if ev.needs_review else "accepted"


def export_status(source: Optional[str], validation: str) -> str:
    """final when a reviewer decided the criterion or the model answer was accepted; otherwise preliminary."""
    return "final" if source == "decision" or validation == "accepted" else "preliminary"


DOSSIER_PROMPT_VERSION = "dossier-v1"
LLMCall = Callable[[str, str], tuple]

DOSSIER_SYSTEM = (
    "Ты — помощник проверяющего контроля качества Freedom Broker. Оператор оспорил оценку одного критерия чек-листа. "
    "Подготовь досье: доводы в пользу апелляции (оценку стоит изменить) и доводы в пользу исходной оценки "
    "(её стоит подтвердить). Каждый довод опирается на конкретные реплики диалога: укажи id реплики и ТОЧНУЮ цитату "
    "(дословный фрагмент текста этой реплики). Реплики бота и системы — контекст, на них тоже можно ссылаться. "
    "Оценивай строго по формулировке критерия, шкале и правилу N/A. Не выдумывай фактов. Решение принимает человек: "
    "дай рекомендацию и уверенность. Пиши по-русски, кратко.\n"
    "Ответ — только JSON-объект: {\"arguments_for\": [{\"text\": str, \"message_ids\": [str], \"quotes\": [str]}], "
    "\"arguments_against\": [{\"text\": str, \"message_ids\": [str], \"quotes\": [str]}], "
    "\"recommended_action\": \"confirm\" | \"change\", \"recommended_answer\": ответ по типу критерия "
    "(\"yes\" / \"no\" / \"na\" / ступень шкалы) или null, \"confidence\": число 0..1, \"recommendation\": str}"
)


class ReviewError(ValueError):
    """Business error with an HTTP status for the API layer (404 not found, 409 conflict, 422 invalid)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _fmt_answer(a: Any) -> str:
    if a is None:
        return "нет"
    if isinstance(a, float):
        return str(int(a)) if a.is_integer() else str(a)
    return {"yes": "да (yes)", "no": "нет (no)", "na": "N/A (na)"}.get(a, str(a))


def _utf16(text: str, i: int) -> int:
    return len(text[:i].encode("utf-16-le")) // 2


# ------------------------------------------------------------------------------------------ dossier (pure)

def _criterion_text(c: Criterion) -> str:
    lines = [f"{c.id}. {c.title} (тип: {c.answer_type}, вес {c.weight:g})", c.description]
    if c.scale:
        labels = c.scale_labels or {}
        steps = []
        for s in sorted(c.scale, reverse=True):
            key = str(int(s)) if float(s).is_integer() else str(s)
            steps.append(f"{key} — {labels.get(key, '')}".rstrip(" —"))
        lines.append("Шкала: " + "; ".join(steps))
    if c.na_allowed:
        lines.append(f"N/A разрешён: {c.na_rule}")
    else:
        lines.append("N/A не разрешён.")
    if c.critical:
        lines.append(f"Грубая ошибка: ответ «{c.critical_when}» означает, что нарушение выявлено.")
    lines += [f"Уточнение: {g}" for g in c.guidance]
    return "\n".join(x for x in lines if x)


def build_dossier_prompt(conv: Conversation, criterion: Criterion, ev: Optional[CriterionEval], appeal: Appeal,
                         current_answer: Any = None) -> tuple[str, str]:
    """(system, user) for the dossier call."""
    by_id = {m.id: m for m in conv.messages}
    transcript = "\n".join(f"[{m.id}] {m.speaker}: {m.text}" for m in conv.messages)
    evidence = "; ".join(f"[{e.message_id}] «{e.quote}»" for e in (ev.evidence if ev else [])) or "нет"
    marked = "\n".join(f"[{mid}] {by_id[mid].speaker}: {by_id[mid].text}" for mid in appeal.message_ids if mid in by_id)
    parts = [f"КРИТЕРИЙ\n{_criterion_text(criterion)}",
             "ИСХОДНАЯ ОЦЕНКА МОДЕЛИ\n"
             f"ответ: {_fmt_answer(ev.answer if ev else None)}\n"
             f"обоснование: {(ev.explanation if ev else '') or 'нет'}\n"
             f"доказательства: {evidence}\n"
             f"проверенный участок: {list(ev.checked_range) if ev and ev.checked_range else 'нет'}\n"
             f"причина N/A: {(ev.na_reason if ev else None) or 'нет'}"]
    if current_answer is not None and ev is not None and current_answer != ev.answer:
        parts.append(f"ТЕКУЩИЙ ОТВЕТ (после прошлых решений проверяющего): {_fmt_answer(current_answer)}")
    parts.append(f"АПЕЛЛЯЦИЯ ОПЕРАТОРА\nкомментарий: {appeal.comment}\n"
                 f"отмеченные реплики: {', '.join(appeal.message_ids) or 'нет'}" + (f"\n{marked}" if marked else ""))
    parts.append(f"ДИАЛОГ ({conv.channel}, {conv.language})\n{transcript}")
    return DOSSIER_SYSTEM, "\n\n".join(parts)


def _find_quote(q: str, mids: list[str], by_id: dict) -> Optional[tuple[str, str, int]]:
    """-> (message_id, exact quote, python start) or None. Tries the quote as given, then without outer quote marks."""
    for cand in dict.fromkeys([q, q.strip("«»\"“”'")]):
        if not cand:
            continue
        for m in [by_id[i] for i in mids] + [m for m in by_id.values() if m.id not in mids]:
            pos = m.text.find(cand)
            if pos >= 0:
                return m.id, cand, pos
    return None


def _arguments(items: Any, by_id: dict, side: str, errors: list[str]) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, list):
        errors.append(f"{side}: ожидался список доводов")
        return []
    out = []
    for a in items:
        if isinstance(a, str):
            a = {"text": a}
        if not isinstance(a, dict):
            errors.append(f"{side}: довод не разобран")
            continue
        text = str(a.get("text") or "").strip()
        if not text:
            errors.append(f"{side}: довод без текста пропущен")
            continue
        mids: list[str] = []
        for m in a.get("message_ids") or []:
            m = str(m)
            if m in by_id:
                if m not in mids:
                    mids.append(m)
            else:
                errors.append(f"{side}: реплики {m} нет в диалоге")
        quotes = []
        for q in a.get("quotes") or []:
            q = str(q).strip()
            if not q:
                continue
            hit = _find_quote(q, mids, by_id)
            if hit is None:
                errors.append(f"{side}: цитата не найдена в диалоге: «{q[:60]}»")
                quotes.append({"quote": q, "message_id": None, "start": None, "end": None, "valid": False})
                continue
            mid, exact, pos = hit
            text_m = by_id[mid].text
            quotes.append({"quote": exact, "message_id": mid, "start": _utf16(text_m, pos),
                           "end": _utf16(text_m, pos + len(exact)), "valid": True})
            if mid not in mids:
                mids.append(mid)
        out.append({"text": text, "message_ids": mids, "quotes": quotes})
    return out


def validate_dossier(data: Any, conv: Conversation, criterion: Criterion) -> tuple[dict, list[str]]:
    """Check the model's dossier against the transcript and the criterion; returns (dossier, problems).
    Quotes must be exact fragments of dialogue messages (UTF-16 start/end added); the recommended answer must fit
    the criterion type / N/A rule / scale steps; confidence is clamped to 0..1."""
    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append("Ответ модели не JSON-объект")
        data = {}
    by_id = {m.id: m for m in conv.messages}
    pro = _arguments(data.get("arguments_for", data.get("for")), by_id, "за апелляцию", errors)
    con = _arguments(data.get("arguments_against", data.get("against")), by_id, "за исходную оценку", errors)
    action = data.get("recommended_action")
    if action not in (None, "confirm", "change"):
        errors.append(f"рекомендация «{action}» не распознана")
        action = None
    answer = data.get("recommended_answer")
    if answer in ("", None):
        answer = None
    else:
        try:
            answer = criterion.check_answer(answer)
        except ValueError as exc:
            errors.append(f"рекомендованный ответ отклонён: {exc}")
            answer = None
    if action == "change" and answer is None:
        errors.append("рекомендация «изменить» без допустимого ответа")
        action = None
    conf = data.get("confidence")
    try:
        conf = None if conf is None else min(1.0, max(0.0, float(conf)))
    except (TypeError, ValueError):
        conf = None
    dossier = {"arguments_for": pro, "arguments_against": con, "recommended_action": action,
               "recommended_answer": answer, "confidence": conf,
               "recommendation": str(data.get("recommendation") or "").strip()}
    dossier["for"], dossier["against"] = dossier["arguments_for"], dossier["arguments_against"]
    return dossier, errors


def _rules_dossier(conv: Conversation, ev: Optional[CriterionEval], appeal: Appeal) -> dict:
    by_id = {m.id: m for m in conv.messages}
    pro = []
    for mid in appeal.message_ids:
        m = by_id.get(mid)
        if m:
            pro.append({"text": f"Оператор указывает на реплику ({m.speaker}): «{m.text[:200]}».", "message_ids": [mid],
                        "quotes": [{"quote": m.text[:200], "message_id": mid, "start": 0,
                                    "end": _utf16(m.text, min(len(m.text), 200)), "valid": True}]})
    if not pro:
        pro.append({"text": f"Комментарий оператора: {appeal.comment}", "message_ids": [], "quotes": []})
    evidence = ev.evidence if ev else []
    con = [{"text": f"Обоснование модели: {(ev.explanation if ev else '') or 'не указано'}",
            "message_ids": [e.message_id for e in evidence],
            "quotes": [{"quote": e.quote, "message_id": e.message_id, "start": e.start, "end": e.end, "valid": e.valid}
                       for e in evidence]}]
    d = {"arguments_for": pro, "arguments_against": con, "recommended_action": None, "recommended_answer": None,
         "confidence": None, "source": "rules",
         "recommendation": "Досье без модели: сверьте отмеченные реплики с обоснованием. Решение принимает проверяющий."}
    d["for"], d["against"] = d["arguments_for"], d["arguments_against"]
    return d


# ----------------------------------------------------------------------------------------------- service

class ReviewService:
    def __init__(self, db: Optional[Database] = None, store: Optional[ChecklistStore] = None, *,
                 llm_call: Optional[LLMCall] = None, eval_modes: Optional[list[str]] = None, prompt_version=None):
        self.db = db or get_db()
        self.store = store or ChecklistStore(self.db)
        self.llm_call = llm_call
        self.eval_modes = eval_modes
        self.prompt_version = prompt_version

    # -- state
    def current_evals(self, conv_id: str, version: Optional[ChecklistVersion] = None) -> list[CriterionEval]:
        return current_evals(self.db, conv_id, version or self.store.active(), self.eval_modes, self.prompt_version)

    def conversation_score(self, conv_id: str, version: Optional[ChecklistVersion] = None) -> Score:
        version = version or self.store.active()
        return conversation_score(self.db, conv_id, version, self.current_evals(conv_id, version))

    def _appeal(self, appeal_id: str) -> Appeal:
        appeal = self.db.get_appeal(appeal_id)
        if appeal is None:
            raise ReviewError("Апелляция не найдена.", 404)
        return appeal

    def _version_of(self, appeal: Appeal) -> ChecklistVersion:
        try:
            return self.store.get(appeal.checklist_version) if appeal.checklist_version else self.store.active()
        except KeyError:
            raise ReviewError("Закреплённая версия чек-листа не найдена.", 409) from None

    def _eval_of(self, appeal: Appeal) -> Optional[CriterionEval]:
        snapshot = getattr(appeal, "evaluation", None)
        if snapshot:
            return CriterionEval.model_validate({"conv_id": appeal.conv_id, "criterion_id": appeal.criterion_id,
                                                  "criterion_hash": appeal.criterion_hash, **snapshot})
        if not appeal.criterion_hash:
            return None
        found = self.db.latest_evals(appeal.conv_id, [appeal.criterion_hash])
        return found.get(appeal.criterion_hash)

    # -- 05: appeal
    def create_appeal(self, conv_id: str, criterion_id: str, comment: str, message_ids: Iterable[str] = (), *,
                      author: Optional[str] = None, version: Optional[int] = None) -> Appeal:
        try:
            data = AppealCreate(conv_id=conv_id, criterion_id=criterion_id, comment=comment or "",
                                message_ids=list(dict.fromkeys(message_ids or [])))
        except ValidationError:
            raise ReviewError("Напишите комментарий к апелляции и выберите критерий.", 422) from None
        conv = self.db.get_conversation(data.conv_id)
        if conv is None:
            raise ReviewError("Диалог не найден.", 404)
        try:
            version = self.store.get(version) if version is not None else self.store.active()
        except KeyError:
            raise ReviewError("Версия чек-листа не найдена.", 404) from None
        try:
            criterion = version.criterion(data.criterion_id)
        except KeyError:
            raise ReviewError(f"Критерий {data.criterion_id} не найден в активной версии чек-листа.", 422) from None
        known = {m.id for m in conv.messages}
        unknown = [m for m in data.message_ids if m not in known]
        if unknown:
            raise ReviewError(f"Реплики не из этого диалога: {', '.join(unknown[:3])}", 422)
        evals = self.current_evals(data.conv_id, version)
        ev = next((e for e in evals if e.criterion_id == criterion.id), None)
        if ev is None:
            raise ReviewError("Критерий ещё не оценён — оспаривать нечего.", 409)
        if any(a.criterion_hash == criterion.hash for a in self.db.list_appeals(status="open", conv_id=data.conv_id)):
            raise ReviewError("По этому критерию уже есть открытая апелляция.", 409)
        score = conversation_score(self.db, data.conv_id, version, evals)
        line = next((ln for ln in score.lines if ln.criterion_id == criterion.id), None)
        appeal = Appeal(**data.model_dump(), id=_new_id("appeal"), status="open", created_at=now_iso(),
                        checklist_version=version.version, criterion_hash=criterion.hash,
                        evaluation_id=f"{ev.conv_id}:{ev.criterion_hash}:{ev.prompt_version}:{ev.mode}",
                        original_answer=ev.answer, current_answer=line.answer if line else ev.answer,
                        total_at_appeal=score.total, criterion_title=criterion.title, author=author,
                        evaluation=ev.model_dump(mode="json"))
        self.db.create_appeal(appeal)
        self.db.add_audit("appeal", appeal.id, "appeal.created",
                          {"criterion_id": criterion.id, "comment": appeal.comment, "message_ids": appeal.message_ids,
                           "original_answer": ev.answer}, conv_id=appeal.conv_id)
        return appeal

    # -- dossier
    def dossier_prompt(self, appeal_id: str) -> tuple[str, str]:
        appeal = self._appeal(appeal_id)
        conv = self.db.get_conversation(appeal.conv_id)
        criterion = self._version_of(appeal).criterion(appeal.criterion_id)
        return build_dossier_prompt(conv, criterion, self._eval_of(appeal), appeal,
                                    current_answer=getattr(appeal, "current_answer", None))

    def build_dossier(self, appeal_id: str, llm_call: Optional[LLMCall] = None) -> dict:
        appeal = self._appeal(appeal_id)
        conv = self.db.get_conversation(appeal.conv_id)
        criterion = self._version_of(appeal).criterion(appeal.criterion_id)
        ev = self._eval_of(appeal)
        call = llm_call or self.llm_call
        if call is None:
            dossier = _rules_dossier(conv, ev, appeal)
        else:
            try:
                system, user = build_dossier_prompt(conv, criterion, ev, appeal,
                                                    current_answer=getattr(appeal, "current_answer", None))
                data, usage = call(system, user)
                usage = usage if isinstance(usage, dict) else {}
                dossier, problems = validate_dossier(data, conv, criterion)
                dossier.update(source="llm", warnings=problems, model=usage.get("model"),
                               provider=usage.get("provider"), cost_kzt=usage.get("cost_kzt"),
                               latency_ms=usage.get("latency_ms"), raw=data)
            except Exception as exc:
                dossier = _rules_dossier(conv, ev, appeal)
                dossier["error"] = f"Модель недоступна ({type(exc).__name__}: {str(exc)[:120]}); показано досье по правилам."
        dossier.update(appeal_id=appeal.id, status="ready", prompt_version=DOSSIER_PROMPT_VERSION,
                       created_at=now_iso(), criterion_id=appeal.criterion_id, conv_id=appeal.conv_id)
        saved = self.db.put_dossier(appeal.id, dossier)
        self.db.add_audit("appeal", appeal.id, "dossier.ready",
                          {"source": dossier["source"], "recommended_action": dossier.get("recommended_action")},
                          conv_id=appeal.conv_id)
        return saved

    def build_dossier_async(self, appeal_id: str, llm_call: Optional[LLMCall] = None) -> dict:
        """Store a pending placeholder and build the dossier in a background thread."""
        self._appeal(appeal_id)
        pending = self.db.put_dossier(appeal_id, {"status": "pending", "arguments_for": [], "arguments_against": [],
                                                  "for": [], "against": [], "recommendation": "Досье готовится…"})

        def job() -> None:
            try:
                self.build_dossier(appeal_id, llm_call)
            except Exception as exc:  # pragma: no cover - the UI shows the error state
                self.db.put_dossier(appeal_id, {"status": "error", "error": str(exc)[:300], "arguments_for": [],
                                                "arguments_against": [], "for": [], "against": [],
                                                "recommendation": "Досье не получено. Проверьте диалог вручную."})
        threading.Thread(target=job, name=f"cqa-dossier-{appeal_id}", daemon=True).start()
        return pending

    def get_dossier(self, appeal_id: str) -> Optional[dict]:
        return self.db.get_dossier(appeal_id)

    # -- 06: decision
    def decide(self, appeal_id: str, action: str, new_answer: Any = None, reason: str = "", *,
               reviewer: Optional[str] = None) -> DecisionResult:
        try:
            data = DecisionCreate(appeal_id=appeal_id, action=action, new_answer=new_answer, reason=reason or "")
        except ValidationError as exc:
            text = str(exc)
            if "reason" in text:
                msg = "Укажите причину решения."
            elif "новый ответ" in text:
                msg = "Для решения «изменить» укажите новый ответ."
            else:
                msg = f"Некорректное решение: {exc.errors()[0].get('msg', '')}"
            raise ReviewError(msg, 422) from None
        appeal = self._appeal(data.appeal_id)
        if appeal.status != "open":
            raise ReviewError("Решение по этой апелляции уже сохранено.", 409)
        active = self.store.active()
        criterion = self._version_of(appeal).criterion(appeal.criterion_id)
        before = self.conversation_score(appeal.conv_id, active)
        warnings: list[str] = []
        # The reviewer saw the appeal snapshot, even if a newer evaluation now
        # supplies the live total. Zero is a valid pinned answer.
        current = getattr(appeal, "current_answer", None)
        if current is None:
            current = appeal.original_answer
        same_wording = any(c.hash == appeal.criterion_hash for c in active.criteria)
        if not same_wording:
            warnings.append(f"{criterion.id} изменён в активной версии чек-листа — решение относится к прежней "
                            f"формулировке и не меняет текущий итог.")
        answer = current if data.action == "confirm" else data.new_answer
        if answer is None:
            raise ReviewError("Нет ответа для подтверждения: критерий не оценён.", 409)
        try:
            answer = criterion.check_answer(answer)
        except ValueError as exc:
            raise ReviewError(str(exc), 422) from None
        decision = Decision(id=_new_id("decision"), appeal_id=appeal.id, action=data.action, new_answer=answer,
                            reason=data.reason, conv_id=appeal.conv_id, criterion_id=criterion.id,
                            criterion_hash=appeal.criterion_hash, old_answer=current, created_at=now_iso(),
                            reviewer=reviewer, checklist_version=appeal.checklist_version)
        # PROD-02: compare-and-set in ONE transaction (appeal still open -> decision + close + audit). A parallel
        # reviewer or a retried request that lost the race gets 409 and nothing is written for it.
        try:
            appeal, decision = self.db.decide_appeal(decision, audit={
                "appeal_id": appeal.id, "criterion_id": criterion.id, "action": data.action,
                "reason": data.reason, "old_answer": current, "new_answer": answer})
        except AppealAlreadyDecidedError as exc:
            raise ReviewError(str(exc), 409) from None
        after = self.conversation_score(appeal.conv_id, active)
        self.db.add_audit("decision", decision.id, "score.recalculated",
                          {"appeal_id": appeal.id, "old_total": before.total, "new_total": after.total,
                           "version": active.version}, conv_id=appeal.conv_id)
        return DecisionResult(old_total=before.total, new_total=after.total, decision=decision, score=after,
                              conv_id=appeal.conv_id, appeal=appeal.model_dump(mode="json"), warnings=warnings)

    # -- card journal
    def history(self, appeal_id: str) -> list[dict]:
        """evaluation → appeal → decision → recalculation for one appeal (from stored records)."""
        appeal = self._appeal(appeal_id)
        snap = getattr(appeal, "evaluation", None) or {}
        steps = [{"step": "evaluation", "title": "Оценка ИИ", "criterion_id": appeal.criterion_id,
                  "answer": appeal.original_answer, "model": snap.get("model"),
                  "explanation": snap.get("explanation"), "ts": snap.get("created_at")},
                 {"step": "appeal", "title": "Апелляция", "comment": appeal.comment,
                  "message_ids": appeal.message_ids, "ts": appeal.created_at}]
        mine = [d for d in self.db.list_decisions(conv_id=appeal.conv_id) if d.appeal_id == appeal.id]
        # The decision the appeal points to is the one the engine applies (legacy DBs may hold a duplicate).
        pinned = getattr(appeal, "decision_id", None)
        decision = next((d for d in mine if d.id == pinned), mine[-1] if mine else None)
        if decision is not None:
            steps.append({"step": "decision", "title": "Решение проверяющего", "action": decision.action,
                          "old_answer": decision.old_answer, "new_answer": decision.new_answer,
                          "reason": decision.reason, "reviewer": decision.reviewer, "ts": decision.created_at})
            recalc = [e for e in self.db.list_audit(entity="decision", entity_id=decision.id)
                      if e.event == "score.recalculated"]
            if recalc:
                p = recalc[-1].payload
                steps.append({"step": "recalculation", "title": "Пересчёт итога", "old_total": p.get("old_total"),
                              "new_total": p.get("new_total"), "ts": recalc[-1].ts})
        return steps
