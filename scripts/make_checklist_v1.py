"""Build data/checklists/v1.json from the organizers' checklist PDF (data/raw/Checklist.pdf, 3 pages, 8 criteria).

Texts of title / description / na_rule / scale_labels are copied from the PDF word for word.
Clarifications from pages 2-3 (scoring procedure and memo) are kept in `guidance` with their source page.

Machine fields:
- critical_when: the answer that raises the gross-error flag ("yes" for C8: «Да» = нарушение выявлено).
- hash: criterion_hash() — sha1 over the evaluation-relevant fields (everything that changes the evaluator prompt,
  including guidance and scale_labels). Weight and section are NOT hashed: changing them never needs re-evaluation.

Run from the repo root: python scripts/make_checklist_v1.py
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
C = []


def add(**k):
    k.setdefault("section", None)
    k.setdefault("scale", None)
    k.setdefault("scale_labels", None)
    k.setdefault("na_allowed", False)
    k.setdefault("na_rule", "")
    k.setdefault("critical", False)
    k.setdefault("critical_when", None)
    k.setdefault("critical_rule", None)
    k.setdefault("guidance", [])
    C.append(k)


S1, S2, S3, S4 = "Начало и понимание запроса", "Безопасность и решение", "Ведение и завершение диалога", "Грубые нарушения"
add(id="C1", section=S1, title="Установление контакта", answer_type="binary", weight=10, max_points=10,
    description="Да — оператор поздоровался на языке клиента и представился в первом ответе.",
    guidance=["Оценивается первый ответ оператора (реплики бота и системы — контекст). Язык приветствия должен совпадать с языком клиента (RU/KZ/EN)."])
add(id="C2", section=S1, title="Выявление потребности", answer_type="binary", weight=15, max_points=15, na_allowed=True,
    description="Да — оператор учёл известные детали и уточнил недостающее до ответа.",
    na_rule="N/A — данных для ответа хватало с самого начала.")
add(id="C3", section=S2, title="Идентификация", answer_type="binary", weight=20, max_points=20, na_allowed=True,
    description="Да — проверка личности подтверждена до раскрытия данных или защищённого действия.",
    na_rule="N/A — запрос не требовал идентификации.",
    guidance=["Памятка (стр. 3): общий вопрос о тарифах не требует идентификации → N/A.",
              "Памятка (стр. 3): если требовалась проверка личности перед защищённым действием и она не подтверждена в чате, ставьте «Нет»."])
add(id="C4", section=S2, title="Корректная и полная консультация", answer_type="scale", weight=20, max_points=20, na_allowed=True,
    scale=[0, 5, 10, 15, 20],
    scale_labels={"20": "Все существенные вопросы закрыты или дан корректный следующий шаг.",
                  "15": "Основной вопрос решён, пропущена второстепенная деталь.",
                  "10": "Часть запроса закрыта, существенный вопрос остался открытым.",
                  "5": "Есть общая подсказка, но клиенту всё ещё непонятно, как действовать.",
                  "0": "Ответа по существу нет или ключевая рекомендация ошибочна."},
    description="Оцените ответ или следующий шаг по шкале на стр. 2.",
    na_rule="N/A — чат закончился до того, как клиент сформулировал запрос.",
    guidance=["Памятка (стр. 3): консультация оценивает, правильно и полно ли решён запрос. Ведение диалога (C6) оценивает, насколько понятно и последовательно оператор общался."])
add(id="C5", section=S3, title="Эмпатия и клиентоориентированность", answer_type="binary", weight=15, max_points=15,
    description="Да — оператор вежлив и, если клиент выразил недовольство или описал неудобство, отреагировал на конкретную проблему.")
add(id="C6", section=S3, title="Управление диалогом, скорость и грамотность", answer_type="scale", weight=15, max_points=15,
    scale=[0, 4, 7, 10],
    scale_labels={"10": "Ответы ясные и связные. Если было явное ожидание, оператор предупредил.",
                  "7": "Один заметный сбой, но клиент может понять ответ и продолжить.",
                  "4": "Клиенту приходится переспрашивать из-за формулировок или порядка ответов.",
                  "0": "Из-за бессвязных или противоречивых реплик ход диалога непонятен."},
    description="Оцените ясность, порядок ответов и предупреждение об ожидании по шкале на стр. 2.",
    guidance=["Порог первой реакции 3 минуты, теги и вложения не оцениваются (стр. 2). Предупреждение клиента о явном ожидании в ходе диалога — часть C6 (ступень 10).",
              "Памятка (стр. 3): ясный ответ на один из двух важных вопросов может получить высокий балл за ведение диалога и частичный за консультацию."])
add(id="C7", section=S3, title="Завершение", answer_type="binary", weight=5, max_points=5, na_allowed=True,
    description="Да — оператор подвёл итог или предложил дальнейшую помощь и попрощался.",
    na_rule="N/A — чат оборвался до возможности его завершить.",
    guidance=["Пример обоснования (стр. 3): «Завершение — Нет. После ответа на вопрос оператор закрыл чат без прощания. Основание — последняя реплика оператора и сообщение о закрытии чата»."])
add(id="C8", section=S4, title="Грубые ошибки", answer_type="binary", weight=0, max_points=0, critical=True, critical_when="yes",
    description="Да — выявлено оскорбление клиента, предложение платной услуги в обход компании или раскрытие конфиденциальной информации. Нет — ни одно из трёх не выявлено.",
    critical_rule="Без баллов. «Да» = нарушение выявлено (инвертированная полярность). Отмечается отдельным флагом, в итоговый балл не входит.",
    guidance=["Памятка (стр. 3): ставьте флаг, если оператор предлагает перевести деньги ему лично для инвестирования или просит личный номер клиента, чтобы оказать платную консультацию в частном порядке.",
              "Памятка (стр. 3): достаточно самого предложения в переписке. Согласие клиента или перевод денег для отметки не требуются."])

HASHED = ("title", "description", "answer_type", "scale", "scale_labels", "na_allowed", "na_rule",
          "critical", "critical_when", "critical_rule", "guidance")


def _num(x):
    x = float(x)
    return int(x) if x.is_integer() else x


def h(c):
    """Mirror of cqa.models.criterion_hash (kept identical; tests compare them)."""
    labels = c.get("scale_labels")
    canon = {
        "title": (c.get("title") or "").strip(),
        "description": (c.get("description") or "").strip(),
        "answer_type": c["answer_type"],
        "scale": [_num(v) for v in c["scale"]] if c.get("scale") is not None else None,
        "scale_labels": {str(_num(k)): str(v).strip() for k, v in labels.items()} if labels else None,
        "na_allowed": bool(c.get("na_allowed")),
        "na_rule": (c.get("na_rule") or "").strip(),
        "critical": bool(c.get("critical")),
        "critical_when": c.get("critical_when") if c.get("critical") else None,
        "critical_rule": (c.get("critical_rule") or "").strip() or None,
        "guidance": [g.strip() for g in c.get("guidance") or [] if g and g.strip()],
    }
    s = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


for c in C:
    c["hash"] = h(c)
total_w = sum(c["weight"] for c in C)
assert total_w == 100, total_w
out = {
    "checklist_id": "freedom-chat-quality", "version": 1, "name": "Чек-лист качества чатов (Freedom Broker, хакатон)",
    "official": True,
    "criteria": C, "sections": [S1, S2, S3, S4],
    "rounding": {"total": 2, "mode": "half_up"},
    "scoring": {
        "yes": "полный вес", "no": 0,
        "scale": "points = value / max(scale) * weight (верхняя ступень = полный вес)",
        "na": "критерии с N/A исключаются, их веса пропорционально перераспределяются между применимыми; максимум остаётся 100",
        "no_applicable": "если применимых критериев с весом нет, итог не рассчитывается",
        "critical": "критерий с critical=true отмечает грубую ошибку, если ответ == critical_when (C8: «Да» = нарушение); вес 0 — в итог не входит",
        "procedure_pdf": [
            "Вес показывает, сколько баллов критерий может дать до перераспределения при N/A.",
            "1. Определите применимость. Ставьте N/A только там, где он разрешён и выполнено условие со стр. 1.",
            "2. Выставьте оценку. «Да» даёт полный вес, «Нет» — ноль. Для шкального критерия выберите одну из указанных ступеней.",
            "3. Пересчитайте шкальные оценки в баллы пропорционально весу. Верхняя ступень шкалы должна давать полный вес критерия.",
            "4. Исключите критерии с N/A и пропорционально перераспределите их веса между применимыми критериями. Максимум должен остаться 100 баллов.",
            "5. Округлите итог до двух знаков. Если применимых критериев с весом нет, итог не рассчитывается. Грубые ошибки отметьте отдельно.",
            "В этом задании порог первой реакции в 3 минуты, теги и вложения в оценку не входят.",
        ],
    },
    "change_note": "Перенесено из Checklist.pdf организаторов без изменений формулировок; guidance — цитаты и пояснения со стр. 2–3",
    "created_at": "2026-11-07",
    "source": "data/raw/Checklist.pdf",
}
p = ROOT / "data/checklists/v1.json"
p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("written", p.relative_to(ROOT), "criteria", len(C), "weights", [c["weight"] for c in C], "sum", total_w)
