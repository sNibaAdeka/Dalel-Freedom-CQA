"""Control results from persisted run snapshots, never from current evaluations.

Mount ``router`` under /api. This is a project export schema, not an official
organizer submission format. Deliberately omit transcripts, evidence, free-form
diagnostics, model payloads and unrecognized fields, including inside scores.
"""
from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()

CSV_COLUMNS = (
    "conv_id", "group", "lang", "channel", "checklist_version", "run_id", "run_status",
    "criterion_id", "answer", "original_answer", "total", "critical", "status",
)
ANSWER_FIELDS = ("criterion_id", "answer", "original_answer", "source", "points", "weight", "critical_hit")
SCORE_FIELDS = ("checklist_version", "total", "total_raw", "status", "critical", "critical_criteria",
                "weight_sum", "applicable_weight", "missing")


def _conflict(code: str, message: str) -> NoReturn:
    # Never echo snapshot contents or exception text in an error response.
    raise HTTPException(409, detail={"code": code, "message": message})


def _missing_snapshot() -> NoReturn:
    _conflict("snapshot_unavailable", "Снимок результатов отсутствует или неполон (возможно, старый прогон). "
              "Создайте новый прогон: текущие оценки не используются для восстановления экспорта.")


def _number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _answer(value: Any) -> bool:
    return value is None or _number(value) or (isinstance(value, str) and value in ("yes", "no", "na"))


def _score_snapshot(row: dict, version: int) -> tuple[dict, list[dict]]:
    score, answers = row["score"], row["answers"]
    if (not isinstance(score, dict) or not isinstance(answers, list) or not answers
            or not {"checklist_version", "total", "critical", "status"} <= score.keys()
            or score["checklist_version"] != version
            or score["status"] not in ("complete", "no_applicable")
            or type(score["critical"]) is not bool):
        _missing_snapshot()
    if ((score["status"] == "complete" and not _number(score["total"]))
            or (score["status"] == "no_applicable" and score["total"] is not None)):
        _missing_snapshot()
    for key in ("total_raw", "weight_sum", "applicable_weight"):
        if key in score and score[key] is not None and not _number(score[key]):
            _missing_snapshot()
    for key in ("critical_criteria", "missing"):
        if key in score and (not isinstance(score[key], list) or not all(isinstance(x, str) for x in score[key])):
            _missing_snapshot()

    clean_answers, seen = [], set()
    for item in answers:
        if (not isinstance(item, dict) or not set(ANSWER_FIELDS) <= item.keys()
                or not isinstance(item["criterion_id"], str) or not item["criterion_id"]
                or item["criterion_id"] in seen
                or not _answer(item["answer"]) or item["answer"] is None
                or not _answer(item["original_answer"])
                or item["source"] not in ("model", "decision")
                or (item["points"] is not None and not _number(item["points"]))
                or not _number(item["weight"]) or type(item["critical_hit"]) is not bool):
            _missing_snapshot()
        seen.add(item["criterion_id"])
        clean_answers.append({key: item[key] for key in ANSWER_FIELDS})
    # A full score carries its criterion set. Reject a truncated/mismatched answer
    # snapshot instead of silently producing a partial control-results file.
    if "lines" in score:
        lines = score["lines"]
        if (not isinstance(lines, list) or len(lines) != len(answers)
                or any(not isinstance(line, dict) for line in lines)):
            _missing_snapshot()
        by_id = {item["criterion_id"]: item for item in clean_answers}
        for line in lines:
            cid = line.get("criterion_id")
            if not isinstance(cid, str) or cid not in by_id or any(
                key not in line or line[key] != by_id[cid][key] for key in ANSWER_FIELDS
            ):
                _missing_snapshot()
        if {line["criterion_id"] for line in lines} != seen:
            _missing_snapshot()
    return {key: score[key] for key in SCORE_FIELDS if key in score}, clean_answers


