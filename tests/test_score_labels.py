"""scripts/score_labels.py on synthetic labels and a synthetic model run (no transcripts, no network)."""
import json
from pathlib import Path

import pytest

from cqa.models import ChecklistVersion
from scripts import score_labels as sl

CL = ChecklistVersion.model_validate_json((Path(__file__).resolve().parents[1] / "data/checklists/v1.json").read_text())
FULL = {"C1": "yes", "C2": "na", "C3": "na", "C4": 15, "C5": "yes", "C6": 7, "C7": "yes", "C8": "no"}   # total 85.38


def rows(annotator, conv, answers, **kw):
    return [{"conv_id": conv, "criterion_id": c, "answer": a, "annotator": annotator, "checklist_version": 1,
             "criterion_hash": CL.criterion(c).hash, "annotated_at": "2026-11-08T09:00:00Z", **kw}
            for c, a in answers.items()]


def write(path, items):
    path.write_text("\n".join(json.dumps(r) for r in items) + "\n", encoding="utf-8")
    return path


def run_file(path, per_conv):
    lines = [{"_meta": {"name": "x"}}] + [{"conv_id": c, "evals": [{"criterion_id": k, "answer": v} for k, v in a.items()]}
                                          for c, a in per_conv.items()]
    return write(path, lines)


@pytest.fixture
def report(tmp_path):
    a = write(tmp_path / "a.jsonl", rows("ann-a", "d1", FULL) + rows("ann-a", "d2", {**FULL, "C1": "no"}))
    b = write(tmp_path / "b.jsonl", rows("ann-b", "d1", FULL) + rows("ann-b", "d2", {**FULL, "C1": "yes", "C6": 7}))
    model = run_file(tmp_path / "run.jsonl", {"d1": {**FULL, "C4": 10, "C8": "yes"}, "d2": {**FULL, "C1": "no"}})
    return sl.build_report([a, b], [model], CL)


def test_agreement_counts_and_disputed_cell(report):
    pair = report["agreement"]["pairs"][0]
    assert pair["shared_cells"] == 16 and pair["overall"]["agreed"] == 15           # only d2/C1 differs
    assert pair["per_criterion"]["C1"]["compared"] == 2 and pair["per_criterion"]["C1"]["agreed"] == 1
    assert report["disputed"] == [{"conv_id": "d2", "criterion_id": "C1", "answers": {"ann-a": "no", "ann-b": "yes"}}]
    assert report["labels"]["consensus_cells"] == 15


def test_model_accuracy_uses_consensus_only(report):
    e = report["runs"]["run.jsonl"]["vs_consensus"]
    assert e["overall"]["n"] == 15 and e["overall"]["correct"] == 13                # d1: C4 and C8 wrong
    assert {x["criterion_id"] for x in e["errors"]} == {"C4", "C8"}
    assert e["overall"]["mae_numeric"] == pytest.approx(5 / 4)                      # numeric cells C4,C6 in d1 + C4,C6 in d2: |10-15|=5
    t = e["total_score"]                                                             # only d1 is fully agreed
    assert t["dialogues"] == 1 and t["rows"][0]["human"] == 85.38 and t["rows"][0]["model"] < 85.38


def test_total_score_mae_on_fully_labeled_dialogue(tmp_path):
    a = write(tmp_path / "a.jsonl", rows("ann-a", "d1", FULL))
    model = run_file(tmp_path / "run.jsonl", {"d1": {**FULL, "C4": 10}})
    r = sl.build_report([a], [model], CL)
    t = r["runs"]["run.jsonl"]["vs_consensus"]["total_score"]
    assert t["dialogues"] == 1 and t["rows"][0]["human"] == 85.38 and t["mae"] > 0
    assert r["agreement"] is None and r["runs"]["run.jsonl"]["vs_annotator"]["ann-a"]["accuracy_pct"] == pytest.approx(87.5)


def test_bad_rows_are_rejected_not_scored(tmp_path):
    items = (rows("ann-a", "d1", {"C1": "yes"}) + rows("ann-a", "d1", {"C2": "na"}, criterion_hash="stale")
             + rows("ann-a", "d1", {"C1": "maybe"}) + rows("ann-a", "d1", {"C3": "yes"}, checklist_version=2)
             + rows("ann-a", "d1", {"C1": "no"}, annotated_at="2026-11-08T08:00:00Z"))   # older repeat of d1/C1
    cells, rejected = sl.load_labels([write(tmp_path / "a.jsonl", items)], CL)
    assert cells == {("ann-a", "d1", "C1"): "yes"}                                  # the latest annotated_at wins
    assert rejected["stale_criterion_hash"] == 1 and rejected["invalid_answer"] == 1
    assert rejected["other_checklist_version"] == 1


def test_group_suffixed_label_id_matches_run_id_and_control_json(tmp_path):
    a = write(tmp_path / "a.jsonl", rows("ann-a", "d1@CTRL", FULL))
    ctrl = {"conversations": [{"conversation_id": "d1", "criteria": [{"criterion_id": k, "answer": v} for k, v in FULL.items()]}]}
    (tmp_path / "control_CTRL.json").write_text(json.dumps(ctrl), encoding="utf-8")
    r = sl.build_report([a], [tmp_path / "control_CTRL.json"], CL)
    assert r["runs"]["control_CTRL.json"]["vs_consensus"]["overall"]["accuracy_pct"] == 100.0


def test_missing_model_answer_is_reported_not_counted_wrong(tmp_path):
    a = write(tmp_path / "a.jsonl", rows("ann-a", "d1", {"C1": "yes", "C5": "yes"}))
    model = run_file(tmp_path / "run.jsonl", {"d1": {"C1": "yes", "C5": None}, "d9": {"C1": "no"}})
    e = sl.build_report([a], [model], CL)["runs"]["run.jsonl"]["vs_consensus"]
    assert e["overall"]["n"] == 1 and e["reference_cells_without_model_answer"] == [{"conv_id": "d1", "criterion_id": "C5"}]


def test_cli_writes_report_without_transcripts(tmp_path, capsys):
    a = write(tmp_path / "a.jsonl", rows("ann-a", "d1", FULL))
    model = run_file(tmp_path / "run.jsonl", {"d1": FULL})
    out = tmp_path / "r.json"
    assert sl.main([str(a), "--run", str(model), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["runs"]["run.jsonl"]["vs_consensus"]["overall"]["accuracy_pct"] == 100.0
    assert "точность 100.0%" in capsys.readouterr().out
