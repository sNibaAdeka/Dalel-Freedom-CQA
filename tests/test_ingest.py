"""Ingest of the organizers' official format: envelope/list, roles, ids, t_sec, rec_sec, recording pauses, meta.

Synthetic records only, except the DEV checks at the bottom (skipped when data/raw is absent; nothing is printed).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cqa import ingest

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "data/raw/dev_records.json"


def chat(cid="t-ru-001", lang="RU", turns=None):
    turns = turns or [
        ("00:00", "client", "Где посмотреть тариф?"),
        ("00:02", "bot", "Здравствуйте! Я AI-ассистент."),
        ("00:40", "system", "Эскалация на оператора."),
        ("01:05", "Operator", "Здравствуйте! Меня зовут Алия."),
        ("01:30", "CLIENT", "Спасибо"),
    ]
    return {"conversation_id": cid, "channel": "agent chat", "language": lang,
            "transcript": [{"timestamp": t, "speaker": s, "text": x} for t, s, x in turns], "audio_url": None}


def iso(sec: float) -> str:
    # 7 fractional digits and an explicit offset, like the organizers' calls
    whole = int(sec)
    frac = int(round((sec - whole) * 10_000_000))
    m, s = divmod(whole, 60)
    return f"2026-11-03T13:{m:02d}:{s:02d}.{frac:07d}+00:00"


def call(events, cid="t-call-001"):
    return {"conversation_id": cid, "channel": "voice call", "language": "RU",
            "transcript": [{"timestamp": iso(t), "speaker": s, "text": x} for t, s, x in events],
            "audio_url": "https://example.invalid/powerautomate/automations/direct/workflows/x/triggers/manual/paths/invoke?api-version=1&token=SECRET123"}


# --- payload formats -------------------------------------------------------------------------------------------

def test_envelope_and_group_id():
    payload = {"error": "", "isSuccess": True,
               "data": {"dataset_group_id": "CTRL-7", "total_records": 1, "records": [chat()]}}
    records, gid = ingest.parse_payload(payload)
    assert gid == "CTRL-7" and len(records) == 1
    convs = ingest.normalize_records(records, gid)
    assert convs[0]["group_id"] == "CTRL-7"


def test_plain_list_and_records_dict():
    assert ingest.parse_payload([chat()]) == ([chat()], None)
    assert ingest.parse_payload({"records": [chat()]})[0] == [chat()]


def test_unsuccessful_envelope_raises():
    with pytest.raises(ingest.IngestError, match="boom"):
        ingest.parse_payload({"error": "boom", "isSuccess": False, "data": None})


def test_empty_payload_raises():
    with pytest.raises(ingest.IngestError):
        ingest.parse_payload({"isSuccess": True, "data": {"records": []}})


# --- chats ----------------------------------------------------------------------------------------------------

def test_chat_roles_ids_and_times():
    conv = ingest.normalize_record(chat(), "DEV")
    assert conv["id"] == "t-ru-001" and conv["channel"] == "chat" and conv["language"] == "RU"
    assert conv["group_id"] == "DEV" and conv["audio_url"] is None
    msgs = conv["messages"]
    assert [m["id"] for m in msgs] == [f"t-ru-001:{i}" for i in range(5)]
    assert [m["idx"] for m in msgs] == list(range(5))
    assert [m["speaker"] for m in msgs] == ["client", "bot", "system", "operator", "client"]
    assert [m["ts_raw"] for m in msgs] == ["00:00", "00:02", "00:40", "01:05", "01:30"]
    assert [m["t_sec"] for m in msgs] == [0, 2, 40, 65, 90]
    assert all(m["rec_sec"] is None for m in msgs)
    assert msgs[3]["text"] == "Здравствуйте! Меня зовут Алия."


def test_chat_meta_handoff_and_wait():
    meta = ingest.normalize_record(chat())["meta"]
    assert meta["handoff_idx"] == 3            # first operator message
    assert meta["handoff_event_idx"] == 2      # «Эскалация на оператора.»
    assert meta["wait_sec"] == 25              # 01:05 - 00:40
    assert meta["recording_gaps"] == [] and meta["lang_switches"] == []


def test_hours_and_fractional_chat_timestamps():
    conv = ingest.normalize_record(chat(turns=[("1:00:05", "client", "a"), ("61:10", "operator", "b"),
                                               ("00:03.5", "client", "c"), ("", "client", "d")]))
    assert [m["t_sec"] for m in conv["messages"]] == [3605, 3670, 3.5, None]


def test_speaker_aliases_and_unknown_role():
    conv = ingest.normalize_record(chat(turns=[("00:00", "Customer", "a"), ("00:01", "agent", "b"),
                                               ("00:02", "IVR", "c"), ("00:03", "robot?", "d")]))
    assert [m["speaker"] for m in conv["messages"]] == ["client", "operator", "bot", "system"]
    assert conv["messages"][3]["speaker_raw"] == "robot?"


def test_language_normalization_and_detection():
    assert ingest.normalize_record(chat(lang="kk"))["language"] == "KZ"
    assert ingest.normalize_record(chat(lang="en"))["language"] == "EN"
    kz = chat(lang=None, turns=[("00:00", "client", "Сәлеметсіз бе, шотымды қалай ашамын?")])
    assert ingest.normalize_record(kz)["language"] == "KZ"
    en = chat(lang="", turns=[("00:00", "client", "Hello, how do I open an account?")])
    assert ingest.normalize_record(en)["language"] == "EN"


def test_messages_are_never_merged():
    rec = chat()
    conv = ingest.normalize_record(rec)
    assert len(conv["messages"]) == len(rec["transcript"])


def test_duplicate_ids_are_kept_once_with_warning():
    convs, warnings = ingest.normalize_records([chat(), chat()], "DEV", return_warnings=True)
    assert len(convs) == 1 and any("t-ru-001" in w for w in warnings)


def test_missing_id_gets_stable_fallback():
    rec = chat()
    rec.pop("conversation_id")
    conv = ingest.normalize_record(rec, "G1", position=4)
    assert conv["id"] == "G1-005"


# --- calls: t_sec, recording start, pauses --------------------------------------------------------------------

def test_call_channel_and_iso_t_sec():
    conv = ingest.normalize_record(call([(0, "system", "Агент присоединился к разговору с клиентом."),
                                         (2.25, "operator", "Здравствуйте")]))
    assert conv["channel"] == "call"
    assert [m["t_sec"] for m in conv["messages"]] == [0, 2.25]
    # no recording event at all -> recording starts at the first event
    assert [m["rec_sec"] for m in conv["messages"]] == [0, 2.25]


def test_recording_starts_later_than_first_message():
    # like p4-ru-002: the recording starts ~30 s after the first event
    conv = ingest.normalize_record(call([
        (0, "system", "Агент присоединился к разговору с клиентом."),
        (5, "operator", "Алло, здравствуйте"),
        (30, "system", "Запись и транскрибирование запущены."),
        (41, "operator", "Чем могу помочь?"),
    ]))
    msgs = conv["messages"]
    assert conv["meta"]["recording_start_sec"] == 30
    assert msgs[0]["rec_sec"] is None and msgs[1]["rec_sec"] is None   # not in the recording
    assert msgs[2]["rec_sec"] == 0
    assert msgs[3]["rec_sec"] == 11


def test_pause_resume_is_subtracted():
    conv = ingest.normalize_record(call([
        (0, "system", "Запись и транскрибирование запущены."),
        (9, "bot", "Учебная линия поддержки, нажмите два."),
        (47, "system", "Запись и транскрибирование приостановлены."),
        (58, "system", "Агент присоединился к разговору с клиентом."),
        (60.5, "system", "Запись и транскрибирование возобновлены."),
        (64, "operator", "Здравствуйте, меня зовут Дана."),
    ]))
    msgs, meta = conv["messages"], conv["meta"]
    assert meta["recording_gaps"] == [[47, 60.5]]
    assert msgs[1]["rec_sec"] == 9
    assert msgs[2]["rec_sec"] is None and msgs[3]["rec_sec"] is None    # inside the pause
    assert msgs[4]["rec_sec"] == 47                                      # resume point
    assert msgs[5]["rec_sec"] == 50.5                                    # 64 - 13.5
    assert meta["recording_duration_sec"] == 50.5


def test_kazakh_and_english_recording_events():
    kz = ingest.normalize_record(call([
        (0, "system", "Synthetic notice: dialog language changed from kk-KZ to ru-RU."),
        (3, "system", "Жазу және транскрипция басталды."),
        (10, "system", "Жазу және транскрипция кідіртілді."),
        (20, "system", "Жазу және транскрипция жалғастырылды."),
        (25, "operator", "Сәлеметсіз бе"),
    ]))
    assert kz["meta"]["recording_start_sec"] == 3
    assert kz["meta"]["recording_gaps"] == [[10, 20]]
    assert kz["messages"][4]["rec_sec"] == 12
    assert kz["meta"]["lang_switches"][0]["from"] == "KZ" and kz["meta"]["lang_switches"][0]["to"] == "RU"
    en = ingest.normalize_record(call([
        (0, "system", "Recording and transcription started."),
        (4, "system", "Recording and transcription paused."),
        (9, "system", "Recording and transcription resumed."),
        (10, "operator", "Hello"),
    ]))
    assert en["meta"]["recording_gaps"] == [[4, 9]] and en["messages"][3]["rec_sec"] == 5


def test_language_update_event_is_not_a_recording_event():
    conv = ingest.normalize_record(call([
        (0, "system", "Synthetic notice: transcription languages have been updated."),
        (2, "operator", "Здравствуйте"),
    ]))
    assert conv["meta"]["recording_gaps"] == [] and conv["meta"]["recording_start_sec"] == 0


def test_unclosed_pause_leaves_later_speech_unplayable():
    conv = ingest.normalize_record(call([
        (0, "system", "Запись и транскрибирование запущены."),
        (10, "operator", "Здравствуйте"),
        (20, "system", "Запись и транскрибирование приостановлены."),
        (30, "operator", "Вы меня слышите?"),
    ]))
    assert conv["messages"][1]["rec_sec"] == 10
    assert conv["messages"][3]["rec_sec"] is None
    assert conv["meta"]["recording_gaps"] == [[20, 30]]
    assert conv["meta"]["warnings"]


def test_channel_inferred_from_iso_and_audio():
    rec = call([(0, "operator", "Алло")])
    rec["channel"] = ""
    assert ingest.normalize_record(rec)["channel"] == "call"


def test_signed_audio_url_kept_internal_only():
    conv = ingest.normalize_record(call([(0, "operator", "Алло")]))
    assert "SECRET123" in conv["audio_url"]          # needed server-side by cqa.audio
    assert "SECRET123" not in json.dumps(ingest.public_view(conv))
    assert ingest.public_view(conv)["audio_url"] == "/api/audio/t-call-001"


def test_load_file_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"isSuccess": True, "data": {"dataset_group_id": "Z", "records": [chat()]}}), encoding="utf-8")
    convs = ingest.ingest_file(p)
    assert convs[0]["group_id"] == "Z"
    assert ingest.ingest_file(p, group_id="OVERRIDE")[0]["group_id"] == "OVERRIDE"


# --- real DEV data (local only) -------------------------------------------------------------------------------

dev = pytest.mark.skipif(not DEV.exists(), reason="organizers' DEV data is not available")
MP3_LEN = {"p4-ru-001": 312, "p4-ru-002": 620, "p4-ru-003": 393, "p4-ru-004": 424, "p4-ru-005": 345}


@dev
def test_dev_all_65():
    convs = ingest.ingest_file(DEV, group_id="DEV")
    assert len(convs) == 65
    calls = [c for c in convs if c["channel"] == "call"]
    assert len(calls) == 5 and {c["id"] for c in calls} == set(MP3_LEN)
    assert sum(c["language"] == "RU" for c in convs) == 35
    assert sum(c["language"] == "KZ" for c in convs) == 15
    assert sum(c["language"] == "EN" for c in convs) == 15
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    raw = raw if isinstance(raw, list) else raw["data"]["records"]
    by_id = {r["conversation_id"]: r for r in raw}
    for c in convs:
        src = by_id[c["id"]]["transcript"]
        assert len(c["messages"]) == len(src)
        for i, m in enumerate(c["messages"]):
            assert m["id"] == f"{c['id']}:{i}" and m["speaker"] in ("client", "operator", "bot", "system")
            assert m["ts_raw"] == src[i]["timestamp"] and m["text"] == src[i]["text"]
            assert m["t_sec"] is not None and m["t_sec"] >= 0


@dev
def test_dev_calls_recording_offsets_match_mp3_length():
    convs = {c["id"]: c for c in ingest.ingest_file(DEV, group_id="DEV") if c["channel"] == "call"}
    p2 = convs["p4-ru-002"]
    assert 29 < p2["meta"]["recording_start_sec"] < 31          # recording starts 30 s after the first event
    assert all(m["rec_sec"] is None for m in p2["messages"][:4])
    for cid, mp3 in MP3_LEN.items():
        c = convs[cid]
        est = c["meta"]["recording_duration_sec"]
        assert abs(est + 6 - mp3) <= 7, (cid, est)
        ops = [m for m in c["messages"] if m["speaker"] == "operator"]
        start = c["meta"]["recording_start_sec"]
        assert ops and all(m["rec_sec"] is not None for m in ops if m["t_sec"] >= start)
        assert sum(m["rec_sec"] is not None for m in ops) >= 0.9 * len(ops)
        recs = [m["rec_sec"] for m in c["messages"] if m["rec_sec"] is not None]
        assert recs == sorted(recs)


def test_group_id_is_added_without_dropping_the_signed_query():
    url = ingest.with_group_id("https://x.invalid/invoke?api-version=1&sp=%2Fa&token=T0K&group_id=OLD", "CTRL 7")
    assert url.startswith("https://x.invalid/invoke?api-version=1&sp=%2Fa&token=T0K&")
    assert url.endswith("group_id=CTRL+7") and "OLD" not in url


def test_output_validates_as_shared_models():
    from cqa.models import Conversation

    for rec in (chat(), call([(0, "system", "Запись и транскрибирование запущены."), (3, "operator", "Алло")])):
        conv = ingest.normalize_record(rec, "DEV")
        model = Conversation.model_validate(conv)
        assert model.messages[0].id == conv["messages"][0]["id"]
    if DEV.exists():
        for conv in ingest.ingest_file(DEV, group_id="DEV"):
            Conversation.model_validate(conv)


# --- call traps (dev_profile.md §5), synthetic re-creations only ---------------------------------------------------

def test_utc_seven_fraction_digits_keep_sub_second_precision():
    conv = ingest.normalize_record(call([(0.1900670, "system", "Запись и транскрибирование запущены."),
                                         (0.2744711, "operator", "Алло")]))
    assert conv["messages"][0]["ts_raw"].endswith(".1900670+00:00")
    assert conv["messages"][1]["t_sec"] == pytest.approx(0.084, abs=0.001)


def test_pause_of_13_to_19_seconds_is_cut_from_offsets():
    for pause in (13.2, 19.2):
        conv = ingest.normalize_record(call([
            (0, "system", "Запись и транскрибирование запущены."),
            (40, "system", "Клиент переведен от агента на представителя службы поддержки"),
            (41, "system", "Запись и транскрибирование приостановлены."),
            (41 + pause - 3, "system", "Агент присоединился к разговору с клиентом."),
            (41 + pause, "system", "Запись и транскрибирование возобновлены."),
            (41 + pause + 9, "operator", "Здравствуйте, служба поддержки."),
        ]))
        op = conv["messages"][-1]
        assert op["t_sec"] == pytest.approx(50 + pause, abs=1e-3)
        assert op["rec_sec"] == pytest.approx(50, abs=1e-3)       # the pause is not in the audio


def test_operator_speaks_before_the_recording_starts():
    conv = ingest.normalize_record(call([
        (0, "system", "Агент присоединился к разговору с клиентом."),
        (23, "client", "Алло."),
        (27, "operator", "Добрый день, это повторный звонок."),
        (30, "system", "Запись и транскрибирование запущены."),
        (33, "operator", "Продолжим."),
    ]))
    m = conv["messages"]
    assert conv["meta"]["handoff_idx"] == 2
    assert m[2]["rec_sec"] is None and m[4]["rec_sec"] == 3
    assert conv["meta"]["recording_start_sec"] == 30


def test_hold_marks_turns_the_client_did_not_hear():
    conv = ingest.normalize_record(call([
        (0, "system", "Запись и транскрибирование запущены."),
        (5, "operator", "Минуту, уточню у коллег."),
        (6, "system", "Инициирована консультация с агентом."),
        (7, "system", "Клиент находится на удержании."),
        (20, "operator", "Коллега, подскажи по заявке."),          # consultation on hold, diarized as operator
        (40, "operator", "Посторонний разговор."),
        (60, "system", "Клиент больше не находится на удержании."),
        (62, "operator", "Спасибо за ожидание."),
    ]))
    m = conv["messages"]
    assert conv["meta"]["hold_intervals"] == [[7, 60]]
    assert [x.get("on_hold", False) for x in m] == [False, False, False, False, True, True, False, False]


def test_turns_after_the_call_end_are_flagged():
    conv = ingest.normalize_record(call([
        (0, "system", "Запись и транскрибирование запущены."),
        (5, "operator", "Всего доброго."),
        (9, "system", "Клиент завершил разговор."),
        (9.15, "client", "До свидания."),
    ]))
    m = conv["messages"]
    assert conv["meta"]["end_idx"] == 2
    assert m[3].get("after_end") is True and not m[1].get("after_end")
    chat_end = ingest.normalize_record(chat(turns=[("00:00", "client", "a"), ("00:05", "operator", "b"),
                                                   ("00:09", "system", "Оператор закрыл чат.")]))
    assert chat_end["meta"]["end_idx"] == 2


def test_key_presses_in_ivr_are_flagged_not_speech():
    conv = ingest.normalize_record(call([
        (0, "system", "Запись и транскрибирование запущены."),
        (3, "bot", "Учебное меню: нажмите два."),
        (5, "client", "2"),
        (7, "client", "#"),
        (8, "client", "Ноль."),
        (20, "system", "Агент присоединился к разговору с клиентом."),
        (22, "operator", "Сколько лотов?"),
        (25, "client", "Два."),                                     # a spoken answer to the operator
    ]))
    m = conv["messages"]
    assert [x.get("dtmf", False) for x in m] == [False, False, True, True, True, False, False, False]
    assert all(x["speaker"] == "client" for x in m if x.get("dtmf"))   # roles stay as given


# --- id collisions: a control dialogue must never inherit DEV evaluations ---------------------------------------------

def test_content_hash_ignores_nothing_that_matters():
    a = ingest.normalize_record(chat(), "DEV")
    b = ingest.normalize_record(chat(), "DEV")
    other = chat()
    other["transcript"][3]["text"] = "Здравствуйте! Меня зовут Дана."
    c = ingest.normalize_record(other, "DEV")
    assert a["meta"]["content_hash"] == b["meta"]["content_hash"] != c["meta"]["content_hash"]


def test_colliding_ids_from_another_group_or_content_get_their_own_id():
    dev = ingest.normalize_record(chat(), "DEV")
    existing = {dev["id"]: {"group_id": "DEV", "content_hash": dev["meta"]["content_hash"]}}
    same = ingest.normalize_record(chat(), "DEV")
    ctrl = ingest.normalize_record(chat(), "CTRL-1")                       # same text, new control group
    changed_rec = chat()
    changed_rec["transcript"][4]["text"] = "Другой ответ клиента."
    changed = ingest.normalize_record(changed_rec, "DEV")                   # same group, different content
    out = ingest.resolve_id_collisions([same, ctrl, changed], existing)
    assert out[0]["id"] == "t-ru-001"                                      # idempotent re-ingest
    assert out[1]["id"] == "t-ru-001@CTRL-1" and out[1]["meta"]["source_id"] == "t-ru-001"
    assert [m["id"] for m in out[1]["messages"]][:2] == ["t-ru-001@CTRL-1:0", "t-ru-001@CTRL-1:1"]
    assert out[2]["id"].startswith("t-ru-001@DEV-") and out[2]["meta"]["source_id"] == "t-ru-001"
    assert len({c["id"] for c in out}) == 3
