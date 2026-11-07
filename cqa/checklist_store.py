"""Checklist versions: load v1, create a version from edits, hashes, diff with needs_reeval, active version.

Versions are immutable: every edit creates version N+1 (stored in SQLite via cqa.db). Version 1 always mirrors
data/checklists/v1.json (the organizers' PDF). Re-evaluation is needed only for criteria whose content hash
changed (models.criterion_hash: wording, type, scale, labels, N/A, gross-error polarity, guidance);
weight / section / order changes only recompute totals.

    from cqa.checklist_store import ChecklistStore
    store = ChecklistStore()                       # default DB (CQA_DB), seeds and activates v1
    res = store.apply([{"op": "replace", "criterion_id": "C5", "criterion": {"title": "...", "description": "..."}}],
                      change_note="Замена C5 по запросу организаторов")
    res.checklist.version, res.diff.needs_reeval   # 2, ["C5"]
    store.activate(2)
    res = store.create(body_from_ui)               # POST /api/checklists: full checklist body without version

Change ops for apply()/apply_changes():
    {"op": "add", "criterion": {...}, "after": "C3" | "position": 0}        id auto "C<n>" if missing
    {"op": "remove", "criterion_id": "C2"}
    {"op": "update", "criterion_id": "C4", "fields": {"description": "...", "weight": 25, ...}}
    {"op": "replace", "criterion_id": "C5", "criterion": {...}}             keeps position; weight/section inherited
    {"op": "set_weight", "criterion_id": "C1", "weight": 15}
    {"op": "set_type", "criterion_id": "C2", "answer_type": "scale", "scale": [0, 1, 2]}
    {"op": "set_scale", "criterion_id": "C6", "scale": [...], "scale_labels": {...}}
    {"op": "set_na", "criterion_id": "C5", "na_allowed": true, "na_rule": "N/A — ..."}
    {"op": "set_checklist", "fields": {"name": "...", "rounding": {...}, "sections": [...]}}
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from pydantic import BaseModel, Field

from .db import Database, get_db
from .models import ChecklistDiff, ChecklistVersion, CriterionChange, now_iso

__all__ = ["V1_PATH", "COMPARED_FIELDS", "load_checklist", "validate_body", "apply_changes", "diff",
           "describe_diff", "CreateResult", "ChecklistStore"]

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = ROOT / "data" / "checklists" / "v1.json"
COMPARED_FIELDS = ("section", "title", "description", "answer_type", "scale", "scale_labels", "weight", "na_allowed",
                   "na_rule", "critical", "critical_when", "critical_rule", "guidance")
_NO_REEVAL = {"weight", "section"}


def load_checklist(path: Union[str, Path] = V1_PATH) -> ChecklistVersion:
    return ChecklistVersion.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------------- validation

def _criteria_warnings(raw_criteria: Iterable[Any], cl: ChecklistVersion) -> list[str]:
    warnings: list[str] = []
    for raw in raw_criteria:
        r = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
        if r.get("critical") and not r.get("critical_when"):
            warnings.append(f"{r.get('id')}: не указано, какой ответ означает грубую ошибку — принято «Да» = нарушение "
                            f"(critical_when = yes).")
    total = cl.weight_sum
    if total <= 0:
        warnings.append("У всех критериев вес 0 — итог не будет рассчитываться.")
    elif abs(total - 100) > 1e-9:
        warnings.append(f"Сумма весов {total:g} ≠ 100 — итог будет нормирован к 100.")
    for c in cl.criteria:
        if c.na_allowed and not c.na_rule:
            warnings.append(f"{c.id}: N/A разрешён, но условие неприменимости не описано.")
        if not c.description:
            warnings.append(f"{c.id}: нет описания — оценщик опирается только на название.")
        if c.answer_type == "scale" and not c.scale_labels:
            warnings.append(f"{c.id}: у ступеней шкалы нет описаний — оценка будет менее точной.")
        if c.weight == 0 and not c.critical:
            warnings.append(f"{c.id}: вес 0 и не грубая ошибка — критерий не влияет на итог.")
    return warnings


def validate_body(body: Union[ChecklistVersion, dict]) -> tuple[ChecklistVersion, list[str]]:
    """Validate a checklist from the UI; returns (checklist, warnings). Raises ValueError (pydantic) on errors."""
    raw = body.model_dump() if isinstance(body, BaseModel) else dict(body)
    cl = ChecklistVersion.model_validate(raw)
    return cl, _criteria_warnings(raw.get("criteria") or [], cl)


# -------------------------------------------------------------------------------------------- changes

def _next_id(ids: Iterable[str]) -> str:
    nums = [int(i[1:]) for i in ids if len(i) > 1 and i[0] in "CcСс" and i[1:].isdigit()]
    return f"C{max(nums, default=0) + 1}"


def _index(criteria: list[dict], cid: str) -> int:
    for i, c in enumerate(criteria):
        if c["id"] == cid:
            return i
    raise ValueError(f"Критерий {cid} не найден")


def apply_changes(base: ChecklistVersion, changes: Iterable[dict]) -> tuple[ChecklistVersion, list[str]]:
    """Apply edit ops to a copy of `base`; returns (draft checklist with version 0, warnings)."""
    data = base.model_dump()
    criteria: list[dict] = data["criteria"]
    raw_new: list[dict] = []
    for ch in changes:
        op = (ch.get("op") or "").strip().lower()
        cid = ch.get("criterion_id")
        if op == "add":
            crit = dict(ch.get("criterion") or {})
            crit.setdefault("id", _next_id(c["id"] for c in criteria))
            if any(c["id"] == crit["id"] for c in criteria):
                raise ValueError(f"Критерий {crit['id']} уже есть в чек-листе")
            if ch.get("after"):
                pos = _index(criteria, ch["after"]) + 1
            elif ch.get("position") is not None:
                pos = int(ch["position"])
            else:
                pos = len(criteria)
            criteria.insert(pos, crit)
            raw_new.append(crit)
        elif op == "remove":
            criteria.pop(_index(criteria, cid))
        elif op in ("update", "set_weight", "set_type", "set_scale", "set_na"):
            fields = dict(ch.get("fields") or {k: v for k, v in ch.items() if k not in ("op", "criterion_id")})
            i = _index(criteria, cid)
            merged = {**criteria[i], **fields}
            if fields.get("answer_type") == "binary":
                merged["scale"], merged["scale_labels"] = None, None
            if "scale" in fields and "scale_labels" not in fields:
                merged["scale_labels"] = criteria[i].get("scale_labels")  # kept labels are filtered to the new steps
            if "id" in fields and fields["id"] != cid and any(c["id"] == fields["id"] for c in criteria):
                raise ValueError(f"Критерий {fields['id']} уже есть в чек-листе")
            criteria[i] = merged
            raw_new.append(merged)
        elif op == "replace":
            i = _index(criteria, cid)
            crit = dict(ch.get("criterion") or {})
            crit.setdefault("id", cid)
            crit.setdefault("weight", criteria[i]["weight"])
            crit.setdefault("section", criteria[i]["section"])
            if crit["id"] != cid and any(c["id"] == crit["id"] for c in criteria):
                raise ValueError(f"Критерий {crit['id']} уже есть в чек-листе")
            criteria[i] = crit
            raw_new.append(crit)
        elif op == "set_checklist":
            for k, v in (ch.get("fields") or {}).items():
                if k in ("name", "rounding", "sections", "change_note", "scoring"):
                    data[k] = v
        else:
            raise ValueError(f"Неизвестная операция: {op or '?'}")
    data.update(version=0, created_at="", official=False)
    cl = ChecklistVersion.model_validate(data)
    return cl, _criteria_warnings(raw_new, cl)


# ----------------------------------------------------------------------------------------------- diff

def _canon(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def diff(a: ChecklistVersion, b: ChecklistVersion) -> ChecklistDiff:
    """Compare two versions by criterion id. needs_reeval: ids in `b` whose content hash is new;
    reusable: ids whose evaluations stay valid (same hash, also under a new id); rescore_only: only weight/section."""
    A, B = a.by_id, b.by_id
    a_hashes = {c.hash for c in a.criteria}
    added = [c.model_dump() for c in b.criteria if c.id not in A]
    removed = [c.model_dump() for c in a.criteria if c.id not in B]
    changed: list[CriterionChange] = []
    needs, reusable, rescore = [], [], []
    for c in b.criteria:
        old = A.get(c.id)
        if old is None:
            (reusable if c.hash in a_hashes else needs).append(c.id)
            continue
        fields = [f for f in COMPARED_FIELDS if _canon(getattr(old, f)) != _canon(getattr(c, f))]
        reeval = old.hash != c.hash
        if fields:
            changed.append(CriterionChange(criterion_id=c.id, fields=fields, needs_reeval=reeval,
                                           before=old.model_dump(), after=c.model_dump()))
        if reeval:
            needs.append(c.id)
        else:
            reusable.append(c.id)
            if fields:
                rescore.append(c.id)
    checklist_fields = [f for f in ("name", "rounding", "sections") if _canon(getattr(a, f)) != _canon(getattr(b, f))]
    common = [i for i in A if i in B]
    if [i for i in B if i in A] != common:
        checklist_fields.append("order")
    warnings = []
    if abs(b.weight_sum - 100) > 1e-9:
        warnings.append(f"Сумма весов версии {b.version} = {b.weight_sum:g} ≠ 100 — итог нормируется к 100.")
    return ChecklistDiff(from_version=a.version, to_version=b.version, added=added, removed=removed, changed=changed,
                         needs_reeval=needs, reusable=reusable, rescore_only=rescore, checklist_fields=checklist_fields,
                         warnings=warnings)


def describe_diff(d: ChecklistDiff) -> str:
    """Short Russian change note, e.g. «C5 изменён (title, description) — нужна переоценка; вес C1»."""
    parts = []
    for c in d.added:
        parts.append(f"добавлен {c['id']} «{c['title']}»")
    for c in d.removed:
        parts.append(f"удалён {c['id']} «{c['title']}»")
    for ch in d.changed:
        if ch.needs_reeval:
            parts.append(f"{ch.criterion_id} изменён ({', '.join(ch.fields)}) — нужна переоценка")
        else:
            parts.append(f"{ch.criterion_id}: {', '.join('вес' if f == 'weight' else 'секция' for f in ch.fields)}")
    if d.checklist_fields:
        parts.append("чек-лист: " + ", ".join(d.checklist_fields))
    return "; ".join(parts) if parts else "без изменений"


# ---------------------------------------------------------------------------------------------- store

class CreateResult(BaseModel):
    checklist: ChecklistVersion
    diff: ChecklistDiff
    warnings: list[str] = Field(default_factory=list)

    def response(self) -> dict:
        """Body for POST /api/checklists: the new checklist (as the frontend expects) + warnings + diff."""
        return {**self.checklist.model_dump(mode="json"), "warnings": self.warnings,
                "diff": self.diff.model_dump(mode="json")}


class ChecklistStore:
    def __init__(self, db: Optional[Database] = None, seed_path: Union[str, Path] = V1_PATH):
        self.db = db or get_db()
        self.seed_path = Path(seed_path)
        self._lock = threading.Lock()
        self.ensure_seed()

    def ensure_seed(self) -> ChecklistVersion:
        """Version 1 always equals the official file; activate it if nothing is active."""
        v1 = load_checklist(self.seed_path).model_copy(update={"version": 1})
        stored = self.db.get_checklist_version(1)
        if stored is None or stored.model_dump() != v1.model_dump():
            self.db.save_checklist_version(v1, replace=True)
            if stored is not None:
                self.db.add_audit("checklist", "1", "checklist.v1_synced", {"source": str(self.seed_path.name)})
        active = self.db.get_active_version()
        if active is None or self.db.get_checklist_version(active) is None:
            self.db.set_active_version(1)
        return v1

    # -- reading
    def versions(self) -> list[ChecklistVersion]:
        return self.db.list_checklist_versions()

    def get(self, version: Union[int, str]) -> ChecklistVersion:
        cl = self.db.get_checklist_version(int(version))
        if cl is None:
            raise KeyError(f"Версия чек-листа {version} не найдена")
        return cl

    def latest(self) -> ChecklistVersion:
        return self.get(self.db.max_checklist_version())

    def active_version(self) -> int:
        return self.db.get_active_version() or 1

    def active(self) -> ChecklistVersion:
        return self.get(self.active_version())

    def activate(self, version: Union[int, str]) -> ChecklistVersion:
        cl = self.get(version)
        self.db.set_active_version(cl.version)
        self.db.add_audit("checklist", str(cl.version), "checklist.activated", {"version": cl.version})
        return cl

    def diff(self, a: Union[int, str], b: Union[int, str]) -> ChecklistDiff:
        return diff(self.get(a), self.get(b))

    # -- writing
    def _save_new(self, draft: ChecklistVersion, base: ChecklistVersion, change_note: Optional[str],
                  warnings: list[str], activate: bool) -> CreateResult:
        with self._lock:
            for _ in range(5):
                version = self.db.max_checklist_version() + 1
                cl = draft.model_copy(update={"version": version, "created_at": now_iso(), "parent_version": base.version,
                                              "checklist_id": base.checklist_id, "official": False,
                                              "name": draft.name or base.name, "source": None})
                d = diff(base, cl)
                note = (change_note or "").strip()
                if note == (base.change_note or "").strip():  # UI echoed the base note: describe the edit instead
                    note = ""
                cl = cl.model_copy(update={"change_note": note or describe_diff(d)})
                try:
                    self.db.save_checklist_version(cl, replace=False)
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise RuntimeError("Не удалось сохранить новую версию чек-листа")
        if not d.added and not d.removed and not d.changed and not d.checklist_fields:
            warnings = warnings + ["Изменений относительно базовой версии нет."]
        self.db.add_audit("checklist", str(cl.version), "checklist.created",
                          {"version": cl.version, "parent_version": base.version, "needs_reeval": d.needs_reeval,
                           "change_note": cl.change_note})
        if activate:
            self.activate(cl.version)
        return CreateResult(checklist=cl, diff=d, warnings=list(dict.fromkeys(warnings + d.warnings)))

    def create(self, body: Union[ChecklistVersion, dict], *, base_version: Optional[int] = None,
               change_note: Optional[str] = None, activate: bool = False) -> CreateResult:
        """New version from a full checklist body (UI). Version / created_at / hashes in the body are ignored.
        Base for the diff: base_version, else body.parent_version, else body.version, else the active version."""
        raw = body.model_dump() if isinstance(body, BaseModel) else dict(body)
        base_no = base_version or raw.get("parent_version") or raw.get("version") or self.active_version()
        base = self.get(base_no)
        raw = {**raw, "version": 0}
        draft, warnings = validate_body(raw)
        return self._save_new(draft, base, change_note if change_note is not None else raw.get("change_note"),
                              warnings, activate)

    def apply(self, changes: Iterable[dict], *, base_version: Optional[int] = None, change_note: str = "",
              activate: bool = False) -> CreateResult:
        """New version = base (default: active) + edit ops (see module doc)."""
        base = self.get(base_version or self.active_version())
        draft, warnings = apply_changes(base, list(changes))
        return self._save_new(draft, base, change_note, warnings, activate)
