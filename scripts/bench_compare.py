"""Model / mode benchmark table over several eval runs (results/eval_runs/*.jsonl from scripts/eval_run.py).

    python scripts/bench_compare.py haiku-fast-a haiku-fast-b sonnet-fast --common --pairs haiku-fast-a:haiku-fast-b
    python scripts/bench_compare.py run1 run2 --split holdout --md experiments/bench_table.md --json experiments/bench.json

Metrics per run (cells = dialogue x criterion that the gold has):
  acc_a   - gold as labelled (all 8 criteria, C7 strict as labelled)
  acc_b   - team readings (C8 own-account disclosure = no, C7 excluded)            [eval/variants.json]
  acc_bl  - acc_b plus C7 against the lenient gold (team's active C7 reading)        [eval_accuracy.lenient_c7]
  per-criterion accuracy (b, C7 lenient), total-score MAE (b), share of cells flagged needs_review,
  failed cells (model returned nothing), KZT and wall seconds per dialogue, LLM calls per dialogue.
Stability (--pairs A:B): share of cells where two identical runs give the same answer (on common dialogues).
Reuses the gold/metric code of scripts/eval_accuracy.py (owner a2) without changing it. Writes no dialogue text.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_accuracy as EA  # noqa: E402
from cqa import evaluator as E  # noqa: E402

RUNS = ROOT / "results/eval_runs"
USD_KZT = 500.0
# OpenRouter list prices, USD per 1M tokens (GET /models, 7 Nov 19:20). The evaluator's own price table does not know these ids,
# so cost of openai:* runs is recomputed here from tokens. Arbiter calls are priced from the run's average tokens per call.
OR_PRICES = {"deepseek/deepseek-v4-pro": (0.688, 1.375), "deepseek/deepseek-v4-flash": (0.091, 0.182),
             "qwen/qwen3.8-max-prime": (4.0, 12.0), "qwen/qwen3.8-27b": (0.15, 1.875), "deepseek/deepseek-chat": (0.32, 0.89)}


def or_price(model: str | None):
    if not model:
        return None
    return OR_PRICES.get(model.split(":", 1)[1] if model.startswith("openai:") else model)


def usd_per_dialogue(meta: dict, rows: list[dict]) -> float | None:
    """USD per dialogue for OpenRouter runs (None for other providers: their cost is in cost_kzt_full)."""
    pre = meta.get("preset") or {}
    main = or_price(pre.get("model"))
    if not main or not rows:
        return None
    arb_model = (pre.get("arbiter") or {}).get("model") if pre.get("arbiter") else None
    arb = or_price(arb_model) or main
    tot = 0.0
    for r in rows:
        tin = sum(e.get("tokens_in") or 0 for e in r["evals"])
        tout = sum(e.get("tokens_out") or 0 for e in r["evals"])
        calls = max(1.0, float(r.get("calls") or 1))
        n_arb = sum(1 for e in r["evals"] if e.get("arbiter") is not None)
        ain, aout = tin / calls * n_arb, tout / calls * n_arb
        tot += ((tin - ain) * main[0] + (tout - aout) * main[1] + ain * arb[0] + aout * arb[1]) / 1e6
    return tot / len(rows)


def run_path(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    return RUNS / (name if name.endswith(".jsonl") else name + ".jsonl")


def describe(meta: dict) -> str:
    pre = meta.get("preset") or {}
    arb = (pre.get("arbiter") or {}).get("model") if pre.get("arbiter") else None
    model = ",".join(pre.get("models") or [pre.get("model", "?")])
    return f"{meta.get('mode')} · {model} · голоса {pre.get('votes', 1)}" + (f" · арбитр {arb}" if arb else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--split", choices=["tune", "holdout", "debug", "all"], default="all")
    ap.add_argument("--common", action="store_true", help="only dialogues present (and answered) in every run")
    ap.add_argument("--pairs", default="", help="stability pairs A:B,C:D (run names)")
    ap.add_argument("--md")
    ap.add_argument("--json")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    ck = EA.find_checklist(None)
    crits = [E.as_criterion(c) for c in ck["criteria"]]
    cids = [c["id"] for c in crits]
    recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data/raw/dev_records.json").read_text(encoding="utf-8"))}
    convs = {k: E.as_conversation(v) for k, v in recs.items()}
    gold = EA.load_gold("silver")
    split = json.loads((ROOT / "eval/split.json").read_text())
    allowed = set(recs) if args.split == "all" else set(split[args.split])
    variants = json.loads((ROOT / "eval/variants.json").read_text())["variants"]
    va, vb = variants["a"], variants["b"]

    def gold_fn(variant: dict, c7_lenient: bool = False):
        ov = variant.get("overrides") or {}

        def f(conv_id, cid):
            if conv_id in (ov.get(cid) or {}):
                return EA.norm(ov[cid][conv_id])
            g = gold.get((conv_id, cid))
            if c7_lenient and cid == "C7" and g is not None:
                return EA.lenient_c7(convs[conv_id], g)
            return g
        return f

    loaded = {}
    for name in args.runs:
        meta, rows = EA.load_run(run_path(name))
        rows = [r for r in rows if r["conv_id"] in allowed and any((r["conv_id"], c) in gold for c in cids)]
        # drop dialogues where the model failed completely (limit / network): reported as failed, not as wrong
        loaded[name] = (meta, rows)

    def answered(r):
        return any(e.get("answer") is not None for e in r["evals"])

    if args.common:
        sets = [{r["conv_id"] for r in rows if answered(r)} for _, rows in loaded.values()]
        common = set.intersection(*sets) if sets else set()
        loaded = {k: (m, [r for r in rows if r["conv_id"] in common]) for k, (m, rows) in loaded.items()}

    out = {}
    for name, (meta, rows) in loaded.items():
        ra = EA.measure(rows, gold_fn(va), crits, set(va.get("exclude") or []))
        rb = EA.measure(rows, gold_fn(vb), crits, set(vb.get("exclude") or []))
        rbl = EA.measure(rows, gold_fn(vb, c7_lenient=True), crits, set())
        n_cells_pred = sum(len(r["evals"]) for r in rows)
        flagged = sum(1 for r in rows for e in r["evals"] if e.get("needs_review"))
        failed = sum(1 for r in rows for e in r["evals"] if e.get("answer") is None)
        ok_rows = [r for r in rows if answered(r)]
        walls = [r["wall_s"] for r in ok_rows]
        out[name] = {
            "desc": describe(meta), "n_dialogues": len(rows), "guidance": f"{meta.get('guidance_version', '?')} {','.join(f'{k}={v}' for k, v in (meta.get('readings') or {}).items() if k in ('C7', 'C8'))}",
            "acc_a": round(ra["acc"], 4), "cells_a": ra["n"], "acc_b": round(rb["acc"], 4), "cells_b": rb["n"],
            "acc_bl": round(rbl["acc"], 4), "cells_bl": rbl["n"],
            "per_criterion_bl": {cid: (round(rbl["per"][cid][0] / rbl["per"][cid][1], 4) if rbl["per"][cid][1] else None) for cid in cids},
            "c7_strict": (round(ra["per"]["C7"][0] / ra["per"]["C7"][1], 4) if ra["per"]["C7"][1] else None),
            "mae_a": round(statistics.mean(ra["maes"]), 2) if ra["maes"] else None,
            "per_criterion_a": {cid: (round(ra["per"][cid][0] / ra["per"][cid][1], 4) if ra["per"][cid][1] else None) for cid in cids},
            "acc_auto_a": round(ra["auto"][0] / ra["auto"][1], 4) if ra["auto"][1] else None,
            "mae_b": round(statistics.mean(rb["maes"]), 2) if rb["maes"] else None, "mae_bl": round(statistics.mean(rbl["maes"]), 2) if rbl["maes"] else None,
            "review_share": round(flagged / n_cells_pred, 4) if n_cells_pred else None,
            "acc_auto_bl": round(rbl["auto"][0] / rbl["auto"][1], 4) if rbl["auto"][1] else None,
            "failed_cells": failed,
            "usd_per_dialogue": (round(usd_per_dialogue(meta, ok_rows), 5) if usd_per_dialogue(meta, ok_rows) is not None else None),
            "kzt_per_dialogue": (round(usd_per_dialogue(meta, ok_rows) * USD_KZT, 2) if usd_per_dialogue(meta, ok_rows) is not None
                                 else round(statistics.mean(r["cost_kzt_full"] for r in ok_rows), 2)) if ok_rows else None,
            "wall_s_per_dialogue": round(statistics.mean(walls), 1) if walls else None,
            "wall_s_max": round(max(walls), 1) if walls else None,
            "calls_per_dialogue": round(statistics.mean(r.get("calls", 0) for r in ok_rows), 1) if ok_rows else None,
            # LLM busy time per dialogue (sum of call latencies) -> projected wall time of a 10-dialogue control at K parallel calls
            "call_s_per_dialogue": round(statistics.mean(sum(e.get("latency_ms") or 0 for e in r["evals"]) / 1000 for r in ok_rows), 1) if ok_rows else None,
            "tokens_in_per_dialogue": round(statistics.mean(sum(e.get("tokens_in") or 0 for e in r["evals"]) for r in ok_rows)) if ok_rows else None,
            "tokens_out_per_dialogue": round(statistics.mean(sum(e.get("tokens_out") or 0 for e in r["evals"]) for r in ok_rows)) if ok_rows else None,
            "errors_bl": [(c, cid, str(g), str(p)) for c, cid, g, p, *_ in rbl["errors"]],
        }

    stab = {}
    for pair in [p for p in args.pairs.split(",") if p.strip()]:
        x, y = pair.split(":")
        rx = {r["conv_id"]: {e["criterion_id"]: EA.norm(e["answer"]) for e in r["evals"]} for r in EA.load_run(run_path(x))[1]}
        ry = {r["conv_id"]: {e["criterion_id"]: EA.norm(e["answer"]) for e in r["evals"]} for r in EA.load_run(run_path(y))[1]}
        same = tot = 0
        for cv in set(rx) & set(ry) & allowed:
            for cid in cids:
                a, b = rx[cv].get(cid), ry[cv].get(cid)
                if a is None or b is None:
                    continue
                tot += 1
                same += a == b
        stab[pair] = {"agree": round(same / tot, 4) if tot else None, "cells": tot}

    md = [f"### {args.title or 'Сравнение прогонов'} (split={args.split}{', общие диалоги' if args.common else ''})", "",
          "| прогон | конфигурация | guidance | диал. | (а) эталон как есть | MAE (а) | (б) старые трактовки | на проверку | точн. авто (а) | сбои | ₸/диал | с/диал | вызовов |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, m in out.items():
        f = lambda v: "—" if v is None else f"{100 * v:.1f}%"  # noqa: E731
        md.append(f"| {name} | {m['desc']} | {m['guidance']} | {m['n_dialogues']} | **{f(m['acc_a'])}** | {m['mae_a'] if m['mae_a'] is not None else '—'} | {f(m['acc_b'])} | "
                  f"{f(m['review_share'])} | {f(m['acc_auto_a'])} | {m['failed_cells']} | "
                  f"{m['kzt_per_dialogue']} | {m['wall_s_per_dialogue']} | {m['calls_per_dialogue']} |")
    md += ["", "Бюджет контроля (10 диалогов): занятость LLM = сумма латентностей вызовов; проекция = 10 x занятость / K параллельных вызовов",
           "(нижняя граница; хвост самого длинного диалога добавляется сверху).", "",
           "| прогон | вх. токены/диал | вых. токены/диал | занятость, с/диал | макс. с/диал | 10 диал. при K=4, мин | K=8, мин | K=12, мин |", "|---|---|---|---|---|---|---|---|"]
    for name, m in out.items():
        cs = m["call_s_per_dialogue"] or 0
        md.append(f"| {name} | {m['tokens_in_per_dialogue']} | {m['tokens_out_per_dialogue']} | {cs} | {m['wall_s_max']} | "
                  + " | ".join(f"{10 * cs / k / 60:.1f}" for k in (4, 8, 12)) + " |")
    md += ["", "По критериям, (а) эталон как есть:", "", "| прогон | " + " | ".join(cids) + " |", "|---|" + "---|" * len(cids)]
    for name, m in out.items():
        md.append(f"| {name} | " + " | ".join("—" if m["per_criterion_a"][c] is None else f"{100 * m['per_criterion_a'][c]:.0f}" for c in cids) + " |")
    if stab:
        md += ["", "Устойчивость (одинаковые ответы в двух одинаковых прогонах):", ""]
        md += [f"- {k}: **{100 * v['agree']:.1f}%** из {v['cells']} ячеек" if v["agree"] is not None else f"- {k}: нет общих ячеек" for k, v in stab.items()]
    text = "\n".join(md)
    print(text)
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"split": args.split, "common": args.common, "runs": out, "stability": stab}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
