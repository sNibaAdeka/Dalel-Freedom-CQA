"""Ingest the organizers' dataset (official API format) into normalized conversations.

Input: the API envelope ``{"isSuccess": true, "data": {"dataset_group_id", "records": [...]}}``, a plain list of records,
or ``{"records": [...]}``. One record = ``{conversation_id, channel, language, transcript: [{timestamp, speaker, text}],
audio_url}``. Every transcript turn becomes exactly one message (never merged).

Output (plain dicts, the frontend detail shape without evaluations)::

    {id, channel: "chat"|"call", language: "RU"|"KZ"|"EN", group_id, audio_url, messages, meta}
    message = {id: "<conv_id>:<idx>", idx, speaker: client|operator|bot|system, text, ts_raw, t_sec, rec_sec}

``t_sec``: chats -- the MM:SS (or H:MM:SS) value; calls -- ISO 8601 minus the first event.
``rec_sec`` (calls only): position in the recording = t_sec - recording start - recording pauses before it. The recording
starts at the system event "Запись и транскрибирование запущены" / "Жазу және транскрипция басталды" / "Recording ...
started" (otherwise at the first event); pauses are "... приостановлены" -> "... возобновлены" (RU/KZ/EN variants).
Messages before the start or inside a pause are not in the recording: ``rec_sec = None``.

``audio_url`` keeps the organizers' signed player link for ``cqa.audio``; never send it to the browser -- use
``public_view()``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("client", "operator", "bot", "system")
SPEAKER_ALIASES = {
    "client": "client", "customer": "client", "user": "client", "клиент": "client",
    "operator": "operator", "agent": "operator", "оператор": "operator", "representative": "operator",
    "bot": "bot", "ivr": "bot", "assistant": "bot", "virtual agent": "bot", "бот": "bot",
    "system": "system", "система": "system", "жүйе": "system",
}
LANG_ALIASES = {"RU": "RU", "RUS": "RU", "RU-RU": "RU", "KZ": "KZ", "KK": "KZ", "KAZ": "KZ", "KK-KZ": "KZ",
                "KZ-KZ": "KZ", "EN": "EN", "ENG": "EN", "EN-US": "EN", "EN-GB": "EN"}
CALL_CHANNELS = ("voice call", "voice", "call", "phone", "phone call", "звонок")
CHAT_CHANNELS = ("agent chat", "chat", "чат", "web chat", "messaging")

# Recording events: the text must mention the recording/transcription AND a state verb.
_REC_SUBJECT = re.compile(r"запис|транскриб|жазу|жазба|транскрипц|record|transcri", re.I)
_REC_RESUME = re.compile(r"возобнов|продолж|жалғас|қайта (?:басталды|қосылды|іске)|resum|continu|restart", re.I)
_REC_PAUSE = re.compile(r"приостанов|пауз|кідірт|уақытша тоқтат|paus|suspend|on hold", re.I)
_REC_STOP = re.compile(r"остановлен|прекращен|завершен|тоқтатыл|аяқталды|stopped|ended|terminated|\bstop", re.I)
_REC_START = re.compile(r"запущен|начал|начат|стартова|басталды|басталған|іске қосылды|started|\bstart|began|begun|initiated", re.I)
_LANG_CHANGE = re.compile(r"language changed from\s+([a-z]{2})(?:-[A-Z]{2})?\s+to\s+([a-z]{2})", re.I)
_HANDOFF = re.compile(
    r"эскалац|оператор(?:у|а)? (?:подключ|присоедин)|переведен .*представител|агент присоединился|"
    r"операторға жіберілді|операторға қосыл|transferred to an agent|agent joined|escalat", re.I)
_HOLD_OFF = re.compile(r"больше не находится на удержании|снят\w* с удержания|no longer on hold|removed from hold|"
                       r"taken off hold|ұстаудан шығарылды|күтуден шықты", re.I)
_HOLD_ON = re.compile(r"на удержани|on hold|ұстап тұр|күту режим", re.I)
_END = re.compile(r"завершил\w* (?:разговор|звонок)|(?:разговор|звонок) заверш|закрыл\w* чат|покинул\w* чат|"
                  r"соединение с клиентом прервано|ended the (?:conversation|call)|call ended|closed the chat|"
                  r"left the chat|connection to (?:the )?customer lost|сөйлесуді аяқтады|қоңырауды аяқтады|"
                  r"чатты жапты|байланыс үзілді|чаттан шықты", re.I)
_DTMF_CHARS = re.compile(r"^[\s\d#*.,+-]+$")
_DTMF_WORDS = {"ноль", "один", "одна", "два", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
               "решётка", "решетка", "звёздочка", "звездочка", "zero", "one", "two", "three", "four", "five", "six",
               "seven", "eight", "nine", "hash", "star", "pound", "нөл", "бір", "екі", "үш", "төрт", "бес", "алты",
               "жеті", "сегіз", "тоғыз"}
_KZ_LETTERS = re.compile(r"[әғқңөұүһі]", re.I)
_CYR = re.compile(r"[а-яё]", re.I)
_LAT = re.compile(r"[a-z]", re.I)
_REL_TIME = re.compile(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d+))?\s*$")


class IngestError(ValueError):
    """The dataset cannot be read (unsuccessful API response, wrong shape, empty)."""


def _r(value: float | None, places: int = 3) -> float | None:
    if value is None:
        return None
    q = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    f = float(q)
    return int(f) if f.is_integer() else f


# --- payload --------------------------------------------------------------------------------------------------

def parse_payload(payload: object) -> tuple[list[dict], str | None]:
    """Return (records, dataset_group_id or None) from any of the accepted shapes."""
    group_id = None
    value = payload
    if isinstance(value, dict):
        if value.get("isSuccess") is False:
            raise IngestError(f"Organizers' API returned an error: {value.get('error') or 'isSuccess=false'}")
        if "data" in value:
            value = value["data"]
        if isinstance(value, dict):
            group_id = value.get("dataset_group_id") or value.get("group_id")
            value = value.get("records")
    if not isinstance(value, list) or not value:
        raise IngestError("Expected a nonempty list of records (organizers' API envelope or a plain list)")
    if not all(isinstance(r, dict) for r in value):
        raise IngestError("Every record must be a JSON object")
    return value, (str(group_id) if group_id else None)


# --- time -----------------------------------------------------------------------------------------------------

def _rel_seconds(raw: str) -> float | None:
    m = _REL_TIME.match(raw)
    if not m:
        return None
    a, b, c, frac = m.groups()
    secs = int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b)
    if frac:
        secs += float(f"0.{frac}")
    return secs


def _abs_seconds(raw: str) -> float | None:
    text = raw.strip().replace("Z", "+00:00").replace("z", "+00:00")
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        m = re.match(r"^(.*?\.\d{6})\d+(.*)$", text)  # >6 fractional digits on older Pythons
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1) + m.group(2))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_time(raw: object) -> tuple[float | None, str]:
    """-> (seconds, kind) with kind in relative|absolute|invalid."""
    text = "" if raw is None else str(raw)
    rel = _rel_seconds(text)
    if rel is not None:
        return rel, "relative"
    ab = _abs_seconds(text)
    if ab is not None:
        return ab, "absolute"
    return None, "invalid"


# --- small classifiers ----------------------------------------------------------------------------------------

def _speaker(raw: object) -> tuple[str, str | None]:
    key = str(raw or "").strip().lower()
    if key in ROLES:
        return key, None
    if key in SPEAKER_ALIASES:
        return SPEAKER_ALIASES[key], None
    return "system", str(raw)


def guess_language(text: str) -> str | None:
    letters = re.sub(r"[^\w]", "", text or "")
    if len(letters) < 3:
        return None
    if _KZ_LETTERS.search(text):
        return "KZ"
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if cyr == 0 and lat == 0:
        return None
    return "RU" if cyr >= lat else "EN"


def _language(raw: object, messages: list[dict]) -> str:
    key = str(raw or "").strip().upper()
    if key in LANG_ALIASES:
        return LANG_ALIASES[key]
    votes: dict[str, int] = {}
    for m in messages:
        if m["speaker"] in ("client", "operator"):
            g = guess_language(m["text"])
            if g:
                votes[g] = votes.get(g, 0) + 1
    return max(votes, key=votes.get) if votes else "RU"


def _channel(raw: object, kinds: list[str], audio_url: object) -> str:
    key = str(raw or "").strip().lower()
    if key in CALL_CHANNELS or "voice" in key or "call" in key:
        return "call"
    if key in CHAT_CHANNELS or "chat" in key:
        return "chat"
    if audio_url or "absolute" in kinds:
        return "call"
    return "chat"


def recording_event(text: str) -> str | None:
    """'on' (start/resume), 'off' (pause/stop) or None for a system message."""
    if not text or not _REC_SUBJECT.search(text) or _LANG_CHANGE.search(text) or re.search(r"language", text, re.I):
        return None
    if _REC_RESUME.search(text):
        return "on"
    if _REC_PAUSE.search(text) or _REC_STOP.search(text):
        return "off"
    if _REC_START.search(text):
        return "on"
    return None


# --- normalization --------------------------------------------------------------------------------------------

def normalize_record(record: dict, group_id: str = "DEV", position: int = 0) -> dict:
    cid = str(record.get("conversation_id") or record.get("id") or "").strip()
    if not cid:
        cid = f"{group_id}-{position + 1:03d}"
    transcript = record.get("transcript") or []
    if isinstance(transcript, str):  # tolerate a JSON-encoded transcript
        try:
            transcript = json.loads(transcript)
        except json.JSONDecodeError:
            transcript = []
    if not isinstance(transcript, list):
        transcript = []
    turns = [t if isinstance(t, dict) else {"text": str(t)} for t in transcript]
    parsed = [parse_time(t.get("timestamp", t.get("ts"))) for t in turns]
    kinds = [k for _, k in parsed]
    absolute = [v for v, k in parsed if k == "absolute"]
    origin = min(absolute) if absolute else None
    audio_url = record.get("audio_url") or None
    channel = _channel(record.get("channel"), kinds, audio_url)

    messages: list[dict] = []
    for idx, (turn, (secs, kind)) in enumerate(zip(turns, parsed)):
        speaker, speaker_raw = _speaker(turn.get("speaker", turn.get("role")))
        text = turn.get("text")
        text = "" if text is None else str(text)
        if kind == "absolute":
            t_sec = _r(secs - origin)
        elif kind == "relative":
            t_sec = _r(secs)
        else:
            t_sec = None
        msg = {"id": f"{cid}:{idx}", "idx": idx, "speaker": speaker, "text": text,
               "ts_raw": "" if turn.get("timestamp", turn.get("ts")) is None else str(turn.get("timestamp", turn.get("ts"))),
               "t_sec": t_sec, "rec_sec": None}
        if speaker_raw is not None:
            msg["speaker_raw"] = speaker_raw
        messages.append(msg)

    warnings: list[str] = []
    meta: dict = {"channel_raw": record.get("channel"), "n_messages": len(messages),
                  "duration_sec": max((m["t_sec"] for m in messages if m["t_sec"] is not None), default=None)}
    meta.update(_handoff(messages))
    meta.update(_end_of_dialogue(messages))
    meta["recording_gaps"] = []
    meta["audio_offset_is_estimate"] = channel == "call"
    if channel == "call":
        meta.update(_recording(messages, warnings))
        meta.update(_hold(messages))
        meta.update(_dtmf(messages))
    meta["lang_switches"] = _lang_switches(messages)
    meta["warnings"] = warnings
    language = _language(record.get("language"), messages)
    meta["source_id"] = cid
    meta["content_hash"] = content_hash(channel, language, messages)
    return {"id": cid, "channel": channel, "language": language,
            "group_id": str(group_id), "audio_url": audio_url, "messages": messages, "meta": meta}


def content_hash(channel: str, language: str, messages: list[dict]) -> str:
    """Fingerprint of what is evaluated: channel, language and every turn (speaker, timestamp, text)."""
    canon = json.dumps([channel, language, [[m["speaker"], m["ts_raw"], m["text"]] for m in messages]],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


def rename_conversation(conv: dict, new_id: str) -> dict:
    """Same dialogue under another id; message ids follow (``<id>:<idx>``)."""
    old = conv["id"]
    out = {**conv, "id": new_id, "meta": {**conv.get("meta", {}), "renamed_from": old}}
    out["messages"] = [{**m, "id": f"{new_id}:{m['idx']}"} for m in conv["messages"]]
    out["meta"]["lang_switches"] = [{**x, "message_id": f"{new_id}:{x['idx']}"} for x in out["meta"].get("lang_switches", [])]
    return out


def resolve_id_collisions(convs: list[dict], existing: dict[str, dict]) -> list[dict]:
    """Never let a new dialogue take over a stored one (its evaluations would be reused as if computed).

    existing: {id: {"group_id", "content_hash"}} of stored conversations. Same id + same group + same content ->
    unchanged (idempotent re-ingest). Same id from another group -> ``<id>@<group>``; same group but changed
    content -> ``<id>@<group>-<hash6>``. The organizers' id stays in ``meta.source_id`` (exports use it)."""
    known = dict(existing)
    out = []
    for conv in convs:
        mine = {"group_id": conv["group_id"], "content_hash": conv["meta"]["content_hash"]}
        candidates = [conv["id"], f"{conv['id']}@{conv['group_id']}",
                      f"{conv['id']}@{conv['group_id']}-{mine['content_hash'][:6]}"]
        chosen = None
        for i, cand in enumerate(candidates):
            other = known.get(cand)
            if other is None or (other.get("group_id") == mine["group_id"]
                                 and other.get("content_hash") == mine["content_hash"]):
                if i == 1 and other is None and known.get(conv["id"], {}).get("group_id") == mine["group_id"]:
                    continue  # same group, changed content: the hash-suffixed id
                chosen = cand
                break
        chosen = chosen or candidates[-1]
        if chosen != conv["id"]:
            conv = rename_conversation(conv, chosen)
            conv["meta"]["warnings"] = conv["meta"].get("warnings", []) + [
                f"conversation_id {conv['meta']['source_id']} уже занят другим диалогом — сохранён как {chosen}."]
        known[chosen] = mine
        out.append(conv)
    return out


