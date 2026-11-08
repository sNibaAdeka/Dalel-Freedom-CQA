"""Prompt builder: every prompt is assembled from the criterion's own fields, so a criterion replaced in the UI is
evaluated without code changes.

Inputs are normalized dicts (see cqa.evaluator.as_conversation / as_criterion):
  conversation: {id, channel: chat|call, language, messages: [{id, idx, speaker, text, ts_raw, t_sec}]}
  criterion:    {id, title, description, answer_type: binary|scale|numeric, scale, scale_labels, na_allowed, na_rule,
                 guidance: [str], critical, critical_rule, critical_when, section, hash}

Calibrated interpretations ("readings") live as data in guidance_v1.json, keyed by criterion id + hash: they apply only
to the unchanged organizer criterion, never to a replaced one. Variants (e.g. C7 strict / lenient) are selected by
the file's "active" field, env CQA_READINGS="C7=strict,C2=default" or the `readings` argument.
Obsolete reading names are canonicalized through the entry's retired_readings mapping; they cannot revive a
superseded C2/C8 policy. C7=lenient remains available only for explicit historical experiments.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUIDANCE_FILE = HERE / "guidance_v1.json"
TEMPLATE_VERSION = "p3"
COMPACT_TEMPLATE_VERSION = "p3-compact-v1"
COMPACT_QUOTE_MAX_CHARS = 240

ROLE = {"operator": "OPERATOR", "client": "CLIENT", "bot": "BOT", "system": "SYSTEM"}

SYSTEM = """Ты — старший эксперт по контролю качества клиентской поддержки Freedom Broker (брокер, Казахстан).
Ты оцениваешь работу ОПЕРАТОРА в чате или телефонном звонке по критериям чек-листа. Диалоги бывают на русском,
казахском и английском; оценивай одинаково строго на любом языке.

Правила оценки (из чек-листа организаторов):
1. Оценивается только оператор (OPERATOR). Реплики клиента (CLIENT), бота-ассистента или IVR (BOT) и системные события
   (SYSTEM) — контекст: оператор за них не отвечает, но на них можно ссылаться как на доказательство.
2. Применяй формулировку критерия как написано: не добавляй требований, которых в ней нет, и не смягчай те, что есть.
   Раздел «Как применять» уточняет критерий — следуй ему.
3. «Нет» — действие было нужно, но оператор его пропустил или выполнил неправильно. N/A — только если для критерия
   N/A разрешён и выполнено его условие неприменимости. Если N/A не разрешён, выбирай только из разрешённых ответов.
4. Для шкального критерия выбирай ровно одну из перечисленных ступеней.
5. Доказательство — 1–3 реплики: id из квадратных скобок и ДОСЛОВНАЯ цитата — непрерывный фрагмент текста именно этой
   реплики, скопированный символ в символ (без перевода, пересказа, многоточий и изменения пунктуации). Можно цитировать
   реплику любой роли, в том числе системное сообщение. Для пропущенного действия укажи checked_range — id первой и
   последней реплики ОПЕРАТОРА в участке, где действие ожидалось. Для N/A укажи, какое условие неприменимости выполнено.
6. Порог первой реакции (3 минуты), время ожидания до подключения оператора, теги и вложения в оценку не входят.
7. Звонки — автоматическая расшифровка речи с ошибками распознавания: оценивай смысл сказанного, ошибки распознавания
   не считай ошибками оператора. «[Имя скрыто]» означает, что имя было произнесено и скрыто при обезличивании.
8. Объяснение — 1–2 предложения по-русски в стиле образца: «Завершение — Нет. После ответа на вопрос оператор закрыл чат
   без прощания. Основание — последняя реплика оператора и сообщение о закрытии чата».
