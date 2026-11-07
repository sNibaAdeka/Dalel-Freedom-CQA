"""cqa/engine.py — deterministic total per the organizers' PDF (page 2, «Порядок расчёта»)."""
from __future__ import annotations

import json

import pytest

from cqa.engine import ScoreValidationError, build_cells, is_critical_hit, round_total, score, summarize
from cqa.models import ChecklistVersion, Conversation, Criterion, CriterionEval, Decision
from tests.conftest import make_evals


def _custom(criteria: list[dict], **kw) -> ChecklistVersion:
    return ChecklistVersion(version=kw.pop("version", 7), criteria=[Criterion(**c) for c in criteria], **kw)


def _decision(did, cid, new, action="change", **kw):
    return Decision(id=did, appeal_id=f"ap-{did}", conv_id="t-001", criterion_id=cid, action=action,
                    new_answer=new, reason="решение проверяющего", **kw)


# ------------------------------------------------------------------ PDF example and basic rules

def test_pdf_example_gives_85_38(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example))
    assert s.total == 85.38
    assert s.total_raw == pytest.approx(55.5 / 65 * 100)
    assert s.status == "complete" and s.critical is False and s.checklist_version == 1 and s.conv_id == "t-001"
    assert s.applicable_weight == 65 and s.weight_sum == 100


def test_pdf_example_lines(v1, pdf_example):
    lines = {line.criterion_id: line for line in score(v1, make_evals(v1, pdf_example)).lines}
    assert [*lines] == [f"C{i}" for i in range(1, 9)]
    assert lines["C1"].weight_effective == pytest.approx(10 / 65 * 100, abs=1e-4)
    assert lines["C1"].contribution == pytest.approx(10 / 65 * 100, abs=1e-4)
    for na in ("C2", "C3"):
        assert lines[na].is_na and lines[na].points is None and lines[na].weight_effective == 0
        assert lines[na].contribution == 0 and not lines[na].counts_in_total
    assert (lines["C4"].points, lines["C4"].max_points) == (15, 20)
    assert (lines["C6"].points, lines["C6"].max_points) == (10.5, 15)
    assert lines["C8"].points is None and lines["C8"].counts_in_total is False and lines["C8"].weight == 0


def test_weight_effective_sums_to_100_over_applicable(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example))
    assert sum(line.weight_effective for line in s.lines) == pytest.approx(100, abs=1e-3)
    assert sum(line.contribution for line in s.lines) == pytest.approx(s.total_raw, abs=1e-3)


def test_all_applicable_yes_and_top_steps_give_100(v1):
    answers = {"C1": "yes", "C2": "yes", "C3": "yes", "C4": 20, "C5": "yes", "C6": 10, "C7": "yes", "C8": "no"}
    s = score(v1, make_evals(v1, answers))
    assert s.total == 100.0 and s.status == "complete"
    assert all(line.weight_effective == line.weight for line in s.lines)  # nothing redistributed


def test_all_no_gives_zero(v1):
    answers = {"C1": "no", "C2": "no", "C3": "no", "C4": 0, "C5": "no", "C6": 0, "C7": "no", "C8": "no"}
    assert score(v1, make_evals(v1, answers)).total == 0.0


def test_all_na_total_not_computed():
    cl = _custom([dict(id=f"K{i}", title=f"k{i}", weight=w, na_allowed=True) for i, w in enumerate([30, 30, 40])])
    s = score(cl, [CriterionEval(conv_id="c", criterion_id=f"K{i}", answer="na") for i in range(3)])
    assert s.total is None and s.total_raw is None and s.status == "no_applicable"
    assert any("итог не рассчитывается" in step for step in s.formula_steps)


def test_c4_top_step_gives_full_weight(v1, pdf_example):
    pdf_example["C4"] = 20
    line = next(x for x in score(v1, make_evals(v1, pdf_example)).lines if x.criterion_id == "C4")
    assert line.points == 20 and line.points == line.max_points
    assert line.contribution == pytest.approx(line.weight_effective)


