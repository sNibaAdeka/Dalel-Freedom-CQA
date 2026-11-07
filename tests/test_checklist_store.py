"""cqa/checklist_store.py — every kind of checklist change (this is what the control asks for)."""
from __future__ import annotations

import json

import pytest

from cqa.checklist_store import ChecklistStore, apply_changes, diff, load_checklist, validate_body
from cqa.db import Database
from cqa.engine import score
from tests.conftest import PDF_EXAMPLE, make_evals

NEW_C5 = {"title": "Проактивность", "description": "Да — оператор сам предложил полезный следующий шаг.",
          "answer_type": "binary"}


@pytest.fixture
def store(tmp_path):
    return ChecklistStore(Database(tmp_path / "cqa.sqlite"))


def _all_reusable_except(d, ids):
    assert sorted(d.needs_reeval) == sorted(ids)
    return d


# ------------------------------------------------------------------ seed / active

def test_seed_v1_and_activate(store):
    v1 = store.active()
    assert v1.version == 1 and v1.official and len(v1.criteria) == 8 and store.active_version() == 1
    assert [c.version for c in store.versions()] == [1]
    assert load_checklist().model_dump() == v1.model_dump()


def test_v1_is_resynced_from_file(tmp_path):
    db = Database(tmp_path / "cqa.sqlite")
    ChecklistStore(db)
    tampered = db.get_checklist_version(1)
    tampered.criteria[0].title = "Подменено"
    db.save_checklist_version(tampered)
    assert ChecklistStore(db).get(1).criterion("C1").title == "Установление контакта"
    assert any(e.event == "checklist.v1_synced" for e in db.list_audit(entity="checklist"))


def test_activate_and_unknown_version(store):
    res = store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 10}], change_note="тест")
    assert store.active_version() == 1
    store.activate(res.checklist.version)
    assert store.active().version == 2 and store.latest().version == 2
    with pytest.raises(KeyError):
        store.get(99)
    assert store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 12}], activate=True).checklist.version == 3
    assert store.active_version() == 3


# ------------------------------------------------------------------ each kind of change

def test_weight_change_needs_no_reeval_and_rescores_with_old_evals(store, v1):
    res = store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 20}])
    d = res.diff
    assert d.needs_reeval == [] and d.rescore_only == ["C1"] and len(d.reusable) == 8
    assert d.changed[0].fields == ["weight"] and d.changed[0].needs_reeval is False
    assert any("≠ 100" in w for w in res.warnings)
    v2 = res.checklist
    assert v2.criterion("C1").hash == v1.criterion("C1").hash and v2.criterion("C1").max_points == 20
    s = score(v2, make_evals(v1, PDF_EXAMPLE))        # old evaluations, new weights
    assert s.total == 87.33                            # 65.5 / 75 × 100


def test_section_change_needs_no_reeval(store):
    d = store.apply([{"op": "update", "criterion_id": "C7", "fields": {"section": "Завершение"}}]).diff
    assert d.needs_reeval == [] and d.rescore_only == ["C7"] and d.changed[0].fields == ["section"]
    assert "sections" in d.checklist_fields


def test_wording_change_reevaluates_only_that_criterion(store):
    d = store.apply([{"op": "update", "criterion_id": "C4",
                      "fields": {"description": "Оцените, решён ли запрос клиента по существу."}}]).diff
    _all_reusable_except(d, ["C4"])
    assert d.changed[0].fields == ["description"] and d.changed[0].needs_reeval
    assert len(d.reusable) == 7


def test_guidance_change_reevaluates(store):
    d = store.apply([{"op": "update", "criterion_id": "C1",
                      "fields": {"guidance": ["Имя оператора обязательно в первом ответе."]}}]).diff
    _all_reusable_except(d, ["C1"])


