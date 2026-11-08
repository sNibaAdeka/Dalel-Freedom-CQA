"""Build the local precedent bank for cqa.precedents (calibration v7.1).

  python scripts/build_precedents.py [--data data/raw/dev_records.json] [--gold eval/gold/silver.jsonl]
                                     [--checklist data/checklists/v1.json] [--pool all|tune] [--out data/processed/precedents.json]

The bank holds compact DEV transcripts (bot lines dropped), the reference answers and bge-m3 embeddings. It contains
real transcripts: it is written to data/processed/ (ignored by Git) and must never be committed. Without Ollama/bge-m3
the bank is still written and cqa.precedents falls back to TF-IDF similarity.
--pool tune keeps only eval/split.json "tune" dialogues (use it to measure on the DEV holdout without leakage);
the default "all" uses every labelled DEV dialogue (for new, e.g. control, dialogues).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import precedents  # noqa: E402
from cqa.evaluator import as_checklist, as_conversation  # noqa: E402


def compact(conv: dict) -> str:
    out = []
    for m in conv["messages"]:
        if m["speaker"] == "bot":
            continue
        out.append(f"[{m['idx']}] {m['speaker']}: {' '.join(str(m['text']).split())[:300]}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data/raw/dev_records.json"))
    ap.add_argument("--gold", default=str(ROOT / "eval/gold/silver.jsonl"))
    ap.add_argument("--checklist", default=str(ROOT / "data/checklists/v1.json"))
    ap.add_argument("--pool", choices=["all", "tune"], default="all")
    ap.add_argument("--out", default=str(ROOT / "data/processed/precedents.json"))
    a = ap.parse_args()
    recs = json.loads(Path(a.data).read_text(encoding="utf-8"))
    recs = recs["data"]["records"] if isinstance(recs, dict) else recs
    gold: dict = {}
    for line in Path(a.gold).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            gold.setdefault(r["conv_id"], {})[r["criterion_id"]] = r["answer"]
    hashes = {c["id"]: c.get("hash") for c in as_checklist(json.loads(Path(a.checklist).read_text(encoding="utf-8")))["criteria"]}
    keep = None
    if a.pool == "tune":
        keep = set(json.loads((ROOT / "eval/split.json").read_text(encoding="utf-8"))["tune"])
    items = []
    for rec in recs:
        cid = rec["conversation_id"]
        if cid not in gold or (keep is not None and cid not in keep):
            continue
        conv = as_conversation(rec)
        items.append({"id": cid, "channel": conv["channel"], "language": conv.get("language"), "text": compact(conv),
                      "embed_text": precedents.embed_text(conv), "answers": gold[cid],
                      "hashes": {k: hashes.get(k) for k in gold[cid]}})
    embs = precedents.embed([it["embed_text"] for it in items])
    if embs:
        for it, e in zip(items, embs):
            it["emb"] = e
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"version": "v7.1", "pool": a.pool, "embed_model": "bge-m3" if embs else None, "items": items},
                              ensure_ascii=False), encoding="utf-8")
    print(f"{len(items)} precedents -> {out} (embeddings: {'bge-m3' if embs else 'none, TF-IDF fallback'})")


if __name__ == "__main__":
    main()