9. Отвечай только JSON-объектом по заданной схеме, без текста до или после него."""


# ---------------------------------------------------------------- guidance overlay (data)
_overlay_cache: dict = {}


def load_overlay(path: Path | None = None) -> dict:
    p = path or Path(os.environ.get("CQA_GUIDANCE_FILE") or GUIDANCE_FILE)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"criteria": {}}
    key = str(p)
    if _overlay_cache.get(key, (None,))[0] != mtime:
        _overlay_cache[key] = (mtime, json.loads(p.read_text(encoding="utf-8")))
    return _overlay_cache[key][1]


def parse_readings(spec: str | dict | None) -> dict:
    if not spec:
        return {}
    if isinstance(spec, dict):
        return dict(spec)
    out = {}
    for part in str(spec).split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def active_readings(readings: dict | str | None = None) -> dict:
    """Effective reading per criterion: defaults < env < argument, then canonicalize retired names."""
    ov = load_overlay()
    eff = {cid: c.get("active") for cid, c in ov.get("criteria", {}).items() if c.get("active")}
    eff.update(parse_readings(os.environ.get("CQA_READINGS")))
    eff.update(parse_readings(readings))
    for cid, entry in ov.get("criteria", {}).items():
        eff[cid] = (entry.get("retired_readings") or {}).get(eff.get(cid), eff.get(cid))
    return {cid: reading for cid, reading in eff.items() if reading}


def _norm_text(s) -> str:
    return " ".join(str(s or "").split()).lower()


OFFICIAL_CHECKLIST = HERE.parents[1] / "data" / "checklists" / "v1.json"
_official_cache: dict = {}


def _official_contracts() -> dict:
    """{criterion id: {hash, title, description}} of the organizer checklist; the hash is RECOMPUTED (full semantic
    contract: wording, guidance, N/A rule, scale, critical rule), never trusted from the file."""
    from ..models import criterion_hash

    try:
        mtime = OFFICIAL_CHECKLIST.stat().st_mtime
    except OSError:
        return {}
    if _official_cache.get("mtime") != mtime:
        try:
            raw = json.loads(OFFICIAL_CHECKLIST.read_text(encoding="utf-8"))
            contracts = {str(c["id"]): {"hash": criterion_hash(c), "title": c.get("title"), "description": c.get("description")}
                         for c in raw.get("criteria") or [] if isinstance(c, dict) and c.get("id")}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            contracts = {}
        _official_cache.update(mtime=mtime, contracts=contracts)
    return _official_cache["contracts"]


def overlay_matches(entry: dict, criterion: dict) -> bool:
    """Overlay (guidance lines AND code sub-checks C3/C7) applies only to the unchanged organizer criterion: its full
    semantic hash, recomputed here (a caller's cached "hash" is ignored), equals the overlay's own hash or the hash of
    the official criterion the overlay was written for. Changing guidance, the N/A rule, the scale or the critical rule
    detaches the overlay; changing only weight/section keeps it (they are not part of the semantic hash)."""
    from ..models import criterion_hash

    try:
        actual = criterion_hash(criterion)
    except (TypeError, ValueError, AttributeError, KeyError):
        return False
    if entry.get("hash") and str(entry["hash"]) == actual:
        return True
    official = _official_contracts().get(str(criterion.get("id")))
    # Legacy overlay entries carry a pre-criterion_hash 12-char hash: they are pinned to the official wording.
    return bool(official) and bool(entry.get("title")) and actual == official["hash"] \
        and _norm_text(entry.get("title")) == _norm_text(official.get("title")) \
        and _norm_text(entry.get("description")) == _norm_text(official.get("description"))


def guidance_for(criterion: dict, readings: dict | str | None = None) -> tuple[list[str], str | None]:
    """Criterion's own guidance + overlay lines for the unchanged organizer criterion (matched by id AND hash).

    Returns (lines, reading_name)."""
    own = criterion.get("guidance") or []
    lines = [own] if isinstance(own, str) else list(own)
    entry = load_overlay().get("criteria", {}).get(criterion.get("id"))
    reading = None
    if entry and overlay_matches(entry, criterion):
        lines += list(entry.get("base") or [])
        variants = entry.get("variants") or {}
        if variants:
            reading = active_readings(readings).get(criterion["id"]) or entry.get("active") or next(iter(variants))
            lines += list(variants.get(reading) or [])
    seen, out = set(), []
    for line in lines:
        s = str(line).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out, reading


def checks_for(criterion: dict) -> dict | None:
    """Sub-check spec ({rule, fields}) for the unchanged organizer criterion, or None. The final answer for such a
    criterion is computed by cqa.checks from the model's sub-check flags (calibration v7.1)."""
    entry = load_overlay().get("criteria", {}).get(criterion.get("id"))
    if entry and overlay_matches(entry, criterion):
        spec = entry.get("checks")
        if isinstance(spec, dict) and spec.get("rule") and isinstance(spec.get("fields"), dict) and spec["fields"]:
            return spec
    return None


def _checks_example(spec: dict | None) -> dict | None:
    if not spec:
        return None
    return {k: ("<id или пустая строка>" if k.endswith("_id") else "<true|false>") for k in spec["fields"]}


# ---------------------------------------------------------------- rendering
def _fmt_sec(sec: float | None) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(round(sec)))
    return f"{sec // 60:02d}:{sec % 60:02d}"