def test_c6_step_4_gives_6_points_of_15(v1, pdf_example):
    pdf_example["C6"] = 4
    line = next(x for x in score(v1, make_evals(v1, pdf_example)).lines if x.criterion_id == "C6")
    assert (line.points, line.max_points) == (6.0, 15)


# ------------------------------------------------------------------ gross errors (critical_when)

def test_c8_yes_sets_critical_flag_and_keeps_total(v1, pdf_example):
    pdf_example["C8"] = "yes"
    s = score(v1, make_evals(v1, pdf_example))
    assert s.critical is True and s.critical_criteria == ["C8"] and s.total == 85.38
    c8 = next(x for x in s.lines if x.criterion_id == "C8")
    assert c8.critical_hit and c8.points is None and c8.contribution == 0
    assert any("грубая ошибка" in step.lower() for step in s.formula_steps)


def test_c8_no_is_not_critical(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example))
    assert s.critical is False and s.critical_criteria == []


def test_critical_uses_critical_when_not_rule_text():
    crit = dict(id="K2", title="Конфиденциальность соблюдена", weight=0, critical=True, critical_when="no",
                critical_rule="Да = нарушение")  # misleading text must not matter
    cl = _custom([dict(id="K1", title="k1", weight=100), crit])
    ok = score(cl, [CriterionEval(conv_id="c", criterion_id="K1", answer="yes"),
                    CriterionEval(conv_id="c", criterion_id="K2", answer="yes")])
    bad = score(cl, [CriterionEval(conv_id="c", criterion_id="K1", answer="yes"),
                     CriterionEval(conv_id="c", criterion_id="K2", answer="no", critical_hit=False)])
    assert ok.critical is False and bad.critical is True and bad.total == ok.total == 100.0
    assert is_critical_hit(cl.criterion("K2"), "no") and not is_critical_hit(cl.criterion("K1"), "no")


def test_weighted_critical_criterion_counts_in_total_and_flags():
    cl = _custom([dict(id="K1", title="k1", weight=60), dict(id="K2", title="k2", weight=40, critical=True,
                                                             critical_when="no")])
    s = score(cl, [CriterionEval(conv_id="c", criterion_id="K1", answer="yes"),
                   CriterionEval(conv_id="c", criterion_id="K2", answer="no")])
    assert s.total == 60.0 and s.critical is True


# ------------------------------------------------------------------ validation

def test_na_where_not_allowed_is_a_validation_error(v1, pdf_example):
    pdf_example["C1"] = "na"
    with pytest.raises(ScoreValidationError, match="N/A не разрешён"):
        score(v1, make_evals(v1, pdf_example))


@pytest.mark.parametrize("cid,value", [("C6", 5), ("C4", 12), ("C4", 25), ("C5", 1), ("C4", "yes")])
def test_invalid_answer_for_type_is_a_validation_error(v1, pdf_example, cid, value):
    pdf_example[cid] = value
    with pytest.raises(ScoreValidationError):
        score(v1, make_evals(v1, pdf_example))


def test_non_strict_mode_treats_invalid_answer_as_missing(v1, pdf_example):
    pdf_example["C6"] = 5
    s = score(v1, make_evals(v1, pdf_example), strict=False)
    assert s.total is None and s.status == "incomplete" and s.missing == ["C6"]
    assert any("C6" in w for w in s.warnings)


# ------------------------------------------------------------------ reviewer decisions

def test_decision_overrides_answer_and_recomputes(v1, pdf_example):
    evals = make_evals(v1, pdf_example)
    s = score(v1, evals, [_decision("d1", "C4", 20, created_at="2026-11-07T20:00:00+05:00")])
    assert s.total == round(60.5 / 65 * 100, 2) == 93.08
    c4 = next(x for x in s.lines if x.criterion_id == "C4")
    assert (c4.answer, c4.original_answer, c4.source, c4.decision_id) == (20, 15, "decision", "d1")
    assert any("проверяющ" in step for step in s.formula_steps)


