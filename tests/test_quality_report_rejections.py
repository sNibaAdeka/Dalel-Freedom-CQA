"""Strict review M1: finally rejected answers stay in the denominator and never count as accepted matches.

Synthetic data only (invented dialogue IDs and texts); no providers, no organizer transcripts.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("quality_report_m1", ROOT / "scripts/quality_report.py")
Q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(Q)

CRITERIA = {
    "C1": {"id": "C1", "answer_type": "binary", "na_allowed": False, "weight": 10},
    "C2": {"id": "C2", "answer_type": "binary", "na_allowed": True, "weight": 10},
}
REJECTED = "ответ модели не прошёл проверку: цитата «выдуманная фраза» не найдена дословно в реплике [syn:3]"


def write_run(tmp_path, evals_by_dialogue):
    rows = [json.dumps({"_meta": {"name": "synthetic-m1"}}, ensure_ascii=False)]
    for conv_id, evals in evals_by_dialogue.items():
        rows.append(json.dumps({"conv_id": conv_id, "evals": evals}, ensure_ascii=False))
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return Q.load_run(path)


def test_final_rejection_is_an_error_not_an_accepted_match(tmp_path):
    run = write_run(tmp_path, {"syn-a": [
        {"criterion_id": "C1", "answer": "yes", "needs_review": True, "review_reasons": [REJECTED], "evidence": []},
    ]})
    assert Q.prediction(run, "syn-a", CRITERIA["C1"]) == (None, "errors")
    cells = Q.measured_cells(run, {("syn-a", "C1"): "yes"}, ["syn-a"], {"C1": CRITERIA["C1"]})
    counts = Q.aggregate(cells)["counts"]
    # Same answer as the reference, but rejected: denominator keeps it, numerator does not.
    assert counts == {"matched": 0, "total": 1, "missing": 0, "errors": 1, "unlabelled": 0}


@pytest.mark.parametrize("reasons", [
    ["низкая уверенность модели 0.41"],
    ["правило по ключевым словам ожидает «no»"],
    ["отмечено грубое нарушение — подтвердите вручную"],
    [],
])
def test_ordinary_review_flag_is_not_an_error(tmp_path, reasons):
    run = write_run(tmp_path, {"syn-b": [
        {"criterion_id": "C1", "answer": "yes", "needs_review": bool(reasons), "review_reasons": reasons,
         "evidence": [{"message_id": "syn-b:1", "quote": "придуманная реплика", "valid": True}]},
    ]})
    assert Q.prediction(run, "syn-b", CRITERIA["C1"]) == ("yes", "valid")


def test_answer_accepted_after_retry_counts(tmp_path):
    # Earlier attempts failed (not stored in slim artifacts); the final answer has valid evidence and no marker.
    run = write_run(tmp_path, {"syn-c": [
        {"criterion_id": "C1", "answer": "no", "needs_review": False, "review_reasons": [],
         "evidence": [{"message_id": "syn-c:2", "quote": "выдуманный ответ оператора", "valid": True}], "calls": 2},
    ]})
    cells = Q.measured_cells(run, {("syn-c", "C1"): "no"}, ["syn-c"], {"C1": CRITERIA["C1"]})
    assert Q.aggregate(cells)["counts"]["matched"] == 1


@pytest.mark.parametrize("entry", [
    {"answer": "yes", "evidence": [{"message_id": "syn-d:1", "quote": "фраза", "valid": False}]},
    {"answer": "yes", "errors": ["invented evidence"]},
    {"answer": "yes", "validation_status": "rejected"},
    {"answer": "yes", "review_reasons": ["оценка не получена: таймаут"]},
    {"answer": "yes", "review_reasons": ["схема CriterionEval: invalid field"]},
    {"answer": "yes", "review_reasons": "  Ответ модели не прошёл проверку: N/A не разрешён"},
])
def test_other_final_rejection_markers(tmp_path, entry):
    run = write_run(tmp_path, {"syn-d": [{"criterion_id": "C1", **entry}]})
    assert Q.prediction(run, "syn-d", CRITERIA["C1"]) == (None, "errors")


def test_loader_keeps_only_a_boolean_not_the_texts(tmp_path):
    run = write_run(tmp_path, {"syn-e": [
        {"criterion_id": "C1", "answer": "yes", "review_reasons": [REJECTED], "explanation": "секрет",
         "evidence": [{"message_id": "syn-e:1", "quote": "выдуманная цитата", "valid": True}]},
    ]})
    entry = run["rows"]["syn-e"]["evals"]["C1"]
    assert entry["final_validation_failed"] is True
    assert "review_reasons" not in entry and "evidence" not in entry and "explanation" not in entry


def test_rejected_cell_excluded_from_score_mae_pairing(tmp_path):
    reference = {("syn-f", "C1"): "yes", ("syn-f", "C2"): "no"}
    ok = write_run(tmp_path, {"syn-f": [{"criterion_id": "C1", "answer": "yes"}, {"criterion_id": "C2", "answer": "no"}]})
    assert Q.score_mae(ok, reference, ["syn-f"], CRITERIA) == {"value": 0.0, "count": 1}
    bad = write_run(tmp_path, {"syn-f": [{"criterion_id": "C1", "answer": "yes", "review_reasons": [REJECTED]},
                                         {"criterion_id": "C2", "answer": "no"}]})
    assert Q.score_mae(bad, reference, ["syn-f"], CRITERIA) == {"value": None, "count": 0}


def test_local_snapshot_note_is_computed_not_hardcoded():
    report = {"id": "local-x", "counts": {"matched": 7, "total": 12, "missing": 0, "errors": 2, "unlabelled": 2},
              "latency": {"mean_seconds": 120.4}}
    payload = {"profile_catalog": {"local": {"report_id": "local-x"}}}
    note = Q.local_note(payload, {"local-x": report})
    assert "7/10 = 70,0%" in note and "отклонённых ответов 2" in note and "120 с на диалог" in note
    assert "62,3" not in note
    assert Q.local_note({"profile_catalog": {}}, {}).startswith("Локальный Ollama")


def test_published_quality_json_counts_rejections_for_local_model():
    """The regenerated aggregate must reflect M1 (qwen 99 raw -> 96 accepted, 3 errors, same denominator)."""
    payload = json.loads((ROOT / "web/data/quality.json").read_text(encoding="utf-8"))
    local_id = payload["profile_catalog"]["local"]["report_id"]
    counts = next(r for r in payload["reports"] if r["id"] == local_id)["counts"]
    assert counts["total"] - counts["unlabelled"] == 159
    assert counts["errors"] == 3 and counts["matched"] == 96
    cloud_id = payload["profile_catalog"]["cloud"]["report_id"]
    assert next(r for r in payload["reports"] if r["id"] == cloud_id)["counts"]["matched"] == 152