def _export_snapshot(run: dict) -> dict:
    if run.get("status") not in ("completed", "failed") or not run.get("finished_at"):
        _conflict("run_not_finished", "Экспорт доступен только после завершения прогона.")
    if (type(run.get("version")) is not int or run["version"] < 1
            or not isinstance(run.get("per_conv"), list)
            or not isinstance(run.get("mode"), str) or not isinstance(run.get("started_at"), str)
            or not isinstance(run["finished_at"], str)
            or (run.get("profile") is not None and not isinstance(run["profile"], str))):
        _missing_snapshot()

    conversations, seen = [], set()
    for row in run["per_conv"]:
        if not isinstance(row, dict):
            _missing_snapshot()
        if row.get("status") in ("pending", "running"):
            _conflict("run_not_finished", "Снимок прогона содержит незавершённые диалоги.")
        if (not {"score", "answers", "id", "group_id", "language", "channel", "status"} <= row.keys()
                or not isinstance(row["id"], str) or not row["id"] or row["id"] in seen
                or any(not isinstance(row[key], str) for key in ("group_id", "language", "channel"))
                or row["status"] not in ("done", "error")
                or (row["status"] == "error" and run["status"] != "failed")):
            _missing_snapshot()
        seen.add(row["id"])
        # Failed conversations have no usable result. In particular, don't use
        # new_total/old_total or expose a partial score as a successful result.
        score, answers = _score_snapshot(row, run["version"]) if row["status"] == "done" else (None, [])
        conversations.append({"conv_id": row["id"], "group": row["group_id"], "lang": row["language"],
                              "channel": row["channel"], "status": row["status"], "score": score, "answers": answers})

    return {
        "schema_version": 1,
        "run_id": run["run_id"], "checklist_version": run["version"], "run_status": run["status"],
        "profile": run.get("profile"), "mode": run["mode"],
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "provenance": {"source": "saved_run_snapshot", "official_organizer_format": False},
        "policy": {
            "results": "Saved run scores and final answers; never recomputed from current evaluations or decisions.",
            "answers": "answer is final; original_answer is the model answer before reviewer decisions.",
            "critical": "Separate saved conversation flag; does not replace the numeric total.",
            "status": "run_status is the run outcome; status is the conversation outcome; score.status is scoring status.",
            "errors": "Failed conversations have null score, no answers, and one CSV row with blank result cells.",
            "privacy": "Only result fields; no transcripts, evidence, model payloads or free-form diagnostics.",
            "csv": "UTF-8 BOM; formula-like text cells are prefixed with an apostrophe. JSON preserves IDs verbatim.",
        },
        "per_conv": conversations,
    }


def _csv_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return "" if value is None else value
    # Whitespace, control characters and invisible formatting before a formula
    # must not bypass protection. csv.writer handles quotes/delimiters/newlines.
    index = 0
    while index < len(value) and (value[index].isspace() or unicodedata.category(value[index]) in ("Cc", "Cf")):
        index += 1
    if (any(unicodedata.category(char) in ("Cc", "Cf") for char in value[:index])
            or value[index:index + 1] in ("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + value
    return value


def _csv_content(export: dict) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
    writer.writerow(CSV_COLUMNS)
    for conv in export["per_conv"]:
        score = conv["score"] or {}
        for answer in conv["answers"] or [{}]:
            row = {
                **{key: conv[key] for key in ("conv_id", "group", "lang", "channel", "status")},
                **{key: export[key] for key in ("checklist_version", "run_id", "run_status")},
                **{key: answer.get(key) for key in ("criterion_id", "answer", "original_answer")},
                "total": score.get("total"), "critical": score.get("critical"),
            }
            writer.writerow([_csv_cell(row[key]) for key in CSV_COLUMNS])
    return stream.getvalue().encode("utf-8-sig")


@router.get("/runs/{run_id}/export")
def export_run(request: Request, run_id: str, format: Literal["json", "csv"] = "json") -> Response:
    """Download a completed/failed run's saved results in the project export schema."""
    run = request.app.state.cqa.repo.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Прогон не найден.")
    export = _export_snapshot(run)
    # ASCII-only, bounded, no paths/header delimiters, even for arbitrary saved IDs.
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", run_id).strip("_-")[:64] or "run"
    headers = {"Content-Disposition": f'attachment; filename="control-results-{slug}.{format}"',
               "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if format == "csv":
        return Response(_csv_content(export), media_type="text/csv; charset=utf-8", headers=headers)
    return JSONResponse(export, headers=headers)
