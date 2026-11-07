"""Run provenance and failure regressions. All dialogues are authored synthetic markers; no network."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from cqa import llm
from cqa.checklist_store import ChecklistStore
from cqa.db import ConversationConflictError, Database
from cqa.models import Conversation, Decision
from cqa.runs import RunManager, current_evals, plan
from tests.conftest import FakeEvaluator


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # Pin configuration without reading/changing the user's environment or calling a provider.
    monkeypatch.setattr(llm, "active_profile", lambda: "cloud")
    monkeypatch.setattr(llm, "config", lambda: {"default_profile": "cloud"})
    db = Database(tmp_path / "integrity.sqlite")
    store = ChecklistStore(db)
    conv = Conversation(id="synthetic-1", channel="chat", language="EN", group_id="TEST",
                        messages=[{"id": "synthetic-1:0", "idx": 0, "speaker": "operator",
                                   "text": "SYNTHETIC_INPUT_MARKER", "ts_raw": "00:00", "t_sec": 0}])
    db.upsert_conversation(conv)
    fake = FakeEvaluator()
    manager = RunManager(db, store, evaluate=fake, workers=1, prompt_version="fake-v1")
    return SimpleNamespace(db=db, store=store, conv=conv, fake=fake, manager=manager)


def _run(ctx, **kwargs):
    return ctx.manager.start_run(wait=True, **kwargs)


def _row(run):
    return run["per_conv"][0]


def _version(ctx):
    return ctx.store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 20}]).checklist


def test_live_reuses_unchanged_stored_answers_without_force(ctx):
    # live = no LLM response cache for evaluated pairs; it must not throw away stored answers of unchanged criteria
    # (applying a version with only a weight change through the board stayed 64 pairs instead of 0 -- m3 bug).
    first = _run(ctx)
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return ctx.fake(*args, **kwargs)

    ctx.manager.evaluate = evaluate
    live = _run(ctx, live=True)
    assert live["status"] == "completed" and live["force"] is False and live["live"] is True
    assert live["pairs_to_evaluate"] == 0 and live["evaluated"] == 0 and calls == []
    assert live["from_cache"] == live["pairs_total"]
    assert _row(live)["new_total"] == _row(first)["new_total"]


def test_live_with_force_bypasses_both_caches_after_success(ctx):
    first = _run(ctx)
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return ctx.fake(*args, **kwargs)

    ctx.manager.evaluate = evaluate
    ctx.fake.answers = {ctx.conv.id: {"C1": "no"}}
    live = _run(ctx, live=True, force=True)
    assert live["status"] == "completed" and live["force"] is True
    assert live["pairs_to_evaluate"] == live["pairs_total"] == live["evaluated"]
    assert live["from_cache"] == 0 and live["llm_calls"] == 1
    assert calls[0]["no_cache"] is True
    assert set(calls[0]["only_criteria"]) == set(ctx.store.active().by_id)
    assert _row(live)["old_total"] == _row(first)["new_total"]
    assert _row(live)["new_total"] < _row(live)["old_total"]


@pytest.mark.parametrize("provider,model", [("mock", "fake"), (None, "mock:synthetic")])
@pytest.mark.parametrize("has_usage", [True, False])
def test_mock_evaluations_are_simulated_not_network_calls(ctx, provider, model, has_usage):
    def evaluate(*args, **kwargs):
        return [ev.model_copy(update={"provider": provider, "model": model,
                                      "raw": {"calls": 3, "cache_hits": 1} if has_usage else {}})
                for ev in ctx.fake(*args, **kwargs)]

    ctx.manager.evaluate = evaluate
    first = _run(ctx, profile="mock", live=True)
    criteria = len(ctx.store.active().criteria)
    assert first["status"] == "completed" and first["llm_calls"] == 0
    assert first["simulated_evaluations"] == first["evaluated"] == criteria
    assert _row(first)["simulated_evaluations"] == criteria
    saved = ctx.db.get_run(first["run_id"])
    assert saved["simulated_evaluations"] == criteria and saved["llm_calls"] == 0
    completed = [e for e in ctx.db.list_audit() if e.event == "run.completed"][-1]
    assert completed.payload["simulated_evaluations"] == criteria and completed.payload["llm_calls"] == 0
    cached = _run(ctx, profile="mock")
    assert cached["from_cache"] == criteria
    assert cached["simulated_evaluations"] == cached["llm_calls"] == 0
    assert _row(cached)["simulated_evaluations"] == 0


@pytest.mark.parametrize("real_usage,expected_calls", [({"calls": 3, "cache_hits": 1}, 2), ({}, 1)])
def test_mixed_mock_and_real_evaluations_keep_separate_accounting(ctx, real_usage, expected_calls):
    def evaluate(*args, **kwargs):
        out = ctx.fake(*args, **kwargs)
        return [out[0].model_copy(update={"provider": "synthetic-provider", "raw": real_usage}),
                *[ev.model_copy(update={"provider": "mock", "raw": {"calls": 100, "cache_hits": 0}})
                  for ev in out[1:]]]

    ctx.manager.evaluate = evaluate
    result = _run(ctx)
    assert result["status"] == "completed" and result["llm_calls"] == expected_calls
    assert result["simulated_evaluations"] == result["evaluated"] - 1


@pytest.mark.parametrize("failure", ["empty", "missing", "null", "invalid", "malformed", "duplicate",
                                      "foreign_conv", "foreign_hash", "foreign_mode", "foreign_prompt"])
def test_failed_refresh_never_scores_old_answers_or_activates(ctx, failure):
    _run(ctx)
    before = ctx.db.list_evals(ctx.conv.id)
    version = _version(ctx)

    def evaluate(*args, **kwargs):
        out = ctx.fake(*args, **kwargs)
        if failure == "empty":
            return []
        if failure == "missing":
            return out[1:]
        if failure == "malformed":
            return [{"answer": "unparseable"}, *out[1:]]
        if failure == "duplicate":
            return [*out, out[0]]
        changes = {"null": {"answer": None}, "invalid": {"answer": "na"},
                   "foreign_conv": {"conv_id": "other-synthetic"}, "foreign_hash": {"criterion_hash": "other"},
                   "foreign_mode": {"mode": "fast:local"}, "foreign_prompt": {"prompt_version": "other"}}
        return [out[0].model_copy(update=changes[failure]), *out[1:]]

    ctx.manager.evaluate = evaluate
    failed = _run(ctx, version=version.version, live=True, force=True, activate=True)
    assert failed["status"] == "failed" and failed["activated"] is False
    assert ctx.store.active_version() == 1
    row = _row(failed)
    assert row["status"] == "error" and row["old_total"] == 100
    assert row["new_total"] is None and row["score"] is None and row["answers"] == []
    assert failed["errors"] and failed["errors"][0]["conv_id"] == ctx.conv.id
    assert ctx.db.get_run(failed["run_id"])["status"] == "failed"
    if failure != "duplicate":
        assert ctx.db.get_eval(ctx.conv.id, before[0].criterion_hash, "fake-v1", "fast") == before[0]


@pytest.mark.parametrize("failure", ["exception", "missing"])
def test_one_failed_dialogue_blocks_whole_run_activation(ctx, failure):
    other = ctx.conv.model_copy(deep=True, update={"id": "synthetic-2"})
    ctx.db.upsert_conversation(other)
    version = _version(ctx)

    def evaluate(conv, *args, **kwargs):
        out = ctx.fake(conv, *args, **kwargs)
        if conv.id == other.id:
            if failure == "exception":
                raise RuntimeError("synthetic provider failure")
            return out[:-1]  # missing the zero-weight critical criterion is still an error
        return out

    ctx.manager.evaluate = evaluate
    failed = _run(ctx, version=version.version, activate=True)
    assert failed["status"] == "failed" and not failed["activated"]
    assert failed["done"] == failed["total"] == 2
    assert [row["status"] for row in failed["per_conv"]] == ["done", "error"]
    assert ctx.store.active_version() == 1 and len(failed["errors"]) == 1
    ctx.manager.evaluate = ctx.fake
    retried = _run(ctx, version=version.version, activate=True)
    assert retried["status"] == "completed" and retried["activated"]
    assert ctx.store.active_version() == version.version


def test_reviewer_decision_cannot_hide_failed_fresh_evaluation(ctx):
    _run(ctx)
    _decision(ctx, "d1", "no")
    ctx.manager.evaluate = lambda *args, **kwargs: []
    failed = _run(ctx, live=True, force=True)
    assert failed["status"] == "failed" and _row(failed)["new_total"] is None


def test_profiles_are_isolated_and_namespaced_modes_reuse_correctly(ctx):
    cloud = _run(ctx, mode="accurate", profile="cloud")
    ctx.fake.answers = {ctx.conv.id: {"C1": "no"}}
    local = _run(ctx, mode="accurate", profile="local")
    assert local["mode"] == "accurate:local" and local["from_cache"] == 0
    assert _row(local)["old_total"] is None
    assert _row(local)["new_total"] < _row(cloud)["new_total"]
    assert {ev.mode for ev in ctx.db.list_evals(ctx.conv.id)} == {"accurate", "accurate:local"}
    calls = len(ctx.fake.calls)
    cached = _run(ctx, mode="fast:local", profile="local")
    assert len(ctx.fake.calls) == calls and cached["from_cache"] == len(ctx.store.active().criteria)
    assert _row(cached)["new_total"] == _row(local)["new_total"]
    assert all(call[2] == "accurate" for call in ctx.fake.calls)
    again = _run(ctx, mode="fast", profile="cloud")
    assert _row(again)["old_total"] == _row(again)["new_total"] == 100
    assert plan(ctx.db, ctx.store.active(), [ctx.conv.id], "fast", profile="local")[0]["evaluate"] == []


def test_local_accurate_does_not_reuse_local_fast(ctx):
    _run(ctx, profile="local")
    accurate = _run(ctx, mode="accurate:local")
    assert accurate["from_cache"] == 0 and accurate["evaluated"] == len(ctx.store.active().criteria)
    assert _row(accurate)["old_total"] == 100
    assert {ev.mode for ev in ctx.db.list_evals(ctx.conv.id)} == {"fast:local", "accurate:local"}


@pytest.mark.parametrize("profile", ["cloud", "local"])
def test_newest_eligible_mode_transitions_and_same_profile_baseline(ctx, profile):
    # Equal timestamps also exercise the database's deterministic write-order tie breaker.
    def evaluate(*args, **kwargs):
        return [ev.model_copy(update={"created_at": "2026-01-01T00:00:00+00:00"})
                for ev in ctx.fake(*args, **kwargs)]

    ctx.manager.evaluate = evaluate
    first = _run(ctx, mode="fast", profile=profile)
    assert _row(first)["new_total"] == 100
    ctx.fake.answers = {ctx.conv.id: {"C6": 7}}
    accurate = _run(ctx, mode="accurate", profile=profile)
    assert (_row(accurate)["old_total"], _row(accurate)["new_total"]) == (100, 95.5)
    assert accurate["from_cache"] == 0

    # A still newer answer from the other profile must not affect reuse or the baseline.
    ctx.fake.answers = {ctx.conv.id: {"C1": "no"}}
    _run(ctx, mode="fast", profile="local" if profile == "cloud" else "cloud")
    calls = len(ctx.fake.calls)
    cached = _run(ctx, mode="fast", profile=profile)
    assert len(ctx.fake.calls) == calls
    assert cached["from_cache"] == cached["pairs_total"]
    assert (_row(cached)["old_total"], _row(cached)["new_total"]) == (95.5, 95.5)

    fresh = _run(ctx, mode="fast", profile=profile, live=True, force=True)
    assert len(ctx.fake.calls) == calls + 1 and fresh["from_cache"] == 0
    assert (_row(fresh)["old_total"], _row(fresh)["new_total"]) == (95.5, 90)
    cached = _run(ctx, mode="fast", profile=profile)
    assert (_row(cached)["old_total"], _row(cached)["new_total"]) == (90, 90)
    assert len(ctx.fake.calls) == calls + 1

    # Accurate reuse remains restricted to accurate; its "before" still shows the latest profile score.
    cached_accurate = _run(ctx, mode="accurate", profile=profile)
    assert (_row(cached_accurate)["old_total"], _row(cached_accurate)["new_total"]) == (90, 95.5)
    assert len(ctx.fake.calls) == calls + 1


def test_profile_is_frozen_before_background_execution(ctx, monkeypatch):
    monkeypatch.setattr(llm, "active_profile", lambda: "local")
    observed = []

    def evaluate(*args, **kwargs):
        monkeypatch.setattr(llm, "active_profile", lambda: "cloud")
        observed.append(kwargs["profile"])
        return ctx.fake(*args, **kwargs)

    ctx.manager.evaluate = evaluate
    result = _run(ctx)
    assert result["profile"] == "local" and result["mode"] == "fast:local"
    assert observed == ["local"] and all(ev.mode == "fast:local" for ev in ctx.db.list_evals())


def test_latest_fast_result_wins_over_older_accurate_even_in_same_second(ctx):
    def evaluate(*args, **kwargs):
        return [ev.model_copy(update={"created_at": "2026-01-01T00:00:00+00:00"})
                for ev in ctx.fake(*args, **kwargs)]

    ctx.manager.evaluate = evaluate
    _run(ctx, mode="fast")
    accurate = _run(ctx, mode="accurate")
    ctx.fake.answers = {ctx.conv.id: {"C1": "no"}}
    fast = _run(ctx, mode="fast", live=True, force=True)
    assert _row(fast)["old_total"] == _row(accurate)["new_total"] == 100
    assert _row(fast)["new_total"] < 100
    newest = current_evals(ctx.db, ctx.conv.id, ctx.store.active())
    assert {ev.mode for ev in newest} == {"fast"}
    again = _run(ctx, mode="fast")
    assert _row(again)["new_total"] == _row(fast)["new_total"]
    assert next(ev.answer for ev in newest if ev.criterion_id == "C1") == "no"


def test_changed_prompt_uses_new_results_and_keeps_old_baseline(ctx):
    _run(ctx)
    ctx.fake.answers = {ctx.conv.id: {"C1": "no"}}
    ctx.manager.prompt_version = "fake-v2"

    def evaluate(*args, **kwargs):
        return [ev.model_copy(update={"prompt_version": "fake-v2"}) for ev in ctx.fake(*args, **kwargs)]

    ctx.manager.evaluate = evaluate
    changed = _run(ctx)
    assert changed["from_cache"] == 0 and changed["status"] == "completed"
    assert _row(changed)["old_total"] == 100 and _row(changed)["new_total"] < 100
    assert {ev.prompt_version for ev in ctx.db.list_evals()} == {"fake-v1", "fake-v2"}


@pytest.mark.parametrize("bad_prompt,bad_answer", [("", "yes"), ("fake-v1", "na")])
def test_legacy_invalid_cache_entries_are_not_reused(ctx, bad_prompt, bad_answer):
    ev = ctx.fake(ctx.conv, ctx.store.active(), only_criteria=["C1"])[0]
    ctx.db.save_eval(ev.model_copy(update={"prompt_version": bad_prompt, "answer": bad_answer}))
    planned = plan(ctx.db, ctx.store.active(), [ctx.conv.id], prompt_version="fake-v1")[0]
    assert "C1" in planned["evaluate"]
    result = _run(ctx)
    assert result["status"] == "completed" and result["from_cache"] == 0


def test_empty_explicit_selection_rejects_without_creating_a_run(ctx):
    with pytest.raises(ValueError, match="conv_ids"):
        _run(ctx, conv_ids=[])
    assert ctx.fake.calls == [] and ctx.db.list_runs() == []


def test_empty_group_cannot_activate_unvalidated_checklist(ctx):
    version = _version(ctx)
    result = _run(ctx, version=version.version, group_id="NO_MATCH", activate=True)
    assert result["total"] == 0 and not result["activated"]
    assert ctx.store.active_version() == 1 and not ctx.fake.calls


def test_contradictory_profile_and_mode_reject_before_work(ctx):
    with pytest.raises(ValueError):
        _run(ctx, mode="fast:local", profile="cloud")
    assert not ctx.fake.calls and not ctx.db.list_runs()


def _decision(ctx, did, answer):
    return ctx.db.add_decision(Decision(
        id=did, appeal_id="synthetic-appeal", conv_id=ctx.conv.id, criterion_id="C1",
        criterion_hash=ctx.store.active().criterion("C1").hash, action="change", new_answer=answer,
        reason="synthetic review", created_at=f"2026-01-01T00:00:0{did[-1]}+00:00"))


def test_run_export_snapshot_survives_reruns_decisions_and_restart(ctx):
    _decision(ctx, "d1", "no")
    first = _run(ctx)
    snapshot = copy.deepcopy(first)
    saved = ctx.db.get_run(first["run_id"])
    row = _row(first)
    assert row["group_id"] == "TEST" and row["language"] == "EN" and row["channel"] == "chat"
    assert row["score"]["total"] == row["new_total"]
    c1 = next(a for a in row["answers"] if a["criterion_id"] == "C1")
    assert set(c1) == {"criterion_id", "answer", "original_answer", "source", "points", "weight", "critical_hit"}
    assert c1["answer"] == "no" and c1["original_answer"] == "yes" and c1["source"] == "decision"
    assert "SYNTHETIC_INPUT_MARKER" not in json.dumps(row)
    assert "raw" not in row and "messages" not in row
    _decision(ctx, "d2", "yes")
    second = _run(ctx, live=True)
    assert _row(second)["new_total"] == 100 and row["new_total"] < 100
    assert ctx.manager.status(first["run_id"]) == snapshot
    assert ctx.db.get_run(first["run_id"]) == saved
    reopened = RunManager(Database(ctx.db.path), ctx.store, evaluate=ctx.fake)
    assert reopened.status(first["run_id"]) == saved
    assert saved["per_conv"] == snapshot["per_conv"]
    # Public status objects must not alias mutable internal export arrays/dictionaries.
    row["score"]["lines"][0]["answer"] = "corrupt"
    row["answers"][0]["answer"] = "corrupt"
    first["errors"].append({"error": "corrupt"})
    assert ctx.manager.status(first["run_id"]) == snapshot


def test_transcript_cannot_be_replaced_during_inflight_run(ctx):
    entered, release = threading.Event(), threading.Event()

    def evaluate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return ctx.fake(*args, **kwargs)

    ctx.manager.evaluate = evaluate
    result = []
    thread = threading.Thread(target=lambda: result.append(_run(ctx)))
    thread.start()
    try:
        assert entered.wait(5)
        changed = ctx.conv.model_copy(deep=True)
        changed.messages[0].text = "SYNTHETIC_REPLACEMENT_MARKER"
        with pytest.raises(ConversationConflictError):
            ctx.db.upsert_conversation(changed)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and result[0]["status"] == "completed"
    assert ctx.db.get_conversation(ctx.conv.id) == ctx.conv


def test_baseline_and_decisions_are_frozen_before_queued_worker(ctx):
    _run(ctx)
    entered, release = threading.Event(), threading.Event()
    original_one = ctx.manager._one

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original_one(*args)

    ctx.manager._one = blocked
    result = []
    thread = threading.Thread(target=lambda: result.append(_run(ctx)))
    thread.start()
    try:
        assert entered.wait(5)
        _decision(ctx, "d1", "no")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert _row(result[0])["old_total"] == _row(result[0])["new_total"] == 100
    assert all(a["source"] == "model" for a in _row(result[0])["answers"])
