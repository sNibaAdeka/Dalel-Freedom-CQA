"""Shared data contracts of «Дәлел» (Freedom CQA), pydantic v2.

Source: TASKS.md §1.2. API response shapes follow the built frontend (branch `frontend-wave1-review`:
docs/FRONTEND_HANDOFF.md, web/js/api.js, scripts/make_ui_mocks.py); where they differ, the frontend wins.
Deviations from TASKS §1.2 are listed in agents/status/a1-core.md.

Key rules encoded here:
- answers: "yes" | "no" | "na" | number (scale step); no "partial" (the PDF has none).
- Criterion.max_points always equals weight (PDF p.1: «Вес показывает, сколько баллов критерий может дать»).
- Criterion.critical_when is the machine polarity of a gross-error criterion (C8: "yes" = violation found).
- Criterion.hash is always recomputed from content (criterion_hash): it covers every field that changes the
  evaluator prompt (incl. guidance and scale_labels) and never weight / section / id.
- Evidence may point to any message: operator, client, bot or system (PDF p.3: «реплику или системное сообщение»).
  Evidence start/end are JavaScript UTF-16 offsets inside the message text, as the frontend checks them.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_serializer,
                      model_validator)

__all__ = [
    "Speaker", "Channel", "Language", "AnswerType", "BinaryAnswer", "Answer", "EvalMode",
    "AnswerValue", "normalize_answer", "criterion_hash", "now_iso", "HASHED_FIELDS",
    "Message", "Conversation", "Criterion", "ChecklistVersion", "Evidence", "CriterionEval",
    "ScoreLine", "Score", "AppealCreate", "Appeal", "DecisionCreate", "Decision", "DecisionResult",
    "AuditEvent", "Cell", "ConversationSummary", "ConversationDetail", "CriterionChange", "ChecklistDiff",
    "RunConvRow", "RunStatus", "AudioInfo", "Health", "LLMCall",
]

Speaker = Literal["client", "operator", "bot", "system"]
Channel = Literal["chat", "call"]
Language = Literal["RU", "KZ", "EN"]
AnswerType = Literal["binary", "scale", "numeric"]
BinaryAnswer = Literal["yes", "no"]
Answer = Union[Literal["yes", "no", "na"], float]
EvalMode = str  # "fast" | "accurate" | "manual" — free string, part of the evaluation cache key

# ----------------------------------------------------------------------------------------------- helpers

_YES = {"yes", "y", "да", "д", "иә", "ия", "true", "+"}
_NO = {"no", "n", "нет", "н", "жоқ", "жок", "false", "-"}
_NA = {"na", "n/a", "n.a.", "н/д", "н/п", "нп", "не применимо", "неприменимо", "not applicable", "қолданылмайды"}


def normalize_answer(value: Any) -> Optional[Answer]:
    """Map raw answers (LLM / UI / CSV) to "yes" | "no" | "na" | float; None stays None (= no answer yet).

    >>> normalize_answer("Да"), normalize_answer("N/A"), normalize_answer("15"), normalize_answer(True)
    ('yes', 'na', 15.0, 'yes')
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"Недопустимый ответ: {value!r}")
        return f
    if isinstance(value, str):
        s = value.strip().lower().replace("ё", "е")
        if s in _YES:
            return "yes"
        if s in _NO:
            return "no"
        if s in _NA:
            return "na"
        try:
            f = float(s.replace(",", "."))
        except ValueError:
            raise ValueError(f"Недопустимый ответ: {value!r}. Ожидается yes / no / na или число из шкалы.") from None
        if not math.isfinite(f):
            raise ValueError(f"Недопустимый ответ: {value!r}")
        return f
    raise ValueError(f"Недопустимый ответ: {value!r}")


def _num(x: Any) -> Union[int, float]:
    f = float(x)
    return int(f) if f.is_integer() else f


def _num_key(k: Any) -> str:
    try:
        return str(_num(str(k).replace(",", ".")))
    except ValueError:
        return str(k).strip()


HASHED_FIELDS = ("title", "description", "answer_type", "scale", "scale_labels", "na_allowed", "na_rule",
                 "critical", "critical_when", "critical_rule", "guidance")


