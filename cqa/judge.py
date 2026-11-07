"""Evaluate one dialogue against the checklist: LLM judge + rule hints + evidence verification + scoring.

Result = {
  dialogue_id, model, provider, created, lang, channel,
  items: [{id, title, verdict: yes|no|partial|na, score, weight, confidence, explanation, evidence: [{turn, quote, valid}],
           rule_signal, disagreement, critical, needs_review}],
  total_score (0..100), max_score, critical_fail, needs_review, review_reasons, summary, customer_issue, resolved
}
"""
from __future__ import annotations

import datetime as dt
import os

from rapidfuzz import fuzz

from . import checklist as ck
from . import llm, rules
from .loader import render

VERDICT_SCORE = {"yes": 1.0, "partial": 0.5, "no": 0.0}
CONF_REVIEW = float(os.environ.get("CQA_CONF_REVIEW", "0.7"))
CRITICAL_ZEROES = os.environ.get("CQA_CRITICAL_ZEROES", "0") == "1"
PROMPT_VERSION = "v1"

SYSTEM = """You are a senior quality-assurance auditor of the customer support of Freedom (broker & bank, Kazakhstan).
You audit support chats and call transcripts against the company's QA checklist. Conversations may be in Russian, Kazakh,
English or mixed. You are strict, fair and evidence-based: every verdict must be supported by quotes from numbered turns.
Only the OPERATOR (support employee) is evaluated; the CLIENT's behaviour is context. BOT turns are automated messages."""

INSTRUCTIONS = """Evaluate the conversation against EVERY checklist item.

For each item:
1. Find the evidence: up to 3 turns (by number) with SHORT exact quotes copied from the transcript (original language).
   If the item is NOT fulfilled, cite the turn where it should have happened (or [] if the moment never came).
2. Decide the verdict:
   - "yes": fully fulfilled; "partial": attempted but incomplete/late/incorrect; "no": not fulfilled or violated;
   - "na": the item is not applicable to this conversation (situation never arose) — do not use "na" just because it is hard.
3. confidence 0..1 — how sure you are (ambiguous wording, missing context, transcription errors → lower).
4. explanation: 1–2 sentences in Russian, concrete (what the operator did/did not do), referencing turn numbers like [5].

Also give: summary (2 sentences in Russian: what the client wanted and how it ended), customer_issue (short tag in Russian),
resolved (true/false/null).

Return ONLY JSON:
{"items":[{"id":"<item id>","evidence":[{"turn":<int>,"quote":"<exact quote>"}],"verdict":"yes|no|partial|na","confidence":0.0,"explanation":"..."}],
 "summary":"...","customer_issue":"...","resolved":true}"""


def _items_block(items: list[dict]) -> str:
    out = []
    for it in items:
        crit = " [CRITICAL]" if it.get("critical") else ""
        w = f" (weight {it['weight']:g})" if it.get("weight", 1) != 1 else ""
        desc = f"\n   Definition: {it['description']}" if it.get("description") else ""
        out.append(f"- id={it['id']}: {it['title']}{crit}{w}{desc}")
    return "\n".join(out)


def build_prompt(d: dict, items: list[dict], rule_res: dict, extra_guidance: str = "") -> str:
    return (
        f"CHECKLIST ({len(items)} items):\n{_items_block(items)}\n\n"
        + (f"ADDITIONAL GUIDANCE (from QA team calibration):\n{extra_guidance}\n\n" if extra_guidance else "")
        + f"CONVERSATION (channel={d['channel']}, language={d['lang']}, {len(d['turns'])} turns):\n{render(d)}\n\n"
        + rules.summary_for_prompt(rule_res) + "\n\n" + INSTRUCTIONS
    )


