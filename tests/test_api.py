"""HTTP API: frontend response shapes + the defence scenario end to end on a fake evaluator (no LLM, < 30 s).

Shapes follow the built frontend (branch frontend-wave1-review: docs/FRONTEND_HANDOFF.md, web/js/api.js,
scripts/make_ui_mocks.py). ``test_shapes_match_frontend_mock_fixtures`` generates the frontend's synthetic fixtures
and checks that every key the UI can read is present in our responses. All transcripts here are synthetic.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.state import AppState
from cqa import audio
from tests.offline_guard import no_network as _no_network

ROOT = Path(__file__).resolve().parents[1]
PLAYER = ('<html><body><audio id="a" controls><source src="https://media.example.invalid/file/1?sp=r&amp;'
          'se=2099-01-01T00:00:00Z&amp;sv=1" type="audio/mpeg"></audio></body></html>')


def _chat(cid, lang, operator_text, client_text):
    return {"conversation_id": cid, "channel": "agent chat", "language": lang, "audio_url": None, "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": client_text},
        {"timestamp": "00:02", "speaker": "bot", "text": "Учебный бот: передаю специалисту."},
        {"timestamp": "00:30", "speaker": "system", "text": "Эскалация на оператора."},
        {"timestamp": "00:45", "speaker": "operator", "text": operator_text},
        {"timestamp": "01:10", "speaker": "client", "text": "Спасибо, понятно."},
        {"timestamp": "01:20", "speaker": "operator", "text": "Рада помочь, хорошего дня!"},
    ]}


def _iso(sec):
    return f"2026-11-03T13:{int(sec) // 60:02d}:{int(sec) % 60:02d}.0000000+00:00"


SYNTH = [
    _chat("t-ru-001", "RU", "Здравствуйте! Меня зовут Алия, сейчас помогу.", "Как посмотреть учебный тариф?"),
    _chat("t-ru-002", "RU", "Добрый день, меня зовут Ерлан.", "Где найти учебный отчёт?"),
    _chat("t-kz-001", "KZ", "Сәлеметсіз бе! Менің атым Дана.", "Оқу тарифін қалай көремін?"),
    _chat("t-en-001", "EN", "Hello! My name is Sam, happy to help.", "Where is the practice report?"),
    {"conversation_id": "t-call-001", "channel": "voice call", "language": "RU",
     "audio_url": "https://player.example.invalid/x?token=SECRET_PLAYER", "transcript": [
         {"timestamp": _iso(0), "speaker": "system", "text": "Запись и транскрибирование запущены."},
         {"timestamp": _iso(8), "speaker": "bot", "text": "Учебная линия, нажмите два."},
         {"timestamp": _iso(10), "speaker": "client", "text": "2"},
         {"timestamp": _iso(20), "speaker": "system", "text": "Запись и транскрибирование приостановлены."},
         {"timestamp": _iso(30), "speaker": "system", "text": "Агент присоединился к разговору с клиентом."},
         {"timestamp": _iso(33), "speaker": "system", "text": "Запись и транскрибирование возобновлены."},
         {"timestamp": _iso(36), "speaker": "operator", "text": "Здравствуйте, меня зовут Нурлан."},
         {"timestamp": _iso(45), "speaker": "client", "text": "Хочу уточнить учебный вопрос."},
         {"timestamp": _iso(60), "speaker": "operator", "text": "Всего доброго!"},
         {"timestamp": _iso(62), "speaker": "system", "text": "Клиент завершил разговор."},
     ]},
]
# PDF control example for t-ru-001: C1 yes, C2 N/A, C3 N/A, C4 15, C5 yes, C6 7, C7 yes, C8 no -> 85.38
EXAMPLE = {"C1": "yes", "C2": "na", "C3": "na", "C4": 15, "C5": "yes", "C6": 7, "C7": "yes", "C8": "no"}


class FakeEvaluator:
    """Deterministic stand-in for cqa.evaluator: full marks, the PDF example for t-ru-001, and a stricter answer
    for any criterion whose wording contains «СТРОГО» (used to check that a replaced criterion is re-evaluated)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, conv, checklist, criterion_ids, mode="fast", profile=None, live=False):
        self.calls.append((conv["id"], tuple(criterion_ids), mode, live))
        ops = [m for m in conv["messages"] if m["speaker"] == "operator"]
        out = []
        for cid in criterion_ids:
            c = checklist.criterion(cid)
            if "СТРОГО" in c.description:
                answer = "no"
            elif mode == "accurate" and cid == "C6":
                answer = 7                                  # the accurate mode is stricter on C6
            elif (conv.get("meta") or {}).get("source_id", conv["id"]) == "t-ru-001" and cid in EXAMPLE:
                answer = EXAMPLE[cid]
            elif c.critical:
                answer = "no"
            else:
                answer = max(c.scale) if c.answer_type != "binary" else "yes"
            quote = ops[0]["text"][:12]
            out.append({"conv_id": conv["id"], "criterion_id": cid, "criterion_hash": c.hash, "prompt_version": "fake-v1",
                        "mode": mode, "answer": answer, "title": c.title,
                        "evidence": [] if answer == "na" else [{"message_id": ops[0]["id"], "quote": quote, "start": 0,
                                                                "end": len(quote), "valid": True}],
                        "checked_range": None, "na_reason": "Условие N/A выполнено (тест)." if answer == "na" else None,
                        "explanation": f"{cid}: тестовое обоснование [{ops[0]['idx']}].", "confidence": 0.9,
                        "model": "fake", "provider": "mock", "tokens_in": 100, "tokens_out": 20, "cost_kzt": 0.5,
                        "latency_ms": 12, "raw": {"fake": True, "criterion_id": cid}})
        return out


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_PROFILE", "mock")
    monkeypatch.setenv("CQA_MODE", "fast")
    fake = FakeEvaluator()
    resolver = audio.AudioResolver(fetch_html=lambda url: PLAYER)
    state = AppState(db_path=tmp_path / "t.sqlite", evaluate=fake, llm_call=None, resolver=resolver,
                     calibration=audio.CalibrationStore(tmp_path / "cal.json"), autoload=False, calibrate_audio=False,
                     web_dir=tmp_path / "web", workers=2)
    (tmp_path / "web" / "js").mkdir(parents=True)
    (tmp_path / "web" / "index.html").write_text("<!doctype html><title>Дәлел</title>", encoding="utf-8")
    (tmp_path / "web" / "js" / "api.js").write_text("export const x = 1;\n", encoding="utf-8")
    with TestClient(create_app(state)) as c:
        c.fake = fake
        yield c


