"""Strict review CQA-03 (second half): a chat with an empty/unknown/non-operator disclosure id must not fall through
to the call branch and trust the model's verified_before_disclosure flag (synthetic, invented messages)."""
from __future__ import annotations

import pytest

from cqa import checks, evaluator


def _chat(msgs, channel="chat"):
    return {"id": "s-9", "channel": channel, "language": "RU",
            "messages": [{"id": f"s-9:{i}", "idx": i, "speaker": s, "text": t} for i, (s, t) in enumerate(msgs)]}


NO_EVENT = _chat([("client", "Какой у меня остаток?"), ("operator", "Здравствуйте! На счёте 120 000 тенге.")])
WITH_EVENT = _chat([("client", "Какой у меня остаток?"), ("system", "Личность клиента подтверждена."),
                    ("operator", "Здравствуйте! На счёте 120 000 тенге.")])


@pytest.mark.parametrize("ref", ["does-not-exist", "", "   ", None, "s-9:0"])  # unknown, empty, blank, missing, client line
def test_chat_without_valid_disclosure_id_and_no_event_is_no(ref):
    flags = {"client_specific_disclosure_or_action": True, "verified_before_disclosure": True}
    if ref is not None:
        flags["first_disclosure_message_id"] = ref
    assert checks.c3_verification(NO_EVENT, flags)[0] == "no"


@pytest.mark.parametrize("ref", ["does-not-exist", "", "s-9:1", "s-9:0"])  # unknown, empty, system line, client line
def test_chat_with_event_but_unknown_order_is_undecided_not_yes(ref):
    flags = {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": ref, "verified_before_disclosure": True}
    assert checks.c3_verification(WITH_EVENT, flags) is None


def test_chat_with_valid_operator_id_is_unchanged():
    flags = {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "s-9:2"}
    assert checks.c3_verification(WITH_EVENT, flags)[0] == "yes"
    assert checks.c3_verification(NO_EVENT, dict(flags, first_disclosure_message_id="s-9:1"))[0] == "no"


def test_call_branch_still_uses_model_flag():
    call = dict(NO_EVENT, channel="call")
    flags = {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "does-not-exist"}
    assert checks.c3_verification(call, dict(flags, verified_before_disclosure=True))[0] == "yes"
    assert checks.c3_verification(call, dict(flags, verified_before_disclosure=False))[0] == "no"


def test_undecided_c3_yes_goes_to_review(v1_raw):
    cl = evaluator.as_checklist(v1_raw)
    c3 = next(c for c in cl["criteria"] if c["id"] == "C3")
    item = {"criterion_id": "C3", "answer": "yes", "explanation": "Личность подтверждена.", "confidence": 0.95,
            "evidence": [{"message_id": "s-9:1", "quote": "Личность клиента подтверждена."}],
            "checks": {"client_specific_disclosure_or_action": True, "first_disclosure_message_id": "does-not-exist",
                       "verified_before_disclosure": True}}
    conv = evaluator.as_conversation(WITH_EVENT)
    v = evaluator.validate_item(item, conv, c3)
    assert v["answer"] == "yes" and v.get("rule_review")
    res = evaluator._finalize(conv, c3, [v], v, "yes", 1.0, "fast", {"model": "mock:test"}, {"yes": 1}, None, None)
    assert res["needs_review"] and any("C3" in r for r in res["review_reasons"])
    # no verification event at all: the rule decides "no", never the model's "yes"
    v2 = evaluator.validate_item(item, evaluator.as_conversation(NO_EVENT), c3)
    assert v2["answer"] == "no" and not v2.get("rule_review")
