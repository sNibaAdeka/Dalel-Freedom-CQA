"""Contracts of cqa/models.py: checklist v1 fidelity, hash rules, answer normalization, validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cqa.models import (Appeal, AppealCreate, ChecklistVersion, Conversation, ConversationDetail, Criterion,
                        CriterionEval, Decision, DecisionCreate, Evidence, criterion_hash, normalize_answer)
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- checklist v1 vs PDF

def test_v1_matches_pdf_structure(v1):
    assert [c.id for c in v1.criteria] == [f"C{i}" for i in range(1, 9)]
    assert [c.weight for c in v1.criteria] == [10, 15, 20, 20, 15, 15, 5, 0]
    assert v1.weight_sum == 100
    assert {c.id for c in v1.criteria if c.na_allowed} == {"C2", "C3", "C4", "C7"}
    assert v1.criterion("C4").scale == [0, 5, 10, 15, 20]
    assert v1.criterion("C6").scale == [0, 4, 7, 10]
    assert v1.rounding == {"total": 2, "mode": "half_up"}
    assert all(c.max_points == c.weight for c in v1.criteria)


def test_v1_c8_is_critical_flag_without_points(v1):
    c8 = v1.criterion("C8")
    assert c8.critical and c8.critical_when == "yes" and c8.weight == 0 and c8.answer_type == "binary"
    assert [c.id for c in v1.criteria if c.critical] == ["C8"]


def test_v1_texts_are_verbatim_from_pdf(v1):
    import re
    pdf = (ROOT / "data" / "raw" / "Checklist.txt")
    if not pdf.exists():
        pytest.skip("organizer checklist text not available")
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    text = norm(pdf.read_text(encoding="utf-8"))
    for c in v1.criteria:
        for field in (c.title, c.description, c.na_rule):
            assert not field or norm(field) in text, (c.id, field)
        for step, label in (c.scale_labels or {}).items():
            assert f"{step} {norm(label)}" in text, (c.id, step)


def test_v1_c6_guidance_follows_pdf_page2(v1):
    g = " ".join(v1.criterion("C6").guidance)
    assert "Время ожидания оператора в баллах не учитывается" not in g
    assert "Предупреждение клиента о явном ожидании в ходе диалога — часть C6 (ступень 10)" in g


def test_v1_stored_hashes_match_model_and_generator(v1_raw):
    src = (ROOT / "scripts" / "make_checklist_v1.py").read_text(encoding="utf-8")
    # take the generator's hash function without running the script (it writes the file)
    ns: dict = {"__file__": str(ROOT / "scripts" / "make_checklist_v1.py")}
    exec(src.split("\nfor c in C:")[0], ns)  # noqa: S102 — trusted repo file
    for raw in v1_raw["criteria"]:
        model = Criterion.model_validate(raw)
        assert raw["hash"] == model.hash == criterion_hash(raw) == ns["h"](raw), raw["id"]


# ---------------------------------------------------------------- hash rules

def _crit(**k):
    base = dict(id="C4", section="S", title="Консультация", description="Оцените ответ", answer_type="scale",
                scale=[0, 5, 10, 15, 20], scale_labels={"20": "всё", "0": "ничего"}, weight=20, na_allowed=True,
                na_rule="N/A — нет запроса", guidance=["g1"])
    base.update(k)
    return Criterion.model_validate(base)


def test_hash_ignores_weight_section_id_and_whitespace():
    h = _crit().hash
    assert _crit(weight=35).hash == h
    assert _crit(section="Другая").hash == h
    assert _crit(id="C9").hash == h
    assert _crit(title="  Консультация  ").hash == h
    assert _crit(scale=[20, 15, 10, 5, 0]).hash == h          # order of steps does not matter
    assert _crit(scale=[0.0, 5.0, 10.0, 15.0, 20.0]).hash == h  # int/float form does not matter


@pytest.mark.parametrize("change", [
    {"title": "Другое"}, {"description": "Иначе"}, {"answer_type": "numeric"}, {"scale": [0, 10, 20]},
    {"scale_labels": {"20": "всё иначе", "0": "ничего"}}, {"na_allowed": False}, {"na_rule": "N/A — другое"},
    {"guidance": ["g1", "g2"]}, {"guidance": []},
])
def test_hash_changes_with_prompt_relevant_fields(change):
    assert _crit(**change).hash != _crit().hash


def test_hash_changes_with_critical_polarity():
    a = Criterion(id="X", title="t", critical=True, critical_when="yes")
    b = Criterion(id="X", title="t", critical=True, critical_when="no")
    c = Criterion(id="X", title="t")
    assert len({a.hash, b.hash, c.hash}) == 3


def test_input_hash_is_ignored_and_recomputed():
    assert _crit(hash="stale").hash == _crit().hash != "stale"


# ---------------------------------------------------------------- criterion validation

def test_critical_defaults_and_rules():
    assert Criterion(id="X", title="t", critical=True).critical_when == "yes"
    assert Criterion(id="X", title="t", critical=True, critical_rule="no").critical_when == "no"
    assert Criterion(id="X", title="t", critical=False, critical_when="yes").critical_when is None
    with pytest.raises(ValidationError, match="только для критериев «да/нет»"):
        Criterion(id="X", title="t", critical=True, answer_type="scale", scale=[0, 1])


def test_scale_type_requires_scale_and_binary_drops_it():
    with pytest.raises(ValidationError, match="нужна шкала"):
        Criterion(id="X", title="t", answer_type="scale")
    with pytest.raises(ValidationError, match="больше 0"):
        Criterion(id="X", title="t", answer_type="scale", scale=[0])
    b = Criterion(id="X", title="t", answer_type="binary", scale=[0, 1], scale_labels={"1": "a"})
    assert b.scale is None and b.scale_labels is None
    s = Criterion(id="X", title="t", answer_type="scale", scale="0, 4, 7, 10", scale_labels={"10.0": "top", "5": "gone"})
    assert s.scale == [0, 4, 7, 10] and s.scale_labels == {"10": "top"}


def test_negative_weight_rejected():
    with pytest.raises(ValidationError):
        Criterion(id="X", title="t", weight=-1)


def test_check_answer(v1):
    assert v1.criterion("C1").check_answer("Да") == "yes"
    assert v1.criterion("C4").check_answer("15") == 15.0
    assert v1.criterion("C2").check_answer("N/A") == "na"
    with pytest.raises(ValueError, match="N/A не разрешён"):
        v1.criterion("C1").check_answer("na")
    with pytest.raises(ValueError, match="не входит в ступени"):
        v1.criterion("C6").check_answer(5)
    with pytest.raises(ValueError, match="ожидается «да» или «нет»"):
        v1.criterion("C5").check_answer(10)
    with pytest.raises(ValueError, match="ожидается число"):
        v1.criterion("C4").check_answer("yes")
    numeric = Criterion(id="N", title="t", answer_type="numeric", scale=[0, 10])
    assert numeric.check_answer(7.5) == 7.5
    with pytest.raises(ValueError, match="вне диапазона"):
        numeric.check_answer(11)


def test_checklist_unique_ids_and_sections(v1_raw):
    raw = json.loads(json.dumps(v1_raw))
    raw["criteria"][1]["id"] = "C1"
    with pytest.raises(ValidationError, match="уникальными: C1"):
        ChecklistVersion.model_validate(raw)
    raw = json.loads(json.dumps(v1_raw))
    raw["sections"] = []
    raw["criteria"][0]["section"] = "Новая секция"
    cl = ChecklistVersion.model_validate(raw)
    assert cl.sections[0] == "Новая секция" and len(cl.sections) == 5


# ---------------------------------------------------------------- answers, evals, appeals

@pytest.mark.parametrize("raw,expected", [
    ("Да", "yes"), ("YES", "yes"), ("иә", "yes"), (True, "yes"), ("нет", "no"), ("жоқ", "no"), (False, "no"),
    ("N/A", "na"), ("н/п", "na"), ("na", "na"), (15, 15.0), ("7", 7.0), ("7,0", 7.0), (None, None),
])
def test_normalize_answer(raw, expected):
    assert normalize_answer(raw) == expected


@pytest.mark.parametrize("raw", ["partial", "может быть", float("nan"), [1]])
def test_normalize_answer_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_answer(raw)


def test_eval_normalizes_and_allows_any_evidence_role():
    ev = CriterionEval(conv_id="c", criterion_id="C7", answer="Нет", confidence=1.7, checked_range=["c:3", "c:9"],
                       evidence=[Evidence(message_id="c:9", quote="Чат закрыт", speaker="system", valid=True),
                                 {"message_id": "c:1", "quote": "x", "speaker": "client"},
                                 {"message_id": "c:2", "quote": "y", "speaker": "bot"}],
                       raw={"criteria": []}, cost_usd=0.01)
    assert ev.answer == "no" and ev.confidence == 1.0 and ev.checked_range == ("c:3", "c:9")
    assert [e.speaker for e in ev.evidence] == ["system", "client", "bot"]
    dumped = json.loads(ev.model_dump_json())
    assert dumped["checked_range"] == ["c:3", "c:9"] and dumped["raw"] == {"criteria": []}


def test_decision_requires_reason_and_answer_for_change():
    with pytest.raises(ValidationError, match="укажите новый ответ"):
        DecisionCreate(appeal_id="a1", action="change", reason="ошибка модели")
    with pytest.raises(ValidationError):
        DecisionCreate(appeal_id="a1", action="confirm", reason="   ")
    d = Decision(id="d1", appeal_id="a1", action="change", new_answer="Да", old_answer="нет", reason="видно в [5]",
                 criterion_id="C1", conv_id="c")
    assert (d.new_answer, d.old_answer) == ("yes", "no")
    assert DecisionCreate(appeal_id="a1", action="confirm", reason="ок").new_answer is None


def test_appeal_and_conversation_normalization():
    with pytest.raises(ValidationError):
        AppealCreate(conv_id="c", criterion_id="C1", comment="  ")
    a = Appeal(id="ap1", conv_id="c", criterion_id="C1", comment="не согласен", original_answer="Нет")
    assert a.status == "open" and a.original_answer == "no"
    conv = Conversation(id="p4-ru-001", channel="voice call", language="ru",
                        messages=[{"id": "p4-ru-001:0", "idx": 0, "speaker": "System", "text": "Запись начата"}])
    assert (conv.channel, conv.language, conv.messages[0].speaker) == ("call", "RU", "system")
    assert Conversation(id="x", channel="agent chat", language="kk").language == "KZ"


def test_detail_shape_matches_frontend_keys(v1):
    detail = ConversationDetail(id="x", channel="chat", language="EN", checklist=v1)
    keys = set(json.loads(detail.model_dump_json()))
    assert {"id", "channel", "language", "group_id", "messages", "meta", "audio_url", "evals", "score", "checklist",
            "appeals", "decisions", "audit"} <= keys
