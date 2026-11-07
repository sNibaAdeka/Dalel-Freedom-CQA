"""Load the QA checklist (csv/xlsx/json/md/txt) into a normalised list of items."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

ID_K = ("id", "code", "item_id", "criterion_id", "№", "no", "n", "номер", "код")
TITLE_K = ("title", "name", "criterion", "criteria", "item", "критерий", "пункт", "название", "параметр", "check")
DESC_K = ("description", "desc", "definition", "details", "rule", "описание", "пояснение", "комментарий", "как оценивать", "guidance")
WEIGHT_K = ("weight", "points", "score", "max_score", "max", "вес", "баллы", "балл", "макс")
CRIT_K = ("critical", "critical_error", "is_critical", "критичный", "критическая", "критично")
CH_K = ("channel", "applies_to", "канал", "тип")
GROUP_K = ("group", "section", "category", "block", "блок", "раздел", "категория")


def _col(cols, keys):
    low = {str(c).strip().lower(): c for c in cols}
    for k in keys:
        if k in low:
            return low[k]
    for k in keys:
        for lc, c in low.items():
            if k in lc:
                return c
    return None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "да", "y", "+", "critical", "крит", "иә")


def from_frame(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(how="all")
    c_id, c_t, c_d, c_w, c_c, c_ch, c_g = (_col(df.columns, k) for k in (ID_K, TITLE_K, DESC_K, WEIGHT_K, CRIT_K, CH_K, GROUP_K))
    if c_t is None:
        c_t = df.columns[1] if len(df.columns) > 1 else df.columns[0]
    items = []
    for k, (_, srow) in enumerate(df.iterrows(), start=1):
        row = srow.to_dict()
        title = str(row[c_t]).strip()
        if not title or title.lower() == "nan":
            continue
        w = pd.to_numeric(row.get(c_w), errors="coerce") if c_w else None
        items.append({
            "id": str(row[c_id]).strip() if c_id and pd.notna(row[c_id]) else f"Q{k}",
            "title": title,
            "description": str(row[c_d]).strip() if c_d and pd.notna(row[c_d]) else "",
            "weight": float(w) if w is not None and pd.notna(w) else 1.0,
            "critical": _truthy(row[c_c]) if c_c else False,
            "applies_to": str(row[c_ch]).strip().lower() if c_ch and pd.notna(row[c_ch]) else "all",
            "group": str(row[c_g]).strip() if c_g and pd.notna(row[c_g]) else "",
        })
    return items


def from_text(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$", line)
        if m:
            t = m.group(1).strip()
            title, _, desc = t.partition(" — ") if " — " in t else t.partition(": ")
            items.append({"id": f"Q{len(items) + 1}", "title": title.strip(), "description": desc.strip(), "weight": 1.0, "critical": False, "applies_to": "all", "group": ""})
    return items


def load(path: str | Path) -> list[dict]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".csv", ".tsv"):
        items = from_frame(pd.read_csv(p, sep=None, engine="python"))
    elif ext in (".xlsx", ".xls"):
        items = from_frame(pd.read_excel(p))
    elif ext == ".json":
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            obj = obj.get("items") or obj.get("criteria") or obj.get("checklist") or list(obj.values())[0]
        items = from_frame(pd.DataFrame(obj))
    else:
        items = from_text(p.read_text(encoding="utf-8"))
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        for k, it in enumerate(items, start=1):
            it["id"] = f"Q{k}"
    return items


def find(folder: str | Path) -> Path | None:
    """Guess the checklist file inside a folder."""
    for p in sorted(Path(folder).rglob("*")):
        if p.is_file() and any(w in p.name.lower() for w in ("checklist", "чек", "criteria", "критер", "rubric")):
            return p
    return None


def applies(item: dict, channel: str) -> bool:
    a = item.get("applies_to", "all")
    return a in ("all", "", "nan", "оба", "both") or channel in a or (channel == "call" and "звон" in a) or (channel == "chat" and "чат" in a)