def _handoff(messages: list[dict]) -> dict:
    first_op = next((m for m in messages if m["speaker"] == "operator"), None)
    event = next((m for m in messages if m["speaker"] == "system" and _HANDOFF.search(m["text"])
                  and (first_op is None or m["idx"] < first_op["idx"])), None)
    wait = None
    if first_op and first_op["t_sec"] is not None:
        ref = event
        if ref is None:
            prev = [m for m in messages if m["idx"] < first_op["idx"] and m["speaker"] in ("bot", "system")]
            ref = prev[-1] if prev else None
        if ref is not None and ref["t_sec"] is not None:
            wait = _r(max(0.0, first_op["t_sec"] - ref["t_sec"]))
    first_client = next((m for m in messages if m["speaker"] == "client"), None)
    return {"handoff_idx": first_op["idx"] if first_op else None,
            "handoff_event_idx": event["idx"] if event else None,
            "first_client_idx": first_client["idx"] if first_client else None,
            "wait_sec": wait, "wait_is_estimate": True}


def _end_of_dialogue(messages: list[dict]) -> dict:
    """First 'client ended the call' / 'operator closed the chat' event; later turns are segmentation artefacts."""
    end = next((m for m in messages if m["speaker"] == "system" and _END.search(m["text"])), None)
    if end is None:
        return {"end_idx": None}
    for m in messages[end["idx"] + 1:]:
        m["after_end"] = True
    return {"end_idx": end["idx"]}


