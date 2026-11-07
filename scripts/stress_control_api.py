"""Control rehearsal through the UI's HTTP API (what the operator clicks on Sunday), with a timer.

    python scripts/stress_control_api.py --repo .            # auto model
    python scripts/stress_control_api.py --model claude_cli:claude-haiku-5-5 --profile claude --mode fast --tag 0130

Steps, each through the product's HTTP API (FastAPI TestClient over api.app.create_app of --repo, fresh temp DB):
  1. POST /api/ingest?group_id=REH-<tag>  — 10 DEV holdout dialogues uploaded as a new set (like the board's file upload);
  2. POST /api/runs {live: true, force: true} — evaluation of the new set exactly as the board sends it after an upload;
  3. POST /api/checklists — editor saves v1 with C2 replaced by «не переспрашивает данные, сообщённые боту»;
  4. POST /api/runs {version: N, live: false, force: false} — apply WITHOUT the live checkbox;
     check: only the changed criterion is re-evaluated (pairs_to_evaluate == number of dialogues, LLM calls == that),
     every other criterion keeps its answer, totals = independent PDF formula;
  5. POST /api/checklists/N/activate, GET /api/conversations (list shows the new version's totals).
Model: --model auto probes OpenRouter; if it answers -> cloud accurate (v4-flash, v4-pro arbiter), else claude_cli Haiku fast.
Output: reports/control_rehearsal_<tag>.md — ids, answers and numbers only (no transcript quotes).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
from stress_control_rehearsal import MAIN, NEW_C2, PRICES, pick_ids  # noqa: E402
from stress_criterion_swap import load_env_file, norm, recompute_total  # noqa: E402

FLASH, PRO, HAIKU = "openai:deepseek/deepseek-v4-flash", "openai:deepseek/deepseek-v4-pro", "claude_cli:claude-haiku-5-5"


def openrouter_ok() -> bool:
    import httpx

    base, key = os.environ.get("CQA_BASE_URL") or "", os.environ.get("CQA_API_KEY") or ""
    if "openrouter" not in base or not key:
        return False
    try:
        r = httpx.post(base.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + key}, timeout=60,
                       json={"model": FLASH.split(":", 1)[1], "messages": [{"role": "user", "content": "ok"}], "max_tokens": 5})
        return r.status_code == 200
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--model", default="auto", help="auto | provider:model | '' (profile preset)")
    ap.add_argument("--arbiter", default="")
    ap.add_argument("--profile", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--tag", default=time.strftime("%H%M"))
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    out = HERE / f"reports/control_rehearsal_{args.tag}.md"
    load_env_file(repo / ".env")
    if args.model == "auto":
        if openrouter_ok():
            args.model, args.arbiter = FLASH, args.arbiter or PRO
            args.profile, args.mode = args.profile or "cloud", args.mode or "accurate"
            why = "OpenRouter отвечает → облачный accurate"
        else:
            args.model, args.arbiter = HAIKU, ""
            args.profile, args.mode = args.profile or "claude", args.mode or "fast"
            why = "OpenRouter недоступен (402/нет ключа) → claude_cli Haiku fast"
    else:
        args.profile, args.mode = args.profile or "cloud", args.mode or "fast"
        why = "модель задана вручную"
    tmp = Path(tempfile.mkdtemp(prefix="cqa-reh-api-"))
    os.environ.update(CQA_DB=str(tmp / "reh.sqlite"), CQA_PROFILE=args.profile, CQA_CACHE=str(tmp / "llm_cache"), CQA_MODE=args.mode)
    if args.model:
        os.environ["CQA_MODEL"] = args.model
    if args.arbiter:
        os.environ["CQA_ARBITER_MODEL"] = args.arbiter
    sys.path.insert(0, str(repo))
    os.chdir(repo)

    import subprocess

    from fastapi.testclient import TestClient

    from api.app import create_app
    from cqa import llm

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    usage, lock = Counter(), threading.Lock()

    def listener(u: dict) -> None:
        with lock:
            if u.get("cache_hit"):
                usage["cache"] += 1
                return
            usage["net"] += 1
            usage["tin"] += int(u.get("tokens_in") or 0)
            usage["tout"] += int(u.get("tokens_out") or 0)
            p = next((v for k, v in PRICES.items() if k in str(u.get("model"))), None)
            usage["usd_x1e6"] += int((u.get("tokens_in") or 0) * p[0] + (u.get("tokens_out") or 0) * p[1]) if p \
                else int(float(u.get("cost_usd") or 0) * 1e6)
            usage["model:" + str(u.get("model"))] += 1

    llm.add_listener(listener)
    data_path = repo / "data/raw/dev_records.json"
    records = json.loads((data_path if data_path.exists() else MAIN / "data/raw/dev_records.json").read_text(encoding="utf-8"))
    ids = pick_ids(repo / "eval/split.json", records, args.n)
    subset = [r for r in records if r["conversation_id"] in ids]
    group = f"REH-{args.tag}"
    timeline, problems = [], []

    def mark(step: str, t0: float, **kw) -> None:
        timeline.append({"step": step, "sec": round(time.perf_counter() - t0, 1), **kw})
        print(f"[{timeline[-1]['sec']:7.1f}s] {step} {json.dumps(kw, ensure_ascii=False, default=str)[:300]}", flush=True)

    T = time.perf_counter()
    c = TestClient(create_app())
    preset = llm.preset(args.mode, args.profile)
    mark("старт сервера", T, why=why, model=preset.get("model"), votes=preset.get("votes"), arbiter=(preset.get("arbiter") or {}).get("model"))

    # 1. upload the set
    r = c.post("/api/ingest", params={"group_id": group}, json=subset)
    if r.status_code != 200:
        problems.append(f"ingest HTTP {r.status_code}: {r.text[:200]}")
    conv_ids = [row.get("conv_id") or row.get("id") for row in c.get("/api/conversations", params={"group_id": group}).json()]
    conv_ids = [x for x in conv_ids if x]
    mark("1. загрузка набора (POST /api/ingest)", T, http=r.status_code, n=len(conv_ids))

    def run(body: dict) -> tuple[dict, int, float]:
        before, t = usage["net"], time.perf_counter()
        r = c.post("/api/runs", json={"conv_ids": conv_ids, "mode": args.mode, "profile": args.profile, "wait": True, **body})
        st = r.json() if r.status_code == 200 else {"status": f"HTTP {r.status_code}", "error": r.text[:300]}
        return st, usage["net"] - before, round(time.perf_counter() - t, 1)

    def snapshot(version: int) -> dict:
        snap = {}
        for cid in conv_ids:
            d = c.get(f"/api/conversations/{cid}", params={"version": version}).json()
            snap[cid] = {"answers": {e["criterion_id"]: e.get("answer") for e in d["evals"]}, "total": d["score"].get("total"),
                         "checklist": d["checklist"], "source_id": (d.get("meta") or {}).get("source_id") or cid,
                         "needs_review": d.get("needs_review")}
        return snap

    # 2. evaluate the new set as the board does after an upload (live + force)
    st1, calls1, s1 = run({"version": 1, "live": True, "force": True})
    v1 = snapshot(1)
    missing1 = [f"{cid}:{k}" for cid, s in v1.items() for k in [cc["id"] for cc in s["checklist"]["criteria"]] if s["answers"].get(k) is None]
    if st1.get("status") != "completed" or missing1:
        problems.append(f"оценка набора: статус {st1.get('status')}, без ответа {missing1[:6]}")
    mark("2. оценка набора (POST /api/runs live)", T, step_s=s1, status=st1.get("status"), pairs=st1.get("pairs_to_evaluate"),
         llm_calls=calls1, missing=len(missing1), needs_review=sum(bool(s["needs_review"]) for s in v1.values()))

    # silver agreement (report only)
    gold = {}
    for p in (repo / "eval/gold/silver.jsonl", MAIN / "eval/gold/silver.jsonl"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                g = json.loads(line)
                gold[(g["conv_id"], g["criterion_id"])] = g["answer"]
            break
    per_crit, dis = {}, []
    for cid, s in v1.items():
        for k, a in s["answers"].items():
            g = gold.get((s["source_id"], k))
            if g is None or a is None:
                continue
            ok = norm(g) == norm(a)
            per_crit.setdefault(k, []).append(ok)
            if not ok:
                dis.append(f"{s['source_id']} {k}: silver {g} / модель {a}")

    # 3. editor saves the replaced criterion
    t = time.perf_counter()
    body = c.get("/api/checklists/1").json()
    for k in ("version", "created_at", "active", "official", "source", "parent_version"):
        body.pop(k, None)
    for cr in body["criteria"]:
        cr.pop("hash", None)
        if cr["id"] == "C2":
            cr.update(NEW_C2)
    body.update(change_note="Репетиция: C2 заменён", base_version=1)
    r = c.post("/api/checklists", json=body)
    cl = r.json() if r.status_code == 200 else {}
    new_v = cl.get("version")
    if r.status_code != 200:
        problems.append(f"сохранение чек-листа HTTP {r.status_code}: {r.text[:200]}")
    mark("3. замена критерия (POST /api/checklists)", T, step_s=round(time.perf_counter() - t, 2), http=r.status_code, version=new_v,
         needs_reeval=(cl.get("diff") or {}).get("needs_reeval"))

    # 4. apply WITHOUT live
    st2, calls2, s2 = run({"version": new_v, "live": False, "force": False})
    v2 = snapshot(new_v)
    expected_pairs = len(conv_ids) * len((cl.get("diff") or {}).get("needs_reeval") or [])
    only_changed = st2.get("pairs_to_evaluate") == expected_pairs
    drift = [f"{cid}:{k} {v1[cid]['answers'].get(k)}→{a}" for cid, s in v2.items() for k, a in s["answers"].items()
             if k != "C2" and norm(a) != norm(v1[cid]["answers"].get(k))]
    formula_bad = []
    for cid, s in v2.items():
        mine = recompute_total(s["checklist"], s["answers"])
        if not ((mine in (None, "missing") and s["total"] is None) or (isinstance(mine, float) and s["total"] is not None and abs(mine - s["total"]) < 0.005)):
            formula_bad.append(f"{cid}: API {s['total']} / формула {mine}")
    if not only_changed:
        problems.append(f"применение: план {st2.get('pairs_to_evaluate')} пар, ждали {expected_pairs} (только изменённый критерий)")
    if drift:
        problems.append(f"неизменённые критерии поменяли ответ: {drift[:5]}")
    if formula_bad:
        problems.append(f"итог ≠ формула: {formula_bad[:3]}")
    mark("4. применение без live (POST /api/runs)", T, step_s=s2, status=st2.get("status"), pairs=st2.get("pairs_to_evaluate"),
         expected_pairs=expected_pairs, reused=st2.get("from_cache"), llm_calls=calls2, drift=len(drift), formula_ok=not formula_bad)

    # 5. activate, list shows the new version
    t = time.perf_counter()
    r = c.post(f"/api/checklists/{new_v}/activate")
    rows = c.get("/api/conversations", params={"group_id": group}).json()
    list_ok = r.status_code == 200 and all(row.get("checklist_version") == new_v for row in rows)
    if not list_ok:
        problems.append(f"активация HTTP {r.status_code}; список показывает версии {sorted({row.get('checklist_version') for row in rows})}")
    mark("5. активация + список (POST activate, GET /api/conversations)", T, step_s=round(time.perf_counter() - t, 2), http=r.status_code,
         list_ok=list_ok)
    total_s = round(time.perf_counter() - T, 1)

    n_ok = sum(sum(v) for v in per_crit.values())
    n_all = sum(len(v) for v in per_crit.values())
    L = [f"# Репетиция контроля {args.tag} (через HTTP API интерфейса)", "",
         f"Код: `{repo}` @ `{commit}`. {why}: режим `{args.mode}`, профиль `{args.profile}`, модель `{preset.get('model')}`, "
         f"голосов {preset.get('votes')}, арбитр `{(preset.get('arbiter') or {}).get('model')}`. Набор: {len(conv_ids)} DEV-диалогов "
         f"holdout как новая группа `{group}` (временная БД).", "",
         f"**{'ВСЁ ОК' if not problems else 'ЕСТЬ ПРОБЛЕМЫ'}. От загрузки до активированной новой версии: {total_s} с ({total_s / 60:.1f} мин).** "
         f"LLM-вызовов {usage['net']}, токены {usage['tin']}/{usage['tout']}, ≈ ${usage['usd_x1e6'] / 1e6:.4f}.", ""]
    if problems:
        L += ["## Проблемы", ""] + [f"- {p}" for p in problems] + [""]
    L += ["## Хронометраж", "", "| t, с | шаг | детали |", "|---|---|---|"]
    for s in timeline:
        det = {k: v for k, v in s.items() if k not in ("step", "sec")}
        L.append(f"| {s['sec']} | {s['step']} | {json.dumps(det, ensure_ascii=False, default=str)[:400].replace('|', '/')} |")
    L += ["", f"## Применение без live: переоценён только изменённый критерий — {'да' if only_changed and not drift else 'НЕТ'}", "",
          f"План {st2.get('pairs_to_evaluate')} пар (ждали {expected_pairs}), переиспользовано {st2.get('from_cache')}, LLM-вызовов {calls2}; "
          f"дрейф неизменённых критериев: {len(drift)}; итог = формула: {'да' if not formula_bad else 'НЕТ'}.", "",
          "| диалог | итог v1 | итог новый | C2 был | C2 новый |", "|---|---|---|---|---|"]
    for cid in conv_ids:
        L.append(f"| {v1[cid]['source_id']} | {v1[cid]['total']} | {v2[cid]['total']} | {v1[cid]['answers'].get('C2')} | {v2[cid]['answers'].get('C2')} |")
    L += ["", f"## Согласие с silver (только отчёт): {n_ok}/{n_all} = {100 * n_ok / max(1, n_all):.1f}%", "",
          "| критерий | согласие |", "|---|---|"] + [f"| {k} | {sum(v)}/{len(v)} |" for k, v in sorted(per_crit.items())]
    if dis:
        L += ["", "Расхождения: " + "; ".join(dis)]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("report ->", out)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
