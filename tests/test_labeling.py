"""Blind input routes: no model reads, explicit whitelists and no writes."""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.labeling import BlindConversation, get_db
from api.state import AppState
from cqa.db import Database
from conftest import synth_conv


@pytest.fixture()
def blind(tmp_path, v1, monkeypatch):
    # Separate app path from get_db()'s default: the router must use AppState.db.
    monkeypatch.setenv("CQA_DB", str(tmp_path / "wrong.sqlite"))
    state = AppState(db_path=tmp_path / "blind.sqlite", autoload=False, calibrate_audio=False,
                     evaluate=None, llm_call=None, web_dir=tmp_path / "no-web")
    poisoned = synth_conv("human-ru").model_dump()
    poisoned.update(score={"total": 85.38}, evals=[{"answer": "LEAK_EVAL"}],
                    model="LEAK_MODEL", audio_url="https://invalid.test/?token=LEAK_AUDIO",
                    meta={"suggested_answer": "LEAK_META"})
    poisoned["messages"][0].update(translation_ru="LEAK_TRANSLATION", explanation="LEAK_EXPLANATION",
                                   suggested_answer="LEAK_ANSWER")
    state.db.upsert_conversation(poisoned)
    state.db.upsert_conversation(synth_conv("human-kz", language="KZ", channel="call"))
    state.db.save_eval({"conv_id": "human-ru", "criterion_id": "C1", "criterion_hash": v1.criteria[0].hash,
                        "prompt_version": "LEAK_PROMPT", "answer": "yes", "model": "LEAK_MODEL"})
    app = create_app(state)  # Main wires this router; no second include_router here.
    with TestClient(app) as client:
        yield client, state.db, app


def test_all_blind_endpoints_only_read_conversation_and_checklist_tables(blind, monkeypatch):
    client, db, _ = blind
    statements = []
    real_connect = db.connect

    @contextmanager
    def audited_connect():
        with real_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(db, "connect", audited_connect)
    for path in ("conversations", "conversations/human-ru", "checklists", "checklists/1"):
        response = client.get(f"/api/labeling/{path}")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert "LEAK_" not in response.text
    assert statements
    assert all(query.lstrip().upper().startswith("SELECT ") for query in statements), statements
    assert all("FROM conversations" in q or "FROM checklist_versions" in q for q in statements), statements


def test_metadata_and_transcript_are_exact_allowlists(blind):
    client, _, _ = blind
    meta = client.get("/api/labeling/conversations").json()
    metadata_keys = {"id", "channel", "language", "group_id", "message_count"}
    assert len(meta) == 2
    assert all(set(c) == metadata_keys for c in meta)
    conv = client.get("/api/labeling/conversations/human-ru").json()
    assert set(conv) == metadata_keys | {"messages", "transcript_hash"}
    assert conv["message_count"] == len(conv["messages"]) == 7
    assert len(conv["transcript_hash"]) == 64
    assert all(set(m) == {"id", "idx", "speaker", "text", "ts_raw", "t_sec"} for m in conv["messages"])
    assert conv["messages"][0]["text"] == synth_conv("human-ru").messages[0].text
    with pytest.raises(ValueError):
        BlindConversation.model_validate({**conv, "model_answer": "yes"})


def test_checklist_only_contains_normative_rules_and_original_hashes(blind, v1):
    client, _, _ = blind
    lists = client.get("/api/labeling/checklists").json()
    assert len(lists) == 1
    meta_keys = {"checklist_id", "version", "name", "criterion_count", "checklist_hash"}
    assert set(lists[0]) == meta_keys
    cl = client.get("/api/labeling/checklists/1").json()
    assert set(cl) == meta_keys | {"criteria"}
    assert type(cl["version"]) is int and cl["version"] == 1
    assert cl["criterion_count"] == 8
    assert len(cl["checklist_hash"]) == 64
    assert cl["checklist_hash"] == lists[0]["checklist_hash"]
    keys = {"id", "hash", "title", "section", "description", "answer_type", "scale", "scale_labels",
            "na_allowed", "na_rule", "guidance"}
    for c, source in zip(cl["criteria"], v1.criteria):
        assert set(c) == keys
        assert c["hash"] == source.hash
        assert c["guidance"] == source.guidance
        assert c["description"] == source.description
    assert [c["id"] for c in cl["criteria"] if c["na_allowed"]] == ["C2", "C3", "C4", "C7"]
    assert cl["criteria"][5]["scale"] == [0, 4, 7, 10]
    assert "Да" in cl["criteria"][7]["description"]  # C8's inverted wording is preserved.
    assert not any(k in json.dumps(cl) for k in ('"weight"', '"score"', '"model"', '"confidence"'))