def _hold(messages: list[dict]) -> dict:
    """Hold intervals (t_sec); speech inside is not heard by the client (consultations, side talk) -> on_hold."""
    intervals: list[list[float]] = []
    start = None
    for m in messages:
        if m["speaker"] != "system" or m["t_sec"] is None:
            continue
        if _HOLD_OFF.search(m["text"]):
            if start is not None:
                intervals.append([start, m["t_sec"]])
                start = None
        elif _HOLD_ON.search(m["text"]) and start is None:
            start = m["t_sec"]
    if start is not None:
        intervals.append([start, max((m["t_sec"] for m in messages if m["t_sec"] is not None), default=start)])
    for m in messages:
        t = m["t_sec"]
        if m["speaker"] != "system" and t is not None and any(a < t < b for a, b in intervals):
            m["on_hold"] = True
    return {"hold_intervals": [[_r(a), _r(b)] for a, b in intervals]}


def _dtmf(messages: list[dict]) -> dict:
    """Key presses in the IVR are transcribed as client turns ("2", "#", "Ноль.") -> dtmf, not speech."""
    boundary = next((m["idx"] for m in messages if m["speaker"] == "operator"
                     or (m["speaker"] == "system" and _HANDOFF.search(m["text"]))), len(messages))
    hits = []
    for m in messages[:boundary]:
        text = m["text"].strip()
        word = re.sub(r"[\s.!,]+", "", text).lower()
        if m["speaker"] == "client" and text and (_DTMF_CHARS.match(text) or word in _DTMF_WORDS):
            m["dtmf"] = True
            hits.append(m["idx"])
    return {"dtmf_idx": hits}


