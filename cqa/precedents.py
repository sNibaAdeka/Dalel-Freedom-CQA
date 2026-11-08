"""Precedents: k labelled DEV dialogues most similar to the one being graded, shown to the model as calibration
examples (calibration v7.1: +4–5 p.p. agreement with the reference on the DEV holdout with Haiku).

The bank is a LOCAL file built by scripts/build_precedents.py from the DEV transcripts and the team's reference
labels. It contains real transcripts, so it lives in data/processed/ (ignored by Git) and is optional: without it
prompts are unchanged. Similarity: multilingual bge-m3 embeddings via Ollama (finds the same story in RU/KZ/EN);
if the embedding service is unavailable, a character TF-IDF fallback is used.

Labels are shown only for criteria whose current hash equals the hash the label was made for, so a criterion
replaced in the UI never receives stale precedent answers. The graded dialogue itself is never its own precedent.
Env: CQA_PRECEDENTS (bank path, or "off"), CQA_PRECEDENTS_K (default 3), CQA_EMBED_URL (default CQA_OLLAMA_URL or
http://localhost:11434), CQA_EMBED_MODEL (default bge-m3).
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BANK = ROOT / "data" / "processed" / "precedents.json"
_lock = threading.Lock()
_bank_cache: dict = {}
_query_cache: dict = {}


def bank_path() -> Path | None:
    v = os.environ.get("CQA_PRECEDENTS", "").strip()
    if v.lower() in ("off", "0", "false", "no"):
        return None
    p = Path(v) if v else DEFAULT_BANK
    return p if p.exists() else None


def load_bank() -> dict | None:
    p = bank_path()
    if p is None:
        return None
    key, mtime = str(p), p.stat().st_mtime
    with _lock:
        if _bank_cache.get(key, (None,))[0] != mtime:
            _bank_cache[key] = (mtime, json.loads(p.read_text(encoding="utf-8")))
        return _bank_cache[key][1]


def embed_text(conv: dict) -> str:
    keep = [str(m.get("text", "")) for m in conv.get("messages") or [] if m.get("speaker") in ("client", "operator")]
    return " \n".join(keep)[:2500]


def embed(texts: list[str]) -> list[list[float]] | None:
    import httpx

    base = (os.environ.get("CQA_EMBED_URL") or os.environ.get("CQA_OLLAMA_URL") or "http://localhost:11434").rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    try:
        r = httpx.post(f"{base}/api/embed", json={"model": os.environ.get("CQA_EMBED_MODEL", "bge-m3"), "input": texts}, timeout=60)
        r.raise_for_status()
        return r.json()["embeddings"]
    except Exception:
        return None


def _cos(a, b) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _tfidf_rank(query: str, items: list[dict]) -> list[tuple[dict, float]]:
    from sklearn.feature_extraction.text import TfidfVectorizer

    docs = [it.get("embed_text") or it.get("text", "") for it in items]
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1, sublinear_tf=True)
    m = vec.fit_transform(docs + [query])
    sims = (m[:-1] @ m[-1].T).toarray().ravel()
    return sorted(zip(items, sims.tolist()), key=lambda t: -t[1])


def find(conv: dict, k: int | None = None) -> list[tuple[dict, float]]:
    """Top-k bank items most similar to conv (never conv itself). [] when no bank is configured."""
    bank = load_bank()
    if not bank or not bank.get("items"):
        return []
    k = int(os.environ.get("CQA_PRECEDENTS_K", "3")) if k is None else k
    items = [it for it in bank["items"] if str(it.get("id")) != str(conv.get("id"))]
    if not items or k <= 0:
        return []
    query = embed_text(conv)
    ckey = (conv.get("id"), hash(query))
    with _lock:
        cached = _query_cache.get(ckey)
    if cached is None:
        ranked = None
        if all(it.get("emb") for it in items):
            q = embed([query])
            if q:
                ranked = sorted(((it, _cos(q[0], it["emb"])) for it in items), key=lambda t: -t[1])
        if ranked is None:
            ranked = _tfidf_rank(query, items)
        cached = [(it["id"], s) for it, s in ranked[:max(k, 5)]]
        with _lock:
            _query_cache[ckey] = cached
    by_id = {it["id"]: it for it in items}
    return [(by_id[i], s) for i, s in cached if i in by_id][:k]


MAX_PRECEDENT_CHARS = int(os.environ.get("CQA_PRECEDENT_CHARS", "3500"))


def _clip(text: str, limit: int = MAX_PRECEDENT_CHARS) -> str:
    """Long calls as precedents: keep the opening (contact, request) and the closing (C7) lines, drop the middle."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    head, tail, size = [], [], 0
    for ln in lines:  # ~65% of the budget for the beginning
        if size + len(ln) > limit * 0.65:
            break
        head.append(ln)
        size += len(ln) + 1
    for ln in reversed(lines[len(head):]):
        if size + len(ln) > limit:
            break
        tail.insert(0, ln)
        size += len(ln) + 1
    return "\n".join(head + ["[… середина диалога опущена …]"] + tail)


def block(conv: dict, criteria: list[dict]) -> str:
    """Prompt section with precedents (labels only for unchanged criteria), or ""."""
    try:
        found = find(conv)
    except Exception:
        return ""
    ids = [c["id"] for c in criteria]
    hashes = {c["id"]: c.get("hash") for c in criteria}
    parts = []
    for i, (it, _s) in enumerate(found, 1):
        labels = it.get("answers") or {}
        lh = it.get("hashes") or {}
        shown = [f"{cid}={json.dumps(labels[cid], ensure_ascii=False)}" for cid in ids
                 if cid in labels and (not lh.get(cid) or not hashes.get(cid) or lh[cid] == hashes[cid])]
        if not shown:
            continue
        parts.append(f"--- Прецедент {i} ({it.get('channel', '')}, {it.get('language', '')}) ---\n{_clip(it['text'])}\n"
                     f"ЭТАЛОННЫЕ ОТВЕТЫ ЭКСПЕРТА: {', '.join(shown)}")
    if not parts:
        return ""
    return ("ПРЕЦЕДЕНТЫ — похожие диалоги, уже оценённые экспертом по этому чек-листу (реплики бота опущены). Они показывают, как "
            "эксперт применяет правила. Оценивай ТЕКУЩИЙ диалог только по его собственным репликам и цитируй только его реплики; "
            "не переноси ответы механически — если ситуация отличается, ответ должен отличаться.\n\n" + "\n\n".join(parts))
