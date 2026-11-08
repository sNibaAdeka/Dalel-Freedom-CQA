"""Code-decided criteria from model sub-checks (calibration v7.1).

For two organizer criteria the checklist wording is a logical formula, and models get the logic wrong more often
than the facts. The model therefore answers short binary sub-questions ("checks") and the final answer is computed
here. Sub-check specs live as data in guidance_v1.json (entry["checks"]) and apply only to the unchanged organizer
criterion (same overlay match as guidance), so a criterion replaced in the UI is evaluated normally.

Rules:
  c7_closing       Завершение: (итог/следующий шаг ИЛИ предложение помощи) И прощание; N/A only if the client or the
                   connection ended the dialogue before the operator closed it; operator lines after the break are ignored.
  c3_verification  Идентификация: N/A unless the operator disclosed client-specific data or acted on the account;
                   then yes iff verification precedes it (chat: system verification event; call: operator's data check).
"""
from __future__ import annotations

import re
from typing import Any, Callable

BREAK_RX = re.compile(r"покинул|left the chat|прерван|үзілді|connection.*lost|lost connection|завершил разговор|ended the call", re.I)
VERIFIED_RX = re.compile(r"личност|identity|жеке бас", re.I)
# A system event only counts as verification when it is not negated/failed ("не подтверждена", "verification failed", "расталмады").
NOT_VERIFIED_RX = re.compile(r"\bне\s|не\s*подтвержд|не\s*пройден|failed|\bnot\b|unsuccess|denied|reject|отклон|ошибк|расталмады|сәтсіз|қате|жоқ", re.I)


def _flag(checks: dict, name: str) -> bool | None:
    v = checks.get(name)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "да", "1"):
            return True
        if s in ("false", "no", "нет", "0"):
            return False
    return None


def c7_closing(conv: dict, checks: dict) -> tuple[Any, str] | None:
    msgs = conv.get("messages") or []
    first_break = next((m["idx"] for m in msgs if m.get("speaker") == "system" and BREAK_RX.search(str(m.get("text", "")))), None)
    last_op = max((m["idx"] for m in msgs if m.get("speaker") == "operator" and (first_break is None or m["idx"] < first_break)),
                  default=-1)
    fw, sm, hp = _flag(checks, "farewell"), _flag(checks, "summary_or_next_step"), _flag(checks, "offer_more_help")
    if fw is None or (sm is None and hp is None):
        return None  # incomplete sub-checks: keep the model's own answer
    if fw and (sm or hp):
        return "yes", "Завершение по формуле чек-листа: прощание есть, " + ("итог или следующий шаг есть." if sm else "предложение помощи есть.")
    if first_break is not None and first_break > last_op and not fw:
        return "na", "Диалог оборвался со стороны клиента или связи до того, как оператор мог его завершить."
    if not fw:
        return "no", "Завершение по формуле чек-листа: прощания нет."
    return "no", "Завершение по формуле чек-листа: только прощание, без итога/следующего шага и без предложения помощи."


def c3_verification(conv: dict, checks: dict) -> tuple[Any, str] | None:
    disc = _flag(checks, "client_specific_disclosure_or_action")
    if disc is None:
        return None
    if not disc:
        return "na", "Запрос не требовал идентификации: данные конкретного счёта не раскрывались, защищённых действий не было."
    msgs = conv.get("messages") or []
    first = next((m["idx"] for m in msgs if str(m.get("id")) == str(checks.get("first_disclosure_message_id"))), None)
    if conv.get("channel") == "chat" and first is not None:
        ver = next((m for m in msgs if m.get("speaker") == "system" and VERIFIED_RX.search(str(m.get("text", "")))
                    and not NOT_VERIFIED_RX.search(str(m.get("text", "")))), None)
        ok = bool(ver and ver["idx"] < first)
    else:  # calls: verification is a data check by the operator, judged by the model
        ok = _flag(checks, "verified_before_disclosure")
        if ok is None:
            return None
    if ok:
        return "yes", "Личность подтверждена до раскрытия сведений или защищённого действия."
    return "no", "Сведения о счёте раскрыты или защищённое действие выполнено без подтверждённой идентификации."


RULES: dict[str, Callable[[dict, dict], tuple[Any, str] | None]] = {"c7_closing": c7_closing, "c3_verification": c3_verification}


def apply(rule: str | None, conv: dict, checks: Any) -> tuple[Any, str] | None:
    """(answer, reason) decided by code, or None when the rule is unknown or the sub-checks are missing/incomplete."""
    fn = RULES.get(rule or "")
    if fn is None or not isinstance(checks, dict):
        return None
    return fn(conv, checks)
