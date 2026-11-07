"""Experiment log -> markdown table (prompt version -> agreement -> cost/time per dialogue).

    python scripts/eval_table.py                 # all experiments
    python scripts/eval_table.py --split tune    # only one split
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CIDS = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split")
    ap.add_argument("--gold")
    args = ap.parse_args()
    rows = [json.loads(x) for x in (ROOT / "eval/experiments.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if (not args.split or r.get("split") == args.split) and (not args.gold or r.get("gold") == args.gold)]
    head = ["время", "прогон", "часть", "режим", "модель", "шаблон/guidance", "(а)", "(б)", *CIDS, "C7 строг./мягк.", "MAE (б)", "₸/дл", "с/дл", "заметка"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        per = r.get("per_criterion_a") or r.get("per_criterion") or {}
        c7 = next(iter((r.get("c7") or {}).values()), {})
        model = f"{r.get('model')}×{r.get('votes') or 1}" + (f"+{r['arbiter']}" if r.get("arbiter") else "")
        cells = [r.get("ts", "")[11:16], r.get("run", ""), f"{r.get('split')}/{r.get('n_dialogues')}", r.get("mode") or "", model,
                 f"{r.get('template')}/{r.get('guidance')}",
                 f"{100 * r['acc_a']:.1f}" if r.get("acc_a") is not None else f"{100 * r.get('accuracy', 0):.1f}",
                 f"{100 * r['acc_b']:.1f}" if r.get("acc_b") is not None else "",
                 *[f"{100 * per[c]:.0f}" if per.get(c) is not None else "" for c in CIDS],
                 (f"{100 * c7['strict']:.0f}/{100 * c7['lenient']:.0f}" if c7.get("strict") is not None and c7.get("lenient") is not None else ""),
                 f"{r['total_mae_b']:.2f}" if r.get("total_mae_b") is not None else "",
                 f"{r.get('cost_kzt_per_dialogue', 0):.2f}", f"{r.get('wall_s_per_dialogue', 0):.1f}", (r.get("note") or "").replace("|", "/")]
        print("| " + " | ".join(str(c) for c in cells) + " |")


if __name__ == "__main__":
    main()