def ingest(c, records=SYNTH, group_id="TEST"):
    r = c.post(f"/api/ingest?group_id={group_id}", json=records)
    assert r.status_code == 200, r.text
    return r.json()


def run(c, **body):
    r = c.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    deadline = time.time() + 15
    while True:
        st = c.get(f"/api/runs/{rid}").json()
        if st["status"] != "running" or time.time() > deadline:
            return st
        time.sleep(0.02)


# --- system --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("offline", [False, True])
def test_health_reports_provider_model_mode_and_cost(client, monkeypatch, offline):
    # The health flag reports configured cache-only policy, not whether this fake needs a network.
    monkeypatch.setenv("CQA_OFFLINE", "1" if offline else "0")
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["offline"] is offline
    assert h["profile"] == "mock" and h["provider"] == "mock" and h["mode"] == "fast"
    assert {"provider", "model", "mode", "cost_per_eval"} <= set(h)
    assert h["cost_per_eval"] is None                     # nothing measured yet
    ingest(client)
    run(client)
    h = client.get("/api/health").json()
    assert h["cost_per_eval"] == pytest.approx(8 * 0.5)  # all calls behind one conversation evaluation


def test_config_switches_mode_and_location(client):
    r = client.post("/api/config", json={"mode": "accurate", "location": "local"})
    assert r.status_code == 200, r.text
    h = client.get("/api/health").json()
    assert h["mode"] == "accurate" and h["location"] == "local"
    assert client.post("/api/config", json={"mode": "turbo"}).status_code == 422


def test_static_frontend_with_module_mime(client):
    assert "Дәлел" in client.get("/").text
    js = client.get("/js/api.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/api/nope").status_code == 404


def test_generated_fixtures_are_not_exposed_by_default(client):
    web = client.app.state.cqa.web_dir
    (web / "mocks").mkdir()
    (web / "mocks" / "private.json").write_text('{"transcript":"synthetic private probe"}')
    (web / ".private").write_text("synthetic private probe")
    assert client.get("/mocks/private.json").status_code == 404
    assert client.get("/.private").status_code == 404


def test_pinned_appeal_uses_requested_checklist_version(client):
    ingest(client)
    run(client)
    original = client.get('/api/checklists/1').json()
    original['criteria'][4]['description'] = 'Да — оператор СТРОГО соблюдает новую формулировку.'
    created = client.post('/api/checklists', json=original).json()
    run(client, version=created['version'], activate=True)
    appeal = client.post('/api/appeals', json={'conv_id':'t-ru-002','criterion_id':'C5',
                       'checklist_version':1,'comment':'Оспариваю исходную версию'}).json()
    assert appeal['checklist_version'] == 1 and appeal['original_answer'] == 'yes'
    before = client.get('/api/conversations/t-ru-002').json()['score']['total']
    decision = client.post('/api/decisions', json={'appeal_id':appeal['id'],'action':'change',
                           'new_answer':'no','reason':'Проверен прежний критерий'})
    assert decision.status_code == 200, decision.text
    assert client.get('/api/conversations/t-ru-002').json()['score']['total'] == before
    assert client.get('/api/conversations/t-ru-002?version=1').json()['score']['total'] == 85


# --- conversations -------------------------------------------------------------------------------------------