def _recording(messages: list[dict], warnings: list[str]) -> dict:
    timed = [m for m in messages if m["t_sec"] is not None]
    events = [(m["t_sec"], recording_event(m["text"]), m["idx"]) for m in timed if m["speaker"] == "system"]
    events = [e for e in events if e[1]]
    start = 0.0
    start_idx = None
    if events and events[0][1] == "on":
        start, start_idx = events[0][0], events[0][2]
        events = events[1:]
    gaps: list[list[float]] = []
    open_at = None
    for t, kind, _ in events:
        if kind == "off" and open_at is None and t >= start:
            open_at = t
        elif kind == "on" and open_at is not None and t >= open_at:
            gaps.append([open_at, t])
            open_at = None
    end = max((m["t_sec"] for m in timed), default=start)
    if open_at is not None:
        later_speech = [m for m in timed if m["t_sec"] > open_at and m["speaker"] in ("client", "operator")]
        if later_speech:
            warnings.append("Запись приостановлена без события возобновления: реплики после паузы не привязаны к записи.")
        gaps.append([open_at, max(end, open_at)])

    def rec(t: float) -> float | None:
        if t < start or (open_at is not None and t >= open_at):
            return None
        if any(a <= t < b for a, b in gaps):
            return None
        removed = sum(min(t, b) - a for a, b in gaps if t > a)
        return _r(max(0.0, t - start - removed))

    for m in timed:
        m["rec_sec"] = rec(m["t_sec"])
    last = [m["rec_sec"] for m in timed if m["rec_sec"] is not None]
    gaps_rec, removed = [], 0.0
    for a, b in gaps:  # where each pause sits on the recording timeline
        gaps_rec.append(_r(max(0.0, a - start - removed)))
        removed += b - a
    return {"recording_start_sec": _r(start), "recording_start_idx": start_idx,
            "recording_gaps": [[_r(a), _r(b)] for a, b in gaps], "recording_gaps_rec": gaps_rec,
            "recording_duration_sec": max(last) if last else None, "audio_offset_is_estimate": True}


