"""«Речь оператора»: informational speech metrics of calls (synthetic data only, no audio, no network)."""
from __future__ import annotations

from cqa import audio, ingest, speech


def _iso(sec: float) -> str:
    whole = int(sec)
    return f"2026-11-03T10:{whole // 60:02d}:{whole % 60:02d}.{int(round((sec - whole) * 10))}000000+00:00"


OP_20 = " ".join(["слово"] * 20)    # 20 words, 119 chars
OP_10 = " ".join(["слово"] * 10)    # 10 words, 59 chars


def _call(turns, cid="s-call-001"):
    return ingest.normalize_record({"conversation_id": cid, "channel": "voice call", "language": "RU", "audio_url": None,
                                    "transcript": [{"timestamp": _iso(t), "speaker": s, "text": x} for t, s, x in turns]})


def test_tempo_and_pauses_from_turn_timestamps():
    conv = _call([
        (0, "system", "Запись и транскрибирование запущены."),
        (5, "bot", "Учебная линия."),
        (20, "client", "Ожидаю оператора."),              # before the operator: not counted as a pause
        (30, "operator", OP_20),                          # gap 10 s -> 10 s of speech for 20 words
        (35, "client", "Да."),                            # gap 5 s - est 0.8 s -> 4.2 s pause
        (40, "operator", OP_10),                          # gap 5 s -> 5 s for 10 words; pause 5 - 59/12 = 0.1
        (60, "client", "Хорошо, спасибо."),               # gap 20 s - 1.3 s -> 18.7 s, the longest
        (61, "operator", OP_10),                          # gap 1 s for 10 words = 600 wpm -> bunched, skipped
    ])
    m = speech.speech_metrics(conv)
    assert m["informational"] is True and m["affects_score"] is False
    assert m["operator_words"] == 30 and m["operator_turns_timed"] == 2
    assert m["tempo_wpm"] == 120                          # 30 words / 15 s
    assert m["pauses_over_3s"] == 2
    assert m["longest_pause_sec"] == 18.7 and m["longest_pause_before"] == "s-call-001:6"
    assert m["source"] == "transcript" and m["silence_share"] is None
    assert "на балл не влияет" in m["note"] and "транскрипт" in m["note"]


def test_turn_duration_is_capped_and_hold_is_not_a_pause():
    conv = _call([
        (0, "system", "Запись и транскрибирование запущены."),
        (100, "operator", OP_20),                         # gap 100 s, capped at 20 words / 60 wpm = 20 s
        (110, "system", "Клиент поставлен на удержание."),
        (170, "system", "Клиент снят с удержания."),
        (175, "client", "Слушаю."),                       # spans the hold: not a pause
    ])
    m = speech.speech_metrics(conv)
    assert m["tempo_wpm"] == 60
    assert m["pauses_over_3s"] == 0


def test_chat_has_no_metrics_and_short_call_has_no_tempo():
    chat = ingest.normalize_record({"conversation_id": "s-chat", "channel": "agent chat", "language": "RU",
                                    "transcript": [{"timestamp": "00:00", "speaker": "operator", "text": OP_20}]})
    assert speech.speech_metrics(chat) is None and speech.cached_speech_metrics(chat) is None
    short = _call([(0, "client", "Алло."), (3, "operator", "Здравствуйте.")])
    assert speech.speech_metrics(short)["tempo_wpm"] is None


def test_audio_profile_from_calibration_cache_and_memo():
    conv = _call([(0, "client", "Алло."), (10, "operator", OP_20), (16, "client", "Понятно.")], cid="s-call-002")
    stats = audio.silence_stats([(0.0, 2.0), (10.0, 14.5), (55.0, None)], 60.0)
    assert stats == {"duration_sec": 60.0, "silence_sec": 11.5, "silence_share": 0.192, "pauses_over_3s": 2,
                     "longest_silence_sec": 5.0}
    assert audio.silence_stats([], None) is None
    cal = {"lead_sec": 4.3, "method": "vad", "computed_at": "2026-11-08T10:00:00Z", "silence": stats}
    m = speech.cached_speech_metrics(conv, cal)
    assert m["source"] == "audio+transcript" and m["silence_share"] == 0.192
    assert m["note"] == "по записи и транскрипту, на балл не влияет"
    assert speech.cached_speech_metrics(conv, cal) == m
    assert speech.cached_speech_metrics(conv, {"lead_sec": 4.3})["source"] == "transcript"  # old cache entry
