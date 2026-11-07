"""SQLite storage for «Дәлел» (WAL, no ORM). Path: env CQA_DB, default results/cqa.sqlite (git-ignored).

Every method opens a short-lived connection, so one Database object is safe to share between threads
(background runs evaluate in parallel). Rows keep the full pydantic object as JSON in `data`; a few columns
are duplicated for filtering. Tables: conversations, checklist_versions, settings, evaluations
(UNIQUE conv_id + criterion_hash + prompt_version + mode), appeals, decisions, audit_log, llm_calls, runs,
dossiers.

    from cqa.db import Database, get_db
    db = get_db()                          # default path; Database(tmp_path / "t.sqlite") in tests
    db.upsert_conversation(conv); db.save_eval(ev); db.latest_evals(conv_id, hashes)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from .models import (Appeal, AuditEvent, ChecklistVersion, Conversation, CriterionEval, Decision, LLMCall,
                     now_iso)

__all__ = ["Database", "ConversationConflictError", "get_db", "db_path", "SCHEMA_VERSION"]

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL, channel TEXT NOT NULL, language TEXT NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_conversations_group ON conversations(group_id);
CREATE TABLE IF NOT EXISTS checklist_versions (
  version INTEGER PRIMARY KEY, checklist_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL, criterion_id TEXT NOT NULL, criterion_hash TEXT NOT NULL,
  prompt_version TEXT NOT NULL, mode TEXT NOT NULL,
  answer TEXT, needs_review INTEGER NOT NULL DEFAULT 0, model TEXT, cost_kzt REAL NOT NULL DEFAULT 0,
  data TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE (conv_id, criterion_hash, prompt_version, mode));
CREATE INDEX IF NOT EXISTS ix_evaluations_conv ON evaluations(conv_id);
CREATE TABLE IF NOT EXISTS appeals (
  id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, criterion_id TEXT NOT NULL, status TEXT NOT NULL,
  data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_appeals_conv ON appeals(conv_id);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, appeal_id TEXT NOT NULL, conv_id TEXT, criterion_id TEXT,
  data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_decisions_conv ON decisions(conv_id);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL, conv_id TEXT,
  event TEXT NOT NULL, payload TEXT NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_conv ON audit_log(conv_id);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, provider TEXT, model TEXT, purpose TEXT,
  conv_id TEXT, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0, cost_kzt REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
  cache_hit INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, version INTEGER, status TEXT NOT NULL, data TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dossiers (
  appeal_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def db_path() -> Path:
    env = os.environ.get("CQA_DB")
    return Path(env) if env else ROOT / "results" / "cqa.sqlite"


def _dump(obj: Any) -> str:
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json()
    return json.dumps(obj, ensure_ascii=False, default=str)


class ConversationConflictError(ValueError):
    """An existing dialogue's evaluation inputs are immutable; API adapters should return HTTP 409."""

    status = 409


def _conversation_content(conv: Conversation) -> dict:
    """Identity, text, speakers and timing bind evaluations and reviewer decisions to a dialogue.

    Grouping/import metadata, audio alignment and translated subtitles may be enriched independently.
    Include legacy transcript input too, since the evaluator accepts it when messages are empty.
    """
    return {"channel": conv.channel, "language": conv.language,
            "messages": [m.model_dump(exclude={"rec_sec", "translation_ru"}) for m in conv.messages],
            "transcript": getattr(conv, "transcript", None) if not conv.messages else None}


