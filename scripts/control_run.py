"""Control run in one command: fetch -> ingest -> LIVE evaluation with the active checklist -> export + trace.

    python scripts/control_run.py <group_id> --source api --version 1 --profile cloud      # the control command
    python scripts/control_run.py <group_id> --ids a,b                  # re-run ONLY these dialogues, update their records
    python scripts/control_run.py DEV --limit 10 --db results/rehearsal.sqlite             # rehearsal (separate DB!)

Defaults are NOT taken from the UI: without --version the OFFICIAL checklist (v1) is used (a warning is printed when
another version is active), without --profile the config default_profile (cloud). Everything that decides the result
(group, source, number of dialogues, checklist version + the criteria titles, profile, model, mode) is printed BEFORE start.

Process correctness (the organizers check it): every conversation of the set is evaluated live -- force=True (no
reuse of stored answers) and no_cache=True (no LLM disk cache). A conversation_id already taken by another dialogue
is stored as <id>@<group> (cqa.ingest), so stored DEV answers can never leak into the control results.

Outputs (results/ is ignored by Git -- they contain transcript quotes, never commit them):
  results/control_<gid>.json         per conversation: answer, points, explanation, fragment per criterion; total;
                                     gross-error flag; run parameters (model, votes, prompt versions, git commit)
  results/control_<gid>.csv          one row per (conversation, criterion), Excel-friendly (UTF-8 BOM)
  results/control_<gid>_trace.jsonl  per evaluation: raw model output (votes, arbiter, usage), model, prompt version,
                                     latency, tokens, cost -- evidence of the live computation
The results are also in the server DB: the UI shows them right away (group filter = <gid>).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import engine, ingest, llm  # noqa: E402
from cqa.models import Score  # noqa: E402
from cqa.review import export_status, validation_status  # noqa: E402
from cqa.runs import current_evals  # noqa: E402

RESULTS = ROOT / "results"


def _git_commit() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _label(answer: Any) -> str:
    if answer == "yes":
        return "Да"
    if answer == "no":
        return "Нет"
    if answer == "na":
        return "N/A"
    if answer is None:
        return "нет ответа"
    return f"{answer:g}" if isinstance(answer, float) else str(answer)


def _num(x: Any) -> Any:
    return int(x) if isinstance(x, float) and x.is_integer() else x


def _csv_safe(value: Any) -> Any:
    """Spreadsheet formula guard for text cells (=, +, -, @ also after whitespace/invisible chars), like the API export."""
    if not isinstance(value, str):
        return value
    import unicodedata
    i = 0
    while i < len(value) and (value[i].isspace() or unicodedata.category(value[i]) in ("Cc", "Cf")):
        i += 1
    if (any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value[:i])
            or value[i:i + 1] in ("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + value
    return value


def _run_id_of(ev: Any) -> Optional[str]:
    return (getattr(ev, "__pydantic_extra__", None) or {}).get("run_id")


class _RunCapture:
    """Keeps an in-memory copy of every evaluation THIS process saves during the run, keyed by its run_id.

    The evaluations table is upserted per (conv, hash, prompt, mode): a later run B (another process, the UI) may
    overwrite run A's rows before A's files are written. The export of run A must come from A's own snapshot
    (strict review PROD-01), so explanations/evidence/trace are taken from these copies, not re-read from the table."""

    def __init__(self, db: Any):
        self.db, self.by_run = db, {}
        self._had = "save_eval" in vars(db)
        self._orig = db.save_eval

    def __enter__(self) -> "_RunCapture":
        orig = self._orig

        def save_eval(ev: Any) -> Any:
            saved = orig(ev)
            rid = _run_id_of(saved) or _run_id_of(ev)
            if rid:
                self.by_run.setdefault(rid, {})[(saved.conv_id, saved.criterion_id)] = saved.model_copy(deep=True)
            return saved
        self.db.save_eval = save_eval
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._had:
            self.db.save_eval = self._orig
        else:
            try:
                del self.db.save_eval
            except AttributeError:
                pass

    def evals(self, run_id: Optional[str], conv_id: str) -> dict[str, Any]:
        return {cid: ev for (conv, cid), ev in (self.by_run.get(run_id) or {}).items() if conv == conv_id}


def _preset(mode: str, profile: Optional[str]) -> dict:
    try:
        from cqa import llm
        return llm.preset(mode, profile)
    except Exception as exc:
        return {"error": str(exc)[:200]}


def load_set(group_id: str, records: Any = None, source: str = "auto") -> tuple[list[dict], str]:
    if records is not None:
        recs, _ = ingest.parse_payload(records)
        return ingest.normalize_records(recs, group_id), "records"
    path = ingest.ROOT / "data/raw/dev_records.json" if group_id == "DEV" else ingest.raw_path(group_id)
    if source != "api" and path.exists() and (source == "file" or group_id == "DEV"):
        return ingest.ingest_file(path, group_id=group_id), "file"
    return ingest.fetch_group(group_id), "api"


def official_version(state) -> int:
    """The organizers' checklist: the stored version flagged official (v1 mirrors data/checklists/v1.json)."""
    for cl in sorted(state.checklists.versions(), key=lambda c: c.version):
        if cl.official:
            return cl.version
    return 1


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _merge_by_conversation(old: list[dict], new: list[dict], redone: set[str]) -> list[dict]:
    """Rows of `old` keep their place; rows of re-run dialogues are replaced by `new` in the same place."""
    fresh: dict[str, list[dict]] = {}
    for r in new:
        fresh.setdefault(r["conversation_id"], []).append(r)
    out, placed = [], set()
    for r in old:
        cid = r["conversation_id"]
        if cid not in redone:
            out.append(r)
        elif cid not in placed:
            placed.add(cid)
            out.extend(fresh.get(cid, []))
    for cid, rows in fresh.items():
        if cid not in placed:
            out.extend(rows)
    return out


