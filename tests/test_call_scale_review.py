"""Calls: scale answers (C4/C6) always go to human review; chats and binary criteria are untouched."""
from cqa import rules

SCALE = {"id": "C4", "answer_type": "scale", "scale": [0, 5, 10, 15, 20]}
BINARY = {"id": "C1", "answer_type": "binary"}


def test_call_scale_answer_is_routed_to_review():
    reasons = rules.call_scale_review_reasons({"channel": "call"}, SCALE, 20.0)
    assert reasons and "звонок" in reasons[0]


def test_chat_scale_and_call_binary_and_na_are_not_routed():
    assert rules.call_scale_review_reasons({"channel": "chat"}, SCALE, 20.0) == []
    assert rules.call_scale_review_reasons({"channel": "call"}, BINARY, "yes") == []
    assert rules.call_scale_review_reasons({"channel": "call"}, SCALE, "na") == []
    assert rules.call_scale_review_reasons({"channel": "call"}, SCALE, None) == []
