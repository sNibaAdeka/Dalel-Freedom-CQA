"""Synthetic recording-boundary safeguards, with no live model or reference labels."""
from __future__ import annotations

import copy

import pytest

from cqa import evaluator as E, rules
from cqa.ingest import normalize_record
from cqa.models import Conversation, criterion_hash


@pytest.fixture
def contact(v1_raw):
    return E.as_criterion(next(c for c in v1_raw["criteria"] if c["id"] == "C1"))


def record(*turns, channel="voice call"):
    return {"conversation_id": "synthetic-recording", "channel": channel, "language": "RU",
            "transcript": [{"speaker": role, "text": text,
                            "timestamp": f"2026-01-01T00:00:{i * 3:02d}Z"}
                           for i, (role, text) in enumerate(turns)]}


def late_start(marker="Запись и транскрибирование запущены."):
    return record(
        ("system", "Агент присоединился к разговору с клиентом."),
        ("client", "Алло."),
        ("operator", "Меня зовут Лея. Откройте учебный отчёт."),
        ("system", marker),
        ("client", "Отчёт открыт."),
        ("operator", "Выберите учебный пример."),
    )


def evaluate(monkeypatch, conv, criterion, mode, answer="no", confidence=0.99):
    normalized = E.as_conversation(conv)
    op = next(m for m in normalized["messages"] if m["speaker"] == "operator")
    item = {"criterion_id": criterion["id"], "answer": answer, "confidence": confidence,
            "explanation": "Синтетический ответ модели.",
            "evidence": [{"message_id": op["id"], "quote": op["text"]}],
            "checked_range": [op["id"], op["id"]]}
    calls = []

    def fake_json(*args, **kwargs):
        calls.append(kwargs)
        return copy.deepcopy(item), {"cache_hit": True}

    monkeypatch.setattr(E.llm, "complete_json", fake_json)
    monkeypatch.setattr(E.llm, "_mock_handler", lambda *args: None)
    result = E.evaluate(conv, {"criteria": [criterion]}, mode=mode, as_models=False,
                        preset={"model": "mock:recording-review", "votes": 3,
                                "temperatures": [0.0, 0.1, 0.2]})[0]
    assert len(calls) == (1 if mode == "fast" else 3)
    return result


@pytest.mark.parametrize("mode", ["fast", "accurate"])
@pytest.mark.parametrize("shape", ["raw", "ingested", "model"])
def test_late_start_confident_no_is_provisional_even_with_cached_votes(monkeypatch, contact, mode, shape):
    conv = late_start()
    if shape != "raw":
        conv = normalize_record(conv, "SYNTHETIC")
    if shape == "model":
        conv = Conversation.model_validate(conv)
    before = copy.deepcopy(conv)
    out = evaluate(monkeypatch, conv, contact, mode)
    assert conv == before
    assert out["answer"] == "no" and out["na_reason"] is None
    assert out["needs_review"] and len(out["review_reasons"]) == 1
    reason = out["review_reasons"][0]
    assert "полноту начала записи" in reason and "предварительный" in reason
    assert "synthetic-recording:3" in reason and "synthetic-recording:2" in reason
    assert "текст до запуска присутствует" in reason
    assert "не доказывает пропуск" in reason
    assert out["confidence"] == (0.99 if mode == "fast" else 1.0)
    assert out["raw"]["agreement"] == (None if mode == "fast" else 1.0)
    assert out["raw"]["cache_hits"] == (1 if mode == "fast" else 3)
    assert all(v["self_confidence"] == 0.99 for v in out["raw"]["votes"])
    assert out["evidence"][0]["quote"] == "Меня зовут Лея. Откройте учебный отчёт."
    assert out["checked_range"] == ["synthetic-recording:2"] * 2
    assert not out["critical_hit"]


@pytest.mark.parametrize("marker", [
    "Запись и транскрибирование запущены.", "Запись возобновлена.",
    "Жазу және транскрипция басталды.", "Жазу және транскрипция жалғастырылды.",
    "Recording and transcription started.", "Recording resumed.",
])
def test_ru_kz_en_late_system_events(contact, marker):
    conv = E.as_conversation(late_start(marker))
    assert rules.recording_review_reasons(conv, contact, "no")
    assert rules.rule_check(contact, conv) is None


@pytest.mark.parametrize("marker", [
    "Начало записи отсутствует.", "Запись начинается с середины разговора.",
    "Жазбаның басы жоқ.", "Recording starts mid-conversation.",
    "The beginning of the recording is missing.",
])
def test_explicit_system_clipping_annotation(contact, marker):
    conv = E.as_conversation(record(("system", marker), ("operator", "Выберите учебный пример.")))
    reasons = rules.recording_review_reasons(conv, contact, "no")
    assert len(reasons) == 1 and "synthetic-recording:0" in reasons[0]


@pytest.mark.parametrize("turns", [
    [("system", "Запись и транскрибирование запущены."), ("operator", "Откройте учебный отчёт.")],
    [("system", "Агент присоединился к разговору с клиентом."), ("client", "Алло."),
     ("system", "Запись и транскрибирование запущены."), ("operator", "Откройте учебный отчёт.")],
    [("bot", "Здравствуйте! Нажмите два."), ("client", "2"),
     ("system", "Запись и транскрибирование запущены."), ("operator", "Откройте учебный отчёт.")],
    [("system", "Recording and transcription started."), ("operator", "Откройте учебный отчёт."),
     ("system", "Recording and transcription paused."), ("system", "Recording and transcription resumed."),
     ("operator", "Продолжим учебный пример.")],
    [("operator", "Мы уже говорили. Продолжим учебный пример.")],
    [("operator", "[неразборчиво] Лея. Отчёт... открыть.")],
])
@pytest.mark.parametrize("mode", ["fast", "accurate"])
def test_normal_or_merely_ambiguous_calls_are_not_flagged(monkeypatch, contact, turns, mode):
    out = evaluate(monkeypatch, record(*turns), contact, mode)
    assert out["answer"] == "no" and not out["needs_review"]
    assert out["review_reasons"] == []


