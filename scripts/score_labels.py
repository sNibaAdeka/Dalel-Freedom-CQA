"""Honest accuracy from human labels: inter-annotator agreement + model run vs the human reference.

    python scripts/score_labels.py labels/*.jsonl --run results/eval_runs/or-hold-google-gemini-4-argon-g9.jsonl \
        [--run results/control_CTRL.json] [--checklist data/checklists/v1.json] [--out reports/score_labels.json]

Labels: JSONL exported by the UI «?label=1» (web/js/labeling.js exportRows): one row per (conv_id, criterion_id) with
answer (yes|no|na|number), annotator, checklist_version, criterion_hash, annotated_at. Any number of files, a file may
hold one or many conversations/annotators; the annotator is read from the row. Rows of a stale criterion hash or another
checklist version are rejected (counted, not scored). A repeated (annotator, cell) keeps the latest annotated_at.

Model runs: eval-run JSONL (results/eval_runs/*.jsonl: {"conv_id", "evals":[{"criterion_id","answer"}]}) or a control
export results/control_<gid>.json. Dialogue ids are matched exactly; a label id «x@GROUP» also matches run id «x».

Reference ("human truth") per cell: with 1 annotator -> that annotator; with >= 2 -> cells where ALL annotators agree
("consensus"); disputed cells are listed, never guessed. Accuracy is computed on the consensus cells only, and also
against each annotator separately. Reported: exact-match accuracy overall/per criterion, N/A precision-style counts,
mean absolute error for numeric answers, confusion pairs, and the total score (cqa.engine) MAE on conversations whose
every criterion has a consensus label. Output holds ids and numbers only, no transcript text.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import engine  # noqa: E402
from cqa.models import ChecklistVersion, CriterionEval, normalize_answer  # noqa: E402
from scripts.annotation_agreement import statistics  # noqa: E402


def _valid(criterion, answer: Any) -> bool:
    if answer == "na":
        return bool(criterion.na_allowed)
    if criterion.answer_type == "binary":
        return answer in ("yes", "no")
    if isinstance(answer, bool) or not isinstance(answer, (int, float)):
        return False
    scale = criterion.scale or []
    return answer in scale if criterion.answer_type == "scale" else (not scale or min(scale) <= answer <= max(scale))


def load_labels(paths: list[Path], checklist: ChecklistVersion) -> tuple[dict, dict]:
    """-> ({(annotator, conv, crit): answer}, rejected-counters). Latest annotated_at wins on repeats."""
    crit = {c.id: c for c in checklist.criteria}
    cells: dict[tuple, tuple[str, Any]] = {}
    rejected: Counter = Counter()
    for path in paths:
        for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f"{Path(path).name}:{number}: not JSON") from None
            conv, cid, who = row.get("conv_id"), row.get("criterion_id"), row.get("annotator")
            if not (isinstance(conv, str) and conv.strip() and isinstance(who, str) and who.strip()) or cid not in crit:
                rejected["malformed_or_unknown_criterion"] += 1
                continue
            if row.get("checklist_version") not in (None, checklist.version):
                rejected["other_checklist_version"] += 1
                continue
            if row.get("criterion_hash") not in (None, crit[cid].hash):
                rejected["stale_criterion_hash"] += 1
                continue
            try:
                answer = normalize_answer(row.get("answer"))
            except ValueError:
                answer = None
            if answer is None or not _valid(crit[cid], answer):
                rejected["invalid_answer"] += 1
                continue
            key = (who.strip(), conv.strip(), cid)
            stamp = str(row.get("annotated_at") or "")
            if key in cells:
                rejected["repeated_cell_older_dropped"] += 1
                if stamp < cells[key][0]:
                    continue
            cells[key] = (stamp, answer)
    return {k: v[1] for k, v in cells.items()}, dict(rejected)


def load_run(path: Path) -> dict[tuple[str, str], Any]:
    """-> {(conv, criterion): answer}. Failed/missing answers (None) are omitted so they count as «not answered»."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    out: dict[tuple[str, str], Any] = {}
    if path.suffix == ".json":
        doc = json.loads(text)
        for conv in doc.get("conversations", []):
            for c in conv.get("criteria", []):
                if c.get("answer") is not None:
                    out[(conv["conversation_id"], c["criterion_id"])] = normalize_answer(c["answer"])
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "evals" not in row:  # _meta line
            continue
        for e in row["evals"]:
            if e.get("answer") is not None:
                out[(row["conv_id"], e["criterion_id"])] = normalize_answer(e["answer"])
    return out