def _lang_switches(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    prev = None
    for m in messages:
        if m["speaker"] == "system":
            hit = _LANG_CHANGE.search(m["text"])
            if hit:
                a, b = (LANG_ALIASES.get(x.upper(), x.upper()) for x in hit.groups())
                if a != b:
                    out.append({"idx": m["idx"], "message_id": m["id"], "from": a, "to": b, "source": "system"})
            continue
        if m["speaker"] not in ("client", "operator") or len(re.sub(r"\W", "", m["text"])) < 12:
            continue
        g = guess_language(m["text"])
        if g and prev and g != prev:
            out.append({"idx": m["idx"], "message_id": m["id"], "from": prev, "to": g, "source": "text",
                        "speaker": m["speaker"]})
        prev = g or prev
    return out


def normalize_records(records: list[dict], group_id: str = "DEV", return_warnings: bool = False):
    seen: set[str] = set()
    out: list[dict] = []
    warnings: list[str] = []
    for pos, rec in enumerate(records):
        conv = normalize_record(rec, group_id, pos)
        if conv["id"] in seen:
            warnings.append(f"Повторный conversation_id {conv['id']}: оставлена первая запись.")
            continue
        seen.add(conv["id"])
        out.append(conv)
    return (out, warnings) if return_warnings else out


def public_view(conv: dict) -> dict:
    """Conversation for the browser: the signed organizers' link is replaced by our audio endpoint."""
    view = {k: v for k, v in conv.items()}
    view["audio_url"] = f"/api/audio/{quote(conv['id'], safe='')}" if conv.get("audio_url") else None
    return view


# --- sources --------------------------------------------------------------------------------------------------

def _group_from_name(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"_records$", "", stem, flags=re.I)
    return stem.upper() or "DEV"


def ingest_file(path: str | Path, group_id: str | None = None) -> list[dict]:
    path = Path(path)
    records, gid = parse_payload(json.loads(path.read_text(encoding="utf-8")))
    return normalize_records(records, group_id or gid or _group_from_name(path))


def data_api_base() -> str:
    env = ROOT / ".env"
    if not os.environ.get("CQA_DATA_API") and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ.get("CQA_DATA_API", "")


def with_group_id(base: str, group_id: str) -> str:
    """Add group_id to the signed link, keeping its own query (api-version, signature). ``params=`` would drop it."""
    parts = urlsplit(base)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "group_id"]
    query.append(("group_id", group_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_payload(group_id: str, base_url: str | None = None, timeout: float = 120.0) -> object:
    """GET the organizers' API for group_id (the link from the Student Guide, kept in .env as CQA_DATA_API)."""
    import httpx

    base = base_url or data_api_base()
    if not base:
        raise IngestError("CQA_DATA_API is not set (.env): the organizers' GET link without group_id")
    try:
        resp = httpx.get(with_group_id(base, group_id), timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:  # never echo the signed URL
        raise IngestError(f"Organizers' API is unreachable: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        raise IngestError(f"Organizers' API answered HTTP {resp.status_code} for group_id={group_id}")
    try:
        return resp.json()
    except ValueError:
        raise IngestError("Organizers' API answered with non-JSON content") from None


def raw_path(group_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", group_id).lower() or "dataset"
    return ROOT / "data/raw" / f"{safe}_records.json"


def save_records(records: list[dict], group_id: str) -> Path:
    """Raw records as a plain list (the format of data/raw/dev_records.json), written atomically. Ignored by Git."""
    out = raw_path(group_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return out


def fetch_group(group_id: str, save: bool = True, base_url: str | None = None) -> list[dict]:
    """Fetch + normalize. The group id is the one we asked for (the organizers' answer is checked against it)."""
    payload = fetch_payload(group_id, base_url)
    records, gid = parse_payload(payload)
    if save:
        save_records(records, group_id)
    convs = normalize_records(records, group_id)
    if gid and gid != group_id:
        for conv in convs:
            conv["meta"]["warnings"].append(f"API вернул dataset_group_id={gid} на запрос {group_id}.")
    return convs