_HOLD_END = re.compile(r"больше не находится на удержании|снят с удержания|no longer on hold|off hold|ұстаудан шығарылды", re.I)
_HOLD_START = re.compile(r"на удержании|on hold|ұстауда", re.I)


def hold_spans(conv: dict) -> list[tuple[int, int]]:
    """(start_idx, end_idx) of call segments while the client was on hold (system events). The operator's talk with
    colleagues/traders in between was not heard by the client and is not graded (guidance C5/C6)."""
    spans, start = [], None
    for m in conv.get("messages") or []:
        if m.get("speaker") != "system":
            continue
        text = str(m.get("text", ""))
        if _HOLD_END.search(text):
            if start is not None:
                spans.append((start, m["idx"]))
            start = None
        elif _HOLD_START.search(text) and start is None:
            start = m["idx"]
    return spans


def render_transcript(conv: dict) -> str:
    lines = []
    spans = hold_spans(conv) if conv.get("channel") == "call" else []
    for m in conv["messages"]:
        span = next((s for s in spans if s[0] < m["idx"] < s[1]), None)
        if span:  # collapse the on-hold segment into one marker line (also ~shorter prompts for calls)
            if m["idx"] == span[0] + 1:
                hidden = span[1] - span[0] - 1
                lines.append(f"[… удержание: {hidden} реплик(и) разговора оператора с коллегами опущены — клиент их не слышал, не оценивай …]")
            continue
        ts = m.get("ts_raw", "") if conv.get("channel") == "chat" else _fmt_sec(m.get("t_sec"))
        role = ROLE.get(m["speaker"], m["speaker"].upper())
        text = " ".join(str(m["text"]).split())
        lines.append(f"[{m['id']}] {ts} {role}: {text}")
    return "\n".join(lines)


def numeric_range(criterion: dict) -> tuple:
    """(min, max) of a numeric criterion: the scale bounds (one value = 0..value), as in cqa.models.check_answer."""
    vals = [float(v) for v in (criterion.get("scale") or [])]
    if not vals:
        return 0, 0
    lo = min(vals) if len(vals) > 1 else 0.0
    return _num(lo), _num(max(vals))


def numeric_text(criterion: dict) -> str:
    lo, hi = numeric_range(criterion)
    return f"число от {lo} до {hi}"


