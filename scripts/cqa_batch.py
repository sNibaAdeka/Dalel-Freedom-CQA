"""Batch-score all dialogues in a folder (DEV or CONTROL) in parallel. Saves incrementally; safe to re-run (cached).

python scripts/cqa_batch.py data/raw/dev --out results/dev_v1 [--checklist path] [--workers 8] [--limit 10]
-> results/<out>/<id>.json, results/<out>/summary.csv, results/<out>/items.csv ; also stored in the UI database.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from cqa import checklist as ck  # noqa: E402
from cqa import judge, llm, loader, store  # noqa: E402


def main(a):
    ds = loader.load_dir(a.folder)
    if a.limit:
        ds = ds[: a.limit]
    cpath = a.checklist or ck.find(a.folder) or ck.find(ROOT / "data/raw")
    items = ck.load(cpath)
    guide = Path(a.guidance).read_text() if a.guidance and Path(a.guidance).exists() else ""
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(ds)} dialogues, checklist {cpath} ({len(items)} items), {llm.provider()}:{llm.model()}, workers={a.workers}")
    t0 = time.time()
    results, errors = [], []

    def one(d):
        p = out / f"{d['id']}.json"
        if p.exists() and not a.force:
            return json.loads(p.read_text())
        r = judge.evaluate(d, items, guide)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=1))
        if not a.no_db:
            store.save_result(r)
        return r

    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(one, d): d["id"] for d in ds}
        for k, f in enumerate(as_completed(futs), start=1):
            try:
                results.append(f.result())
            except Exception as e:
                errors.append((futs[f], str(e)))
            if k % 5 == 0 or k == len(ds):
                print(f"  {k}/{len(ds)}  {time.time() - t0:.0f}s  errors={len(errors)}")
    summ = pd.DataFrame([{"dialogue_id": r["dialogue_id"], "channel": r["channel"], "lang": r["lang"], "score": r["total_score"], "critical_fail": r["critical_fail"], "needs_review": r["needs_review"], "issue": r.get("customer_issue"), "summary": r.get("summary")} for r in results])
    rows = [{"dialogue_id": r["dialogue_id"], "item_id": i["id"], "title": i["title"], "verdict": i["verdict"], "score": i["score"], "confidence": i["confidence"], "needs_review": i["needs_review"], "explanation": i["explanation"], "evidence_turns": " ".join(str(e["turn"]) for e in i["evidence"])} for r in results for i in r["items"]]
    summ.to_csv(out / "summary.csv", index=False)
    pd.DataFrame(rows).to_csv(out / "items.csv", index=False)
    if errors:
        (out / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=1))
    print(f"done {len(results)} ok, {len(errors)} errors in {time.time() - t0:.0f}s -> {out}/summary.csv, items.csv")
    if len(summ):
        print(summ[["score"]].describe().T.round(1).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="results/run")
    ap.add_argument("--checklist")
    ap.add_argument("--guidance", default="data/guidance.md")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-db", action="store_true")
    main(ap.parse_args())