def test_route_validation_and_missing_inputs(blind):
    client, _, _ = blind
    assert client.get("/api/labeling/conversations?language=KZ&channel=call").json()[0]["id"] == "human-kz"
    assert client.get("/api/labeling/conversations?group_id=MISSING").json() == []
    for query in ("language=INVALID", "channel=email"):
        assert client.get(f"/api/labeling/conversations?{query}").status_code == 422
    for version in ("0", "-1", "1.5", "bad"):
        assert client.get(f"/api/labeling/checklists/{version}").status_code == 422
    assert client.get("/api/labeling/checklists/999").status_code == 404
    assert client.get("/api/labeling/conversations/unknown").status_code == 404
    # Guessing scored-child paths never returns a view projection.
    assert client.get("/api/labeling/conversations/human-ru/evals").status_code == 404
    assert client.get("/api/labeling/conversations/human-ru?trace=true&version=1").status_code == 200


def test_no_annotation_mutation_routes(blind):
    client, db, _ = blind
    with db.connect() as conn:
        before = list(conn.iterdump())
    for method in ("post", "put", "patch", "delete"):
        for path in ("conversations", "conversations/human-ru", "checklists", "checklists/1"):
            result = client.request(method, f"/api/labeling/{path}", json={"answer": "yes"})
            assert result.status_code in (404, 405)
    with db.connect() as conn:
        assert list(conn.iterdump()) == before


def test_dependency_is_overridable_and_does_not_seed_an_empty_db(blind, tmp_path):
    client, _, app = blind
    empty = Database(tmp_path / "empty.sqlite")
    app.dependency_overrides[get_db] = lambda: empty
    assert client.get("/api/labeling/conversations").json() == []
    assert client.get("/api/labeling/checklists").json() == []
    assert empty.get_checklist_version(1) is None
    assert client.get("/api/labeling/checklists/1").status_code == 404


def test_hashes_detect_changed_inputs_but_ignore_extra_model_metadata(blind):
    client, db, _ = blind
    original = client.get("/api/labeling/conversations/human-ru").json()
    conv = db.get_conversation("human-ru")
    conv.meta["suggested_answer"] = "LEAK_OTHER"
    conv.messages[0].translation_ru = "LEAK_OTHER_TRANSLATION"
    db.upsert_conversation(conv)
    assert client.get("/api/labeling/conversations/human-ru").json() == original

    # Simulate restoring a different dataset under the same ID outside normal ingest.
    data = conv.model_dump()
    data["messages"][0]["text"] += " Уточнение."
    with db.connect() as conn:
        conn.execute("UPDATE conversations SET data=? WHERE id=?", (json.dumps(data), conv.id))
    assert client.get("/api/labeling/conversations/human-ru").json()["transcript_hash"] != original["transcript_hash"]

    cl_before = client.get("/api/labeling/checklists/1").json()
    cl = db.get_checklist_version(1)
    changed = cl.model_dump()
    changed["criteria"][0]["description"] += " Уточнение правила."
    db.save_checklist_version(type(cl).model_validate(changed))
    cl_after = client.get("/api/labeling/checklists/1").json()
    assert cl_after["checklist_hash"] != cl_before["checklist_hash"]
    assert cl_after["criteria"][0]["hash"] != cl_before["criteria"][0]["hash"]


def test_scale_and_numeric_domains_are_exposed_without_preselected_answers(blind, v1):
    client, db, _ = blind
    data = v1.model_dump()
    data["version"] = 2
    data["criteria"][0].update(answer_type="numeric", scale=[2, 8], na_allowed=True,
                                na_rule="Нет применимого случая")
    data["criteria"].append({"id": "NEW", "title": "Новая шкала", "answer_type": "scale", "scale": [0, 2.5, 5]})
    db.save_checklist_version(type(v1).model_validate(data))
    cl = client.get("/api/labeling/checklists/2").json()
    assert cl["version"] == 2 and cl["criterion_count"] == 9
    assert cl["criteria"][0]["answer_type"] == "numeric"
    assert cl["criteria"][0]["scale"] == [2, 8]
    assert cl["criteria"][-1]["scale"] == [0, 2.5, 5]
    assert all("answer" not in c for c in cl["criteria"])
