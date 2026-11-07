"""Deterministic offline answers for the 'mock' provider (no key needed): API tests, UI plumbing, demos without network.
Parses the prompt built by cqa.prompts (criterion blocks + transcript) and answers with simple rules. NOT a quality
evaluator: answers are illustrative and every result is marked as mock by the model name 'mock:*'."""
from __future__ import annotations

import re

LINE = re.compile(r"^\[(?P<id>[^\]]+)\] \S+ (?P<role>OPERATOR|CLIENT|BOT|SYSTEM): (?P<text>.*)$", re.M)
CRIT = re.compile(r"^КРИТЕРИЙ (?P<id>\S+) «(?P<title>[^»]*)».*?^Допустимые значения answer: (?P<allowed>[^\n]*)$", re.M | re.S)


def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:160]


def rule_mock(system: str, user: str, model: str, temperature: float, run: int) -> dict:
    msgs = [m.groupdict() for m in LINE.finditer(user)]
    ops = [m for m in msgs if m["role"] == "OPERATOR"]
    items = []
    for c in CRIT.finditer(user):
        allowed = [a.strip().strip('"') for a in c.group("allowed").split(",")]
        block = c.group(0)
        critical = "НАРУШЕНИЕ ВЫЯВЛЕНО" in block
        nums = [float(a) for a in allowed if re.fullmatch(r"-?\d+(\.\d+)?", a)]
        if critical:
            answer = "no" if "no" in allowed else allowed[0]
        elif nums:
            answer = max(nums)
        else:
            answer = "yes" if "yes" in allowed else allowed[0]
        ev = [{"message_id": ops[0]["id"], "quote": _first_sentence(ops[0]["text"])}] if ops and not critical else []
        rng = [ops[0]["id"], ops[-1]["id"]] if ops and critical else None
        items.append({"criterion_id": c.group("id"), "analysis": "mock: правило по умолчанию", "answer": answer, "evidence": ev,
                      "checked_range": rng, "na_reason": None, "explanation": f"{c.group('title')} — демо-ответ без модели (mock).",
                      "suggestion": None, "confidence": 0.5})
    if not items:
        return {"criteria": []}
    return items[0] if len(items) == 1 and "criteria" not in user.split("Верни JSON строго по схеме:")[-1][:40] else {"criteria": items}
