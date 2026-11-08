"""Calibration v7.1: code-decided C3/C7 from sub-checks, overlay-only application, precedents bank (synthetic data)."""
from __future__ import annotations

import json

from cqa import checks, evaluator, precedents
from cqa import prompts as P


def _conv(channel="chat", msgs=None, cid="t-1"):
    msgs = msgs or []
    return {"id": cid, "channel": channel, "language": "RU",
            "messages": [{"id": f"{cid}:{i}", "idx": i, "speaker": s, "text": t} for i, (s, t) in enumerate(msgs)]}


CHAT = _conv(msgs=[("client", "Где тариф?"), ("system", "Эскалация на оператора."), ("operator", "Здравствуйте, я Алина. В кабинете."),
                   ("client", "Нашла, спасибо."), ("operator", "Пожалуйста. Хорошего дня!")])
BROKEN = _conv(msgs=[("client", "Где тариф?"), ("operator", "Здравствуйте, я Алина. В кабинете."), ("system", "Клиент покинул чат.")])
CALL_HANGUP = _conv("call", [("operator", "Здравствуйте, меня зовут [Имя скрыто]."), ("client", "Понял."),
                             ("system", "Клиент завершил разговор."), ("operator", "Всего доброго, обращайтесь!")])
VERIFIED = _conv(msgs=[("client", "Где мой платёж?"), ("operator", "Подтвердите личность."), ("system", "Личность клиента подтверждена."),
                       ("operator", "Платёж на проверке.")])


def test_c7_formula_and_breaks():
    assert checks.c7_closing(CHAT, {"farewell": True, "summary_or_next_step": False, "offer_more_help": False})[0] == "no"
    assert checks.c7_closing(CHAT, {"farewell": True, "summary_or_next_step": True, "offer_more_help": False})[0] == "yes"
    assert checks.c7_closing(CHAT, {"farewell": True, "summary_or_next_step": False, "offer_more_help": True})[0] == "yes"
    assert checks.c7_closing(CHAT, {"farewell": False, "summary_or_next_step": True, "offer_more_help": True})[0] == "no"
    assert checks.c7_closing(BROKEN, {"farewell": False, "summary_or_next_step": False, "offer_more_help": False})[0] == "na"
    # operator lines after the client hung up are not heard -> N/A, not "no"/"yes"
    assert checks.c7_closing(CALL_HANGUP, {"farewell": False, "summary_or_next_step": False, "offer_more_help": False})[0] == "na"
    assert checks.c7_closing(CHAT, {"farewell": True}) is None  # incomplete -> keep the model's answer


def test_c3_verification_chat_and_call():
    assert checks.c3_verification(VERIFIED, {"client_specific_disclosure_or_action": False})[0] == "na"
    assert checks.c3_verification(VERIFIED, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "t-1:3"})[0] == "yes"
    assert checks.c3_verification(VERIFIED, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "t-1:1"})[0] == "no"
    call = dict(VERIFIED, channel="call")
    assert checks.c3_verification(call, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "t-1:3",
                                         "verified_before_disclosure": True})[0] == "yes"
    assert checks.c3_verification(call, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "t-1:3",
                                         "verified_before_disclosure": "false"})[0] == "no"


def test_checks_apply_only_to_unchanged_organizer_criterion(v1_raw):
    cl = evaluator.as_checklist(v1_raw)
    c7 = next(c for c in cl["criteria"] if c["id"] == "C7")
    assert P.checks_for(c7) and P.checks_for(c7)["rule"] == "c7_closing"
    assert "checks" in P.criterion_block(c7)
    replaced = evaluator.as_criterion(dict(c7, description="Оператор поблагодарил клиента за обращение."))
    assert P.checks_for(replaced) is None and "Подпроверки" not in P.criterion_block(replaced)


def test_validate_item_uses_code_rule(v1_raw):
    cl = evaluator.as_checklist(v1_raw)
    c7 = next(c for c in cl["criteria"] if c["id"] == "C7")
    item = {"criterion_id": "C7", "answer": "yes", "explanation": "Попрощался.",
            "evidence": [{"message_id": "t-1:4", "quote": "Хорошего дня!"}],
            "checks": {"farewell": True, "summary_or_next_step": False, "offer_more_help": False}}
    v = evaluator.validate_item(item, CHAT, c7)
    assert v["answer"] == "no" and any("заменён правилом" in n for n in v["notes"]) and v["checks"]["farewell"] is True
    v2 = evaluator.validate_item({**item, "checks": None}, CHAT, c7)  # no sub-checks -> model answer kept
    assert v2["answer"] == "yes"


