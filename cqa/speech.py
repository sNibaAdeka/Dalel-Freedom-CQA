"""«Речь оператора»: informational speech metrics of a call. Never part of the score.

Organizers' brief: transcripts are given; diction and intonation need the sound. We do not transcribe or judge voice
quality, we only measure what the transcript timing and (when already processed) the recording can show:

* tempo -- operator words per minute. Turn timestamps mark the END of an utterance (measured in ``cqa.audio``), so an
  operator turn lasts at most the gap since the previous timed event; the gap is capped (``MAX_TURN_SEC``, and no
  slower than ``MIN_WPM``) so a pause before the turn does not dilute the tempo; turns bunched into one second are
  skipped as unmeasurable.
* pauses -- silence between consecutive speech turns from the first operator turn on: gap minus the estimated length
  of the later utterance (``len(text) / CHARS_PER_SEC``, as in ``cqa.audio``). Gaps across a hold are not pauses.
  Uses ``rec_sec`` (recording clock, recording pauses removed).
* silence share -- from ffmpeg ``silencedetect`` stats stored by ``cqa.audio.calibrate`` in the calibration cache.
  Never downloads anything here: without a cached profile the block is transcript-only and says so.

Chats have no metrics (``None``).
"""
from __future__ import annotations

import re
import threading

from cqa.audio import CHARS_PER_SEC, PAUSE_SEC, silence_stats  # noqa: F401 (re-exported)

MAX_TURN_SEC = 30.0       # cap of one operator turn's duration
MIN_WPM = 60.0            # slower than this the rest of the gap is a pause, not speech
MAX_WPM = 360.0           # faster than this the timestamps are bunched: turn skipped
MIN_WORDS = 10            # below this (or MIN_SECONDS) the tempo is not reported
MIN_SECONDS = 5.0
SPEECH = ("client", "operator", "bot")
NOTE_AUDIO = "по записи и транскрипту, на балл не влияет"
NOTE_TRANSCRIPT = "по транскрипту (запись ещё не обработана), на балл не влияет"

_WORD = re.compile(r"\w+(?:[-'’]\w+)*", re.UNICODE)
_cache: dict[tuple, dict] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 512


def word_count(text: str | None) -> int:
    return len(_WORD.findall(text or ""))


def _clock(m: dict, use_rec: bool) -> float | None:
    value = m.get("rec_sec") if use_rec else m.get("t_sec")
    return float(value) if isinstance(value, (int, float)) else None


def _spans_hold(prev: dict, cur: dict, holds: list) -> bool:
    a, b = prev.get("t_sec"), cur.get("t_sec")
    if a is None or b is None:
        return False
    return any(s < b and e > a for s, e in holds)


def transcript_metrics(conv: dict) -> dict:
    """Tempo and pauses from the turn timestamps of a call."""
    messages = conv.get("messages") or []
    use_rec = any(m.get("rec_sec") is not None for m in messages)
    holds = [h for h in (conv.get("meta") or {}).get("hold_intervals") or [] if isinstance(h, (list, tuple)) and len(h) == 2]
    timed = [m for m in messages if _clock(m, use_rec) is not None]
    first_op = next((i for i, m in enumerate(timed) if m.get("speaker") == "operator"), None)

    words = seconds = turns = 0
    pauses: list[tuple[float, str]] = []
    prev_any = prev_speech = None
    for i, m in enumerate(timed):
        t = _clock(m, use_rec)
        is_speech = (m.get("speaker") in SPEECH and (m.get("text") or "").strip()
                     and not m.get("dtmf") and not m.get("on_hold"))
        if m.get("speaker") == "operator" and is_speech and prev_any is not None:
            w = word_count(m.get("text"))
            gap = t - _clock(prev_any, use_rec)
            if w and gap > 0:
                dur = min(gap, MAX_TURN_SEC, w * 60.0 / MIN_WPM)
                if w * 60.0 / dur <= MAX_WPM:
                    words, seconds, turns = words + w, seconds + dur, turns + 1
        if is_speech and first_op is not None and i >= first_op:
            if prev_speech is not None and not _spans_hold(prev_speech, m, holds):
                gap = t - _clock(prev_speech, use_rec)
                est = max(0.8, len(m.get("text") or "") / CHARS_PER_SEC)
                pause = round(max(0.0, gap - est), 1)
                if pause > 0:
                    pauses.append((pause, m["id"]))
            prev_speech = m
        prev_any = m
    tempo = round(words * 60.0 / seconds) if words >= MIN_WORDS and seconds >= MIN_SECONDS else None
    longest = max(pauses, default=None)
    return {"tempo_wpm": tempo, "operator_words": words, "operator_turns_timed": turns,
            "operator_seconds_est": round(seconds, 1),
            "pauses_over_3s": sum(1 for p, _ in pauses if p > PAUSE_SEC), "pause_threshold_sec": PAUSE_SEC,
            "longest_pause_sec": longest[0] if longest else None,
            "longest_pause_before": longest[1] if longest else None,
            "clock": "rec_sec" if use_rec else "t_sec"}


def speech_metrics(conv: dict, calibration: dict | None = None) -> dict | None:
    """Informational block for the call card; ``None`` for chats. ``calibration``: cached ``cqa.audio`` entry."""
    if conv.get("channel") != "call":
        return None
    out = transcript_metrics(conv)
    profile = (calibration or {}).get("silence") if isinstance(calibration, dict) else None
    profile = profile if isinstance(profile, dict) and isinstance(profile.get("silence_share"), (int, float)) else None
    out.update({"informational": True, "affects_score": False, "estimate": True,
                "source": "audio+transcript" if profile else "transcript",
                "silence_share": profile["silence_share"] if profile else None, "audio": profile,
                "note": NOTE_AUDIO if profile else NOTE_TRANSCRIPT})
    return out


def cached_speech_metrics(conv: dict, calibration: dict | None = None) -> dict | None:
    """``speech_metrics`` memoized per conversation content and calibration profile (computed for opened calls only)."""
    if conv.get("channel") != "call":
        return None
    meta = conv.get("meta") or {}
    msgs = conv.get("messages") or []
    profile = (calibration or {}).get("silence") if isinstance(calibration, dict) else None
    key = (conv.get("id"), meta.get("content_hash") or (len(msgs), msgs[-1].get("ts_raw") if msgs else None),
           (calibration or {}).get("computed_at") if profile else None)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return dict(hit)
    value = speech_metrics(conv, calibration)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = value
    return dict(value)