@pytest.mark.parametrize("marker", [
    "Запись и транскрибирование не запущены.",
    "Запись и транскрибирование будут запущены.",
    "Если запись начинается с середины разговора, проверьте её.",
    "Начало записи не отсутствует.",
    "Recording and transcription have not started.",
    "Recording and transcription language changed from en-US to ru-RU.",
    "Customer said: Recording starts mid-conversation.",
    "Запись и транскрибирование приостановлены.",
])
def test_negative_hypothetical_and_unrelated_system_wording_is_not_evidence(contact, marker):
    assert not rules.recording_review_reasons(E.as_conversation(late_start(marker)), contact, "no")


@pytest.mark.parametrize("role", ["client", "operator", "bot", "unknown-role"])
@pytest.mark.parametrize("shape", ["raw", "ingested", "model"])
def test_only_explicit_system_role_is_structural_evidence(contact, role, shape):
    raw = late_start()
    raw["transcript"][3]["speaker"] = role
    conv = raw if shape == "raw" else normalize_record(raw, "SYNTHETIC")
    if shape == "model":
        conv = Conversation.model_validate(conv)
    assert not rules.recording_review_reasons(E.as_conversation(conv), contact, "no")


def test_timestamp_offset_and_metadata_alone_do_not_establish_clipping(contact):
    conv = E.as_conversation(record(("operator", "Откройте учебный отчёт.")))
    conv["messages"][0].update(t_sec=240, rec_sec=None)
    conv["meta"] = {"recording_start_sec": 240, "warnings": ["неполная запись"]}
    assert not rules.recording_review_reasons(conv, contact, "no")


def test_chat_is_not_a_recording(contact):
    conv = E.as_conversation(late_start())
    conv["channel"] = "chat"
    assert not rules.recording_review_reasons(conv, contact, "no")


@pytest.mark.parametrize("answer", ["yes", "na", None, 0])
def test_helper_never_reinterprets_other_answers(contact, answer):
    assert not rules.recording_review_reasons(E.as_conversation(late_start()), contact, answer)


@pytest.mark.parametrize("mode", ["fast", "accurate"])
def test_asr_name_is_not_rewritten_or_lexically_rejected_on_uncertain_beginning(monkeypatch, contact, mode):
    conv = late_start()
    conv["transcript"][2]["text"] = "Здраствуйте, Лея... учебный отчёт."
    out = evaluate(monkeypatch, conv, contact, mode, answer="yes")
    assert out["answer"] == "yes" and not out["needs_review"]
    assert out["evidence"][0]["quote"] == conv["transcript"][2]["text"]


@pytest.mark.parametrize("edit", [
    {"title": "Приветствие в конце разговора"},
    {"description": "Да — оператор поздоровался в последнем ответе."},
    {"guidance": ["Оценивайте только окончание звонка."]},
    {"na_allowed": True}, {"na_rule": "N/A при неполной записи."},
    {"critical": True, "critical_when": "yes"}, {"critical_rule": "Иная полярность."},
    {"answer_type": "scale", "scale": [0, 1]}, {"scale_labels": {"1": "Иное правило."}},
    {"id": "CUSTOM"},
])
def test_edited_contact_never_inherits_recording_or_keyword_policy(contact, edit):
    edited = {**contact, **edit}  # Deliberately retain the stale official hash.
    conv = E.as_conversation(late_start())
    assert not rules.recording_review_reasons(conv, edited, "no")
    assert rules.rule_check(edited, conv) is None


def test_weight_and_section_edits_keep_the_same_contact_contract(contact):
    edited = {**contact, "weight": 25, "section": "Свой раздел", "hash": "stale-cache-key"}
    assert criterion_hash(edited) == criterion_hash(contact)
    assert rules.recording_review_reasons(E.as_conversation(late_start()), edited, "no")


@pytest.mark.parametrize("mode", ["fast", "accurate"])
def test_edited_c1_result_uses_its_own_policy(monkeypatch, contact, mode):
    edited = {**contact, "description": "Да — оператор открыл отчёт в конце разговора."}
    out = evaluate(monkeypatch, late_start(), edited, mode, answer="yes")
    assert out["answer"] == "yes" and not out["needs_review"]
    assert out["criterion_hash"] == criterion_hash(edited)


def test_review_helper_is_repeatable_nonmutating_and_reusable_after_pilot_validation(contact):
    conv = E.as_conversation(late_start())
    original = copy.deepcopy((conv, contact))
    validated = E.validate_item({"answer": "no", "confidence": 0.99,
                                 "explanation": "Синтетический ответ."}, conv, contact)
    assert not validated["errors"]
    reasons = rules.recording_review_reasons(conv, contact, validated["answer"])
    assert reasons and reasons == rules.recording_review_reasons(conv, contact, validated["answer"])
    assert (conv, contact) == original
    assert validated["answer"] == "no" and validated["na_reason"] is None
    assert E.normalize_answer("na", contact)[1]  # The official C1 still forbids N/A.
