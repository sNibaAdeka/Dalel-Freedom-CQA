"""LLM evaluator: conversation x checklist -> one CriterionEval per criterion.

    evals = evaluate(conversation, checklist_version, mode="accurate", only_criteria=None)

Modes (presets in config/models.json, profile = CQA_PROFILE or argument):
  fast      all criteria in one call; criteria whose answer fails validation are re-asked one by one
  accurate  one call per criterion x N votes (different temperatures, run number in the cache key);
            if the votes disagree a stronger arbiter model re-checks; confidence = share of agreeing votes
            (agreement, NOT model self-confidence). Low self-confidence independently requires review.

The prompt is built from the criterion's fields (cqa.prompts), so a criterion replaced in the UI works without code
changes. The validator checks every model answer before it is accepted:
  - answer is one of the allowed values (N/A only where na_allowed, scale only on its steps)
  - every quote is an exact slice of the cited message (any role, incl. system); fuzzy matching is used only to FIND
    the slice, the stored quote is always the original text; start/end are UTF-16 offsets like in the browser
  - checked_range points to operator messages; a missed action without evidence gets the operator span
  - critical criteria: critical_hit = (answer == criterion.critical_when)
Invalid -> re-ask with the validator's errors in the prompt -> needs_review.
Deterministic rules (cqa.rules) are prompt hints and disagreement flags only; the final answer is the model's.
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from rapidfuzz import fuzz

from . import llm, rules
from . import prompts as P

REVIEW_CONFIDENCE = float(os.environ.get("CQA_REVIEW_CONF", "0.67"))
MODEL_REVIEW_CONFIDENCE = 0.7
FUZZY_MIN = 90
MAX_EVIDENCE = 3


class EvaluationError(RuntimeError):
    pass


# ================================================================ adapters
def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _dump(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(vars(obj))


def _mmss(ts: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+):(\d{2})(?::(\d{2}))?\s*", str(ts or ""))
    if not m:
        return None
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


def _iso(ts: str) -> float | None:
    from datetime import datetime

    s = str(ts or "").strip()
    if not s or ":" not in s or "T" not in s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def as_conversation(obj: Any) -> dict:
    """Organizer record ({conversation_id, channel, transcript}) or cqa.models.Conversation -> normalized dict."""
    d = _dump(obj)
    cid = d.get("id") or d.get("conversation_id")
    channel = str(d.get("channel") or "chat").lower()
    channel = "call" if ("call" in channel or "voice" in channel) else "chat"
    raw_msgs = d.get("messages")
    msgs = []
    if raw_msgs:
        for i, m in enumerate(raw_msgs):
            m = _dump(m)
            idx = m.get("idx", i)
            msgs.append({"id": m.get("id") or f"{cid}:{idx}", "idx": idx, "speaker": str(m.get("speaker", "")).lower(),
                         "text": str(m.get("text") or ""), "ts_raw": str(m.get("ts_raw") or m.get("timestamp") or ""), "t_sec": m.get("t_sec"),
                         **({"speaker_raw": m["speaker_raw"]} if m.get("speaker_raw") else {})})
    else:
        for i, t in enumerate(d.get("transcript") or []):
            msgs.append({"id": f"{cid}:{i}", "idx": i, "speaker": str(t.get("speaker", "")).lower(), "text": str(t.get("text") or ""),
                         "ts_raw": str(t.get("timestamp") or ""), "t_sec": None})
    if msgs and any(m["t_sec"] is None for m in msgs):
        if channel == "chat":
            for m in msgs:
                m["t_sec"] = _mmss(m["ts_raw"]) if m["t_sec"] is None else m["t_sec"]
        else:
            stamps = [_iso(m["ts_raw"]) for m in msgs]
            t0 = next((s for s in stamps if s is not None), None)
            for m, s in zip(msgs, stamps):
                if m["t_sec"] is None and s is not None and t0 is not None:
                    m["t_sec"] = round(s - t0, 3)
    lang = d.get("language") or d.get("lang")
    return {"id": cid, "channel": channel, "language": str(lang).upper() if lang else None, "messages": msgs}


def as_criterion(obj: Any) -> dict:
    """cqa.models.Criterion or dict -> dict. The hash is recomputed with cqa.models.criterion_hash when available, so
    CriterionEval.criterion_hash always equals the engine's hash of this criterion (else the engine ignores the eval)."""
    d = dict(_dump(obj))
    g = d.get("guidance") or []
    d["guidance"] = [g] if isinstance(g, str) else list(g)
    d.setdefault("answer_type", "binary")
    d.setdefault("na_allowed", False)
    if d.get("critical") and not d.get("critical_when"):
        d["critical_when"] = "no" if str(d.get("critical_rule") or "").strip().lower() == "no" else "yes"
    try:
        from .models import criterion_hash

        d["hash"] = criterion_hash(d)
    except Exception:
        pass
    return d


def as_checklist(obj: Any) -> dict:
    d = dict(_dump(obj))
    d["criteria"] = [as_criterion(c) for c in d.get("criteria") or []]
    return d


# ================================================================ answers
_YES = {"yes", "да", "true", "y", "иә", "ия"}
_NO = {"no", "нет", "false", "n", "жоқ"}
_NA = {"na", "n/a", "н/п", "н/д", "not applicable", "неприменимо", "null"}


