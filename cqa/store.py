"""SQLite store: AI results (latest per dialogue) + human reviews (overrides & comments)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path

DB = Path(os.environ.get("CQA_DB", "results/cqa.sqlite"))
_lock = threading.Lock()


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("create table if not exists results (dialogue_id text primary key, json text, created text)")
    c.execute("create table if not exists reviews (dialogue_id text, item_id text, verdict text, comment text, reviewer text, created text, primary key (dialogue_id, item_id))")
    c.execute("create table if not exists dialogue_reviews (dialogue_id text primary key, status text, comment text, reviewer text, created text)")
    return c


def save_result(res: dict) -> None:
    with _lock, _conn() as c:
        c.execute("insert or replace into results values (?,?,?)", (res["dialogue_id"], json.dumps(res, ensure_ascii=False), res.get("created")))


def get_result(did: str) -> dict | None:
    with _conn() as c:
        r = c.execute("select json from results where dialogue_id=?", (did,)).fetchone()
    return json.loads(r[0]) if r else None


def all_results() -> dict[str, dict]:
    with _conn() as c:
        return {d: json.loads(j) for d, j in c.execute("select dialogue_id, json from results")}


def save_review(did: str, item_id: str, verdict: str | None, comment: str = "", reviewer: str = "qa") -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _lock, _conn() as c:
        if verdict is None and not comment:
            c.execute("delete from reviews where dialogue_id=? and item_id=?", (did, item_id))
        else:
            c.execute("insert or replace into reviews values (?,?,?,?,?,?)", (did, item_id, verdict, comment, reviewer, now))


def save_dialogue_review(did: str, status: str, comment: str = "", reviewer: str = "qa") -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _lock, _conn() as c:
        c.execute("insert or replace into dialogue_reviews values (?,?,?,?,?)", (did, status, comment, reviewer, now))


def reviews(did: str | None = None) -> dict:
    with _conn() as c:
        q = "select dialogue_id, item_id, verdict, comment, reviewer, created from reviews" + (" where dialogue_id=?" if did else "")
        rows = c.execute(q, (did,) if did else ()).fetchall()
        dq = "select dialogue_id, status, comment, reviewer, created from dialogue_reviews" + (" where dialogue_id=?" if did else "")
        drows = c.execute(dq, (did,) if did else ()).fetchall()
    out: dict = {}
    for d, i, v, cm, rv, cr in rows:
        out.setdefault(d, {"items": {}, "status": None})["items"][i] = {"verdict": v, "comment": cm, "reviewer": rv, "created": cr}
    for d, s, cm, rv, cr in drows:
        out.setdefault(d, {"items": {}, "status": None})["status"] = {"status": s, "comment": cm, "reviewer": rv, "created": cr}
    return out


def final_verdicts(res: dict, rev: dict | None) -> dict:
    """Merge AI result with human overrides -> final item verdicts + recomputed score."""
    from .judge import VERDICT_SCORE

    items = []
    tot = mx = 0.0
    for it in res["items"]:
        o = (rev or {}).get("items", {}).get(it["id"])
        v = o["verdict"] if o and o.get("verdict") else it["verdict"]
        sc = VERDICT_SCORE.get(v)
        if sc is not None:
            tot += sc * it["weight"]
            mx += it["weight"]
        items.append({**it, "final_verdict": v, "overridden": bool(o and o.get("verdict") and o["verdict"] != it["verdict"]), "review_comment": (o or {}).get("comment", "")})
    return {"items": items, "final_score": round(100 * tot / mx, 1) if mx else None}
