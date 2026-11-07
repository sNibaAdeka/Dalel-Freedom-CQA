"""Agreement of evaluator predictions with a supplied reference.

    python scripts/eval_accuracy.py results/eval_runs/<run>.jsonl --gold silver --split tune --log "note"
    python scripts/eval_accuracy.py results/eval_runs/<run>.jsonl --gold debug        # pass_A batches 2,4,5 only
    python scripts/eval_accuracy.py run_lenient.jsonl run_strict.jsonl --gold silver  # C7 table for several runs

The default preserves reference labels and all criteria. Historical variants from eval/variants.json
are available only with --legacy-variants and are not current policy. Reports per criterion / language / channel,
confusion matrices, total-score MAE,
accuracy of auto-approved vs flagged cells, cost and time per dialogue, C7 under BOTH readings
(strict = gold as labelled; lenient = derived from strict by a rule, see lenient_c7) and a gold-free consistency check
of parallel RU/KZ/EN scenarios. Writes reports/accuracy_<date>_<run>.md; --log appends metrics (no texts) to
eval/experiments.jsonl. Silver agreement is not independently measured human accuracy.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cqa import evaluator as E  # noqa: E402
from cqa import rules  # noqa: E402

SIL = ROOT / "data/processed/silver"
DEBUG_FILES = [SIL / f"pass_A/batch_{b}.json" for b in (2, 4, 5)]
HELP_OFFER = re.compile(r"обращайтесь|напишите нам|остались (ли )?вопросы|чем (ещё|еще) (могу )?помочь|если (вопрос|что-то) останется|"
                        r"тағы сұрақ|жазыңыз|anything else|feel free to|reach out|let us know|other questions", re.I)


def find_checklist(path: str | None) -> dict:
    for p in [Path(path)] if path else [ROOT / "data/checklists/v1.json"]:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("checklist not found")


def norm(a):
    if a is None:
        return None
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, (int, float)):
        return float(a)
    s = str(a).strip().lower()
    if s in ("yes", "да", "true"):
        return "yes"
    if s in ("no", "нет", "false"):
        return "no"
    if s in ("na", "n/a", "н/п"):
        return "na"
    try:
        return float(s)
    except ValueError:
        return s


def load_gold(kind: str) -> dict[tuple[str, str], object]:
    gold = {}
    if kind == "debug":
        for p in DEBUG_FILES:
            for d in json.loads(p.read_text(encoding="utf-8"))["dialogues"]:
                for cid, a in d["answers"].items():
                    gold[(d["conversation_id"], cid)] = norm(a.get("answer"))
        return gold
    if kind == "silver":
        path = ROOT / "eval/gold/silver.jsonl"
    else:
        path = Path(kind.removeprefix("file:"))
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            g = json.loads(line)
            key = (g["conv_id"], g["criterion_id"])
            if key in gold:
                raise ValueError(f'{path.name}:{number}: duplicate reference cell {key}; adjudicate annotations before model evaluation')
            answer = norm(g["answer"])
            if answer is None or (isinstance(answer, float) and not math.isfinite(answer)):
                raise ValueError(f'{path.name}:{number}: missing or non-finite reference answer')
            gold[key] = answer
    return gold


def lenient_c7(conv: dict, strict) -> object:
    """Lenient-reading C7 derived from the strict label: strict 'no' -> 'yes' when the operator's last reply (before the
    client ended a call) is a farewell or a help offer and the operator did not close the chat silently. Approximation:
    a farewell while the client's question is still open stays 'yes' here (cannot be detected by a rule)."""
    if strict != "no":
        return strict
    msgs = conv["messages"]
    end = next((m["idx"] for m in msgs if m["speaker"] == "system" and rules.CLIENT_ENDED.search(m["text"])), None)
    ops = [m for m in msgs if m["speaker"] == "operator" and (end is None or m["idx"] < end)]
    if not ops:
        return strict
    last = ops[-1]
    after = [m for m in msgs if m["idx"] > last["idx"]]
    if any(m["speaker"] == "system" and rules.OP_CLOSED.search(m["text"]) for m in after):
        return "no"
    return "yes" if (rules.FAREWELL.search(last["text"]) or HELP_OFFER.search(last["text"])) else "no"


def total(answers: dict, crits: list[dict], exclude: set[str]):
    num = den = Fraction(0)
    for c in crits:
        w = Fraction(str(c.get("weight") or 0))
        a = answers.get(c["id"])
        if w == 0 or c["id"] in exclude or a == "na":
            continue
        if a is None:
            return None
        den += w
        if c.get("answer_type") == "scale":
            num += w * Fraction(str(a)) / max(Fraction(str(x)) for x in c["scale"])
        else:
            num += w if a == "yes" else Fraction(0)
    if den == 0:
        return None
    result = 100 * num / den
    return float((Decimal(result.numerator) / Decimal(result.denominator)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))