def _mock(d: dict, items: list[dict], rr: dict) -> dict:
    """Offline fallback: rule-based verdicts so the product works without any API key."""
    out = []
    for it in items:
        key = rules.rule_key_for(it)
        if key:
            sig = rr[key]["signal"]
            bad = key in ("rudeness", "sensitive_data_request")
            ok = (not sig) if bad else sig
            turns = rr[key]["turns"]
            out.append({"id": it["id"], "verdict": "yes" if ok else "no", "confidence": 0.6,
                        "evidence": [{"turn": t, "quote": next(x["text"] for x in d["turns"] if x["i"] == t)[:80]} for t in turns[:2]],
                        "explanation": f"[mock] Правило «{rr[key]['note']}»: {'найдено' if sig else 'не найдено'}" + (f" в репликах {turns}" if turns else "") + "."})
        else:
            out.append({"id": it["id"], "verdict": "yes", "confidence": 0.3, "evidence": [], "explanation": "[mock] Нет правила для пункта — требуется LLM или ручная проверка."})
    return {"items": out, "summary": "[mock] Оценка без LLM: только правила по ключевым словам.", "customer_issue": "", "resolved": None}


def _verify_evidence(d: dict, ev: list) -> list[dict]:
    by_i = {t["i"]: t["text"] for t in d["turns"]}
    out = []
    for e in ev or []:
        try:
            turn = int(e.get("turn"))
        except Exception:
            continue
        q = str(e.get("quote", ""))
        txt = by_i.get(turn)
        valid = txt is not None and (not q or fuzz.partial_ratio(q.lower(), txt.lower()) >= 70)
        out.append({"turn": turn, "quote": q, "valid": bool(valid)})
    return out


def evaluate(d: dict, items: list[dict], extra_guidance: str = "") -> dict:
    items_ = [it for it in items if ck.applies(it, d["channel"])]
    rr = rules.run(d)
    raw = llm.complete_json(SYSTEM, build_prompt(d, items_, rr, extra_guidance), max_tokens=6000, mock_fn=lambda: _mock(d, items_, rr))
    got = {str(x.get("id")): x for x in raw.get("items", []) if isinstance(x, dict)}
    res_items, reasons = [], []
    total = max_total = 0.0
    critical_fail = False
    for it in items_:
        x = got.get(str(it["id"]), {})
        verdict = str(x.get("verdict", "")).lower().strip()
        if verdict not in ("yes", "no", "partial", "na"):
            verdict = "no" if not x else "partial"
        conf = float(x.get("confidence", 0.5) or 0.5)
        ev = _verify_evidence(d, x.get("evidence"))
        key = rules.rule_key_for(it)
        rule_sig = rr[key]["signal"] if key else None
        bad_rule = key in ("rudeness", "sensitive_data_request")
        rule_ok = (not rule_sig) if (key and bad_rule) else rule_sig
        disagree = key is not None and verdict in ("yes", "no") and ((verdict == "yes") != bool(rule_ok))
        nr = []
        if not x:
            nr.append("нет ответа модели")
        if conf < CONF_REVIEW:
            nr.append(f"низкая уверенность {conf:.2f}")
        if ev and not all(e["valid"] for e in ev):
            nr.append("цитата не найдена в диалоге")
        if disagree:
            nr.append("модель и правило расходятся")
        if it.get("critical") and verdict in ("no", "partial"):
            nr.append("критический пункт нарушен")
            critical_fail = True
        sc = VERDICT_SCORE.get(verdict)
        if sc is not None:
            total += sc * it["weight"]
            max_total += it["weight"]
        res_items.append({
            "id": it["id"], "title": it["title"], "group": it.get("group", ""), "verdict": verdict,
            "score": sc, "weight": it["weight"], "confidence": round(conf, 2), "explanation": x.get("explanation", ""),
            "evidence": ev, "rule_signal": rule_sig, "disagreement": disagree, "critical": it.get("critical", False),
            "needs_review": bool(nr), "review_reasons": nr,
        })
        reasons += [f"{it['id']}: {r}" for r in nr]
    pct = 100 * total / max_total if max_total else None
    if critical_fail and CRITICAL_ZEROES:
        pct = 0.0
    return {
        "dialogue_id": d["id"], "channel": d["channel"], "lang": d["lang"], "provider": llm.provider(), "model": llm.model(),
        "prompt_version": PROMPT_VERSION, "created": dt.datetime.now().isoformat(timespec="seconds"),
        "items": res_items, "total_score": None if pct is None else round(pct, 1), "max_score": max_total,
        "critical_fail": critical_fail, "needs_review": bool(reasons), "review_reasons": reasons,
        "summary": raw.get("summary", ""), "customer_issue": raw.get("customer_issue", ""), "resolved": raw.get("resolved"),
        "rule_stats": rr.get("stats", {}),
    }
