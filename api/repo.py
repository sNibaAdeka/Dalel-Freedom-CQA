"""Storage adapter of the API server over ``cqa.db.Database`` (a1: SQLite WAL, results/cqa.sqlite, env CQA_DB).

Gives ``cqa.runs`` / ``cqa.review`` / the routers plain dicts (frontend JSON) instead of pydantic objects, and adds
what the API needs on top of the tables: evaluation lookup by criterion wording (hash) with mode preference,
dossiers (settings key ``dossier:<appeal_id>``), run snapshots. Evaluations are keyed by
(conv_id, criterion_hash, prompt_version, mode): reviewer decisions live in their own table, so an appeal never
modifies the original model evaluation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from cqa.db import Database
from cqa.models import now_iso


def plain(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    return obj


def eval_id(ev: dict) -> str:
    return f"{ev.get('conv_id')}:{ev.get('criterion_hash')}:{ev.get('mode') or ''}:{ev.get('prompt_version') or ''}"


def pick_eval(cands: list[dict], modes: Optional[list[str]] = None, prompt_version: Optional[str] = None) -> Optional[dict]:
    """Newest answered evaluation among candidates (oldest -> newest); modes = acceptable modes in preference order."""
    rows = [(i, e) for i, e in enumerate(cands) if e.get("answer") is not None]
    if modes is not None:
        rank = {m: i for i, m in enumerate(modes)}
        rows = [(i, e) for i, e in rows if (e.get("mode") or "") in rank]
    else:
        rank = {}
    if not rows:
        return None
    if prompt_version:
        rows = [(i, e) for i, e in rows if (e.get("prompt_version") or "") == prompt_version]
        if not rows:
            return None
    rows.sort(key=lambda r: (rank.get(r[1].get("mode") or "", 0), -r[0]))
    return rows[0][1]


class Repo:
    def __init__(self, db: Database | str | Path | None = None):
        self.db = db if isinstance(db, Database) else Database(db)

    # --- settings --------------------------------------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        return self.db.get_setting(key, default)

    def set_settings(self, **values) -> None:
        for k, v in values.items():
            if v is not None:
                self.db.set_setting(k, v)

    # --- conversations -------------------------------------------------------------------------------------------
    def upsert_conversations(self, convs: Iterable[dict]) -> tuple[int, int]:
        existing = {c.id for c in self.db.list_conversations()}
        new = updated = 0
        batch = []
        for conv in convs:
            conv = plain(conv)
            conv.setdefault("ingested_at", now_iso())
            batch.append(conv)
            if conv["id"] in existing:
                updated += 1
            else:
                new += 1
                existing.add(conv["id"])
        self.db.upsert_conversations(batch)
        return new, updated

    def conversation(self, conv_id: str) -> Optional[dict]:
        conv = self.db.get_conversation(conv_id)
        return conv.model_dump(mode="json") if conv else None

    def conversations(self, group_id: Optional[str] = None, language: Optional[str] = None,
                      channel: Optional[str] = None) -> list[dict]:
        return [c.model_dump(mode="json") for c in self.db.list_conversations(group_id, language, channel)]

    # --- evaluations ---------------------------------------------------------------------------------------------
    def add_evals(self, evals: Iterable[Any]) -> list[dict]:
        out = []
        for ev in evals:
            ev = plain(ev)
            ev.setdefault("created_at", now_iso())
            ev["evaluation_id"] = ev.get("evaluation_id") or eval_id(ev)
            self.db.save_eval(ev)
            out.append(ev)
        return out

    def evals_for(self, conv_id: str) -> list[dict]:
        return [e.model_dump(mode="json") for e in self.db.list_evals(conv_id)]

    def all_evals(self) -> list[dict]:
        return [e.model_dump(mode="json") for e in self.db.list_evals()]

    def find_eval(self, conv_id: str, criterion_hash: str, modes: Optional[list[str]] = None,
                  prompt_version: Optional[str] = None) -> Optional[dict]:
        cands = [e for e in self.evals_for(conv_id) if e.get("criterion_hash") == criterion_hash]
        return pick_eval(cands, modes, prompt_version)

    # --- appeals, decisions, dossiers ----------------------------------------------------------------------------
    def add_appeal(self, appeal: dict) -> dict:
        return self.db.create_appeal(plain(appeal)).model_dump(mode="json")

    def update_appeal(self, appeal_id: str, status: str, **_ignored) -> dict:
        return self.db.set_appeal_status(appeal_id, status).model_dump(mode="json")

    def appeal(self, appeal_id: str) -> Optional[dict]:
        a = self.db.get_appeal(appeal_id)
        return a.model_dump(mode="json") if a else None

    def list_appeals(self, status: Optional[str] = None, conv_id: Optional[str] = None) -> list[dict]:
        return [a.model_dump(mode="json") for a in self.db.list_appeals(status, conv_id)]

    def add_decision(self, decision: dict) -> dict:
        return self.db.add_decision(plain(decision)).model_dump(mode="json")

    def list_decisions(self, conv_id: Optional[str] = None, appeal_id: Optional[str] = None) -> list[dict]:
        return [d.model_dump(mode="json") for d in self.db.list_decisions(conv_id, appeal_id)]

    def put_dossier(self, appeal_id: str, dossier: dict) -> dict:
        return self.db.put_dossier(appeal_id, plain(dossier))

    def dossier(self, appeal_id: str) -> Optional[dict]:
        return self.db.get_dossier(appeal_id)

    # --- audit, runs, llm calls ----------------------------------------------------------------------------------
    def log(self, entity: str, entity_id: str, event: str, payload: Optional[dict] = None,
            conv_id: Optional[str] = None) -> dict:
        return self.db.add_audit(entity, entity_id, event, plain(payload or {}), conv_id).model_dump(mode="json")

    def audit(self, conv_id: Optional[str] = None) -> list[dict]:
        return [a.model_dump(mode="json") for a in self.db.list_audit(conv_id=conv_id)]

    def save_run(self, run: dict) -> None:
        self.db.save_run(run["run_id"], run["status"], plain(run), run.get("version"))

    def get_run(self, run_id: str) -> Optional[dict]:
        return self.db.get_run(run_id)

    def list_runs(self, limit: int = 20) -> list[dict]:
        return self.db.list_runs(limit)

    def log_llm_call(self, call: dict) -> None:
        self.db.log_llm_call(plain(call))

    def llm_stats(self) -> dict:
        return self.db.llm_stats()
