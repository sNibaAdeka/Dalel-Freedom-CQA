"""cqa/review.py — Student Guide 05 (appeal) and 06 (decision): appeal → decision → recalculation, dossier."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cqa.checklist_store import ChecklistStore
from cqa.db import Database
from cqa.review import DOSSIER_SYSTEM, ReviewError, ReviewService, validate_dossier
from cqa.runs import RunManager
from tests.conftest import PDF_EXAMPLE, FakeEvaluator, make_evals, synth_conv


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    store = ChecklistStore(db)
    v1 = store.active()
    conv = synth_conv("t-001")
    db.upsert_conversation(conv)
    db.save_evals(make_evals(v1, PDF_EXAMPLE, conv_id="t-001", mode="fast"))
    return SimpleNamespace(db=db, store=store, v1=v1, conv=conv, svc=ReviewService(db, store))


def _status(exc_info) -> int:
    return exc_info.value.status


def test_dossier_retains_appealed_evaluation_after_rerun(env):
    a = env.svc.create_appeal('t-001', 'C4', 'Проверьте прежнюю оценку')
    ev = env.svc._eval_of(a)
    assert ev.answer == 15
    env.db.save_eval(ev.model_copy(update={'answer':20.0, 'explanation':'Новое обоснование после прогона'}))
    assert env.svc._eval_of(a).answer == 15
    _, user = env.svc.dossier_prompt(a.id)
    assert 'Новое обоснование после прогона' not in user


def test_unknown_pinned_version_does_not_fall_back(env):
    with pytest.raises(ReviewError) as exc:
        env.svc.create_appeal('t-001','C4','Проверка',version=999)
    assert exc.value.status == 404


# ------------------------------------------------------------------ full cycle

def test_appeal_decision_recalculation_cycle(env):
    a = env.svc.create_appeal("t-001", "C4", "Оператор закрыл все вопросы клиента", ["t-001:3"])
    assert a.status == "open" and a.checklist_version == 1 and a.original_answer == 15
    assert a.criterion_hash == env.v1.criterion("C4").hash
    res = env.svc.decide(a.id, "change", 20, "Все вопросы закрыты: реплика [3]")
    assert (res.old_total, res.new_total) == (85.38, 93.08) and res.score.total == 93.08
    d = res.decision
    assert (d.criterion_id, d.criterion_hash, d.conv_id) == ("C4", a.criterion_hash, "t-001")
    assert (d.old_answer, d.new_answer, d.action) == (15, 20, "change")
    h = env.v1.criterion("C4").hash
    assert env.db.latest_evals("t-001", [h])[h].answer == 15          # the model's evaluation is untouched
    assert env.db.get_appeal(a.id).status == "decided"
    assert env.svc.conversation_score("t-001").total == 93.08          # the decision persists
    events = [e.event for e in env.db.list_audit(conv_id="t-001")]
    assert events == ["appeal.created", "decision.saved", "score.recalculated"]
    steps = env.svc.history(a.id)
    assert [s["step"] for s in steps] == ["evaluation", "appeal", "decision", "recalculation"]
    assert steps[0]["answer"] == 15 and steps[2]["new_answer"] == 20 and steps[3]["new_total"] == 93.08


def test_card_journal_has_four_events_after_a_run(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    store = ChecklistStore(db)
    db.upsert_conversation(synth_conv("t-001"))
    RunManager(db, store, evaluate=FakeEvaluator(), workers=2).start_run(wait=True)
    svc = ReviewService(db, store)
    a = svc.create_appeal("t-001", "C5", "Оператор был вежлив", [])
    svc.decide(a.id, "change", "no", "Шаблонный ответ без реакции на проблему")
    events = [e.event for e in db.list_audit(conv_id="t-001")]
    assert events == ["evaluation.created", "appeal.created", "decision.saved", "score.recalculated"]


def test_confirm_keeps_answer_and_total(env):
    a = env.svc.create_appeal("t-001", "C5", "Прошу проверить вежливость", [])
    res = env.svc.decide(a.id, "confirm", None, "Оценка верна")
    assert res.old_total == res.new_total == 85.38
    assert res.decision.old_answer == res.decision.new_answer == "yes"


@pytest.mark.parametrize("criterion_id,original,later", [("C5", "no", "yes"), ("C4", 15.0, 20.0)])
def test_confirm_uses_pinned_answer_after_same_hash_rerun(env, criterion_id, original, later):
    ev = next(e for e in env.svc.current_evals("t-001") if e.criterion_id == criterion_id)
    env.db.save_eval(ev.model_copy(update={"answer": original}))
    original_total = env.svc.conversation_score("t-001").total
    appeal = env.svc.create_appeal("t-001", criterion_id, "Проверить закреплённый ответ")
    env.db.save_eval(ev.model_copy(update={"answer": later}))
    current_total = env.svc.conversation_score("t-001").total
    assert current_total != original_total

    result = env.svc.decide(appeal.id, "confirm", reason="Подтверждаю ответ из досье")

    assert result.decision.old_answer == result.decision.new_answer == original
    assert (result.old_total, result.new_total) == (current_total, original_total)
    assert env.svc._eval_of(appeal).answer == original
    assert env.db.latest_evals("t-001", [ev.criterion_hash])[ev.criterion_hash].answer == later
    recalc = env.db.list_audit(entity="decision", entity_id=result.decision.id)[-1]
    assert recalc.payload["old_total"] == current_total and recalc.payload["new_total"] == original_total


def test_change_records_pinned_old_answer_but_recalculates_current_total(env):
    appeal = env.svc.create_appeal("t-001", "C4", "Проверить исходную оценку")
    ev = env.svc._eval_of(appeal)
    env.db.save_eval(ev.model_copy(update={"answer": 20.0}))
    result = env.svc.decide(appeal.id, "change", 10, "Существенный вопрос остался открытым")
    assert (result.decision.old_answer, result.decision.new_answer) == (15, 10)
    assert (result.old_total, result.new_total) == (93.08, 77.69)


def test_confirm_preserves_pinned_zero_after_wording_changes(env):
    first = env.svc.create_appeal("t-001", "C4", "Первый пересмотр")
    env.svc.decide(first.id, "change", 0, "Ответ по существу отсутствовал")
    appeal = env.svc.create_appeal("t-001", "C4", "Проверить нулевую оценку")
    assert appeal.current_answer == 0 and appeal.original_answer == 15
    v2 = env.store.apply([{"op": "update", "criterion_id": "C4",
                           "fields": {"description": "Новая синтетическая формулировка."}}]).checklist
    env.store.activate(v2.version)
    env.db.save_evals(make_evals(v2, {**PDF_EXAMPLE, "C4": 20}, conv_id="t-001", mode="fast"))
    before = env.svc.conversation_score("t-001").total

    result = env.svc.decide(appeal.id, "confirm", reason="Ноль в закреплённой оценке верен")

    assert result.decision.old_answer == result.decision.new_answer == 0
    assert result.old_total == result.new_total == before == 93.08
    assert result.warnings and result.decision.criterion_hash == appeal.criterion_hash


def test_confirm_falls_back_to_original_only_when_pinned_current_is_none(env):
    appeal = env.svc.create_appeal("t-001", "C4", "Старая апелляция без current_answer")
    env.db.update_appeal(appeal.id, current_answer=None)
    env.db.save_eval(env.svc._eval_of(appeal).model_copy(update={"answer": 20.0}))
    result = env.svc.decide(appeal.id, "confirm", reason="Подтверждаю исходные 15")
    assert result.decision.old_answer == result.decision.new_answer == 15
    assert (result.old_total, result.new_total) == (93.08, 85.38)


@pytest.mark.parametrize("action,new_answer,expected,total", [("confirm", None, 15, 85.38), ("change", 10, 10, 77.69)])
def test_decision_preserves_pinned_identity_after_api_rename(env, action, new_answer, expected, total):
    appeal = env.svc.create_appeal("t-001", "C4", "Проверить оценку до смены ID")
    v2 = env.store.apply([{"op": "update", "criterion_id": "C4", "fields": {"id": "C4-renamed"}}]).checklist
    env.store.activate(v2.version)
    renamed = v2.criterion("C4-renamed")
    assert renamed.hash == appeal.criterion_hash
    env.db.save_eval(env.svc._eval_of(appeal).model_copy(update={"criterion_id": renamed.id, "answer": 20.0}))

    result = env.svc.decide(appeal.id, action, new_answer, "Решение по закреплённому ответу")

    assert result.decision.old_answer == 15 and result.decision.new_answer == expected
    assert result.decision.criterion_id == appeal.criterion_id
    assert result.decision.checklist_version == appeal.checklist_version
    pinned_score = env.svc.conversation_score("t-001", env.v1)
    line = next(line for line in pinned_score.lines if line.criterion_id == appeal.criterion_id)
    assert line.source == "decision" and line.answer == expected and line.original_answer == 20
    assert pinned_score.total == total and result.old_total == 93.08
    stored = env.db.get_appeal(appeal.id)
    assert stored.criterion_id == "C4" and stored.checklist_version == 1
    assert env.svc._eval_of(stored).answer == 15


def test_second_appeal_starts_from_previous_decision(env):
    a1 = env.svc.create_appeal("t-001", "C4", "Всё закрыто", [])
    env.svc.decide(a1.id, "change", 20, "Да, закрыто")
    a2 = env.svc.create_appeal("t-001", "C4", "Нет, второй вопрос открыт", [])
    res = env.svc.decide(a2.id, "change", 10, "Второй вопрос остался открытым")
    assert res.decision.old_answer == 20 and res.old_total == 93.08 and res.new_total == 77.69  # 50.5 / 65


def test_decision_can_raise_gross_error_flag(env):
    a = env.svc.create_appeal("t-001", "C8", "Оператор раскрыл чужие данные", ["t-001:5"])
    res = env.svc.decide(a.id, "change", "yes", "Подтверждено: раскрытие данных")
    assert res.score.critical is True and res.new_total == res.old_total == 85.38


# ------------------------------------------------------------------ validation

def test_create_appeal_validation(env):
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("t-001", "C4", "   ", [])
    assert _status(e) == 422
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("missing", "C4", "текст", [])
    assert _status(e) == 404
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("t-001", "C42", "текст", [])
    assert _status(e) == 422
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("t-001", "C4", "текст", ["t-002:3"])
    assert _status(e) == 422 and "t-002:3" in str(e.value)
    env.db.upsert_conversation(synth_conv("t-009"))
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("t-009", "C4", "текст", [])
    assert _status(e) == 409                                   # nothing evaluated yet
    env.svc.create_appeal("t-001", "C4", "первая", ["t-001:3", "t-001:3"])
    with pytest.raises(ReviewError) as e:
        env.svc.create_appeal("t-001", "C4", "вторая", [])
    assert _status(e) == 409                                   # one open appeal per criterion


@pytest.mark.parametrize("cid,action,answer,reason,status", [
    ("C4", "change", 20, "  ", 422),        # reason is mandatory
    ("C4", "change", None, "причина", 422),  # change needs a new answer
    ("C5", "change", 7, "причина", 422),     # not a binary answer
    ("C1", "change", "na", "причина", 422),  # N/A not allowed for C1
    ("C6", "change", 5, "причина", 422),     # 5 is not a step of 0/4/7/10
])
def test_decide_validation(env, cid, action, answer, reason, status):
    a = env.svc.create_appeal("t-001", cid, "оспариваю", [])
    with pytest.raises(ReviewError) as e:
        env.svc.decide(a.id, action, answer, reason)
    assert _status(e) == status
    assert env.db.get_appeal(a.id).status == "open" and env.db.list_decisions(conv_id="t-001") == []


def test_decide_unknown_or_decided_appeal(env):
    with pytest.raises(ReviewError) as e:
        env.svc.decide("nope", "confirm", None, "причина")
    assert _status(e) == 404
    a = env.svc.create_appeal("t-001", "C2", "оспариваю N/A", [])
    env.svc.decide(a.id, "change", "yes", "Оператор уточнил детали")
    with pytest.raises(ReviewError) as e:
        env.svc.decide(a.id, "confirm", None, "повтор")
    assert _status(e) == 409


# ------------------------------------------------------------------ dossier

def test_dossier_prompt_has_criterion_evaluation_appeal_and_transcript(env):
    a = env.svc.create_appeal("t-001", "C4", "Клиенту всё объяснили", ["t-001:3"])
    system, user = env.svc.dossier_prompt(a.id)
    assert system == DOSSIER_SYSTEM and "arguments_for" in system and "JSON" in system
    for fragment in ("Корректная и полная консультация", "20 — Все существенные вопросы закрыты",
                     "N/A — чат закончился", "ответ: 15", "Клиенту всё объяснили", "t-001:3",
                     "[t-001:1] bot:", "[t-001:6] system:", "[t-001:3] operator: Здравствуйте! Меня зовут Алия."):
        assert fragment in user, fragment


def test_validate_dossier(env):
    c4 = env.v1.criterion("C4")
    quote = "Учебный тариф виден в разделе «Тарифы»"
    data = {"arguments_for": [{"text": "Оператор назвал раздел", "message_ids": ["t-001:3", "t-001:99"],
                               "quotes": [quote, "выдуманная цитата"]}, {"text": "  "}],
            "arguments_against": ["Клиенту не объяснили стоимость"],
            "recommended_action": "change", "recommended_answer": "20", "confidence": 1.7,
            "recommendation": "Изменить на 20"}
    d, errors = validate_dossier(data, env.conv, c4)
    arg = d["arguments_for"][0]
    text3 = env.conv.messages[3].text
    assert len(d["arguments_for"]) == 1 and arg["message_ids"] == ["t-001:3"]
    q = arg["quotes"][0]
    assert q["valid"] and q["message_id"] == "t-001:3" and text3[q["start"]:q["end"]] == quote
    assert arg["quotes"][1]["valid"] is False
    assert d["arguments_against"][0]["text"] == "Клиенту не объяснили стоимость"
    assert (d["recommended_action"], d["recommended_answer"], d["confidence"]) == ("change", 20, 1.0)
    assert d["for"] == d["arguments_for"] and d["against"] == d["arguments_against"]
    assert any("t-001:99" in e for e in errors) and any("цитата" in e for e in errors)
    bad, errs = validate_dossier({"recommended_action": "change", "recommended_answer": "na"}, env.conv,
                                 env.v1.criterion("C1"))
    assert bad["recommended_answer"] is None and bad["recommended_action"] is None
    assert any("N/A не разрешён" in e for e in errs)
    junk, errs = validate_dossier("не JSON", env.conv, c4)
    assert junk["arguments_for"] == [] and errs


def test_build_dossier_with_injected_llm(env):
    a = env.svc.create_appeal("t-001", "C4", "Клиенту всё объяснили", ["t-001:3"])
    calls = []

    def llm(system, user):
        calls.append((system, user))
        return ({"arguments_for": [{"text": "Раздел назван", "message_ids": ["t-001:3"],
                                    "quotes": ["Учебный тариф виден в разделе «Тарифы»"]}],
                 "arguments_against": [{"text": "Стоимость не названа", "message_ids": [], "quotes": []}],
                 "recommended_action": "change", "recommended_answer": 20, "confidence": 0.7,
                 "recommendation": "Изменить оценку на 20"},
                {"provider": "mock", "model": "fake", "cost_kzt": 1.5, "latency_ms": 10})

    d = env.svc.build_dossier(a.id, llm_call=llm)
    assert len(calls) == 1 and d["source"] == "llm" and d["status"] == "ready" and d["appeal_id"] == a.id
    assert d["recommended_answer"] == 20 and d["model"] == "fake" and d["cost_kzt"] == 1.5
    assert env.db.get_dossier(a.id)["recommended_action"] == "change"
    assert "dossier.ready" in [e.event for e in env.db.list_audit(conv_id="t-001")]


def test_build_dossier_falls_back_to_rules(env):
    a = env.svc.create_appeal("t-001", "C4", "Клиенту всё объяснили", ["t-001:3"])
    d = env.svc.build_dossier(a.id)                                  # no LLM wired
    assert d["source"] == "rules" and d["arguments_for"][0]["message_ids"] == ["t-001:3"]
    assert "C4: тестовое" not in str(d)  # make_evals has no explanation; the model side is still present
    assert d["arguments_against"] and d["recommendation"]

    def broken(system, user):
        raise TimeoutError("нет ответа")

    d2 = env.svc.build_dossier(a.id, llm_call=broken)
    assert d2["source"] == "rules" and "TimeoutError" in d2["error"]
