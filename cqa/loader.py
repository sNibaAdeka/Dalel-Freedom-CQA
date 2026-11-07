"""Load dialogues from whatever format the organizers ship and normalise them.

Dialogue = {"id", "channel": chat|call, "lang", "turns": [{"i", "speaker": OPERATOR|CLIENT|BOT|SYSTEM, "text", "ts"}], "meta", "audio"}
Supported: .json (one dialogue, list of dialogues, {"dialogues": [...]}, chat exports), .jsonl, .csv/.xlsx
(one row per message with a dialogue id column, or one row per dialogue with a text column), .txt/.md transcripts
("Оператор: ...", "[00:12] Client: ..."). Audio files with the same stem are attached as `audio`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

OPERATOR_WORDS = ("operator", "agent", "manager", "support", "оператор", "менеджер", "специалист", "сотрудник", "консультант", "employee", "staff", "admin", "freedom", "маман", "қызметкер", "speaker_0", "spk_0", "speaker 0")
CLIENT_WORDS = ("client", "customer", "user", "клиент", "пользователь", "абонент", "заявитель", "клиент", "тұтынушы", "speaker_1", "spk_1", "speaker 1", "caller")
BOT_WORDS = ("bot", "бот", "auto", "system", "система")
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".webm"}
TEXT_KEYS = ("text", "message", "content", "body", "msg", "utterance", "transcript", "текст", "сообщение")
SPEAKER_KEYS = ("speaker", "role", "author", "sender", "from", "who", "participant", "user_type", "роль", "автор")
TS_KEYS = ("ts", "timestamp", "time", "created_at", "date", "datetime", "start", "start_time", "время")
ID_KEYS = ("dialogue_id", "dialog_id", "conversation_id", "chat_id", "call_id", "session_id", "ticket_id", "id")


def norm_speaker(s) -> str:
    x = str(s or "").strip().lower()
    if any(w in x for w in BOT_WORDS) and not any(w in x for w in OPERATOR_WORDS[:4]):
        return "BOT"
    if any(w in x for w in OPERATOR_WORDS):
        return "OPERATOR"
    if any(w in x for w in CLIENT_WORDS):
        return "CLIENT"
    if x in ("a", "0", "agent_0", "speaker_a"):
        return "OPERATOR"
    if x in ("b", "1", "speaker_b"):
        return "CLIENT"
    return str(s).strip().upper() or "UNKNOWN"


def detect_lang(text: str) -> str:
    t = text.lower()
    if re.search(r"[әғқңөұүһі]", t):
        return "kk"
    cyr = len(re.findall(r"[а-яё]", t))
    lat = len(re.findall(r"[a-z]", t))
    if cyr == 0 and lat == 0:
        return "unknown"
    if cyr and lat and min(cyr, lat) / max(cyr, lat) > 0.3:
        return "mixed"
    return "ru" if cyr > lat else "en"


def _pick(d: dict, keys) -> object:
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def _turns_from_messages(msgs: list) -> list[dict]:
    turns = []
    for m in msgs:
        if isinstance(m, str):
            turns.extend(_turns_from_text(m))
            continue
        if not isinstance(m, dict):
            continue
        text = _pick(m, TEXT_KEYS)
        if text is None:
            continue
        turns.append({"speaker": norm_speaker(_pick(m, SPEAKER_KEYS)), "text": str(text).strip(), "ts": _pick(m, TS_KEYS)})
    return turns


LINE_RE = re.compile(r"^\s*(?:\[?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\]?\s*[-–]?\s*)?(?P<spk>[^:\n]{1,40}?)\s*:\s*(?P<text>.+)$")


def _turns_from_text(text: str) -> list[dict]:
    turns = []
    for line in str(text).splitlines():
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        if m and len(m.group("spk").split()) <= 4:
            turns.append({"speaker": norm_speaker(m.group("spk")), "text": m.group("text").strip(), "ts": m.group("ts")})
        elif turns:
            turns[-1]["text"] += " " + line.strip()
        else:
            turns.append({"speaker": "UNKNOWN", "text": line.strip(), "ts": None})
    return turns


def _finalize(did: str, turns: list[dict], channel: str | None = None, meta: dict | None = None) -> dict:
    for i, t in enumerate(turns, start=1):
        t["i"] = i
        if t.get("ts") is not None and not isinstance(t["ts"], (str, int, float)):
            t["ts"] = str(t["ts"])
    full = " ".join(t["text"] for t in turns)
    return {"id": str(did), "channel": channel or "chat", "lang": detect_lang(full), "turns": turns, "meta": meta or {}, "audio": None}


def _from_obj(obj, default_id: str, channel: str | None) -> list[dict]:
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and _pick(x, TEXT_KEYS) is not None and not any(k in x for k in ("messages", "turns", "dialog", "dialogue")) for x in obj[:5]):
            # list of messages, maybe with dialogue ids
            if any(_pick(x, ID_KEYS[:-1]) is not None for x in obj[:5]):
                df = pd.DataFrame(obj)
                return _from_frame(df, default_id, channel)
            return [_finalize(default_id, _turns_from_messages(obj), channel)]
        out = []
        for k, x in enumerate(obj):
            out += _from_obj(x, f"{default_id}_{k}", channel)
        return out
    if isinstance(obj, dict):
        for key in ("dialogues", "dialogs", "conversations", "chats", "calls", "data", "items"):
            if key in obj and isinstance(obj[key], list):
                return _from_obj(obj[key], default_id, channel)
        did = _pick(obj, ID_KEYS) or default_id
        ch = channel or ("call" if any(k in str(list(obj.keys())).lower() for k in ("call", "audio", "duration")) else None)
        ch = str(obj.get("channel") or obj.get("type") or ch or "chat").lower()
        ch = "call" if any(w in ch for w in ("call", "звон", "voice", "phone")) else "chat"
        meta = {k: v for k, v in obj.items() if isinstance(v, (str, int, float, bool)) and str(k).lower() not in TEXT_KEYS}
        for key in ("messages", "turns", "dialog", "dialogue", "conversation", "utterances", "segments", "replicas", "chat", "transcript", "text"):
            if key in obj:
                v = obj[key]
                turns = _turns_from_messages(v) if isinstance(v, list) else _turns_from_text(v)
                return [_finalize(did, turns, ch, meta)]
    return []


def _from_frame(df: pd.DataFrame, default_id: str, channel: str | None) -> list[dict]:
    cols = {c.lower(): c for c in df.columns}
    idc = next((cols[k] for k in ID_KEYS if k in cols), None)
    txt = next((cols[k] for k in TEXT_KEYS if k in cols), None)
    spk = next((cols[k] for k in SPEAKER_KEYS if k in cols), None)
    tsc = next((cols[k] for k in TS_KEYS if k in cols), None)
    if txt is None:
        return []
    out = []
    groups = df.groupby(idc, sort=False) if idc else [(default_id, df)]
    for did, g in groups:
        if spk is None and len(g) == 1:  # one row per dialogue with whole text
            turns = _turns_from_text(g[txt].iloc[0])
        else:
            turns = [{"speaker": norm_speaker(r[spk]) if spk else "UNKNOWN", "text": str(r[txt]).strip(), "ts": (str(r[tsc]) if tsc and pd.notna(r[tsc]) else None)} for _, r in g.iterrows() if pd.notna(r[txt])]
        out.append(_finalize(did, turns, channel))
    return out


def load_file(path: str | Path, channel: str | None = None) -> list[dict]:
    p = Path(path)
    ext = p.suffix.lower()
    if channel is None:
        low = str(p).lower()
        channel = "call" if any(w in low for w in ("call", "звон", "voice", "audio")) else None
    if ext == ".json":
        return _from_obj(json.loads(p.read_text(encoding="utf-8")), p.stem, channel)
    if ext in (".jsonl", ".ndjson"):
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        return _from_obj(rows, p.stem, channel)
    if ext in (".csv", ".tsv"):
        df = pd.read_csv(p, sep=None, engine="python")
        return _from_frame(df, p.stem, channel)
    if ext in (".xlsx", ".xls"):
        return _from_frame(pd.read_excel(p), p.stem, channel)
    if ext in (".txt", ".md", ".srt", ".vtt"):
        return [_finalize(p.stem, _turns_from_text(p.read_text(encoding="utf-8", errors="replace")), channel or "chat")]
    return []


def load_dir(folder: str | Path, skip: tuple[str, ...] = ("checklist", "чек", "guide", "gold", "label", "score", "оценк")) -> list[dict]:
    """Load every dialogue under folder (recursive). Files whose name contains a skip word are ignored."""
    folder = Path(folder)
    dialogues, audio = [], {}
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() in AUDIO_EXT:
            audio[p.stem] = str(p)
            continue
        if any(w in p.name.lower() for w in skip):
            continue
        try:
            dialogues += load_file(p)
        except Exception as e:  # keep going, report
            print(f"[loader] skip {p}: {e}")
    seen = set()
    out = []
    for d in dialogues:
        if not d["turns"]:
            continue
        if d["id"] in seen:
            d["id"] = f"{d['id']}_{len(seen)}"
        seen.add(d["id"])
        if d["id"] in audio:
            d["audio"] = audio[d["id"]]
            d["channel"] = "call"
        out.append(d)
    return out


def render(d: dict, max_chars: int = 400_000) -> str:
    """Numbered transcript for the LLM prompt."""
    lines = []
    for t in d["turns"]:
        ts = f" ({t['ts']})" if t.get("ts") else ""
        lines.append(f"[{t['i']}] {t['speaker']}{ts}: {t['text']}")
    s = "\n".join(lines)
    return s[:max_chars]
