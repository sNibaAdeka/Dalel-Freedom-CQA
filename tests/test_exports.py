"""Export contract on synthetic saved snapshots and a temp DB; no run manager, LLM or app wiring."""
from __future__ import annotations

import copy
import csv
import io
import re
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.exports import router
from api.repo import Repo
from cqa.db import Database


@pytest.fixture
def snapshot():
    answers = [
        {"criterion_id": "C1", "answer": "yes", "original_answer": "no", "source": "decision",
         "points": 25.0, "weight": 25.0, "critical_hit": False},
        {"criterion_id": "C2", "answer": "na", "original_answer": "na", "source": "model",
         "points": None, "weight": 25.0, "critical_hit": False},
        {"criterion_id": "C4", "answer": 15, "original_answer": 15, "source": "model",
         "points": 37.5, "weight": 50.0, "critical_hit": False},
        {"criterion_id": "C8", "answer": "yes", "original_answer": "yes", "source": "model",
         "points": None, "weight": 0.0, "critical_hit": True},
    ]
    return {
        "run_id": "run-export-test", "version": 7, "profile": "local", "mode": "accurate:local",
        "status": "completed", "started_at": "2026-11-07T01:00:00Z", "finished_at": "2026-11-07T01:00:02Z",
        "per_conv": [{
            "id": "диалог-Қазақ", "group_id": "CTRL-Қазақ", "language": "KZ", "channel": "chat",
            "status": "done", "old_total": 0, "new_total": 1,  # neither is a result snapshot
            "score": {"conv_id": "диалог-Қазақ", "checklist_version": 7, "total": 83.33,
                      "total_raw": 83.33333333333333, "status": "complete", "critical": True,
                      "critical_criteria": ["C8"], "lines": copy.deepcopy(answers), "sections": {},
                      "weight_sum": 100.0, "applicable_weight": 75.0, "missing": [],
                      "warnings": [], "formula_steps": []},
            "answers": answers,
        }],
    }


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "exports.sqlite")
    app = FastAPI()
    app.state.cqa = SimpleNamespace(repo=Repo(db))
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        yield SimpleNamespace(db=db, repo=app.state.cqa.repo, client=client, app=app)


def save(env, snapshot):
    env.db.save_run(snapshot["run_id"], snapshot["status"], snapshot, version=snapshot.get("version"))


def download(env, snapshot, fmt="json"):
    return env.client.get(f"/api/runs/{quote(snapshot['run_id'], safe='')}/export", params={"format": fmt})


def csv_rows(response):
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


def test_json_preserves_saved_answers_score_and_provenance(env, snapshot):
    save(env, snapshot)
    before = env.db.get_run(snapshot["run_id"])
    response = download(env, snapshot)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["run_id"] == snapshot["run_id"]
    assert payload["checklist_version"] == 7
    assert payload["run_status"] == "completed"
    assert payload["profile"] == "local" and payload["mode"] == "accurate:local"
    assert payload["started_at"] == snapshot["started_at"]
    assert payload["finished_at"] == snapshot["finished_at"]
    assert payload["provenance"] == {"source": "saved_run_snapshot", "official_organizer_format": False}
    assert {"results", "answers", "critical", "status", "errors", "privacy", "csv"} <= payload["policy"].keys()
    conv = payload["per_conv"][0]
    assert (conv["conv_id"], conv["group"], conv["lang"], conv["channel"]) == (
        "диалог-Қазақ", "CTRL-Қазақ", "KZ", "chat")
    assert conv["answers"] == snapshot["per_conv"][0]["answers"]
    assert conv["score"]["total"] == 83.33 and conv["score"]["critical"] is True
    assert conv["status"] == "done" and conv["score"]["status"] == "complete"
    assert env.db.get_run(snapshot["run_id"]) == before  # read-only, including updated_at
    assert env.client.get(f"/api/runs/{snapshot['run_id']}/export").content == response.content


