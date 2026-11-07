"""Round 2 of the control script (critic-final B1, M1, M2, m6, M6): synthetic data, fake evaluator, no network.

B1  no --version -> the OFFICIAL checklist (v1), not the active one; loud warning when they differ;
M1  no --profile -> config default_profile, not the profile last picked in the UI;
M2  a dialogue with one broken criterion keeps this run's other answers; --ids re-runs only the named dialogues;
"""
from __future__ import annotations

import json

import pytest

from tests.test_api import SYNTH, _control_module, client, ingest, run  # noqa: F401  (client is a fixture)
from tests.offline_guard import no_network  # noqa: F401


@pytest.fixture(autouse=True)
def _no_profile_env(monkeypatch, client):
    # The shared client sets mock and injects FakeEval. Remove the profile only
    # after that fixture runs, so these tests exercise config-default selection.
    # Calls still use FakeEval; network and provider fallback remain forbidden.
    monkeypatch.delenv("CQA_PROFILE", raising=False)


def _export(tmp_path, gid):
    return json.loads((tmp_path / f"control_{gid}.json").read_text(encoding="utf-8"))


def _replace_c1_and_activate(state):
    res = state.checklists.apply([{"op": "replace", "criterion_id": "C1",
                                   "criterion": {"title": "Проверка резервного телефона",
                                                 "description": "Оператор уточнил резервный телефон."}}],
                                 change_note="репетиция")
    state.checklists.activate(res.checklist.version)
    return res.checklist.version


# ----------------------------------------------------------------------------------------------------- B1
def test_default_version_is_the_official_checklist_even_when_another_is_active(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    assert _replace_c1_and_activate(state) == 2 and state.checklists.active_version() == 2
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)    # no new kwargs: pure behaviour
    data = _export(tmp_path, "CTRL")
    assert data["checklist"]["version"] == 1
    assert data["checklist"]["criteria"][0]["title"] == "Установление контакта"
    assert data["run"]["version"] == 1


def test_plan_and_warning_are_printed_before_the_run(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    _replace_c1_and_activate(state)
    said = []
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, announce=said.append)
    data = _export(tmp_path, "CTRL")
    assert rep["version"] == 1
    assert any("ВНИМАНИЕ" in w and "2" in w for w in rep["warnings"])      # active v2 != official v1
    text = "\n".join(said)                                                 # printed BEFORE the run started
    assert "CTRL" in text and "диалогов: 5" in text and "v1" in text and "cloud" in text and "fast" in text
    for c in data["checklist"]["criteria"]:
        assert c["title"] in text                                          # all 8 criteria named


def test_explicit_version_still_wins_and_no_warning_when_active_is_official(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    v2 = _replace_c1_and_activate(state)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, version=v2)
    assert rep["version"] == 2 and _export(tmp_path, "CTRL")["checklist"]["version"] == 2
    state.checklists.activate(1)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    assert rep["version"] == 1 and not [w for w in rep["warnings"] if "чек-лист" in w.lower()]