def normalize_answer(raw: Any, criterion: dict) -> tuple[Any, str | None]:
    """Map a model answer to the allowed value set. Returns (value, error)."""
    allowed = P.allowed_answers(criterion)
    if raw is None:
        return None, "нет поля answer"
    if isinstance(raw, bool):
        raw = "yes" if raw else "no"
    if isinstance(raw, str):
        s = raw.strip().lower().strip(".«»\"'")
        if s in _NA:
            val: Any = "na"
        elif s in _YES:
            val = "yes"
        elif s in _NO:
            val = "no"
        else:
            try:
                val = float(s.replace(",", "."))
            except ValueError:
                return None, f"answer «{raw}» не распознан; допустимо: {allowed}"
    else:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None, f"answer «{raw}» не распознан; допустимо: {allowed}"
    if val == "na" and not criterion.get("na_allowed"):
        return None, f"N/A для критерия {criterion['id']} не разрешён; допустимо: {allowed}"
    if criterion.get("answer_type") == "scale":
        if val in ("yes", "no"):
            return None, f"для шкалы нужен балл из ступеней {allowed}, а не «{val}»"
        if val != "na":
            steps = [float(x) for x in criterion.get("scale") or []]
            if not any(abs(val - s) < 1e-9 for s in steps):
                return None, f"балл {raw} не входит в ступени шкалы {allowed}"
            val = P._num(val)
    else:
        if isinstance(val, float):
            return None, f"для критерия да/нет нужен ответ из {allowed}, а не число {raw}"
    return val, None


def answer_key(v: Any) -> Any:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def is_top(answer: Any, criterion: dict) -> bool:
    """Best possible answer (action done): yes / max step; for critical criteria: no violation."""
    if criterion.get("critical"):
        return answer != criterion.get("critical_when", "yes")
    if criterion.get("answer_type") == "scale":
        return isinstance(answer, (int, float)) and float(answer) >= max(float(x) for x in criterion.get("scale") or [0])
    return answer == "yes"


def critical_hit(answer: Any, criterion: dict) -> bool:
    """Gross-error flag: critical criterion and answer == critical_when (engine.is_critical_hit when available)."""
    if not criterion.get("critical") or answer is None:
        return False
    try:
        from .engine import is_critical_hit
        from .models import Criterion

        return bool(is_critical_hit(Criterion(**criterion), answer))
    except Exception:
        return answer == criterion.get("critical_when", "yes")


# ================================================================ evidence
_CANON = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "’": "'", "‘": "'", "ё": "е", "Ё": "Е",
                        "—": "-", "–": "-", " ": " ", " ": " "})


def utf16_index(text: str, i: int) -> int:
    return len(text[:i].encode("utf-16-le")) // 2