def _match_conv(conv: str, run_ids: set[str]) -> Optional[str]:
    if conv in run_ids:
        return conv
    base = conv.split("@", 1)[0]
    return base if base in run_ids else None


def agreement(by_annotator: dict[str, dict], checklist: ChecklistVersion) -> dict:
    names = sorted(by_annotator)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = sorted(by_annotator[a].keys() & by_annotator[b].keys())
            per = {c.id: statistics([(by_annotator[a][k], by_annotator[b][k]) for k in shared if k[1] == c.id])
                   for c in checklist.criteria}
            pairs.append({"annotators": [a, b], "shared_cells": len(shared),
                          "overall": statistics([(by_annotator[a][k], by_annotator[b][k]) for k in shared]),
                          "per_criterion": per})
    return {"annotators": names, "cells_per_annotator": {n: len(by_annotator[n]) for n in names}, "pairs": pairs}


def consensus(by_annotator: dict[str, dict]) -> tuple[dict, list[dict]]:
    """Cells labeled by >= 1 annotator. Several annotators on a cell must all agree; otherwise it is disputed."""
    votes: dict[tuple, dict[str, Any]] = defaultdict(dict)
    for who, cells in by_annotator.items():
        for k, v in cells.items():
            votes[k][who] = v
    agreed, disputed = {}, []
    for k, vs in sorted(votes.items()):
        values = list(vs.values())
        if all(v == values[0] for v in values):
            agreed[k] = {"answer": values[0], "n_annotators": len(values)}
        else:
            disputed.append({"conv_id": k[0], "criterion_id": k[1], "answers": vs})
    return agreed, disputed


def _acc(pairs: list[tuple]) -> dict:
    n = len(pairs)
    num = [(m, h) for m, h in pairs if isinstance(m, (int, float)) and isinstance(h, (int, float))
           and not isinstance(m, bool) and not isinstance(h, bool)]
    return {"n": n, "correct": sum(m == h for m, h in pairs),
            "accuracy_pct": round(100 * sum(m == h for m, h in pairs) / n, 2) if n else None,
            "mae_numeric": round(sum(abs(m - h) for m, h in num) / len(num), 4) if num else None,
            "numeric_n": len(num)}


def score_run(run: dict, reference: dict, checklist: ChecklistVersion) -> dict:
    """reference: {(conv, crit): answer}. Compares only cells that BOTH have; reports what is missing."""
    run_ids = {k[0] for k in run}
    pairs, missing, unmatched = [], [], set()
    for (conv, cid), human in sorted(reference.items()):
        rc = _match_conv(conv, run_ids)
        if rc is None:
            unmatched.add(conv)
            continue
        if (rc, cid) not in run:
            missing.append({"conv_id": conv, "criterion_id": cid})
            continue
        pairs.append((conv, cid, run[(rc, cid)], human))
    per = {c.id: _acc([(m, h) for _, k, m, h in pairs if k == c.id]) for c in checklist.criteria}
    confusion = Counter((str(h), str(m)) for _, _, m, h in pairs if m != h)
    errors = [{"conv_id": cv, "criterion_id": k, "model": m, "human": h} for cv, k, m, h in pairs if m != h]
    return {"overall": _acc([(m, h) for _, _, m, h in pairs]), "per_criterion": per,
            "confusion_human_to_model": {f"{h}->{m}": n for (h, m), n in confusion.most_common()},
            "errors": errors, "reference_cells_without_model_answer": missing,
            "dialogues_not_in_run": sorted(unmatched),
            "total_score": _total_score(run, reference, checklist, run_ids)}


