"""Deterministic total score — the organizers' PDF, page 2 («Порядок расчёта»). No LLM, no DB.

    score(checklist_version, evals, decisions=None) -> Score

1. Applicability: N/A only where the criterion allows it (otherwise ScoreValidationError).
2. «Да» = full weight, «Нет» = 0; a scale answer must be one of the steps.
3. Scale points = value / top step × weight (the top step gives the full weight).
4. N/A criteria are excluded; their weight is redistributed proportionally (max stays 100).
   Weights not summing to 100 are normalized the same way, with a warning.
5. Total rounded per checklist (v1: 2 digits, half-up); no applicable weighted criteria → no total.
   Gross errors: a criterion with critical=True raises the flag when answer == critical_when (C8: "yes");
   weight-0 criteria never enter the total.
Reviewer decisions override the model answer (last decision per criterion wins) and the total is recomputed.
Exact arithmetic (fractions) is used, so ties like 13.125 round half-up correctly.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from typing import Any, Iterable, Optional, Union

from .models import (Answer, Cell, ChecklistVersion, Conversation, ConversationSummary, Criterion, CriterionEval,
                     Decision, Score, ScoreLine, normalize_answer)

__all__ = ["score", "ScoreValidationError", "is_critical_hit", "round_total", "answer_fraction", "points_for",
           "build_cells", "summarize"]


class ScoreValidationError(ValueError):
    """An answer violates the checklist (N/A not allowed, value not a scale step, wrong type)."""


# ------------------------------------------------------------------------------------------------ helpers

def _frac(x: Union[int, float, Fraction, str]) -> Fraction:
    if isinstance(x, Fraction):
        return x
    if isinstance(x, float):
        return Fraction(repr(x))  # decimal reading of the float: 2.675 → 2675/1000, not 2.67499…
    return Fraction(x)


def _round_fraction(value: Fraction, digits: int = 2, mode: str = "half_up") -> Fraction:
    q = value * (10 ** digits)
    if mode == "half_even":
        n = round(q)
    else:  # half_up: ties away from zero
        sign = -1 if q < 0 else 1
        n = sign * int(abs(q) + Fraction(1, 2))
    return Fraction(n, 10 ** digits)


def round_total(value: Optional[Union[int, float, Fraction, str]], digits: int = 2,
                mode: str = "half_up") -> Optional[float]:
    """Round like the checklist says (default: 2 digits, half-up). Floats are read by their decimal repr."""
    if value is None:
        return None
    return float(_round_fraction(_frac(value), digits, mode))


def is_critical_hit(criterion: Criterion, answer: Any) -> bool:
    """True when this answer raises the gross-error flag (critical criterion and answer == critical_when)."""
    try:
        a = normalize_answer(answer)
    except ValueError:
        return False
    return bool(criterion.critical and criterion.critical_when is not None and a == criterion.critical_when)


def answer_fraction(criterion: Criterion, answer: Answer) -> Optional[Fraction]:
    """Share of the criterion weight earned: yes → 1, no → 0, scale value → value / top step; N/A → None."""
    if answer == "na" or answer is None:
        return None
    if answer == "yes":
        return Fraction(1)
    if answer == "no":
        return Fraction(0)
    top = max(criterion.scale or [0.0])
    if top <= 0:
        raise ScoreValidationError(f"{criterion.id}: у шкалы нет положительной верхней ступени")
    return _frac(float(answer)) / _frac(top)


def points_for(criterion: Criterion, answer: Answer) -> Optional[float]:
    """Points before N/A redistribution (fraction × weight); None for N/A and for weight-0 criteria."""
    f = answer_fraction(criterion, answer)
    if f is None or criterion.weight <= 0:
        return None
    return float(f * _frac(criterion.weight))


def _fmt(value: Union[Fraction, float, int, None], digits: int = 2) -> str:
    """Russian number: decimal comma, trailing zeros trimmed (10.5 → «10,5», 15.0 → «15»)."""
    if value is None:
        return "—"
    v = _round_fraction(_frac(value), digits)
    s = f"{float(v):.{digits}f}"
    if digits > 0:
        s = s.rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    return s.replace(".", ",")


def _fmt_exact(value: Fraction, digits: int = 4) -> str:
    s = _fmt(value, digits)
    return s if _round_fraction(value, digits) == value else s + "…"


def _join_ru(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def _plural_criteria(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "критерием"
    return "критериями"


def _answer_ru(criterion: Criterion, answer: Answer) -> str:
    if answer == "yes":
        return "«Да»"
    if answer == "no":
        return "«Нет»"
    if answer == "na":
        return "N/A"
    return f"{_fmt(float(answer))} из {_fmt(max(criterion.scale or [0.0]))} по шкале"


# ------------------------------------------------------------------------------------------------- engine

def score(checklist_version: Union[ChecklistVersion, dict], evals: Iterable[Union[CriterionEval, dict]],
          decisions: Optional[Iterable[Union[Decision, dict]]] = None, *, conv_id: Optional[str] = None,
          strict: bool = True) -> Score:
    """Compute the total for one conversation.

    evals: model evaluations for this checklist version (criterion_hash must match the criterion, else the
    evaluation is stale and the criterion counts as missing). decisions: reviewer decisions with criterion_id;
    the last one per criterion (by created_at, then list order) overrides the model answer.
    strict=True raises ScoreValidationError on invalid answers; strict=False treats them as missing + warning.
    """
    cl = checklist_version if isinstance(checklist_version, ChecklistVersion) else ChecklistVersion.model_validate(checklist_version)
    ev_list = [e if isinstance(e, CriterionEval) else CriterionEval.model_validate(e) for e in evals]
    dec_list = [d if isinstance(d, Decision) else Decision.model_validate(d) for d in (decisions or [])]
    digits = int(cl.rounding.get("total", 2))
    mode = cl.rounding.get("mode", "half_up")
    by_id = cl.by_id
    warnings: list[str] = []
    cid = conv_id or (ev_list[0].conv_id if ev_list else "") or (dec_list[0].conv_id if dec_list else "") or ""

    # -- model answers (last evaluation per criterion wins; stale hash → missing)
    model: dict[str, CriterionEval] = {}
    for ev in ev_list:
        if cid and ev.conv_id and ev.conv_id != cid:
            warnings.append(f"Оценка {ev.criterion_id} относится к другому диалогу ({ev.conv_id}) — пропущена.")
            continue
        c = by_id.get(ev.criterion_id)
        if c is None:
            continue  # criterion removed from this version
        if ev.criterion_hash and ev.criterion_hash != c.hash:
            warnings.append(f"{c.id}: оценка выполнена для другой формулировки критерия — нужна переоценка.")
            continue
        if ev.criterion_id in model:
            warnings.append(f"{c.id}: несколько оценок, использована последняя.")
        model[ev.criterion_id] = ev

    # -- reviewer decisions (last per criterion wins)
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for d in dec_list:
        if not d.criterion_id or d.criterion_id not in by_id:
            warnings.append(f"Решение {d.id}: критерий {d.criterion_id or '?'} не найден в версии {cl.version} — не применено.")
            continue
        if cid and d.conv_id and d.conv_id != cid:
            warnings.append(f"Решение {d.id} относится к другому диалогу ({d.conv_id}) — не применено.")
            continue
        if d.criterion_hash and d.criterion_hash != by_id[d.criterion_id].hash:
            warnings.append(f"{d.criterion_id}: решение {d.id} принято по прежней формулировке критерия — не применено.")
            continue
        grouped[d.criterion_id].append(d)
    final_decision = {k: sorted(v, key=lambda d: d.created_at or "")[-1] for k, v in grouped.items()}

    # -- per-criterion final answers
    rows = []
    for c in cl.criteria:
        ev = model.get(c.id)
        original = ev.answer if ev else None
        d = final_decision.get(c.id)
        source = "model" if ev else "missing"
        answer: Optional[Answer] = original
        if d is not None:
            source = "decision"
            if d.action == "change":
                answer = d.new_answer
            else:
                answer = d.new_answer if d.new_answer is not None else (d.old_answer if d.old_answer is not None else original)
        if answer is not None:
            try:
                answer = c.check_answer(answer)
            except ValueError as exc:
                who = "решение проверяющего" if d is not None else "ответ модели"
                if strict:
                    raise ScoreValidationError(f"{exc} ({who})") from None
                warnings.append(f"{exc} ({who}) — критерий считается неоценённым.")
                answer = None
        rows.append((c, answer, original, source, d))

    missing = [c.id for c, a, *_ in rows if a is None]
    missing_weighted = [c.id for c, a, *_ in rows if a is None and c.weight > 0]
    for c, a, *_ in rows:
        if a is None and c.critical:
            warnings.append(f"{c.id}: нет ответа — грубые ошибки не проверены.")
    for m in missing:
        if not any(m in w for w in warnings):
            warnings.append(f"{m}: нет оценки.")

    weight_sum = sum((_frac(c.weight) for c in cl.criteria), Fraction(0))
    if weight_sum > 0 and weight_sum != 100:
        warnings.append(f"Сумма весов чек-листа {_fmt(weight_sum)} ≠ 100 — веса нормированы к 100.")

    counted = [(c, a) for c, a, *_ in rows if a is not None and a != "na" and c.weight > 0]
    applicable = sum((_frac(c.weight) for c, _ in counted), Fraction(0))
    # denominator for effective weights: answered applicable + still-missing weighted criteria
    denom = applicable + sum((_frac(c.weight) for c, a, *_ in rows if a is None and c.weight > 0), Fraction(0))

    total_raw: Optional[Fraction] = None
    if not missing_weighted and applicable > 0:
        total_raw = sum((answer_fraction(c, a) * _frac(c.weight) for c, a in counted), Fraction(0)) / applicable * 100
    status = "incomplete" if missing else ("no_applicable" if applicable == 0 else "complete")

    lines: list[ScoreLine] = []
    critical_ids: list[str] = []
    for c, a, original, source, d in rows:
        is_na = a == "na"
        counts = a is not None and not is_na and c.weight > 0
        frac = answer_fraction(c, a) if a is not None else None
        eff = _frac(c.weight) / denom * 100 if (denom > 0 and c.weight > 0 and not is_na) else Fraction(0)
        hit = is_critical_hit(c, a)
        if hit:
            critical_ids.append(c.id)
        lines.append(ScoreLine(
            criterion_id=c.id, title=c.title, section=c.section, answer_type=c.answer_type,
            answer=a, original_answer=original, source="missing" if (a is None and d is None) else source,
            decision_id=d.id if d is not None else None,
            points=float(frac * _frac(c.weight)) if (frac is not None and c.weight > 0) else None,
            max_points=c.weight, weight=c.weight, weight_effective=round(float(eff), 4),
            contribution=round(float(frac * eff), 4) if (counts and frac is not None) else 0.0,
            is_na=is_na, counts_in_total=counts, critical=c.critical, critical_hit=hit, missing=a is None,
        ))

    sections: dict[str, Optional[float]] = {}
    for name in cl.sections:
        sec = [(c, a) for c, a, *_ in rows if c.section == name and c.weight > 0]
        part = [(c, a) for c, a in sec if a is not None and a != "na"]
        if any(a is None for _, a in sec) or not part:
            sections[name] = None
            continue
        w = sum((_frac(c.weight) for c, _ in part), Fraction(0))
        pts = sum((answer_fraction(c, a) * _frac(c.weight) for c, a in part), Fraction(0))
        sections[name] = float(_round_fraction(pts / w * 100, digits, mode))

    total = float(_round_fraction(total_raw, digits, mode)) if total_raw is not None else None
    steps = _formula_steps(cl, rows, counted, applicable, weight_sum, total_raw, total, digits, mode, missing_weighted)

    return Score(
        conv_id=cid, checklist_version=cl.version, total=total,
        total_raw=float(total_raw) if total_raw is not None else None, status=status, sections=sections,
        critical=bool(critical_ids), critical_criteria=critical_ids, lines=lines, formula_steps=steps,
        weight_sum=float(weight_sum), applicable_weight=float(applicable), missing=missing, warnings=warnings,
    )


def _formula_steps(cl, rows, counted, applicable, weight_sum, total_raw, total, digits, mode, missing_weighted) -> list[str]:
    steps: list[str] = []
    na_ids = [c.id for c, a, *_ in rows if a == "na" and c.weight > 0]
    na_weight = sum((_frac(c.weight) for c, a, *_ in rows if a == "na" and c.weight > 0), Fraction(0))
    counted_ids = [c.id for c, _ in counted]
    if weight_sum != 100 and weight_sum > 0:
        steps.append(f"Сумма весов чек-листа {_fmt(weight_sum)} ≠ 100 — веса нормированы: эффективный вес = вес × 100 / "
                     f"сумма весов применимых критериев.")
    if na_ids:
        steps.append(f"{_join_ru(na_ids)} N/A → вес {_fmt(na_weight)} перераспределён между {len(counted_ids)} "
                     f"{_plural_criteria(len(counted_ids))} пропорционально весам: эффективный вес = вес × 100 / "
                     f"{_fmt(applicable)}.")
    elif counted_ids:
        steps.append(f"Все критерии с весом применимы ({_join_ru(counted_ids)}, сумма весов {_fmt(applicable)}) — "
                     f"перераспределение не требуется.")
    for c, a, original, source, d in rows:
        if c.weight <= 0:
            continue
        prefix = f"{c.id} {c.title}: "
        note = ""
        if d is not None:
            was = _answer_ru(c, original) if original is not None else "нет"
            note = f" (решение проверяющего; исходная оценка ИИ: {was})"
        if a is None:
            steps.append(prefix + "нет оценки — итог не рассчитывается до оценки критерия.")
        elif a == "na":
            steps.append(prefix + f"N/A{note} — исключён из расчёта (вес {_fmt(c.weight)}).")
        else:
            f = answer_fraction(c, a)
            eff = _frac(c.weight) / applicable * 100 if applicable > 0 else Fraction(0)
            steps.append(prefix + f"{_answer_ru(c, a)}{note} → {_fmt(f * _frac(c.weight))} из {_fmt(c.weight)} баллов; "
                                  f"вес {_fmt(c.weight)} → {_fmt(eff)}%, вклад {_fmt(f * eff)}.")
    if missing_weighted:
        steps.append(f"Нет оценки по {_join_ru(missing_weighted)} — итог не рассчитывается.")
    elif applicable == 0:
        steps.append("Применимых критериев с весом нет — итог не рассчитывается (стр. 2, п. 5).")
    else:
        pts = sum((answer_fraction(c, a) * _frac(c.weight) for c, a in counted), Fraction(0))
        how = "половина вверх" if mode == "half_up" else "к чётному"
        steps.append(f"Итог = {_fmt(pts)} / {_fmt(applicable)} × 100 = {_fmt_exact(total_raw)} → {_fmt(total, digits)} "
                     f"(округление до {digits} знаков, {how}).")
    for c, a, original, source, d in rows:
        if not c.critical:
            continue
        label = f"{c.id} {c.title}"
        if a is None:
            steps.append(f"{label}: нет ответа — грубые ошибки не проверены.")
        elif is_critical_hit(c, a):
            extra = " (критерий без баллов, итог не меняется)" if c.weight <= 0 else ""
            by = " по решению проверяющего" if d is not None else ""
            steps.append(f"{label}: {_answer_ru(c, a)} → отмечена грубая ошибка{by}{extra}.")
        else:
            by = " (решение проверяющего)" if d is not None else ""
            steps.append(f"{label}: {_answer_ru(c, a)}{by} — грубая ошибка не выявлена.")
    return steps


# ------------------------------------------------------------------------------------- list-row helpers

def build_cells(result: Score, evals: Iterable[Union[CriterionEval, dict]]) -> list[Cell]:
    """Cells of a list row in checklist order: final answer (after decisions) and the gross-error flag come from
    the engine (critical_when), confidence / needs_review from the model evaluation."""
    by_id: dict[str, CriterionEval] = {}
    for e in evals:
        e = e if isinstance(e, CriterionEval) else CriterionEval.model_validate(e)
        by_id[e.criterion_id] = e
    cells = []
    for line in result.lines:
        ev = by_id.get(line.criterion_id)
        cells.append(Cell(criterion_id=line.criterion_id, answer=line.answer,
                          confidence=ev.confidence if ev else 0.0, critical_hit=line.critical_hit,
                          needs_review=bool(ev and ev.needs_review and line.source != "decision")))
    return cells


def summarize(conv: Union[Conversation, dict], result: Optional[Score],
              evals: Iterable[Union[CriterionEval, dict]]) -> ConversationSummary:
    """Row of GET /api/conversations. status: pending (no evaluations) | incomplete | needs_review | evaluated."""
    conv = conv if isinstance(conv, Conversation) else Conversation.model_validate(conv)
    evals = list(evals)
    cells = build_cells(result, evals) if result is not None else []
    needs_review = any(c.needs_review for c in cells)
    if result is None or not evals:
        status = "pending"
    elif result.status == "incomplete":
        status = "incomplete"
    else:
        status = "needs_review" if needs_review else "evaluated"
    return ConversationSummary(id=conv.id, channel=conv.channel, language=conv.language, group_id=conv.group_id,
                               total=result.total if result else None, critical=bool(result and result.critical),
                               needs_review=needs_review, status=status, cells=cells)