def reference_variants(legacy=False):
    variants = json.loads((ROOT / 'eval/variants.json').read_text())['variants']
    if not legacy:
        variants['b'] = {'label':'все критерии, без изменения исходных меток', 'overrides':{}, 'exclude':[]}
    return variants


def load_run(path: Path) -> tuple[dict, list[dict]]:
    meta, rows = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            if "_meta" in d:
                meta = d["_meta"]
            else:
                rows.append(d)
    return meta, rows


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "—"


def complete_mean(rows, field):
    values = [r.get(field) for r in rows]
    if not values or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return None
    return sum(values) / len(values)


def display_measurement(value, digits=2):
    return f'{value:.{digits}f}' if value is not None else 'не измерено'


def measure(rows: list[dict], gold_of, crits: list[dict], exclude: set[str]) -> dict:
    """Cell-level comparison. gold_of(conv_id, criterion_id) -> gold answer or None."""
    cids = [c["id"] for c in crits]
    per, lang, chan = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    conf, scale_abs = defaultdict(Counter), defaultdict(list)
    auto, flagged = [0, 0], [0, 0]
    errors, maes, missing = [], [], []
    for r in rows:
        pred = {e["criterion_id"]: norm(e["answer"]) for e in r["evals"]}
        ev_by = {e["criterion_id"]: e for e in r["evals"]}
        g_ans = {}
        for cid in cids:
            if cid in exclude:
                continue
            g = gold_of(r["conv_id"], cid)
            if g is None:
                continue
            if pred.get(cid) is None:  # model failed (rate limit / network): not an answer, reported separately (a2)
                missing.append((r['conv_id'], cid))
                continue
            g_ans[cid] = g
            p = pred.get(cid)
            ok = p == g
            for bucket in (per[cid], lang[r["language"]], chan[r["channel"]], flagged if p is None or ev_by[cid].get("needs_review") else auto):
                bucket[0] += ok
                bucket[1] += 1
            conf[cid][(str(g), str(p))] += 1
            if isinstance(g, float) and isinstance(p, float):
                scale_abs[cid].append(abs(g - p))
            if not ok:
                e = ev_by.get(cid, {})
                errors.append((r["conv_id"], cid, g, p, e.get("confidence"), e.get("votes"), e.get("arbiter"), (e.get("explanation") or "")[:220]))
        if all(c in g_ans for c in cids if c not in exclude):
            tp, tg = total(pred, crits, exclude), total(g_ans, crits, exclude)
            if tp is not None and tg is not None:
                maes.append(abs(tp - tg))
    n = sum(v[1] for v in per.values())
    ok = sum(v[0] for v in per.values())
    return {"per": per, "lang": lang, "chan": chan, "conf": conf, "scale_abs": scale_abs, "auto": auto, "flagged": flagged,
            "errors": errors, "missing": missing, "maes": maes, "n": n, "ok": ok, "acc": ok / n if n else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--gold", default="silver", help="silver | debug | file:<path.jsonl>")
    ap.add_argument('--legacy-variants', action='store_true', help='Historical sensitivity analysis only: modifies reference labels in memory; not accepted policy')
    ap.add_argument("--split", choices=["tune", "holdout", "debug", "all"], default="all")
    ap.add_argument("--checklist")
    ap.add_argument("--data", default=str(ROOT / "data/raw/dev_records.json"))
    ap.add_argument("--log", default=None, help="append metrics to eval/experiments.jsonl with this note")
    ap.add_argument("--out")
    args = ap.parse_args()

    ck = find_checklist(args.checklist)
    crits = [E.as_criterion(c) for c in ck["criteria"]]
    cids = [c["id"] for c in crits]
    recs = {r["conversation_id"]: r for r in json.loads(Path(args.data).read_text(encoding="utf-8"))}
    convs = {k: E.as_conversation(v) for k, v in recs.items()}
    gold = load_gold(args.gold)
    split = json.loads((ROOT / "eval/split.json").read_text())
    allowed = set(recs) if args.split == "all" else set(split[args.split])
    variants = reference_variants(args.legacy_variants)

    meta, rows = load_run(Path(args.runs[0]))
    rows = [r for r in rows if r["conv_id"] in allowed and any((r["conv_id"], c) in gold for c in cids)]

    def gold_fn(variant: dict):
        ov = variant.get("overrides") or {}

        def f(conv_id, cid):
            if conv_id in (ov.get(cid) or {}):
                return norm(ov[cid][conv_id])
            return gold.get((conv_id, cid))
        return f

    res = {k: measure(rows, gold_fn(v), crits, set(v.get("exclude") or [])) for k, v in variants.items()}
    a, b = res["a"], res["b"]

    # C7 under both readings, for every run given
    c7_lines, c7_metrics = [], {}
    for rp in args.runs:
        m2, rows2 = load_run(Path(rp))
        rd = (m2.get("readings") or {}).get("C7", "strict")
        cell = {}
        for gr in (("strict", "lenient") if args.legacy_variants else ("strict",)):
            ok = n = 0
            for r in rows2:
                if r["conv_id"] not in allowed:
                    continue
                p = next((norm(e["answer"]) for e in r["evals"] if e["criterion_id"] == "C7"), None)
                g = gold.get((r["conv_id"], "C7"))
                if g is None:
                    continue
                g = lenient_c7(convs[r["conv_id"]], g) if gr == "lenient" else g
                ok += p == g
                n += 1
            cell[gr] = (ok, n)
        c7_metrics[Path(rp).stem] = {"reading": rd, **{k: round(v[0] / v[1], 4) if v[1] else None for k, v in cell.items()}}
        c7_lines.append(f"| {Path(rp).stem} | {rd} | {pct(*cell['strict'])} ({cell['strict'][1]}) |" +
                        (f" {pct(*cell['lenient'])} ({cell['lenient'][1]}) |" if args.legacy_variants else ''))

    cost = complete_mean(rows, 'cost_kzt_full')
    wall = complete_mean(rows, 'wall_s')
    calls = complete_mean(rows, 'calls')
    name = meta.get("name") or Path(args.runs[0]).stem
    pre = meta.get("preset") or {}

    def mae_txt(x):
        return f"{sum(x['maes']) / len(x['maes']):.2f} ({len(x['maes'])} дл.)" if x["maes"] else "—"

    md = [f"# Сопоставление оценщика с разметкой: {name}", "",
          'Совпадение с предоставленной разметкой не доказывает её правильность. Silver/debug — машинная разметка, не независимый человеческий эталон.', '',
          ('Включён исторический анализ с изменением меток и исключением C7; он не соответствует принятой политике g6.' if args.legacy_variants else 'Основной расчёт сохраняет все исходные метки; C7 не исключается, C8 не переопределяется.'), '',
          f"Эталон: **{args.gold}** · часть: **{args.split}** · диалогов: {len(rows)} · режим: {meta.get('mode')} · "
          f"модель: {pre.get('model')} ×{pre.get('votes', 1)} · арбитр: {(pre.get('arbiter') or {}).get('model')} · шаблон: {meta.get('template')} · "
          f"guidance: {meta.get('guidance_version')} · трактовки в промпте: {meta.get('readings')}", "",
          "| вариант эталона | совпадение по критериям | ячеек | MAE итога, баллы |", "|---|---|---|---|",
          f"| (а) {variants['a']['label']} | **{pct(a['ok'], a['n'])}** | {a['n']} | {mae_txt(a)} |",
          f"| (б) {variants['b']['label']} | **{pct(b['ok'], b['n'])}** | {b['n']} | {mae_txt(b)} |", "",
          f"Не оценено (ошибка модели или лимит): {len(a['missing'])} ячеек — не входят в точность и MAE не подменяют. Диалоги, отсутствующие в файле прогона, не оценивались.", "",
          f"Вариант (б): авто-одобренные ячейки {pct(*b['auto'])} из {b['auto'][1]}, отправленные на проверку {pct(*b['flagged'])} из {b['flagged'][1]} "
          f"(доля проверки {pct(b['flagged'][1], b['n'])}).", "",
          f"Стоимость по данным файла на диалог: {display_measurement(cost)} ₸ · время на диалог: {display_measurement(wall, 1)} с "
          f"· вызовов LLM на диалог: {display_measurement(calls, 1)}. Неполные метаданные не заменяются нулём.", "",
          "> Для заявления о точности нужен независимый, согласованный человеческий эталон. Этот отчёт сам не подтверждает происхождение разметки.", "",
          "## По критериям", "| критерий | (а) как есть | (б) трактовки команды | n | MAE ступени (а) |", "|---|---|---|---|---|"]
    for cid in cids:
        mae = f"{sum(a['scale_abs'][cid]) / len(a['scale_abs'][cid]):.2f}" if a["scale_abs"][cid] else ""
        bcell = pct(*b["per"][cid]) if b["per"][cid][1] else ("исключён" if cid in set(variants['b'].get('exclude') or []) else "нет сравнимых меток")
        md.append(f"| {cid} | {pct(*a['per'][cid])} | {bcell} | {a['per'][cid][1]} | {mae} |")
    md += ["", "## C7" + (" — исторический анализ двух прочтений" if args.legacy_variants else " — исходные метки"),
           "| прогон | трактовка C7 в промпте | исходный эталон |" + (" мягкий эталон (выведен правилом) |" if args.legacy_variants else ''),
           "|---|---|---|" + ('---|' if args.legacy_variants else ''), *c7_lines]
    md += ["", "## По языкам, вариант (б)", "| язык | совпадение | n |", "|---|---|---|"] + [f"| {k} | {pct(*v)} | {v[1]} |" for k, v in sorted(b["lang"].items())]
    md += ["", "## По каналам, вариант (б)", "| канал | совпадение | n |", "|---|---|---|"] + [f"| {k} | {pct(*v)} | {v[1]} |" for k, v in sorted(b["chan"].items())]
    md += ["", "## Матрицы ошибок, вариант (а) (эталон → прогноз)"]
    for cid in cids:
        cells = a["conf"][cid]
        if not cells:
            continue
        labels = sorted({x for x, _ in cells} | {y for _, y in cells})
        md += [f"**{cid}**", "", "| эталон \\ прогноз | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]
        md += [f"| {g} | " + " | ".join(str(cells.get((g, p), "")) for p in labels) + " |" for g in labels] + [""]

    groups_path = ROOT / "eval/scenario_groups.json"
    cons = None
    if groups_path.exists():
        preds = {r["conv_id"]: {e["criterion_id"]: norm(e["answer"]) for e in r["evals"]} for r in load_run(Path(args.runs[0]))[1]}
        cons_ok = cons_n = 0
        diffs = []
        for gname, ids in json.loads(groups_path.read_text())["groups"].items():
            have = [i for i in ids if i in preds]
            if len(have) < 2:
                continue
            for cid in cids:
                vals = [preds[i].get(cid) for i in have]
                if any(value is None for value in vals):
                    continue
                cons_n += 1
                if len(set(map(str, vals))) == 1:
                    cons_ok += 1
                else:
                    diffs.append(f"| {gname} | {cid} | " + ", ".join(f"{i}={preds[i].get(cid)}" for i in have) + " |")
        cons = cons_ok / cons_n if cons_n else None
        md += [f"## Согласованность параллельных сценариев RU/KZ/EN (без эталона): {pct(cons_ok, cons_n)} из {cons_n}",
               "| сценарий | критерий | ответы |", "|---|---|---|", *diffs, ""]
    md += ["## Ошибки, вариант (б)", "| диалог | критерий | эталон | прогноз | уверенность | голоса | арбитр | объяснение модели |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {c1} | {c2} | {c3} | {c4} | {c5} | {c6} | {c7} | {c8.replace('|', '/')} |" for c1, c2, c3, c4, c5, c6, c7, c8 in sorted(b["errors"])]

    out = Path(args.out) if args.out else ROOT / f"reports/accuracy_{time.strftime('%Y%m%d')}_{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[2:8]))
    print("per criterion (a):", {cid: pct(*a["per"][cid]) for cid in cids})
    print("per criterion (b):", {cid: pct(*b["per"][cid]) for cid in cids if cid not in set(variants["b"].get("exclude") or [])})
    print("C7:", *c7_lines, sep="\n")
    if cons is not None:
        print(f"scenario consistency: {100 * cons:.1f}%")
    print("report ->", out)
    if args.log is not None:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "run": name, "note": args.log or meta.get("note", ""), "gold": args.gold, "split": args.split,
               "mode": meta.get("mode"), "model": pre.get("model"), "votes": pre.get("votes"), "arbiter": (pre.get("arbiter") or {}).get("model"),
               "template": meta.get("template"), "guidance": meta.get("guidance_version"), "readings": meta.get("readings"), "n_dialogues": len(rows),
               "acc_a": round(a["acc"], 4) if a['acc'] is not None else None, "acc_b": round(b["acc"], 4) if b['acc'] is not None else None, "cells_a": a["n"], "cells_b": b["n"],
               "per_criterion_a": {cid: round(a["per"][cid][0] / a["per"][cid][1], 4) if a["per"][cid][1] else None for cid in cids},
               "c7": c7_metrics, "total_mae_a": round(sum(a["maes"]) / len(a["maes"]), 3) if a["maes"] else None,
               "total_mae_b": round(sum(b["maes"]) / len(b["maes"]), 3) if b["maes"] else None,
               "review_share_b": round(b["flagged"][1] / b["n"], 3) if b["n"] else None, "consistency": round(cons, 4) if cons is not None else None,
               "cost_kzt_per_dialogue": round(cost, 3) if cost is not None else None,
               "wall_s_per_dialogue": round(wall, 2) if wall is not None else None}
        with (ROOT / "eval/experiments.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("logged -> eval/experiments.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
