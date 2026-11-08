"""Strict review regressions: PROD-01 (control export = its own run), PROD-02 (one decision per appeal),
CQA-02 (exports mark rejected / flagged answers as preliminary), CLI CSV formula guard. Synthetic data only."""
from __future__ import annotations

import copy
import csv
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from cqa.checklist_store import ChecklistStore
from cqa.db import Database
from cqa.models import CriterionEval, Evidence
from cqa.review import ReviewError, ReviewService, export_status, validation_status
from tests.conftest import PDF_EXAMPLE, make_evals, synth_conv
from tests.test_api import SYNTH, _control_module, client  # noqa: F401  (client is a fixture)
from tests.offline_guard import no_network  # noqa: F401

TARGET = "t-ru-002"


@pytest.fixture
def ctl(client, monkeypatch):
    # As in test_control_round2: drop the mock profile so the config default is used; FakeEval still answers.
    monkeypatch.delenv("CQA_PROFILE", raising=False)
    return client


def _export(tmp_path, gid="CTRL"):
    return json.loads((tmp_path / f"control_{gid}.json").read_text(encoding="utf-8"))


def _csv(tmp_path, gid="CTRL"):
    with (tmp_path / f"control_{gid}.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _trace(tmp_path, gid="CTRL"):
    return [json.loads(x) for x in (tmp_path / f"control_{gid}_trace.jsonl").read_text(encoding="utf-8").splitlines()]


def _conv(doc, cid=TARGET):
    return next(c for c in doc["conversations"] if c["conversation_id"] == cid)


# ------------------------------------------------------------------------------------------------ PROD-01
def test_control_export_is_its_own_run_even_if_run_b_and_a_decision_land_before_the_files(ctl, tmp_path):
    client = ctl
    control = _control_module()
    state = client.app.state.cqa
    clean_dir, raced_dir = tmp_path / "clean", tmp_path / "raced"
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=clean_dir)        # reference: run A alone
    reference = _conv(_export(clean_dir))
    real = client.fake.__call__
    orig_start = state.runs.start_run

    def run_b(conv, checklist, criterion_ids, **kw):                                         # later run B: other C4
        out = real(conv, checklist, criterion_ids, **kw)
        for e in out:
            if e["criterion_id"] == "C4":
                e["answer"], e["explanation"] = 10, "ответ прогона B"
        return out

    def start_then_race(*args, **kwargs):
        st_a = orig_start(*args, **kwargs)                       # run A finished ...
        state._evaluate = run_b
        try:
            orig_start(*args, **kwargs)                          # ... run B overwrites the evaluations table ...
        finally:
            state._evaluate = real
        internal = next(r["id"] for r in st_a["per_conv"] if state.conversation(r["id"])["meta"].get("source_id",
                                                                                                    r["id"]) == TARGET)
        appeal = state.review.create_appeal(internal, "C6", "Синтетическая апелляция между прогоном и выгрузкой")
        state.review.decide(appeal.id, "change", 0, "Синтетическое решение после окончания прогона A")   # ... + human
        return st_a

    state.runs.start_run = start_then_race
    try:
        rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=raced_dir)
    finally:
        state.runs.start_run = orig_start
    doc = _export(raced_dir)
    got = _conv(doc)
    by = {r["criterion_id"]: r for r in got["criteria"]}
    assert by["C4"]["answer"] == 20 and "прогона B" not in by["C4"]["explanation"]
    assert by["C6"]["source"] == "model" and by["C6"]["answer"] == {r["criterion_id"]: r for r in
                                                                     reference["criteria"]}["C6"]["answer"]
    assert got["total"] == reference["total"] and rep["mean_total"] is not None
    rows = [r for r in _csv(raced_dir) if r["conversation_id"] == TARGET]
    assert {r["criterion_id"]: r["answer"] for r in rows}["C4"] == "20"
    assert {r["total"] for r in rows} == {str(reference["total"])}
    trace = [t for t in _trace(raced_dir) if t["conversation_id"] == TARGET]
    assert len(trace) == 8 and len({t["run_id"] for t in trace}) == 1
    assert trace[0]["run_id"] == doc["run"]["run_id"]


