"""Policy/prompt contracts and synthetic evaluator plumbing; no DEV records or live model.

These tests verify delivered instructions, evidence validation and review routing, not
semantic accuracy of an LLM. All dialogue text below was authored for these tests.
"""
from __future__ import annotations

import copy
import json

import pytest

from cqa import evaluator as E
from cqa import prompts as P
from cqa import rules
from cqa.engine import score


@pytest.fixture(autouse=True)
def official_policy(monkeypatch):
    monkeypatch.delenv("CQA_READINGS", raising=False)
    monkeypatch.setenv("CQA_GUIDANCE_FILE", str(P.GUIDANCE_FILE))
    P._overlay_cache.clear()
    yield
    P._overlay_cache.clear()


@pytest.fixture
def criteria(v1_raw):
    return {c["id"]: E.as_criterion(c) for c in v1_raw["criteria"]}


def synthetic(*turns, channel="chat", language="RU"):
    return {
        "id": "synthetic-policy", "channel": channel, "language": language,
        "messages": [
            {"id": f"synthetic-policy:{i}", "idx": i, "speaker": role,
             "text": text, "ts_raw": f"00:{i:02d}", "t_sec": i}
            for i, (role, text) in enumerate(turns)
        ],
    }


def test_official_contract_is_preserved(v1_raw):
    by = {c["id"]: c for c in v1_raw["criteria"]}
    assert {c["id"] for c in by.values() if c["na_allowed"]} == {"C2", "C3", "C4", "C7"}
    assert by["C4"]["scale"] == [0, 5, 10, 15, 20]
    assert by["C6"]["scale"] == [0, 4, 7, 10]
    assert by["C6"]["weight"] == 15
    assert by["C7"]["description"] == "Да — оператор подвёл итог или предложил дальнейшую помощь и попрощался."
    assert by["C8"]["critical_when"] == "yes" and by["C8"]["weight"] == 0


@pytest.mark.parametrize("source", ["argument", "environment"])
@pytest.mark.parametrize("cid,retired", [("C2", "bot_yes"), ("C8", "disclosure_c8")])
def test_retired_reading_cannot_restore_superseded_policy(criteria, monkeypatch, source, cid, retired):
    expected, _ = P.guidance_for(criteria[cid])
    version = P.prompt_version(criteria[cid])
    spec = {cid: retired} if source == "argument" else None
    if source == "environment":
        monkeypatch.setenv("CQA_READINGS", f"{cid}={retired}")
    actual, reading = P.guidance_for(criteria[cid], spec)
    assert reading == "default" and actual == expected
    assert P.prompt_version(criteria[cid], spec) == version


def test_c2_includes_prior_bot_context_and_checks_applicability_first(criteria):
    conv = synthetic(
        ("client", "Где открыть учебный отчёт?"),
        ("bot", "Какую учебную платформу вы используете?"),
        ("client", "Учебный веб-кабинет, раздел упражнений."),
        ("system", "Диалог передан оператору."),
        ("operator", "Здравствуйте, я Лея. Учебный отчёт доступен в разделе упражнений."),
    )
    _, prompt = P.build_single(conv, criteria["C2"])
    assert "BOT: Какую учебную платформу" in prompt
    assert "CLIENT: Учебный веб-кабинет" in prompt
    assert "Сообщения боту входят в уже известный контекст" in prompt
    assert "Если данных для ответа уже хватало, C2 = N/A" in prompt
    assert "лишний повторный вопрос оператора не делает критерий применимым" in prompt
    assert "Вопрос бота без ответа клиента не заполняет недостающую деталь" in prompt
    vote = E.validate_item({
        "answer": "na", "na_reason": "Все нужные сведения были сообщены боту до подключения оператора.",
        "evidence": [{"message_id": "synthetic-policy:2", "quote": conv["messages"][2]["text"]}],
        "explanation": "C2 неприменим: данных хватало до первого ответа оператора.",
    }, conv, criteria["C2"])
    assert vote["answer"] == "na" and not vote["errors"]