def allowed_answers(criterion: dict) -> list:
    if criterion.get("answer_type") == "scale":
        vals = [_num(v) for v in (criterion.get("scale") or [])]
    elif criterion.get("answer_type") == "numeric":
        vals = [numeric_text(criterion)]
    else:
        vals = ["yes", "no"]
    if criterion.get("na_allowed"):
        vals.append("na")
    return vals


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def criterion_block(criterion: dict, readings: dict | str | None = None) -> str:
    c = criterion
    out = [f"КРИТЕРИЙ {c['id']} «{c.get('title', '')}»" + (f" (раздел «{c['section']}»)" if c.get("section") else "")]
    if c.get("answer_type") == "scale":
        steps = [_num(v) for v in (c.get("scale") or [])]
        out.append("Тип ответа: шкала, допустимые ступени: " + ", ".join(str(s) for s in steps) + (" или N/A" if c.get("na_allowed") else ""))
    elif c.get("answer_type") == "numeric":
        out.append("Тип ответа: " + numeric_text(c) + " включительно (answer — JSON-число, не строка)"
                   + (" или N/A" if c.get("na_allowed") else ""))
    else:
        out.append("Тип ответа: да / нет" + (" / N/A" if c.get("na_allowed") else " (N/A не разрешён)"))
    out.append(f"Формулировка чек-листа: {c.get('description', '').strip()}")
    labels = c.get("scale_labels") or {}
    if c.get("answer_type") == "scale" and labels:
        out.append("Ступени шкалы:")
        for s in sorted((c.get("scale") or []), key=lambda x: -float(x)):
            lab = labels.get(str(_num(s))) or labels.get(str(s)) or labels.get(_num(s)) or ""
            out.append(f"  {_num(s)} — {lab}")
    elif c.get("answer_type") == "numeric" and labels:
        out.append("Ориентиры диапазона:")
        for k, lab in sorted(labels.items(), key=lambda kv: -float(kv[0])):
            out.append(f"  {_num(k)} — {lab}")
    if c.get("na_allowed"):
        out.append(f"Условие N/A: {c.get('na_rule') or 'не указано — ставь N/A только если критерий явно неприменим'}")
    else:
        out.append("N/A для этого критерия не разрешён.")
    if c.get("critical"):
        when = c.get("critical_when") or "yes"
        word = {"yes": "«yes» (Да)", "no": "«no» (Нет)"}.get(when, when)
        out.append(f"ВНИМАНИЕ: это критерий грубых нарушений без баллов. Ответ {word} означает, что НАРУШЕНИЕ ВЫЯВЛЕНО.")
        if c.get("critical_rule"):
            out.append(f"Правило: {c['critical_rule']}")
    guidance, _ = guidance_for(c, readings)
    if guidance:
        out.append("Как применять:")
        out += [f"- {g}" for g in guidance]
    spec = checks_for(c)
    if spec:
        out.append("Подпроверки — ОБЯЗАТЕЛЬНО заполни объект \"checks\" в ответе по этому критерию (итоговый ответ по ним "
                   "вычисляет код по формуле чек-листа; answer всё равно укажи):")
        out += [f"- {k}: {v}" for k, v in spec["fields"].items()]
    if c.get("answer_type") == "numeric":
        out.append("Допустимые значения answer: " + numeric_text(c) + (", \"na\"" if c.get("na_allowed") else ""))
    else:
        out.append("Допустимые значения answer: " + ", ".join(json.dumps(a, ensure_ascii=False) for a in allowed_answers(c)))
    return "\n".join(out)


def _schema_item(criterion_id: str, spec: dict | None = None) -> str:
    checks = _checks_example(spec)
    return (
        "{\n"
        f'  "criterion_id": "{criterion_id}",\n'
        + (f'  "checks": {json.dumps(checks, ensure_ascii=False)},\n' if checks else "")
        +
        '  "analysis": "2–6 коротких предложений: что требует критерий, что именно произошло в диалоге (с id реплик), какой ответ из этого следует",\n'
        '  "answer": <одно из допустимых значений>,\n'
        '  "evidence": [{"message_id": "<id реплики>", "quote": "<дословный фрагмент текста этой реплики>"}],\n'
        '  "checked_range": ["<id первой реплики оператора>", "<id последней реплики оператора>"] или null,\n'
        '  "na_reason": "<какое условие N/A выполнено>" или null,\n'
        '  "explanation": "<Название критерия> — <Да/Нет/балл/N/A>. <почему, 1–2 предложения>. Основание — <какие реплики>.",\n'
        '  "suggestion": "<как оператору следовало ответить, если ответ не лучший>" или null,\n'
        '  "confidence": <0.0–1.0, насколько ты уверен>\n'
        "}"
    )


def _compact_schema_item(criterion_id: str, spec: dict | None = None) -> str:
    # Keep analysis: some guidance explicitly requires details there (and, in
    # uncertain cases, a warning in BOTH analysis and explanation).
    head = {"criterion_id": criterion_id}
    if spec:
        head["checks"] = _checks_example(spec)
    return json.dumps({
        **head,
        "analysis": "<только сведения, прямо требуемые в analysis разделом «Как применять», иначе пустая строка>",
        "answer": "<одно из допустимых значений; балл — число>",
        "evidence": [{"message_id": "<id>", "quote": "<короткий дословный фрагмент>"}],
        "checked_range": ["<первый id оператора>", "<последний id оператора>"],
        "na_reason": None,
        "explanation": "<критерий> — <ответ>. <конкретная причина и id реплик>",
        "suggestion": None,
        "confidence": 0.0,
    }, ensure_ascii=False, separators=(",", ":"))


