"""The synthetic edge-case set (eval/synthetic) is well-formed and its expected answers are legal for checklist v1."""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "eval/synthetic"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def built():
    subprocess.run([sys.executable, str(SYN / "build.py")], check=True, capture_output=True)
    recs = json.loads((SYN / "records.json").read_text(encoding="utf-8"))
    exp = [json.loads(x) for x in (SYN / "expected.jsonl").read_text(encoding="utf-8").splitlines() if x]
    return recs, exp


def _checklist():
    for p in (ROOT / "data/checklists/v1.json", (ROOT / "data/checklists/v1.json")):
        if p.exists():
            return {c["id"]: c for c in json.loads(p.read_text(encoding="utf-8"))["criteria"]}
    pytest.skip("checklist v1 not found")


def test_dev_format(built):
    recs, exp = built
    assert len(recs) == len(exp) >= 48
    assert len({r["conversation_id"] for r in recs}) == len(recs)
    for r in recs:
        assert set(r) == {"conversation_id", "channel", "language", "transcript", "audio_url"}
        assert r["channel"] in ("agent chat", "voice call") and r["language"] in ("RU", "KZ", "EN")
        assert (r["audio_url"] is None) == (r["channel"] == "agent chat")
        assert any(t["speaker"] == "operator" for t in r["transcript"])
        for t in r["transcript"]:
            assert set(t) == {"timestamp", "speaker", "text"} and t["speaker"] in ("client", "operator", "bot", "system") and t["text"]
            if r["channel"] == "agent chat":
                assert len(t["timestamp"]) == 5 and t["timestamp"][2] == ":"
            else:
                assert "T" in t["timestamp"] and t["timestamp"].endswith("+00:00")


def test_timestamps_monotonic_and_parse(built):
    from cqa import evaluator as E

    recs, _ = built
    for r in recs:
        secs = [m["t_sec"] for m in E.as_conversation(r)["messages"]]
        assert all(s is not None for s in secs), r["conversation_id"]
        assert secs == sorted(secs), r["conversation_id"]


def test_expected_answers_are_legal(built):
    from cqa import evaluator as E

    _, exp = built
    cl = _checklist()
    for e in exp:
        assert e["target"] and set(e["target"]) <= {c for c, v in e["expected"].items() if v is not None}
        assert e["rule"] and e["why"]
        for cid, v in list(e["expected"].items()) + [(c, a) for c, alts in e["accept"].items() for a in alts]:
            if v is None:
                continue
            val, err = E.normalize_answer(v, E.as_criterion(cl[cid]))
            assert err is None, (e["conversation_id"], cid, v, err)


def test_coverage_per_criterion_and_language(built):
    _, exp = built
    per = Counter(c for e in exp for c in e["target"])
    assert all(per[c] >= 6 for c in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")), per
    langs = Counter(e["language"] for e in exp)
    assert all(langs[x] >= 8 for x in ("RU", "KZ", "EN")), langs
    # the edges named in the brief are present
    want = {("C3", "na"), ("C8", "yes"), ("C4", "na"), ("C7", "na"), ("C6", 7), ("C4", 10), ("C4", 15), ("C5", "no"), ("C1", "no")}
    have = {(c, e["expected"][c]) for e in exp for c in e["target"]}
    assert want <= have, want - have


def test_scorer_grades():
    import robust_score as R

    assert R.grade("na", [], "N/A") == "exact"
    assert R.grade(7, [4], 4.0) == "soft"
    assert R.grade(7, [4], 10) == "miss"
    assert R.grade("yes", [], None) == "missing"
    exp = {"x": {"conversation_id": "x", "language": "RU", "channel": "agent chat", "target": ["C1"],
                 "expected": {"C1": "no", "C8": "no"}, "accept": {}, "reading": {}, "rule": "r", "why": "w"}}
    run = {"x": {"C1": {"answer": "yes"}, "C8": {"answer": "no"}, "_line": {}}}
    rows = R.score(exp, run)
    assert {(r["criterion"], r["kind"], r["grade"]) for r in rows} == {("C1", "target", "miss"), ("C8", "secondary", "exact")}