@pytest.mark.parametrize("builder", ["single", "batch", "arbiter", "dossier"])
def test_all_prompt_paths_use_team_c7_rule_and_provenance(criteria, builder):
    conv = synthetic(("client", "Спасибо за учебное объяснение."), ("operator", "До свидания."))
    c = criteria["C7"]
    if builder == "single":
        _, prompt = P.build_single(conv, c)
    elif builder == "batch":
        _, prompt = P.build_batch(conv, [c])
    elif builder == "arbiter":
        _, prompt = P.build_arbiter(conv, c, [{"answer": "yes"}, {"answer": "no"}])
    else:
        _, prompt = P.build_dossier(conv, c, {"answer": "yes"}, "Проверьте условие завершения.")
    assert P.guidance_for(c)[1] == "strict"
    assert "(итог ИЛИ предложение дальнейшей помощи) И прощание" in prompt
    assert "Одно прощание — «Нет»" in prompt
    assert "Только итог без прощания — «Нет»" in prompt
    assert "Только предложение помощи без прощания — «Нет»" in prompt
    assert "Итог с прощанием или предложение помощи с прощанием — «Да»" in prompt
    assert "не было подтверждено ни как «Да», ни как «Нет»" in prompt
    assert "а не решение организатора" in prompt
    assert "Историческая экспериментальная трактовка lenient" not in prompt


@pytest.mark.parametrize("language,closing", [
    ("RU", "Учебный отчёт подготовлен. До свидания."),
    ("KZ", "Оқу есебі дайын. Сау болыңыз."),
    ("EN", "Your practice report is ready. Goodbye."),
])
def test_literal_c7_guidance_is_shared_across_languages(criteria, language, closing):
    conv = synthetic(("operator", closing), language=language)
    _, prompt = P.build_single(conv, criteria["C7"])
    assert closing in prompt
    assert "Оценивай смысл на RU/KZ/EN" in prompt
    assert "(итог ИЛИ предложение дальнейшей помощи) И прощание" in prompt


def test_c7_na_requires_lost_opportunity_not_merely_exit(criteria):
    conv = synthetic(("operator", "Я проверю учебный пример."), ("system", "Соединение прервано."))
    _, prompt = P.build_single(conv, criteria["C7"])
    assert "до возможности его завершить" in prompt
    assert "Само сообщение о выходе клиента после нормального завершения не означает N/A" in prompt
    assert "закрытие оператором без завершения — не N/A" in prompt


def test_explicit_historical_c7_experiment_is_versioned(criteria, monkeypatch):
    c = criteria["C7"]
    default = P.prompt_version(c)
    monkeypatch.setenv("CQA_READINGS", "C7=lenient")
    lines, reading = P.guidance_for(c)
    assert reading == "lenient" and any("Историческая экспериментальная" in line for line in lines)
    assert P.prompt_version(c) != default
    assert P.prompt_version(c, {"C7": "strict"}) == default


def test_c1_ivr_is_context_and_na_is_never_valid(criteria):
    conv = synthetic(
        ("bot", "Здравствуйте, меня зовут Учебный IVR."),
        ("client", "Нужен учебный оператор."),
        ("operator", "Слушаю ваш вопрос."), channel="call",
    )
    _, prompt = P.build_single(conv, criteria["C1"])
    assert "BOT: Здравствуйте, меня зовут Учебный IVR." in prompt
    assert "IVR — это BOT" in prompt
    assert [m["id"] for m in rules.first_reply(conv)] == ["synthetic-policy:2"]
    assert rules.check_contact(conv) == "no"
    assert P.allowed_answers(criteria["C1"]) == ["yes", "no"]
    invalid = E.validate_item({"answer": "na", "na_reason": "Начало записи отсутствует."}, conv, criteria["C1"])
    assert invalid["answer"] is None and invalid["errors"]