COMPACT_OUTPUT_RULES = f"""Компактный формат ответа; все правила оценки и «Как применять» сохраняются полностью.
Не пересказывай критерий. analysis заполняй только когда «Как применять» прямо требует сведения в этом поле;
в остальных случаях analysis="". Все требуемые детали сохраняй, даже если их много.
explanation: 1–2 коротких предложения с конкретной причиной и id; не повторяй analysis и цитаты.
Если «Как применять» требует повторить предупреждение в обоих полях, выполни это требование.
evidence: до 3 достаточных цитат, каждая 1–{COMPACT_QUOTE_MAX_CHARS} символов, непрерывный дословный фрагмент
указанной реплики; без пересказа, перевода, добавленных многоточий или изменения пунктуации.
Выбирай короткий фрагмент, сохраняющий смысл доказательства, а не целую длинную реплику.
Для пропуска сохрани checked_range из двух id оператора; иначе null, если диапазон не нужен.
Для N/A сохрани конкретный na_reason; иначе null. suggestion: одна короткая рекомендация при ответе ниже
лучшего, иначе null. Не добавляй вступление, общий итог или повторное объяснение за пределами JSON."""


def facts_block(facts: list[str]) -> str:
    if not facts:
        return ""
    return "ФАКТЫ О СТРУКТУРЕ ДИАЛОГА (вычислены кодом, проверь по тексту):\n" + "\n".join(f"- {f}" for f in facts)


def precedents_block(conv: dict, criteria: list[dict]) -> str:
    """Similar labelled DEV dialogues (calibration v7.1), only when a local bank exists; see cqa.precedents."""
    try:
        from .. import precedents
    except Exception:
        return ""
    return precedents.block(conv, criteria)


def _header(conv: dict) -> str:
    ch = {"chat": "чат", "call": "звонок (расшифровка речи)"}.get(conv.get("channel"), conv.get("channel"))
    return f"ДИАЛОГ {conv['id']} · канал: {ch} · язык набора: {conv.get('language') or '?'} · реплик: {len(conv['messages'])}"


def build_single(conv: dict, criterion: dict, facts: list[str] | None = None, readings: dict | str | None = None,
                 errors: list[str] | None = None) -> tuple[str, str]:
    """Prompt for one criterion. The transcript comes first (shared prefix -> provider-side prompt caching)."""
    parts = [_header(conv), render_transcript(conv), facts_block(facts or []), criterion_block(criterion, readings),
             "Оцени этот критерий. Верни JSON строго по схеме:\n" + _schema_item(criterion["id"], checks_for(criterion))]
    if errors:
        parts.append("Твой предыдущий ответ отклонён проверкой:\n" + "\n".join(f"- {e}" for e in errors)
                     + "\nИсправь ошибки и верни полный JSON заново.")
    return SYSTEM, "\n\n".join(p for p in parts if p)


def build_batch(conv: dict, criteria: list[dict], facts: list[str] | None = None, readings: dict | str | None = None,
                *, compact: bool = False) -> tuple[str, str]:
    """All criteria in one call; compact output is explicitly opt-in, never an env default."""
    blocks = "\n\n".join(criterion_block(c, readings) for c in criteria)
    ids = ", ".join(c["id"] for c in criteria)
    item_schema = _compact_schema_item if compact else _schema_item
    schema = '{"criteria": [\n' + ",\n".join(item_schema(c["id"], checks_for(c)) for c in criteria[:1]) + ",\n  … по одному объекту на каждый критерий: " + ids + "\n]}"
    with_checks = [c for c in criteria if checks_for(c)]
    if with_checks:
        schema += ("\nДля критериев " + ", ".join(c["id"] for c in with_checks) + " добавь в их объект поле \"checks\" с подпроверками: "
                   + "; ".join(c["id"] + ": " + json.dumps(_checks_example(checks_for(c)), ensure_ascii=False) for c in with_checks))
    parts = [_header(conv), render_transcript(conv), facts_block(facts or []), precedents_block(conv, criteria), blocks,
             COMPACT_OUTPUT_RULES if compact else "",
             f"Оцени КАЖДЫЙ критерий ({ids}) независимо. Верни JSON строго по схеме:\n" + schema]
    return SYSTEM, "\n\n".join(p for p in parts if p)


