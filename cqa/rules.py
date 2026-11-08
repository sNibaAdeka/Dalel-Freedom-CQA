"""Deterministic, explainable facts about a conversation (RU / KZ / EN). Used ONLY as prompt hints and to flag
model-vs-rule disagreement for human review; the final answer always comes from the model.

Input: normalized conversation {id, channel, language, messages: [{id, idx, speaker, text, ts_raw, t_sec}]}.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .models import criterion_hash

HANDOFF = re.compile(r"эскалац|передан[ао]? оператор|операторға жіберілді|transferred to an agent|агент присоединился|"
                     r"переведен от агента|connected to an agent", re.I)
IDENTITY_OK = re.compile(r"личност\w* .{0,20}подтвержд|жеке бас\w* расталды|identity (was )?verified|identity confirmed", re.I)
OP_CLOSED = re.compile(r"оператор закрыл|оператор чатты жапты|agent closed|operator closed|агент вышел", re.I)
BROKEN = re.compile(r"соединени\w* .{0,20}прерван|клиент покинул|байланыс үзілді|connection .{0,20}lost|customer left|"
                    r"клиент чаттан шықты", re.I)
CLIENT_ENDED = re.compile(r"клиент завершил разговор|клиент аяқтады|customer ended", re.I)

GREET = re.compile(r"здравствуй|добр(ый|ое|ого) (день|вечер|утр)|приветству|\bпривет|сәлем|сәлеметсіз|қайырлы|"
                   r"\bhello\b|\bhi\b|\bhey\b|good (morning|afternoon|evening)|welcome", re.I)
INTRO_I = re.compile(r"меня зовут|на связи|вас приветствует|менің атым|my name is|\[имя скрыто\]", re.I)
INTRO_CS = re.compile(r"\b[яЯ] [А-ЯЁ][а-яё]+|\b[мМ]ен [А-ЯӘҒҚҢӨҰҮҺІ][а-яәғқңөұүһі]+|"
                      r"\b[А-ЯӘҒҚҢӨҰҮҺІ][а-яәғқңөұүһі]+(мын|мін|пын|пін|бын|бін)\b|\b[Ii]['’]?m [A-Z][a-z]+|\b[Tt]his is [A-Z][a-z]+")
FAREWELL = re.compile(r"до свидания|всего (доброго|хорошего)|хорошего (дня|вечера)|до встречи|сау болыңыз|күніңіз сәтті|"
                      r"\bgoodbye\b|\bbye\b|have a (nice|good|great) (day|evening)|take care", re.I)
BYPASS = re.compile(r"мне на карту|скиньте деньги|переведите (мне|деньги мне)|я (сам|лично) (их )?вложу|лично предлагаю|"
                    r"через компанию (оформлять )?не надо|личн\w* номер|жеке нөмір|ақылы түрде|personal number|"
                    r"private advice|i charge for|send (me|the money to me)", re.I)
INSULT = re.compile(r"читать умеете|вы (что|вообще) .{0,15}(тупой|глупый)|идиот|дура[кч]|тупо|заткн|"
                    r"оқи аласыз ба|can'?t you read|are you stupid|idiot", re.I)

PAUSE_SEC = 120

# Only direct system event statements qualify. In particular, spoken requests,
# negations, language-change notices and references to a prior call do not.
_RECORDING_ON = re.compile(
    r"(?:запись(?: и транскрибирование)? (?:запущена|запущены|возобновлена|возобновлены)|"
    r"жазу(?: және транскрипция)? (?:басталды|жалғастырылды)|"
    r"recording(?: and transcription)? (?:started|resumed))[.!]?", re.I)
_MISSING_BEGINNING = re.compile(
    r"(?:начало записи (?:отсутствует|обрезано)|запись начинается с середины разговора|"
    r"жазбаның басы жоқ|recording starts mid-conversation|"
    r"(?:the )?beginning of (?:the )?recording is missing)[.!]?", re.I)


@lru_cache(maxsize=1)
def _official_contact_hash() -> str | None:
    """Use the local official contract, independently of editable prompt overlays."""
    try:
        path = Path(__file__).resolve().parents[1] / "data/checklists/v1.json"
        criteria = json.loads(path.read_text(encoding="utf-8"))["criteria"]
        contact = next(c for c in criteria if c["id"] == "C1")
        return criterion_hash(contact)
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None  # No trusted contract: abstain from the special-case rule.


def _is_official_contact(criterion: dict) -> bool:
    # Never trust a caller's cached hash or just the title/id: guidance, polarity,
    # applicability and scale edits can all change the meaning of an answer.
    return criterion.get("id") == "C1" and criterion_hash(criterion) == _official_contact_hash()


def _recording_beginning_signals(conv: dict) -> list[str]:
    """Evidence to CHECK call completeness, never proof that speech is missing.

    Message order suffices; neither a large timestamp/offset nor missing greeting
    keywords establishes clipping. Require operator speech before a late start,
    so IVR selections/client speech before agent connection alone do not qualify.
    Only the first recording on-event matters:
    an ordinary pause/resume later in an already recorded call is not a signal.
    """
    if conv.get("channel") != "call":
        return []
    spoken = []
    operator_spoke = False
    seen_on = False
    signals = []
    for m in conv["messages"]:
        text = " ".join(m["text"].split())
        if m["speaker"] in ("client", "operator") and text:
            spoken.append(m["id"])
            operator_spoke = operator_spoke or m["speaker"] == "operator"
        if m["speaker"] != "system" or m.get("speaker_raw"):
            continue
        if _MISSING_BEGINNING.fullmatch(text):
            signals.append(f"системная пометка о неполном начале [{m['id']}]")
        if _RECORDING_ON.fullmatch(text):
            if not seen_on and operator_spoke:
                span = spoken[0] if len(spoken) == 1 else f"{spoken[0]} … {spoken[-1]}"
                signals.append(f"системный запуск/возобновление записи [{m['id']}] следует за репликами "
                               f"клиента/оператора [{span}]; текст до запуска присутствует")
            seen_on = True
    return signals


def recording_review_reasons(conv: dict, criterion: dict, answer: object) -> list[str]:
    """Reusable by evaluator and pilot after answer validation (normalized inputs).

    Only unchanged official C1 / no is affected. This adds review routing, not
    an answer, confidence, evidence or score override; C1 still cannot become N/A.
    """
    if answer != "no" or not _is_official_contact(criterion):
        return []
    signals = _recording_beginning_signals(conv)
    if not signals:
        return []
    return ["проверьте полноту начала записи: " + "; ".join(signals)
            + ". Это не доказывает пропуск приветствия или представления; ответ «no» предварительный "
              "— нужна ручная проверка."]


def call_scale_review_reasons(conv: dict, criterion: dict, answer: object) -> list[str]:
    """Calls: every scale answer goes to a human. On DEV calls the model overrated the C4/C6 scales
    (29/37 cells vs the machine reference) and its own confidence did not flag those errors.
    This adds review routing only; the answer, evidence and score are unchanged."""
    if conv.get("channel") != "call" or criterion.get("answer_type") != "scale":
        return []
    if answer is None or answer == "na":
        return []
    return ["звонок: шкальную оценку проверяет человек — на звонках DEV модель завышала шкалы (C4/C6)"]


def _ops(conv: dict) -> list[dict]:
    return [m for m in conv["messages"] if m["speaker"] == "operator"]


def handoff_message(conv: dict) -> dict | None:
    for m in conv["messages"]:
        if m["speaker"] == "system" and HANDOFF.search(m["text"]):
            return m
    return None


def first_reply(conv: dict) -> list[dict]:
    """First operator answer: the first operator message (chat); in a call all consecutive operator segments until the
    next client message (system/bot events in between do not interrupt)."""
    msgs = conv["messages"]
    start = next((i for i, m in enumerate(msgs) if m["speaker"] == "operator"), None)
    if start is None:
        return []
    if conv.get("channel") != "call":
        return [msgs[start]]
    out = []
    for m in msgs[start:]:
        if m["speaker"] == "client":
            break
        if m["speaker"] == "operator":
            out.append(m)
    return out


def pauses(conv: dict, min_sec: float = PAUSE_SEC) -> list[tuple[dict, dict, float]]:
    """Client waited >= min_sec for the operator's next message, after the operator's first reply."""
    msgs = conv["messages"]
    first = next((i for i, m in enumerate(msgs) if m["speaker"] == "operator"), None)
    if first is None:
        return []
    out, last_client = [], None
    for m in msgs[first + 1 :]:
        if m["speaker"] == "client":
            last_client = last_client or m
        elif m["speaker"] == "operator":
            if last_client and m.get("t_sec") is not None and last_client.get("t_sec") is not None:
                gap = m["t_sec"] - last_client["t_sec"]
                if gap >= min_sec:
                    out.append((last_client, m, gap))
            last_client = None
    return out


def _clock(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _short(text: str, n: int = 90) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def facts(conv: dict) -> list[str]:
    """Human-readable structural facts for the prompt."""
    out = []
    if conv.get("language"):
        out.append(f"Язык диалога по данным набора: {conv['language']}.")
    ops = _ops(conv)
    if not ops:
        out.append("Оператор в диалоге не написал ни одной реплики.")
    h = handoff_message(conv)
    if h:
        out.append(f"Передача оператору: [{h['id']}] «{_short(h['text'])}».")
    fr = first_reply(conv)
    if fr:
        ids = fr[0]["id"] if len(fr) == 1 else f"{fr[0]['id']} … {fr[-1]['id']} ({len(fr)} сегм.)"
        out.append(f"Первый ответ оператора: [{ids}].")
    if ops:
        last = ops[-1]
        tail = [m for m in conv["messages"] if m["idx"] > last["idx"]]
        if tail:
            desc = "; ".join(f"[{m['id']}] {m['speaker'].upper()} «{_short(m['text'], 60)}»" for m in tail[:4])
            out.append(f"Последняя реплика оператора: [{last['id']}]; после неё: {desc}.")
        else:
            out.append(f"Последняя реплика оператора: [{last['id']}] — она же последняя в диалоге.")
    sys_events = [m for m in conv["messages"] if m["speaker"] == "system" and m is not h]
    if sys_events:
        out.append("Системные события: " + "; ".join(f"[{m['id']}] «{_short(m['text'], 70)}»" for m in sys_events[:12]) + ".")
    for c, o, gap in pauses(conv)[:5]:
        out.append(f"Пауза в ходе диалога: клиент [{c['id']}] ждал ответа оператора [{o['id']}] {_clock(gap)}.")
    return out


# ---------------------------------------------------------------- rule checks (disagreement flags only)
def check_contact(conv: dict) -> str | None:
    fr = first_reply(conv)
    if not fr:
        return "no"
    text = " ".join(m["text"] for m in fr)
    return "yes" if GREET.search(text) and (INTRO_I.search(text) or INTRO_CS.search(text)) else "no"


def check_closing_lenient(conv: dict) -> str | None:
    ops = _ops(conv)
    if not ops:
        return None
    last = ops[-1]
    tail = [m for m in conv["messages"] if m["idx"] > last["idx"]]
    if any(m["speaker"] == "system" and BROKEN.search(m["text"]) for m in tail) and not FAREWELL.search(last["text"]):
        return "na"
    return "yes" if FAREWELL.search(last["text"]) else "no"


def check_gross(conv: dict) -> str | None:
    """'yes' if an operator message matches insult/bypass patterns; None otherwise (no claim of absence)."""
    for m in _ops(conv):
        if BYPASS.search(m["text"]) or INSULT.search(m["text"]):
            return "yes"
    return None


def check_identity(conv: dict) -> str | None:
    """'yes' if the system confirmed identity; None otherwise."""
    return "yes" if any(m["speaker"] == "system" and IDENTITY_OK.search(m["text"]) for m in conv["messages"]) else None


RULE_FOR_TITLE = [  # (regex on criterion title, check, compare only these answers)
    (re.compile(r"установлени\w* контакт|приветств|greeting", re.I), check_contact),
    (re.compile(r"грубы\w* (ошибк|нарушени)|gross|critical", re.I), check_gross),
    (re.compile(r"идентификац|identification", re.I), check_identity),
]


def rule_check(criterion: dict, conv: dict) -> str | None:
    """Expected answer from a deterministic rule for well-known criterion kinds, or None (no opinion)."""
    title = criterion.get("title", "")
    for rx, fn in RULE_FOR_TITLE:
        if rx.search(title):
            if fn is check_contact:
                if not _is_official_contact(criterion):
                    return None
                expected = fn(conv)
                # ASR/recording uncertainty cannot prove a missing greeting/name.
                if expected == "no" and _recording_beginning_signals(conv):
                    return None
                return expected
            return fn(conv)
    return None