# ------------------------------------------------------------------------------------------------ PROD-02
@pytest.fixture
def review_env(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    store = ChecklistStore(db)
    v1 = store.active()
    db.upsert_conversation(synth_conv("t-001"))
    db.save_evals(make_evals(v1, PDF_EXAMPLE, conv_id="t-001", mode="fast"))
    return SimpleNamespace(path=tmp_path / "t.sqlite", db=db, store=store, svc=ReviewService(db, store))


def test_two_parallel_decisions_on_one_appeal_only_one_wins(review_env):
    appeal = review_env.svc.create_appeal("t-001", "C4", "Синтетическая апелляция")
    barrier = threading.Barrier(2, timeout=10)
    results: dict[int, object] = {}

    def reviewer(n: int, answer: int) -> None:
        db = Database(review_env.path)                         # independent connection objects, same file
        svc = ReviewService(db, ChecklistStore(db))
        orig = svc._appeal

        def read_then_wait(appeal_id):
            found = orig(appeal_id)
            barrier.wait()                                      # both saw status=open
            return found
        svc._appeal = read_then_wait
        try:
            results[n] = svc.decide(appeal.id, "change", answer, f"Синтетическое решение {n}")
        except ReviewError as exc:
            results[n] = exc

    threads = [threading.Thread(target=reviewer, args=(1, 10)), threading.Thread(target=reviewer, args=(2, 20))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results.values() if not isinstance(r, ReviewError)]
    losses = [r for r in results.values() if isinstance(r, ReviewError)]
    assert len(wins) == 1 and len(losses) == 1 and losses[0].status == 409
    decisions = review_env.db.list_decisions(appeal_id=appeal.id)
    assert len(decisions) == 1 and decisions[0].id == wins[0].decision.id
    stored = review_env.db.get_appeal(appeal.id)
    assert stored.status == "decided" and stored.decision_id == decisions[0].id
    history = {s["step"]: s for s in review_env.svc.history(appeal.id)}
    assert history["decision"]["new_answer"] == decisions[0].new_answer
    assert history["recalculation"]["new_total"] == review_env.svc.conversation_score("t-001").total
    assert len([e for e in review_env.db.list_audit(entity="decision") if e.event == "decision.saved"]) == 1


def test_failure_inside_the_decision_transaction_rolls_everything_back(review_env):
    appeal = review_env.svc.create_appeal("t-001", "C4", "Синтетическая апелляция")
    with sqlite3.connect(review_env.path) as conn:
        conn.execute("CREATE TRIGGER boom BEFORE UPDATE ON appeals BEGIN SELECT RAISE(ABORT, 'synthetic'); END;")
    with pytest.raises(sqlite3.DatabaseError):
        review_env.svc.decide(appeal.id, "change", 20, "Синтетическое решение")
    assert review_env.db.list_decisions(appeal_id=appeal.id) == []
    assert review_env.db.get_appeal(appeal.id).status == "open"
    assert not [e for e in review_env.db.list_audit(entity="decision") if e.event == "decision.saved"]
    with sqlite3.connect(review_env.path) as conn:
        conn.execute("DROP TRIGGER boom")
    assert review_env.svc.decide(appeal.id, "change", 20, "Повтор после сбоя").new_total is not None


def test_decide_appeal_refuses_when_a_decision_already_exists_even_if_status_is_open(review_env):
    appeal = review_env.svc.create_appeal("t-001", "C4", "Синтетическая апелляция")
    review_env.db.add_decision({"id": "legacy", "appeal_id": appeal.id, "action": "confirm", "reason": "x"})
    with pytest.raises(ReviewError) as exc:
        review_env.svc.decide(appeal.id, "change", 20, "Второе решение")
    assert exc.value.status == 409 and len(review_env.db.list_decisions(appeal_id=appeal.id)) == 1


# ------------------------------------------------------------------------------------------------ CQA-02
def _ev(**kw):
    base = dict(conv_id="c", criterion_id="C1", criterion_hash="h", answer="yes",
                evidence=[Evidence(message_id="c:1", quote="q", valid=True)])
    return CriterionEval(**{**base, **kw})


def test_validation_status_separates_rejected_evidence_from_low_confidence():
    assert validation_status(_ev()) == "accepted"
    assert validation_status(_ev(needs_review=True, review_reasons=["низкая уверенность модели 0.40"])) == "needs_review"
    assert validation_status(_ev(evidence=[], needs_review=True,
                                 review_reasons=["ответ модели не прошёл проверку: цитата не найдена"])) == "rejected"
    assert validation_status(_ev(evidence=[Evidence(message_id="c:1", quote="выдумка", valid=False)])) == "rejected"
    assert validation_status(_ev(answer=None)) == "missing" and validation_status(None) == "missing"
    assert export_status("decision", "rejected") == "final" and export_status("model", "rejected") == "preliminary"


def test_control_export_marks_a_rejected_quote_as_preliminary(ctl, tmp_path):
    client = ctl
    control = _control_module()
    state = client.app.state.cqa
    real = client.fake.__call__

    def fabricated(conv, checklist, criterion_ids, **kw):
        out = real(conv, checklist, criterion_ids, **kw)
        if (conv.get("meta") or {}).get("source_id", conv["id"]) == TARGET:
            for e in out:
                if e["criterion_id"] == "C2":
                    e.update(answer="yes", evidence=[], needs_review=True,
                             review_reasons=["ответ модели не прошёл проверку: цитата не найдена дословно"])
        return out

    state._evaluate = fabricated
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    doc = _export(tmp_path)
    bad = _conv(doc)
    c2 = next(r for r in bad["criteria"] if r["criterion_id"] == "C2")
    assert (c2["status"], c2["validation_status"], c2["needs_review"]) == ("preliminary", "rejected", True)
    assert bad["preliminary"] is True and bad["result_status"] == "preliminary"
    assert TARGET in doc["summary"]["preliminary_ids"]
    good = [c for c in doc["conversations"] if c["conversation_id"] != TARGET]
    assert good and all(c["preliminary"] is False and c["result_status"] == "final" for c in good)
    rows = {(r["conversation_id"], r["criterion_id"]): r for r in _csv(tmp_path)}
    assert rows[(TARGET, "C2")]["validation_status"] == "rejected" and rows[(TARGET, "C2")]["preliminary"] == "True"
    assert rows[(TARGET, "C1")]["status"] == "final" and rows[(TARGET, "C1")]["preliminary"] == "True"


def test_api_export_passes_recorded_validation_and_never_calls_unverified_final(tmp_path):
    from api.exports import _csv_content, _export_snapshot
    answers = [
        {"criterion_id": "C1", "answer": "yes", "original_answer": "yes", "source": "model", "points": 50.0,
         "weight": 50.0, "critical_hit": False, "needs_review": True, "validation_status": "rejected",
         "evidence_valid": False},
        {"criterion_id": "C2", "answer": "yes", "original_answer": "yes", "source": "model", "points": 50.0,
         "weight": 50.0, "critical_hit": False, "needs_review": False, "validation_status": "accepted",
         "evidence_valid": True},
    ]
    row = {"id": "c-1", "group_id": "G", "language": "RU", "channel": "chat", "status": "done", "answers": answers,
           "score": {"checklist_version": 1, "total": 100.0, "status": "complete", "critical": False}}
    run = {"run_id": "r", "version": 1, "mode": "fast", "status": "completed", "started_at": "s", "finished_at": "f",
           "per_conv": [row, {**copy.deepcopy(row), "id": "c-2",
                              "answers": [{**a, "needs_review": False, "validation_status": "accepted",
                                           "evidence_valid": True} for a in answers]},
                        {**copy.deepcopy(row), "id": "c-3",
                         "answers": [{k: v for k, v in a.items() if k not in ("needs_review", "validation_status",
                                                                             "evidence_valid")} for a in answers]}]}
    export = _export_snapshot(run)
    by = {c["conv_id"]: c for c in export["per_conv"]}
    assert by["c-1"]["preliminary"] is True and by["c-1"]["preliminary_criteria"] == ["C1"]
    assert by["c-1"]["answers"][0]["validation_status"] == "rejected"
    assert by["c-2"]["preliminary"] is False
    assert by["c-3"]["preliminary"] is True and set(by["c-3"]["answer_status"].values()) == {"unverified"}
    assert export["preliminary_conversations"] == ["c-1", "c-3"]
    text = _csv_content(export).decode("utf-8-sig")
    assert "rejected" not in text and ",\"preliminary\"" in text.splitlines()[0] + ","


# ------------------------------------------------------------------------------------------------ P2 CSV guard
def test_cli_csv_neutralizes_formula_cells(ctl, tmp_path):
    client = ctl
    control = _control_module()
    state = client.app.state.cqa
    real = client.fake.__call__

    def formula(conv, checklist, criterion_ids, **kw):
        out = real(conv, checklist, criterion_ids, **kw)
        for e in out:
            e["explanation"] = '=HYPERLINK("http://example.invalid","x")'
        return out

    state._evaluate = formula
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    rows = _csv(tmp_path)
    assert rows and all(r["explanation"].startswith("'=") for r in rows)
    assert control._csv_safe("-1") == "'-1" and control._csv_safe("\t@x") == "'\t@x" and control._csv_safe(5) == 5
    assert _export(tmp_path)["conversations"][0]["criteria"][0]["explanation"].startswith("=")   # JSON verbatim