def run_control(state, group_id: str, *, records: Any = None, source: str = "auto", mode: str = "accurate",
                profile: Optional[str] = None, limit: Optional[int] = None, version: Optional[int] = None,
                out_dir: Optional[Path] = None, ids: Optional[list[str]] = None, allow_mock: bool = False,
                dry_run: bool = False,
                announce: Optional[Callable[[str], None]] = None) -> dict:
    t0 = time.perf_counter()
    only = {x.strip() for x in ids or [] if x.strip()}
    # -- everything that decides the result is validated BEFORE anything is stored or any model is called
    profile = profile or llm.active_profile()          # --profile > CQA_PROFILE (env/.env) > config default_profile; NOT the UI
    profiles = sorted(p for p in llm.config().get("profiles", {}) if not p.startswith("_"))
    if profile not in profiles:
        raise ingest.IngestError(f"неизвестный профиль «{profile}»; доступны: {', '.join(profiles)}")
    official = official_version(state)
    active = state.checklists.active_version()
    use_version = official if version is None else version
    known = sorted(c.version for c in state.checklists.versions())
    if use_version not in known:
        raise ingest.IngestError(f"версии чек-листа {use_version} нет; есть: {', '.join(map(str, known))}")
    cl = state.checklists.get(use_version)
    key = state.eval_mode_key(mode, profile)
    preset = _preset(mode, profile)
    model = preset.get("model") if isinstance(preset, dict) else None
    prov, _, mname = (model or "").partition(":")
    def refuse_mock() -> None:
        raise ingest.IngestError(f"модель профиля «{profile}» — {model} (заглушка, отвечает «Да» на всё): для группы {group_id} "
                                 f"это не контроль. Нужен реальный профиль (cloud) или явный флаг --allow-mock (только репетиция).")

    if prov == "mock" and not allow_mock and (group_id != "DEV" or source == "api"):
        refuse_mock()                                  # before load_set: no API call for a run that is refused anyway
    convs, src = load_set(group_id, records, source)
    if prov == "mock" and not allow_mock and src == "api":
        refuse_mock()
    if prov == "ollama":  # fail loudly BEFORE the run instead of N "error" dialogues
        problem = llm.ollama_problem(mname)
        if problem:
            raise ingest.IngestError(f"профиль «{profile}»: {problem}. Контроль не начат.")
    if limit:
        convs = convs[:limit]
    if only:  # organizers' ids in the fetched set (before any renaming); the internal ids are resolved after ingest
        unknown = sorted(only - {c["id"] for c in convs} - {(c.get("meta") or {}).get("source_id") for c in convs})
        if unknown:
            raise ingest.IngestError(f"--ids: в наборе {group_id} нет диалогов: {', '.join(unknown)}")
    out_dir = Path(out_dir or RESULTS)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in group_id)
    paths = {"json": out_dir / f"control_{safe}.json", "csv": out_dir / f"control_{safe}.csv",
             "trace": out_dir / f"control_{safe}_trace.jsonl"}
    existing: Optional[dict] = None
    if only:  # validate BEFORE storing anything or spending money: the records are merged into the existing export
        if not paths["json"].exists():
            raise ingest.IngestError(f"--ids: нет {paths['json']} — сначала полный прогон без --ids")
        existing = json.loads(paths["json"].read_text(encoding="utf-8"))
        if (existing.get("checklist") or {}).get("version") != cl.version:
            raise ingest.IngestError(f"--ids: в {paths['json'].name} чек-лист версии {(existing.get('checklist') or {}).get('version')}, "
                                     f"а этот прогон — версии {cl.version}; в одном файле версии не смешиваем")
        if (existing.get("run") or {}).get("profile") != profile:
            raise ingest.IngestError(f"--ids: в {paths['json'].name} профиль {(existing.get('run') or {}).get('profile')}, "
                                     f"а этот прогон — {profile}; в одном файле профили не смешиваем")
        if (existing.get("run") or {}).get("limit") != (limit or None):
            raise ingest.IngestError(f"--ids: в {paths['json'].name} прогон шёл с --limit {(existing.get('run') or {}).get('limit')}, "
                                     f"а сейчас --limit {limit or None}: набор должен совпадать (укажите тот же --limit)")
    n_selected = len(only) if only else len(convs)
    if dry_run:  # plan only: nothing is stored, no model is called
        conv_ids = all_ids = [c["id"] for c in convs]
    else:
        loaded = state.ingest(convs, src)
        conv_ids = loaded["ids"]
        all_ids = list(conv_ids)
    if only and not dry_run:  # re-run just these dialogues (organizers' ids or internal ids)
        by_source = {}
        for cid in conv_ids:
            by_source[cid] = cid
            by_source[state.conversation(cid)["meta"].get("source_id") or cid] = cid
        conv_ids = [cid for cid in conv_ids if cid in {by_source[x] for x in only}]
    ui_profile = state.settings()["profile"]
    warnings: list[str] = []
    if version is None and active != official:
        warnings.append(f"ВНИМАНИЕ: в хранилище активна версия чек-листа v{active}, а официальная — v{official}. Контроль считается "
                        f"по официальной v{official} (активная НЕ используется). Если организаторы просили другую версию — "
                        f"укажите --version N явно.")
    if profile != ui_profile:
        warnings.append(f"ВНИМАНИЕ: контроль считается профилем «{profile}», а профиль интерфейса «{ui_profile}». Замена критерия "
                        f"в UI не найдёт эти ответы и пересчитает ВСЕ критерии. Перед заменой выровняйте: "
                        f"curl -X POST localhost:8000/api/config -H 'Content-Type: application/json' "
                        f"-d '{{\"profile\":\"{profile}\"}}' (или выберите профиль в UI).")
    chk_hash = hashlib.sha1("".join(c.hash for c in cl.criteria).encode()).hexdigest()[:10]
    plan_text = [f"Группа: {group_id} | источник: {src} | диалогов: {len(conv_ids)}"
                 + (f" (из {len(all_ids)}, режим --ids)" if only else ""),
                 f"Чек-лист: v{cl.version}{' (официальный)' if cl.version == official else ''}, хеш {chk_hash}, «{cl.name}», критериев {len(cl.criteria)}:"]
    plan_text += [f"  {c.id}. {c.title}" for c in cl.criteria]
    plan_text += [f"Профиль: {profile} | модель: {model} | режим: {mode} ({key}) | воркеров: {state.workers}",
                  "Живой расчёт: force + без LLM-кэша" + (f" | БД: {state.db.path}" if hasattr(state.db, "path") else "")]
    plan_text += warnings
    if dry_run:
        plan_text.append("DRY-RUN: данные не сохранялись, модель не вызывалась. Боевой запуск — та же команда без --dry-run.")
    if announce:
        announce("\n".join(plan_text))
    if dry_run:
        return {"dry_run": True, "status": "dry-run", "group_id": group_id, "conversations": n_selected, "version": cl.version,
                "profile": profile, "model": model, "mode": mode, "warnings": warnings, "plan": "\n".join(plan_text),
                "source": src}
    # Decisions as the run sees them (start_run pins the same list): a reviewer decision saved AFTER this point
    # never leaks into this run's export (strict review PROD-01).
    decisions_at_start = {cid: state.db.list_decisions(conv_id=cid) for cid in conv_ids}
    run_db = getattr(state.runs, "db", state.db)
    with _RunCapture(run_db) as capture:
        st = state.runs.start_run(version=cl.version, conv_ids=conv_ids, mode=key, profile=profile, live=True,
                                  force=True, wait=True)
    run_id = st.get("run_id")
    snap_rows = {r["id"]: r for r in st["per_conv"]}
    errors = {r["id"]: r.get("error") for r in st["per_conv"] if r.get("status") == "error" or r.get("error")}

    conversations, csv_rows, traces = [], [], []
    for cid in conv_ids:
        conv = state.conversation(cid)
        source_id = conv["meta"].get("source_id") or cid

        def ext(mid: Optional[str]) -> Optional[str]:  # organizers' message ids even after an id rename
            return f"{source_id}:{mid.split(':')[-1]}" if mid else mid

        # PROD-01: only THIS run's own answers -- the in-memory copies written by run_id (a later run B may already
        # have overwritten the table rows), falling back to table rows still stamped with this run_id. A dialogue with
        # a partial failure keeps the answers of this run; the missing criterion shows "нет ответа" + the error,
        # total stays None. Older answers are never passed off as fresh.
        own = capture.evals(run_id, cid)
        stored = current_evals(state.db, cid, cl, modes=[key])
        for e in stored:
            if run_id and _run_id_of(e) == run_id:
                own.setdefault(e.criterion_id, e)
        evals = [own[c.id] for c in cl.criteria if c.id in own]
        stale = sum(1 for e in stored if _run_id_of(e) != run_id)
        snap = snap_rows.get(cid) or {}
        if snap.get("status") == "done" and isinstance(snap.get("score"), dict):
            result = Score.model_validate(snap["score"])   # the run's immutable score (decisions pinned at its start)
        else:
            result = engine.score(cl, evals, decisions_at_start.get(cid, []), conv_id=cid, strict=False)
        by_eval = {e.criterion_id: e for e in evals}
        crit_rows = []
        for line in result.lines:
            ev = by_eval.get(line.criterion_id)
            evidence = [{"message_id": ext(x.message_id), "quote": x.quote, "valid": x.valid} for x in (ev.evidence if ev else [])]
            validation = validation_status(ev)
            row = {"criterion_id": line.criterion_id, "title": line.title, "answer": _num(line.answer),
                   "answer_label": _label(line.answer), "points": _num(line.points), "max_points": _num(line.max_points),
                   "weight_effective": round(line.weight_effective, 4), "is_na": line.is_na,
                   "critical": line.critical, "critical_hit": line.critical_hit,
                   "source": line.source,
                   # CQA-02: acceptance state next to the answer -- a rejected/flagged model answer is preliminary
                   "status": export_status(line.source, validation) if line.answer is not None else "missing",
                   "validation_status": validation,
                   "evidence_valid": (all(x.valid for x in ev.evidence) if ev.evidence else None) if ev else None,
                   "review_reasons": list(ev.review_reasons) if ev else [],
                   "explanation": ev.explanation if ev else "", "evidence": evidence,
                   "checked_range": [ext(x) for x in ev.checked_range] if ev and ev.checked_range else None,
                   "na_reason": ev.na_reason if ev else None, "confidence": ev.confidence if ev else None,
                   "needs_review": ev.needs_review if ev else True, "model": ev.model if ev else None,
                   "prompt_version": ev.prompt_version if ev else None,
                   "error": None if ev else errors.get(cid)}
            crit_rows.append(row)
        # dialogue level: the total is preliminary while any criterion is not final (rejected/flagged/missing answer)
        preliminary = result.total is None or any(r["status"] != "final" for r in crit_rows)
        for row in crit_rows:
            evidence = row["evidence"]
            csv_rows.append({"conversation_id": source_id, "channel": conv["channel"], "language": conv["language"],
                             "criterion_id": row["criterion_id"], "title": row["title"], "answer": row["answer_label"],
                             "points": "" if row["points"] is None else row["points"], "weight": row["max_points"],
                             "explanation": row["explanation"], "evidence": " | ".join(e["quote"] for e in evidence),
                             "evidence_message_ids": " ".join(e["message_id"] or "" for e in evidence),
                             "na_reason": row["na_reason"] or "", "confidence": row["confidence"], "error": row["error"] or "",
                             "needs_review": row["needs_review"], "total": "" if result.total is None else result.total,
                             "critical": result.critical, "status": row["status"],
                             "validation_status": row["validation_status"],
                             "evidence_valid": "" if row["evidence_valid"] is None else row["evidence_valid"],
                             "preliminary": preliminary})
        for ev in evals:
            traces.append({"conversation_id": source_id, "internal_id": cid, "criterion_id": ev.criterion_id,
                           "criterion_hash": ev.criterion_hash, "prompt_version": ev.prompt_version, "mode": ev.mode,
                           "provider": ev.provider, "model": ev.model, "created_at": ev.created_at,
                           "latency_ms": ev.latency_ms, "tokens_in": ev.tokens_in, "tokens_out": ev.tokens_out,
                           "cost_usd": ev.cost_usd, "cost_kzt": ev.cost_kzt, "run_id": _run_id_of(ev), "raw": ev.raw})
        conversations.append({"conversation_id": source_id, "internal_id": cid, "group_id": conv["group_id"],
                              "channel": conv["channel"], "language": conv["language"], "total": result.total,
                              # CQA-02: a computed total that still rests on a rejected/flagged model answer is
                              # "provisional", never "complete" next to preliminary=true
                              "status": "provisional" if (preliminary and result.status == "complete") else result.status,
                              "score_status": result.status, "critical": result.critical,
                              "critical_criteria": result.critical_criteria, "sections": result.sections,
                              "needs_review": any(e.needs_review for e in evals), "preliminary": preliminary,
                              "result_status": "preliminary" if preliminary else "final", "criteria": crit_rows,
                              "formula_steps": result.formula_steps, "cost_kzt": round(sum(e.cost_kzt for e in evals), 4),
                              "latency_ms": sum(e.latency_ms for e in evals),
                              "models": sorted({e.model for e in evals if e.model}), "error": errors.get(cid),
                              "stale_answers_ignored": stale})

    run_info = {k: st.get(k) for k in ("run_id", "status", "version", "done", "total", "from_cache", "llm_calls",
                                       "cost_kzt", "elapsed", "started_at", "finished_at", "errors")}
    run_info.update({"mode": mode, "mode_key": key, "profile": profile, "live": True, "force": True,
                     "preset": _preset(mode, profile), "source": src, "limit": limit or None, "git_commit": _git_commit()})
    doc = {"group_id": group_id, "generated_at": st.get("finished_at"), "run": run_info,
           "checklist": {"version": cl.version, "name": cl.name, "criteria": [
               {"id": c.id, "title": c.title, "hash": c.hash, "weight": c.weight, "answer_type": c.answer_type,
                "scale": c.scale, "na_allowed": c.na_allowed, "critical": c.critical} for c in cl.criteria]},
           "conversations": conversations}
    if existing is not None:  # --ids: replace only the re-run dialogues inside the existing export, keep the rest as is
        redone = {c["conversation_id"] for c in conversations}
        merged = {c["conversation_id"]: c for c in conversations}
        doc = dict(existing)
        doc["conversations"] = [merged.get(c["conversation_id"], c) if c["conversation_id"] in redone else c
                                for c in existing["conversations"]]
        doc["conversations"] += [c for c in conversations if c["conversation_id"] not in
                                 {x["conversation_id"] for x in existing["conversations"]}]
        doc["generated_at"] = st.get("finished_at")
        doc["reruns"] = list(existing.get("reruns") or []) + [
            {"run_id": st.get("run_id"), "finished_at": st.get("finished_at"), "ids": sorted(redone),
             "llm_calls": st.get("llm_calls"), "git_commit": run_info["git_commit"]}]
        csv_rows = _merge_by_conversation(_read_csv(paths["csv"]), csv_rows, redone)
        traces = _merge_by_conversation(_read_jsonl(paths["trace"]), traces, redone)
        conversations = doc["conversations"]
    totals = [c["total"] for c in conversations if c["total"] is not None]
    failed = [c for c in conversations if c["error"] or c["status"] == "incomplete"]
    # top-level summary of the FILE as it is now (after --ids merges too), so nobody has to read run.status of run 1
    doc["summary"] = {"status": "completed" if not failed and st["status"] == "completed" else "failed",
                      "conversations": len(conversations), "evaluated": len(totals), "failed": len(failed),
                      "failed_ids": [c["conversation_id"] for c in failed],
                      # CQA-02: dialogues whose total is not final (an answer rejected by the validator or flagged)
                      "preliminary": sum(1 for c in conversations if c.get("preliminary", True)),
                      "preliminary_ids": [c["conversation_id"] for c in conversations if c.get("preliminary", True)]}
    if existing is not None:
        doc["run"] = {**doc["run"], "status_after_reruns": doc["summary"]["status"]}
    paths["json"].write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    fields = list(csv_rows[0]) if csv_rows else ["conversation_id"]
    with paths["csv"].open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _csv_safe(v) for k, v in r.items()} for r in csv_rows)
    with paths["trace"].open("w", encoding="utf-8") as fh:
        for t in traces:
            fh.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")

    return {"status": st["status"], "group_id": group_id, "conversations": len(conversations),
            "evaluated": len(totals), "failed": len(failed), "failed_ids": [c["conversation_id"] for c in failed],
            "mean_total": round(mean(totals), 2) if totals else None,
            "critical": sum(c["critical"] for c in conversations),
            "needs_review": sum(c["needs_review"] for c in conversations),
            "llm_calls": st.get("llm_calls"), "cost_kzt": round(sum(c["cost_kzt"] for c in conversations), 4),
            "elapsed": round(time.perf_counter() - t0, 1), "mode": mode, "profile": profile,
            "ui_profile": ui_profile, "ui_profile_mismatch": profile != ui_profile,
            "version": cl.version, "official_version": official, "warnings": warnings, "plan": "\n".join(plan_text),
            "model": run_info["preset"].get("model") if isinstance(run_info["preset"], dict) else None,
            "files": {k: str(v) for k, v in paths.items()}}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("group_id")
    ap.add_argument("--mode", default="fast", choices=["fast", "accurate"], help="fast = one Gemini call per dialogue (measured, used for the control); accurate is not benchmarked")
    ap.add_argument("--profile", default=None, help="cloud | claude | local | mock (default: config default_profile = cloud, NOT the UI setting)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="validate everything, print the plan, call NO model, store nothing (exit 0)")
    ap.add_argument("--allow-mock", action="store_true", help="allow the mock model (rehearsal of the process only; refused for real groups / --source api)")
    ap.add_argument("--version", type=int, default=None, help="checklist version (default: the OFFICIAL one, v1 -- not the active)")
    ap.add_argument("--ids", default=None, help="comma-separated dialogue ids: re-run only these and update their records "
                    "in the existing control_<GID>.json/.csv/_trace.jsonl (same version and profile)")
    ap.add_argument("--source", default="auto", choices=["auto", "api", "file"])
    ap.add_argument("--db", default=None, help="SQLite path (default: env CQA_DB or results/cqa.sqlite)")
    ap.add_argument("--out", default=None, help="output dir (default: results/)")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args(argv)

    from api.state import AppState

    state = AppState(db_path=args.db, autoload=False, calibrate_audio=False, workers=args.workers)
    if not state.evaluator_ready:
        print("ERROR: оценщик (cqa.evaluator) недоступен", file=sys.stderr)
        return 2
    try:
        rep = run_control(state, args.group_id, source=args.source, mode=args.mode, profile=args.profile,
                          limit=args.limit, version=args.version, out_dir=Path(args.out) if args.out else None,
                          ids=[x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None,
                          allow_mock=args.allow_mock, dry_run=args.dry_run, announce=lambda text: print(text, flush=True))
    except ingest.IngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if rep.get("dry_run"):
        return 0
    print(f"Контроль {rep['group_id']}: {rep['conversations']} диалогов, оценено {rep['evaluated']}, "
          f"ошибок {rep['failed']}, средний итог {rep['mean_total']}, грубых ошибок {rep['critical']}, "
          f"на проверку {rep['needs_review']}")
    print(f"Модель {rep['model']} ({rep['profile']}, {rep['mode']}), вызовов LLM {rep['llm_calls']}, "
          f"{rep['cost_kzt']} ₸, {rep['elapsed']} с")
    for w in rep["warnings"]:  # repeated after the run so they are the last thing on screen
        print(w)
    for name, path in rep["files"].items():
        print(f"  {name}: {path}")
    if rep["failed_ids"]:
        print("  нужна повторная оценка: " + ", ".join(rep["failed_ids"]))
    return 0 if rep["status"] == "completed" and not rep["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
