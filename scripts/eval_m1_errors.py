"""m1: list every mismatched cell of a run against the silver gold (with the model's analysis and the dialogue tail).

    python scripts/eval_m1_errors.py results/eval_runs/<run>.jsonl [--split tune] [--crit C7] [--ctx 0]

Prints to stdout (quotes real dialogues: never commit the output). Also a compact per-criterion error summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from eval_accuracy import load_gold, load_run, norm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--split", default="tune")
    ap.add_argument("--crit")
    ap.add_argument("--ctx", type=int, default=0, help="print last N transcript turns for each error")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    a = ap.parse_args()
    gold = load_gold("silver")
    split = json.loads((ROOT / "eval/split.json").read_text())
    ids = set(split[a.split]) if a.split != "all" else None
    meta, rows = load_run(Path(a.run))
    recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data/raw/dev_records.json").read_text())}
    silver = Path(__file__).resolve().parents[1] / "data/processed/silver"
    gexp = defaultdict(list)
    for f in sorted(silver.glob("pass_*/batch_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for dl in d.get("dialogues", []):
            for c, ans in (dl.get("answers") or {}).items():
                gexp[(dl["conversation_id"], c)].append(f"{f.parent.name}:{ans.get('answer')}: {(ans.get('explanation') or ans.get('na_reason') or '')[:300]}")
    summ, n = Counter(), Counter()
    pairs = defaultdict(Counter)
    for row in sorted(rows, key=lambda r: r["conv_id"]):
        cid = row["conv_id"]
        if ids is not None and cid not in ids:
            continue
        for e in row["evals"]:
            c = e["criterion_id"]
            if a.crit and c not in a.crit.split(","):
                continue
            g = gold.get((cid, c))
            if g is None or e.get("answer") is None:
                continue
            p = norm(e["answer"])
            n[c] += 1
            if p == norm(g):
                continue
            summ[c] += 1
            pairs[c][f"{norm(g)}->{p}"] += 1
            if a.quiet:
                continue
            print(f"\n=== {cid} {c} gold={norm(g)} pred={p} conf={e.get('confidence')}")
            print("  expl:", (e.get("explanation") or "")[:400])
            for ge in gexp.get((cid, c), []):
                print("  GOLD", ge)
            for an in e.get("analysis") or []:
                print("  anal:", an[:500])
            if a.ctx:
                for m in recs[cid]["transcript"][-a.ctx:]:
                    print(f"    {m['speaker']}: {m['text'][:200]}")
    print("\nSUMMARY errors/n:", {c: f"{summ[c]}/{n[c]}" for c in sorted(n)}, "total", f"{sum(summ.values())}/{sum(n.values())}")
    for c in sorted(pairs):
        print(" ", c, dict(pairs[c]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
