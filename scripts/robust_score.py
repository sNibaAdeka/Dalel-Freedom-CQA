"""Score the evaluator on the synthetic edge cases (eval/synthetic) and write reports/robustness.md.

    # 1) run the judge on the synthetic set (any eval_run.py options), then score:
    python scripts/robust_score.py --run --name syn-haiku-fast --model claude_cli:claude-haiku-5-5 --mode fast
    # 2) score an existing run (results/eval_runs/<name>.jsonl), optionally several runs side by side:
    python scripts/robust_score.py --name syn-haiku-fast [--confirm syn-sonnet-confirm] [--out reports/robustness.md]

Expected answers come from eval/synthetic/expected.jsonl (written by hand, by construction; see eval/synthetic/build.py).
"target" cells are the edge the dialogue was built for; "secondary" cells are sanity checks of the other criteria.
A cell is: exact (== expected), soft (in accept list: an adjacent defensible step), miss (otherwise), missing (no answer).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "eval/synthetic"
RUNS = ROOT / "results/eval_runs"
CRITERIA = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"]


def norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    if s in ("n/a", "na", "null"):
        return "na"
    try:
        return float(s)
    except ValueError:
        return s


def fmt(v) -> str:
    v = norm(v)
    if v is None:
        return "—"
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    return {"yes": "Да", "no": "Нет", "na": "N/A"}.get(v, str(v))


# USD per 1M tokens (in, out), OpenRouter list prices on 2026-11-07
PRICES = {"deepseek-v4-flash": (0.091, 0.182), "deepseek-v4-pro": (0.687648, 1.375296), "deepseek-chat": (0.28, 0.42)}


def run_usd(run: dict, model: str) -> tuple[float, int, int]:
    tin = sum(int(e.get("tokens_in") or 0) for v in run.values() for k, e in v.items() if k != "_line")
    tout = sum(int(e.get("tokens_out") or 0) for v in run.values() for k, e in v.items() if k != "_line")
    p = next((v for k, v in PRICES.items() if k in model), None)
    if p is None:  # claude_cli etc.: the provider reports its own cost (cost_kzt_full, 500 ₸/$)
        return sum(float(v["_line"].get("cost_kzt_full") or 0) for v in run.values()) / 500.0, tin, tout
    return (tin * p[0] + tout * p[1]) / 1e6, tin, tout


def load_expected(path: Path = SYN / "expected.jsonl") -> dict[str, dict]:
    return {e["conversation_id"]: e for e in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if e}


def load_run(name_or_path: str) -> tuple[dict, dict[str, dict]]:
    p = Path(name_or_path)
    if not p.exists():
        p = RUNS / f"{name_or_path}.jsonl"
    meta, out = {}, {}
    for line in p.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if "_meta" in d:
            meta = d["_meta"]
            continue
        out[d["conv_id"]] = {e["criterion_id"]: e for e in d["evals"]} | {"_line": d}
    return meta, out


def grade(expected_v, accept: list, got_v) -> str:
    if got_v is None:
        return "missing"
    e, g = norm(expected_v), norm(got_v)
    if g == e:
        return "exact"
    if g in {norm(a) for a in accept or []}:
        return "soft"
    return "miss"


def score(expected: dict[str, dict], run: dict[str, dict]) -> list[dict]:
    """One row per asserted cell: conv, criterion, kind (target/secondary), expected, got, grade, explanation."""
    rows = []
    for cid, e in expected.items():
        evs = run.get(cid)
        if evs is None:  # dialogue not in this run (subset run, e.g. confirmation)
            continue
        for c in CRITERIA:
            want = e["expected"].get(c)
            if want is None:
                continue
            ev = (evs or {}).get(c) or {}
            got = ev.get("answer") if evs is not None else None
            g = grade(want, (e.get("accept") or {}).get(c, []), got) if evs is not None else "missing"
            rows.append({"conv": cid, "lang": e["language"], "channel": e["channel"], "criterion": c,
                         "kind": "target" if c in e["target"] else "secondary", "expected": want, "got": got, "grade": g,
                         "needs_review": bool(ev.get("needs_review")), "votes": ev.get("votes"),
                         "explanation": (ev.get("explanation") or "")[:300], "analysis": ((ev.get("analysis") or [""])[0] or "")[:400],
                         "rule": e["rule"], "why": e["why"], "reading": (e.get("reading") or {}).get(c)})
    return rows


def acc(rows: list[dict], strict: bool = False) -> tuple[int, int]:
    ok = sum(r["grade"] == "exact" or (not strict and r["grade"] == "soft") for r in rows if r["grade"] != "missing")
    n = sum(r["grade"] != "missing" for r in rows)
    return ok, n


def pct(ok: int, n: int) -> str:
    return f"{ok}/{n} ({100 * ok / n:.0f}%)" if n else "—"


def table_by(rows: list[dict], key: str, values: list[str]) -> list[str]:
    out = [f"| {key} | цель (с допуском) | цель строго | вторичные | все |", "|---|---|---|---|---|"]
    for v in values:
        rs = [r for r in rows if r[key] == v]
        t = [r for r in rs if r["kind"] == "target"]
        s = [r for r in rs if r["kind"] == "secondary"]
        out.append(f"| {v} | {pct(*acc(t))} | {pct(*acc(t, True))} | {pct(*acc(s))} | {pct(*acc(rs))} |")
    return out


def report(expected: dict, runs: list[tuple[str, dict, dict]], out: Path) -> str:
    lines = ["# Устойчивость судьи на синтетических трудных случаях", "",
             f"Набор: `eval/synthetic/` — {len(expected)} коротких диалогов, написанных вручную на грани правил чек-листа "
             f"(RU {sum(e['language'] == 'RU' for e in expected.values())}, KZ {sum(e['language'] == 'KZ' for e in expected.values())}, "
             f"EN {sum(e['language'] == 'EN' for e in expected.values())}; из них 2 звонка). Ожидаемые ответы заданы конструкцией "
             "(`expected.jsonl`, обоснование по PDF у каждого случая), моделью не генерировались.",
             "«Цель» — критерий, ради которого построен диалог; «вторичные» — проверка остальных критериев там, где ответ однозначен. "
             "«С допуском» — засчитана соседняя защитимая ступень из `accept`.", ""]
    for name, meta, run in runs:
        rows = score(expected, run)
        missing = sorted({f'{r["conv"]}:{r["criterion"]}' for r in rows if r["grade"] == "missing"})
        lines += [f"## Прогон `{name}`", "",
                  f"Модель: `{(meta.get('preset') or {}).get('model')}`, режим `{meta.get('mode')}`, голосов {(meta.get('preset') or {}).get('votes')}, "
                  f"guidance `{meta.get('guidance_version')}`, шаблон `{meta.get('template')}`, прочтения {meta.get('readings')}. "
                  f"Диалогов с ответом: {len(run)}/{len(expected)}." + (f" Ячейки без ответа (ошибка провайдера): {', '.join(missing)}." if missing else ""), ""]
        t = [r for r in rows if r["kind"] == "target"]
        lines += [f"**Целевые ячейки: {pct(*acc(t))} с допуском, {pct(*acc(t, True))} строго. Все ячейки: {pct(*acc(rows))}.**", ""]
        lines += table_by(rows, "criterion", CRITERIA) + [""]
        lines += table_by(rows, "lang", ["RU", "KZ", "EN"]) + [""]
        wall = [v["_line"].get("wall_s", 0) for v in run.values()]
        if wall:
            usd, tin, tout = run_usd(run, (meta.get("preset") or {}).get("model") or "")
            lines += [f"Токены: вход {tin}, выход {tout}; стоимость ≈ ${usd:.4f} (OpenRouter — по прайсу; claude_cli — эквивалент подписки); "
                      f"медиана времени на диалог {sorted(wall)[len(wall) // 2]:.1f} с.", ""]
        miss = [r for r in rows if r["grade"] == "miss"]
        if miss:
            lines += ["### Провалы (ожидание ≠ ответ модели)", "",
                      "| диалог | крит. | тип | ждали | модель | правило | что сказала модель |", "|---|---|---|---|---|---|---|"]
            for r in sorted(miss, key=lambda r: (r["kind"] != "target", r["criterion"], r["conv"])):
                expl = (r["explanation"] or r["analysis"]).replace("|", "/").replace("\n", " ")[:220]
                lines.append(f"| {r['conv']} | {r['criterion']} | {r['kind']} | {fmt(r['expected'])} | {fmt(r['got'])}"
                             f"{' ⚑' if r['needs_review'] else ''} | {r['rule'].replace('|', '/')[:140]} | {expl} |")
            lines.append("")
    if len(runs) > 1:
        base = score(expected, runs[0][2])
        lines += ["## Сверка прогонов по провалам первого", "", "| диалог | крит. | ждали | " + " | ".join(n for n, _, _ in runs) + " |",
                  "|---|---|---|" + "---|" * len(runs)]
        others = [{(r["conv"], r["criterion"]): r for r in score(expected, run)} for _, _, run in runs]
        for r in base:
            if r["grade"] != "miss":
                continue
            k = (r["conv"], r["criterion"])
            lines.append(f"| {r['conv']} | {r['criterion']} | {fmt(r['expected'])} | " +
                         " | ".join(fmt(o.get(k, {}).get("got")) + ("" if o.get(k, {}).get("grade") in ("exact", "soft") else " ✗") for o in others) + " |")
        lines.append("")
    text = "\n".join(lines) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="run name (results/eval_runs/<name>.jsonl)")
    ap.add_argument("--confirm", action="append", default=[], help="extra run names to compare (e.g. Sonnet confirmation)")
    ap.add_argument("--run", action="store_true", help="first run scripts/eval_run.py on the synthetic set; other args go to it")
    ap.add_argument("--out", default=str(ROOT / "reports/robustness.md"))
    args, rest = ap.parse_known_args()
    if args.run:
        cmd = [sys.executable, str(ROOT / "scripts/eval_run.py"), "--data", str(SYN / "records.json"), "--name", args.name, *rest]
        print("$", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=False)
    expected = load_expected()
    runs = [(n, *load_run(n)) for n in [args.name, *args.confirm]]
    text = report(expected, runs, Path(args.out))
    print(text[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
