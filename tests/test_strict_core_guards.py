"""Strict review regressions CQA-01/02/05/06/07 and C7 (CQA-04), synthetic invented dialogues only."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cqa import checks
from cqa import evaluator as E
from cqa import llm
from cqa import prompts as P
from tests.offline_guard import no_network as _no_network  # noqa: F401


def _official(cid: str) -> dict:
    raw = json.loads(P.OFFICIAL_CHECKLIST.read_text(encoding="utf-8"))
    return E.as_criterion(next(c for c in raw["criteria"] if c["id"] == cid))


def _conv(msgs, cid="syn-1", channel="chat"):
    return {"id": cid, "channel": channel, "language": "RU",
            "messages": [{"id": f"{cid}:{i}", "idx": i, "speaker": s, "text": t} for i, (s, t) in enumerate(msgs)]}


# ---------------------------------------------------------------- CQA-01
@pytest.mark.parametrize("cid", ["C3", "C7"])
@pytest.mark.parametrize("field,value", [
    ("guidance", ["Синтетическое правило учебного примера."]),
    ("na_rule", "N/A — только для учебного примера."),
    ("na_allowed", False),
    ("answer_type", "scale"),
    ("scale", [0, 10, 20]),
    ("scale_labels", {"0": "нет", "10": "частично", "20": "да"}),
    ("critical", True),
])
def test_semantic_change_detaches_overlay_and_code_subchecks(cid, field, value):
    original = _official(cid)
    entry = P.load_overlay()["criteria"][cid]
    assert P.overlay_matches(entry, original) and P.checks_for(original)
    changed = {**original, field: value}  # the stale cached "hash" of the original must not bypass the guard
    if field == "scale_labels":
        changed["answer_type"], changed["scale"] = "scale", [0, 10, 20]
    assert changed["hash"] == original["hash"]
    assert not P.overlay_matches(entry, changed)
    assert P.checks_for(changed) is None
    assert "Подпроверки" not in P.criterion_block(changed)
    lines, reading = P.guidance_for(changed)
    assert not set(entry.get("base") or []) & set(lines) and reading is None


@pytest.mark.parametrize("cid", ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"])
def test_weight_or_section_change_keeps_overlay(cid):
    original = _official(cid)
    entry = P.load_overlay()["criteria"][cid]
    moved = {**original, "weight": 999, "section": "Синтетический раздел"}
    assert P.overlay_matches(entry, original) and P.overlay_matches(entry, moved)
    assert P.guidance_for(moved) == P.guidance_for(original)
    assert P.checks_for(moved) == P.checks_for(original)


def test_changed_c7_rules_are_not_overridden_by_code_formula():
    c7 = {**_official("C7"), "guidance": ["Синтетика: прощание не обязательно."], "na_rule": "N/A — синтетика."}
    conv = _conv([("client", "Вопрос?"), ("operator", "Ответ по учебному примеру."), ("client", "Ясно.")])
    item = {"answer": "yes", "confidence": 0.9, "explanation": "Синтетика.",
            "evidence": [{"message_id": "syn-1:1", "quote": "Ответ по учебному примеру."}],
            "checks": {"farewell": False, "summary_or_next_step": True, "offer_more_help": False}}
    vote = E.validate_item(item, conv, c7)
    assert vote["answer"] == "yes" and not any("заменён правилом" in n for n in vote["notes"])


# ---------------------------------------------------------------- CQA-02 / CQA-06 / CQA-07 (fast path, mock model)
BINARY = {"id": "X", "title": "Пример", "description": "Да — оператор назвал учебный раздел.", "answer_type": "binary",
          "na_allowed": False, "weight": 100, "section": "Синтетика"}
CONV = _conv([("client", "Где учебный раздел?"), ("operator", "Учебный раздел находится в меню."), ("client", "Спасибо.")])


def _run_fast(monkeypatch, replies):
    calls = []

    def handler(system, user, model, temperature, run):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        return json.dumps({"criteria": [dict(reply, criterion_id="X")]}, ensure_ascii=False)

    monkeypatch.setattr(llm, "_mock_handler", handler)
    ck = {"version": 1, "criteria": [BINARY]}
    res = E.evaluate(CONV, ck, "fast", preset={"model": "mock:syn", "temperatures": [0.0], "votes": 1}, as_models=False)[0]
    return res, calls


GOOD = {"answer": "yes", "confidence": 0.95, "explanation": "Назвал раздел.",
        "evidence": [{"message_id": "syn-1:1", "quote": "Учебный раздел находится в меню."}]}


def test_valid_answer_is_accepted_and_not_provisional(monkeypatch):
    res, _ = _run_fast(monkeypatch, [GOOD])
    assert res["answer"] == "yes" and res["validation_status"] == "valid" and not res["provisional"]
    assert not res["needs_review"]


def test_finally_rejected_evidence_is_provisional_not_accepted(monkeypatch):
    fake = dict(GOOD, evidence=[{"message_id": "syn-1:1", "quote": "Этой фразы в диалоге нет."}])
    res, calls = _run_fast(monkeypatch, [fake])
    assert len(calls) >= 2  # validator-driven re-asks happened
    assert res["answer"] == "yes"  # kept for the reviewer ...
    assert res["validation_status"] == "rejected" and res["provisional"] is True  # ... but not an accepted grade
    assert res["needs_review"] and any("предварительная оценка" in r for r in res["review_reasons"])
    model = E.to_model(res)
    assert getattr(model, "validation_status") == "rejected" and getattr(model, "provisional") is True


def test_low_confidence_with_valid_evidence_is_not_a_validation_failure(monkeypatch):
    res, _ = _run_fast(monkeypatch, [dict(GOOD, confidence=0.3)])
    assert res["needs_review"] and res["validation_status"] == "valid" and not res["provisional"]


@pytest.mark.parametrize("confidence", [None, True, False, float("nan"), "NaN", "Infinity", -0.01, 1.01, 85, "invalid", [0.9]])
def test_missing_or_malformed_confidence_requires_review(monkeypatch, confidence):
    reply = {k: v for k, v in GOOD.items() if k != "confidence"}
    if confidence is not None:
        reply["confidence"] = confidence
    if isinstance(confidence, float) and confidence != confidence:
        reply["confidence"] = "NaN"  # json.dumps(nan) is not standard JSON
    res, _ = _run_fast(monkeypatch, [reply])
    assert res["answer"] == "yes" and res["needs_review"]
    assert any("уверенность" in r for r in res["review_reasons"])


@pytest.mark.parametrize("value,expected", [(0.0, 0.0), (1.0, 1.0), (0.8, 0.8), ("0.8", 0.8), (" 0.8 ", 0.8)])
def test_valid_confidence_values_are_kept(value, expected):
    assert E.parse_confidence(value) == expected


@pytest.mark.parametrize("field", ["explanation", "na_reason", "analysis", "suggestion"])
@pytest.mark.parametrize("bad", [{"invalid": "object"}, ["список"], 42])
def test_non_string_text_field_goes_to_repair_not_crash(monkeypatch, field, bad):
    res, calls = _run_fast(monkeypatch, [dict(GOOD, **{field: bad}), GOOD])
    assert res["answer"] == "yes" and isinstance(res["explanation"], str)
    if field in ("explanation", "na_reason"):  # schema error -> validator-driven re-ask repairs it
        assert len(calls) == 2 and res["validation_status"] == "valid"
    else:  # trace-only fields are dropped with a note, no extra paid call
        assert len(calls) == 1 and res["validation_status"] == "valid"


def test_non_string_explanation_exhausted_retries_is_flagged(monkeypatch):
    res, calls = _run_fast(monkeypatch, [dict(GOOD, explanation={"invalid": "object"})])
    assert len(calls) >= 2 and res["needs_review"] and res["validation_status"] == "rejected"
    assert any("должно быть строкой" in r for r in res["review_reasons"])


# ---------------------------------------------------------------- CQA-05 cascade
def _result(answer, status="valid", reasons=()):
    return {"answer": answer, "validation_status": status, "review_reasons": list(reasons), "needs_review": bool(reasons),
            "model": "mock:model", "raw": {}, "tokens_in": 3, "tokens_out": 2, "cost_kzt": 1.0, "cost_kzt_full": 1.0,
            "latency_ms": 4}


@pytest.mark.parametrize("second,expected,flag", [
    (_result("yes", "rejected", ["ответ модели не прошёл проверку: синтетика"]), "no", True),
    (_result(None, "failed", ["оценка не получена: синтетика"]), "no", True),
    (_result("yes", "valid", ["низкая уверенность модели 0.40"]), "yes", True),
    (_result("yes", "valid", []), "yes", False),
])
def test_cascade_keeps_valid_first_answer_when_second_is_rejected(monkeypatch, second, expected, flag):
    first = _result("no", "valid", ["низкая уверенность модели 0.40"])
    monkeypatch.setattr(E, "_fast_pass", lambda *a, **k: {"X": dict(second)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        out = E._cascade({"channel": "chat"}, [{"id": "X"}], [], {"model": "mock:first"}, 0.0, pool,
                         {"X": first}, {"model": "mock:second"})["X"]
    assert out["answer"] == expected and out["needs_review"] is flag
    assert out["cost_kzt_full"] == 2.0 and out["cost_kzt"] == 2.0 and out["tokens_in"] == 6  # both stages paid
    if expected == "no":
        assert out["validation_status"] == "valid" and any("каскад" in r for r in out["review_reasons"])


def test_cascade_both_rejected_keeps_second_answer_flagged(monkeypatch):
    first = _result("no", "rejected", ["ответ модели не прошёл проверку: синтетика"])
    second = _result("yes", "rejected", ["ответ модели не прошёл проверку: синтетика"])
    monkeypatch.setattr(E, "_fast_pass", lambda *a, **k: {"X": dict(second)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        out = E._cascade({"channel": "chat"}, [{"id": "X"}], [], {"model": "mock:first"}, 0.0, pool,
                         {"X": first}, {"model": "mock:second"})["X"]
    assert out["answer"] == "yes" and out["needs_review"] and out["validation_status"] == "rejected"
    assert out["cost_kzt_full"] == 2.0


# ---------------------------------------------------------------- C7 (CQA-04)
NO_FAREWELL = {"farewell": False, "summary_or_next_step": False, "offer_more_help": False}


def test_c7_operator_closed_before_client_left_is_no_not_na():
    conv = _conv([("client", "Вопрос?"), ("operator", "Ответ по учебному примеру."),
                  ("system", "Operator closed the chat."), ("system", "Customer left the chat.")])
    assert checks.c7_closing(conv, NO_FAREWELL)[0] == "no"
    ru = _conv([("client", "Вопрос?"), ("operator", "Ответ."), ("system", "Оператор закрыл чат."), ("system", "Клиент покинул чат.")])
    assert checks.c7_closing(ru, NO_FAREWELL)[0] == "no"
    kz = _conv([("client", "Сұрақ?"), ("operator", "Жауап."), ("system", "Оператор чатты жапты."), ("system", "Клиент чаттан шықты.")])
    assert checks.c7_closing(kz, NO_FAREWELL)[0] == "no"


@pytest.mark.parametrize("event", ["Клиент чаттан шықты.", "Клиент покинул чат.", "Customer left the chat."])
def test_c7_client_left_before_closing_is_na_in_all_languages(event):
    conv = _conv([("client", "Вопрос?"), ("operator", "Ответ по учебному примеру."), ("system", event)])
    assert checks.c7_closing(conv, NO_FAREWELL)[0] == "na"


def test_c7_full_closing_before_client_left_is_yes():
    conv = _conv([("client", "Вопрос?"), ("operator", "Итог: заявка создана. Всего доброго!"), ("system", "Клиент покинул чат.")])
    assert checks.c7_closing(conv, {"farewell": True, "summary_or_next_step": True, "offer_more_help": False})[0] == "yes"


def test_c7_unknown_deciding_subcheck_keeps_model_answer():
    conv = _conv([("client", "Вопрос?"), ("operator", "Всего доброго!")])
    assert checks.c7_closing(conv, {"farewell": True, "offer_more_help": False}) is None
    assert checks.c7_closing(conv, {"farewell": True, "summary_or_next_step": False}) is None
    assert checks.c7_closing(conv, {"farewell": True, "summary_or_next_step": False, "offer_more_help": False})[0] == "no"