def _collapse(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, canonicalized text + map from collapsed index to original index."""
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out).translate(_CANON).lower(), idx


def _clean_quote(q: str) -> str:
    q = " ".join(str(q or "").split())
    q = q.strip().strip("\"'«»“”„")
    q = re.sub(r"^(\.\.\.|…)\s*|\s*(\.\.\.|…)$", "", q).strip()
    return q


def _snap_words(text: str, s: int, e: int) -> tuple[int, int]:
    while s > 0 and text[s - 1].isalnum() and text[s].isalnum():
        s -= 1
    while e < len(text) and e > 0 and text[e - 1].isalnum() and text[e].isalnum():
        e += 1
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def locate_quote(text: str, quote: str) -> tuple[int, int, float] | None:
    """Find the quote in text. Returns (start, end, score) as code-point indices of the ORIGINAL text, or None."""
    q = _clean_quote(quote)
    if not q or not text:
        return None
    ctext, cmap = _collapse(text)
    cq = _collapse(q)[0].strip()
    if len(cq) < 2 and cq != ctext.strip():
        return None
    # 1) exact (canonical, whitespace-insensitive)
    pos = ctext.find(cq)
    if pos >= 0:
        return cmap[pos], cmap[pos + len(cq) - 1] + 1, 100.0
    # 2) elided quote "a … b": take the contiguous span from a to b
    parts = [p.strip() for p in re.split(r"\.\.\.|…", cq) if p.strip()]
    if len(parts) > 1:
        first = ctext.find(parts[0])
        last = ctext.find(parts[-1], first + len(parts[0])) if first >= 0 else -1
        if first >= 0 and last >= 0:
            return cmap[first], cmap[last + len(parts[-1]) - 1] + 1, 95.0
    # 3) fuzzy alignment (only to locate; the stored quote will be the original slice)
    if len(cq) <= len(ctext):
        al = fuzz.partial_ratio_alignment(cq, ctext)
        if al and al.score >= FUZZY_MIN and al.dest_end > al.dest_start:
            s, e = cmap[al.dest_start], cmap[al.dest_end - 1] + 1
            s, e = _snap_words(text, s, e)
            if e > s:
                return s, e, float(al.score)
    else:
        al = fuzz.partial_ratio_alignment(ctext, cq)
        if al and al.score >= FUZZY_MIN:
            s, e = _snap_words(text, 0, len(text))
            return s, e, float(al.score)
    return None


def validate_evidence(items: Any, conv: dict) -> tuple[list[dict], list[str], list[str]]:
    """Returns (valid evidence, errors for the model, notes about automatic fixes)."""
    by_id = {m["id"]: m for m in conv["messages"]}
    valid, errors, notes = [], [], []
    if not isinstance(items, list):
        items = [items] if items else []
    for ev in items:
        if not isinstance(ev, dict):
            continue
        mid = str(ev.get("message_id") or ev.get("id") or "").strip().strip("[]")
        if mid and mid not in by_id and re.fullmatch(r"\d+", mid):
            mid = f"{conv['id']}:{mid}"
        quote = str(ev.get("quote") or "")
        msg = by_id.get(mid)
        hit = locate_quote(msg["text"], quote) if msg else None
        if hit is None and quote.strip():
            # the quote may sit in another message (wrong id): search all, prefer the nearest
            best = None
            for m in conv["messages"]:
                h = locate_quote(m["text"], quote)
                if h and (best is None or h[2] > best[1][2] or (h[2] == best[1][2] and msg and abs(m["idx"] - msg["idx"]) < abs(best[0]["idx"] - msg["idx"]))):
                    best = (m, h)
            if best and best[1][2] >= 95:
                notes.append(f"id цитаты исправлен: {mid or '—'} → {best[0]['id']}")
                msg, hit = best
        if msg is None:
            errors.append(f"реплики с id «{mid}» нет в диалоге" if mid else "у доказательства нет message_id")
            continue
        if hit is None:
            errors.append(f"цитата «{quote[:80]}» не найдена дословно в реплике [{msg['id']}]")
            continue
        s, e, score = hit
        text = msg["text"]
        if score < 100:
            notes.append(f"цитата выровнена по тексту реплики [{msg['id']}] (сходство {score:.0f})")
        item = {"message_id": msg["id"], "quote": text[s:e], "start": utf16_index(text, s), "end": utf16_index(text, e),
                "valid": True, "speaker": msg["speaker"]}
        if not any(v["message_id"] == item["message_id"] and v["start"] == item["start"] and v["end"] == item["end"] for v in valid):
            valid.append(item)
    return valid[:MAX_EVIDENCE], errors, notes


def operator_span(conv: dict) -> list[str] | None:
    ops = [m for m in conv["messages"] if m["speaker"] == "operator"]
    return [ops[0]["id"], ops[-1]["id"]] if ops else None


def validate_range(rng: Any, conv: dict) -> tuple[list[str] | None, list[str]]:
    """checked_range must name operator messages; ids are snapped inward to the nearest operator message."""
    if not rng:
        return None, []
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None, ["checked_range должен быть списком из двух id"]
    by_id = {m["id"]: m for m in conv["messages"]}
    ids = []
    for x in rng:
        x = str(x).strip().strip("[]")
        if x not in by_id and re.fullmatch(r"\d+", x):
            x = f"{conv['id']}:{x}"
        ids.append(x)
    if ids[0] not in by_id or ids[1] not in by_id:
        return None, [f"checked_range: id {[i for i in ids if i not in by_id]} нет в диалоге"]
    a, b = sorted((by_id[ids[0]]["idx"], by_id[ids[1]]["idx"]))
    ops = [m for m in conv["messages"] if m["speaker"] == "operator" and a <= m["idx"] <= b]
    if not ops:
        return None, ["checked_range должен включать реплики оператора"]
    return [ops[0]["id"], ops[-1]["id"]], []


# ================================================================ one model answer -> validated vote
def validate_item(item: dict, conv: dict, criterion: dict, require_evidence: bool = True) -> dict:
    """Validate one criterion object from the model. Returns a vote dict with errors (retryable) and notes.
    require_evidence=False (arbiter): missing/invalid quotes become notes; evidence is then borrowed from an agreeing vote."""
    errors, notes = [], []
    if not isinstance(item, dict):
        return {"answer": None, "errors": ["ответ не является JSON-объектом критерия"], "notes": [], "evidence": []}
    answer, err = normalize_answer(item.get("answer"), criterion)
    if err:
        errors.append(err)
    evidence, ev_err, ev_notes = validate_evidence(item.get("evidence") or [], conv)
    notes += ev_notes
    rng, rng_err = validate_range(item.get("checked_range"), conv)
    na_reason = (item.get("na_reason") or "").strip() or None
    explanation = (item.get("explanation") or "").strip()
    if answer is not None and not require_evidence:
        notes += ev_err + rng_err
        if answer == "na" and not na_reason:
            na_reason = explanation or None
    elif answer is not None:
        if ev_err and not evidence:
            errors += ev_err
        elif ev_err:
            notes += [f"отброшено: {e}" for e in ev_err]
        if rng_err:
            if answer == "na" or evidence:
                notes += rng_err
            else:
                errors += rng_err
        if answer == "na":
            if not na_reason:
                na_reason = explanation or None
                notes.append("na_reason взят из объяснения")
                if not na_reason:
                    errors.append("для N/A нужно указать na_reason — какое условие неприменимости выполнено")
        elif critical_hit(answer, criterion) and not evidence:
            errors.append("грубое нарушение отмечено без цитаты-доказательства: приведи реплику оператора")
        elif is_top(answer, criterion) and not evidence and not criterion.get("critical"):
            errors.append("нужна цитата-доказательство (реплика, подтверждающая ответ)")
        elif not is_top(answer, criterion) and not evidence and not rng:
            rng = operator_span(conv)
            notes.append("проверенный участок не указан моделью — подставлен весь участок оператора")
        if criterion.get("critical") and not critical_hit(answer, criterion) and not rng:
            rng = operator_span(conv)
        if not explanation:
            notes.append("нет объяснения")
    try:
        conf = float(item.get("confidence"))
        conf = min(max(conf, 0.0), 1.0)
    except (TypeError, ValueError):
        conf = None
    return {"answer": answer, "evidence": evidence, "checked_range": rng, "na_reason": na_reason if answer == "na" else None,
            "explanation": explanation, "analysis": (item.get("analysis") or "").strip(), "suggestion": (item.get("suggestion") or None),
            "self_confidence": conf, "errors": errors, "notes": notes}


# ================================================================ model calls
def _usage_sum(usages: list[dict]) -> dict:
    return {"tokens_in": sum(u.get("tokens_in", 0) for u in usages), "tokens_out": sum(u.get("tokens_out", 0) for u in usages),
            "cost_kzt": round(sum(u.get("cost_kzt", 0.0) for u in usages), 4),
            "cost_kzt_full": round(sum(u.get("cost_kzt_full", 0.0) for u in usages), 4),
            "latency_ms": sum(u.get("latency_ms_full", u.get("latency_ms", 0)) for u in usages),
            "calls": len(usages), "cache_hits": sum(1 for u in usages if u.get("cache_hit")),
            "fallback_models": _fallback_models(usages)}


def _fallback_models(usages: list[dict]) -> list[str]:
    """Models that really answered instead of the configured one (cqa.llm fallback chain)."""
    return sorted({f"{u.get('provider')}:{u.get('model')}" for u in usages if u.get("fallback")}
                  | {m for u in usages for m in u.get("fallback_models") or []})


def _pick_item(data: Any, criterion_id: str) -> Any:
    if isinstance(data, dict) and "criteria" in data and isinstance(data["criteria"], list):
        return next((x for x in data["criteria"] if isinstance(x, dict) and str(x.get("criterion_id")) == criterion_id), None)
    return data


def ask_single(conv: dict, crit: dict, facts: list[str], model: str, temperature: float, run: int, *, readings=None,
               no_cache=None, retries: int = 2, errors: list[str] | None = None, arbiter_votes: list[dict] | None = None) -> dict:
    """One vote for one criterion (with validator-driven re-asks). Never raises on model failure: returns a vote with
    answer None and the error."""
    usages, attempts = [], []
    errs = list(errors or [])
    vote: dict = {"answer": None, "errors": ["нет ответа модели"], "notes": [], "evidence": []}
    for attempt in range(retries + 1):
        if arbiter_votes is not None:
            system, user = P.build_arbiter(conv, crit, arbiter_votes, facts, readings)
            if errs:
                user += "\n\nТвой предыдущий ответ отклонён проверкой:\n" + "\n".join(f"- {e}" for e in errs) + "\nИсправь и верни полный JSON заново."
        else:
            system, user = P.build_single(conv, crit, facts, readings, errors=errs or None)
        try:
            data, usage = llm.complete_json(system, user, model, temperature, run=run, no_cache=no_cache, max_tokens=2500)
        except llm.LLMError as e:
            vote = {"answer": None, "errors": [f"ошибка модели: {e}"], "notes": [], "evidence": [], "fatal": True}
            attempts.append({"error": str(e)[:300]})
            break
        usages.append(usage)
        item = _pick_item(data, crit["id"])
        vote = validate_item(item, conv, crit, require_evidence=arbiter_votes is None)
        attempts.append({"raw": item, "errors": vote["errors"], "notes": vote["notes"]})
        if not vote["errors"]:
            break
        errs = vote["errors"]
    vote.update({"model": model, "temperature": temperature, "run": run, "usage": _usage_sum(usages), "attempts": attempts,
                 "role": "arbiter" if arbiter_votes is not None else "vote"})
    return vote


def ask_batch(conv: dict, crits: list[dict], facts: list[str], model: str, temperature: float, *, readings=None, no_cache=None) -> tuple[dict, dict, Any]:
    """Mode 'fast': all criteria in one call. Returns ({criterion_id: vote}, usage, raw)."""
    system, user = P.build_batch(conv, crits, facts, readings)
    data, usage = llm.complete_json(system, user, model, temperature, run=0, no_cache=no_cache, max_tokens=1200 + 700 * len(crits))
    out = {}
    for c in crits:
        item = _pick_item(data, c["id"])
        if item is None and isinstance(data, dict) and isinstance(data.get("criteria"), list) and len(crits) == 1 and data["criteria"]:
            item = data["criteria"][0]
        v = validate_item(item, conv, c) if item is not None else {"answer": None, "errors": [f"в ответе нет критерия {c['id']}"], "notes": [], "evidence": []}
        v.update({"model": model, "temperature": temperature, "run": 0, "attempts": [{"raw": item, "errors": v["errors"]}], "role": "batch"})
        out[c["id"]] = v
    return out, usage, data


# ================================================================ aggregation
def aggregate(votes: list[dict], criterion: dict) -> tuple[Any, float, dict]:
    """Majority of valid votes. Returns (answer, agreement share, distribution)."""
    vals = [answer_key(v["answer"]) for v in votes if v.get("answer") is not None and not v.get("errors")]
    if not vals:
        vals = [answer_key(v["answer"]) for v in votes if v.get("answer") is not None]
    if not vals:
        return None, 0.0, {}
    dist = Counter(vals)
    top, n = dist.most_common(1)[0]
    if n > len(vals) / 2:
        return top, n / len(vals), dict(dist)
    nums = sorted(x for x in vals if isinstance(x, float))
    if criterion.get("answer_type") == "scale" and nums:  # no majority on a scale: median, snapped to a step
        med = nums[len(nums) // 2]
        return med, dist[med] / len(vals), dict(dist)
    best = max((v for v in votes if v.get("answer") is not None), key=lambda v: v.get("self_confidence") or 0)
    return answer_key(best["answer"]), dist[answer_key(best["answer"])] / len(vals), dict(dist)


def _representative(votes: list[dict], answer: Any) -> dict | None:
    same = [v for v in votes if v.get("answer") is not None and answer_key(v["answer"]) == answer_key(answer)]
    if not same:
        return None
    return max(same, key=lambda v: (not v.get("errors"), bool(v.get("evidence")), v.get("self_confidence") or 0))


def _model_label(preset: dict, arbiter_used: bool, votes: int) -> str:
    models = preset.get("models")
    m = (" + ".join(models) if models else preset["model"] + (f" ×{votes}" if votes > 1 else ""))
    if arbiter_used and preset.get("arbiter"):
        m += f" + арбитр {preset['arbiter']['model']}"
    return m


def _trace(v: dict | None) -> dict | None:
    if not v:
        return None
    return {"role": v.get("role"), "model": v.get("model"), "temperature": v.get("temperature"), "run": v.get("run"),
            "answer": v.get("answer"), "self_confidence": v.get("self_confidence"), "analysis": (v.get("analysis") or "")[:1500],
            "errors": v.get("errors"), "attempts": v.get("attempts"), "usage": v.get("usage")}


def _finalize(conv: dict, crit: dict, votes: list[dict], final: dict | None, answer: Any, confidence: float, mode: str,
              preset: dict, dist: dict, reading: str | None, readings, arb_vote: dict | None = None) -> dict:
    """Build the CriterionEval dict from the chosen answer and the vote that carries its evidence/explanation."""
    usages = [v.get("usage") or {} for v in votes] + ([arb_vote.get("usage") or {}] if arb_vote else [])
    tot = {k: sum(u.get(k, 0) for u in usages) for k in ("tokens_in", "tokens_out", "cost_kzt", "cost_kzt_full", "latency_ms", "calls", "cache_hits")}
    rep = final or {}
    if isinstance(answer, float):
        answer = P._num(answer)
    reasons = []
    if answer is None:
        errs = [e for v in votes for e in (v.get("errors") or [])] or ["нет ответа"]
        reasons.append("оценка не получена: " + "; ".join(errs[:3]))
    elif rep.get("errors"):
        reasons.append("ответ модели не прошёл проверку: " + "; ".join(rep["errors"][:3]))
    if mode == "accurate" and len(votes) > 1 and answer is not None and confidence < REVIEW_CONFIDENCE:
        reasons.append(f"голоса разошлись: {', '.join(f'{k}×{n}' for k, n in dist.items())}" + (", решил арбитр" if arb_vote else ""))
    if mode == "fast" and answer is not None and rep.get("self_confidence") is not None and rep["self_confidence"] < MODEL_REVIEW_CONFIDENCE:
        reasons.append(f"низкая уверенность модели {rep['self_confidence']:.2f}")
    if mode == "accurate" and answer is not None:
        # Inspect all valid supporters, not just the most confident representative.
        # An arbiter may borrow evidence from that representative while remaining uncertain.
        supporters = [v for v in votes + ([arb_vote] if arb_vote else [])
                      if not v.get("errors") and v.get("answer") is not None
                      and answer_key(v["answer"]) == answer_key(answer)]
        reported = [v["self_confidence"] for v in supporters if v.get("self_confidence") is not None]
        low = [value for value in reported if value < MODEL_REVIEW_CONFIDENCE]
        if low:
            reasons.append(f"низкая заявленная уверенность модели в {len(low)} из {len(reported)} ответов, "
                           f"поддерживающих итог (минимум {min(low):.2f}, порог {MODEL_REVIEW_CONFIDENCE:.2f}); "
                           "согласие голосов не устраняет неопределённость — нужна ручная проверка")
    if critical_hit(answer, crit):
        reasons.append("отмечено грубое нарушение — подтвердите вручную")
    expected = rules.rule_check(crit, conv) if answer is not None else None
    if expected is not None and answer_key(expected) != answer_key(answer):
        reasons.append(f"правило по ключевым словам ожидает «{expected}»")
    reasons.extend(rules.recording_review_reasons(conv, crit, answer))
    if answer == "na" and not rep.get("na_reason"):
        reasons.append("N/A без причины")
    if answer is not None and answer != "na" and not rep.get("evidence") and not rep.get("checked_range"):
        reasons.append("нет доказательства и проверенного участка")
    return {
        "conv_id": conv["id"], "criterion_id": crit["id"], "criterion_hash": crit.get("hash"), "prompt_version": P.prompt_version(crit, readings),
        "title": crit.get("title"), "answer": answer,
        "evidence": [{k: e[k] for k in ("message_id", "quote", "start", "end", "valid")} for e in rep.get("evidence") or []] if answer is not None else [],
        "checked_range": rep.get("checked_range") if answer is not None else None,
        "na_reason": rep.get("na_reason") if answer == "na" else None,
        "explanation": rep.get("explanation") or "" if answer is not None else "",
        "suggestion": rep.get("suggestion") if answer is not None and not is_top(answer, crit) else None,
        # Keep the existing API contract: accurate confidence is agreement, fast is self-confidence.
        "confidence": round(float(confidence), 3), "critical_hit": critical_hit(answer, crit),
        "model": _model_label(preset, arb_vote is not None, len(votes))
                 + (f" · фолбэк {', '.join(_fallback_models(usages))}" if _fallback_models(usages) else ""), "tokens_in": int(tot["tokens_in"]), "tokens_out": int(tot["tokens_out"]),
        "cost_kzt": round(tot["cost_kzt"], 4), "cost_kzt_full": round(tot["cost_kzt_full"], 4), "latency_ms": int(tot["latency_ms"]),
        "needs_review": bool(reasons), "review_reasons": reasons, "mode": mode, "reading": reading,
        "raw": {"votes": [_trace(v) for v in votes], "arbiter": _trace(arb_vote), "distribution": {str(k): n for k, n in dist.items()},
                "agreement": round(float(confidence), 3) if mode == "accurate" else None,
                "calls": tot["calls"], "cache_hits": tot["cache_hits"], "notes": (rep.get("notes") or [])[:6],
                "fallback": _fallback_models(usages)},
    }


def _merge_arbiter(arb: dict, rep: dict) -> dict:
    """Arbiter decided but gave no usable evidence: keep its explanation, borrow the evidence of an agreeing vote."""
    merged = dict(rep)
    merged.update({k: arb[k] for k in ("explanation", "analysis", "suggestion") if arb.get(k)})
    merged["errors"] = []
    return merged


# ================================================================ public API
def _criteria(checklist: dict, only: Iterable[str] | None) -> list[dict]:
    crits = checklist["criteria"]
    if only:
        want = set(only)
        crits = [c for c in crits if c["id"] in want]
    return crits


def evaluate_criterion(conv: dict, crit: dict, preset: dict, *, facts: list[str], readings=None, no_cache=None) -> dict:
    """Accurate path for one criterion: N votes in parallel; if they are not unanimous the arbiter re-checks."""
    temps = preset["temperatures"]
    models = list(preset.get("models") or [preset["model"]])  # optional mixed-model votes: ["openai:deepseek-chat", "claude_cli:..."]
    _, reading = P.guidance_for(crit, readings)
    jobs = [(models[k % len(models)], temps[k], k) for k in range(len(temps))]
    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=len(jobs)) as vp:
            votes = list(vp.map(lambda j: ask_single(conv, crit, facts, j[0], j[1], j[2], readings=readings, no_cache=no_cache), jobs))
    else:
        votes = [ask_single(conv, crit, facts, m, t, r, readings=readings, no_cache=no_cache) for m, t, r in jobs]
    answer, agreement, dist = aggregate(votes, crit)
    final = _representative(votes, answer)
    arb_vote = None
    arb = preset.get("arbiter") or {}
    valid_votes = [v for v in votes if v.get("answer") is not None]
    unanimous = len(dist) == 1 and len(valid_votes) == len(votes)
    if arb.get("model") and len(votes) > 1 and not unanimous and valid_votes:
        arb_vote = ask_single(conv, crit, facts, arb["model"], float(arb.get("temperature", 0.0)), 0, readings=readings,
                              no_cache=no_cache, retries=1, arbiter_votes=valid_votes)
        if arb_vote.get("answer") is not None and not arb_vote.get("errors"):
            answer = answer_key(arb_vote["answer"])
            agreement = (sum(1 for v in valid_votes if answer_key(v["answer"]) == answer) + 1) / (len(votes) + 1)
            rep = _representative(votes, answer)
            has_proof = bool(arb_vote.get("evidence")) or (answer == "na" and arb_vote.get("na_reason")) or bool(arb_vote.get("checked_range"))
            final = arb_vote if (has_proof or rep is None) else _merge_arbiter(arb_vote, rep)
    return _finalize(conv, crit, votes, final, answer, agreement, "accurate", preset, dist, reading, readings, arb_vote)


def _route(pre: dict, conv: dict) -> dict | None:
    """Routing by channel (profile "routing"): preset {"route_by_channel": {"call": "provider:model"}} sends that channel
    straight to the given model (no cascade first pass); other channels follow the preset as usual. Phone calls are long
    and noisy (ASR), so the cheap model's doubt usually escalates them anyway: routing them directly saves the first pass
    and roughly halves their latency. Returns {"channel", "model", "rule"} or None."""
    rules_ = pre.get("route_by_channel") or {}
    target = rules_.get(conv.get("channel"))
    if isinstance(target, dict):
        target = target.get("model")
    if not target:
        return None
    return {"channel": conv.get("channel"), "model": str(target), "rule": f"route_by_channel.{conv.get('channel')}"}


def _fast_pass(conv: dict, crits: list[dict], facts: list[str], pre: dict, temp: float, pool: ThreadPoolExecutor, *,
               readings=None, no_cache=None) -> dict[str, dict]:
    """Fast path with one model: all criteria in one call; criteria that fail validation are re-asked one by one."""
    model = pre["model"]
    try:
        votes, usage, _raw = ask_batch(conv, crits, facts, model, temp, readings=readings, no_cache=no_cache)
    except llm.LLMError as e:
        votes, usage = {c["id"]: {"answer": None, "errors": [f"ошибка модели: {e}"], "notes": [], "evidence": [], "role": "batch"} for c in crits}, {}
    n = len(crits)
    share = {"tokens_in": usage.get("tokens_in", 0) / n, "tokens_out": usage.get("tokens_out", 0) / n, "cost_kzt": usage.get("cost_kzt", 0.0) / n,
             "cost_kzt_full": usage.get("cost_kzt_full", 0.0) / n, "latency_ms": usage.get("latency_ms_full", usage.get("latency_ms", 0)) / n,
             "calls": 1 / n, "cache_hits": (1 / n) if usage.get("cache_hit") else 0,
             "fallback_models": _fallback_models([usage])}
    fixes = {c["id"]: pool.submit(ask_single, conv, c, facts, model, temp, 0, readings=readings, no_cache=no_cache,
                                  errors=votes[c["id"]]["errors"])
             for c in crits if votes[c["id"]].get("errors")}
    results: dict[str, dict] = {}
    for c in crits:
        v = votes[c["id"]]
        v["usage"] = dict(share)
        vs = [v]
        if c["id"] in fixes:
            fx = fixes[c["id"]].result()
            vs.append(fx)
            if fx.get("answer") is not None and (not fx.get("errors") or v.get("answer") is None):
                v = fx
        _, reading = P.guidance_for(c, readings)
        ans = answer_key(v["answer"]) if v.get("answer") is not None else None
        conf = v.get("self_confidence") if v.get("self_confidence") is not None else 0.5
        res = _finalize(conv, c, vs, v, ans, conf, "fast", pre, {str(ans): 1} if ans is not None else {}, reading, readings)
        fb = res["raw"].get("fallback") or []
        res["model"] = model + (f" · фолбэк {', '.join(fb)}" if fb else "")
        results[c["id"]] = res
    return results


def _cascade(conv: dict, crits: list[dict], facts: list[str], pre: dict, temp: float, pool: ThreadPoolExecutor,
             first: dict[str, dict], esc: dict, *, readings=None, no_cache=None) -> dict[str, dict]:
    """Cascade (profile "cascade"): a cheap model answers first; if it marks any criterion for review, the whole
    dialogue is re-evaluated by a stronger model and its answers are used. Measured on the 21 holdout dialogues
    (2026-11-07): Haiku -> Gemini 4 Argon = same accuracy as Gemini alone (95.6%) at about half the cost.
    The first pass's cost is added to the final evaluation; raw["cascade"] records what happened."""
    flagged = [c["id"] for c in crits if first[c["id"]].get("needs_review")]
    # Policy triggers (profile "routing"): escalate when the first pass gives one of these answers, e.g.
    # {"C3": ["yes", "no"]} = identification was applicable, i.e. the operator touched the client's account data.
    policy = [f"{cid}={first[cid].get('answer')}" for cid, vals in (esc.get("when_answers") or {}).items()
              if cid in first and first[cid].get("answer") in vals]
    info = {"first_model": pre["model"], "escalate_model": esc["model"], "when": esc.get("when", "needs_review"),
            "escalated": bool(flagged or policy), "flagged": flagged, "policy": policy,
            "first_answers": {cid: r.get("answer") for cid, r in first.items()}}
    flagged = flagged or policy
    if not flagged:
        for r in first.values():
            r["raw"]["cascade"] = info
            r["model"] = f"{r['model']} (каскад: без эскалации)"
        return first
    second = _fast_pass(conv, crits, facts, dict(pre, model=esc["model"]), temp, pool, readings=readings, no_cache=no_cache)
    out: dict[str, dict] = {}
    for c in crits:
        a, b = first[c["id"]], second[c["id"]]
        r = b if b.get("answer") is not None else a  # stronger model failed on this criterion: keep the first answer
        for k in ("tokens_in", "tokens_out", "latency_ms"):
            r[k] = int(a.get(k, 0) + b.get(k, 0))
        for k in ("cost_kzt", "cost_kzt_full"):
            r[k] = round(a.get(k, 0.0) + b.get(k, 0.0), 4)
        r["model"] = f"{a['model']} → {b['model']}" + ("" if r is b else " (ответ первой модели: вторая не ответила)")
        r["raw"]["cascade"] = info
        out[c["id"]] = r
    return out


def evaluate(conversation: Any, checklist_version: Any, mode: str = "accurate", only_criteria: Iterable[str] | None = None, *,
             profile: str | None = None, readings: dict | str | None = None, no_cache: bool | None = None,
             preset: dict | None = None, workers: int = 8, as_models: bool = True,
             on_criterion: Callable[[dict], None] | None = None) -> list:
    """Evaluate one conversation. Returns one CriterionEval (pydantic, when cqa.models is available and as_models)
    or dict per criterion, in checklist order."""
    conv = as_conversation(conversation)
    checklist = as_checklist(checklist_version)
    crits = _criteria(checklist, only_criteria)
    if not crits:
        return []
    pre = dict(preset or llm.preset(mode, profile))
    routed = _route(pre, conv)
    if routed:
        pre = dict(pre, model=routed["model"], escalate=None)
    pre["temperatures"] = list(pre.get("temperatures") or [0.0])[: max(1, int(pre.get("votes", 1)))]
    if llm.parse_model(pre["model"])[0] == "mock" and llm._mock_handler is None:
        from .prompts.mock import rule_mock

        llm.set_mock(rule_mock)  # offline default: deterministic illustrative answers, marked by model "mock:*"
    facts = rules.facts(conv)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(2, workers)) as pool:
        if mode == "fast":
            temp = pre["temperatures"][0]
            results = _fast_pass(conv, crits, facts, pre, temp, pool, readings=readings, no_cache=no_cache)
            esc = pre.get("escalate") or {}
            if esc.get("model"):
                results = _cascade(conv, crits, facts, pre, temp, pool, results, esc, readings=readings, no_cache=no_cache)
            for c in crits:
                if on_criterion:
                    on_criterion(results[c["id"]])
        else:
            futs = {c["id"]: pool.submit(evaluate_criterion, conv, c, pre, facts=facts, readings=readings, no_cache=no_cache) for c in crits}
            for c in crits:
                res = futs[c["id"]].result()
                results[c["id"]] = res
                if on_criterion:
                    on_criterion(res)
    ordered = [results[c["id"]] for c in crits]
    if routed:
        for r in ordered:
            r.setdefault("raw", {})["routing"] = routed
            r["model"] = f"{r['model']} (маршрут: {'звонок' if conv['channel'] == 'call' else 'чат'})"
    prov = llm.parse_model(pre["model"])[0]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rate = llm.usd_kzt() or 1.0
    for r in ordered:
        r["mode"] = mode_key(mode, profile) if preset is None else mode
        r["provider"] = prov
        r["cost_usd"] = round(r.get("cost_kzt", 0.0) / rate, 6)
        r["created_at"] = stamp
    return [to_model(r) for r in ordered] if as_models else ordered


