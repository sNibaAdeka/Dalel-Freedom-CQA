"""Build private frontend fixtures; never evaluate real operator performance.

Local DEV: python3 scripts/make_ui_mocks.py
Public demo: python3 scripts/make_ui_mocks.py --synthetic
Generated JSON belongs in ignored web/mocks/, not in the public repository.
All verdicts, confidence, token counts and costs are illustrative mock values.
Only transcript text and quoted slices come from the chosen input records.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-11-07T12:00:00+05:00"
NOTICE = "Демо: ответы и показатели придуманы для проверки интерфейса, это не оценка оператора."
PAUSE = re.compile(r"приостанов|пауза записи|recording.*paus|транскриб.*тоқтат|жазб.*тоқтат", re.I)
RESUME = re.compile(r"возобнов|recording.*resum|жазб.*жалғас|транскриб.*жалғас", re.I)
START = re.compile(r"(?:запис|транскриб).*(?:запущен|начал)|recording.*start", re.I)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")


def rounded(value: float, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def time_value(raw: object) -> tuple[float | None, str]:
    text = str(raw or "")
    if re.fullmatch(r"\d+:\d{2}(?::\d{2})?(?:\.\d+)?", text):
        parts = list(map(float, text.split(":")))
        return sum(value * 60 ** i for i, value in enumerate(reversed(parts))), "relative"
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None, "invalid"
        return value.timestamp(), "absolute"
    except (ValueError, TypeError):
        return None, "invalid"


def normalize(record: dict, group_id: str = "DEV") -> dict:
    did = str(record.get("conversation_id", record.get("id", "")))
    if not SAFE_ID.fullmatch(did):
        raise ValueError("Conversation IDs must be safe nonempty filename components")
    channel = "call" if record.get("channel") in ("voice call", "call") else "chat"
    language = str(record.get("language", "RU")).upper()
    language = "KZ" if language == "KK" else language
    if language not in ("RU", "KZ", "EN"):
        raise ValueError(f"Unsupported language for {did}")
    transcript = record.get("transcript", [])
    if not isinstance(transcript, list):
        raise ValueError(f"transcript must be a list for {did}")
    parsed = [time_value(t.get("timestamp")) for t in transcript]
    absolute_start = next((v for v, kind in parsed if kind == "absolute"), None)
    messages = []
    for idx, (turn, (seconds, kind)) in enumerate(zip(transcript, parsed)):
        speaker = str(turn.get("speaker", "")).lower()
        if speaker not in ("client", "operator", "bot", "system"):
            raise ValueError(f"Invalid role at {did}:{idx}")
        if not isinstance(turn.get("text"), str):
            raise ValueError(f"Message text must be a string at {did}:{idx}")
        t_sec = seconds - absolute_start if kind == "absolute" else seconds
        messages.append({"id": f"{did}:{idx}", "idx": idx, "speaker": speaker,
                         "text": turn["text"], "ts_raw": str(turn.get("timestamp", "")),
                         "t_sec": rounded(t_sec, 3) if t_sec is not None else None, "rec_sec": None})
    recording_start = next((m["t_sec"] for m in messages if m["speaker"] == "system" and m["t_sec"] is not None and START.search(m["text"])), 0)
    gaps, warnings, paused_at = [], [], None
    for message in messages:
        sec = message["t_sec"]
        if channel != "call" or message["speaker"] != "system" or sec is None:
            continue
        if RESUME.search(message["text"]):
            if paused_at is not None and sec >= paused_at:
                gaps.append([paused_at, sec])
                paused_at = None
        elif PAUSE.search(message["text"]) and paused_at is None:
            paused_at = sec
    if paused_at is not None:
        end = max((m["t_sec"] for m in messages if m["t_sec"] is not None), default=paused_at)
        gaps.append([paused_at, end])
        warnings.append("Пауза записи без события возобновления: смещения после неё не подтверждены.")
    for message in messages:
        sec = message["t_sec"]
        in_gap = sec is not None and any(a <= sec < b for a, b in gaps)
        open_pause = paused_at is not None and sec is not None and sec >= paused_at
        if channel == "call" and sec is not None and sec >= recording_start and not in_gap and not open_pause:
            removed = sum(max(0, min(sec, b) - max(a, recording_start)) for a, b in gaps if sec > a)
            message["rec_sec"] = rounded(max(0, sec - recording_start - removed), 3)
    operators = [m for m in messages if m["speaker"] == "operator"]
    first = operators[0] if operators else None
    preceding = [m for m in messages if first and m["idx"] < first["idx"] and m["speaker"] in ("bot", "system")]
    previous = preceding[-1] if preceding else None
    wait = None
    if first and previous and first["t_sec"] is not None and previous["t_sec"] is not None:
        wait = max(0, first["t_sec"] - previous["t_sec"])
    return {"id": did, "channel": channel, "language": language, "group_id": group_id,
            "messages": messages, "audio_url": None,
            "meta": {"handoff_idx": first["idx"] if first else None, "wait_sec": wait,
                     "wait_is_estimate": True, "recording_gaps": gaps, "lang_switches": [],
                     "audio_offset_is_estimate": channel == "call", "warnings": warnings},
            "is_mock": True, "mock_notice": NOTICE}


def criterion_hash(c: dict) -> str:
    def num(value):
        f = float(value)
        return int(f) if f.is_integer() else f
    canon = {"title": (c.get("title") or "").strip(), "description": (c.get("description") or "").strip(),
             "answer_type": c.get("answer_type") or "binary", "scale": [num(v) for v in c["scale"]] if c.get("scale") is not None else None,
             "scale_labels": {str(num(k)): str(v).strip() for k, v in c["scale_labels"].items()} if c.get("scale_labels") else None, "na_allowed": bool(c.get("na_allowed")),
             "na_rule": (c.get("na_rule") or "").strip(), "critical": bool(c.get("critical")),
             "critical_when": (c.get("critical_when") or "yes") if c.get("critical") else None,
             "critical_rule": (c.get("critical_rule") or "").strip() or None,
             "guidance": [g.strip() for g in c.get("guidance", []) if g and g.strip()]}
    return hashlib.sha1(json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def make_checklist(version: int = 1, source: dict | None = None) -> dict:
    checklist = copy.deepcopy(source) if source is not None else json.loads((ROOT / "data/checklists/v1.json").read_text(encoding="utf-8"))
    checklist["version"] = version
    checklist["active"] = version == 1
    if version == 2:
        criterion = next((c for c in checklist["criteria"] if c["id"] == "C4"), checklist["criteria"][0])
        criterion["title"] = "Полнота ответа и проверяемый следующий шаг"
        criterion["description"] = "Демонстрация замены: максимальный балл требует ответа на все вопросы и конкретного следующего шага."
        criterion["hash"] = criterion_hash(criterion)
        checklist["change_note"] = "Демо: заменён один критерий C4"
        checklist["official"] = False
    return checklist


def evidence_for(message: dict) -> dict:
    text = message["text"]
    # Trim only the quoted slice, never rewrite the original message.
    start = len(text) - len(text.lstrip())
    end = min(len(text.rstrip()), start + 110)
    quote = text[start:end]
    return {"message_id": message["id"], "quote": quote, "start": utf16_length(text[:start]),
            "end": utf16_length(text[:end]), "valid": bool(quote), "verification": "exact_source_slice"}


def make_evals(conversation: dict, checklist: dict, position: int) -> list[dict]:
    operators = [m for m in conversation["messages"] if m["speaker"] == "operator" and m["text"].strip()]
    result = []
    for ci, criterion in enumerate(checklist["criteria"]):
        answer = "yes"
        if criterion["answer_type"] in ("scale", "numeric"):
            answer = criterion["scale"][(position + ci) % len(criterion["scale"])]
            if checklist["version"] > 1 and criterion["id"] == "C4":
                answer = criterion["scale"][max(0, criterion["scale"].index(answer) - 1)]
        elif (position + ci) % 7 == 3:
            answer = "no"
        if criterion["critical"]:
            answer = "yes" if position % 17 == 0 else "no"
        # KZ always has a clear N/A example; other languages include N/A too.
        if criterion["na_allowed"] and (conversation["language"] == "KZ" or (position + ci) % 4 == 0):
            answer = "na"
        if not operators:
            answer = "na" if criterion["na_allowed"] else None
        evidence, checked_range = [], None
        if operators and answer not in ("na", None):
            if answer in ("no", 0):
                checked_range = [operators[0]["id"], operators[-1]["id"]]
            else:
                evidence = [evidence_for(operators[ci % len(operators)])]
        confidence = 0.58 if 4 <= position < 13 and criterion["id"] == "C4" else 0.92
        critical = criterion["critical"] and answer == criterion.get("critical_when", "yes")
        needs_review = not operators or confidence < 0.75 or critical
        reasons = ["Демонстрационный флаг ручной проверки"] if needs_review else []
        tokens_in = 100 + sum(len(m["text"]) for m in conversation["messages"]) // 4
        tokens_out = 45 + ci * 3
        row = {"conv_id": conversation["id"], "criterion_id": criterion["id"],
               "criterion_hash": criterion["hash"], "prompt_version": "ui-fixture-v1",
               "answer": answer, "title": criterion["title"], "evidence": evidence,
               "checked_range": checked_range,
               "na_reason": "Демонстрационный N/A: действие не требуется для темы обращения; не экспертный вывод." if answer == "na" else None,
               "explanation": "Иллюстративный ответ для проверки интерфейса. Цитата сверена с текстом, правильность оценки не проверялась.",
               "suggestion": "Пример подсказки: уточните, понятен ли клиенту следующий шаг." if checked_range else None,
               "confidence": confidence, "critical_hit": critical, "model": "mock-illustration",
               "tokens_in": tokens_in, "tokens_out": tokens_out,
               "cost_kzt": rounded(tokens_in * 0.0002 + tokens_out * 0.0006, 4),
               "latency_ms": 90 + ci * 13, "needs_review": needs_review, "review_reasons": reasons,
               "is_mock": True, "metrics_are_illustrative": True,
               "evaluation_id": f"mock:{conversation['id']}:{criterion['hash'][:12]}:v{checklist['version']}"}
        result.append(row)
    return result


def score(conversation_id: str, checklist: dict, evals: list[dict]) -> dict:
    by_id = {item["criterion_id"]: item for item in evals}
    applicable = Fraction(0)
    earned_total = Fraction(0)
    lines = []
    complete = True
    for criterion in checklist["criteria"]:
        answer = by_id.get(criterion["id"], {}).get("answer")
        na = answer == "na"
        weight = Fraction(str(criterion["weight"]))
        if not na:
            applicable += weight
        if answer is None:
            complete = False
        ratio = None
        if not na and answer is not None:
            ratio = Fraction(1 if answer == "yes" else 0) if criterion["answer_type"] == "binary" else Fraction(str(answer)) / Fraction(str(max(criterion["scale"])))
        earned = ratio * weight if ratio is not None else None
        earned_total += earned or 0
        lines.append({"criterion_id": criterion["id"], "title": criterion["title"], "answer": answer,
                      "section": criterion["section"], "points": float(earned) if earned is not None else None,
                      "max_points": criterion["weight"], "weight": criterion["weight"],
                      "weight_effective": 0.0, "contribution": 0.0 if na else None, "is_na": na,
                      "critical": bool(criterion.get("critical")),
                      "critical_hit": bool(criterion.get("critical")) and answer == criterion.get("critical_when", "yes"), "_earned": earned})
    for line in lines:
        if applicable and not line["is_na"]:
            line["weight_effective"] = float(Fraction(str(line["weight"])) * 100 / applicable)
            line["contribution"] = float(line["_earned"] * 100 / applicable) if line["_earned"] is not None else None
    raw = earned_total * 100 / applicable if complete and applicable else None
    places = checklist.get("rounding", {}).get("total", 2)
    def finish(value):
        if value is None:
            return None
        return float((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
    sections = {}
    for name in checklist["sections"]:
        group = [line for line in lines if line["section"] == name and not line["is_na"]]
        weights = sum((Fraction(str(line["weight"])) for line in group), Fraction(0))
        sections[name] = finish(sum((line["_earned"] for line in group), Fraction(0)) * 100 / weights) if weights and all(line["_earned"] is not None for line in group) else None
    critical = [line["criterion_id"] for line in lines if line["critical_hit"]]
    steps = ["N/A: " + (", ".join(line["criterion_id"] for line in lines if line["is_na"]) or "нет"),
             f"Сумма весов применимых: {float(applicable):g}",
             "Баллы: " + " + ".join(f"{line['points']:g}" for line in lines if line["points"] is not None and line["weight"] > 0) + f" = {float(earned_total):g}",
             f"{float(earned_total):g} / {float(applicable):g} × 100 = {float(raw):.4f} → {finish(raw):g}" if raw is not None else "Итог не рассчитывается: нет применимых критериев или оценка не завершена."]
    for line in lines:
        del line["_earned"]
    return {"conv_id": conversation_id, "checklist_version": checklist["version"], "total": finish(raw),
            "total_raw": float(raw) if raw is not None else None, "sections": sections, "critical": bool(critical),
            "critical_criteria": critical, "lines": lines, "weight_sum": sum(c["weight"] for c in checklist["criteria"]),
            "applicable_weight": float(applicable), "status": "incomplete" if not complete else "complete" if applicable else "no_applicable",
            "formula_steps": steps, "is_mock": True}


def synthetic_records(count: int = 65) -> list[dict]:
    """Authored demonstration texts, wholly independent of organizer transcripts."""
    if count < 1:
        raise ValueError("count must be positive")
    sentences = {
        "RU": ["Здравствуйте, где посмотреть статус учебной заявки?", "Демо-помощник передаёт обращение специалисту.",
               "Здравствуйте! Я проверю учебную заявку. Уточните, когда вы её отправили.", "Сегодня утром.",
               "Откройте раздел «Мои заявки», затем выберите учебный пример.", "Вижу запись, спасибо.",
               "Статус обновится после проверки. Уведомление появится в разделе сообщений.", "Понятно.",
               "Рад помочь. Если возникнет вопрос, напишите снова. Хорошего дня!"],
        "KZ": ["Сәлеметсіз бе! Оқу өтінімінің күйін қайдан көруге болады?", "Демо-көмекші сізді маманға қосады.",
               "Сәлеметсіз бе! Өтінімді қашан жібергеніңізді нақтылаңыз.", "Бүгін таңертең жібердім.",
               "«Менің өтінімдерім» бөлімін ашып, оқу мысалын таңдаңыз.", "Көріп тұрмын, рақмет.",
               "Тексеруден кейін күйі өзгереді. Хабарлама хабарлар бөлімінде пайда болады.", "Түсінікті.",
               "Сұрағыңыз болса, қайта жазыңыз. Күніңіз жақсы өтсін!"],
        "EN": ["Hello, where can I find the status of a practice request?", "The demo assistant is connecting you to a specialist.",
               "Hello! When did you submit the practice request?", "This morning.",
               "Open My Requests and select the practice example.", "I can see it, thank you.",
               "Its status will change after the review. A notification will appear in Messages.", "Understood.",
               "Happy to help. Please contact us again if you have a question."],
    }
    result = []
    for i in range(count):
        call = i >= max(0, count - 5) if count >= 10 else i == count - 1
        language = "RU" if call or i % 4 < 2 else "KZ" if i % 4 == 2 else "EN"
        roles = ["client", "bot", "operator", "client", "operator", "client", "operator", "client", "operator"]
        timeline = [2, 5, 15, 24, 35, 43, 70, 76, 90]
        events = list(zip(timeline, roles, sentences[language]))
        if call:
            events.extend([(45, "system", "Запись и транскрибирование приостановлены"),
                           (65, "system", "Запись и транскрибирование возобновлены")])
        origin = datetime(2026, 11, 7, 12, i % 60, tzinfo=timezone(timedelta(hours=5)))
        transcript = []
        for sec, role, text in sorted(events):
            timestamp = (origin + timedelta(seconds=sec)).isoformat() if call else f"{sec // 60:02}:{sec % 60:02}"
            transcript.append({"timestamp": timestamp, "speaker": role, "text": text})
        result.append({"conversation_id": f"demo-{'call' if call else 'chat'}-{language.lower()}-{i + 1:03}",
                       "channel": "voice call" if call else "agent chat", "language": language,
                       "transcript": transcript, "audio_url": None})
    return result


def unpack_records(value: object) -> list[dict]:
    if isinstance(value, dict):
        if value.get("isSuccess") is False:
            raise ValueError("Dataset reports an unsuccessful response")
        value = value.get("data", value)
        if isinstance(value, dict):
            value = value.get("records", [])
    if not isinstance(value, list) or not value or not all(isinstance(r, dict) for r in value):
        raise ValueError("Expected a nonempty records array or organizer API envelope")
    return value


def build_fixtures(records: list[dict], *, synthetic: bool = False, checklist: dict | None = None) -> dict[str, object]:
    checklists = [make_checklist(1, checklist), make_checklist(2, checklist)]
    details, summaries, ids = [], [], set()
    for pos, record in enumerate(records):
        conversation = normalize(record, "SYNTHETIC" if synthetic else "DEV")
        if conversation["id"] in ids:
            raise ValueError("Duplicate conversation IDs are not allowed")
        ids.add(conversation["id"])
        evals = make_evals(conversation, checklists[0], pos)
        result = score(conversation["id"], checklists[0], evals)
        detail = {**conversation, "evals": evals, "score": result, "checklist": checklists[0],
                  "appeals": [], "decisions": [], "audit": []}
        details.append(detail)
        summaries.append({"id": conversation["id"], "channel": conversation["channel"],
                          "language": conversation["language"], "group_id": conversation["group_id"], "total": result["total"],
                          "critical": result["critical"], "needs_review": any(e["needs_review"] for e in evals),
                          "status": "needs_review" if any(e["needs_review"] for e in evals) else "evaluated",
                          "cells": [{**line, "confidence": e["confidence"], "needs_review": e["needs_review"]} for line, e in zip(result["lines"], evals)],
                          "is_mock": True})
    fixture = {"conversations.json": summaries, "checklists.json": checklists,
               "checklist_1.json": checklists[0], "checklist_2.json": checklists[1]}
    fixture["checklist_diff.json"] = {"from": 1, "to": 2, "from_version": 1, "to_version": 2, "added": [], "removed": [], "needs_reeval": ["C4"], "reusable": ["C1", "C2", "C3", "C5", "C6", "C7", "C8"], "rescore_only": [],
                                       "changed": [{"criterion_id": "C4", "fields": ["title", "description"],
                                                    "needs_reeval": True, "before": checklists[0]["criteria"][3], "after": checklists[1]["criteria"][3]}], "is_mock": True}
    for detail in details:
        did = detail["id"]
        fixture[f"conversation_{did}.json"] = detail
        fixture[f"audio_{did}.json"] = {"src": None, "expires_at": None, "gaps": detail["meta"]["recording_gaps"],
                                       "offsets": {m["id"]: m["rec_sec"] for m in detail["messages"] if m["rec_sec"] is not None},
                                       "offsets_are_estimates": True, "is_mock": True}
    chosen = next((d for d in details if d["evals"][1]["answer"] is not None), None)
    appeals = []
    if chosen:
        evaluation = chosen["evals"][1]
        appeal = {"id": "demo-appeal-001", "conv_id": chosen["id"], "criterion_id": "C2",
                  "evaluation_id": evaluation["evaluation_id"], "checklist_version": 1,
                  "criterion_hash": evaluation["criterion_hash"], "comment": "Демо-апелляция: прошу проверить оценку уточнения вопроса.",
                  "message_ids": [m["id"] for m in chosen["messages"] if m["speaker"] == "operator"][:1],
                  "status": "open", "created_at": STAMP, "is_mock": True}
        appeals.append(appeal)
        chosen["appeals"].append(appeal)
        updated = copy.deepcopy(chosen["evals"])
        old_answer = updated[1]["answer"]
        updated[1]["answer"] = "yes" if old_answer != "yes" else "no"
        revised = score(chosen["id"], checklists[0], updated)
        decision = {"id": "demo-decision-001", "appeal_id": appeal["id"], "action": "change",
                    "old_answer": old_answer, "new_answer": updated[1]["answer"],
                    "reason": "Пример решения для проверки интерфейса, не экспертная оценка.", "created_at": STAMP, "is_mock": True}
        fixture["decision_result.json"] = {"old_total": chosen["score"]["total"], "new_total": revised["total"],
                                           "conv_id": chosen["id"], "decision": decision, "score": revised, "is_mock": True}
        fixture[f"dossier_{appeal['id']}.json"] = {"appeal_id": appeal["id"], "is_mock": True,
                                                  "for": [{"text": "Демонстрационный довод для пересмотра", "evidence": evaluation["evidence"]}],
                                                  "arguments_for": [], "arguments_against": [],
                                                  "against": [{"text": "Нужна проверка всего участка человеком", "checked_range": evaluation["checked_range"]}],
                                                  "recommendation": "Демонстрационное досье. Решение принимает проверяющий."}
    fixture["appeals.json"] = appeals
    total = len(details)
    changed_totals = []
    for pos, detail in enumerate(details):
        newer = copy.deepcopy(detail["evals"])
        newer[3] = make_evals(detail, checklists[1], pos)[3]
        changed_totals.append(score(detail["id"], checklists[1], newer)["total"])
    for step in range(5):
        done = total * step // 4
        fixture[f"run_progress_{step}.json"] = {
            "run_id": "demo-run-001", "version": 2, "status": "complete" if done == total else "running",
            "done": done, "total": total, "from_cache": done * 7, "cache_total": total * 8,
            "llm_calls": 0, "simulated_evaluations": done, "cost_kzt": 0,
            "elapsed": done * 0.6, "is_mock": True, "metrics_are_illustrative": True,
            "per_conv": [{"id": d["id"], "old_total": d["score"]["total"],
                          "new_total": changed_totals[i] if i < done else None,
                          "status": "complete" if i < done else "pending"} for i, d in enumerate(details)]}
    demo_ids = []
    for channel, language, count in [("chat", "RU", 2), ("chat", "KZ", 1), ("chat", "EN", 1), ("call", "RU", 2)]:
        candidates = [d["id"] for d in details if d["channel"] == channel and d["language"] == language]
        demo_ids.extend(candidates[:count])
    fixture["health.json"] = {"ok": True, "provider": "mock", "model": "mock-illustration", "offline": True, "is_mock": True}
    fixture["metrics.json"] = {"conversations": total, "evaluated": total,
                               "cost_kzt": rounded(sum(e["cost_kzt"] for d in details for e in d["evals"]), 4),
                               "is_mock": True, "metrics_are_illustrative": True}
    fixture["manifest.json"] = {"schema_version": 1, "count": total, "demo_ids": demo_ids,
                                "source": "authored_synthetic" if synthetic else "private_organizer_dev",
                                "is_mock": True, "notice": NOTICE, "files": sorted(fixture)}
    return fixture


def write_fixtures(fixtures: dict[str, object], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # Only remove obsolete outputs created by this generator, not arbitrary files.
    manifest = out / "manifest.json"
    if manifest.exists():
        try:
            stale = json.loads(manifest.read_text()).get("files", [])
        except (ValueError, OSError):
            stale = []
        for name in stale:
            if isinstance(name, str) and Path(name).name == name and name.endswith(".json") and name not in fixtures:
                (out / name).unlink(missing_ok=True)
    for name, value in fixtures.items():
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def private_output_is_ignored(out: Path) -> bool:
    try:
        relative = (out / "privacy_probe.json").resolve().relative_to(ROOT.resolve())
    except ValueError:
        return False
    result = subprocess.run(["git", "check-ignore", "--quiet", "--", str(relative)], cwd=ROOT, capture_output=True)
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/raw/dev_records.json")
    parser.add_argument("--checklist", type=Path, default=ROOT / "data/checklists/v1.json")
    parser.add_argument("--out", type=Path, default=ROOT / "web/mocks")
    parser.add_argument("--synthetic", action="store_true", help="Generate authored fictitious conversations without DEV")
    parser.add_argument("--count", type=int, default=65, help="Synthetic record count (default 65)")
    args = parser.parse_args()
    if args.synthetic:
        records = synthetic_records(args.count)
    else:
        if not private_output_is_ignored(args.out):
            parser.error("DEV-derived output must be inside an ignored directory in this repository (normally web/mocks/)")
        if not args.input.is_file():
            parser.error("DEV input is missing. Use --synthetic for a public demo without organizer data.")
        records = unpack_records(json.loads(args.input.read_text(encoding="utf-8")))
    checklist = json.loads(args.checklist.read_text(encoding="utf-8"))
    fixtures = build_fixtures(records, synthetic=args.synthetic, checklist=checklist)
    write_fixtures(fixtures, args.out)
    print(f"Created {len(fixtures)} mock JSON files for {len(records)} conversations in {args.out}")
    print("Illustrative verdicts and metrics; generated data must not be committed.")


if __name__ == "__main__":
    main()