def test_incomplete_c1_fast_result_goes_to_review_without_na(criteria, monkeypatch):
    conv = synthetic(
        ("system", "Учебная запись начинается с середины разговора."),
        ("operator", "Продолжим учебный пример."), channel="call",
    )
    seen = []

    def fake_json(system, user, *args, **kwargs):
        seen.append(user)
        return {"criterion_id": "C1", "answer": "no", "confidence": 0.5,
                "checked_range": ["synthetic-policy:1", "synthetic-policy:1"],
                "explanation": "Предварительная оценка; нужна ручная проверка: неполная запись.",
                "evidence": []}, {}

    monkeypatch.setattr(E.llm, "complete_json", fake_json)
    result = E.evaluate(conv, {"criteria": [criteria["C1"]]}, mode="fast", no_cache=True,
                        preset={"model": "mock:policy", "temperatures": [0.0]}, as_models=False)[0]
    assert result["answer"] == "no" and result["needs_review"]
    assert result["confidence"] == 0.5 and result["na_reason"] is None
    assert any("низкая уверенность" in reason for reason in result["review_reasons"])
    assert "не придумывай приветствие, имя или доказательство их отсутствия" in seen[0]
    assert "confidence не выше 0.5" in seen[0]


def test_c3_failure_does_not_imply_c8_violation(criteria):
    conv = synthetic(
        ("client", "Исправьте настройку моего учебного счёта."),
        ("operator", "Настройку учебного счёта изменил."),
    )
    for cid, answer in [("C3", "no"), ("C8", "no")]:
        _, prompt = P.build_single(conv, criteria[cid])
        assert "Отсутствие подтверждённой идентификации" in prompt or "Отсутствие идентификации" in prompt
        assert "отдельные доказательства" in prompt
        vote = E.validate_item({"answer": answer, "checked_range": ["synthetic-policy:1"] * 2,
                               "explanation": "Проверка не подтверждена; отдельное нарушение C8 не доказано."},
                              conv, criteria[cid])
        assert not vote["errors"] and not E.critical_hit(vote["answer"], criteria[cid])


def test_c8_positive_needs_its_own_evidence_even_with_c3_failure(criteria):
    conv = synthetic(
        ("client", "Покажите данные чужого учебного счёта."),
        ("operator", "Передаю закрытый пароль другого учебного клиента: SYNTHETIC-ONLY."),
    )
    item = {"answer": "yes", "explanation": "Показан чужой закрытый пароль.", "evidence": []}
    assert E.validate_item(item, conv, criteria["C8"])["errors"]
    item["evidence"] = [{"message_id": "synthetic-policy:1", "quote": conv["messages"][1]["text"]}]
    valid = E.validate_item(item, conv, criteria["C8"])
    assert not valid["errors"] and E.critical_hit(valid["answer"], criteria["C8"])
    _, prompt = P.build_single(conv, criteria["C8"])
    assert "какая конфиденциальная информация фактически сообщена" in prompt
    assert "не меняет итоговую оценку" in prompt


def test_c8_is_separate_flag_and_never_changes_total(v1, pdf_example, evals_for):
    pdf_example["C3"] = "no"
    before = score(v1, evals_for(v1, pdf_example))
    pdf_example["C8"] = "yes"
    after = score(v1, evals_for(v1, pdf_example))
    assert not before.critical and after.critical and after.critical_criteria == ["C8"]
    assert before.total == after.total
    assert not next(line for line in after.lines if line.criterion_id == "C8").counts_in_total


def test_guidance_change_invalidates_only_its_criterion_version(criteria, tmp_path, monkeypatch):
    before = {cid: P.prompt_version(c) for cid, c in criteria.items()}
    overlay = copy.deepcopy(P.load_overlay())
    overlay["criteria"]["C2"]["base"].append("Синтетическая проверка версии только C2.")
    path = tmp_path / "synthetic-guidance.json"
    path.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CQA_GUIDANCE_FILE", str(path))
    changed = {cid for cid, c in criteria.items() if P.prompt_version(c) != before[cid]}
    assert changed == {"C2"}


def test_replacement_criterion_does_not_inherit_official_policy(criteria):
    replaced = {**criteria["C7"], "hash": "synthetic-new-criterion",
                "description": "Да — оператор назвал учебный раздел.", "guidance": ["Синтетическое правило."]}
    lines, reading = P.guidance_for(replaced)
    assert lines == ["Синтетическое правило."] and reading is None
    assert "предложение дальнейшей помощи" not in P.criterion_block(replaced)
