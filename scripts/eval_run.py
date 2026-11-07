"""Run the evaluator over DEV dialogues and save predictions (for scripts/eval_accuracy.py).

    python scripts/eval_run.py --profile cloud --mode fast --split debug --name ds-fast-g5
    python scripts/eval_run.py --profile cloud --mode accurate --ids p1-ru-010,p1-kz-003 --readings C7=strict
    python scripts/eval_run.py --model claude_cli:claude-haiku-5-5 --mode fast --limit 3

Output: results/eval_runs/<name>.jsonl (one line per dialogue: evals, wall time, cost). results/ is git-ignored because
the predictions quote real dialogues. Re-running the same name with the cache on is free (DEV only).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import evaluator as E  # noqa: E402
from cqa import llm  # noqa: E402
from cqa import prompts as P  # noqa: E402

def find_checklist(path: str | None) -> Path:
    for p in [Path(path)] if path else [ROOT / "data/checklists/v1.json"]:
        if p.exists():
            return p
    raise SystemExit("checklist not found: pass --checklist")


def load_records(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, dict):
        d = d.get("data", d).get("records", d.get("records", []))
    return d


def select(records: list[dict], args) -> list[dict]:
    ids = [r["conversation_id"] for r in records]
    if args.ids:
        want = [x.strip() for x in args.ids.split(",") if x.strip()]
    elif args.split:
        split = json.loads((ROOT / "eval/split.json").read_text())
        want = split[args.split] if args.split != "all" else ids
    else:
        want = ids
    if args.lang:
        langs = {x.upper() for x in args.lang.split(",")}
        want = [i for i in want if next(r["language"] for r in records if r["conversation_id"] == i).upper() in langs]
    by = {r["conversation_id"]: r for r in records}
    out = [by[i] for i in want if i in by]
    return out[: args.limit] if args.limit else out


def slim(e: dict) -> dict:
    keys = ("criterion_id", "answer", "confidence", "needs_review", "review_reasons", "critical_hit", "evidence", "checked_range",
            "na_reason", "explanation", "model", "tokens_in", "tokens_out", "cost_kzt", "cost_kzt_full", "latency_ms", "reading",
            "prompt_version", "mode")
    out = {k: e.get(k) for k in keys}
    raw = e.get("raw") or {}
    out["votes"] = [v.get("answer") for v in raw.get("votes") or [] if v]
    out["arbiter"] = (raw.get("arbiter") or {}).get("answer") if raw.get("arbiter") else None
    out["calls"] = raw.get("calls")
    out["analysis"] = [((v or {}).get("analysis") or "")[:600] for v in (raw.get("votes") or [])][:3]
    if raw.get("arbiter"):
        out["analysis"].append("ARBITER: " + (raw["arbiter"].get("analysis") or "")[:600])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/raw/dev_records.json"))
    ap.add_argument("--checklist")
    ap.add_argument("--profile", default=None, help="cloud | claude | local | mock (config/models.json)")
    ap.add_argument("--mode", default="fast", choices=["fast", "accurate"])
    ap.add_argument("--model", help="override main model, e.g. openai:deepseek-chat")
    ap.add_argument("--arbiter", help="override arbiter model ('none' to disable)")
    ap.add_argument("--votes", type=int)
    ap.add_argument("--models", help="mixed-model votes, comma-separated (accurate mode), e.g. openai:deepseek-chat,claude_cli:claude-haiku-5-5")
    ap.add_argument("--temps", help="vote temperatures, comma-separated, e.g. 0,0.5,0.9")
    ap.add_argument("--split", choices=["debug", "tune", "holdout", "all"])
    ap.add_argument("--ids")
    ap.add_argument("--lang")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="criteria ids, e.g. C7,C2")
    ap.add_argument("--readings", help="e.g. C7=strict,C8=disclosure_c8")
    ap.add_argument("--workers", type=int, default=4, help="dialogues in parallel")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--note", default="", help="what changed in this experiment (goes to the log)")
    args = ap.parse_args()

    checklist = json.loads(find_checklist(args.checklist).read_text(encoding="utf-8"))
    recs = select(load_records(Path(args.data)), args)
    pre = llm.preset(args.mode, args.profile)
    if args.model:
        pre["model"] = args.model
    if args.votes:
        pre["votes"] = args.votes
        t = pre["temperatures"]
        pre["temperatures"] = [t[i % len(t)] for i in range(args.votes)] if t else [0.0] * args.votes
    if args.models:
        pre["models"] = [m.strip() for m in args.models.split(",") if m.strip()]
        pre["model"] = pre["models"][0]
        pre["votes"] = args.votes or len(pre["models"])
        t = pre["temperatures"]
        pre["temperatures"] = [t[i % len(t)] for i in range(pre["votes"])]
    if args.temps:
        pre["temperatures"] = [float(x) for x in args.temps.split(",")]
        pre["votes"] = len(pre["temperatures"])
    if args.arbiter:
        pre["arbiter"] = None if args.arbiter == "none" else {**(pre.get("arbiter") or {"when": "disagree"}), "model": args.arbiter}
    name = args.name or f"{args.mode}-{pre['model'].replace(':', '_').replace('/', '_')}-{time.strftime('%H%M')}"
    out_dir = ROOT / "results/eval_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.jsonl"
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    meta = {"name": name, "mode": args.mode, "profile": args.profile or llm.active_profile(), "preset": pre, "readings": P.active_readings(args.readings),
            "template": P.TEMPLATE_VERSION, "guidance_version": P.load_overlay().get("version"), "n": len(recs), "only": only,
            "note": args.note, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(json.dumps({k: meta[k] for k in ("name", "mode", "profile", "readings", "template", "guidance_version", "n")}, ensure_ascii=False))
    print("model:", pre["model"], "votes:", pre.get("votes"), "arbiter:", (pre.get("arbiter") or {}).get("model"))

    lock = threading.Lock()
    t_all = time.perf_counter()
    done, failed = 0, 0
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")

        def one(rec):
            t0 = time.perf_counter()
            evals = E.evaluate(rec, checklist, args.mode, only, preset=pre, readings=args.readings, no_cache=args.no_cache or None,
                               as_models=False, workers=8)
            return rec, evals, time.perf_counter() - t0

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = [pool.submit(one, r) for r in recs]
            for f in as_completed(futs):
                try:
                    rec, evals, wall = f.result()
                except Exception as e:  # keep going: one broken dialogue must not kill the run
                    failed += 1
                    print("FAILED:", type(e).__name__, str(e)[:300], flush=True)
                    continue
                line = {"conv_id": rec["conversation_id"], "language": rec["language"], "channel": rec["channel"], "wall_s": round(wall, 2),
                        "cost_kzt": round(sum(e.get("cost_kzt", 0) for e in evals), 4),
                        "cost_kzt_full": round(sum(e.get("cost_kzt_full", 0) for e in evals), 4),
                        "calls": sum((e.get("raw") or {}).get("calls", 0) for e in evals), "evals": [slim(e) for e in evals]}
                with lock:
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                    fh.flush()
                    done += 1
                ans = " ".join(f"{e['criterion_id']}={e['answer']}" + ("?" if e.get("needs_review") else "") for e in evals)
                print(f"[{done}/{len(recs)}] {rec['conversation_id']} {wall:5.1f}s {line['cost_kzt_full']:.2f}₸ | {ans}", flush=True)
    print(f"done {done}, failed {failed}, wall {time.perf_counter() - t_all:.1f}s -> {out_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
