"""CQA-02 follow-up (verifier refutation): the structured evaluator status reaches every export.

(1) cqa.review.validation_status reads the declared CriterionEval.validation_status / provisional fields;
(2) RunManager records validation_status per answer in the run snapshot, so the API export tells a clean
    run (final) from one with a rejected answer (preliminary) instead of calling everything "unverified";
(3) the CLI dialogue status is "provisional", not "complete", next to preliminary=true.
Synthetic data only (invented dialogues and quotes), no network, no paid LLM calls."""
from __future__ import annotations

import csv
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.exports import router
from api.repo import Repo
from cqa.checklist_store import ChecklistStore
from cqa.db import Database
from cqa.models import CriterionEval, Evidence
from cqa.review import validation_status
from cqa.runs import RunManager
from tests.conftest import FakeEvaluator, synth_conv
from tests.offline_guard import no_network  # noqa: F401
from tests.test_api import SYNTH, _control_module, client  # noqa: F401  (client is a fixture)


def _ev(**kw):
    base = dict(conv_id="c", criterion_id="C1", criterion_hash="h", answer="yes",
                evidence=[Evidence(message_id="c:1", quote="выдуманная фраза", valid=True)])
    return CriterionEval(**{**base, **kw})


# ------------------------------------------------------------------------------------------------ (1)
def test_declared_validation_status_wins_over_reason_heuristics():
    # rejected + provisional with NO reason prefix used to come out as accepted/final
    assert validation_status(_ev(validation_status="rejected", provisional=True)) == "rejected"
    assert validation_status(_ev(provisional=True)) == "rejected"
    # evaluator-rejected N/A without a reason is rejected, not merely needs_review
    assert validation_status(_ev(answer="na", evidence=[], needs_review=True, validation_status="rejected",
                                 provisional=True)) == "rejected"
    assert validation_status(_ev(validation_status="failed")) == "missing"
    assert validation_status(_ev(validation_status="valid")) == "accepted"
    assert validation_status(_ev(validation_status="valid", needs_review=True)) == "needs_review"
    # the evaluator's raw vocabulary never leaks: only accepted | needs_review | rejected | missing
    for raw in ("valid", "rejected", "failed", None):
        assert validation_status(_ev(validation_status=raw)) in ("accepted", "needs_review", "rejected", "missing")
    # legacy evaluations (no structured field) keep the reason-prefix behaviour
    assert validation_status(_ev(review_reasons=["нет доказательства в тексте"])) == "rejected"
    assert validation_status(_ev()) == "accepted"


# ------------------------------------------------------------------------------------------------ (2)
REJECTED_CONV, CLEAN_CONV = "t-002", "t-003"


class _RejectingEvaluator(FakeEvaluator):
    """FakeEvaluator, but C1 of one dialogue carries a fabricated quote the validator rejected."""

    def __call__(self, conversation, checklist_version, mode="fast", only_criteria=None, **kw):
        out = super().__call__(conversation, checklist_version, mode, only_criteria, **kw)
        if conversation.id != REJECTED_CONV:
            return out
        return [ev.model_copy(update=dict(
            validation_status="rejected", provisional=True, needs_review=True,
            evidence=[Evidence(message_id=f"{conversation.id}:1", quote="цитаты нет в диалоге", valid=False)]))
            if ev.criterion_id == "C1" else ev for ev in out]


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_PROFILE", "mock")
    db = Database(tmp_path / "cqa02.sqlite")
    store = ChecklistStore(db)
    for cid in (REJECTED_CONV, CLEAN_CONV):
        db.upsert_conversation(synth_conv(cid, "RU", "chat"))
    mgr = RunManager(db, store, evaluate=_RejectingEvaluator(), workers=2)
    app = FastAPI()
    app.state.cqa = SimpleNamespace(repo=Repo(db))
    app.include_router(router, prefix="/api")
    with TestClient(app) as http:
        yield SimpleNamespace(db=db, mgr=mgr, http=http)


def test_run_snapshot_records_validation_and_api_export_separates_clean_from_rejected(run_env):
    st = run_env.mgr.start_run(wait=True)
    assert st["status"] == "completed"
    saved = {row["id"]: row for row in run_env.db.get_run(st["run_id"])["per_conv"]}
    states = {a["criterion_id"]: a["validation_status"] for a in saved[REJECTED_CONV]["answers"]}
    assert states["C1"] == "rejected" and saved[REJECTED_CONV]["answers"][0]["evidence_valid"] is False
    assert {a["validation_status"] for a in saved[CLEAN_CONV]["answers"]} == {"accepted"}

    response = run_env.http.get(f"/api/runs/{st['run_id']}/export", params={"format": "json"})
    assert response.status_code == 200
    by = {c["conv_id"]: c for c in response.json()["per_conv"]}
    clean, rejected = by[CLEAN_CONV], by[REJECTED_CONV]
    assert clean["preliminary"] is False and clean["result_status"] == "final"
    assert set(clean["answer_status"].values()) == {"final"} and clean["preliminary_criteria"] == []
    assert rejected["preliminary"] is True and rejected["result_status"] == "preliminary"
    assert rejected["answer_status"]["C1"] == "preliminary" and rejected["preliminary_criteria"] == ["C1"]
    assert rejected["answer_status"]["C2"] == "final"          # one bad answer does not taint the others
    assert response.json()["preliminary_conversations"] == [REJECTED_CONV]

    csv_text = run_env.http.get(f"/api/runs/{st['run_id']}/export", params={"format": "csv"}).content.decode("utf-8-sig")
    rows = {(r["conv_id"], r["criterion_id"]): r for r in csv.DictReader(io.StringIO(csv_text, newline=""))}
    assert (rows[(REJECTED_CONV, "C1")]["answer_status"], rows[(REJECTED_CONV, "C1")]["preliminary"]) == (
        "preliminary", "True")
    assert (rows[(CLEAN_CONV, "C1")]["answer_status"], rows[(CLEAN_CONV, "C1")]["preliminary"]) == ("final", "False")


# ------------------------------------------------------------------------------------------------ (3)
TARGET = "t-ru-002"


def test_cli_dialogue_status_is_provisional_next_to_a_structured_rejection(client, tmp_path, monkeypatch):
    monkeypatch.delenv("CQA_PROFILE", raising=False)
    control = _control_module()
    state = client.app.state.cqa
    real = client.fake.__call__

    def structured_reject(conv, checklist, criterion_ids, **kw):
        out = real(conv, checklist, criterion_ids, **kw)
        if (conv.get("meta") or {}).get("source_id", conv["id"]) == TARGET:
            for e in out:
                if e["criterion_id"] == "C2":   # structured rejection only: no reason prefix to pattern-match
                    e.update(answer="yes", evidence=[], validation_status="rejected", provisional=True)
        return out

    state._evaluate = structured_reject
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    doc = json.loads((tmp_path / "control_CTRL.json").read_text(encoding="utf-8"))
    bad = next(c for c in doc["conversations"] if c["conversation_id"] == TARGET)
    c2 = next(r for r in bad["criteria"] if r["criterion_id"] == "C2")
    assert (c2["validation_status"], c2["status"]) == ("rejected", "preliminary")
    assert bad["preliminary"] is True and bad["status"] == "provisional" and bad["score_status"] == "complete"
    good = [c for c in doc["conversations"] if c["conversation_id"] != TARGET]
    assert good and all(c["status"] == "complete" and c["preliminary"] is False for c in good)
    assert doc["summary"]["failed"] == 0                         # provisional is not a failure
