"""Answer type «Число» (answer_type 'numeric', a min–max range) end to end on the mock provider:
prompt -> evaluator validation (repair when out of range) -> engine points = value / max × weight.
Synthetic dialogue and checklist only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa import engine  # noqa: E402
from cqa import evaluator as E  # noqa: E402
from cqa import llm  # noqa: E402
from cqa import prompts as P  # noqa: E402
from cqa.prompts.mock import rule_mock  # noqa: E402
from tests.offline_guard import no_network as _no_network  # noqa: E402,F401

CONV = {
    "conversation_id": "syn-num-1", "channel": "agent chat", "language": "RU",
    "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": "Сколько стоит вывод средств?"},
        {"timestamp": "00:10", "speaker": "operator", "text": "Здравствуйте, меня зовут Ольга. Вывод бесплатный."},
        {"timestamp": "00:20", "speaker": "client", "text": "Спасибо."},
        {"timestamp": "00:25", "speaker": "operator", "text": "Хорошего дня!"},
    ],
}
OP1 = "syn-num-1:1"
NUM = {"id": "N1", "title": "Вежливость оператора", "answer_type": "numeric", "scale": [0, 10], "weight": 50,
       "na_allowed": False, "description": "Оцените вежливость оператора числом от 0 до 10."}
CHECKLIST = {"checklist_id": "syn", "version": 2, "criteria": [
    {"id": "C1", "title": "Приветствие", "answer_type": "binary", "weight": 50, "na_allowed": False,
     "description": "Да — оператор поздоровался."},
    NUM,
]}


@pytest.fixture(autouse=True)
def _mock(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CQA_PROFILE", "mock")
    monkeypatch.delenv("CQA_READINGS", raising=False)
    yield
    llm.set_mock(None)


def _item(cid, answer):
    return {"criterion_id": cid, "analysis": "разбор", "answer": answer,
            "evidence": [{"message_id": OP1, "quote": "Здравствуйте, меня зовут Ольга."}],
            "checked_range": None, "na_reason": None, "explanation": f"{cid} — пояснение.", "suggestion": None, "confidence": 0.9}


class Script:
    def __init__(self, first, repaired=None):
        self.first, self.repaired, self.calls = first, repaired, []

    def __call__(self, system, user, model, temperature, run):
        self.calls.append(user)
        retry = "отклонён проверкой" in user
        ids = [line.split()[1] for line in user.splitlines() if line.startswith("КРИТЕРИЙ ")]
        out = [_item(cid, "yes" if cid == "C1" else (self.repaired if retry and self.repaired is not None else self.first)) for cid in ids]
        return {"criteria": out} if len(ids) > 1 else out[0]


def test_prompt_asks_for_a_number_in_range_not_yes_no():
    c = E.as_criterion(NUM)
    block = P.criterion_block(c)
    assert "число от 0 до 10" in block and "да / нет" not in block
    assert P.allowed_answers(c) == ["число от 0 до 10"]
    with_na = E.as_criterion({**NUM, "scale": [1, 5], "na_allowed": True, "na_rule": "N/A — вопроса не было."})
    assert "число от 1 до 5" in P.criterion_block(with_na) and P.allowed_answers(with_na)[-1] == "na"


def test_existing_binary_and_scale_prompts_unchanged():
    binary = P.criterion_block(E.as_criterion(CHECKLIST["criteria"][0]))
    assert "Тип ответа: да / нет (N/A не разрешён)" in binary and 'Допустимые значения answer: "yes", "no"' in binary
    scale = E.as_criterion({"id": "S1", "title": "Шкала", "answer_type": "scale", "scale": [0, 5, 10], "weight": 10, "description": "x"})
    assert "Тип ответа: шкала, допустимые ступени: 0, 5, 10" in P.criterion_block(scale)
    assert P.allowed_answers(scale) == [0, 5, 10]


def test_normalize_numeric_answer_in_range_only():
    c = E.as_criterion(NUM)
    assert E.normalize_answer(7.5, c) == (7.5, None)
    assert E.normalize_answer("3", c) == (3, None)
    assert E.normalize_answer("10", c) == (10, None)
    assert E.normalize_answer(11, c)[1] and E.normalize_answer(-1, c)[1]
    assert E.normalize_answer("yes", c)[1]
    assert E.normalize_answer("na", c)[1]  # N/A not allowed here
    assert E.is_top(10, c) and not E.is_top(7, c)


def test_numeric_criterion_evaluates_without_review_and_scores_value_over_max():
    llm.set_mock(Script(first=7))
    evals = E.evaluate(CONV, CHECKLIST, "fast", profile="mock")
    n1 = next(e for e in evals if e.criterion_id == "N1")
    assert n1.answer == 7 and not n1.needs_review, n1.review_reasons
    s = engine.score(CHECKLIST, evals)
    line = next(line for line in s.lines if line.criterion_id == "N1")
    assert line.points == pytest.approx(35.0)  # 7 / 10 × 50
    assert s.total == pytest.approx(85.0)


def test_out_of_range_answer_is_repaired():
    script = Script(first=12, repaired=6)
    llm.set_mock(script)
    out = E.evaluate(CONV, CHECKLIST, "fast", profile="mock", as_models=False, only_criteria=["N1"])[0]
    assert out["answer"] == 6
    assert any("отклонён проверкой" in u and "вне диапазона" in u for u in script.calls)


def test_default_rule_mock_answers_numeric_without_type_mismatch():
    llm.set_mock(rule_mock)
    out = E.evaluate(CONV, CHECKLIST, "fast", profile="mock", as_models=False)
    n1 = next(r for r in out if r["criterion_id"] == "N1")
    assert n1["answer"] == 10
    assert not any("не распознан" in r or "да/нет" in r or "диапазон" in r for r in n1["review_reasons"])
