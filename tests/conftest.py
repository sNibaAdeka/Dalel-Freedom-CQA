"""Shared fixtures for core tests (no LLM, no network, no organizer data)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("CQA_PRECEDENTS", "off")  # a local precedent bank (real DEV transcripts) must not leak into tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cqa.models import ChecklistVersion, CriterionEval  # noqa: E402

V1_PATH = ROOT / "data" / "checklists" / "v1.json"


@pytest.fixture
def v1_raw() -> dict:
    return json.loads(V1_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def v1(v1_raw) -> ChecklistVersion:
    return ChecklistVersion.model_validate(v1_raw)


def make_evals(checklist: ChecklistVersion, answers: dict, conv_id: str = "t-001", **extra) -> list[CriterionEval]:
    """answers: {"C1": "yes", "C4": 15, ...}; criteria not listed get no evaluation."""
    out = []
    for cid, answer in answers.items():
        c = checklist.criterion(cid)
        out.append(CriterionEval(conv_id=conv_id, criterion_id=cid, criterion_hash=c.hash, prompt_version="test",
                                 answer=answer, confidence=0.9, **extra))
    return out


@pytest.fixture
def evals_for():
    return make_evals


# PDF example (COMMON.md): C1 yes, C2 N/A, C3 N/A, C4=15, C5 yes, C6=7, C7 yes (+ C8 no) → 85.38
PDF_EXAMPLE = {"C1": "yes", "C2": "na", "C3": "na", "C4": 15, "C5": "yes", "C6": 7, "C7": "yes", "C8": "no"}


@pytest.fixture
def pdf_example() -> dict:
    return dict(PDF_EXAMPLE)


# ------------------------------------------------------------------ synthetic dialogues + fake evaluator (no LLM)

def synth_conv(conv_id: str = "t-001", language: str = "RU", channel: str = "chat"):
    """Authored synthetic dialogue (not organizer data): client, bot, system, operator turns."""
    from cqa.models import Conversation
    texts = [("client", "Здравствуйте, как посмотреть учебный тариф?"),
             ("bot", "Учебный бот: соединяю со специалистом."),
             ("system", "Оператор подключился к чату."),
             ("operator", "Здравствуйте! Меня зовут Алия. Учебный тариф виден в разделе «Тарифы»."),
             ("client", "Спасибо, нашёл."),
             ("operator", "Рада помочь! Хорошего дня."),
             ("system", "Чат закрыт оператором.")]
    msgs = [{"id": f"{conv_id}:{i}", "idx": i, "speaker": s, "text": t, "ts_raw": f"00:{i * 5:02d}"}
            for i, (s, t) in enumerate(texts)]
    return Conversation(id=conv_id, channel=channel, language=language, group_id="TEST", messages=msgs)


class FakeEvaluator:
    """Stand-in for a2's cqa.evaluator.evaluate(conversation, checklist_version, mode, only_criteria, **kw).

    t-001 gets the PDF example (85.38); others full marks; a criterion whose description contains «СТРОГО» → "no".
    answers={conv_id: {criterion_id: answer}} overrides; fail_for=conv ids that raise; one call per conversation.
    """

    def __init__(self, answers=None, fail_for=(), delay: float = 0.0):
        import threading
        self.calls: list[tuple] = []
        self.answers = answers or {}
        self.fail_for = set(fail_for)
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, conversation, checklist_version, mode="fast", only_criteria=None, **kw):
        import time
        ids = list(only_criteria or [c.id for c in checklist_version.criteria])
        with self._lock:
            self.calls.append((conversation.id, tuple(ids), mode, tuple(sorted(kw))))
        if self.delay:
            time.sleep(self.delay)
        if conversation.id in self.fail_for:
            raise RuntimeError("модель недоступна (тест)")
        out = []
        for cid in ids:
            c = checklist_version.criterion(cid)
            if cid in self.answers.get(conversation.id, {}):
                answer = self.answers[conversation.id][cid]
            elif "СТРОГО" in c.description:
                answer = "no"
            elif conversation.id == "t-001" and cid in PDF_EXAMPLE:
                answer = PDF_EXAMPLE[cid]
            elif c.critical:
                answer = "no"
            else:
                answer = max(c.scale) if c.answer_type != "binary" else "yes"
            out.append(CriterionEval(conv_id=conversation.id, criterion_id=cid, criterion_hash=c.hash,
                                     prompt_version="fake-v1", mode=mode, answer=answer, confidence=0.9,
                                     explanation=f"{cid}: тестовое обоснование.", model="fake", cost_kzt=0.5,
                                     raw={"calls": 1 / len(ids), "cache_hits": 0}))
        return out


@pytest.fixture
def fake_evaluator():
    return FakeEvaluator()
