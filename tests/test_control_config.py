"""Control-day config (critic-final m6, M6): provider timeout and default run workers."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_openai_provider_timeout_is_150_seconds():
    cfg = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    assert cfg["providers"]["openai"]["timeout_s"] == 150
    doc = cfg["profiles"]["cloud"]["_doc"]
    assert "3 votes" not in doc and "no arbiter" in doc


def test_default_run_workers_is_8(tmp_path, monkeypatch):
    from api.state import AppState
    monkeypatch.delenv("CQA_RUN_WORKERS", raising=False)
    st = AppState(db_path=tmp_path / "w.sqlite", evaluate=None, llm_call=None, autoload=False, calibrate_audio=False)
    assert st.workers == 8
    monkeypatch.setenv("CQA_RUN_WORKERS", "10")
    assert AppState(db_path=tmp_path / "w2.sqlite", evaluate=None, llm_call=None, autoload=False,
                    calibrate_audio=False).workers == 10