class Database:
    def __init__(self, path: Union[str, Path, None] = None):
        self.path = Path(path) if path else db_path()
        if str(self.path) == ":memory:":
            raise ValueError("Используйте файл (например, tmp_path / 'cqa.sqlite'): каждое соединение открывается заново")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('schema_version', ?)",
                         (json.dumps(SCHEMA_VERSION),))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    # -------------------------------------------------------------------------------- conversations
    def upsert_conversation(self, conv: Union[Conversation, dict]) -> Conversation:
        """Insert/enrich one dialogue using the same atomic conflict checks as a batch."""
        return self.upsert_conversations([conv])[0]

    def upsert_conversations(self, convs: Iterable[Union[Conversation, dict]]) -> list[Conversation]:
        """Atomically insert/enrich a batch, returning validated conversations in input order.

        Changed evaluation inputs require a new id, even before evaluations exist. All checks/writes share
        one write transaction across Database instances/processes; a conflict rolls back the entire batch.
        A repeated id may enrich metadata, but conflicting content inside the batch is rejected as well.
        """
        conversations = [Conversation.model_validate(conv.model_dump() if isinstance(conv, Conversation) else conv)
                         for conv in convs]
        if not conversations:
            return []
        ts = now_iso()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for conv in conversations:
                existing = c.execute("SELECT data FROM conversations WHERE id = ?", (conv.id,)).fetchone()
                if existing is not None:
                    previous = Conversation.model_validate_json(existing["data"])
                    if _conversation_content(previous) != _conversation_content(conv):
                        raise ConversationConflictError(
                            f"Диалог {conv.id!r} уже существует с другим содержимым; используйте новый id. "
                            "Исходный диалог, оценки и решения сохранены."
                        )
                c.execute("""INSERT INTO conversations(id, group_id, channel, language, data, created_at, updated_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?)
                             ON CONFLICT(id) DO UPDATE SET group_id = excluded.group_id, channel = excluded.channel,
                               language = excluded.language, data = excluded.data, updated_at = excluded.updated_at""",
                          (conv.id, conv.group_id, conv.channel, conv.language, _dump(conv), ts, ts))
        return conversations

    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        with self.connect() as c:
            row = c.execute("SELECT data FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return Conversation.model_validate_json(row["data"]) if row else None

    def list_conversations(self, group_id: Optional[str] = None, language: Optional[str] = None,
                           channel: Optional[str] = None) -> list[Conversation]:
        sql, args = "SELECT data FROM conversations WHERE 1=1", []
        for col, val in (("group_id", group_id), ("language", language), ("channel", channel)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        with self.connect() as c:
            rows = c.execute(sql + " ORDER BY id", args).fetchall()
        return [Conversation.model_validate_json(r["data"]) for r in rows]

    # ---------------------------------------------------------------------------- checklist versions
    def save_checklist_version(self, cl: ChecklistVersion, replace: bool = True) -> ChecklistVersion:
        """replace=False raises sqlite3.IntegrityError if the version number is taken (versions are immutable)."""
        if cl.version < 1:
            raise ValueError("Версия чек-листа должна быть ≥ 1 (присваивает checklist_store)")
        sql = "INSERT INTO checklist_versions(version, checklist_id, name, data, created_at) VALUES (?, ?, ?, ?, ?)"
        if replace:
            sql += """ ON CONFLICT(version) DO UPDATE SET checklist_id = excluded.checklist_id, name = excluded.name,
                       data = excluded.data"""
        with self.connect() as c:
            c.execute(sql, (cl.version, cl.checklist_id, cl.name, _dump(cl), cl.created_at or now_iso()))
        return cl

    def get_checklist_version(self, version: int) -> Optional[ChecklistVersion]:
        with self.connect() as c:
            row = c.execute("SELECT data FROM checklist_versions WHERE version = ?", (int(version),)).fetchone()
        return ChecklistVersion.model_validate_json(row["data"]) if row else None

    def list_checklist_versions(self) -> list[ChecklistVersion]:
        with self.connect() as c:
            rows = c.execute("SELECT data FROM checklist_versions ORDER BY version").fetchall()
        return [ChecklistVersion.model_validate_json(r["data"]) for r in rows]

    def max_checklist_version(self) -> int:
        with self.connect() as c:
            row = c.execute("SELECT MAX(version) AS v FROM checklist_versions").fetchone()
        return int(row["v"] or 0)

    def set_active_version(self, version: int) -> None:
        self.set_setting("active_checklist_version", int(version))

    def get_active_version(self) -> Optional[int]:
        v = self.get_setting("active_checklist_version")
        return int(v) if v is not None else None

    # ---------------------------------------------------------------------------------- evaluations
    def save_eval(self, ev: Union[CriterionEval, dict]) -> CriterionEval:
        """Insert or replace the evaluation for (conv_id, criterion_hash, prompt_version, mode).

        Replacement gets the new internal row id so timestamps tied to one second still sort by write order.
        """
        ev = ev if isinstance(ev, CriterionEval) else CriterionEval.model_validate(ev)
        if not ev.criterion_hash:
            raise ValueError("criterion_hash обязателен для сохранения оценки")
        if not ev.created_at:
            ev = ev.model_copy(update={"created_at": now_iso()})
        with self.connect() as c:
            c.execute("""INSERT INTO evaluations(conv_id, criterion_id, criterion_hash, prompt_version, mode, answer,
                           needs_review, model, cost_kzt, data, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(conv_id, criterion_hash, prompt_version, mode) DO UPDATE SET
                           id = excluded.id, criterion_id = excluded.criterion_id, answer = excluded.answer,
                           needs_review = excluded.needs_review, model = excluded.model, cost_kzt = excluded.cost_kzt,
                           data = excluded.data, created_at = excluded.created_at""",
                      (ev.conv_id, ev.criterion_id, ev.criterion_hash, ev.prompt_version, ev.mode,
                       json.dumps(ev.answer), int(ev.needs_review), ev.model, ev.cost_kzt, _dump(ev), ev.created_at))
        return ev

    def save_evals(self, evals: Iterable[Union[CriterionEval, dict]]) -> int:
        n = 0
        for ev in evals:
            self.save_eval(ev)
            n += 1
        return n

    def max_eval_id(self) -> int:
        """Highest evaluations row id (0 if empty). Replacement of a row gives it a NEW id (see save_eval), so
        `id > max_eval_id()-before-a-run` means "written by that run" -- exact, unlike one-second created_at."""
        with self.connect() as c:
            return int(c.execute("SELECT COALESCE(MAX(id), 0) FROM evaluations").fetchone()[0])

    def eval_keys_since(self, conv_id: str, after_id: int) -> set[tuple[str, str, str]]:
        """(criterion_hash, prompt_version, mode) of the conversation's evaluations written after row id `after_id`."""
        with self.connect() as c:
            rows = c.execute("""SELECT criterion_hash, prompt_version, mode FROM evaluations
                                WHERE conv_id = ? AND id > ?""", (conv_id, int(after_id))).fetchall()
        return {(r[0], r[1], r[2]) for r in rows}

    def get_eval(self, conv_id: str, criterion_hash: str, prompt_version: str, mode: str) -> Optional[CriterionEval]:
        with self.connect() as c:
            row = c.execute("""SELECT data FROM evaluations WHERE conv_id = ? AND criterion_hash = ?
                               AND prompt_version = ? AND mode = ?""",
                            (conv_id, criterion_hash, prompt_version, mode)).fetchone()
        return CriterionEval.model_validate_json(row["data"]) if row else None

    def list_evals(self, conv_id: Optional[str] = None, criterion_hashes: Optional[Iterable[str]] = None,
                   prompt_version: Optional[str] = None, mode: Optional[str] = None) -> list[CriterionEval]:
        """Evaluations ordered oldest → newest (so 'last wins' gives the most recent)."""
        sql, args = "SELECT data FROM evaluations WHERE 1=1", []
        if conv_id:
            sql += " AND conv_id = ?"
            args.append(conv_id)
        if prompt_version:
            sql += " AND prompt_version = ?"
            args.append(prompt_version)
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        if criterion_hashes is not None:
            hashes = list(criterion_hashes)
            if not hashes:
                return []
            sql += f" AND criterion_hash IN ({','.join('?' * len(hashes))})"
            args.extend(hashes)
        with self.connect() as c:
            rows = c.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [CriterionEval.model_validate_json(r["data"]) for r in rows]

    def latest_evals(self, conv_id: str, criterion_hashes: Iterable[str], prompt_version: Optional[str] = None,
                     mode: Optional[str] = None) -> dict[str, CriterionEval]:
        """{criterion_hash: most recent evaluation} for one conversation."""
        out: dict[str, CriterionEval] = {}
        for ev in self.list_evals(conv_id, criterion_hashes, prompt_version, mode):
            out[ev.criterion_hash] = ev
        return out

    def existing_eval_keys(self, conv_ids: Optional[Iterable[str]] = None, prompt_version: Optional[str] = None,
                           mode: Optional[str] = None) -> set[tuple[str, str]]:
        """{(conv_id, criterion_hash)} already evaluated — for the run planner."""
        sql, args = "SELECT conv_id, criterion_hash FROM evaluations WHERE answer IS NOT NULL AND answer != 'null'", []
        if prompt_version:
            sql += " AND prompt_version = ?"
            args.append(prompt_version)
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        if conv_ids is not None:
            ids = list(conv_ids)
            if not ids:
                return set()
            sql += f" AND conv_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        with self.connect() as c:
            return {(r["conv_id"], r["criterion_hash"]) for r in c.execute(sql, args).fetchall()}

    # ------------------------------------------------------------------------------ appeals/decisions
    def create_appeal(self, appeal: Union[Appeal, dict]) -> Appeal:
        appeal = appeal if isinstance(appeal, Appeal) else Appeal.model_validate(appeal)
        if not appeal.created_at:
            appeal = appeal.model_copy(update={"created_at": now_iso()})
        with self.connect() as c:
            c.execute("INSERT INTO appeals(id, conv_id, criterion_id, status, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (appeal.id, appeal.conv_id, appeal.criterion_id, appeal.status, _dump(appeal), appeal.created_at))
        return appeal

    def get_appeal(self, appeal_id: str) -> Optional[Appeal]:
        with self.connect() as c:
            row = c.execute("SELECT data FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
        return Appeal.model_validate_json(row["data"]) if row else None

    def list_appeals(self, status: Optional[str] = None, conv_id: Optional[str] = None) -> list[Appeal]:
        sql, args = "SELECT data FROM appeals WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if conv_id:
            sql += " AND conv_id = ?"
            args.append(conv_id)
        with self.connect() as c:
            rows = c.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [Appeal.model_validate_json(r["data"]) for r in rows]

    def update_appeal(self, appeal_id: str, **fields: Any) -> Appeal:
        """Merge fields into the stored appeal (re-validated: an invalid status raises ValueError)."""
        appeal = self.get_appeal(appeal_id)
        if appeal is None:
            raise KeyError(appeal_id)
        appeal = Appeal.model_validate({**appeal.model_dump(), **fields})
        with self.connect() as c:
            c.execute("UPDATE appeals SET status = ?, data = ? WHERE id = ?", (appeal.status, _dump(appeal), appeal_id))
        return appeal

    def set_appeal_status(self, appeal_id: str, status: str) -> Appeal:
        return self.update_appeal(appeal_id, status=status)

    def add_decision(self, decision: Union[Decision, dict]) -> Decision:
        decision = decision if isinstance(decision, Decision) else Decision.model_validate(decision)
        if not decision.created_at:
            decision = decision.model_copy(update={"created_at": now_iso()})
        with self.connect() as c:
            c.execute("INSERT INTO decisions(id, appeal_id, conv_id, criterion_id, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (decision.id, decision.appeal_id, decision.conv_id, decision.criterion_id, _dump(decision),
                       decision.created_at))
        return decision

    def list_decisions(self, conv_id: Optional[str] = None, appeal_id: Optional[str] = None) -> list[Decision]:
        sql, args = "SELECT data FROM decisions WHERE 1=1", []
        if conv_id:
            sql += " AND conv_id = ?"
            args.append(conv_id)
        if appeal_id:
            sql += " AND appeal_id = ?"
            args.append(appeal_id)
        with self.connect() as c:
            rows = c.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [Decision.model_validate_json(r["data"]) for r in rows]

    # ---------------------------------------------------------------------------------------- audit
    def add_audit(self, entity: str, entity_id: str, event: str, payload: Optional[dict] = None,
                  conv_id: Optional[str] = None) -> AuditEvent:
        ts = now_iso()
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self.connect() as c:
            cur = c.execute("INSERT INTO audit_log(entity, entity_id, conv_id, event, payload, ts) VALUES (?, ?, ?, ?, ?, ?)",
                            (entity, entity_id, conv_id, event, body, ts))
            new_id = cur.lastrowid
        return AuditEvent(id=new_id, entity=entity, entity_id=entity_id, event=event, payload=payload or {}, ts=ts,
                          conv_id=conv_id)

    def list_audit(self, conv_id: Optional[str] = None, entity: Optional[str] = None,
                   entity_id: Optional[str] = None, limit: Optional[int] = None) -> list[AuditEvent]:
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        for col, val in (("conv_id", conv_id), ("entity", entity), ("entity_id", entity_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.connect() as c:
            rows = c.execute(sql, args).fetchall()
        return [AuditEvent(id=r["id"], entity=r["entity"], entity_id=r["entity_id"], event=r["event"],
                           payload=json.loads(r["payload"]), ts=r["ts"], conv_id=r["conv_id"]) for r in rows]

    # ------------------------------------------------------------------------------------ llm calls
    def log_llm_call(self, call: Union[LLMCall, dict]) -> int:
        call = call if isinstance(call, LLMCall) else LLMCall.model_validate(call)
        if not call.ts:
            call = call.model_copy(update={"ts": now_iso()})
        with self.connect() as c:
            cur = c.execute("""INSERT INTO llm_calls(ts, provider, model, purpose, conv_id, tokens_in, tokens_out, cost_usd,
                                 cost_kzt, latency_ms, cache_hit, ok, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (call.ts, call.provider, call.model, call.purpose, call.conv_id, call.tokens_in,
                             call.tokens_out, call.cost_usd, call.cost_kzt, call.latency_ms, int(call.cache_hit),
                             int(call.ok), _dump(call)))
            return int(cur.lastrowid)

    def llm_stats(self, since: Optional[str] = None) -> dict[str, Any]:
        sql, args = """SELECT COUNT(*) AS calls, SUM(cache_hit) AS cache_hits, SUM(1 - ok) AS errors,
                       COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(cost_kzt), 0) AS cost_kzt,
                       AVG(CASE WHEN cache_hit = 0 THEN latency_ms END) AS avg_latency_ms FROM llm_calls""", []
        if since:
            sql += " WHERE ts >= ?"
            args.append(since)
        with self.connect() as c:
            r = dict(c.execute(sql, args).fetchone())
        return {"calls": r["calls"] or 0, "cache_hits": r["cache_hits"] or 0, "errors": r["errors"] or 0,
                "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "cost_usd": round(r["cost_usd"], 6),
                "cost_kzt": round(r["cost_kzt"], 4), "avg_latency_ms": round(r["avg_latency_ms"] or 0)}

    # ------------------------------------------------------------------------------------- dossiers
    def put_dossier(self, appeal_id: str, dossier: dict) -> dict:
        ts = now_iso()
        data = {**dossier, "appeal_id": appeal_id, "updated_at": ts}
        with self.connect() as c:
            c.execute("""INSERT INTO dossiers(appeal_id, data, updated_at) VALUES (?, ?, ?)
                         ON CONFLICT(appeal_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
                      (appeal_id, _dump(data), ts))
        return data

    def get_dossier(self, appeal_id: str) -> Optional[dict]:
        with self.connect() as c:
            row = c.execute("SELECT data FROM dossiers WHERE appeal_id = ?", (appeal_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ----------------------------------------------------------------------------------------- runs
    def save_run(self, run_id: str, status: str, data: dict, version: Optional[int] = None) -> None:
        ts = now_iso()
        with self.connect() as c:
            c.execute("""INSERT INTO runs(id, version, status, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)
                         ON CONFLICT(id) DO UPDATE SET version = COALESCE(excluded.version, runs.version),
                           status = excluded.status, data = excluded.data, updated_at = excluded.updated_at""",
                      (run_id, version, status, _dump(data), ts, ts))

    def get_run(self, run_id: str) -> Optional[dict]:
        with self.connect() as c:
            row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "run_id": row["id"], "status": row["status"], "version": row["version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),)).fetchall()
        return [r for r in (self.get_run(row["id"]) for row in rows) if r]


_DEFAULT: Optional[Database] = None
_LOCK = threading.Lock()


def get_db(path: Union[str, Path, None] = None) -> Database:
    """Shared Database for the default path (env CQA_DB); a new one for an explicit path."""
    global _DEFAULT
    if path is not None:
        return Database(path)
    with _LOCK:
        if _DEFAULT is None or _DEFAULT.path != db_path():
            _DEFAULT = Database()
        return _DEFAULT
