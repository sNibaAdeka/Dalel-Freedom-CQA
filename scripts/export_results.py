"""Convert a control_run JSON export into the organizers' results format (schema 1.0).

Usage:
  python scripts/export_results.py results/control_FBroker.json results/prod/results.json \
      --records data/raw/fbroker_records.json --model-name "google/gemini-4-argon" --mode external_api

Format (CQA Student Guide, appendix «Как сдать результаты»): one object per file, `results` holds every
dialogue of the set, exactly once, in the API's conversation_id. Criteria codes, value types and the score
come from the official checklist v1: true = «Да», false = «Нет», null = N/A, numbers = the scale step
before weighting. evidence_turns are 1-based positions in the original transcript array (client, bot and
system events included). A dialogue that could not be scored keeps its ID with status "error".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CODES = {"C1": "contact", "C2": "discovery", "C3": "identification", "C4": "consultation",
         "C5": "empathy", "C6": "dialogue", "C7": "closure", "C8": "critical"}
SCALES = {"consultation": {0, 5, 10, 15, 20}, "dialogue": {0, 4, 7, 10}}
NA_ALLOWED = {"discovery", "identification", "consultation", "closure"}
REF = re.compile(r"\[([^\[\]:]+):(\d+)\]")


def turn(message_id: str | None) -> int | None:
    """'p2-ru-001:9' → 10 (1-based position in the original transcript)."""
    if not message_id or ":" not in str(message_id):
        return None
    tail = str(message_id).rsplit(":", 1)[1]
    return int(tail) + 1 if tail.isdigit() else None


def value_of(code: str, answer):
    if answer in (None, "", "missing"):
        raise ValueError("нет ответа")
    if answer == "na":
        if code not in NA_ALLOWED:
            raise ValueError(f"{code}: N/A не разрешён чек-листом")
        return None
    if code in SCALES:
        v = int(round(float(answer)))
        if v not in SCALES[code]:
            raise ValueError(f"{code}: {answer} не ступень шкалы")
        return v
    if answer in ("yes", True):
        return True
    if answer in ("no", False):
        return False
    raise ValueError(f"{code}: неизвестный ответ {answer!r}")


def rationale(text: str | None, limit: int = 600) -> str:
    text = REF.sub(lambda m: f"№{int(m.group(2)) + 1}", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def evidence_turns(cr: dict, n_turns: int | None) -> list[int]:
    turns = [turn(e.get("message_id")) for e in (cr.get("evidence") or [])]
    turns = [t for t in turns if t]
    rng = cr.get("checked_range")
    if not turns and isinstance(rng, (list, tuple)) and len(rng) == 2:
        a, b = turn(rng[0]), turn(rng[1])
        if a and b:
            turns = list(range(a, b + 1))[:20]
    if n_turns:
        turns = [t for t in turns if 1 <= t <= n_turns]
    return sorted(set(turns))


def convert(export: dict, records: dict[str, dict], team: str, model: dict) -> dict:
    out = []
    by_id = {c.get("conversation_id"): c for c in export.get("conversations", [])}
    ids = list(records) if records else list(by_id)
    for cid in ids:
        conv = by_id.get(cid)
        n_turns = len(records[cid]["transcript"]) if cid in records else None
        if conv is None:
            out.append({"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                        "error": "Диалог не оценён системой."})
            continue
        try:
            if conv.get("error") or conv.get("total") is None:
                raise ValueError(conv.get("error") or "итог не рассчитан")
            crit = {c["criterion_id"]: c for c in conv.get("criteria", [])}
            criteria = []
            for cid8, code in CODES.items():
                cr = crit.get(cid8)
                if cr is None:
                    raise ValueError(f"нет ответа по {cid8}")
                criteria.append({"criterion_id": code, "value": value_of(code, cr.get("answer")),
                                 "rationale": rationale(cr.get("explanation") or cr.get("na_reason")),
                                 "evidence_turns": evidence_turns(cr, n_turns)})
            critical = next(c["value"] for c in criteria if c["criterion_id"] == "critical")
            out.append({"conversation_id": cid, "status": "ok", "criteria": criteria,
                        "score": {"final_score": round(float(conv["total"]), 2), "critical_flag": bool(critical)}})
        except (ValueError, KeyError, TypeError) as exc:
            out.append({"conversation_id": cid, "status": "error", "criteria": [], "score": None,
                        "error": f"Не удалось сформировать оценку: {exc}"})
    return {"schema_version": "1.0", "team_name": team, "model": model, "results": out}


def load_records(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    recs = raw.get("data", {}).get("records", raw) if isinstance(raw, dict) else raw
    return {r["conversation_id"]: r for r in recs}


def validate(doc: dict, expected: int | None) -> list[str]:
    problems, seen = [], set()
    for r in doc["results"]:
        if r["conversation_id"] in seen:
            problems.append(f"повтор ID {r['conversation_id']}")
        seen.add(r["conversation_id"])
        if r["status"] == "ok":
            codes = [c["criterion_id"] for c in r["criteria"]]
            if sorted(codes) != sorted(CODES.values()):
                problems.append(f"{r['conversation_id']}: критерии {codes}")
            if r["score"]["critical_flag"] != next(c["value"] for c in r["criteria"] if c["criterion_id"] == "critical"):
                problems.append(f"{r['conversation_id']}: critical_flag не совпадает")
    if expected is not None and len(doc["results"]) != expected:
        problems.append(f"записей {len(doc['results'])}, ожидалось {expected}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export"); ap.add_argument("out")
    ap.add_argument("--records", type=Path, help="raw API records (to keep every ID and bound turn numbers)")
    ap.add_argument("--team", default="−60HZ")
    ap.add_argument("--model-name", default="google/gemini-4-argon")
    ap.add_argument("--mode", default="external_api", choices=["local", "external_api", "hybrid"])
    ap.add_argument("--expect", type=int, help="exact number of dialogues (prod: 10)")
    a = ap.parse_args(argv)
    export = json.loads(Path(a.export).read_text(encoding="utf-8"))
    doc = convert(export, load_records(a.records), a.team, {"mode": a.mode, "name": a.model_name})
    problems = validate(doc, a.expect)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = sum(r["status"] == "ok" for r in doc["results"])
    print(f"{a.out}: {len(doc['results'])} диалогов, ok {ok}, error {len(doc['results']) - ok}")
    for p in problems:
        print("ПРОБЛЕМА:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
