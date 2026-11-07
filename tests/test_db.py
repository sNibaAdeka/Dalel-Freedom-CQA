"""cqa/db.py on a temporary SQLite file (no organizer data)."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from cqa import db as dbmod
from cqa.db import ConversationConflictError, Database, get_db
from cqa.models import Appeal, Conversation, CriterionEval, Decision


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "cqa.sqlite")


def _conv(cid="demo-ru-001", lang="RU", channel="chat", group="DEV"):
    return Conversation(id=cid, channel=channel, language=lang, group_id=group,
                        messages=[{"id": f"{cid}:0", "idx": 0, "speaker": "operator", "text": "Здравствуйте!"}])


def _ev(conv="demo-ru-001", cid="C1", h="h1", answer="yes", mode="fast", pv="p1", **kw):
    return CriterionEval(conv_id=conv, criterion_id=cid, criterion_hash=h, prompt_version=pv, mode=mode,
                         answer=answer, **kw)


def test_schema_and_wal(db):
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversations", "checklist_versions", "evaluations", "appeals", "decisions", "audit_log", "llm_calls",
            "runs", "settings"} <= tables
    assert db.get_setting("schema_version") == 1
    Database(db.path)  # re-open is idempotent («создать, если нет»)


def test_conversations(db):
    db.upsert_conversation(_conv())
    db.upsert_conversation(_conv("demo-kz-001", "KZ"))
    db.upsert_conversation(_conv("demo-call-001", "RU", "call", group="CTRL"))
    assert db.get_conversation("demo-kz-001").language == "KZ"
    assert db.get_conversation("missing") is None
    assert [c.id for c in db.list_conversations(group_id="DEV")] == ["demo-kz-001", "demo-ru-001"]
    assert [c.id for c in db.list_conversations(channel="call")] == ["demo-call-001"]
    changed = _conv()
    changed.messages[0].text = "synthetic replacement"
    with pytest.raises(ConversationConflictError) as exc:
        db.upsert_conversation(changed)
    assert exc.value.status == 409
    assert db.get_conversation("demo-ru-001").messages == _conv().messages
    assert len(db.list_conversations()) == 3


@pytest.mark.parametrize("field", ["text", "speaker", "ts_raw", "t_sec", "id", "idx", "language", "channel", "messages"])
def test_reject_changed_content_preserves_evaluations_decisions_and_audit(db, field):
    original = _conv()
    original.messages[0].text = "SYNTHETIC_ORIGINAL"
    db.upsert_conversation(original)
    db.save_eval(_ev())
    db.create_appeal(Appeal(id="synthetic-appeal", conv_id=original.id, criterion_id="C1", comment="synthetic review"))
    db.add_decision(Decision(id="synthetic-decision", appeal_id="synthetic-appeal", conv_id=original.id,
                             criterion_id="C1", action="change", new_answer="no", reason="synthetic reason"))
    db.add_audit("decision", "synthetic-decision", "decision.saved", conv_id=original.id)
    before = (db.get_conversation(original.id), db.list_evals(), db.list_decisions(), db.list_appeals(), db.list_audit())
    changed = original.model_copy(deep=True)
    if field in ("language", "channel", "messages"):
        setattr(changed, field, {"language": "EN", "channel": "call", "messages": []}[field])
    else:
        setattr(changed.messages[0], field, {"text": "SYNTHETIC_CHANGED", "speaker": "client", "ts_raw": "00:09",
                                           "t_sec": 9, "id": "synthetic-other:0", "idx": 9}[field])
    with pytest.raises(ConversationConflictError) as exc:
        db.upsert_conversation(changed)
    assert exc.value.status == 409
    assert "SYNTHETIC_ORIGINAL" not in str(exc.value) and "SYNTHETIC_CHANGED" not in str(exc.value)
    after = (db.get_conversation(original.id), db.list_evals(), db.list_decisions(), db.list_appeals(), db.list_audit())
    assert after == before


def test_identical_reimport_and_metadata_enrichment_remain_allowed(db):
    conv = _conv()
    db.upsert_conversation(conv)
    db.save_eval(_ev())
    enriched = conv.model_copy(deep=True)
    enriched.group_id = "SYNTHETIC_CONTROL"
    enriched.meta = {"synthetic": True}
    enriched.messages[0].rec_sec = 1
    enriched.messages[0].translation_ru = "synthetic subtitle"
    db.upsert_conversation(enriched)
    db.upsert_conversation(enriched.model_dump())
    assert db.get_conversation(conv.id) == enriched
    assert len(db.list_conversations()) == len(db.list_evals()) == 1


def test_batch_upsert_accepts_iterable_of_dicts_and_models(db):
    original = _conv("synthetic-existing")
    db.upsert_conversation(original)
    enriched = original.model_copy(update={"group_id": "SYNTHETIC_CONTROL"})
    new = _conv("synthetic-new")
    result = db.upsert_conversations(item for item in (new.model_dump(), enriched))
    assert result == [new, enriched]
    assert db.get_conversation(new.id) == new and db.get_conversation(original.id) == enriched
    assert db.upsert_conversations(iter(())) == []


def test_batch_conflict_rolls_back_earlier_insert_and_metadata_update(db):
    original, protected = _conv("synthetic-existing"), _conv("synthetic-protected")
    db.upsert_conversations([original, protected])
    db.save_eval(_ev(conv=protected.id))
    db.add_audit("conversation", protected.id, "synthetic.saved", conv_id=protected.id)
    before = (db.list_conversations(), db.list_evals(), db.list_audit())
    enriched = original.model_copy(update={"group_id": "SYNTHETIC_CONTROL"})
    changed = protected.model_copy(deep=True)
    changed.messages[0].text = "SYNTHETIC_CONFLICT"
    with pytest.raises(ConversationConflictError) as exc:
        db.upsert_conversations([_conv("synthetic-new"), enriched, changed])
    assert exc.value.status == 409
    assert (db.list_conversations(), db.list_evals(), db.list_audit()) == before


def test_batch_conflicting_duplicate_ids_roll_back_all_inserts(db):
    first = _conv("synthetic-duplicate")
    changed = first.model_copy(deep=True)
    changed.messages[0].text = "SYNTHETIC_CONFLICT"
    with pytest.raises(ConversationConflictError):
        db.upsert_conversations([first, _conv("synthetic-other"), changed])
    assert db.list_conversations() == []


@pytest.mark.parametrize("as_model", [False, True])
def test_batch_invalid_late_item_leaves_database_untouched(db, as_model):
    invalid = _conv("synthetic-invalid")
    invalid.language = "invalid-language"
    with pytest.raises(ValueError):
        db.upsert_conversations([_conv("synthetic-new"), invalid if as_model else invalid.model_dump()])
    assert db.list_conversations() == []


def test_batch_generator_failure_does_not_import_partial_input(db):
    def broken_input():
        yield _conv("synthetic-new")
        raise RuntimeError("synthetic input failure")

    with pytest.raises(RuntimeError, match="synthetic input failure"):
        db.upsert_conversations(broken_input())
    assert db.list_conversations() == []


def test_changed_legacy_transcript_is_rejected(db):
    conv = {"id": "synthetic-legacy", "channel": "chat", "language": "EN",
            "transcript": [{"speaker": "operator", "text": "SYNTHETIC_A"}]}
    db.upsert_conversation(conv)
    changed = {**conv, "transcript": [{"speaker": "operator", "text": "SYNTHETIC_B"}]}
    with pytest.raises(ConversationConflictError):
        db.upsert_conversation(changed)
    assert db.get_conversation(conv["id"]).transcript == conv["transcript"]


def test_competing_first_imports_do_not_overwrite_each_other(db):
    other = Database(db.path)
    barrier = threading.Barrier(2)
    accepted, rejected, errors = [], [], []

    def insert(storage, text):
        conv = _conv("synthetic-race")
        conv.messages[0].text = text
        try:
            barrier.wait(5)
            storage.upsert_conversation(conv)
            accepted.append(text)
        except ConversationConflictError:
            rejected.append(text)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=insert, args=(storage, text))
               for storage, text in ((db, "SYNTHETIC_A"), (other, "SYNTHETIC_B"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert len(accepted) == len(rejected) == 1
    assert db.get_conversation("synthetic-race").messages[0].text == accepted[0]


def test_checklist_versions_and_active(db, v1):
    db.save_checklist_version(v1)
    assert db.get_checklist_version(1).criterion("C8").critical_when == "yes"
    assert db.max_checklist_version() == 1 and db.get_active_version() is None
    db.set_active_version(1)
    assert db.get_active_version() == 1
    with pytest.raises(ValueError):
        db.save_checklist_version(v1.model_copy(update={"version": 0}))


def test_evaluation_unique_key_replaces(db):
    db.save_eval(_ev(answer="yes", explanation="first"))
    db.save_eval(_ev(answer="no", explanation="second"))            # same key → replaced
    db.save_eval(_ev(answer="yes", mode="accurate"))                 # other mode → separate row
    db.save_eval(_ev(answer="yes", pv="p2"))                         # other prompt version → separate row
    assert len(db.list_evals("demo-ru-001")) == 3
    got = db.get_eval("demo-ru-001", "h1", "p1", "fast")
    assert got.answer == "no" and got.explanation == "second" and got.created_at
    with pytest.raises(ValueError):
        db.save_eval(_ev(h=""))


def test_evaluation_update_is_newest_when_timestamps_tie(db):
    stamp = "2026-01-01T00:00:00+00:00"
    db.save_eval(_ev(mode="fast", created_at=stamp))
    db.save_eval(_ev(mode="accurate", created_at=stamp))
    db.save_eval(_ev(mode="fast", answer="no", created_at=stamp))
    assert [ev.mode for ev in db.list_evals()] == ["accurate", "fast"]
    assert db.latest_evals("demo-ru-001", ["h1"])["h1"].answer == "no"


def test_latest_evals_and_planner_keys(db):
    db.save_evals([_ev(cid="C1", h="h1"), _ev(cid="C4", h="h4", answer=15), _ev(cid="C6", h="h6", answer=None),
                   _ev(conv="other", cid="C1", h="h1")])
    latest = db.latest_evals("demo-ru-001", ["h1", "h4", "h6", "hX"])
    assert set(latest) == {"h1", "h4", "h6"} and latest["h4"].answer == 15
    keys = db.existing_eval_keys(["demo-ru-001"], prompt_version="p1", mode="fast")
    assert keys == {("demo-ru-001", "h1"), ("demo-ru-001", "h4")}  # failed eval (answer None) must be redone
    assert db.list_evals(criterion_hashes=[]) == []


def test_appeals_decisions_audit(db):
    a = db.create_appeal(Appeal(id="ap1", conv_id="demo-ru-001", criterion_id="C4", comment="Ответ был полным",
                                message_ids=["demo-ru-001:5"], original_answer=15))
    assert a.created_at and db.list_appeals(status="open")[0].id == "ap1"
    db.add_decision(Decision(id="d1", appeal_id="ap1", conv_id="demo-ru-001", criterion_id="C4", action="change",
                             new_answer=20, old_answer=15, reason="Все вопросы закрыты [5]",
                             created_at="2026-11-07T20:00:00+05:00"))
    db.set_appeal_status("ap1", "decided")
    assert db.get_appeal("ap1").status == "decided" and db.list_appeals(status="open") == []
    with pytest.raises(Exception):
        db.set_appeal_status("ap1", "bogus")
    with pytest.raises(KeyError):
        db.set_appeal_status("nope", "decided")
    decisions = db.list_decisions(conv_id="demo-ru-001")
    assert [d.new_answer for d in decisions] == [20.0]
    db.add_audit("appeal", "ap1", "appeal.created", {"criterion_id": "C4"}, conv_id="demo-ru-001")
    db.add_audit("decision", "d1", "decision.saved", {"old_total": 85.38, "new_total": 93.08}, conv_id="demo-ru-001")
    db.add_audit("checklist", "2", "checklist.created")
    events = db.list_audit(conv_id="demo-ru-001")
    assert [e.event for e in events] == ["appeal.created", "decision.saved"]
    assert events[1].payload["new_total"] == 93.08 and len(db.list_audit()) == 3


def test_llm_calls_and_runs(db):
    db.log_llm_call({"provider": "claude_cli", "model": "claude-opus-5-5", "purpose": "evaluate", "conv_id": "c",
                     "tokens_in": 1000, "tokens_out": 200, "cost_usd": 0.05, "cost_kzt": 25.0, "latency_ms": 4000})
    db.log_llm_call({"provider": "claude_cli", "model": "claude-opus-5-5", "cache_hit": True})
    stats = db.llm_stats()
    assert stats["calls"] == 2 and stats["cache_hits"] == 1 and stats["cost_kzt"] == 25.0
    assert stats["avg_latency_ms"] == 4000
    db.save_run("run-1", "running", {"done": 1, "total": 65, "per_conv": []}, version=2)
    db.save_run("run-1", "completed", {"done": 65, "total": 65, "per_conv": []})
    run = db.get_run("run-1")
    assert run["status"] == "completed" and run["done"] == 65 and run["version"] == 2
    assert [r["run_id"] for r in db.list_runs()] == ["run-1"] and db.get_run("nope") is None


def test_parallel_writers_do_not_lock(db):
    errors = []

    def work(t):
        try:
            for i in range(15):
                db.save_eval(_ev(conv=f"c{t}", cid=f"C{i}", h=f"h{i}"))
                db.log_llm_call({"provider": "mock", "model": "m", "conv_id": f"c{t}"})
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors and len(db.list_evals()) == 8 * 15 and db.llm_stats()["calls"] == 8 * 15


def test_get_db_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_DB", str(tmp_path / "env.sqlite"))
    monkeypatch.setattr(dbmod, "_DEFAULT", None)
    assert get_db().path == tmp_path / "env.sqlite" and get_db() is get_db()
    with pytest.raises(ValueError):
        Database(":memory:")


def test_update_appeal_fields(db):
    db.create_appeal(Appeal(id="ap1", conv_id="c", criterion_id="C4", comment="не согласен"))
    a = db.update_appeal("ap1", status="decided", decision_id="d1", decided_at="2026-11-07T20:00:00+05:00")
    assert a.status == "decided" and a.decision_id == "d1"
    assert db.get_appeal("ap1").decision_id == "d1" and db.list_appeals(status="decided")[0].id == "ap1"
    with pytest.raises(KeyError):
        db.update_appeal("nope", status="decided")
    with pytest.raises(ValueError):
        db.update_appeal("ap1", status="bogus")


def test_dossiers_roundtrip(db):
    assert db.get_dossier("ap1") is None
    db.put_dossier("ap1", {"appeal_id": "ap1", "status": "pending", "arguments_for": []})
    db.put_dossier("ap1", {"appeal_id": "ap1", "status": "ready", "arguments_for": [{"text": "довод"}]})
    d = db.get_dossier("ap1")
    assert d["status"] == "ready" and d["arguments_for"][0]["text"] == "довод" and d["updated_at"]
