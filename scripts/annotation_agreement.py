"""Compare two independent annotation exports without manufacturing a gold standard.

python scripts/annotation_agreement.py eval/gold/alice.jsonl eval/gold/bob.jsonl

Only shared cells are compared; coverage and missing cells remain explicit. Cohen's
kappa is unweighted (scale values are categories). Constant identical labels have
undefined kappa, not 1. The output contains no transcript text.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa.models import ChecklistVersion, normalize_answer


def load_labels(path: Path, checklist: ChecklistVersion) -> dict:
    criteria = {c.id: c for c in checklist.criteria}
    labels = {}
    annotators = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("expected an object")
            conv_id, criterion_id = row.get("conv_id"), row.get("criterion_id")
            if not isinstance(conv_id, str) or not conv_id.strip():
                raise ValueError("conv_id is required")
            if not isinstance(criterion_id, str) or criterion_id not in criteria:
                raise ValueError("unknown criterion_id")
            annotator = row.get("annotator")
            if not isinstance(annotator, str) or not annotator.strip():
                raise ValueError("annotator is required")
            version = row.get("checklist_version")
            if isinstance(version, bool) or not isinstance(version, int) or version != checklist.version:
                raise ValueError("checklist_version must match the supplied checklist")
            criterion = criteria[criterion_id]
            if row.get("criterion_hash") not in (None, criterion.hash):
                raise ValueError("criterion_hash does not match the supplied checklist")
            answer = normalize_answer(row.get("answer"))
            if answer is None:
                raise ValueError("answer is missing")
            if answer == "na":
                valid = criterion.na_allowed
            elif criterion.answer_type == "binary":
                valid = answer in ("yes", "no")
            elif isinstance(answer, (int, float)):
                valid = answer in criterion.scale if criterion.answer_type == "scale" else min(criterion.scale) <= answer <= max(criterion.scale)
            else:
                valid = False
            if not valid:
                raise ValueError("answer is not allowed for this criterion")
            key = (conv_id.strip(), criterion_id)
            if key in labels:
                raise ValueError("duplicate conversation/criterion cell")
            annotators.add(annotator.strip())
            labels[key] = {"answer": answer, "annotator": annotator.strip(),
                           "criterion_hash": row.get("criterion_hash"),
                           "transcript_hash": row.get("transcript_hash"),
                           "checklist_hash": row.get("checklist_hash")}
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"{path.name}:{number}: {exc}") from exc
    if not labels:
        raise ValueError(f"{path.name}: no labels")
    if len(annotators) != 1:
        raise ValueError(f"{path.name}: expected one annotator per file")
    return labels


def statistics(pairs: list[tuple]) -> dict:
    n = len(pairs)
    if not n:
        return {"compared": 0, "agreed": 0, "agreement_pct": None, "cohen_kappa": None}
    agreed = sum(a == b for a, b in pairs)
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    # Integer arithmetic prevents floating point noise for constant-label samples.
    expected = sum(left[k] * right[k] for k in left.keys() | right.keys())
    denominator = n * n - expected
    kappa = (agreed * n - expected) / denominator if denominator else None
    return {"compared": n, "agreed": agreed, "agreement_pct": round(100 * agreed / n, 4),
            "cohen_kappa": round(kappa, 6) if kappa is not None else None}


def compare(left: dict, right: dict, checklist: ChecklistVersion) -> dict:
    a, b = next(iter(left.values()))["annotator"], next(iter(right.values()))["annotator"]
    if a == b:
        raise ValueError("The two files must identify different annotators")
    shared = sorted(left.keys() & right.keys())
    for key in shared:
        for field in ('transcript_hash', 'checklist_hash'):
            if left[key].get(field) and right[key].get(field) and left[key][field] != right[key][field]:
                raise ValueError(f'{key}: different {field}; the annotation inputs must match')
    per = {c.id: statistics([(left[k]["answer"], right[k]["answer"]) for k in shared if k[1] == c.id])
           for c in checklist.criteria}
    def cell(key):
        return {"conv_id": key[0], "criterion_id": key[1]}
    disagreements = [{**cell(k), "answer_a": left[k]["answer"], "answer_b": right[k]["answer"]}
                     for k in shared if left[k]["answer"] != right[k]["answer"]]
    return {
        "kind": "inter_annotator_agreement", "annotators": [a, b], "checklist_version": checklist.version,
        "note": "Agreement is not model accuracy. Disagreements require independent adjudication. Kappa is nominal/unweighted; null means undefined or no overlap.",
        "coverage": {"cells_a": len(left), "cells_b": len(right), "shared_cells": len(shared),
                     "union_cells": len(left.keys() | right.keys()),
                     "shared_pct_of_union": round(100 * len(shared) / len(left.keys() | right.keys()), 4),
                     "rows_without_criterion_hash": sum(not r.get("criterion_hash") for r in [*left.values(), *right.values()])},
        "overall": statistics([(left[k]["answer"], right[k]["answer"]) for k in shared]),
        "per_criterion": per,
        "only_a": [cell(k) for k in sorted(left.keys() - right.keys())],
        "only_b": [cell(k) for k in sorted(right.keys() - left.keys())],
        "disagreements": disagreements,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--checklist", type=Path, default=ROOT / "data/checklists/v1.json")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/annotation-agreement.json")
    args = parser.parse_args(argv)
    try:
        checklist = ChecklistVersion.model_validate_json(args.checklist.read_text(encoding="utf-8"))
        report = compare(load_labels(args.left, checklist), load_labels(args.right, checklist), checklist)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.out), "overall": report["overall"],
                      "disagreements": len(report["disagreements"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
