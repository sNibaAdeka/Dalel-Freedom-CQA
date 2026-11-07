"""Independent PDF-formula recomputation used by the stress tests (scripts/stress_criterion_swap.py)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _v1():
    for p in (ROOT / "data/checklists/v1.json", (ROOT / "data/checklists/v1.json")):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    pytest.skip("checklist v1 not found")


def test_pdf_example_85_38():
    from stress_criterion_swap import recompute_total

    a = {"C1": "yes", "C2": "na", "C3": "na", "C4": 15, "C5": "yes", "C6": 7, "C7": "yes", "C8": "no"}
    assert recompute_total(_v1(), a) == 85.38


def test_all_na_and_missing():
    from stress_criterion_swap import recompute_total

    cl = {"criteria": [{"id": "A", "weight": 50, "answer_type": "binary"}, {"id": "B", "weight": 50, "answer_type": "binary"},
                       {"id": "X", "weight": 0, "critical": True, "answer_type": "binary"}]}
    assert recompute_total(cl, {"A": "na", "B": "na", "X": "yes"}) is None
    assert recompute_total(cl, {"A": "yes"}) == "missing"
    assert recompute_total(cl, {"A": "yes", "B": "no"}) == 50.0


def test_rehearsal_picks_diverse_holdout():
    from stress_control_rehearsal import pick_ids

    recs = json.loads((ROOT / "data/raw/dev_records.json").read_text(encoding="utf-8")) \
        if (ROOT / "data/raw/dev_records.json").exists() else pytest.skip("no DEV")
    ids = pick_ids(ROOT / "eval/split.json", recs, 10)
    hold = set(json.loads((ROOT / "eval/split.json").read_text())["holdout"])
    assert len(ids) == 10 and set(ids) <= hold
    by = {r["conversation_id"]: r for r in recs}
    assert {by[i]["language"] for i in ids} == {"RU", "KZ", "EN"} or len({by[i]["language"] for i in ids}) >= 2