def test_replace_criterion_same_id(store, v1):
    res = store.apply([{"op": "replace", "criterion_id": "C5", "criterion": NEW_C5}],
                      change_note="Замена C5 по запросу организаторов")
    v2 = res.checklist
    c5 = v2.criterion("C5")
    assert c5.title == "Проактивность" and c5.weight == 15 and c5.section == v1.criterion("C5").section
    _all_reusable_except(res.diff, ["C5"])
    assert {"title", "description"} <= set(res.diff.changed[0].fields)
    assert v2.change_note == "Замена C5 по запросу организаторов" and v2.parent_version == 1
    # old C5 evaluation is stale for v2 → total waits for the re-evaluation of C5 only
    s = score(v2, make_evals(v1, PDF_EXAMPLE))
    assert s.total is None and s.missing == ["C5"]
    fresh = make_evals(v2, {"C5": "no"})
    s2 = score(v2, [e for e in make_evals(v1, PDF_EXAMPLE) if e.criterion_id != "C5"] + fresh)
    assert s2.total == round(40.5 / 65 * 100, 2)


def test_replace_criterion_with_new_id(store):
    d = store.apply([{"op": "replace", "criterion_id": "C5", "criterion": {**NEW_C5, "id": "C9"}}]).diff
    assert [c["id"] for c in d.added] == ["C9"] and [c["id"] for c in d.removed] == ["C5"]
    assert d.needs_reeval == ["C9"] and d.added[0]["weight"] == 15


def test_add_criterion(store):
    res = store.apply([{"op": "add", "after": "C7", "criterion": {"title": "Проверка понимания", "weight": 5,
                        "description": "Да — оператор убедился, что клиент понял ответ.", "na_allowed": True}}])
    v2 = res.checklist
    assert [c.id for c in v2.criteria][-2:] == ["C9", "C8"]
    assert res.diff.needs_reeval == ["C9"] and len(res.diff.reusable) == 8
    assert any("≠ 100" in w for w in res.warnings) and any("C9: N/A разрешён" in w for w in res.warnings)
    with pytest.raises(ValueError, match="уже есть"):
        store.apply([{"op": "add", "criterion": {"id": "C1", "title": "дубль"}}])


def test_remove_criterion_rescores_without_llm(store, v1):
    res = store.apply([{"op": "remove", "criterion_id": "C2"}])
    assert [c["id"] for c in res.diff.removed] == ["C2"] and res.diff.needs_reeval == []
    s = score(res.checklist, make_evals(v1, PDF_EXAMPLE))  # C2 was N/A → same applicable set
    assert s.total == 85.38 and s.weight_sum == 85


def test_change_type_binary_to_scale(store):
    with pytest.raises(ValueError, match="нужна шкала"):
        store.apply([{"op": "set_type", "criterion_id": "C2", "answer_type": "scale"}])
    res = store.apply([{"op": "set_type", "criterion_id": "C2", "answer_type": "scale", "scale": [0, 1, 2],
                        "scale_labels": {"2": "всё уточнил", "1": "частично", "0": "не уточнил"}}])
    c2 = res.checklist.criterion("C2")
    assert c2.answer_type == "scale" and c2.scale == [0, 1, 2]
    _all_reusable_except(res.diff, ["C2"])
    assert {"answer_type", "scale", "scale_labels"} <= set(res.diff.changed[0].fields)


def test_change_type_scale_to_binary_drops_scale(store):
    res = store.apply([{"op": "set_type", "criterion_id": "C6", "answer_type": "binary"}])
    c6 = res.checklist.criterion("C6")
    assert c6.answer_type == "binary" and c6.scale is None and c6.scale_labels is None
    _all_reusable_except(res.diff, ["C6"])


def test_change_scale_steps(store):
    res = store.apply([{"op": "set_scale", "criterion_id": "C6", "scale": [0, 5, 10]}])
    c6 = res.checklist.criterion("C6")
    assert c6.scale == [0, 5, 10] and set(c6.scale_labels) == {"0", "10"}  # labels of removed steps dropped
    _all_reusable_except(res.diff, ["C6"])


def test_toggle_na(store):
    res = store.apply([{"op": "set_na", "criterion_id": "C5", "na_allowed": True,
                        "na_rule": "N/A — клиент не выражал недовольства."}])
    assert res.checklist.criterion("C5").na_allowed
    _all_reusable_except(res.diff, ["C5"])
    res2 = store.apply([{"op": "set_na", "criterion_id": "C2", "na_allowed": False, "na_rule": ""}])
    _all_reusable_except(res2.diff, ["C2"])


