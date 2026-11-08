"""cqa.evaluator on the mock provider: validator, UTF-16 evidence, votes + arbiter, retries, dynamic prompts.
Only synthetic dialogues here (no organizer data in the repo)."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa import evaluator as E  # noqa: E402
from cqa import llm, rules  # noqa: E402
from cqa import prompts as P  # noqa: E402
from tests.offline_guard import no_network as _no_network  # noqa: E402

CONV = {
    "conversation_id": "t-ru-001", "channel": "agent chat", "language": "RU", "audio_url": None,
    "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": "Где посмотреть комиссию за вывод?"},
        {"timestamp": "00:02", "speaker": "bot", "text": "Здравствуйте! Я виртуальный помощник."},
        {"timestamp": "00:20", "speaker": "client", "text": "Соедините с оператором."},
        {"timestamp": "00:21", "speaker": "system", "text": "Эскалация на оператора."},
        {"timestamp": "01:10", "speaker": "operator", "text": "Здравствуйте, меня зовут Ольга. Комиссия указана в разделе «Тарифы» 😀 внизу страницы."},
        {"timestamp": "01:30", "speaker": "client", "text": "Спасибо, нашёл."},
        {"timestamp": "01:45", "speaker": "operator", "text": "Рада помочь. Хорошего дня!"},
    ],
}

CHECKLIST = {
    "checklist_id": "test", "version": 1, "criteria": [
        {"id": "C1", "title": "Установление контакта", "answer_type": "binary", "weight": 10, "na_allowed": False, "na_rule": "",
         "description": "Да — оператор поздоровался на языке клиента и представился в первом ответе.", "guidance": [], "hash": "h1"},
        {"id": "C2", "title": "Выявление потребности", "answer_type": "binary", "weight": 15, "na_allowed": True,
         "description": "Да — оператор учёл известные детали и уточнил недостающее до ответа.",
         "na_rule": "N/A — данных для ответа хватало с самого начала.", "guidance": [], "hash": "h2"},
        {"id": "C4", "title": "Корректная и полная консультация", "answer_type": "scale", "scale": [0, 5, 10, 15, 20],
         "scale_labels": {"20": "Все вопросы закрыты.", "15": "Пропущена деталь.", "10": "Часть закрыта.", "5": "Общая подсказка.", "0": "Нет ответа."},
         "weight": 20, "na_allowed": True, "na_rule": "N/A — чат закончился до запроса.", "description": "Оцените ответ.", "guidance": [], "hash": "h4"},
        {"id": "C6", "title": "Управление диалогом", "answer_type": "scale", "scale": [0, 4, 7, 10], "weight": 15, "na_allowed": False,
         "description": "Оцените ясность.", "guidance": [], "hash": "h6"},
        {"id": "C8", "title": "Грубые ошибки", "answer_type": "binary", "weight": 0, "na_allowed": False, "critical": True, "critical_when": "yes",
         "description": "Да — выявлено оскорбление клиента.", "guidance": [], "hash": "h8"},
    ],
}

OP1 = "t-ru-001:4"
OP2 = "t-ru-001:6"
GOOD = {
    "C1": {"answer": "yes", "evidence": [{"message_id": OP1, "quote": "Здравствуйте, меня зовут Ольга."}]},
    "C2": {"answer": "na", "evidence": [], "na_reason": "вопрос был понятен из первого сообщения"},
    "C4": {"answer": 20, "evidence": [{"message_id": OP1, "quote": "Комиссия указана в разделе «Тарифы» 😀 внизу страницы."}]},
    "C6": {"answer": 10, "evidence": [{"message_id": OP2, "quote": "Рада помочь."}]},
    "C8": {"answer": "no", "evidence": []},
}


def asked_ids(user: str) -> list[str]:
    return re.findall(r"^КРИТЕРИЙ (\S+) «", user, flags=re.M)


def item(cid, **over):
    base = {"criterion_id": cid, "analysis": "разбор", "explanation": f"{cid} — пояснение.", "checked_range": None, "na_reason": None,
            "suggestion": None, "confidence": 0.9, **copy.deepcopy(GOOD.get(cid, {"answer": "yes", "evidence": []}))}
    base.update(over)
    return base


class Script:
    """Mock handler: per-criterion answers per run; records every call."""

    def __init__(self, per_run=None, arbiter=None, first_bad=None):
        self.per_run, self.arbiter, self.first_bad = per_run or {}, arbiter or {}, set(first_bad or [])
        self.calls = []

    def __call__(self, system, user, model, temperature, run):
        ids = asked_ids(user)
        self.calls.append({"ids": ids, "model": model, "temperature": temperature, "run": run, "user": user,
                           "arbiter": "Ты — арбитр" in user, "retry": "отклонён проверкой" in user})
        out = []
        for cid in ids:
            if "Ты — арбитр" in user and cid in self.arbiter:
                out.append(item(cid, **self.arbiter[cid]))
                continue
            if cid in self.first_bad and "отклонён проверкой" not in user:
                out.append(item(cid, evidence=[{"message_id": OP1, "quote": "этой фразы в диалоге нет совсем"}]))
                continue
            seq = self.per_run.get(cid)
            out.append(item(cid, **(seq[run % len(seq)] if seq else {})))
        return {"criteria": out} if len(ids) > 1 else out[0]


@pytest.fixture(autouse=True)
def _mock(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CQA_PROFILE", "mock")
    monkeypatch.delenv("CQA_READINGS", raising=False)
    yield
    llm.set_mock(None)


def run_eval(script, mode="accurate", **kw):
    llm.set_mock(script)
    return E.evaluate(CONV, CHECKLIST, mode, profile="mock", as_models=False, **kw)


# ---------------------------------------------------------------- evidence & UTF-16
def test_locate_quote_exact_and_normalized():
    text = "Здравствуйте, меня   зовут Алёна. Комиссия — 0%."
    s, e, score = E.locate_quote(text, "меня зовут Алена")
    assert text[s:e] == "меня   зовут Алёна" and score == 100
    s, e, _ = E.locate_quote(text, "«Комиссия - 0%.»")
    assert text[s:e] == "Комиссия — 0%."


def test_locate_quote_fuzzy_returns_original_slice():
    text = "Откройте раздел Отчёты и документы, затем выберите Отчёт брокера."
    s, e, score = E.locate_quote(text, "Откройте раздел Отчеты и документы, затем выбирите Отчёт брокера")
    assert 90 <= score < 100 and text[s:e].startswith("Откройте раздел") and text[s:e] in text


def test_locate_quote_elided_and_missing():
    text = "Здравствуйте, я Тимур. Понимаю ваше беспокойство. Где вы смотрели тариф?"
    s, e, _ = E.locate_quote(text, "Здравствуйте, я Тимур … Где вы смотрели тариф?")
    assert text[s:e] == text
    assert E.locate_quote(text, "Совсем другая фраза про погоду") is None
    assert E.locate_quote(text, "") is None


def test_utf16_offsets_match_javascript():
    conv = E.as_conversation(CONV)
    ev, errors, _ = E.validate_evidence([{"message_id": OP1, "quote": "внизу страницы"}], conv)
    text = conv["messages"][4]["text"]
    assert not errors and ev[0]["quote"] == "внизу страницы"
    js = text.encode("utf-16-le")
    assert js[ev[0]["start"] * 2: ev[0]["end"] * 2].decode("utf-16-le") == "внизу страницы"
    assert ev[0]["start"] == text.index("внизу") + 1  # the emoji before it is 2 UTF-16 units


def test_evidence_any_role_wrong_id_and_unknown_id():
    conv = E.as_conversation(CONV)
    ev, errors, notes = E.validate_evidence([
        {"message_id": "t-ru-001:3", "quote": "Эскалация на оператора."},          # system message is valid evidence
        {"message_id": "t-ru-001:5", "quote": "Хорошего дня!"},                    # wrong id -> moved to :6
        {"message_id": "t-ru-001:99", "quote": "что-то"},
    ], conv)
    assert [e["message_id"] for e in ev] == ["t-ru-001:3", OP2]
    assert any("исправлен" in n for n in notes) and any("t-ru-001:99" in e for e in errors)


def test_checked_range_snaps_to_operator_messages():
    conv = E.as_conversation(CONV)
    assert E.validate_range(["t-ru-001:0", "t-ru-001:6"], conv) == ([OP1, OP2], [])
    assert E.validate_range(["t-ru-001:6", "t-ru-001:3"], conv) == ([OP1, OP2], [])
    rng, err = E.validate_range(["t-ru-001:0", "t-ru-001:2"], conv)
    assert rng is None and err


# ---------------------------------------------------------------- answers
def test_normalize_answer_rules():
    crit = {c["id"]: E.as_criterion(c) for c in CHECKLIST["criteria"]}
    assert E.normalize_answer("Да", crit["C1"]) == ("yes", None)
    assert E.normalize_answer("N/A", crit["C1"])[1]           # N/A not allowed for C1
    assert E.normalize_answer("n/a", crit["C2"]) == ("na", None)
    assert E.normalize_answer("15", crit["C4"]) == (15, None)
    assert E.normalize_answer(12, crit["C4"])[1]               # not a scale step
    assert E.normalize_answer("yes", crit["C6"])[1]            # scale needs a number
    assert E.normalize_answer(7.0, crit["C6"]) == (7, None)


def test_validate_item_requirements():
    conv = E.as_conversation(CONV)
    crit = {c["id"]: E.as_criterion(c) for c in CHECKLIST["criteria"]}
    assert E.validate_item({"answer": "yes", "evidence": []}, conv, crit["C1"])["errors"]           # top answer needs a quote
    v = E.validate_item({"answer": "no", "evidence": [], "explanation": "не представился"}, conv, crit["C1"])
    assert not v["errors"] and v["checked_range"] == [OP1, OP2]                                     # missed action -> operator span
    v = E.validate_item({"answer": "na", "evidence": [], "explanation": "нечего уточнять"}, conv, crit["C2"])
    assert not v["errors"] and v["na_reason"] == "нечего уточнять"
    assert E.validate_item({"answer": "yes", "evidence": []}, conv, crit["C8"])["errors"]           # violation needs a quote
    v = E.validate_item({"answer": "no", "evidence": []}, conv, crit["C8"])
    assert not v["errors"] and v["checked_range"] == [OP1, OP2]


# ---------------------------------------------------------------- end-to-end on mock
def test_fast_mode_one_call_valid_output():
    s = Script()
    out = run_eval(s, "fast")
    assert len(s.calls) == 1 and s.calls[0]["ids"] == ["C1", "C2", "C4", "C6", "C8"]
    by = {r["criterion_id"]: r for r in out}
    assert by["C1"]["answer"] == "yes" and by["C2"]["answer"] == "na" and by["C4"]["answer"] == 20 and by["C8"]["answer"] == "no"
    for r in out:
        for ev in r["evidence"]:
            text = next(m["text"] for m in E.as_conversation(CONV)["messages"] if m["id"] == ev["message_id"])
            assert ev["valid"] and text.encode("utf-16-le")[ev["start"] * 2: ev["end"] * 2].decode("utf-16-le") == ev["quote"]
    assert by["C2"]["na_reason"] and by["C8"]["critical_hit"] is False and by["C8"]["checked_range"] == [OP1, OP2]
    assert all(r["prompt_version"].startswith(P.TEMPLATE_VERSION) for r in out)


def test_accurate_unanimous_no_arbiter():
    s = Script()
    out = run_eval(s, "accurate")
    assert len(s.calls) == 3 * 5 and not any(c["arbiter"] for c in s.calls)
    assert sorted({c["run"] for c in s.calls}) == [0, 1, 2]
    assert all(r["confidence"] == 1.0 and not r["needs_review"] for r in out if r["criterion_id"] != "C8")
    assert all("×3" in r["model"] for r in out)
    assert all(r["raw"]["agreement"] == 1.0 for r in out)
    assert all(v["self_confidence"] == 0.9 for r in out for v in r["raw"]["votes"])


@pytest.mark.parametrize("votes", [1, 3])
@pytest.mark.parametrize("self_confidence", [0.0, 0.5, 0.69])
def test_accurate_incomplete_c1_unanimous_low_confidence_requires_review(votes, self_confidence):
    conv = {"conversation_id": "synthetic-incomplete", "channel": "voice call", "language": "RU", "transcript": [
        {"speaker": "system", "text": "Учебная запись начинается с середины разговора."},
        {"speaker": "operator", "text": "Продолжим учебный пример."},
    ]}
    script = Script(per_run={"C1": [{"answer": "no", "evidence": [], "confidence": self_confidence,
                                    "explanation": "Нужна ручная проверка: неполная запись."}]})
    llm.set_mock(script)
    pre = {"model": "mock:policy", "votes": votes, "temperatures": [0.0] * votes,
           "arbiter": {"model": "mock:arbiter"}}
    result = E.evaluate(conv, CHECKLIST, "accurate", only_criteria=["C1"], preset=pre, as_models=False)[0]
    assert result["answer"] == "no" and result["na_reason"] is None
    assert result["needs_review"] and len(result["review_reasons"]) == 1
    assert "низкая заявленная уверенность модели" in result["review_reasons"][0]
    assert "согласие голосов не устраняет неопределённость" in result["review_reasons"][0]
    # Agreement remains 1.0; it does not replace the model's reported uncertainty.
    assert result["confidence"] == result["raw"]["agreement"] == 1.0
    assert [v["self_confidence"] for v in result["raw"]["votes"]] == [self_confidence] * votes
    assert result["raw"]["arbiter"] is None and not any(c["arbiter"] for c in script.calls)


def test_accurate_confident_representative_does_not_hide_uncertain_supporter():
    script = Script(per_run={"C4": [{"confidence": 0.5}, {"confidence": 0.95}, {"confidence": 0.9}]})
    result = run_eval(script, "accurate", only_criteria=["C4"])[0]
    assert result["answer"] == 20 and result["needs_review"]
    assert result["raw"]["agreement"] == 1.0
    assert "1 из 3" in result["review_reasons"][0] and "минимум 0.50" in result["review_reasons"][0]


@pytest.mark.parametrize("self_confidence", [0.7, 0.95])
def test_accurate_confident_unanimous_answers_do_not_need_confidence_review(self_confidence):
    script = Script(per_run={"C4": [{"confidence": self_confidence}]})
    result = run_eval(script, "accurate", only_criteria=["C4"])[0]
    assert result["answer"] == 20 and not result["needs_review"]


def test_accurate_missing_self_confidence_is_not_filled_from_agreement():
    script = Script(per_run={"C4": [{"confidence": None}]})
    result = run_eval(script, "accurate", only_criteria=["C4"])[0]
    assert result["raw"]["agreement"] == 1.0
    assert all(v["self_confidence"] is None for v in result["raw"]["votes"])


def test_accurate_invalid_low_confidence_vote_is_not_a_valid_supporter():
    conv = E.as_conversation(CONV)
    crit = E.as_criterion(next(c for c in CHECKLIST["criteria"] if c["id"] == "C4"))
    valid = E.validate_item(item("C4"), conv, crit)
    invalid = E.validate_item(item("C4", confidence=0.2, evidence=[]), conv, crit)
    assert not valid["errors"] and invalid["errors"]
    votes = [valid, valid, invalid]
    answer, agreement, dist = E.aggregate(votes, crit)
    result = E._finalize(conv, crit, votes, valid, answer, agreement, "accurate", {"model": "mock:test"},
                         dist, None, None)
    assert result["answer"] == 20 and not result["needs_review"]


@pytest.mark.parametrize("arbiter_evidence", [[], [{"message_id": OP1, "quote": "Комиссия указана в разделе"}]])
def test_accurate_low_confidence_arbiter_requires_review_even_with_borrowed_evidence(arbiter_evidence):
    script = Script(per_run={"C4": [{"answer": 20}, {"answer": 15}, {"answer": 20}]},
                    arbiter={"C4": {"answer": 20, "confidence": 0.5, "evidence": arbiter_evidence}})
    result = run_eval(script, "accurate", only_criteria=["C4"])[0]
    assert result["answer"] == 20 and result["evidence"] and result["needs_review"]
    assert result["raw"]["agreement"] == 0.75 and result["raw"]["arbiter"]["self_confidence"] == 0.5
    assert "1 из 3" in result["review_reasons"][0]


def test_accurate_uncertain_dissent_does_not_lower_confidence_of_supported_answer():
    script = Script(per_run={"C4": [{"answer": 20}, {"answer": 15, "confidence": 0.2}, {"answer": 20}]},
                    arbiter={"C4": {"answer": 20, "confidence": 0.9}})
    result = run_eval(script, "accurate", only_criteria=["C4"])[0]
    assert result["answer"] == 20 and result["raw"]["agreement"] == 0.75
    assert not result["needs_review"]


def test_accurate_disagreement_goes_to_arbiter():
    s = Script(per_run={"C2": [{"answer": "na"}, {"answer": "no"}, {"answer": "na"}]},
               arbiter={"C2": {"answer": "no", "evidence": [], "explanation": "Выявление потребности — Нет."}})
    out = run_eval(s, "accurate", only_criteria=["C2"])
    arb = [c for c in s.calls if c["arbiter"]]
    assert len(arb) == 1 and arb[0]["model"] == "mock-arbiter"
    r = out[0]
    assert r["answer"] == "no" and r["confidence"] == pytest.approx(2 / 4) and r["needs_review"]
    assert "арбитр" in r["model"] and r["raw"]["arbiter"]["answer"] == "no" and r["checked_range"] == [OP1, OP2]


def test_arbiter_confirms_majority_high_confidence():
    s = Script(per_run={"C4": [{"answer": 20}, {"answer": 15, "evidence": [{"message_id": OP1, "quote": "Комиссия указана в разделе"}]}, {"answer": 20}]},
               arbiter={"C4": {"answer": 20, "evidence": []}})
    r = run_eval(s, "accurate", only_criteria=["C4"])[0]
    assert r["answer"] == 20 and r["confidence"] == pytest.approx(3 / 4) and not r["needs_review"]
    assert r["evidence"] and r["evidence"][0]["message_id"] == OP1  # arbiter gave no quote -> evidence of an agreeing vote


def test_invalid_quote_is_retried_with_validator_errors():
    s = Script(first_bad=["C1"])
    r = run_eval(s, "accurate", only_criteria=["C1"])[0]
    retries = [c for c in s.calls if c["retry"]]
    assert len(retries) == 3 and "не найдена дословно" in retries[0]["user"]
    assert r["answer"] == "yes" and r["evidence"][0]["quote"] == "Здравствуйте, меня зовут Ольга." and not r["needs_review"]


def test_na_not_allowed_never_reaches_output():
    s = Script(per_run={"C1": [{"answer": "na", "na_reason": "x"}]})
    r = run_eval(s, "accurate", only_criteria=["C1"])[0]
    assert r["answer"] is None and r["needs_review"] and "N/A" in r["review_reasons"][0]


def test_critical_polarity_from_critical_when():
    ck = copy.deepcopy(CHECKLIST)
    c8 = next(c for c in ck["criteria"] if c["id"] == "C8")
    c8["critical_when"] = "no"  # inverted polarity defined in data
    s = Script(per_run={"C8": [{"answer": "no", "evidence": [{"message_id": OP2, "quote": "Рада помочь."}]}]})
    llm.set_mock(s)
    r = E.evaluate(CONV, ck, "fast", profile="mock", as_models=False, only_criteria=["C8"])[0]
    assert r["critical_hit"] is True and "грубое нарушение" in " ".join(r["review_reasons"])
    assert "«no» (Нет) означает, что НАРУШЕНИЕ ВЫЯВЛЕНО" in s.calls[0]["user"]


def test_replaced_criterion_prompt_is_built_from_its_fields():
    new = {"id": "C9", "title": "Предложил подписку на рассылку", "answer_type": "scale", "scale": [0, 1, 2, 3], "na_allowed": False,
           "description": "3 — предложил и объяснил пользу; 0 — не предлагал.", "scale_labels": {"3": "предложил и объяснил", "0": "не предлагал"},
           "guidance": ["Учитывай только реплики оператора."], "hash": "new-hash"}
    ck = {"criteria": [new]}
    s = Script(per_run={"C9": [{"answer": 0, "evidence": []}]})
    llm.set_mock(s)
    r = E.evaluate(CONV, ck, "fast", profile="mock", as_models=False)[0]
    user = s.calls[0]["user"]
    assert "Предложил подписку на рассылку" in user and "0, 1, 2, 3" in user and "Учитывай только реплики оператора." in user
    assert "N/A для этого критерия не разрешён" in user and r["answer"] == 0 and r["checked_range"] == [OP1, OP2]


def test_overlay_applies_only_to_unchanged_organizer_criterion(monkeypatch):
    ov = P.load_overlay()["criteria"]["C7"]
    # CQA-01: the overlay needs the full unchanged official contract (incl. its guidance); a stale hash is not enough.
    official = json.loads(P.OFFICIAL_CHECKLIST.read_text(encoding="utf-8"))
    c7 = E.as_criterion(next(c for c in official["criteria"] if c["id"] == "C7"))
    strict = P.criterion_block(c7, {"C7": "strict"})
    lenient = P.criterion_block(c7, {"C7": "lenient"})
    assert ov["variants"]["strict"][0] in strict and ov["variants"]["lenient"][0] in lenient
    assert P.prompt_version(c7, {"C7": "strict"}) != P.prompt_version(c7, {"C7": "lenient"})
    monkeypatch.setenv("CQA_READINGS", "C7=lenient")
    assert ov["variants"]["lenient"][0] in P.criterion_block(c7)
    replaced = {**c7, "description": "Да — оператор попрощался.", "hash": "other"}
    assert ov["variants"]["lenient"][0] not in P.criterion_block(replaced)


def test_only_criteria_and_run_number_in_cache(monkeypatch):
    calls = []

    def fake_dispatch(provider, system, user, model, temperature, run, max_tokens, timeout, effort):
        calls.append(run)
        return json.dumps(item("C1")), {"tokens_in": 10, "tokens_out": 5, "tokens_cached": 0, "cost_usd": 0.0}

    monkeypatch.setattr(llm, "_dispatch", fake_dispatch)
    # Populate the real cache path through an injected provider; sockets stay forbidden.
    monkeypatch.setenv("CQA_OFFLINE", "0")
    pre = {"model": "openai:fake", "votes": 3, "temperatures": [0.5, 0.5, 0.5], "arbiter": None}
    out = E.evaluate(CONV, CHECKLIST, "accurate", only_criteria=["C1"], preset=pre, as_models=False, no_cache=False)
    assert len(out) == 1 and sorted(calls) == [0, 1, 2]  # same temperature, three injected dispatches
    assert out[0]["criterion_id"] == "C1" and out[0]["answer"] == "yes"
    assert out[0]["raw"]["cache_hits"] == 0
    again = E.evaluate(CONV, CHECKLIST, "accurate", only_criteria=["C1"], preset=pre, as_models=False, no_cache=False)
    assert len(calls) == 3 and again[0]["raw"]["cache_hits"] == 3
    monkeypatch.setenv("CQA_OFFLINE", "1")
    cached = E.evaluate(CONV, CHECKLIST, "accurate", only_criteria=["C1"], preset=pre, as_models=False)
    assert cached[0]["answer"] == "yes" and cached[0]["raw"]["cache_hits"] == 3 and len(calls) == 3
    miss = E.evaluate(CONV, CHECKLIST, "accurate", only_criteria=["C1"],
                      preset={**pre, "model": "openai:uncached-synthetic"}, as_models=False)
    assert miss[0]["answer"] is None and miss[0]["needs_review"] and len(calls) == 3
    assert any("CQA_OFFLINE=1 and no cached answer" in reason for reason in miss[0]["review_reasons"])


def test_model_failure_gives_review_not_crash(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("network down")

    monkeypatch.setattr(llm, "complete_json", boom)
    out = E.evaluate(CONV, CHECKLIST, "accurate", profile="mock", as_models=False, only_criteria=["C1"])
    assert out[0]["answer"] is None and out[0]["needs_review"] and "network down" in out[0]["review_reasons"][0]


# ---------------------------------------------------------------- facts / rules
def test_rules_facts_and_first_reply():
    conv = E.as_conversation(CONV)
    facts = rules.facts(conv)
    assert any("Первый ответ оператора: [t-ru-001:4]" in f for f in facts)
    assert any("Передача оператору" in f for f in facts)
    assert rules.check_contact(conv) == "yes"
    call = {"conversation_id": "t-call", "channel": "voice call", "transcript": [
        {"timestamp": "2026-01-01T10:00:00+00:00", "speaker": "client", "text": "Алло"},
        {"timestamp": "2026-01-01T10:00:02.1234567+00:00", "speaker": "operator", "text": "Компания Фридом брокер"},
        {"timestamp": "2026-01-01T10:00:03+00:00", "speaker": "system", "text": "Запись и транскрибирование запущены."},
        {"timestamp": "2026-01-01T10:00:04+00:00", "speaker": "operator", "text": "Здравствуйте, меня зовут [Имя скрыто]"},
        {"timestamp": "2026-01-01T10:00:09+00:00", "speaker": "client", "text": "Да, здравствуйте"}]}
    c = E.as_conversation(call)
    assert [m["id"] for m in rules.first_reply(c)] == ["t-call:1", "t-call:3"]
    assert rules.check_contact(c) == "yes" and c["messages"][1]["t_sec"] == pytest.approx(2.123, abs=1e-3)


# ---------------------------------------------------------------- integration with cqa.models / engine (a1)
def test_returns_criterion_eval_models_with_engine_hash():
    models = pytest.importorskip("cqa.models")
    llm.set_mock(Script())
    out = E.evaluate(CONV, CHECKLIST, "fast", profile="mock")
    assert all(isinstance(r, models.CriterionEval) for r in out)
    c1 = next(r for r in out if r.criterion_id == "C1")
    assert c1.criterion_hash == models.criterion_hash(CHECKLIST["criteria"][0]) and c1.provider == "mock"
    assert c1.mode == "fast:mock" and c1.evidence[0].valid and c1.created_at


def test_engine_scores_evaluator_output():
    engine = pytest.importorskip("cqa.engine")
    models = pytest.importorskip("cqa.models")
    ck = models.ChecklistVersion(**{**CHECKLIST, "criteria": [{**c, "max_points": c["weight"], "section": "S"} for c in CHECKLIST["criteria"]]})
    llm.set_mock(Script())
    evals = E.evaluate(CONV, ck, "fast", profile="mock")
    s = engine.score(ck, evals)
    assert s.total == 100.0 and not s.critical  # C1 yes, C2 n/a, C4 20/20, C6 10/10, C8 no


def test_mode_key_separates_profiles():
    assert E.mode_key("accurate", "cloud") == "accurate"
    assert E.mode_key("accurate", "local") == "accurate:local"


def test_dossier_validates_quotes_and_answer():
    def handler(system, user, model, temperature, run):
        assert "Апелляция оператора: «Я представилась»" in user and "Исходная оценка ИИ: answer=\"no\"" in user
        return {"for": [{"text": "Имя есть в первой реплике", "evidence": [{"message_id": OP1, "quote": "меня зовут Ольга"}]}],
                "against": [{"text": "Выдуманная цитата", "evidence": [{"message_id": OP1, "quote": "такого текста нет"}]}],
                "recommendation": "Изменить на «Да».", "suggested_answer": "Да", "confidence": 0.8}

    llm.set_mock(handler)
    d = E.dossier(CONV, CHECKLIST["criteria"][0], {"answer": "no", "explanation": "не представилась", "evidence": []},
                  "Я представилась", [OP1], profile="mock")
    assert d["for"][0]["evidence"][0]["quote"] == "меня зовут Ольга" and d["against"][0]["evidence"] == []
    assert d["suggested_answer"] == "yes" and d["confidence"] == 0.8 and d["errors"]


# ---------------------------------------------------------------- cascade (cheap model first, stronger on doubt)
CASCADE = {"model": "mock:cheap", "votes": 1, "temperatures": [0.0], "escalate": {"model": "mock:strong", "when": "needs_review"}}


class ByModel:
    """Route mock calls to a Script per model name."""

    def __init__(self, **scripts):
        self.scripts, self.models = scripts, []

    def __call__(self, system, user, model, temperature, run):
        self.models.append(model)
        return self.scripts[model](system, user, model, temperature, run)


def test_cascade_without_doubt_uses_only_the_cheap_model():
    h = ByModel(cheap=Script(), strong=Script())
    llm.set_mock(h)
    out = E.evaluate(CONV, CHECKLIST, "fast", preset=CASCADE, as_models=False)
    assert set(h.models) == {"cheap"} and len(h.models) == 1
    assert all(not r["raw"]["cascade"]["escalated"] for r in out)
    assert all("без эскалации" in r["model"] for r in out)


def test_cascade_escalates_whole_dialogue_when_a_criterion_needs_review():
    cheap = Script(per_run={"C4": [{"answer": 5, "confidence": 0.4,
                                    "evidence": [{"message_id": OP1, "quote": "Комиссия указана в разделе «Тарифы» 😀 внизу страницы."}]}]})
    h = ByModel(cheap=cheap, strong=Script())
    llm.set_mock(h)
    out = {r["criterion_id"]: r for r in E.evaluate(CONV, CHECKLIST, "fast", preset=CASCADE, as_models=False)}
    assert h.models == ["cheap", "strong"]
    assert out["C4"]["answer"] == 20 and out["C4"]["raw"]["cascade"]["first_answers"]["C4"] == 5
    assert out["C4"]["raw"]["cascade"]["flagged"] == ["C4"] and out["C1"]["raw"]["cascade"]["escalated"]
    assert all(r["model"] == "mock:cheap → mock:strong" for r in out.values())
    # cost and tokens include both passes
    assert out["C1"]["tokens_in"] > 0 and out["C1"]["raw"]["votes"][0]["model"] == "mock:strong"


def test_cascade_always_escalates_calls_when_configured():
    preset = dict(CASCADE, escalate={"model": "mock:strong", "when": "needs_review_or_call"})
    h = ByModel(cheap=Script(), strong=Script())
    llm.set_mock(h)
    out = E.evaluate(dict(CONV, channel="call"), CHECKLIST, "fast", preset=preset, as_models=False)
    assert h.models == ["cheap", "strong"] and all(r["raw"]["cascade"]["call_escalation"] for r in out)
    h2 = ByModel(cheap=Script(), strong=Script())
    llm.set_mock(h2)
    E.evaluate(CONV, CHECKLIST, "fast", preset=preset, as_models=False)  # a chat without doubts stays on the cheap model
    assert h2.models == ["cheap"]


def test_cascade_keeps_first_answer_when_stronger_model_gives_none():
    cheap = Script(per_run={"C6": [{"answer": 7, "confidence": 0.3, "evidence": [{"message_id": OP2, "quote": "Рада помочь."}]}]})

    def strong(system, user, model, temperature, run):
        ids = asked_ids(user)
        items = [item(cid) for cid in ids if cid != "C6"]
        return {"criteria": items} if len(ids) > 1 else {}

    llm.set_mock(ByModel(cheap=cheap, strong=strong))
    out = {r["criterion_id"]: r for r in E.evaluate(CONV, CHECKLIST, "fast", preset=CASCADE, as_models=False)}
    assert out["C6"]["answer"] == 7 and "вторая не ответила" in out["C6"]["model"]
    assert out["C4"]["answer"] == 20


def test_cascade_profile_is_configured():
    pre = llm.preset("fast", "cascade")
    assert pre["model"].endswith("claude-haiku-5.5") and pre["escalate"]["model"].endswith("gemini-4-argon")


# ---------------------------------------------------------------- routing (calls and account-data dialogues to the strong model)
ROUTING = {"model": "mock:cheap", "votes": 1, "temperatures": [0.0], "route_by_channel": {"call": "mock:strong"},
           "escalate": {"model": "mock:strong", "when": "needs_review", "when_answers": {"C4": [5.0, 0.0]}}}


def test_routing_sends_calls_straight_to_the_strong_model():
    call = dict(CONV, conversation_id="t-call-001", channel="voice call")
    h = ByModel(cheap=Script(), strong=Script())
    llm.set_mock(h)
    out = E.evaluate(call, CHECKLIST, "fast", preset=ROUTING, as_models=False)
    assert h.models == ["strong"]                                   # no cheap first pass for a call
    assert all(r["raw"]["routing"]["channel"] == "call" and "маршрут: звонок" in r["model"] for r in out)
    assert all("cascade" not in r["raw"] for r in out)


def test_routing_escalates_a_chat_on_a_policy_answer_without_doubt():
    cheap = Script(per_run={"C4": [{"answer": 5, "confidence": 0.95,
                                    "evidence": [{"message_id": OP1, "quote": "Комиссия указана в разделе «Тарифы» 😀 внизу страницы."}]}]})
    h = ByModel(cheap=cheap, strong=Script())
    llm.set_mock(h)
    out = {r["criterion_id"]: r for r in E.evaluate(CONV, CHECKLIST, "fast", preset=ROUTING, as_models=False)}
    assert h.models == ["cheap", "strong"]
    info = out["C1"]["raw"]["cascade"]
    assert info["escalated"] and info["policy"] == ["C4=5"] and "routing" not in out["C1"]["raw"]


def test_routing_keeps_a_simple_chat_on_the_cheap_model():
    h = ByModel(cheap=Script(), strong=Script())
    llm.set_mock(h)
    out = E.evaluate(CONV, CHECKLIST, "fast", preset=ROUTING, as_models=False)
    assert h.models == ["cheap"] and all(not r["raw"]["cascade"]["escalated"] for r in out)


def test_routing_profile_is_configured():
    pre = llm.preset("fast", "routing")
    assert pre["route_by_channel"]["call"].endswith("gemini-4-argon") and pre["model"].endswith("claude-haiku-5.5")
    assert pre["escalate"]["when_answers"] == {"C3": ["yes", "no"]}