def mode_key(mode: str, profile: str | None = None) -> str:
    """Value stored in CriterionEval.mode (part of the DB key): 'accurate' for the default cloud profile,
    'accurate:local' / 'fast:claude' for others, so cloud and local results never overwrite each other."""
    prof = profile or llm.active_profile()
    return mode if prof in (None, "", llm.config().get("default_profile", "cloud")) else f"{mode}:{prof}"


def to_model(d: dict):
    """dict -> cqa.models.CriterionEval when that model exists (extra keys are kept: the model allows extras)."""
    try:
        from .models import CriterionEval  # type: ignore
    except Exception:
        return d
    try:
        return CriterionEval(**d)
    except Exception as e:  # never lose an evaluation because of a schema detail
        d = dict(d)
        d.setdefault("review_reasons", []).append(f"схема CriterionEval: {str(e)[:200]}")
        return d


def dossier(conversation: Any, criterion: Any, evaluation: Any, comment: str = "", message_ids: list[str] | None = None, *,
            profile: str | None = None, model: str | None = None, readings: dict | str | None = None, no_cache: bool | None = None) -> dict:
    """Appeal dossier for the reviewer (cqa.review injects this): ONE LLM call.

    Returns {"for": [{"text", "evidence": [Evidence]}], "against": [...], "recommendation": str, "suggested_answer",
             "confidence", "model", "provider", "tokens_in", "tokens_out", "cost_kzt", "cost_usd", "latency_ms", "raw", "errors"}
    "for" = arguments for revising the original answer (the appeal side), "against" = arguments for keeping it.
    Quotes are validated like evaluation evidence (exact slices, UTF-16); invalid ones are dropped."""
    conv = as_conversation(conversation)
    crit = as_criterion(criterion)
    ev = _dump(evaluation) if evaluation is not None else {}
    acc = llm.preset("accurate", profile)
    m = model or (acc.get("arbiter") or {}).get("model") or acc["model"]  # the strongest configured model
    if llm.parse_model(m)[0] == "mock" and llm._mock_handler is None:
        from .prompts.mock import rule_mock

        llm.set_mock(rule_mock)
    system, user = P.build_dossier(conv, crit, ev, comment, message_ids, rules.facts(conv), readings)
    out = {"for": [], "against": [], "recommendation": "", "suggested_answer": None, "confidence": 0.0, "model": m,
           "provider": llm.parse_model(m)[0], "tokens_in": 0, "tokens_out": 0, "cost_kzt": 0.0, "cost_usd": 0.0, "latency_ms": 0, "raw": None, "errors": []}
    try:
        data, usage = llm.complete_json(system, user, m, 0.0, no_cache=no_cache, max_tokens=2500)
    except llm.LLMError as e:
        out["errors"].append(f"ошибка модели: {e}")
        out["recommendation"] = "Досье не получено — проверьте оценку вручную."
        return out
    out.update(tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0), cost_kzt=usage.get("cost_kzt", 0.0),
               cost_usd=usage.get("cost_usd", 0.0), latency_ms=usage.get("latency_ms", 0), raw=data)
    data = data if isinstance(data, dict) else {}
    for side in ("for", "against"):
        for arg in data.get(side) or []:
            if not isinstance(arg, dict) or not str(arg.get("text") or "").strip():
                continue
            evs, errs, _ = validate_evidence(arg.get("evidence") or [], conv)
            out[side].append({"text": str(arg["text"]).strip(), "evidence": [{k: e[k] for k in ("message_id", "quote", "start", "end", "valid")} for e in evs]})
            out["errors"] += errs
    out["recommendation"] = str(data.get("recommendation") or "").strip()
    sa = data.get("suggested_answer")
    if sa is not None:
        val, err = normalize_answer(sa, crit)
        out["suggested_answer"] = val
        if err:
            out["errors"].append(err)
    try:
        out["confidence"] = min(max(float(data.get("confidence")), 0.0), 1.0)
    except (TypeError, ValueError):
        pass
    return out


def evaluate_many(conversations: Iterable[Any], checklist_version: Any, mode: str = "accurate", *, workers: int = 4, **kw) -> dict[str, list]:
    """Several conversations in parallel (provider concurrency is still capped inside cqa.llm)."""
    convs = list(conversations)
    out: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(evaluate, c, checklist_version, mode, **kw): as_conversation(c)["id"] for c in convs}
        for f, cid in futs.items():
            out[cid] = f.result()
    return out


def timed_evaluate(conversation: Any, checklist_version: Any, mode: str = "accurate", **kw) -> tuple[list, float]:
    t0 = time.perf_counter()
    res = evaluate(conversation, checklist_version, mode, **kw)
    return res, time.perf_counter() - t0
