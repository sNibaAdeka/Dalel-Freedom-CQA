"""Stress test of criterion replacement (the Sunday control: «replace one criterion in the UI and apply it»).

    python scripts/stress_criterion_swap.py --repo . \
        --model openai:deepseek/deepseek-v4-flash [--n 8] [--out reports/robustness_swap.md]

Runs the product's own HTTP API in-process (FastAPI TestClient over api.app.create_app of --repo) on a fresh temporary
SQLite DB, exactly the way the UI does it:
  1. POST /api/ingest (group SYN-SWAP) with hand-written synthetic dialogues (eval/synthetic, known expected answers);
  2. POST /api/runs {version: 1} — baseline evaluation with the organizers' checklist;
  3. for each scenario: POST /api/checklists (full checklist body as the editor sends it, base_version 1) ->
     POST /api/runs {version: N, live: true, wait: true} -> GET /api/conversations/{id}?version=N.
For each scenario it records: works (HTTP + run status), planned evaluations, real LLM calls (llm listener: non-cached
calls), cache hits, wall time, cost, plausibility (agreement with the hand-written expected answers / the v1 answers)
and whether the API total equals an independent recomputation of the PDF formula from the per-criterion answers.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SYN = HERE / "eval/synthetic"

# dialogues chosen so that every scenario has known expectations (see eval/synthetic/expected.jsonl)
DEFAULT_IDS = ["syn-c2-01-ru", "syn-c5-06-ru", "syn-ctx-01-ru", "syn-c5-01-ru", "syn-c5-02-en", "syn-c4-03-kz",
               "syn-c7-02-en", "syn-c1-06-kz"]

# hand-written expectations for the NEW criteria (by construction of the dialogues)
EXPECT_REASK = {  # «не переспрашивает данные, уже сообщённые боту»
    "syn-c2-01-ru": ("no", "клиент в первом сообщении (боту) назвал Android и текст ошибки — оператор переспросил оба"),
    "syn-c5-06-ru": ("no", "номер поручения 77120 клиент сообщил боту дважды — оператор переспросил"),
    "syn-ctx-01-ru": ("yes", "оператор опёрся на ответ клиента боту «счёт пустой» и не переспросил"),
    "syn-c5-01-ru": ("yes", "оператор ничего не переспрашивал"),
    "syn-c5-02-en": ("yes", "оператор ничего не переспрашивал"),
    "syn-c4-03-kz": ("na", "клиент ничего не сообщил боту, кроме приветствия"),
    "syn-c7-02-en": ("yes", "оператор ничего не переспрашивал"),
    "syn-c1-06-kz": ("yes", "оператор ничего не переспрашивал"),
}
EXPECT_TIMING = {  # «назвал срок, если решение требует времени; N/A — решено сразу»
    "syn-c2-01-ru": ("na", "решено сразу в чате"),
    "syn-c5-06-ru": ("na", "поручение отменено сразу"),
    "syn-ctx-01-ru": ("yes", "«Счёт закроют в течение 3 рабочих дней»"),
    "syn-c5-01-ru": ("yes", "«Срок зачисления — до 5 рабочих дней»"),
    "syn-c5-02-en": ("yes", "стоп-лосс сработает с открытием сессии в 19:30"),
    "syn-c4-03-kz": ("na", "запроса нет"),
    "syn-c7-02-en": ("na", "решение не дошло до срока — обрыв"),
    "syn-c1-06-kz": ("na", "решено сразу"),
}


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE pairs into os.environ without printing them."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def norm(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return v


def frac(answer, crit: dict):
    if answer in (None, "na"):
        return None
    if crit.get("answer_type") == "scale":
        return Decimal(str(float(answer))) / Decimal(str(max(float(x) for x in crit["scale"])))
    return Decimal(1) if answer == "yes" else Decimal(0)


def recompute_total(checklist: dict, answers: dict):
    """Independent PDF formula: yes=weight, no=0, scale=value/max*weight, N/A excluded and its weight redistributed,
    critical / weight 0 excluded, 2 digits half-up; None if nothing applicable (or an answer is missing)."""
    num, den = Decimal(0), Decimal(0)
    for c in checklist["criteria"]:
        w = Decimal(str(c.get("weight") or 0))
        if c.get("critical") or w == 0:
            continue
        a = answers.get(c["id"])
        if a is None:
            return "missing"
        f = frac(a, c)
        if f is None:
            continue
        num += f * w
        den += w
    if den == 0:
        return None
    return float((num / den * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------------------------------------ scenarios
def scenarios(v1: dict) -> list[dict]:
    """Each scenario = (name, how the editor changes checklist v1, what must happen)."""
    def body(mutate, note):
        cl = copy.deepcopy(v1)
        for k in ("version", "created_at", "hash", "active", "official", "source", "parent_version"):
            cl.pop(k, None)
        for c in cl["criteria"]:
            c.pop("hash", None)
        mutate(cl)
        cl["change_note"] = note
        cl["base_version"] = 1
        return cl

    def crit(cl, cid):
        return next(c for c in cl["criteria"] if c["id"] == cid)

    def s1(cl):  # binary -> scale
        c = crit(cl, "C5")
        c.update(answer_type="scale", scale=[0, 5, 10, 15],
                 scale_labels={"15": "Вежлив и отреагировал на конкретную проблему клиента (или недовольства не было).",
                               "10": "Вежлив, но реакция на недовольство общая, без конкретики.",
                               "5": "Вежлив формально, недовольство клиента проигнорировано.",
                               "0": "Грубость, сарказм или пренебрежение."},
                 description="Оцените вежливость и реакцию на недовольство клиента по шкале.")

    def s2(cl):  # new criterion with N/A (+ keep the sum 100: C2 15 -> 5)
        crit(cl, "C2")["weight"] = 5
        crit(cl, "C2")["max_points"] = 5
        cl["criteria"].insert(4, {"id": "C9", "section": "Безопасность и решение", "title": "Информирование о сроках",
                                  "answer_type": "binary", "weight": 10, "max_points": 10, "na_allowed": True,
                                  "description": "Да — если решение требует времени, оператор назвал конкретный срок или время.",
                                  "na_rule": "N/A — вопрос решён сразу в чате, ожидать нечего.", "guidance": [],
                                  "scale": None, "scale_labels": None, "critical": False})

    def s3(cl):  # weights only
        crit(cl, "C1").update(weight=15, max_points=15)
        crit(cl, "C5").update(weight=10, max_points=10)

    def s4(cl):  # remove C7, its weight to C6
        cl["criteria"] = [c for c in cl["criteria"] if c["id"] != "C7"]
        crit(cl, "C6").update(weight=20, max_points=20)

    def s5(cl):  # replace C2 with «does not re-ask data already given to the bot»
        c = crit(cl, "C2")
        c.update(title="Не переспрашивает данные, сообщённые боту",
                 description="Да — оператор не переспрашивал у клиента данные, которые клиент уже сообщил боту или в первом сообщении.",
                 na_allowed=True, na_rule="N/A — клиент до подключения оператора не сообщил никаких данных по своему вопросу.",
                 guidance=[])

    def s6(cl):  # criterion in English (replaces C5 wording)
        c = crit(cl, "C5")
        c.update(title="Empathy and customer focus",
                 description="Yes — the operator was polite and, if the client expressed dissatisfaction or described an inconvenience, responded to that specific problem.",
                 guidance=[])

    return [
        {"key": "S1", "name": "Да/Нет → шкала (C5 → 0/5/10/15)", "body": body(s1, "C5: шкала"), "changed": ["C5"]},
        {"key": "S2", "name": "Новый критерий с N/A (C9 «Информирование о сроках», вес C2 15→5)", "body": body(s2, "добавлен C9"),
         "changed": ["C9"], "expect": ("C9", EXPECT_TIMING)},
        {"key": "S3", "name": "Смена весов (C1 10→15, C5 15→10)", "body": body(s3, "веса"), "changed": []},
        {"key": "S4", "name": "Удаление критерия (C7, вес → C6 20)", "body": body(s4, "удалён C7"), "changed": []},
        {"key": "S5", "name": "C2 → «не переспрашивает данные, уже сообщённые боту»", "body": body(s5, "C2 заменён"),
         "changed": ["C2"], "expect": ("C2", EXPECT_REASK)},
        {"key": "S6", "name": "Критерий на английском (C5 EN-формулировка)", "body": body(s6, "C5 EN"), "changed": ["C5"],
         "same_as_v1": "C5"},
    ]


# ------------------------------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--model", default="openai:deepseek/deepseek-v4-flash")
    ap.add_argument("--profile", default="cloud")
    ap.add_argument("--mode", default="fast")
    ap.add_argument("--ids", default=",".join(DEFAULT_IDS))
    ap.add_argument("--only", default="", help="scenario keys, e.g. S3,S5")
    ap.add_argument("--out", default=str(HERE / "reports/robustness_swap.md"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--live", action="store_true", help="apply runs with live=true (UI checkbox «Новый расчёт без кэша»)")
    ap.add_argument("--live-check", action="store_true", help="also measure S3 (weights) with live=true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    args.out = str(Path(args.out).resolve())  # before chdir(repo)
    load_env_file(repo / ".env")
    tmp = Path(tempfile.mkdtemp(prefix="cqa-swap-"))
    os.environ.update(CQA_DB=args.db or str(tmp / "swap.sqlite"), CQA_PROFILE=args.profile, CQA_MODEL=args.model,
                      CQA_MODE=args.mode)
    os.environ.setdefault("CQA_CACHE", str(tmp / "llm_cache"))
    sys.path.insert(0, str(repo))
    os.chdir(repo)

    from fastapi.testclient import TestClient

    from api.app import create_app
    from cqa import llm

    calls = {"net": 0, "cache": 0, "usd": 0.0, "tin": 0, "tout": 0}
    lock = threading.Lock()

    def listener(u: dict) -> None:
        with lock:
            if u.get("cache_hit"):
                calls["cache"] += 1
            else:
                calls["net"] += 1
                calls["tin"] += int(u.get("tokens_in") or 0)
                calls["tout"] += int(u.get("tokens_out") or 0)

    llm.add_listener(listener)
    client = TestClient(create_app())

    ids = [x for x in args.ids.split(",") if x]
    recs = [r for r in json.loads((SYN / "records.json").read_text(encoding="utf-8")) if r["conversation_id"] in ids]
    expected = {e["conversation_id"]: e for e in map(json.loads, (SYN / "expected.jsonl").read_text(encoding="utf-8").splitlines())}
    r = client.post("/api/ingest", params={"group_id": "SYN-SWAP"}, json=recs)
    assert r.status_code == 200, r.text[:500]
    conv_ids = [c["conversation_id"] if "conversation_id" in c else c.get("id") for c in client.get("/api/conversations", params={"group_id": "SYN-SWAP"}).json()]
    conv_ids = [c for c in conv_ids if c]
    print("ingested:", conv_ids, flush=True)

    def answers_for(version: int) -> dict:
        out = {}
        for cid in conv_ids:
            d = client.get(f"/api/conversations/{cid}", params={"version": version}).json()
            out[cid] = {"answers": {e["criterion_id"]: e.get("answer") for e in d["evals"]},
                        "expl": {e["criterion_id"]: (e.get("explanation") or "")[:160] for e in d["evals"]},
                        "total": d["score"].get("total"), "status": d["score"].get("status"), "checklist": d["checklist"]}
        return out

    def run(version: int, live: bool) -> dict:
        before = dict(calls)
        t0 = time.perf_counter()
        r = client.post("/api/runs", json={"version": version, "conv_ids": conv_ids, "mode": args.mode, "profile": args.profile,
                                           "live": live, "wait": True})
        wall = time.perf_counter() - t0
        st = r.json() if r.status_code == 200 else {"error": r.text[:300]}
        rid = st.get("run_id")
        for _ in range(600):  # wait=True should block; poll just in case
            if not rid or st.get("status") in ("done", "completed", "failed", "error", "finished"):
                break
            time.sleep(1)
            st = client.get(f"/api/runs/{rid}").json()
            wall = time.perf_counter() - t0
        return {"http": r.status_code, "status": st.get("status"), "wall_s": round(wall, 1), "run": st,
                "net_calls": calls["net"] - before["net"], "cache_hits": calls["cache"] - before["cache"],
                "tin": calls["tin"] - before["tin"], "tout": calls["tout"] - before["tout"]}

    v1 = client.get("/api/checklists/1").json()
    print("baseline run v1 ...", flush=True)
    base = run(1, live=False)
    base_ans = answers_for(1)
    print(f"baseline: status={base['status']} net_calls={base['net_calls']} cache={base['cache_hits']} wall={base['wall_s']}s", flush=True)

    results = []
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    for sc in scenarios(v1):
        if only and sc["key"] not in only:
            continue
        print(f"--- {sc['key']} {sc['name']}", flush=True)
        t0 = time.perf_counter()
        r = client.post("/api/checklists", json=sc["body"])
        save_s = time.perf_counter() - t0
        row = {"key": sc["key"], "name": sc["name"], "save_http": r.status_code, "save_s": round(save_s, 2)}
        if r.status_code != 200:
            row.update(works=False, error=r.text[:300])
            results.append(row)
            continue
        cl = r.json()
        ver = cl["version"]
        row.update(version=ver, needs_reeval=cl["diff"]["needs_reeval"], rescore_only=cl["diff"]["rescore_only"],
                   warnings=cl.get("warnings"))
        rr = run(ver, live=args.live)
        st = rr["run"]
        row.update(run_http=rr["http"], run_status=rr["status"], wall_s=rr["wall_s"], net_calls=rr["net_calls"],
                   cache_hits=rr["cache_hits"], planned=st.get("pairs_to_evaluate"), from_cache=st.get("from_cache"),
                   errors=[p for p in (st.get("per_conv") or []) if p.get("error")][:3],
                   usd=round(rr["tin"] * 0.091e-6 + rr["tout"] * 0.182e-6, 5))
        ans = answers_for(ver)
        # totals: API vs independent formula
        mism, total_changes = [], 0
        for cid, a in ans.items():
            mine = recompute_total(a["checklist"], a["answers"])
            if mine != "missing" and (mine is None) != (a["total"] is None) or (mine not in (None, "missing") and a["total"] is not None and abs(mine - a["total"]) > 0.005):
                mism.append((cid, a["total"], mine))
            if a["total"] != base_ans[cid]["total"]:
                total_changes += 1
        missing = [cid for cid, a in ans.items() if any(a["answers"].get(c["id"]) is None for c in a["checklist"]["criteria"])]
        row.update(total_mismatch=mism, totals_changed=total_changes, missing_answers=missing)
        # unchanged criteria must keep v1 answers (reused, not re-evaluated)
        kept = [(cid, c) for cid, a in ans.items() for c, v in a["answers"].items()
                if c not in sc["changed"] and c in base_ans[cid]["answers"] and norm(v) != norm(base_ans[cid]["answers"][c])]
        row["unchanged_drift"] = kept
        # plausibility
        plaus = []
        if sc.get("expect"):
            c, exp = sc["expect"]
            for cid, (want, why) in exp.items():
                if cid in ans:
                    got = ans[cid]["answers"].get(c)
                    plaus.append({"conv": cid, "want": want, "got": got, "ok": norm(got) == norm(want), "why": why, "expl": ans[cid]["expl"].get(c)})
        elif sc.get("same_as_v1"):
            c = sc["same_as_v1"]
            for cid in ans:
                want = (expected.get(cid) or {}).get("expected", {}).get(c) or base_ans[cid]["answers"].get(c)
                got = ans[cid]["answers"].get(c)
                plaus.append({"conv": cid, "want": want, "got": got, "ok": norm(got) == norm(want), "why": "как C5 v1 / эталон синтетики",
                              "expl": ans[cid]["expl"].get(c)})
        elif sc["key"] == "S1":
            for cid in ans:
                v1a = base_ans[cid]["answers"].get("C5")
                got = ans[cid]["answers"].get("C5")
                ok = got is not None and ((v1a == "yes" and float(got) >= 10) or (v1a == "no" and float(got) <= 5))
                plaus.append({"conv": cid, "want": f"v1 C5={v1a} → {'≥10' if v1a == 'yes' else '≤5'}", "got": got, "ok": ok,
                              "why": "монотонность со старой бинарной оценкой", "expl": ans[cid]["expl"].get("C5")})
        row["plausibility"] = plaus
        row["works"] = rr["http"] == 200 and not mism and not missing and not row["errors"]
        results.append(row)
        print(json.dumps({k: row[k] for k in ("key", "works", "net_calls", "planned", "wall_s", "totals_changed")}, ensure_ascii=False), flush=True)

    if args.live_check:
        v3 = next((r for r in results if r["key"] == "S3" and r.get("version")), None)
        if v3:
            rr = run(v3["version"], live=True)
            results.append({"key": "S3-live", "name": "Смена весов, но прогон с галочкой «Новый расчёт без кэша» (live)",
                            "version": v3["version"], "save_http": 200, "save_s": 0, "needs_reeval": [], "rescore_only": v3["rescore_only"],
                            "run_http": rr["http"], "run_status": rr["status"], "wall_s": rr["wall_s"], "net_calls": rr["net_calls"],
                            "cache_hits": rr["cache_hits"], "planned": rr["run"].get("pairs_to_evaluate"), "from_cache": rr["run"].get("from_cache"),
                            "errors": [], "missing_answers": [], "unchanged_drift": [], "total_mismatch": [], "totals_changed": "—",
                            "usd": round(rr["tin"] * 0.091e-6 + rr["tout"] * 0.182e-6, 5), "works": rr["http"] == 200})
    write_report(Path(args.out), args, base, results, conv_ids)
    print("report ->", args.out)
    return 0


def write_report(out: Path, args, base: dict, results: list[dict], conv_ids: list[str]) -> None:
    L = ["# Стресс замены критерия (контроль)", "",
         f"Продукт: `{args.repo}` (HTTP API в процессе, TestClient, временная БД). Модель `{args.model}`, режим `{args.mode}`, "
         f"профиль `{args.profile}`. Диалоги: {len(conv_ids)} синтетических из `eval/synthetic` (ожидаемые ответы известны). "
         f"Базовый прогон v1: статус {base['status']}, LLM-вызовов {base['net_calls']} (+{base['cache_hits']} из кэша), {base['wall_s']} с.",
         "", "Каждый сценарий — новая версия от v1 через `POST /api/checklists` (полное тело, как редактор) → `POST /api/runs` "
         "{version, live: " + str(bool(args.live)).lower() + "} → чтение карточек. «Вызовы LLM» — реальные сетевые вызовы (без кэша); «план» — сколько "
         "(диалог × критерий) планировщик отправил в оценщик.", "",
         "| # | сценарий | работает | переоценка (diff) | план | вызовы LLM | время, с | $ | итог = формула | итог изменился | правдоподобие |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        pl = r.get("plausibility") or []
        pls = f"{sum(p['ok'] for p in pl)}/{len(pl)}" if pl else "—"
        L.append(f"| {r['key']} | {r['name']} | {'да' if r.get('works') else 'НЕТ'} | {', '.join(r.get('needs_reeval') or []) or '—'} | "
                 f"{r.get('planned', '—')} | {r.get('net_calls', '—')} | {r.get('wall_s', '—')} | {r.get('usd', '—')} | "
                 f"{'да' if not r.get('total_mismatch') else 'НЕТ ' + str(r['total_mismatch'][:2])} | {r.get('totals_changed', '—')}/{len(conv_ids)} | {pls} |")
    L.append("")
    for r in results:
        L += [f"## {r['key']}. {r['name']}", ""]
        if r.get("error"):
            L += [f"Ошибка сохранения: HTTP {r['save_http']}: `{r['error']}`", ""]
            continue
        L.append(f"- версия {r['version']}; сохранение {r['save_s']} с; diff: переоценка {r['needs_reeval']}, только пересчёт {r['rescore_only']}; "
                 f"предупреждения: {r.get('warnings') or '—'}")
        L.append(f"- прогон: HTTP {r['run_http']}, статус {r['run_status']}, вызовов LLM {r['net_calls']} (план {r['planned']}, переиспользовано {r['from_cache']}), "
                 f"{r['wall_s']} с; ошибки: {r['errors'] or 'нет'}; без ответа: {r['missing_answers'] or 'нет'}")
        L.append(f"- неизменённые критерии сохранили ответы v1: {'да' if not r['unchanged_drift'] else 'НЕТ ' + str(r['unchanged_drift'][:4])}")
        if r.get("plausibility"):
            L += ["", "| диалог | ждали | модель | | почему ждали | объяснение модели |", "|---|---|---|---|---|---|"]
            for p in r["plausibility"]:
                L.append(f"| {p['conv']} | {p['want']} | {p['got']} | {'✓' if p['ok'] else '✗'} | {p['why']} | {(p['expl'] or '').replace('|', '/')} |")
        L.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
