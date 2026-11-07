"""Audio service: fresh src from the organizers' HTML player, expiry cache, playback offsets, VAD lead fit.

No network: the HTML fetcher is a fake. The optional live check runs only with CQA_LIVE_AUDIO=1.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from cqa import audio, ingest

PLAYER = """<!DOCTYPE html><html><head><title>Воспроизведение записи</title></head><body>
<audio id="myAudio" controls style="width:100%;">
    <source src="https://crm.example.invalid/api/filedownload/1/a/b?sp=r&amp;st=2026-11-07T09:29:19Z&amp;se=2026-11-07T10:30:19Z&amp;sv=2022-11-02&amp;rsct=audio%2Fmpeg" type="audio/mpeg">
    Ваш браузер не поддерживает аудиоплеер.
</audio></body></html>"""
NOW = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)


class FakeFetch:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        page = self.pages[min(self.calls - 1, len(self.pages) - 1)]
        if isinstance(page, Exception):
            raise page
        return page


def conv_call():
    rec = {"conversation_id": "c-1", "channel": "voice call", "language": "RU", "audio_url": "https://player.invalid/x",
           "transcript": [
               {"timestamp": "2026-11-03T13:00:00+00:00", "speaker": "system", "text": "Запись и транскрибирование запущены."},
               {"timestamp": "2026-11-03T13:00:10+00:00", "speaker": "bot", "text": "Учебная линия поддержки, для русского языка нажмите два."},
               {"timestamp": "2026-11-03T13:00:20+00:00", "speaker": "system", "text": "Запись и транскрибирование приостановлены."},
               {"timestamp": "2026-11-03T13:00:30+00:00", "speaker": "system", "text": "Запись и транскрибирование возобновлены."},
               {"timestamp": "2026-11-03T13:00:33+00:00", "speaker": "operator", "text": "Здравствуйте!"},
               {"timestamp": "2026-11-03T13:00:40+00:00", "speaker": "client", "text": "Добрый день, хочу узнать про тариф и комиссию за сделки на бирже."},
           ]}
    return ingest.normalize_record(rec, "DEV")


# --- src extraction & expiry --------------------------------------------------------------------------------

def test_extract_src_from_source_tag_and_entities():
    src = audio.extract_src(PLAYER)
    assert src.startswith("https://crm.example.invalid/api/filedownload/")
    assert "&amp;" not in src and "&se=2026-11-07T10:30:19Z" in src


def test_extract_src_from_audio_attribute_and_missing():
    assert audio.extract_src('<audio controls src="https://a.invalid/x.mp3?se=2026-11-07T10%3A30%3A19Z"></audio>') \
        == "https://a.invalid/x.mp3?se=2026-11-07T10%3A30%3A19Z"
    assert audio.extract_src("<html><body>Flow failed</body></html>") is None
    assert audio.extract_src('<audio><source src="javascript:alert(1)"></audio>') is None


def test_expiry_from_se_param():
    assert audio.expires_at(audio.extract_src(PLAYER)) == datetime(2026, 11, 7, 10, 30, 19, tzinfo=timezone.utc)
    assert audio.expires_at("https://a.invalid/x.mp3?se=2026-11-07T10%3A30%3A19Z").minute == 30
    assert audio.expires_at("https://a.invalid/x.mp3") is None


# --- resolver cache ----------------------------------------------------------------------------------------------

def test_resolver_caches_until_expiry_then_refreshes():
    fetch = FakeFetch([PLAYER, PLAYER.replace("10:30:19Z", "11:40:00Z")])
    clock = {"now": NOW}
    r = audio.AudioResolver(fetch_html=fetch, now=lambda: clock["now"])
    first = r.resolve("c-1", "https://player.invalid/x")
    assert first["src"] and first["expires_at"] == "2026-11-07T10:30:19Z"
    r.resolve("c-1", "https://player.invalid/x")
    assert fetch.calls == 1                                   # cached
    clock["now"] = NOW + timedelta(minutes=59)                # < 2 min before se -> refresh
    second = r.resolve("c-1", "https://player.invalid/x")
    assert fetch.calls == 2 and second["expires_at"] == "2026-11-07T11:40:00Z"
    third = r.resolve("c-1", "https://player.invalid/x", force=True)
    assert fetch.calls == 3 and third["src"]


def test_resolver_error_is_reported_not_raised():
    r = audio.AudioResolver(fetch_html=FakeFetch([RuntimeError("timeout")]), now=lambda: NOW)
    out = r.resolve("c-1", "https://player.invalid/x")
    assert out["src"] is None and out["error"]
    r2 = audio.AudioResolver(fetch_html=FakeFetch(["<html>no audio</html>"]), now=lambda: NOW)
    assert r2.resolve("c-1", "https://player.invalid/x")["src"] is None


# --- offsets ---------------------------------------------------------------------------------------------------

def test_utterance_starts_and_playback_offsets():
    conv = conv_call()
    msgs = {m["idx"]: m for m in conv["messages"]}
    assert msgs[4]["rec_sec"] == 23 and msgs[5]["rec_sec"] == 30            # event clock (ends of utterances)
    starts = audio.utterance_starts(conv)
    # the client's long phrase started before its end-of-utterance timestamp, but not before the previous turn
    assert msgs[4]["rec_sec"] <= starts[msgs[5]["id"]] < msgs[5]["rec_sec"]
    offsets = audio.playback_offsets(conv, lead=4.0)
    assert offsets[msgs[5]["id"]] == pytest.approx(starts[msgs[5]["id"]] + 4.0, abs=0.05)
    assert msgs[2]["id"] not in offsets and msgs[3]["id"] in offsets        # inside the pause: not playable
    assert all(v >= 0 for v in offsets.values())


def test_default_lead_depends_on_recording_start():
    conv = conv_call()
    assert audio.default_lead(conv) == audio.LEAD_CALL_START
    conv["meta"]["recording_start_sec"] = 30
    assert audio.default_lead(conv) == audio.LEAD_MID_CALL


def test_fit_lead_recovers_shift():
    ends = [5.0, 12.0, 20.0, 31.0, 44.0, 52.5, 61.0]
    silence_starts = [e + 4.2 for e in ends] + [100.0]
    lead, hit = audio.fit_lead(ends, silence_starts)
    assert lead == pytest.approx(4.2, abs=0.3) and hit == 1.0


def test_payload_shape_for_call_and_chat():
    conv = conv_call()
    r = audio.AudioResolver(fetch_html=FakeFetch([PLAYER]), now=lambda: NOW)
    p = audio.payload(conv, r, calibrate=False)
    assert set(p) >= {"src", "expires_at", "offsets", "gaps"}
    assert p["src"] and p["gaps"] == conv["meta"]["recording_gaps"]
    assert p["offset_method"] == "heuristic" and p["lead_sec"] == audio.LEAD_CALL_START
    chat = ingest.normalize_record({"conversation_id": "ch", "channel": "agent chat", "language": "RU",
                                    "transcript": [{"timestamp": "00:01", "speaker": "client", "text": "x"}]})
    pc = audio.payload(chat, r, calibrate=False)
    assert pc["src"] is None and pc["offsets"] == {} and pc["gaps"] == []


def test_payload_uses_stored_calibration(tmp_path):
    conv = conv_call()
    store = audio.CalibrationStore(tmp_path / "cal.json")
    store.put(conv["id"], {"lead_sec": 1.5, "hit_rate": 0.8, "method": "vad"})
    r = audio.AudioResolver(fetch_html=FakeFetch([PLAYER]), now=lambda: NOW)
    p = audio.payload(conv, r, store=store, calibrate=False)
    assert p["offset_method"] == "vad" and p["lead_sec"] == 1.5
    assert audio.CalibrationStore(tmp_path / "cal.json").get(conv["id"])["lead_sec"] == 1.5


def test_apply_offsets_overlays_message_rec_sec():
    conv = conv_call()
    detail = audio.with_playback_offsets(conv, lead=2.0)
    assert detail["messages"][4]["rec_sec"] != conv["messages"][4]["rec_sec"]
    assert detail["messages"][4]["rec_sec_event"] == conv["messages"][4]["rec_sec"]
    assert conv["messages"][4].get("rec_sec_event") is None                 # source untouched


@pytest.mark.skipif(os.environ.get("CQA_LIVE_AUDIO") != "1", reason="live organizers' audio check (CQA_LIVE_AUDIO=1)")
def test_live_dev_calls_resolve():
    convs = [c for c in ingest.ingest_file(ingest.ROOT / "data/raw/dev_records.json", "DEV") if c["channel"] == "call"]
    r = audio.AudioResolver()
    for conv in convs:
        p = audio.payload(conv, r, calibrate=False)
        assert p["src"] and p["expires_at"]