def test_critical_polarity_change_and_default_warning(store):
    res = store.apply([{"op": "update", "criterion_id": "C5", "fields": {"critical": True}}])
    assert res.checklist.criterion("C5").critical_when == "yes"
    assert any("C5" in w and "critical_when" in w for w in res.warnings)
    _all_reusable_except(res.diff, ["C5"])
    res2 = store.apply([{"op": "update", "criterion_id": "C8", "fields": {"critical_when": "no"}}])
    _all_reusable_except(res2.diff, ["C8"])


def test_rename_id_with_same_content_reuses_evaluations(store):
    d = store.apply([{"op": "update", "criterion_id": "C7", "fields": {"id": "C7a"}}]).diff
    assert d.needs_reeval == [] and "C7a" in d.reusable and [c["id"] for c in d.removed] == ["C7"]


def test_invalid_ops(store):
    with pytest.raises(ValueError, match="Неизвестная операция"):
        store.apply([{"op": "explode"}])
    with pytest.raises(ValueError, match="не найден"):
        store.apply([{"op": "remove", "criterion_id": "C42"}])


# ------------------------------------------------------------------ UI path: full body

def test_create_from_ui_body_recomputes_hash_and_diffs_against_base(store, v1_raw):
    body = json.loads(json.dumps(v1_raw))
    body.pop("created_at")
    body["criteria"][3]["description"] = "Новая формулировка консультации."
    body["criteria"][3]["hash"] = "stale-from-ui"
    body["criteria"][0]["weight"] = 10
    res = store.create(body)                  # body.version == 1 → base 1
    cl = res.checklist
    assert cl.version == 2 and cl.parent_version == 1 and not cl.official and cl.created_at
    assert cl.criterion("C4").hash != "stale-from-ui"
    assert res.diff.needs_reeval == ["C4"] and res.diff.from_version == 1 and res.diff.to_version == 2
    assert "C4 изменён" in cl.change_note      # echoed base note replaced by a description of the edit
    resp = res.response()
    assert resp["version"] == 2 and resp["diff"]["from"] == 1 and resp["diff"]["needs_reeval"] == ["C4"]


def test_create_rejects_invalid_body(store, v1_raw):
    body = json.loads(json.dumps(v1_raw))
    body["criteria"][1]["id"] = "C1"
    with pytest.raises(ValueError, match="уникальными"):
        store.create(body)
    body = json.loads(json.dumps(v1_raw))
    body["criteria"] = []
    with pytest.raises(ValueError):
        store.create(body)


def test_create_without_changes_warns(store, v1_raw):
    res = store.create(v1_raw, change_note="повтор")
    assert res.diff.needs_reeval == [] and any("Изменений" in w for w in res.warnings)


def test_audit_events(store):
    res = store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 10}], activate=True)
    events = [e.event for e in store.db.list_audit(entity="checklist")]
    assert events == ["checklist.created", "checklist.activated"]
    assert store.db.list_audit(entity="checklist")[0].payload["version"] == res.checklist.version


# ------------------------------------------------------------------ pure helpers

def test_diff_identical_is_empty(v1):
    d = diff(v1, v1)
    assert not d.added and not d.removed and not d.changed and d.needs_reeval == [] and len(d.reusable) == 8


def test_apply_changes_is_pure(v1):
    before = v1.model_dump()
    draft, _ = apply_changes(v1, [{"op": "remove", "criterion_id": "C2"}])
    assert v1.model_dump() == before and draft.version == 0 and len(draft.criteria) == 7


def test_validate_body_warnings(v1_raw):
    body = json.loads(json.dumps(v1_raw))
    body["criteria"].append({"id": "C9", "title": "Без описания", "weight": 0})
    _, warnings = validate_body(body)
    assert any("C9: нет описания" in w for w in warnings) and any("C9: вес 0" in w for w in warnings)