def build_arbiter(conv: dict, criterion: dict, votes: list[dict], facts: list[str] | None = None,
                  readings: dict | str | None = None) -> tuple[str, str]:
    """Arbiter prompt: independent assessors disagreed; the arbiter re-checks the dialogue and decides."""
    lines = []
    for i, v in enumerate(votes, 1):
        ans = json.dumps(v.get("answer"), ensure_ascii=False)
        why = (v.get("analysis") or v.get("explanation") or "").strip().replace("\n", " ")
        lines.append(f"Эксперт {i}: answer={ans}. Обоснование: {why[:700]}")
    parts = [_header(conv), render_transcript(conv), facts_block(facts or []), precedents_block(conv, [criterion]),
             criterion_block(criterion, readings),
             "Независимые эксперты разошлись во мнениях:\n" + "\n".join(lines)
             + "\nТы — арбитр. Не голосуй большинством: перепроверь сам по тексту диалога и правилам критерия, какой ответ верен.",
             "Верни JSON строго по схеме:\n" + _schema_item(criterion["id"], checks_for(criterion))]
    return SYSTEM, "\n\n".join(p for p in parts if p)


def prompt_version(criterion: dict | None = None, readings: dict | str | None = None) -> str:
    """Template version + hash of the criterion block (includes guidance and the active reading)."""
    h = hashlib.sha1((TEMPLATE_VERSION + SYSTEM + (criterion_block(criterion, readings) if criterion else "")).encode("utf-8")).hexdigest()[:8]
    return f"{TEMPLATE_VERSION}-{h}"


def build_dossier(conv: dict, criterion: dict, evaluation: dict, comment: str, message_ids: list[str] | None = None,
                  facts: list[str] | None = None, readings: dict | str | None = None) -> tuple[str, str]:
    """Reviewer dossier for an appeal: arguments for revising vs keeping the original answer, with quotes."""
    ev = evaluation or {}
    quotes = "; ".join(f"[{e.get('message_id')}] «{e.get('quote')}»" for e in (ev.get("evidence") or [])) or "нет"
    orig = (f"Исходная оценка ИИ: answer={json.dumps(ev.get('answer'), ensure_ascii=False)}. Объяснение: {ev.get('explanation') or '—'} "
            f"Доказательства: {quotes}. Проверенный участок: {ev.get('checked_range') or '—'}." + (f" Причина N/A: {ev.get('na_reason')}." if ev.get("na_reason") else ""))
    appeal = f"Апелляция оператора: «{(comment or '').strip() or 'без комментария'}»." + (f" Указанные реплики: {', '.join(message_ids)}." if message_ids else "")
    schema = (
        "{\n"
        '  "for": [{"text": "довод за пересмотр оценки", "evidence": [{"message_id": "<id>", "quote": "<дословный фрагмент>"}]}],\n'
        '  "against": [{"text": "довод за сохранение исходной оценки", "evidence": [{"message_id": "<id>", "quote": "<дословный фрагмент>"}]}],\n'
        '  "recommendation": "1–2 предложения: сохранить или изменить оценку и почему (решение остаётся за проверяющим)",\n'
        '  "suggested_answer": <допустимое значение критерия или null>,\n'
        '  "confidence": <0.0–1.0>\n'
        "}"
    )
    parts = [_header(conv), render_transcript(conv), facts_block(facts or []), criterion_block(criterion, readings), orig, appeal,
             "Подготовь досье для проверяющего: честно приведи доводы обеих сторон с дословными цитатами реплик, затем рекомендацию. "
             "Верни JSON строго по схеме:\n" + schema]
    return SYSTEM, "\n\n".join(p for p in parts if p)