def test_ingest_list_and_filters(client):
    out = ingest(client)
    assert out["count"] == 5 and out["group_id"] == "TEST"
    rows = client.get("/api/conversations").json()
    assert len(rows) == 5
    row = rows[0]
    assert {"id", "channel", "language", "group_id", "total", "critical", "needs_review", "status", "cells"} <= set(row)
    assert row["status"] == "pending" and row["total"] is None
    assert len(client.get("/api/conversations?lang=KZ").json()) == 1
    assert len(client.get("/api/conversations?channel=call").json()) == 1
    assert len(client.get("/api/conversations?group_id=OTHER").json()) == 0


def test_detail_shape_and_no_signed_links(client):
    ingest(client)
    d = client.get("/api/conversations/t-call-001").json()
    assert {"id", "channel", "language", "group_id", "messages", "meta", "audio_url", "evals", "score", "checklist",
            "appeals", "decisions", "audit"} <= set(d)
    assert d["channel"] == "call" and d["messages"][0]["id"] == "t-call-001:0"
    assert all(m["speaker"] in ("client", "operator", "bot", "system") for m in d["messages"])
    assert d["audio_url"] == "/api/audio/t-call-001"
    assert "SECRET_PLAYER" not in json.dumps(d)
    assert client.get("/api/conversations/missing").status_code == 404


def test_speech_metrics_only_for_opened_calls(client):
    ingest(client)
    call = client.get("/api/conversations/t-call-001").json()["speech_metrics"]
    assert call["informational"] is True and call["affects_score"] is False
    assert call["source"] == "transcript" and call["silence_share"] is None   # no cached audio profile
    assert {"tempo_wpm", "pauses_over_3s", "longest_pause_sec", "note"} <= set(call)
    assert call["pauses_over_3s"] >= 1 and "на балл не влияет" in call["note"]
    assert client.get("/api/conversations/t-ru-001").json()["speech_metrics"] is None
    assert all("speech_metrics" not in row for row in client.get("/api/conversations").json())


def test_upload_single_record_body(client):
    out = client.post("/api/ingest?group_id=UPLOAD", json=SYNTH[0])
    assert out.status_code == 200 and out.json()["count"] == 1


# --- runs and scoring --------------------------------------------------------------------------------------------

def test_run_progress_shape_then_full_reuse(client):
    ingest(client)
    st = run(client)
    assert st["status"] == "completed", st
    assert {"run_id", "done", "total", "from_cache", "llm_calls", "cost_kzt", "elapsed", "per_conv"} <= set(st)
    assert st["done"] == st["total"] == 5 and st["from_cache"] == 0
    assert {"id", "old_total", "new_total", "status"} <= set(st["per_conv"][0])
    assert all(p["status"] == "done" for p in st["per_conv"])
    assert len(client.fake.calls) == 5
    again = run(client)
    assert again["from_cache"] == 5 * 8 and len(client.fake.calls) == 5      # nothing re-evaluated
    assert {p["new_total"] for p in again["per_conv"]} == {p["new_total"] for p in st["per_conv"]}


def test_score_follows_the_pdf_formula(client):
    ingest(client)
    run(client)
    d = client.get("/api/conversations/t-ru-001").json()
    s = d["score"]
    assert s["total"] == 85.38 and s["status"] == "complete" and s["critical"] is False
    assert any("85,38" in step or "85.38" in step for step in s["formula_steps"])  # Russian decimals
    assert len(d["evals"]) == 8
    ev = d["evals"][0]
    for key in ("answer", "evidence", "explanation", "confidence", "model", "tokens_in", "tokens_out", "cost_kzt",
                "latency_ms", "needs_review", "criterion_hash", "from_cache"):
        assert key in ev, key
    assert "raw" not in ev                                     # traces only on request
    assert "raw" in client.get("/api/conversations/t-ru-001?trace=1").json()["evals"][0]
    assert d["latency_ms"] == 8 * 12 and d["cost_kzt"] == pytest.approx(4.0)
    row = next(r for r in client.get("/api/conversations").json() if r["id"] == "t-ru-001")
    assert row["total"] == 85.38 and row["status"] in ("evaluated", "needs_review") and len(row["cells"]) == 8


# --- appeals and decisions -------------------------------------------------------------------------------------