def test_decision_confirm_keeps_model_answer(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example), [_decision("d1", "C4", None, action="confirm", old_answer=15)])
    c4 = next(x for x in s.lines if x.criterion_id == "C4")
    assert s.total == 85.38 and c4.answer == 15 and c4.source == "decision"


def test_last_decision_wins(v1, pdf_example):
    d_late = _decision("d2", "C5", "yes", created_at="2026-11-07T21:00:00+05:00")
    d_early = _decision("d1", "C5", "no", created_at="2026-11-07T20:00:00+05:00")
    s = score(v1, make_evals(v1, pdf_example), [d_late, d_early])
    assert s.total == 85.38 and next(x for x in s.lines if x.criterion_id == "C5").decision_id == "d2"


def test_decision_to_na_redistributes_weight(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example), [_decision("d1", "C7", "na")])
    assert s.applicable_weight == 60 and s.total == 84.17  # 50.5 / 60 × 100 = 84.1666…


def test_decision_with_invalid_answer_is_rejected(v1, pdf_example):
    with pytest.raises(ScoreValidationError, match="N/A не разрешён"):
        score(v1, make_evals(v1, pdf_example), [_decision("d1", "C1", "na")])


def test_decision_for_old_wording_is_not_applied(v1, pdf_example):
    stale = _decision("d1", "C4", 0, criterion_hash="hash-of-old-wording")
    s = score(v1, make_evals(v1, pdf_example), [stale])
    assert s.total == 85.38 and any("C4" in w and "не применено" in w for w in s.warnings)


def test_decision_can_set_critical_flag(v1, pdf_example):
    s = score(v1, make_evals(v1, pdf_example), [_decision("d1", "C8", "yes")])
    assert s.critical is True and s.total == 85.38


# ------------------------------------------------------------------ missing / stale evaluations

def test_missing_weighted_eval_gives_incomplete(v1, pdf_example):
    del pdf_example["C4"]
    s = score(v1, make_evals(v1, pdf_example))
    assert s.total is None and s.status == "incomplete" and s.missing == ["C4"]


def test_missing_c8_keeps_total_but_marks_incomplete(v1, pdf_example):
    del pdf_example["C8"]
    s = score(v1, make_evals(v1, pdf_example))
    assert s.total == 85.38 and s.status == "incomplete" and s.missing == ["C8"]
    assert any("C8" in w for w in s.warnings)


def test_stale_eval_hash_is_treated_as_missing(v1, pdf_example):
    evals = make_evals(v1, pdf_example)
    evals[3].criterion_hash = "old-hash"
    s = score(v1, evals)
    assert s.total is None and s.missing == ["C4"] and any("переоцен" in w for w in s.warnings)


def test_duplicate_evals_last_wins(v1, pdf_example):
    evals = make_evals(v1, pdf_example) + make_evals(v1, {"C4": 20})
    s = score(v1, evals)
    assert s.total == 93.08 and any("C4" in w for w in s.warnings)


# ------------------------------------------------------------------ rounding

def test_half_up_on_exact_tie_13_125(v1):
    answers = {"C1": "no", "C2": "no", "C3": "no", "C4": "na", "C5": "no", "C6": 7, "C7": "no", "C8": "no"}
    s = score(v1, make_evals(v1, answers))  # 10.5 / 80 × 100 = 13.125 exactly
    assert round(13.125, 2) == 13.12       # Python's banker's rounding would be wrong here
    assert s.total == 13.13


def test_half_up_on_exact_tie_38_125(v1):
    answers = {"C1": "yes", "C2": "no", "C3": "na", "C4": 5, "C5": "no", "C6": 7, "C7": "yes", "C8": "no"}
    assert score(v1, make_evals(v1, answers)).total == 38.13  # 30.5 / 80 × 100 = 38.125


def test_round_total_helper():
    assert round_total(85.384615, 2) == 85.38
    assert round_total(2.675, 2) == 2.68      # float 2.675 is 2.67499… in binary
    assert round_total(85.385, 2) == 85.39
    assert round_total(0.5, 0) == 1.0 and round_total(0.5, 0, "half_even") == 0.0
    assert round_total(None, 2) is None


