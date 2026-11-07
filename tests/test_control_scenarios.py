"""Control-day failure scenarios for scripts/control_run.py (synthetic data, fake evaluator, no network).

(a) provider down on every dialogue while the same ids were evaluated before -> no stale answers exported;
(b) control ids equal to DEV ids -> stored as <id>@<group>, DEV answers neither reused nor modified;
(c) re-running the same group is fully live again (no stored answers, no cache);
(d) a control set with a call (audio_url) ingests and exports without leaking the player link.

The injected fake evaluator is an explicit mock rehearsal (allow_mock=True).
Production refusal of unapproved mock control runs is covered by test_control_round2.py.
"""
from __future__ import annotations

import json

from tests.test_api import SYNTH, _control_module, client, ingest, run  # noqa: F401  (client is a fixture)


def _export(tmp_path, gid):
    return json.loads((tmp_path / f"control_{gid}.json").read_text(encoding="utf-8"))


def _c1(conv):
    return next(c for c in conv["criteria"] if c["criterion_id"] == "C1")


def test_dev_id_collision_is_stored_renamed_and_dev_answers_do_not_leak(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    ingest(client, group_id="DEV")
    run(client)                                           # DEV answers: C1 = yes for every dialogue
    dev_ids = sorted(d["id"] for d in client.get("/api/conversations?group_id=DEV").json())
    dev_c1 = [e.answer for e in state.db.list_evals(conv_id="t-ru-002") if e.criterion_id == "C1"]
    assert dev_c1 == ["yes"]
    real = client.fake.__call__

    def strict_c1(conv, checklist, criterion_ids, **kw):  # control run answers differently -> a leak would be visible
        out = real(conv, checklist, criterion_ids, **kw)
        for e in out:
            if e["criterion_id"] == "C1":
                e["answer"] = "no"
        return out

    state._evaluate = strict_c1
    calls = len(client.fake.calls)
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
    assert rep["failed"] == 0 and rep["conversations"] == 5
    assert len(client.fake.calls) - calls == 5            # every control dialogue went to the evaluator
    data = _export(tmp_path, "CTRL")
    assert {c["conversation_id"] for c in data["conversations"]} == {r["conversation_id"] for r in SYNTH}
    assert all(c["internal_id"].endswith("@CTRL") for c in data["conversations"])
    assert all(_c1(c)["answer"] == "no" for c in data["conversations"])
    assert all(c["stale_answers_ignored"] == 0 for c in data["conversations"])
    # DEV is untouched: same ids, same single stored C1 answer
    assert sorted(d["id"] for d in client.get("/api/conversations?group_id=DEV").json()) == dev_ids
    assert [e.answer for e in state.db.list_evals(conv_id="t-ru-002") if e.criterion_id == "C1"] == ["yes"]


def test_provider_down_on_all_dialogues_exports_no_old_answers(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    ingest(client, group_id="DEV")
    run(client)
    control.run_control(state, "CTRL", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)    # a good earlier control run

    def down(conv, checklist, criterion_ids, **kw):
        raise RuntimeError("provider down (test)")

    state._evaluate = down
    rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
    assert rep["status"] != "completed" and rep["failed"] == 5 and rep["evaluated"] == 0 and rep["mean_total"] is None
    data = _export(tmp_path, "CTRL")
    for conv in data["conversations"]:
        assert conv["error"] and conv["total"] is None
        assert all(c["answer"] is None and not c["evidence"] for c in conv["criteria"])
    assert (tmp_path / "control_CTRL_trace.jsonl").read_text(encoding="utf-8").strip() == ""


def test_rerun_of_same_group_is_fully_live_every_time(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    for attempt in range(2):
        calls = len(client.fake.calls)
        rep = control.run_control(state, "CTRL", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
        new = client.fake.calls[calls:]
        assert rep["failed"] == 0 and len(new) == 5 and all(c[3] is True for c in new), attempt
        data = _export(tmp_path, "CTRL")
        assert data["run"]["from_cache"] == 0 and data["run"]["force"] is True
        assert len(data["conversations"]) == 5                # ids not duplicated by the rerun


def test_control_set_with_a_call_ingests_and_never_exports_the_player_link(client, tmp_path):
    control = _control_module()
    state = client.app.state.cqa
    call = [r for r in SYNTH if r["conversation_id"] == "t-call-001"]
    rep = control.run_control(state, "CTRL-CALL", records=call, mode="fast", allow_mock=True, out_dir=tmp_path)
    assert rep["status"] == "completed" and rep["conversations"] == 1 and rep["failed"] == 0
    data = _export(tmp_path, "CTRL-CALL")
    assert data["conversations"][0]["channel"] == "call" and data["conversations"][0]["total"] is not None
    for f in tmp_path.glob("control_CTRL-CALL*"):
        assert "SECRET_PLAYER" not in f.read_text(encoding="utf-8-sig")


def test_report_flags_profile_that_differs_from_the_ui_default(client, tmp_path):
    # Control computed with another profile (e.g. --profile claude after an OpenRouter outage): the UI criterion swap
    # plans with the UI profile and would not find these answers -> the script must say so.
    control = _control_module()
    state = client.app.state.cqa
    same = control.run_control(state, "CTRL", records=SYNTH, mode="fast", allow_mock=True, out_dir=tmp_path)
    assert same["ui_profile_mismatch"] is False
    other = control.run_control(state, "CTRL2", records=SYNTH, mode="fast", profile="claude", allow_mock=True, out_dir=tmp_path)
    assert other["ui_profile_mismatch"] is True and other["profile"] == "claude" and other["ui_profile"] != "claude"