# ----------------------------------------------------------------------------------------------------- M1
def test_default_profile_is_config_default_not_the_ui_setting(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    state.update_settings(profile="claude")                                # someone clicked "Claude" in the UI
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    assert rep["profile"] == "cloud" and rep["ui_profile"] == "claude"
    assert _export(tmp_path, "CTRL")["run"]["profile"] == "cloud"
    warn = " ".join(rep["warnings"])
    assert "claude" in warn and "api/config" in warn and '"profile":"cloud"' in warn   # how to align the UI


def test_explicit_profile_is_respected(client, tmp_path):
    control = _control_module()
    rep = control.run_control(client.app.state.cqa, "CTRL", records=SYNTH, mode="fast", profile="mock", allow_mock=True,
                              out_dir=tmp_path)
    assert rep["profile"] == "mock"


# ----------------------------------------------------------------------------------------------------- M2
def _breaking(real, cid_broken="C5", target="t-ru-002"):
    def evaluator(conv, checklist, criterion_ids, **kw):
        out = real(conv, checklist, criterion_ids, **kw)
        if (conv.get("meta") or {}).get("source_id", conv["id"]) == target:
            for e in out:
                if e["criterion_id"] == cid_broken:
                    e["answer"] = "maybe"                                  # not an allowed answer
        return out
    return evaluator


def test_partial_failure_exports_this_runs_answers_and_marks_only_the_missing_criterion(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)       # earlier good run
    state._evaluate = _breaking(client.fake.__call__)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    bad = next(c for c in _export(tmp_path, "CTRL")["conversations"] if c["conversation_id"] == "t-ru-002")
    answers = {r["criterion_id"]: r["answer_label"] for r in bad["criteria"]}
    assert answers["C5"] == "нет ответа"
    assert sum(1 for k, v in answers.items() if v != "нет ответа") == 7      # the other 7 answers are kept
    c5 = next(r for r in bad["criteria"] if r["criterion_id"] == "C5")
    assert c5["error"] and "C5" in c5["error"]
    assert bad["total"] is None and bad["status"] == "incomplete" and bad["error"]
    assert "t-ru-002" in rep["failed_ids"] and rep["evaluated"] == 4
    trace = [json.loads(x) for x in (tmp_path / "control_CTRL_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for t in trace if t["conversation_id"] == "t-ru-002") == 7


def test_answers_from_an_earlier_run_are_not_passed_off_as_fresh_even_in_the_same_second(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    real = client.fake.__call__

    def never(conv, checklist, criterion_ids, **kw):                       # provider returns nothing valid at all
        out = real(conv, checklist, criterion_ids, **kw)
        for e in out:
            e["answer"] = "maybe"
        return out

    state._evaluate = never
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    for c in _export(tmp_path, "CTRL")["conversations"]:
        assert all(r["answer"] is None for r in c["criteria"]) and c["total"] is None


def _csv_rows(tmp_path, gid):
    import csv
    return list(csv.DictReader((tmp_path / f"control_{gid}.csv").open(encoding="utf-8-sig")))


def test_ids_reruns_only_the_named_dialogues_and_updates_their_records_in_place(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    state._evaluate = _breaking(client.fake.__call__)
    first = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    assert first["failed_ids"] == ["t-ru-002"]
    before = _export(tmp_path, "CTRL")
    trace_before = [json.loads(x) for x in (tmp_path / "control_CTRL_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    state._evaluate = client.fake                                          # the provider recovered
    calls = len(client.fake.calls)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, ids=["t-ru-002"])
    assert len(client.fake.calls) - calls == 1 and client.fake.calls[-1][0].startswith("t-ru-002")
    assert rep["failed"] == 0 and rep["conversations"] == 5 and rep["evaluated"] == 5   # summary covers the merged file
    after = _export(tmp_path, "CTRL")
    assert [c["conversation_id"] for c in after["conversations"]] == [c["conversation_id"] for c in before["conversations"]]
    for b, a in zip(before["conversations"], after["conversations"]):
        if a["conversation_id"] == "t-ru-002":
            assert a["total"] is not None and a["error"] is None and all(r["answer"] is not None for r in a["criteria"])
        else:
            assert a == b                                                  # untouched
    rows = _csv_rows(tmp_path, "CTRL")
    assert len(rows) == 5 * 8 and {r["conversation_id"] for r in rows if r["answer"] == "нет ответа"} == set()
    trace = [json.loads(x) for x in (tmp_path / "control_CTRL_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [t for t in trace if t["conversation_id"] != "t-ru-002"] == \
           [t for t in trace_before if t["conversation_id"] != "t-ru-002"]
    assert sum(1 for t in trace if t["conversation_id"] == "t-ru-002") == 8


def test_ids_unknown_or_without_an_existing_export_is_refused(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    with pytest.raises(Exception, match="nope"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, ids=["nope"])
    assert not (tmp_path / "control_CTRL.json").exists()
    with pytest.raises(Exception, match="control_CTRL.json"):                  # valid id, but nothing to update yet
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, ids=["t-ru-002"])
    assert not state.db.list_evals(conv_id="t-ru-002")                         # refused BEFORE any evaluation


def test_ids_refuses_to_mix_checklist_versions_in_one_export(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    v2 = _replace_c1_and_activate(state)
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)            # v1 export
    with pytest.raises(Exception, match="версии|version"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, ids=["t-ru-002"], version=v2)


def test_cli_ids_flag_is_parsed(monkeypatch):
    control = _control_module()
    seen = {}

    def fake_run(state, gid, **kw):
        seen.update(kw)
        raise control.ingest.IngestError("stop")

    monkeypatch.setattr(control, "run_control", fake_run)
    monkeypatch.setattr("api.state.AppState", lambda **kw: type("S", (), {"evaluator_ready": True})())
    assert control.main(["G", "--ids", "a, b", "--db", ":memory:"]) == 2
    assert seen["ids"] == ["a", "b"]


# ---------------------------------------------------------------------- critic round 2 additions
def test_ui_profile_never_reaches_start_run(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    seen = []
    real = state.runs.start_run

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    state.runs.start_run = spy
    for ui in ("cascade", "local"):
        state.update_settings(profile=ui)
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    assert [k["profile"] for k in seen] == ["cloud", "cloud"] and all(k["version"] == 1 for k in seen)


def test_env_profile_beats_config_default_and_flag_beats_env(client, tmp_path, monkeypatch):
    control = _control_module()
    state = client.app.state.cqa
    monkeypatch.setenv("CQA_PROFILE", "mock")
    assert control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path,
                               allow_mock=True)["profile"] == "mock"
    assert control.run_control(state, "CTRL", records=SYNTH, mode="fast", profile="claude",
                               out_dir=tmp_path)["profile"] == "claude"


def test_local_profile_without_ollama_stops_before_any_evaluation(client, tmp_path, monkeypatch):
    import httpx
    control = _control_module()
    state = client.app.state.cqa

    def down(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    calls = len(client.fake.calls)
    with pytest.raises(control.ingest.IngestError, match="Ollama"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", profile="local", out_dir=tmp_path)
    assert len(client.fake.calls) == calls and not (tmp_path / "control_CTRL.json").exists()


def test_local_profile_with_a_missing_model_names_what_is_available(client, tmp_path, monkeypatch):
    import httpx
    control = _control_module()

    class R:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": "qwen2.5:3b-instruct"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: R())
    with pytest.raises(control.ingest.IngestError, match="qwen2.5:3b-instruct"):
        control.run_control(client.app.state.cqa, "CTRL", records=SYNTH, mode="fast", profile="local", out_dir=tmp_path)


def test_plan_shows_checklist_hash(client, tmp_path):
    said = []
    _control_module().run_control(client.app.state.cqa, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path,
                                  announce=said.append)
    import re
    assert re.search(r"хеш [0-9a-f]{10}", said[0])


def test_ids_rerun_does_not_touch_other_dialogues_evaluations(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    state._evaluate = _breaking(client.fake.__call__)
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    others = [c["internal_id"] for c in _export(tmp_path, "CTRL")["conversations"] if c["conversation_id"] != "t-ru-002"]
    mark = state.db.max_eval_id()
    state._evaluate = client.fake
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, ids=["t-ru-002"])
    assert all(not state.db.eval_keys_since(cid, mark) for cid in others)     # same DB rows, no rewrite
    doc = _export(tmp_path, "CTRL")
    assert all(c["status"] == "complete" for c in doc["conversations"])
    assert doc["summary"]["status"] == "completed" and doc["summary"]["failed"] == 0 and doc["summary"]["evaluated"] == 5
    assert doc["run"]["status_after_reruns"] == "completed"                    # not the stale "failed" of run 1


def test_unknown_profile_is_refused_before_anything_runs(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    seen = []
    state.runs.start_run = lambda *a, **kw: seen.append(kw)
    with pytest.raises(control.ingest.IngestError, match="clod.*cloud|cloud.*clod"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", profile="clod", out_dir=tmp_path)
    assert seen == [] and not (tmp_path / "control_CTRL.json").exists()


def test_mock_model_is_refused_for_a_real_group_unless_allowed(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    with pytest.raises(control.ingest.IngestError, match="allow-mock"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", profile="mock", out_dir=tmp_path)
    with pytest.raises(control.ingest.IngestError, match="allow-mock"):                       # DEV from the API too
        control.run_control(state, "DEV", records=SYNTH, source="api", mode="fast", profile="mock", out_dir=tmp_path)
    assert not (tmp_path / "control_CTRL.json").exists()
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", profile="mock", allow_mock=True, out_dir=tmp_path)
    assert rep["profile"] == "mock"


def test_unknown_version_is_a_clean_error(client, tmp_path):
    control = _control_module()
    with pytest.raises(control.ingest.IngestError, match="7.*1"):
        control.run_control(client.app.state.cqa, "CTRL", records=SYNTH, mode="fast", version=7, out_dir=tmp_path)


def test_ids_with_limit_requires_the_same_limit_as_the_export(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    state._evaluate = _breaking(client.fake.__call__)
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", limit=3, out_dir=tmp_path)        # rehearsal style: --limit
    assert _export(tmp_path, "CTRL")["run"]["limit"] == 3
    state._evaluate = client.fake
    calls = len(client.fake.calls)
    with pytest.raises(control.ingest.IngestError, match="limit"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", limit=4, ids=["t-ru-002"], out_dir=tmp_path)
    with pytest.raises(control.ingest.IngestError, match="limit"):
        control.run_control(state, "CTRL", records=SYNTH, mode="fast", ids=["t-ru-002"], out_dir=tmp_path)
    assert len(client.fake.calls) == calls
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", limit=3, ids=["t-ru-002"], out_dir=tmp_path)
    assert rep["failed"] == 0 and rep["conversations"] == 3 and len(client.fake.calls) - calls == 1


def test_dry_run_validates_prints_the_plan_and_changes_nothing(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    said, started = [], []
    state.runs.start_run = lambda *a, **kw: started.append(kw)
    calls = len(client.fake.calls)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, dry_run=True,
                              announce=said.append)
    assert rep["dry_run"] is True and rep["conversations"] == 5 and rep["version"] == 1 and rep["profile"] == "cloud"
    assert "диалогов: 5" in said[0] and "Установление контакта" in said[0]
    assert started == [] and len(client.fake.calls) == calls
    assert list(tmp_path.glob("control_*")) == [] and state.db.list_conversations(group_id="CTRL") == []     # nothing stored
    for bad in ({"profile": "clod"}, {"version": 9}, {"profile": "mock"}):                            # still validates
        with pytest.raises(control.ingest.IngestError):
            control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, dry_run=True, **bad)


def test_dry_run_with_unknown_ids_fails(client, tmp_path):
    control = _control_module()
    with pytest.raises(control.ingest.IngestError, match="nope"):
        control.run_control(client.app.state.cqa, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path, dry_run=True,
                            ids=["nope"])


def test_cli_dry_run_flag_is_parsed(monkeypatch):
    control = _control_module()
    seen = {}

    def fake_run(state, gid, **kw):
        seen.update(kw)
        raise control.ingest.IngestError("stop")

    monkeypatch.setattr(control, "run_control", fake_run)
    monkeypatch.setattr("api.state.AppState", lambda **kw: type("S", (), {"evaluator_ready": True})())
    control.main(["G", "--dry-run", "--db", ":memory:"])
    assert seen["dry_run"] is True


def test_mock_for_a_real_group_is_refused_before_the_set_is_fetched(client, tmp_path, monkeypatch):
    control = _control_module()

    def boom(*a, **kw):
        raise AssertionError("load_set must not run: the API would be called for nothing")

    monkeypatch.setattr(control, "load_set", boom)
    with pytest.raises(control.ingest.IngestError, match="allow-mock"):
        control.run_control(client.app.state.cqa, "CTRL", mode="fast", profile="mock", source="file", out_dir=tmp_path)


def test_stale_c5_is_not_resurrected_when_the_second_run_changes_values_and_breaks_one_criterion(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)               # run 1: all OK
    real = client.fake.__call__

    def run2(conv, checklist, criterion_ids, **kw):
        out = real(conv, checklist, criterion_ids, **kw)
        for e in out:
            if (conv.get("meta") or {}).get("source_id", conv["id"]) == "t-ru-002":
                if e["criterion_id"] == "C4":
                    e["answer"] = 10                                                               # differs from run 1
                if e["criterion_id"] == "C5":
                    e["answer"] = "maybe"
        return out

    state._evaluate = run2
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", out_dir=tmp_path)
    bad = next(c for c in _export(tmp_path, "CTRL")["conversations"] if c["conversation_id"] == "t-ru-002")
    by = {r["criterion_id"]: r for r in bad["criteria"]}
    assert by["C5"]["answer"] is None and by["C5"]["answer_label"] == "нет ответа"   # run 1 said "yes": must not leak
    assert by["C4"]["answer"] == 10 and sum(1 for r in bad["criteria"] if r["answer"] is not None) == 7