def test_appeal_dossier_decision_flow(client):
    ingest(client)
    run(client)
    before = client.get("/api/conversations/t-ru-002").json()
    r = client.post("/api/appeals", json={"conv_id": "t-ru-002", "criterion_id": "C5", "comment": "Оператор был вежлив?",
                                          "message_ids": ["t-ru-002:3"]})
    assert r.status_code == 200, r.text
    appeal = r.json()
    assert appeal["status"] == "open" and appeal["id"]
    assert client.post("/api/appeals", json={"conv_id": "t-ru-002", "criterion_id": "C5", "comment": "ещё раз"}).status_code == 409
    assert client.post("/api/appeals", json={"conv_id": "t-ru-002", "criterion_id": "C5", "comment": ""}).status_code == 422
    assert [a["id"] for a in client.get("/api/appeals?status=open").json()] == [appeal["id"]]
    dossier = client.get(f"/api/appeals/{appeal['id']}/dossier").json()
    assert {"appeal_id", "arguments_for", "arguments_against", "recommendation"} <= set(dossier)
    assert client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "change", "new_answer": "no",
                                               "reason": ""}).status_code == 422
    assert client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "change", "new_answer": 7,
                                               "reason": "Тест"}).status_code == 422          # not a binary answer
    r = client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "change", "new_answer": "no",
                                            "reason": "Проверено вручную: шаблонный ответ"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["old_total"] == 100 and res["new_total"] == 85.0
    assert {"old_total", "new_total", "decision", "score"} <= set(res)
    after = client.get("/api/conversations/t-ru-002").json()
    c5_before = next(e for e in before["evals"] if e["criterion_id"] == "C5")
    c5_after = next(e for e in after["evals"] if e["criterion_id"] == "C5")
    assert c5_after["answer"] == c5_before["answer"] == "yes"          # the original evaluation is kept
    assert after["score"]["total"] == 85.0 and len(after["decisions"]) == 1
    events = [a["event"] for a in after["audit"]]
    assert "appeal.created" in events and "decision.saved" in events and "score.recalculated" in events
    assert client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "confirm",
                                               "reason": "повтор"}).status_code == 409
    assert client.get("/api/appeals?status=open").json() == []


# --- checklist versions: replace a criterion, apply ------------------------------------------------------------------