def test_csv_columns_bom_unicode_and_criterion_values(env, snapshot):
    save(env, snapshot)
    response = download(env, snapshot, "csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    rows = csv_rows(response)
    assert list(rows[0]) == ["conv_id", "group", "lang", "channel", "checklist_version", "run_id", "run_status",
                             "criterion_id", "answer", "original_answer", "total", "critical", "status"]
    assert len(rows) == 4
    assert rows[0] == {"conv_id": "диалог-Қазақ", "group": "CTRL-Қазақ", "lang": "KZ", "channel": "chat",
                       "checklist_version": "7", "run_id": "run-export-test", "run_status": "completed",
                       "criterion_id": "C1", "answer": "yes", "original_answer": "no",
                       "total": "83.33", "critical": "True", "status": "done"}
    assert rows[1]["answer"] == "na" and rows[2]["answer"] == "15"
    assert rows[3]["criterion_id"] == "C8" and rows[3]["critical"] == "True"


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_snapshot_is_independent_of_current_state(env, snapshot, monkeypatch, fmt):
    cid = snapshot["per_conv"][0]["id"]
    env.db.upsert_conversation({"id": cid, "group_id": "CHANGED", "language": "EN", "channel": "call"})
    env.db.save_eval({"conv_id": cid, "criterion_id": "C1", "criterion_hash": "new-hash", "prompt_version": "new",
                      "mode": "accurate:local", "answer": "no"})
    env.db.set_setting("active_checklist_version", 99)
    save(env, snapshot)
    first = download(env, snapshot, fmt)
    assert first.status_code == 200
    env.db.save_eval({"conv_id": cid, "criterion_id": "C1", "criterion_hash": "new-hash", "prompt_version": "new",
                      "mode": "accurate:local", "answer": "yes"})
    env.db.add_decision({"id": "later-decision", "appeal_id": "later-appeal", "conv_id": cid,
                         "criterion_id": "C1", "action": "change", "new_answer": "no",
                         "reason": "Synthetic decision after the saved run"})
    env.db.upsert_conversation({"id": cid, "group_id": "CHANGED-AGAIN", "language": "EN", "channel": "call"})
    env.db.set_setting("active_checklist_version", 100)

    def forbidden(*args, **kwargs):
        pytest.fail("Export read mutable state instead of the saved run")

    for name in ("get_conversation", "get_eval", "list_evals", "latest_evals", "list_decisions",
                 "get_active_version", "get_checklist_version", "get_setting"):
        monkeypatch.setattr(env.db, name, forbidden)
    env.app.state.cqa.runs = SimpleNamespace(status=forbidden)
    assert download(env, snapshot, fmt).content == first.content


@pytest.mark.parametrize("change", ["nan_total", "null_answer", "object_answer", "duplicate_criterion",
                                   "duplicate_conversation", "incomplete_score", "mismatched_answer"])
def test_malformed_snapshots_fail_closed(env, snapshot, change):
    row = snapshot["per_conv"][0]
    if change == "nan_total":
        row["score"]["total"] = float("nan")
    elif change == "null_answer":
        row["answers"][0]["answer"] = None
    elif change == "object_answer":
        row["answers"][0]["answer"] = {"secret": "DO_NOT_EXPORT"}
    elif change == "duplicate_criterion":
        row["answers"].append(copy.deepcopy(row["answers"][0]))
    elif change == "duplicate_conversation":
        snapshot["per_conv"].append(copy.deepcopy(row))
    elif change == "incomplete_score":
        row["score"]["status"] = "incomplete"
    elif change == "mismatched_answer":
        row["answers"][0]["answer"] = "no"
    save(env, snapshot)
    response = download(env, snapshot)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "snapshot_unavailable"
    assert "DO_NOT_EXPORT" not in response.text


def test_decision_without_an_original_model_answer_is_preserved(env, snapshot):
    row = snapshot["per_conv"][0]
    row["answers"][0]["original_answer"] = row["score"]["lines"][0]["original_answer"] = None
    save(env, snapshot)
    answer = download(env, snapshot).json()["per_conv"][0]["answers"][0]
    assert answer["answer"] == "yes" and answer["original_answer"] is None and answer["source"] == "decision"
    assert csv_rows(download(env, snapshot, "csv"))[0]["original_answer"] == ""


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_failed_run_has_explicit_error_rows_without_fabricated_scores(env, snapshot, fmt):
    snapshot["status"] = "failed"
    failed = copy.deepcopy(snapshot["per_conv"][0])
    failed.update(id="failed-dialogue", status="error", score=None, answers=[],
                  old_total=100, new_total=100, error="token=DO_NOT_EXPORT; private transcript")
    snapshot["per_conv"].append(failed)
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 200
    assert "DO_NOT_EXPORT" not in response.text and "private transcript" not in response.text
    if fmt == "json":
        result = response.json()
        assert result["run_status"] == "failed"
        assert result["per_conv"][0]["score"]["total"] == 83.33
        assert result["per_conv"][1]["status"] == "error"
        assert result["per_conv"][1]["score"] is None and result["per_conv"][1]["answers"] == []
    else:
        rows = csv_rows(response)
        assert len(rows) == 5 and all(row["run_status"] == "failed" for row in rows)
        assert rows[-1]["conv_id"] == "failed-dialogue" and rows[-1]["status"] == "error"
        assert all(rows[-1][key] == "" for key in ("criterion_id", "answer", "original_answer", "total", "critical"))


def test_no_applicable_and_zero_are_not_confused_with_errors(env, snapshot):
    zero = snapshot["per_conv"][0]
    zero["score"].update(total=0, total_raw=0, critical=False, critical_criteria=[])
    no_applicable = copy.deepcopy(zero)
    no_applicable["id"] = "all-na"
    no_applicable["answers"] = [{"criterion_id": "C2", "answer": "na", "original_answer": "na", "source": "model",
                                 "points": None, "weight": 100, "critical_hit": False}]
    no_applicable["score"].update(total=None, total_raw=None, status="no_applicable", applicable_weight=0,
                                  lines=copy.deepcopy(no_applicable["answers"]))
    snapshot["per_conv"].append(no_applicable)
    save(env, snapshot)
    rows = csv_rows(download(env, snapshot, "csv"))
    assert rows[0]["total"] == "0" and rows[0]["critical"] == "False"
    assert rows[-1]["total"] == "" and rows[-1]["answer"] == "na" and rows[-1]["status"] == "done"
    assert download(env, snapshot).json()["per_conv"][-1]["score"]["status"] == "no_applicable"


@pytest.mark.parametrize("change", ["running", "queued", "unfinished", "pending_row", "running_row"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_unfinished_runs_return_409(env, snapshot, change, fmt):
    if change in ("running", "queued"):
        snapshot["status"] = change
    elif change == "unfinished":
        snapshot["finished_at"] = None
    else:
        snapshot["per_conv"][0]["status"] = change.removesuffix("_row")
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_not_finished"
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("change", ["score", "answers", "group_id", "per_conv", "null_score", "empty_answers",
                                   "missing_original", "truncated_answers", "version_mismatch", "legacy_error"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_missing_or_legacy_snapshots_return_409(env, snapshot, change, fmt):
    row = snapshot["per_conv"][0]
    if change in ("score", "answers", "group_id"):
        del row[change]
    elif change == "per_conv":
        del snapshot["per_conv"]
    elif change == "null_score":
        row["score"] = None
    elif change == "empty_answers":
        row["answers"] = []
    elif change == "missing_original":
        del row["answers"][0]["original_answer"]
    elif change == "truncated_answers":
        row["answers"].pop()
    elif change == "version_mismatch":
        row["score"]["checklist_version"] = 9
    elif change == "legacy_error":
        snapshot["status"], row["status"] = "failed", "error"
        del row["score"]
        del row["answers"]
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "snapshot_unavailable"
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_export_allowlist_omits_private_fields_at_every_level(env, snapshot, fmt):
    private = "DO_NOT_EXPORT_TRANSCRIPT_OR_SECRET"
    snapshot.update(api_key=private, transcript=private, errors=[private], config={"token": private})
    row = snapshot["per_conv"][0]
    row.update(transcript=private, messages=[private], audio_url=private, error=private)
    row["score"].update(formula_steps=[private], warnings=[private], raw={"key": private})
    for answer in row["answers"] + row["score"]["lines"]:
        answer.update(evidence=[{"quote": private}], explanation=private, raw={"secret": private}, title=private)
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 200
    assert private not in response.text


@pytest.mark.parametrize("dangerous", ["=1+1", "+SUM(1,2)", "-1+2", "@SUM(1,2)", "  =1+1", "\t=1+1",
                                      "\r=1+1", "\n=1+1", "\x00=1+1", "\ufeff=1+1", "\u200b=1+1",
                                      "\u00a0=1+1", "＝1+1", '=HYPERLINK("url","label")'])
def test_csv_formula_injection_in_arbitrary_ids(env, snapshot, dangerous):
    row = snapshot["per_conv"][0]
    snapshot["run_id"] = row["id"] = row["group_id"] = row["language"] = row["channel"] = dangerous
    row["answers"][0]["criterion_id"] = row["score"]["lines"][0]["criterion_id"] = dangerous
    save(env, snapshot)
    response = download(env, snapshot, "csv")
    assert response.status_code == 200
    first = csv_rows(response)[0]
    for key in ("run_id", "conv_id", "group", "lang", "channel", "criterion_id"):
        assert first[key] == "'" + dangerous
    json_result = download(env, snapshot).json()
    assert json_result["run_id"] == dangerous and json_result["per_conv"][0]["conv_id"] == dangerous
    assert json_result["per_conv"][0]["answers"][0]["criterion_id"] == dangerous


def test_csv_quotes_do_not_create_extra_cells_or_records(env, snapshot):
    row = snapshot["per_conv"][0]
    row["id"] = 'safe,"quoted"\r\n=1+1'
    row["group_id"] = 'Контроль, "тест"\nҚазақ'
    save(env, snapshot)
    rows = csv_rows(download(env, snapshot, "csv"))
    assert len(rows) == 4 and all(len(item) == 13 for item in rows)
    assert rows[0]["conv_id"] == row["id"] and rows[0]["group"] == row["group_id"]


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_safe_attachment_headers_for_hostile_run_id(env, snapshot, fmt):
    snapshot["run_id"] = '..\\";\r\nX-Injected: true Қазағы' + "a" * 200
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 200
    assert re.fullmatch(r'attachment; filename="control-results-[A-Za-z0-9_-]{1,64}\.' + fmt + '"',
                        response.headers["content-disposition"])
    assert "x-injected" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_unknown_run_returns_404(env, snapshot, fmt):
    assert download(env, snapshot, fmt).status_code == 404


@pytest.mark.parametrize("fmt", ["xml", "CSV", "", "../../csv"])
def test_invalid_format_is_rejected(env, snapshot, fmt):
    save(env, snapshot)
    response = download(env, snapshot, fmt)
    assert response.status_code == 422
    assert "content-disposition" not in response.headers


def test_empty_completed_run_is_an_honest_empty_export(env, snapshot):
    snapshot["per_conv"] = []
    save(env, snapshot)
    assert download(env, snapshot).json()["per_conv"] == []
    assert csv_rows(download(env, snapshot, "csv")) == []
