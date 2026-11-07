"""Control rehearsal with a timer: 10 DEV dialogues as if they were the new control set, then the criterion swap.

    python scripts/stress_control_rehearsal.py --repo . \
        --mode accurate --model openai:deepseek/deepseek-v4-flash --arbiter openai:deepseek/deepseek-v4-pro
    # use the product's own config (profile presets from config/models.json of --repo): --model "" --arbiter ""

Steps (each timed), all through the product's code of --repo on a fresh temporary DB:
  1. ingest 10 DEV dialogues as group REH-<HHMM> (as records, like a new set from the organizers' API);
  2. scripts/control_run.run_control: live evaluation (no cache), exports json/csv/trace to a temp dir;
  3. agreement with the silver reference (eval/gold/silver.jsonl) — reported only, never used for tuning;
  4. criterion swap like in the UI: new version = v1 with C2 replaced by «не переспрашивает данные, сообщённые боту»
     (ChecklistStore.create, the same call as POST /api/checklists);
  5. apply: RunManager.start_run(version=new, live=False) — only the changed criterion must be re-evaluated;
     then activate. Checks: LLM calls, reused cells, totals recomputed = independent formula.
Output: reports/control_rehearsal_<HHMM>.md (ids, answers and numbers only — no transcript quotes).
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
from stress_criterion_swap import load_env_file, norm, recompute_total  # noqa: E402

MAIN = Path(__file__).resolve().parents[1]

NEW_C2 = {"title": "Не переспрашивает данные, сообщённые боту",
          "description": "Да — оператор не переспрашивал у клиента данные, которые клиент уже сообщил боту или в первом сообщении.",
          "na_allowed": True, "na_rule": "N/A — клиент до подключения оператора не сообщил никаких данных по своему вопросу.",
          "guidance": []}
PRICES = {"deepseek-v4-flash": (0.091, 0.182), "deepseek-v4-pro": (0.687648, 1.375296)}


def pick_ids(split_path: Path, records: list[dict], n: int) -> list[str]:
    hold = json.loads(split_path.read_text(encoding="utf-8"))["holdout"]
    by = {r["conversation_id"]: r for r in records}
    calls = [i for i in hold if i in by and "call" in by[i]["channel"]]
    chats = [i for i in hold if i in by and "call" not in by[i]["channel"]]
    langs = {"RU": [], "KZ": [], "EN": []}
    for i in chats:
        langs[by[i]["language"]].append(i)
    out = calls[:1]
    while len(out) < n and any(langs.values()):
        for lang in ("RU", "KZ", "EN"):
            if langs[lang] and len(out) < n:
                out.append(langs[lang].pop(0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--mode", default="accurate", choices=["fast", "accurate"])
    ap.add_argument("--profile", default="cloud")
    ap.add_argument("--model", default="openai:deepseek/deepseek-v4-flash", help="'' = use the profile preset")
    ap.add_argument("--arbiter", default="openai:deepseek/deepseek-v4-pro", help="'' = use the profile preset")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--ids", default="")
    ap.add_argument("--tag", default=time.strftime("%H%M"))
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    load_env_file(repo / ".env")
    tmp = Path(tempfile.mkdtemp(prefix="cqa-reh-"))
    os.environ.update(CQA_DB=str(tmp / "reh.sqlite"), CQA_PROFILE=args.profile, CQA_CACHE=str(tmp / "llm_cache"))
    if args.model:
        os.environ["CQA_MODEL"] = args.model
    if args.arbiter:
        os.environ["CQA_ARBITER_MODEL"] = args.arbiter
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "scripts"))
    os.chdir(repo)

    import subprocess

    from api.state import AppState
    from control_run import run_control
    from cqa import llm
    from cqa.runs import current_evals

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    usage = Counter()
    lock = threading.Lock()

    def listener(u: dict) -> None:
        with lock:
            key = "cache" if u.get("cache_hit") else "net"
            usage[key] += 1
            if not u.get("cache_hit"):
                p = next((v for k, v in PRICES.items() if k in str(u.get("model"))), None)
                usage["usd_x1e6"] += int((u.get("tokens_in") or 0) * p[0] + (u.get("tokens_out") or 0) * p[1]) if p \
                    else int(float(u.get("cost_usd") or 0) * 1e6)  # claude_cli reports its own cost
                usage["tin"] += int(u.get("tokens_in") or 0)
                usage["tout"] += int(u.get("tokens_out") or 0)
                usage["model:" + str(u.get("model"))] += 1

    llm.add_listener(listener)
    data_path = repo / "data/raw/dev_records.json"
    if not data_path.exists():
        data_path = MAIN / "data/raw/dev_records.json"
    records = json.loads(data_path.read_text(encoding="utf-8"))
    ids = [x for x in args.ids.split(",") if x] or pick_ids(repo / "eval/split.json", records, args.n)
    subset = [r for r in records if r["conversation_id"] in ids]
    group = f"REH-{args.tag}"
    timeline = []

    def mark(step: str, t0: float, **kw) -> None:
        timeline.append({"step": step, "sec": round(time.perf_counter() - t0, 1), **kw})
        print(f"[{timeline[-1]['sec']:7.1f}s] {step} {json.dumps(kw, ensure_ascii=False, default=str)[:300]}", flush=True)

    T = time.perf_counter()
    state = AppState(db_path=os.environ["CQA_DB"], autoload=False, calibrate_audio=False, workers=args.workers)
    preset = llm.preset(args.mode, args.profile)
    mark("старт, оценщик готов", T, ready=state.evaluator_ready, model=preset.get("model"),
         votes=preset.get("votes"), arbiter=(preset.get("arbiter") or {}).get("model"))

    # 1-2. control run
    t = time.perf_counter()
    before = dict(usage)
    rep = run_control(state, group, records=subset, mode=args.mode, profile=args.profile, out_dir=tmp / "out")
    mark("контрольный прогон (ingest + live оценка + экспорт)", T, step_s=round(time.perf_counter() - t, 1),
         status=rep["status"], evaluated=rep["evaluated"], failed=rep["failed_ids"], llm_calls=usage["net"] - before.get("net", 0),
         needs_review=rep["needs_review"], critical=rep["critical"])
    ctrl = json.loads(Path(rep["files"]["json"]).read_text(encoding="utf-8"))

    # 3. agreement with silver (report only)
    gold = {}
    for p in (repo / "eval/gold/silver.jsonl", MAIN / "eval/gold/silver.jsonl"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                g = json.loads(line)
                gold[(g["conv_id"], g["criterion_id"])] = g["answer"]
            break
    agree, per_crit = [], {}
    for c in ctrl["conversations"]:
        for row in c["criteria"]:
            g = gold.get((c["conversation_id"], row["criterion_id"]))
            if g is None or row["answer"] is None:
                continue
            ok = norm(g) == norm(row["answer"])
            agree.append(ok)
            per_crit.setdefault(row["criterion_id"], []).append(ok)
            if not ok:
                timeline.append({"step": "расхождение с silver", "sec": None, "conv": c["conversation_id"],
                                 "crit": row["criterion_id"], "gold": g, "model": row["answer"], "needs_review": row["needs_review"]})

    # 4. swap C2 (as the editor: full body, base = v1)
    t = time.perf_counter()
    v1 = state.checklists.get(1)
    body = v1.model_dump(mode="json")
    for c in body["criteria"]:
        c.pop("hash", None)
        if c["id"] == "C2":
            c.update(NEW_C2)
    body.update(change_note="Репетиция: C2 заменён на «не переспрашивает данные боту»")
    res = state.checklists.create(body, base_version=1)
    new_v = res.checklist.version
    mark("замена критерия сохранена", T, step_s=round(time.perf_counter() - t, 2), version=new_v,
         needs_reeval=res.diff.needs_reeval, warnings=res.warnings)

    # 5. apply (no live: reuse stored live answers of unchanged criteria)
    t = time.perf_counter()
    before = dict(usage)
    conv_ids = [c["internal_id"] for c in ctrl["conversations"]]
    key = state.eval_mode_key(args.mode, args.profile)
    st = state.runs.start_run(version=new_v, conv_ids=conv_ids, mode=key, profile=args.profile, live=False, wait=True)
    apply_calls = usage["net"] - before.get("net", 0)
    state.checklists.activate(new_v)
    mark("применение новой версии (переоценка + активация)", T, step_s=round(time.perf_counter() - t, 1), status=st["status"],
         pairs_to_evaluate=st.get("pairs_to_evaluate"), from_cache=st.get("from_cache"), llm_calls=apply_calls,
         errors=st.get("errors"))
    cl2 = state.checklists.get(new_v)
    from cqa import engine

    checks, c2_answers = [], Counter()
    for cid in conv_ids:
        evs = current_evals(state.db, cid, cl2, modes=[key])
        sc = engine.score(cl2, evs, state.db.list_decisions(conv_id=cid), conv_id=cid, strict=False)
        answers = {ln.criterion_id: ln.answer for ln in sc.lines}
        mine = recompute_total(cl2.model_dump(mode="json"), answers)
        old = next(c["total"] for c in ctrl["conversations"] if c["internal_id"] == cid)
        checks.append({"conv": cid, "old_total": old, "new_total": sc.total, "formula": mine,
                       "ok": mine == "missing" and sc.total is None or (mine is None and sc.total is None) or
                       (isinstance(mine, float) and sc.total is not None and abs(mine - sc.total) < 0.005),
                       "C2_old": next(r["answer"] for c in ctrl["conversations"] if c["internal_id"] == cid for r in c["criteria"] if r["criterion_id"] == "C2"),
                       "C2_new": answers.get("C2")})
        c2_answers[str(answers.get("C2"))] += 1
    total_s = round(time.perf_counter() - T, 1)

    out = HERE / f"reports/control_rehearsal_{args.tag}.md"
    n_ok = sum(agree)
    L = [f"# Репетиция контроля {args.tag}", "",
         f"Код: `{repo}` @ `{commit}`. Режим `{args.mode}`, профиль `{args.profile}`, модель `{preset.get('model')}`, голосов "
         f"{preset.get('votes')}, арбитр `{(preset.get('arbiter') or {}).get('model')}`. Набор: {len(subset)} DEV-диалогов из holdout "
         f"как «новые» (группа {group}, живой расчёт без кэша, временная БД).", "",
         f"**Итого от старта до применённой новой версии: {total_s} с ({total_s / 60:.1f} мин).** "
         f"LLM-вызовов всего {usage['net']}, токены {usage['tin']}/{usage['tout']}, ≈ ${usage['usd_x1e6'] / 1e6:.4f}. "
         f"Модели: {', '.join(k[6:] + '×' + str(v) for k, v in usage.items() if k.startswith('model:'))}.", "",
         "## Хронометраж", "", "| t, с | шаг | детали |", "|---|---|---|"]
    for s in timeline:
        if s["sec"] is None:
            continue
        det = {k: v for k, v in s.items() if k not in ("step", "sec")}
        L.append(f"| {s['sec']} | {s['step']} | {json.dumps(det, ensure_ascii=False, default=str)[:400].replace('|', '/')} |")
    L += ["", f"## Согласие с silver (только отчёт, holdout): {n_ok}/{len(agree)} = {100 * n_ok / max(1, len(agree)):.1f}%", "",
          "| критерий | согласие |", "|---|---|"]
    for c in sorted(per_crit):
        v = per_crit[c]
        L.append(f"| {c} | {sum(v)}/{len(v)} |")
    dis = [s for s in timeline if s["step"] == "расхождение с silver"]
    if dis:
        L += ["", "Расхождения: " + "; ".join(f"{d['conv']} {d['crit']}: silver {d['gold']} / модель {d['model']}{' (на проверку)' if d['needs_review'] else ''}" for d in dis)]
    L += ["", "## Замена критерия и применение", "",
          f"Новая версия {new_v}: переоценка {res.diff.needs_reeval}; план {st.get('pairs_to_evaluate')} пар, переиспользовано {st.get('from_cache')}, "
          f"LLM-вызовов {apply_calls}. Ответы нового C2: {dict(c2_answers)}.", "",
          "| диалог | итог v1 | итог новый | формула | ок | C2 был | C2 новый |", "|---|---|---|---|---|---|---|"]
    for c in checks:
        L.append(f"| {c['conv']} | {c['old_total']} | {c['new_total']} | {c['formula']} | {'✓' if c['ok'] else '✗'} | {c['C2_old']} | {c['C2_new']} |")
    L += ["", f"Экспорт контроля (json/csv/trace) создан во временной папке `{tmp / 'out'}` (не коммитится).", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("report ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