def test_replace_criterion_reevaluates_only_it(client):
    ingest(client)
    run(client)
    versions = client.get("/api/checklists").json()
    assert [v["version"] for v in versions] == [1] and versions[0]["active"] is True
    v1 = client.get("/api/checklists/1").json()
    body = {k: v for k, v in v1.items() if k not in ("version", "created_at", "active")}
    body["criteria"] = [dict(c) for c in v1["criteria"]]
    c5 = next(c for c in body["criteria"] if c["id"] == "C5")
    c5["description"] = "Да — оператор СТРОГО следует стандарту вежливости."
    body["change_note"] = "Заменён C5"
    r = client.post("/api/checklists", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    diff = client.get("/api/checklists/1/diff/2").json()
    assert diff["needs_reeval"] == ["C5"] and diff["from"] == 1 and diff["to"] == 2
    calls_before = len(client.fake.calls)
    preview = run(client, version=2, preview=True)
    assert preview["total"] == 5 and preview["status"] == "completed"
    assert preview["from_cache"] == 5 * 7                                 # only the replaced criterion is evaluated
    assert client.get("/api/checklists").json()[0]["active"] is True          # preview does not activate
    st = run(client, version=2, activate=True)
    new_calls = client.fake.calls[calls_before:]
    assert {c[1] for c in new_calls} == {("C5",)}                         # only the replaced criterion
    assert st["from_cache"] == 5 * 8                                      # the preview answers are reused
    assert all(p["new_total"] < p["old_total"] for p in st["per_conv"])
    d = client.get("/api/conversations/t-ru-002").json()
    assert d["checklist"]["version"] == 2 and d["score"]["total"] == 85.0


def test_weight_only_change_costs_no_llm_call(client):
    ingest(client)
    run(client)
    v1 = client.get("/api/checklists/1").json()
    body = {k: v for k, v in v1.items() if k not in ("version", "created_at", "active")}
    body["criteria"] = [dict(c) for c in v1["criteria"]]
    body["criteria"][0]["weight"] = 15
    body["criteria"][1]["weight"] = 10
    v = client.post("/api/checklists", json=body).json()["version"]
    assert client.get(f"/api/checklists/1/diff/{v}").json()["needs_reeval"] == []
    calls = len(client.fake.calls)
    st = run(client, version=v)
    assert len(client.fake.calls) == calls and st["from_cache"] == 5 * 8


def test_invalid_checklist_is_rejected(client):
    v1 = client.get("/api/checklists/1").json()
    body = {k: v for k, v in v1.items() if k not in ("version", "created_at", "active")}
    body["criteria"] = [dict(c) for c in v1["criteria"]] + [dict(v1["criteria"][0])]   # duplicate id
    assert client.post("/api/checklists", json=body).status_code == 422
    assert client.get("/api/checklists/99").status_code == 404


# --- audio ---------------------------------------------------------------------------------------------------

def test_audio_endpoint(client):
    ingest(client)
    a = client.get("/api/audio/t-call-001").json()
    assert {"src", "expires_at", "offsets", "gaps"} <= set(a)
    assert a["src"].startswith("https://media.example.invalid/") and a["expires_at"] == "2099-01-01T00:00:00Z"
    assert "t-call-001:6" in a["offsets"] and a["gaps"] == [[20, 33]]
    chat = client.get("/api/audio/t-ru-001").json()
    assert chat["src"] is None and chat["offsets"] == {}
    assert client.get("/api/audio/missing").status_code == 404


def test_metrics(client):
    ingest(client)
    run(client)
    m = client.get("/api/metrics").json()
    assert {"evaluated", "cost_kzt", "llm_calls"} <= set(m)
    assert m["evaluated"] == 5 and m["conversations"] == 5


# --- contract against the frontend's own mock fixtures ---------------------------------------------------------------

MOCK_ONLY = {"is_mock", "mock_origin", "mock_notice", "metrics_are_illustrative", "verification", "notice"}
MAPS = {"offsets", "sections", "scale_labels", "translation_ru", "payload", "by_model"}   # keyed by data, not schema


def _load_mock_generator(tmp_path):
    local = ROOT / "scripts" / "make_ui_mocks.py"
    path = local if local.exists() else tmp_path / "make_ui_mocks.py"
    if not local.exists():
        try:
            src = subprocess.run(["git", "show", "frontend-wave1-review:scripts/make_ui_mocks.py"], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("frontend mock generator is not available")
        path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("ui_mocks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _missing_keys(mock, ours, path=""):
    """Keys the UI can read in the mock that our response lacks (recursing into dicts and first list items)."""
    out = []
    if isinstance(mock, dict) and isinstance(ours, dict):
        for k, v in mock.items():
            if k in MOCK_ONLY:
                continue
            if k not in ours:
                out.append(f"{path}.{k}")
            elif k in MAPS:
                if v is not None and ours[k] is not None and type(v) is not type(ours[k]):
                    out.append(f"{path}.{k} (type)")
            else:
                out += _missing_keys(v, ours[k], f"{path}.{k}")
    elif isinstance(mock, list) and isinstance(ours, list) and mock and ours:
        out += _missing_keys(mock[0], ours[0], f"{path}[0]")
    return out


def test_shapes_match_frontend_mock_fixtures(client, tmp_path):
    mocks = _load_mock_generator(tmp_path)
    fx = mocks.build_fixtures(mocks.synthetic_records(), synthetic=True)
    ingest(client)
    run(client)
    call_mock = next(v for k, v in fx.items() if k.startswith("conversation_") and v["channel"] == "call")
    chat_mock = next(v for k, v in fx.items() if k.startswith("conversation_") and v["channel"] == "chat")
    checks = {
        "conversations[0]": (fx["conversations.json"][0], client.get("/api/conversations").json()[0]),
        "detail(chat)": (chat_mock, client.get("/api/conversations/t-ru-001").json()),
        "detail(call)": (call_mock, client.get("/api/conversations/t-call-001").json()),
        "audio": (fx[f"audio_{call_mock['id']}.json"], client.get("/api/audio/t-call-001").json()),
        "checklists": (fx["checklists.json"][0], client.get("/api/checklists").json()[0]),
        "health": (fx["health.json"], client.get("/api/health").json()),
        "metrics": (fx["metrics.json"], client.get("/api/metrics").json()),
        "run": (fx["run_progress_4.json"], run(client)),
    }
    appeal = client.post("/api/appeals", json={"conv_id": "t-ru-001", "criterion_id": "C1", "comment": "Проверьте",
                                               "message_ids": ["t-ru-001:3"]}).json()
    checks["appeal"] = (fx["appeals.json"][0], appeal)
    checks["dossier"] = ({"appeal_id": 1, "arguments_for": [], "arguments_against": [], "recommendation": ""},
                         client.get(f"/api/appeals/{appeal['id']}/dossier").json())
    decision = client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "change", "new_answer": "no",
                                                   "reason": "Тест"}).json()
    checks["decision"] = ({k: v for k, v in fx["decision_result.json"].items() if k != "conv_id"}, decision)
    problems = {name: _missing_keys(m, o) for name, (m, o) in checks.items()}
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, json.dumps(problems, ensure_ascii=False, indent=1)


def test_defence_scenario_under_30_seconds(client):
    t0 = time.perf_counter()
    ingest(client)
    run(client)
    appeal = client.post("/api/appeals", json={"conv_id": "t-kz-001", "criterion_id": "C6", "comment": "Ясно ответил"}).json()
    client.post("/api/decisions", json={"appeal_id": appeal["id"], "action": "change", "new_answer": 7, "reason": "Один сбой"})
    v1 = client.get("/api/checklists/1").json()
    body = {k: v for k, v in v1.items() if k not in ("version", "created_at", "active")}
    body["criteria"] = [dict(c) for c in v1["criteria"]]
    body["criteria"][6]["description"] = "Да — оператор СТРОГО подвёл итог и попрощался."
    v2 = client.post("/api/checklists", json=body).json()["version"]
    st = run(client, version=v2, activate=True)
    assert st["status"] == "completed" and st["from_cache"] == 5 * 7
    assert client.get("/api/conversations/t-kz-001").json()["score"]["total"] < 100
    assert time.perf_counter() - t0 < 30


def test_accurate_run_does_not_reuse_fast_and_the_card_shows_the_newest(client):
    ingest(client)
    run(client)
    assert client.get("/api/conversations/t-ru-002").json()["score"]["total"] == 100
    client.post("/api/config", json={"mode": "accurate"})
    st = run(client)
    assert st["from_cache"] == 0 and len(client.fake.calls) == 10        # accurate answers are computed anew
    assert client.get("/api/conversations/t-ru-002").json()["score"]["total"] == 95.5   # C6 = 7: 15 - 10.5
    client.post("/api/config", json={"mode": "fast"})
    run(client)
    assert client.get("/api/conversations/t-ru-002").json()["score"]["total"] == 95.5   # newest answer stays


def test_cloud_and_local_answers_do_not_overwrite_each_other(client):
    ingest(client)
    run(client)
    client.post("/api/config", json={"location": "local"})
    first = run(client)
    assert first["from_cache"] == 0
    again = run(client)
    assert again["from_cache"] == 5 * 8                                    # local answers are found again
    repo = client.app.state.cqa.repo
    modes = {e["mode"] for e in repo.evals_for("t-ru-001")}
    assert len(repo.evals_for("t-ru-001")) == 16 and len(modes) == 2      # both kept, under different keys


def test_card_does_not_show_other_profile_or_stale_policy(client, monkeypatch):
    ingest(client)
    run(client)
    client.post('/api/config',json={'location':'local'})
    assert client.get('/api/conversations/t-ru-001').json()['evals'] == []
    client.post('/api/config',json={'location':'cloud'})
    state = client.app.state.cqa
    monkeypatch.setattr(state, 'prompt_version_for', lambda c: 'new-policy' if c.id == 'C2' else None)
    card = client.get('/api/conversations/t-ru-001').json()
    assert len(card['evals']) == 7
    assert 'C2' in card['score']['missing']
    assert card['score']['total'] is None


def test_changed_transcript_rejected_without_reusing_evidence(client):
    # a3 ingest semantics: a changed dialogue under a taken id is stored under its own id (meta.source_id keeps the
    # organizers' id); the stored dialogue, its evaluations and decisions stay untouched and nothing is reused.
    import copy
    ingest(client)
    run(client)
    changed=copy.deepcopy(SYNTH[0])
    changed['transcript'][3]['text']='Другая синтетическая реплика.'
    response=client.post('/api/ingest',json=changed)
    assert response.status_code == 200, response.text
    new_id=response.json()['ids'][0]
    assert new_id != 't-ru-001' and response.json()['renamed'] == {'t-ru-001': new_id}
    fresh=client.get(f'/api/conversations/{new_id}').json()
    assert fresh['evals'] == [] and fresh['meta']['source_id'] == 't-ru-001'
    card=client.get('/api/conversations/t-ru-001').json()
    assert card['messages'][3]['text'] == SYNTH[0]['transcript'][3]['text']
    assert card['score']['total'] == 85.38


def test_live_without_force_and_empty_selection(client):
    ingest(client)
    run(client)
    count=len(client.fake.calls)
    live=run(client,live=True)                      # live alone: stored answers of unchanged criteria are reused
    assert len(client.fake.calls)==count and live['from_cache']==5*8 and live['force'] is False
    forced=run(client,live=True,force=True)         # explicit full refresh (control_run.py): everything live
    assert len(client.fake.calls)==count+5 and forced['from_cache']==0
    assert all(c[3] for c in client.fake.calls[count:])
    assert client.post('/api/runs',json={'conv_ids':[]}).status_code==422


def _changed_version(client, changes):
    v=client.get('/api/checklists/1').json()
    for cid, fields in changes.items():
        next(c for c in v['criteria'] if c['id']==cid).update(fields)
    for k in ('version','created_at','active','parent_version'):
        v.pop(k,None)
    r=client.post('/api/checklists',json=v)
    assert r.status_code==200, r.text
    return r.json()['version']


def test_live_apply_of_weight_change_costs_zero_calls(client):
    ingest(client)
    run(client)
    count=len(client.fake.calls)
    weights={c['id']:c['weight'] for c in client.get('/api/checklists/1').json()['criteria']}
    version=_changed_version(client,{'C1':{'weight':weights['C1']+5},'C4':{'weight':weights['C4']-5}})
    st=run(client,version=version,live=True,activate=True)
    assert st['status']=='completed' and st['activated'] is True
    assert len(client.fake.calls)==count and st['pairs_to_evaluate']==0 and st['from_cache']==st['pairs_total']==5*8


def test_live_apply_of_replaced_criterion_reevaluates_only_its_pairs(client):
    ingest(client)
    run(client)
    count=len(client.fake.calls)
    version=_changed_version(client,{'C5':{'title':'Проактивность','description':'СТРОГО: оператор сам предложил следующий шаг.'}})
    st=run(client,version=version,live=True,activate=True)
    new=client.fake.calls[count:]
    assert st['status']=='completed' and st['pairs_to_evaluate']==5 and st['from_cache']==5*7
    assert len(new)==5 and all(c[1]==('C5',) and c[3] is True for c in new)    # only C5, live (no LLM cache)
    assert client.get('/api/conversations/t-ru-002').json()['score']['total'] < 100


def test_live_run_on_new_dialogues_evaluates_everything(client):
    ingest(client)
    run(client)
    count=len(client.fake.calls)
    import copy
    fresh=copy.deepcopy(SYNTH[1]); fresh['conversation_id']='t-new-777'
    ingest(client,[fresh],group_id='CTRL-NEW')
    st=run(client,group_id='CTRL-NEW',live=True)
    new=client.fake.calls[count:]
    assert st['status']=='completed' and st['from_cache']==0 and st['pairs_to_evaluate']==8
    assert len(new)==1 and new[0][0]=='t-new-777' and len(new[0][1])==8 and new[0][3] is True


def test_ingest_conflict_does_not_partially_import_batch(client):
    # With a3's collision renaming a batch with a changed dialogue imports fully and never overwrites the original.
    import copy
    ingest(client)
    fresh = copy.deepcopy(SYNTH[0])
    fresh['conversation_id'] = 'synthetic-new-in-conflicting-batch'
    changed = copy.deepcopy(SYNTH[0])
    changed['transcript'][3]['text'] = 'Изменённый синтетический текст.'
    response = client.post('/api/ingest', json=[fresh, changed])
    assert response.status_code == 200, response.text
    assert client.get('/api/conversations/synthetic-new-in-conflicting-batch').status_code == 200
    assert client.get('/api/conversations/t-ru-001').json()['messages'][3]['text'] == SYNTH[0]['transcript'][3]['text']


def test_db_still_rejects_changed_content_under_the_same_id(tmp_path):
    # Safety net below the ingest layer stays: a direct upsert with changed content is a conflict.
    import copy
    from cqa import ingest as ingest_mod
    from cqa.db import ConversationConflictError, Database
    db = Database(tmp_path / 'x.sqlite')
    conv = ingest_mod.normalize_record(copy.deepcopy(SYNTH[0]), 'DEV')
    db.upsert_conversation(conv)
    other = copy.deepcopy(conv)
    other['messages'][3]['text'] = 'Другая реплика.'
    with pytest.raises(ConversationConflictError):
        db.upsert_conversation(other)


def test_export_freezes_results_before_later_decision(client):
    ingest(client)
    st=run(client)
    url=f"/api/runs/{st['run_id']}/export"
    response=client.get(url)
    assert response.status_code==200, response.text
    saved=response.json()
    assert len(saved['per_conv'])==5
    assert all(len(c['answers'])==8 for c in saved['per_conv'])
    a=client.post('/api/appeals',json={'conv_id':'t-ru-002','criterion_id':'C5','comment':'Пересмотреть'}).json()
    r=client.post('/api/decisions',json={'appeal_id':a['id'],'action':'change','new_answer':'no','reason':'Проверено'})
    assert r.status_code==200
    assert client.get(url).json()==saved
    csv=client.get(url+'?format=csv')
    assert csv.status_code==200 and csv.content.startswith(b'\xef\xbb\xbf')
    assert len(csv.content.decode('utf-8-sig').splitlines())==41


def test_renamed_criterion_with_same_wording_keeps_its_evaluation(client):
    ingest(client)
    run(client)
    v1 = client.get("/api/checklists/1").json()
    body = {k: v for k, v in v1.items() if k not in ("version", "created_at", "active")}
    body["criteria"] = [dict(c) for c in v1["criteria"]]
    body["criteria"][6]["id"] = "C7x"
    v = client.post("/api/checklists", json=body).json()["version"]
    assert client.get(f"/api/checklists/1/diff/{v}").json()["needs_reeval"] == []
    client.post(f"/api/checklists/{v}/activate")
    d = client.get("/api/conversations/t-ru-001").json()
    assert "C7x" in [e["criterion_id"] for e in d["evals"]] and d["score"]["total"] == 85.38



def test_control_dialogue_with_a_reused_id_never_inherits_evaluations(client):
    ingest(client)
    run(client)
    clash = json.loads(json.dumps(SYNTH[0]))
    clash["transcript"][3]["text"] = "Добрый вечер, это другой оператор."
    out = client.post("/api/ingest?group_id=CTRL", json=[clash]).json()
    assert out["count"] == 1
    rows = {r["id"]: r for r in client.get("/api/conversations?group_id=CTRL").json()}
    assert list(rows) == ["t-ru-001@CTRL"] and rows["t-ru-001@CTRL"]["status"] == "pending"
    d = client.get("/api/conversations/t-ru-001@CTRL").json()
    assert d["meta"]["source_id"] == "t-ru-001" and d["evals"] == [] and d["messages"][0]["id"] == "t-ru-001@CTRL:0"
    assert client.get("/api/conversations/t-ru-001").json()["score"]["total"] == 85.38   # DEV dialogue untouched
    calls = len(client.fake.calls)
    st = run(client, group_id="CTRL", live=True)
    assert st["from_cache"] == 0 and len(client.fake.calls) == calls + 1
    assert client.fake.calls[-1][0] == "t-ru-001@CTRL" and client.fake.calls[-1][3] is True   # live: no LLM cache
    again = client.post("/api/ingest?group_id=CTRL", json=[clash]).json()
    assert again["new"] == 0 and len(client.get("/api/conversations?group_id=CTRL").json()) == 1   # idempotent


# --- control run (scripts/control_run.py) ---------------------------------------------------------------------------
# Synthetic fake-evaluator runs explicitly opt into rehearsal; the production mock guard stays enabled.

def _control_module():
    spec = importlib.util.spec_from_file_location("control_run", ROOT / "scripts" / "control_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_control_run_is_live_and_exports_results_with_traces(client, tmp_path):
    import csv

    control = _control_module()
    state = client.app.state.cqa
    ingest(client)                                    # DEV-like data already evaluated with the same ids ...
    run(client)
    calls = len(client.fake.calls)
    rep = control.run_control(state, "CTRL-9", records=SYNTH, mode="accurate", allow_mock=True, out_dir=tmp_path)
    assert rep["status"] == "completed" and rep["conversations"] == 5 and rep["failed"] == 0
    new_calls = client.fake.calls[calls:]
    assert len(new_calls) == 5 and all(c[3] is True and c[2] == "accurate" for c in new_calls)   # live, nothing reused
    data = json.loads((tmp_path / "control_CTRL-9.json").read_text(encoding="utf-8"))
    by_id = {d["conversation_id"]: d for d in data["conversations"]}
    assert set(by_id) == {r["conversation_id"] for r in SYNTH}                # the organizers' ids
    first = by_id["t-ru-001"]
    assert first["total"] == 85.38 and first["critical"] is False and len(first["criteria"]) == 8
    c4 = next(c for c in first["criteria"] if c["criterion_id"] == "C4")
    assert c4["answer"] == 15 and c4["points"] == 15 and c4["explanation"] and c4["evidence"][0]["quote"]
    assert data["run"]["live"] is True and data["run"]["mode"] == "accurate" and data["checklist"]["version"] == 1
    rows = list(csv.DictReader((tmp_path / "control_CTRL-9.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 5 * 8 and {"conversation_id", "criterion_id", "answer", "points", "explanation", "evidence",
                                    "total", "critical"} <= set(rows[0])
    trace = [json.loads(line) for line in (tmp_path / "control_CTRL-9_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(trace) == 5 * 8 and all(t["raw"] and t["model"] and "prompt_version" in t for t in trace)
    assert rep["files"]["json"].endswith("control_CTRL-9.json")


def test_dossier_has_flat_quotes_and_appeal_history(client):
    ingest(client)
    run(client)
    a = client.post("/api/appeals", json={"conv_id": "t-ru-002", "criterion_id": "C5", "comment": "Пересмотреть"}).json()
    d = client.get(f"/api/appeals/{a['id']}/dossier").json()
    assert "arguments_for" in d and d["for"] == d["arguments_for"] and d["against"] == d["arguments_against"]
    assert all("quote" in x and "message_id" in x for x in d["arguments_for"] + d["arguments_against"])
    r = client.post("/api/decisions", json={"appeal_id": a["id"], "action": "change", "new_answer": "no", "reason": "Проверено"})
    assert r.status_code == 200, r.text
    h = client.get(f"/api/appeals/{a['id']}/history")
    assert h.status_code == 200, h.text
    kinds = [x.get("kind") or x.get("event") or x.get("type") for x in h.json()]
    assert len(kinds) >= 3
    assert client.get("/api/appeals/nope/history").status_code == 404


def test_control_run_never_exports_old_answers_for_a_failed_live_evaluation(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    first = control.run_control(state, "CTRL-F", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
    assert first["failed"] == 0
    real = client.fake.__call__

    def flaky(conv, checklist, criterion_ids, **kw):
        if (conv.get("meta") or {}).get("source_id", conv["id"]) == "t-ru-002":
            raise RuntimeError("provider down (test)")
        return real(conv, checklist, criterion_ids, **kw)

    state._evaluate = flaky
    rep = control.run_control(state, "CTRL-F", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
    data = json.loads((tmp_path / "control_CTRL-F.json").read_text(encoding="utf-8"))
    bad = next(c for c in data["conversations"] if c["conversation_id"] == "t-ru-002")
    assert bad["error"] and bad["total"] is None and all(c["answer"] is None for c in bad["criteria"])
    assert rep["failed"] >= 1 and "t-ru-002" in rep["failed_ids"]
    trace = [json.loads(x) for x in (tmp_path / "control_CTRL-F_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not any(t["conversation_id"] == "t-ru-002" for t in trace)
