"""Offline ensemble of several eval runs: per-cell majority vote, optional arbiter call on disagreement cells.

Simulates "fast x N votes" (N independent one-call-per-dialogue runs, possibly different models) without changing the
evaluator. Output is a normal run file (results/eval_runs/<name>.jsonl) for scripts/bench_compare.py.

    python scripts/bench_ensemble.py bench-haiku-fast-a bench-haiku-fast-b bench-haiku-fast-c --name ens-haiku3
    python scripts/bench_ensemble.py bench-haiku-fast-a bench-sonnet-fast-a bench-dflash-fast-a --name ens-mix3 \
        --arbiter claude_cli:claude-opus-5-5          # disagreement cells -> one per-criterion call of the arbiter model

Rules: majority answer wins; scale ties -> median of the votes rounded down to an allowed step; other ties -> the arbiter
if given, else the first run's answer. Cost = sum of members (+ arbiter calls); wall = max of members (they run in
parallel) + arbiter wall. needs_review = not unanimous and no arbiter.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_accuracy as EA  # noqa: E402
from bench_compare import run_path  # noqa: E402
from cqa import evaluator as E  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--name", required=True)
    ap.add_argument("--arbiter", help="model id for disagreement cells, e.g. claude_cli:claude-opus-5-5")
    ap.add_argument("--arbiter-on", default="disagree", choices=["disagree", "tie"], help="call arbiter on any disagreement or only on ties")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    ck = EA.find_checklist(None)
    crits = {c["id"]: E.as_criterion(c) for c in ck["criteria"]}
    recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data/raw/dev_records.json").read_text(encoding="utf-8"))}
    members = [EA.load_run(run_path(r)) for r in args.runs]
    by = [{r["conv_id"]: r for r in rows} for _, rows in members]
    common = set.intersection(*[set(b) for b in by])

    jobs, out_rows = [], {}
    for cv in sorted(common):
        rows = [b[cv] for b in by]
        if any(all(e.get("answer") is None for e in r["evals"]) for r in rows):
            continue
        evals = []
        for cid, crit in crits.items():
            votes = [next((e for e in r["evals"] if e["criterion_id"] == cid), None) for r in rows]
            ans = [EA.norm(v["answer"]) for v in votes if v and v.get("answer") is not None]
            if not ans:
                evals.append({"criterion_id": cid, "answer": None, "needs_review": True, "votes": [], "tokens_in": 0, "tokens_out": 0, "latency_ms": 0})
                continue
            cnt = Counter(map(str, ans)).most_common()
            top, k = cnt[0]
            tie = len(cnt) > 1 and cnt[1][1] == k
            if tie and crit.get("answer_type") == "scale" and all(isinstance(a, float) for a in ans):
                med = statistics.median(ans)
                steps = sorted(float(x) for x in crit["scale"])
                top = str(max(s for s in steps if s <= med))
            elif tie:
                top = str(ans[0])
            final = next(a for a in ans if str(a) == top)
            unanimous = len(cnt) == 1 and len(ans) == len(votes)
            e = {"criterion_id": cid, "answer": final, "votes": [str(a) for a in ans], "arbiter": None,
                 "confidence": round(k / len(votes), 3), "needs_review": not unanimous,
                 "tokens_in": sum((v or {}).get("tokens_in") or 0 for v in votes), "tokens_out": sum((v or {}).get("tokens_out") or 0 for v in votes),
                 "latency_ms": sum((v or {}).get("latency_ms") or 0 for v in votes)}
            if args.arbiter and not unanimous and (args.arbiter_on == "disagree" or tie):
                jobs.append((cv, cid, e))
            evals.append(e)
        out_rows[cv] = {"conv_id": cv, "language": rows[0]["language"], "channel": rows[0]["channel"],
                        "wall_s": max(r["wall_s"] for r in rows), "cost_kzt": sum(r["cost_kzt"] for r in rows),
                        "cost_kzt_full": round(sum(r["cost_kzt_full"] for r in rows), 4), "calls": sum(r.get("calls") or 0 for r in rows), "evals": evals}

    arb_wall: dict[str, float] = {}
    if jobs:
        pre = {"model": args.arbiter, "votes": 1, "temperatures": [0.0], "arbiter": None}

        def arb(job):
            cv, cid, e = job
            t0 = time.perf_counter()
            res = E.evaluate(recs[cv], ck, "accurate", [cid], preset=pre, as_models=False, workers=2)
            return cv, cid, e, res[0] if res else None, time.perf_counter() - t0

        print(f"arbiter {args.arbiter}: {len(jobs)} cells", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for cv, cid, e, r, wall in pool.map(arb, jobs):
                arb_wall[cv] = max(arb_wall.get(cv, 0.0), wall)
                row = out_rows[cv]
                row["calls"] += (r or {}).get("raw", {}).get("calls", 1) if r else 1
                row["cost_kzt_full"] = round(row["cost_kzt_full"] + ((r or {}).get("cost_kzt_full") or 0), 4)
                if r and r.get("answer") is not None:
                    e["arbiter"] = str(EA.norm(r["answer"]))
                    e["answer"] = r["answer"]
                    e["needs_review"] = bool(r.get("needs_review"))
                    e["tokens_in"] += r.get("tokens_in") or 0
                    e["tokens_out"] += r.get("tokens_out") or 0
                    e["latency_ms"] += r.get("latency_ms") or 0
                print(f"  {cv} {cid}: votes {e['votes']} -> arbiter {e['arbiter']}", flush=True)
    for cv, w in arb_wall.items():
        out_rows[cv]["wall_s"] = round(out_rows[cv]["wall_s"] + w, 2)

    metas = [m for m, _ in members]
    model = ",".join((m.get("preset") or {}).get("model", "?") for m in metas)
    meta = {"name": args.name, "mode": "fast-ensemble", "preset": {"model": model, "votes": len(members),
            "arbiter": {"model": args.arbiter, "when": args.arbiter_on} if args.arbiter else None},
            "members": args.runs, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    path = ROOT / "results/eval_runs" / f"{args.name}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for row in out_rows.values():
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(out_rows)} dialogues -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
