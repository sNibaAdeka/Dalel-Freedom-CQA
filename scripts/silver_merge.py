"""Merge two independent silver-labelling passes of DEV (data/processed/silver/pass_A|B/batch_*.json).

Outputs
  reports/silver_agreement.md          agreement/kappa per criterion, per language, total-score gap, evidence validity
  data/processed/silver/disputed.md    every A/B disagreement with both explanations -> for HUMAN review (Adilbek + Alim)
  eval/gold/silver.jsonl               {conv_id, criterion_id, answer, annotator} for agreed items (+ resolved ones from resolutions.json)
  data/processed/silver/totals.csv     total score per dialogue for A and B using the checklist formula

Human resolution of disputes: put {"<conv_id>|<criterion>": <answer>} into data/processed/silver/resolutions.json and re-run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
SIL = ROOT / "data/processed/silver"
CK = json.loads((ROOT / "data/checklists/v1.json").read_text())
CRIT = {c["id"]: c for c in CK["criteria"]}
IDS = [c["id"] for c in CK["criteria"]]
recs = {r["conversation_id"]: r for r in json.loads((ROOT / "data/raw/dev_records.json").read_text())}


def norm(a):
    if isinstance(a, str):
        s = a.strip().lower()
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
    return float(a) if a is not None else None


def load(pass_name):
    out = {}
    for p in sorted((SIL / f"pass_{pass_name}").glob("batch_*.json")):
        d = json.loads(p.read_text())
        for dl in d["dialogues"]:
            out[dl["conversation_id"]] = dl
    return out


def total(ans):
    """Checklist formula: yes=weight, no=0, scale=value/max*weight, N/A redistributed, 2 decimals, C8 separate."""
    num = den = 0.0
    for cid in IDS:
        c = CRIT[cid]
        if c["weight"] == 0:
            continue
        a = norm(ans[cid]["answer"])
        if a == "na":
            continue
        den += c["weight"]
        if c["answer_type"] == "scale":
            num += c["weight"] * float(a) / max(c["scale"])
        else:
            num += c["weight"] if a == "yes" else 0
    return None if den == 0 else round(100 * num / den, 2)


def valid_quote(conv_id, ev):
    msgs = {f"{conv_id}:{i}": t["text"] for i, t in enumerate(recs[conv_id]["transcript"])}
    t = msgs.get(ev.get("message_id"))
    q = str(ev.get("quote", ""))
    return t is not None and (not q or fuzz.partial_ratio(q.lower(), t.lower()) >= 85)


def main():
    A, B = load("A"), load("B")
    ids = [i for i in recs if i in A and i in B]
    missing = [i for i in recs if i not in A or i not in B]
    print(f"A: {len(A)} B: {len(B)} both: {len(ids)} missing: {len(missing)} {missing[:10]}")
    rows, disputes, gold = [], [], []
    res_path = SIL / "resolutions.json"
    resolutions = json.loads(res_path.read_text()) if res_path.exists() else {}
    ev_total = ev_ok = 0
    for i in ids:
        a, b = A[i]["answers"], B[i]["answers"]
        lang, ch = recs[i]["language"], recs[i]["channel"]
        for cid in IDS:
            if cid not in a or cid not in b:
                continue
            x, y = norm(a[cid]["answer"]), norm(b[cid]["answer"])
            for who, d in (("A", a[cid]), ("B", b[cid])):
                for ev in d.get("evidence", []) or []:
                    ev_total += 1
                    ev_ok += valid_quote(i, ev)
            agree = x == y
            rows.append({"conv": i, "lang": lang, "channel": ch, "crit": cid, "A": x, "B": y, "agree": agree})
            key = f"{i}|{cid}"
            final = x if agree else (norm(resolutions[key]) if key in resolutions else None)
            if not agree:
                disputes.append((i, lang, cid, a[cid], b[cid], A[i].get("notes", ""), B[i].get("notes", "")))
            if final is not None:
                gold.append({"conv_id": i, "criterion_id": cid, "answer": final, "annotator": "silver_agree" if agree else "silver_resolved"})
    df = pd.DataFrame(rows)
    tot = pd.DataFrame([{"conv": i, "lang": recs[i]["language"], "A": total(A[i]["answers"]), "B": total(B[i]["answers"])} for i in ids])
    tot["abs_diff"] = (tot["A"] - tot["B"]).abs()
    tot.to_csv(SIL / "totals.csv", index=False)

    def kappa(sub):
        from sklearn.metrics import cohen_kappa_score

        x, y = sub["A"].astype(str), sub["B"].astype(str)
        return cohen_kappa_score(x, y) if x.nunique() > 1 or y.nunique() > 1 else float("nan")

    per = df.groupby("crit").apply(lambda g: pd.Series({"n": len(g), "agree_%": 100 * g["agree"].mean(), "kappa": kappa(g)}), include_groups=False).round(2)
    perl = df.groupby("lang")["agree"].mean().mul(100).round(1)
    perc = df.groupby("channel")["agree"].mean().mul(100).round(1)
    dist = df.groupby("crit")["A"].apply(lambda s: s.astype(str).value_counts().to_dict())
    md = [
        "# Серебряный эталон DEV: согласие двух независимых проходов", "",
        f"Диалогов с обоими проходами: {len(ids)}/65 · пар «диалог×критерий»: {len(df)} · **полное согласие: {100 * df['agree'].mean():.1f}%** · спорных: {len(disputes)}", "",
        "## По критериям", per.to_markdown(), "", "## По языкам, %", perl.to_markdown(), "", "## По каналам, %", perc.to_markdown(), "",
        f"## Итоговый балл: |A−B| средний {tot['abs_diff'].mean():.2f}, медиана {tot['abs_diff'].median():.2f}, макс {tot['abs_diff'].max():.2f}; "
        f"диалогов с разницей > 5 баллов: {(tot['abs_diff'] > 5).sum()}", "",
        f"## Доказательства: цитат проверено {ev_total}, найдены в тексте реплики {ev_ok} ({100 * ev_ok / max(ev_total, 1):.1f}%)", "",
        "## Распределение ответов (проход A)", pd.DataFrame(dist).fillna(0).astype(int).T.to_markdown() if False else "\n".join(f"- {k}: {v}" for k, v in dist.items()), "",
        f"Незавершённые диалоги: {missing}",
    ]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/silver_agreement.md").write_text("\n".join(md))
    dm = ["# Спорные случаи для ручного решения", "", "Запиши решение в `data/processed/silver/resolutions.json` как {\"conv_id|C#\": ответ} и перезапусти `python scripts/silver_merge.py`.", ""]
    for i, lang, cid, da, db, na, nb in disputes:
        dm += [f"## {i} ({lang}) · {cid} {CRIT[cid]['title']}",
               f"- **A: {da['answer']}** (conf {da.get('confidence')}) — {da.get('explanation')}  \n  доказательства: {[(e.get('message_id'), e.get('quote')) for e in da.get('evidence', [])][:2]}",
               f"- **B: {db['answer']}** (conf {db.get('confidence')}) — {db.get('explanation')}  \n  доказательства: {[(e.get('message_id'), e.get('quote')) for e in db.get('evidence', [])][:2]}"]
        if na or nb:
            dm.append(f"- заметки A: {na or '—'} | заметки B: {nb or '—'}")
        dm.append("")
    (SIL / "disputed.md").write_text("\n".join(dm))
    (ROOT / "eval/gold").mkdir(parents=True, exist_ok=True)
    (ROOT / "eval/gold/silver.jsonl").write_text("\n".join(json.dumps(g, ensure_ascii=False) for g in gold))
    print(f"agreement {100 * df['agree'].mean():.1f}% · disputes {len(disputes)} · gold rows {len(gold)} · evidence valid {100 * ev_ok / max(ev_total, 1):.1f}%")
    print(per.to_string())


if __name__ == "__main__":
    sys.exit(main())