def _total_score(run: dict, reference: dict, checklist: ChecklistVersion, run_ids: set[str]) -> dict:
    convs = sorted({k[0] for k in reference})
    rows = []
    for conv in convs:
        rc = _match_conv(conv, run_ids)
        if rc is None or not all((conv, c.id) in reference and (rc, c.id) in run for c in checklist.criteria):
            continue

        def total(get):
            ev = [CriterionEval(conv_id=conv, criterion_id=c.id, criterion_hash=c.hash, answer=get(c.id))
                  for c in checklist.criteria]
            return engine.score(checklist, ev, conv_id=conv, strict=False).total

        t_model, t_human = total(lambda c: run[(rc, c)]), total(lambda c: reference[(conv, c)])
        if t_model is not None and t_human is not None:
            rows.append({"conv_id": conv, "model": t_model, "human": t_human, "abs_diff": round(abs(t_model - t_human), 2)})
    return {"dialogues": len(rows), "mae": round(sum(r["abs_diff"] for r in rows) / len(rows), 3) if rows else None,
            "rows": rows}


def build_report(label_paths: list[Path], run_paths: list[Path], checklist: ChecklistVersion) -> dict:
    cells, rejected = load_labels(label_paths, checklist)
    by_annotator: dict[str, dict] = defaultdict(dict)
    for (who, conv, cid), ans in cells.items():
        by_annotator[who][(conv, cid)] = ans
    agreed, disputed = consensus(by_annotator)
    reference = {k: v["answer"] for k, v in agreed.items()}
    report = {"kind": "human_label_scoring", "checklist_version": checklist.version,
              "note": "Accuracy is vs HUMAN consensus cells only (disputed cells excluded and listed). Agreement is not accuracy.",
              "labels": {"rejected_rows": rejected, "annotators": sorted(by_annotator),
                         "cells": len(cells), "consensus_cells": len(reference), "disputed_cells": len(disputed),
                         "dialogues": len({k[0] for k in reference})},
              "agreement": agreement(by_annotator, checklist) if len(by_annotator) > 1 else None,
              "disputed": disputed, "runs": {}}
    for path in run_paths:
        run = load_run(path)
        entry = {"vs_consensus": score_run(run, reference, checklist), "vs_annotator": {}}
        for who, cs in by_annotator.items():
            entry["vs_annotator"][who] = score_run(run, cs, checklist)["overall"]
        report["runs"][Path(path).name] = entry
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="+", type=Path, help="label JSONL files (UI export)")
    ap.add_argument("--run", action="append", default=[], type=Path, help="model run (eval_runs/*.jsonl or control_*.json), repeatable")
    ap.add_argument("--checklist", type=Path, default=ROOT / "data/checklists/v1.json")
    ap.add_argument("--out", type=Path, default=ROOT / "reports/score_labels.json")
    args = ap.parse_args(argv)
    try:
        cl = ChecklistVersion.model_validate_json(args.checklist.read_text(encoding="utf-8"))
        report = build_report(args.labels, args.run, cl)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lab = report["labels"]
    print(f"Разметчики {lab['annotators']}: ячеек {lab['cells']}, согласованных {lab['consensus_cells']}, спорных {lab['disputed_cells']}, "
          f"отброшено {sum(lab['rejected_rows'].values())}")
    for pair in (report["agreement"] or {}).get("pairs", []):
        o = pair["overall"]
        print(f"  согласие {pair['annotators']}: {o['agreement_pct']}% на {o['compared']} ячейках, kappa {o['cohen_kappa']}")
    for name, e in report["runs"].items():
        o, t = e["vs_consensus"]["overall"], e["vs_consensus"]["total_score"]
        print(f"  {name}: точность {o['accuracy_pct']}% ({o['correct']}/{o['n']}), MAE итога {t['mae']} на {t['dialogues']} диалогах")
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