# ------------------------------------------------------------------ formula, sections, weights ≠ 100

def test_formula_steps_in_russian(v1, pdf_example):
    steps = score(v1, make_evals(v1, pdf_example)).formula_steps
    text = "\n".join(steps)
    assert "C2 и C3 N/A → вес 35 перераспределён между 5 критериями" in text
    assert "85,38" in text and "55,5" in text
    assert any(step.startswith("C6") and "10,5 из 15" in step for step in steps)


def test_sections(v1, pdf_example):
    sections = score(v1, make_evals(v1, pdf_example)).sections
    assert sections == {"Начало и понимание запроса": 100.0, "Безопасность и решение": 75.0,
                        "Ведение и завершение диалога": 87.14, "Грубые нарушения": None}


def test_weights_not_summing_to_100_are_normalized_with_warning():
    cl = _custom([dict(id="K1", title="k1", weight=1), dict(id="K2", title="k2", weight=1),
                  dict(id="K3", title="k3", weight=2)])
    ok = score(cl, [CriterionEval(conv_id="c", criterion_id=k, answer="yes") for k in ("K1", "K2", "K3")])
    half = score(cl, [CriterionEval(conv_id="c", criterion_id="K1", answer="yes"),
                      CriterionEval(conv_id="c", criterion_id="K2", answer="yes"),
                      CriterionEval(conv_id="c", criterion_id="K3", answer="no")])
    assert ok.total == 100.0 and half.total == 50.0
    assert any("≠ 100" in w for w in half.warnings)
    assert [line.weight_effective for line in half.lines] == [25.0, 25.0, 50.0]


def test_numeric_criterion_scores_proportionally():
    cl = _custom([dict(id="K1", title="k1", weight=50, answer_type="numeric", scale=[0, 10]),
                  dict(id="K2", title="k2", weight=50)])
    s = score(cl, [CriterionEval(conv_id="c", criterion_id="K1", answer=7.5),
                   CriterionEval(conv_id="c", criterion_id="K2", answer="yes")])
    assert s.total == 87.5


def test_accepts_dicts_and_serializes(v1_raw, pdf_example):
    cl = ChecklistVersion.model_validate(v1_raw)
    evals = [e.model_dump() for e in make_evals(cl, pdf_example)]
    s = score(v1_raw, evals, [])
    data = json.loads(s.model_dump_json())
    assert data["total"] == 85.38 and len(data["lines"]) == 8 and data["formula_steps"]


# ------------------------------------------------------------------ list-row helpers

def test_cells_use_engine_flags_not_llm_flags(v1, pdf_example):
    pdf_example["C8"] = "yes"
    evals = make_evals(v1, pdf_example)
    for e in evals:
        e.critical_hit = False          # LLM forgot the flag — engine must still raise it
    evals[3].needs_review = True
    s = score(v1, evals, [_decision("d1", "C4", 20)])
    cells = {c.criterion_id: c for c in build_cells(s, evals)}
    assert cells["C8"].critical_hit is True and cells["C4"].answer == 20
    assert cells["C4"].needs_review is False   # resolved by the reviewer decision
    assert [c.criterion_id for c in build_cells(s, evals)] == [f"C{i}" for i in range(1, 9)]


def test_summarize_statuses(v1, pdf_example):
    conv = Conversation(id="t-001", channel="chat", language="RU")
    evals = make_evals(v1, pdf_example)
    row = summarize(conv, score(v1, evals), evals)
    assert (row.total, row.status, row.critical, len(row.cells)) == (85.38, "evaluated", False, 8)
    evals[0].needs_review = True
    assert summarize(conv, score(v1, evals), evals).status == "needs_review"
    partial = evals[:3]
    assert summarize(conv, score(v1, partial), partial).status == "incomplete"
    assert summarize(conv, None, []).status == "pending"