def test_precedents_bank_tfidf_and_hash_guard(tmp_path, monkeypatch, v1_raw):
    cl = evaluator.as_checklist(v1_raw)
    h = {c["id"]: c["hash"] for c in cl["criteria"]}
    bank = {"items": [
        {"id": "t-1", "channel": "chat", "language": "RU", "text": "[0] client: Где тариф?", "embed_text": "Где тариф? В кабинете.",
         "answers": {"C1": "yes"}, "hashes": {"C1": h["C1"]}},
        {"id": "d-2", "channel": "chat", "language": "RU", "text": "[0] client: Где посмотреть тариф?", "embed_text": "Где посмотреть тариф? В кабинете",
         "answers": {"C1": "no", "C7": "no"}, "hashes": {"C1": h["C1"], "C7": "stale-hash"}},
        {"id": "d-3", "channel": "chat", "language": "RU", "text": "[0] client: Как вывести деньги?", "embed_text": "Как вывести деньги на карту",
         "answers": {"C1": "yes"}, "hashes": {"C1": h["C1"]}}]}
    p = tmp_path / "bank.json"
    p.write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CQA_PRECEDENTS", str(p))
    monkeypatch.setattr(precedents, "embed", lambda texts: None)  # no network: TF-IDF fallback
    found = precedents.find(CHAT, k=2)
    assert [it["id"] for it, _ in found][0] == "d-2" and all(it["id"] != "t-1" for it, _ in found)  # never itself
    text = P.precedents_block(CHAT, cl["criteria"])
    assert "C1=\"no\"" in text and "C7=" not in text  # stale label for a changed criterion is hidden
    monkeypatch.setenv("CQA_PRECEDENTS", "off")
    assert P.precedents_block(CHAT, cl["criteria"]) == ""


def test_call_hold_segment_is_collapsed():
    call = _conv("call", [("operator", "Здравствуйте, я Дина."), ("operator", "Ожидайте на линии."),
                          ("system", "Клиент находится на удержании."), ("operator", "Коллега, клиент хочет купить бумагу."),
                          ("operator", "Только через трейдеров."), ("system", "Клиент больше не находится на удержании."),
                          ("operator", "Благодарю за ожидание, купить можно только через трейдеров.")])
    assert P.hold_spans(call) == [(2, 5)]
    text = P.render_transcript(call)
    assert "Коллега, клиент" not in text and "удержание: 2" in text and "Благодарю за ожидание" in text
    assert "удержание" not in P.render_transcript(dict(call, channel="chat"))  # chats are rendered unchanged


def test_long_precedent_is_clipped_keeping_the_end():
    text = "\n".join(f"[{i}] operator: фраза номер {i} " + "x" * 80 for i in range(200))
    out = precedents._clip(text, 3000)
    assert len(out) <= 3100 and "[0]" in out and "[199]" in out and "опущена" in out


def test_c3_failed_verification_event_is_not_verification():
    from cqa import checks
    conv = {"channel": "chat", "messages": [
        {"idx": 0, "id": "0", "speaker": "system", "text": "Identity verification failed."},
        {"idx": 1, "id": "1", "speaker": "operator", "text": "Ваш баланс 100."}]}
    ans, _ = checks.c3_verification(conv, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "1"})
    assert ans == "no"
    conv["messages"][0]["text"] = "Личность клиента не подтверждена"
    assert checks.c3_verification(conv, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "1"})[0] == "no"
    conv["messages"][0]["text"] = "Личность клиента подтверждена"
    assert checks.c3_verification(conv, {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "1"})[0] == "yes"


def test_c3_started_or_pending_verification_is_not_verification():
    from cqa import checks
    flags = {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "1"}
    for text, expected in [("Identity verification started.", "no"), ("Проверка личности запрошена", "no"),
                           ("Жеке басын растау басталды", "no"), ("Customer identity verified.", "yes"),
                           ("Личность клиента подтверждена.", "yes"), ("Клиенттің жеке басы расталды.", "yes")]:
        conv = {"channel": "chat", "messages": [{"idx": 0, "id": "0", "speaker": "system", "text": text},
                                                {"idx": 1, "id": "1", "speaker": "operator", "text": "Баланс 100."}]}
        assert checks.c3_verification(conv, flags)[0] == expected, text
