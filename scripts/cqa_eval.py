"""Agreement of AI verdicts with gold (expert) labels.

python scripts/cqa_eval.py results/dev_v1 data/raw/<gold_file>        (csv/xlsx/json)
Gold formats accepted: long (dialogue_id, item_id, verdict|score) or wide (dialogue_id + one column per item id).
Verdict mapping: 1/yes/да/+ -> yes, 0/no/нет/- -> no, 0.5/partial/частично -> partial, na/н/п/- empty -> na.
Writes reports/cqa_eval.md (per-item accuracy, Cohen's kappa, confusion, worst dialogues) and logs to experiments.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import cohen_kappa_score  # noqa: E402


def norm(v) -> str:
    s = str(v).strip().lower()
    if s in ("1", "1.0", "yes", "да", "+", "true", "y", "иә", "выполнено", "ok"):
        return "yes"
    if s in ("0", "0.0", "no", "нет", "false", "n", "жоқ", "не выполнено", "fail"):
        return "no"
    if s in ("0.5", "partial", "частично", "p"):
        return "partial"
    if s in ("na", "n/a", "н/п", "nan", "", "none", "-", "не применимо"):
        return "na"
    try:
        x = float(s)
        return "yes" if x >= 0.75 else "partial" if x > 0.25 else "no"
    except ValueError:
        return s


def load_gold(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p, sep=None, engine="python") if p.suffix in (".csv", ".tsv") else pd.read_excel(p) if p.suffix in (".xlsx", ".xls") else pd.DataFrame(json.loads(p.read_text()))
    cols = {c.lower(): c for c in df.columns}
    did = next(cols[k] for k in cols if k in ("dialogue_id", "dialog_id", "chat_id", "call_id", "conversation_id", "id"))
    iid = next((cols[k] for k in cols if k in ("item_id", "criterion_id", "question_id", "criterion", "item")), None)
    val = next((cols[k] for k in cols if k in ("verdict", "score", "value", "label", "answer", "оценка")), None)
    if iid and val:
        g = df[[did, iid, val]].rename(columns={did: "dialogue_id", iid: "item_id", val: "gold"})
    else:
        g = df.melt(id_vars=[did], var_name="item_id", value_name="gold").rename(columns={did: "dialogue_id"})
    g["dialogue_id"] = g["dialogue_id"].astype(str)
    g["item_id"] = g["item_id"].astype(str)
    g["gold"] = g["gold"].map(norm)
    return g


def main(res_dir: str, gold_path: str, name: str | None = None):
    rows = []
    for p in Path(res_dir).glob("*.json"):
        if p.name == "errors.json":
            continue
        r = json.loads(p.read_text())
        for i in r["items"]:
            rows.append({"dialogue_id": str(r["dialogue_id"]), "item_id": str(i["id"]), "title": i["title"], "ai": i["verdict"], "conf": i["confidence"]})
    ai = pd.DataFrame(rows)
    gold = load_gold(Path(gold_path))
    m = ai.merge(gold, on=["dialogue_id", "item_id"], how="inner")
    if m.empty:
        print("no overlap between results and gold — check ids", ai.head(3), gold.head(3), sep="\n")
        return
    m["ok"] = m["ai"] == m["gold"]
    overall = m["ok"].mean()
    kappa = cohen_kappa_score(m["gold"], m["ai"])
    per = m.groupby(["item_id", "title"]).apply(lambda g: pd.Series({"n": len(g), "acc": g["ok"].mean(), "kappa": cohen_kappa_score(g["gold"], g["ai"]) if g["gold"].nunique() > 1 or g["ai"].nunique() > 1 else np.nan, "ai_yes%": (g["ai"] == "yes").mean() * 100, "gold_yes%": (g["gold"] == "yes").mean() * 100}), include_groups=False).reset_index().sort_values("acc")
    conf = pd.crosstab(m["gold"], m["ai"], rownames=["gold"], colnames=["ai"])
    worst = m.groupby("dialogue_id")["ok"].mean().sort_values().head(10)
    calib = m.assign(bin=pd.cut(m["conf"], [0, 0.5, 0.7, 0.85, 1.0])).groupby("bin", observed=True)["ok"].agg(["mean", "size"])
    md = [f"# CQA eval: {res_dir} vs {gold_path}", "", f"Pairs: {len(m)} · **item accuracy {overall:.1%}** · Cohen's kappa {kappa:.3f}", "",
          "## Per item (worst first)", per.round(3).to_markdown(index=False), "", "## Confusion (rows=gold)", conf.to_markdown(), "",
          "## Accuracy by model confidence (is confidence useful for routing to review?)", calib.round(3).to_markdown(), "",
          "## Worst dialogues", worst.round(2).to_markdown(), "",
          "## Disagreements sample", m[~m["ok"]].head(25)[["dialogue_id", "item_id", "gold", "ai", "conf"]].to_markdown(index=False)]
    Path("reports").mkdir(exist_ok=True)
    Path("reports/cqa_eval.md").write_text("\n".join(md))
    print("\n".join(md[:6]))
    try:
        from dc import tracker

        tracker.log(name or Path(res_dir).name, "item_acc", overall, notes=f"kappa={kappa:.3f} n={len(m)}")
    except Exception as e:
        print("tracker:", e)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