def criterion_hash(c: Union["Criterion", dict]) -> str:
    """sha1 over everything that changes the evaluator prompt; weight, section and id are excluded.

    Mirrors scripts/make_checklist_v1.py::h (tests/test_models.py checks that data/checklists/v1.json matches).
    """
    d = c.model_dump() if isinstance(c, BaseModel) else dict(c)
    labels = d.get("scale_labels")
    critical = bool(d.get("critical"))
    canon = {
        "title": (d.get("title") or "").strip(),
        "description": (d.get("description") or "").strip(),
        "answer_type": d.get("answer_type") or "binary",
        "scale": [_num(v) for v in d["scale"]] if d.get("scale") is not None else None,
        "scale_labels": {_num_key(k): str(v).strip() for k, v in labels.items()} if labels else None,
        "na_allowed": bool(d.get("na_allowed")),
        "na_rule": (d.get("na_rule") or "").strip(),
        "critical": critical,
        "critical_when": d.get("critical_when") if critical else None,
        "critical_rule": (d.get("critical_rule") or "").strip() or None,
        "guidance": [g.strip() for g in d.get("guidance") or [] if g and g.strip()],
    }
    s = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    """Local time with offset, seconds precision (e.g. 2026-11-07T15:04:05+05:00)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Optional answer that accepts raw values ("Да", "N/A", "15", True) and stores the normalized form.
AnswerValue = Annotated[Optional[Answer], BeforeValidator(normalize_answer)]


# ----------------------------------------------------------------------------------------- conversations

class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str                                   # "<conv_id>:<idx>", idx from 0
    idx: int
    speaker: Speaker
    text: str
    ts_raw: str = ""                          # original timestamp, kept as is
    t_sec: Optional[float] = None             # seconds from dialogue start (chats MM:SS; calls ISO - first event)
    rec_sec: Optional[float] = None           # seconds from recording start, pauses removed (calls only)
    translation_ru: Optional[str] = None      # optional RU subtitle for KZ/EN

    @field_validator("speaker", mode="before")
    @classmethod
    def _speaker(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


class Conversation(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    channel: Channel
    language: Language
    messages: list[Message] = Field(default_factory=list)
    audio_url: Optional[str] = None
    group_id: str = "DEV"
    meta: dict[str, Any] = Field(default_factory=dict)  # handoff_idx, wait_sec, recording_gaps, lang_switches, ...

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower()
            if "call" in s or "звон" in s or "voice" in s:
                return "call"
            if "chat" in s or "чат" in s:
                return "chat"
        return v

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().upper()
            return {"KK": "KZ", "KAZ": "KZ", "RUS": "RU", "ENG": "EN"}.get(s, s)
        return v


# --------------------------------------------------------------------------------------------- checklist

class Criterion(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: str = Field(min_length=1)                 # stable: "C1".."C8"
    section: Optional[str] = None
    title: str = Field(min_length=1)
    description: str = ""
    answer_type: AnswerType = "binary"
    scale: Optional[list[float]] = None           # scale: allowed steps (top step = full weight); numeric: [min..max]
    scale_labels: Optional[dict[str, str]] = None  # step -> wording from the PDF, e.g. {"20": "..."}
    weight: float = Field(default=0.0, ge=0)      # points before N/A redistribution (PDF); 0 = not in the total
    max_points: float = 0.0                       # derived: always == weight
    na_allowed: bool = False
    na_rule: str = ""
    critical: bool = False                        # gross-error flag criterion
    critical_when: Optional[BinaryAnswer] = None  # answer that raises the flag (C8: "yes")
    critical_rule: Optional[str] = None           # human-readable rule (not used by code)
    guidance: list[str] = Field(default_factory=list)  # clarifications for the evaluator (memo quotes)
    hash: str = ""                                # recomputed on validation, input value is ignored

    @field_validator("answer_type", mode="before")
    @classmethod
    def _answer_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower()
            return {"yes_no": "binary", "boolean": "binary", "bool": "binary", "да/нет": "binary",
                    "шкала": "scale", "number": "numeric", "числовой": "numeric"}.get(s, s)
        return v

    @field_validator("scale", mode="before")
    @classmethod
    def _scale(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = [p for p in v.replace(";", ",").split(",") if p.strip()]
        if isinstance(v, (list, tuple)):
            out = []
            for x in v:
                f = float(str(x).replace(",", ".")) if isinstance(x, str) else float(x)
                if not math.isfinite(f) or f < 0:
                    raise ValueError(f"Ступень шкалы должна быть неотрицательным числом: {x!r}")
                out.append(f)
            return sorted(set(out))
        return v

    @field_validator("critical_when", mode="before")
    @classmethod
    def _critical_when(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        a = normalize_answer(v)
        if a not in ("yes", "no"):
            raise ValueError("critical_when должен быть yes или no")
        return a

    @field_validator("guidance", mode="before")
    @classmethod
    def _guidance(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(g).strip() for g in v if g is not None and str(g).strip()]

    @model_validator(mode="after")
    def _derive(self) -> "Criterion":
        if self.answer_type == "binary":
            self.scale = None
            self.scale_labels = None
        else:
            if not self.scale:
                raise ValueError(f"{self.id}: для типа {self.answer_type} нужна шкала (список ступеней)")
            if max(self.scale) <= 0:
                raise ValueError(f"{self.id}: верхняя ступень шкалы должна быть больше 0")
            if self.scale_labels:
                lo = min(self.scale) if self.answer_type == "numeric" and len(self.scale) > 1 else 0.0
                keep = {}
                for k, label in self.scale_labels.items():
                    key = _num_key(k)
                    try:
                        val = float(key)
                    except ValueError:
                        continue
                    ok = val in self.scale if self.answer_type == "scale" else lo <= val <= max(self.scale)
                    if ok and str(label).strip():
                        keep[key] = str(label).strip()
                self.scale_labels = keep or None
        if self.critical:
            if self.answer_type != "binary":
                raise ValueError(f"{self.id}: грубая ошибка (critical) поддерживается только для критериев «да/нет»")
            if self.critical_when is None:
                rule = (self.critical_rule or "").strip().lower()
                self.critical_when = "no" if rule in _NO else "yes"
        else:
            self.critical_when = None
        self.max_points = self.weight
        self.hash = criterion_hash(self)
        return self

    # -- helpers used by engine / evaluator
    @property
    def scale_max(self) -> Optional[float]:
        return max(self.scale) if self.scale else None

    def check_answer(self, answer: Any) -> Answer:
        """Normalize and validate an answer for this criterion; raises ValueError with a Russian message."""
        a = normalize_answer(answer)
        if a is None:
            raise ValueError(f"{self.id}: нет ответа")
        if a == "na":
            if not self.na_allowed:
                raise ValueError(f"{self.id}: N/A не разрешён для критерия «{self.title}»")
            return a
        if self.answer_type == "binary":
            if a not in ("yes", "no"):
                raise ValueError(f"{self.id}: ожидается «да» или «нет», получено {answer!r}")
            return a
        if not isinstance(a, float):
            raise ValueError(f"{self.id}: ожидается число по шкале {self.scale}, получено {answer!r}")
        if self.answer_type == "scale":
            if a not in (self.scale or []):
                raise ValueError(f"{self.id}: значение {_num(a)} не входит в ступени шкалы {[_num(s) for s in self.scale or []]}")
            return a
        lo = min(self.scale) if self.scale and len(self.scale) > 1 else 0.0
        hi = max(self.scale or [0.0])
        if not lo <= a <= hi:
            raise ValueError(f"{self.id}: значение {_num(a)} вне диапазона {_num(lo)}–{_num(hi)}")
        return a


class ChecklistVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    checklist_id: str = "freedom-chat-quality"
    version: int = 0                               # assigned by checklist_store on save; 0 = unsaved draft
    name: str = ""
    criteria: list[Criterion] = Field(min_length=1)
    rounding: dict[str, Any] = Field(default_factory=lambda: {"total": 2, "mode": "half_up"})
    sections: list[str] = Field(default_factory=list)
    change_note: str = ""
    created_at: str = ""
    scoring: dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = None
    official: bool = False
    parent_version: Optional[int] = None

    @model_validator(mode="after")
    def _check(self) -> "ChecklistVersion":
        ids = [c.id for c in self.criteria]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"Идентификаторы критериев должны быть уникальными: {', '.join(dup)}")
        sections = [s for s in self.sections if s]
        for c in self.criteria:
            if c.section and c.section not in sections:
                sections.append(c.section)
        self.sections = sections
        r = dict(self.rounding or {})
        digits = int(r.get("total", 2))
        if not 0 <= digits <= 6:
            raise ValueError("rounding.total должно быть от 0 до 6")
        mode = r.get("mode", "half_up")
        if mode not in ("half_up", "half_even"):
            raise ValueError("rounding.mode: half_up или half_even")
        r.update(total=digits, mode=mode)
        self.rounding = r
        return self

    @property
    def by_id(self) -> dict[str, Criterion]:
        return {c.id: c for c in self.criteria}

    def criterion(self, criterion_id: str) -> Criterion:
        for c in self.criteria:
            if c.id == criterion_id:
                return c
        raise KeyError(criterion_id)

    @property
    def weight_sum(self) -> float:
        return sum(c.weight for c in self.criteria)


# ------------------------------------------------------------------------------------------- evaluations

class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    message_id: str                     # any role: operator, client, bot or system message
    quote: str = ""
    start: Optional[int] = None         # UTF-16 offsets (JS string positions) in the message text
    end: Optional[int] = None
    valid: bool = False                 # quote verified against the transcript
    speaker: Optional[Speaker] = None   # optional, informational


class CriterionEval(BaseModel):
    model_config = ConfigDict(extra="allow")
    conv_id: str
    criterion_id: str
    criterion_hash: str = ""
    prompt_version: str = ""
    mode: EvalMode = "fast"
    title: Optional[str] = None
    answer: AnswerValue = None             # binary → yes/no; scale → step value; N/A → "na"; None = failed
    evidence: list[Evidence] = Field(default_factory=list)
    checked_range: Optional[tuple[str, str]] = None  # message ids: first and last of the checked span
    na_reason: Optional[str] = None
    explanation: str = ""
    suggestion: Optional[str] = None
    confidence: float = 0.0
    critical_hit: bool = False                  # informational; the engine recomputes it via critical_when
    provider: Optional[str] = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_kzt: float = 0.0
    latency_ms: int = 0
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    raw: Any = None                             # raw model output(s) for tracing
    created_at: Optional[str] = None


    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> Any:
        if v is None:
            return 0.0
        f = float(v)
        if not math.isfinite(f):
            return 0.0
        return min(1.0, max(0.0, f))

    @field_validator("checked_range", mode="before")
    @classmethod
    def _range(cls, v: Any) -> Any:
        if v is None or v == [] or v == ():
            return None
        if isinstance(v, (list, tuple)):
            if len(v) == 1:
                return (v[0], v[0])
            if len(v) >= 2:
                return (v[0], v[-1])
        return v


# ----------------------------------------------------------------------------------------------- scoring

class ScoreLine(BaseModel):
    criterion_id: str
    title: str = ""
    section: Optional[str] = None
    answer_type: AnswerType = "binary"
    answer: AnswerValue = None           # final answer (decision overrides the model)
    original_answer: AnswerValue = None  # model answer before reviewer decisions
    source: Literal["model", "decision", "missing"] = "model"
    decision_id: Optional[str] = None
    points: Optional[float] = None            # fraction × weight (before redistribution); None for N/A / weight 0
    max_points: float = 0.0                   # == weight
    weight: float = 0.0                       # as in the checklist
    weight_effective: float = 0.0             # percent of the total after N/A redistribution (and normalization)
    contribution: float = 0.0                 # fraction × weight_effective
    is_na: bool = False
    counts_in_total: bool = True              # False for N/A, weight 0 and missing answers
    critical: bool = False
    critical_hit: bool = False
    missing: bool = False


class Score(BaseModel):
    conv_id: str = ""
    checklist_version: int = 0
    total: Optional[float] = None             # rounded per checklist (2 digits, half-up); None = not computed
    total_raw: Optional[float] = None
    status: Literal["complete", "incomplete", "no_applicable"] = "complete"
    sections: dict[str, Optional[float]] = Field(default_factory=dict)  # percent within section
    critical: bool = False
    critical_criteria: list[str] = Field(default_factory=list)
    lines: list[ScoreLine] = Field(default_factory=list)
    formula_steps: list[str] = Field(default_factory=list)
    weight_sum: float = 0.0
    applicable_weight: float = 0.0
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------ appeals, decisions

class AppealCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    conv_id: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    message_ids: list[str] = Field(default_factory=list)


class Appeal(AppealCreate):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    id: str
    status: Literal["open", "decided"] = "open"
    created_at: str = ""
    checklist_version: Optional[int] = None
    criterion_hash: Optional[str] = None
    evaluation_id: Optional[str] = None
    original_answer: AnswerValue = None



class DecisionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    appeal_id: str = Field(min_length=1)
    action: Literal["confirm", "change"]
    new_answer: AnswerValue = None
    reason: str = Field(min_length=1)


    @model_validator(mode="after")
    def _need_answer(self) -> "DecisionCreate":
        if self.action == "change" and self.new_answer is None:
            raise ValueError("Для решения «изменить» укажите новый ответ")
        return self


class Decision(DecisionCreate):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    id: str
    conv_id: Optional[str] = None
    criterion_id: Optional[str] = None       # filled by the API from the appeal; the engine needs it
    criterion_hash: Optional[str] = None     # decision applies only to this wording of the criterion
    old_answer: AnswerValue = None
    created_at: str = ""
    reviewer: Optional[str] = None



class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    old_total: Optional[float] = None
    new_total: Optional[float] = None
    decision: Decision
    score: Score


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int = 0
    entity: str                  # "evaluation" | "appeal" | "decision" | "checklist" | "run" ...
    entity_id: str
    event: str                   # e.g. "appeal.created", "decision.saved"
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str = ""
    conv_id: Optional[str] = None


# -------------------------------------------------------------------------------- API shapes (frontend)

class Cell(BaseModel):
    criterion_id: str
    answer: AnswerValue = None
    confidence: float = 0.0
    critical_hit: bool = False
    needs_review: bool = False


class ConversationSummary(BaseModel):
    """Row of GET /api/conversations."""
    model_config = ConfigDict(extra="allow")
    id: str
    channel: Channel
    language: Language
    group_id: str = "DEV"
    total: Optional[float] = None
    critical: bool = False
    needs_review: bool = False
    status: str = "pending"          # "pending" | "evaluated" | "needs_review" | "incomplete"
    cells: list[Cell] = Field(default_factory=list)


class ConversationDetail(BaseModel):
    """GET /api/conversations/{id}: {id,channel,language,group_id,messages,meta,audio_url,evals,score,checklist,
    appeals,decisions,audit}."""
    model_config = ConfigDict(extra="allow")
    id: str
    channel: Channel
    language: Language
    group_id: str = "DEV"
    messages: list[Message] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    audio_url: Optional[str] = None
    evals: list[CriterionEval] = Field(default_factory=list)
    score: Optional[Score] = None
    checklist: Optional[ChecklistVersion] = None
    appeals: list[Appeal] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)


class CriterionChange(BaseModel):
    criterion_id: str
    fields: list[str] = Field(default_factory=list)
    needs_reeval: bool = False
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None


class ChecklistDiff(BaseModel):
    """GET /api/checklists/{a}/diff/{b}."""
    from_version: int
    to_version: int
    added: list[dict[str, Any]] = Field(default_factory=list)     # full criteria (each needs evaluation)
    removed: list[dict[str, Any]] = Field(default_factory=list)
    changed: list[CriterionChange] = Field(default_factory=list)
    needs_reeval: list[str] = Field(default_factory=list)        # criterion ids to (re)evaluate in version b
    reusable: list[str] = Field(default_factory=list)            # ids whose cached evaluations stay valid
    rescore_only: list[str] = Field(default_factory=list)        # only weight/section changed: recompute total
    checklist_fields: list[str] = Field(default_factory=list)    # changed checklist-level fields (rounding, ...)
    warnings: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _with_short_keys(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict):  # web/js/api.js reads {from, to}; make_ui_mocks.py writes {from_version, to_version}
            data["from"], data["to"] = self.from_version, self.to_version
        return data


class RunConvRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    old_total: Optional[float] = None
    new_total: Optional[float] = None
    status: str = "pending"


class RunStatus(BaseModel):
    """GET /api/runs/{run_id}."""
    model_config = ConfigDict(extra="allow")
    run_id: str
    version: Optional[int] = None
    status: str = "running"          # "running" | "completed" | "failed"
    done: int = 0
    total: int = 0
    from_cache: int = 0
    llm_calls: int = 0
    cost_kzt: float = 0.0
    elapsed: float = 0.0
    per_conv: list[RunConvRow] = Field(default_factory=list)


class AudioInfo(BaseModel):
    """GET /api/audio/{conv_id}."""
    model_config = ConfigDict(extra="allow")
    src: Optional[str] = None
    expires_at: Optional[str] = None
    gaps: list[list[float]] = Field(default_factory=list)
    offsets: dict[str, float] = Field(default_factory=dict)


class Health(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: bool = True
    provider: str = ""
    model: str = ""
    mode: str = ""
    offline: bool = False


class LLMCall(BaseModel):
    """One provider call (row of llm_calls)."""
    model_config = ConfigDict(extra="allow")
    ts: str = ""
    provider: str = ""
    model: str = ""
    purpose: str = ""                # "evaluate" | "dossier" | "lint" | ...
    conv_id: Optional[str] = None
    criterion_ids: list[str] = Field(default_factory=list)
    prompt_version: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_kzt: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    ok: bool = True
    error: Optional[str] = None
