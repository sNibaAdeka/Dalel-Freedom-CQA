"""cqa/runs.py — planner (what to evaluate), background runs with progress in the DB, preview, safety."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from cqa.checklist_store import ChecklistStore
from cqa.db import Database
from cqa.runs import RunManager, pick_preview, plan
from tests.conftest import FakeEvaluator, synth_conv
from tests.offline_guard import no_network as _no_network

IDS = ["t-001", "t-002", "t-003", "t-004"]
STRICT_C5 = {"title": "Проактивность", "description": "СТРОГО: оператор сам предложил следующий шаг.",
             "answer_type": "binary"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_PROFILE", "mock")
    db = Database(tmp_path / "t.sqlite")
    store = ChecklistStore(db)
    for cid, lang, ch in (("t-001", "RU", "chat"), ("t-002", "KZ", "chat"), ("t-003", "EN", "chat"),
                          ("t-004", "RU", "call")):
        db.upsert_conversation(synth_conv(cid, lang, ch))
    fake = FakeEvaluator()
    mgr = RunManager(db, store, evaluate=fake, workers=4)
    return SimpleNamespace(db=db, store=store, fake=fake, mgr=mgr, profile="mock")


def _row(st, cid):
    return next(p for p in st["per_conv"] if p["id"] == cid)


def _evaluated(fake, since=0):
    return sorted((c[0], c[1]) for c in fake.calls[since:])


# ------------------------------------------------------------------ planner

def test_plan_new_dialogs_evaluate_everything(env):
    rows = plan(env.db, env.store.active(), IDS, mode="fast", profile=env.profile)
    assert [r["conv_id"] for r in rows] == IDS
    assert all(len(r["evaluate"]) == 8 and r["reuse"] == [] for r in rows)


def test_first_run_is_live_and_progress_is_stored(env):
    st = env.mgr.start_run(wait=True)
    assert st["profile"] == env.profile and st["mode"] == "fast:mock"
    assert st["status"] == "completed" and st["done"] == st["total"] == 4 and st["from_cache"] == 0
    assert {"run_id", "done", "total", "from_cache", "llm_calls", "cost_kzt", "elapsed", "status", "per_conv"} <= set(st)
    assert sorted(c[0] for c in env.fake.calls) == IDS and all(len(c[1]) == 8 for c in env.fake.calls)
    assert st["llm_calls"] == 4 and st["cost_kzt"] == 16.0                   # 1 call and 8 × 0.5 ₸ per dialogue
    t1 = _row(st, "t-001")
    assert (t1["old_total"], t1["new_total"], t1["status"]) == (None, 85.38, "done")
    saved = env.db.get_run(st["run_id"])
    assert saved["status"] == "completed" and saved["done"] == 4 and len(saved["per_conv"]) == 4
    assert env.mgr.status(st["run_id"])["done"] == 4


def test_rerun_reuses_everything(env):
    first = env.mgr.start_run(wait=True)
    n = len(env.fake.calls)
    st = env.mgr.start_run(wait=True)
    assert len(env.fake.calls) == n and st["from_cache"] == 4 * 8 and st["llm_calls"] == 0 and st["cost_kzt"] == 0
    assert [p["new_total"] for p in st["per_conv"]] == [p["new_total"] for p in first["per_conv"]]


def test_weight_change_costs_zero_calls_and_rescores(env):
    env.mgr.start_run(wait=True)
    n = len(env.fake.calls)
    v2 = env.store.apply([{"op": "set_weight", "criterion_id": "C1", "weight": 20}]).checklist
    assert all(r["evaluate"] == [] for r in plan(env.db, v2, IDS, profile=env.profile))
    st = env.mgr.start_run(version=v2.version, wait=True)
    assert len(env.fake.calls) == n and st["llm_calls"] == 0 and st["from_cache"] == 4 * 8
    t1 = _row(st, "t-001")
    assert (t1["old_total"], t1["new_total"]) == (85.38, 87.33)             # old = active v1, new = v2 weights


def test_replacing_one_criterion_reevaluates_only_it_on_all_dialogs(env):
    env.mgr.start_run(wait=True)
    n = len(env.fake.calls)
    v2 = env.store.apply([{"op": "replace", "criterion_id": "C5", "criterion": STRICT_C5}]).checklist
    rows = plan(env.db, v2, IDS, profile=env.profile)
    assert all(r["evaluate"] == ["C5"] and len(r["reuse"]) == 7 for r in rows)
    st = env.mgr.start_run(version=v2.version, wait=True)
    assert _evaluated(env.fake, n) == [(cid, ("C5",)) for cid in IDS]
    assert st["from_cache"] == 4 * 7 and st["llm_calls"] == 4
    assert (_row(st, "t-001")["old_total"], _row(st, "t-001")["new_total"]) == (85.38, 62.31)  # C5 «Нет»


def test_new_dialogs_are_evaluated_in_full(env):
    env.mgr.start_run(wait=True)
    n = len(env.fake.calls)
    env.db.upsert_conversation(synth_conv("t-005", "EN"))
    st = env.mgr.start_run(wait=True)
    assert [(c[0], len(c[1])) for c in env.fake.calls[n:]] == [("t-005", 8)]
    assert st["from_cache"] == 4 * 8 and _row(st, "t-005")["new_total"] == 100.0


def test_preview_five_dialogs_never_activates(env):
    for i in range(5, 9):
        env.db.upsert_conversation(synth_conv(f"t-00{i}"))
    v2 = env.store.apply([{"op": "replace", "criterion_id": "C5", "criterion": STRICT_C5}]).checklist
    st = env.mgr.start_run(version=v2.version, preview=True, activate=True, wait=True)
    assert st["total"] == 5 and st["preview"] is True and env.store.active_version() == 1
    ids = [p["id"] for p in st["per_conv"]]
    assert {"t-002", "t-003", "t-004"} <= set(ids)                            # KZ, EN and the call are sampled
    full = env.mgr.start_run(version=v2.version, activate=True, wait=True)
    assert full["total"] == 8 and full["activated"] is True and env.store.active_version() == v2.version


def test_pick_preview_is_diverse_and_deterministic(env):
    convs = env.db.list_conversations()
    assert pick_preview(convs) == pick_preview(list(reversed(convs)))
    assert set(pick_preview(convs, 3)) == {"t-004", "t-002", "t-003"}        # call, KZ, EN first


def test_parallel_runs_are_safe(env):
    env.fake.delay = 0.05
    results, errors = [], []

    def go():
        try:
            results.append(env.mgr.start_run(wait=True))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and all(r["status"] == "completed" for r in results)
    assert sorted(c[0] for c in env.fake.calls) == IDS                        # each dialogue evaluated once
    assert all(_row(r, "t-001")["new_total"] == 85.38 for r in results)
    assert len(env.db.list_evals()) == 4 * 8


def test_evaluator_failure_marks_one_dialogue_and_retries_next_run(env):
    env.fake.fail_for = {"t-002"}
    st = env.mgr.start_run(wait=True)
    assert st["status"] == "failed" and _row(st, "t-002")["status"] == "error"
    assert "модель недоступна" in _row(st, "t-002")["error"] and _row(st, "t-001")["status"] == "done"
    env.fake.fail_for = set()
    n = len(env.fake.calls)
    env.mgr.start_run(wait=True)
    assert [(c[0], len(c[1])) for c in env.fake.calls[n:]] == [("t-002", 8)]


def test_invalid_answer_from_evaluator_is_stored_unanswered(env):
    env.fake.answers = {"t-001": {"C1": "na"}}                               # N/A is not allowed for C1
    st = env.mgr.start_run(conv_ids=["t-001"], wait=True)
    h = env.store.active().criterion("C1").hash
    ev = env.db.list_evals("t-001", [h])[-1]
    assert ev.answer is None and ev.needs_review and any("N/A" in r for r in ev.review_reasons)
    assert _row(st, "t-001")["new_total"] is None and _row(st, "t-001")["status"] == "error"
    env.fake.answers = {}
    n = len(env.fake.calls)
    env.mgr.start_run(conv_ids=["t-001"], wait=True)
    assert [c[1] for c in env.fake.calls[n:]] == [("C1",)]


def test_mode_reuse_rules(env):
    env.mgr.start_run(mode="fast", wait=True)
    n = len(env.fake.calls)
    acc = env.mgr.start_run(mode="accurate", wait=True)                      # accurate never reuses fast answers
    assert len(env.fake.calls) == n + 4 and acc["from_cache"] == 0
    assert all(c[2] == "accurate" for c in env.fake.calls[n:])
    fast = env.mgr.start_run(mode="fast", wait=True)                         # fast may reuse accurate answers
    assert len(env.fake.calls) == n + 4 and fast["from_cache"] == 4 * 8


def test_prompt_version_change_reevaluates(env, tmp_path):
    env.mgr.start_run(wait=True)
    n = len(env.fake.calls)
    def evaluate_v2(*args, **kwargs):
        return [ev.model_copy(update={"prompt_version": "fake-v2"}) for ev in env.fake(*args, **kwargs)]

    mgr2 = RunManager(env.db, env.store, evaluate=evaluate_v2, workers=2, prompt_version="fake-v2")
    st = mgr2.start_run(wait=True)
    assert len(env.fake.calls) == n + 4 and st["from_cache"] == 0


def test_live_flag_reaches_the_evaluator_and_unknown_ids_fail(env):
    env.mgr.start_run(conv_ids=["t-003"], live=True, wait=True)
    assert "no_cache" in env.fake.calls[-1][3]
    with pytest.raises(KeyError):
        env.mgr.start_run(conv_ids=["t-404"])


def test_status_survives_restart(env):
    st = env.mgr.start_run(wait=True)
    fresh = RunManager(env.db, env.store, evaluate=env.fake)
    assert fresh.status(st["run_id"])["done"] == 4
    with pytest.raises(KeyError):
        fresh.status("run-missing")
