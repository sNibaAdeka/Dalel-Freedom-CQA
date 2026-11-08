"""Strict review PROD-03: a clean start following README must send the cloud/cascade/routing models
(OpenRouter ids) to the OpenRouter endpoint, never google/gemini-4-argon to api.deepseek.com.

No network: the OpenAI client class is replaced by an intercept that records (base_url, model) and raises.
No real key is read: the repo .env is never loaded (a clean checkout has none).
"""
import json
import re
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "https://openrouter.ai/api/v1"


def readme_env_block() -> dict:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```dotenv\n(.*?)```", text, re.S)
    assert block, "README must keep a dotenv example"
    return dict(line.split("=", 1) for line in block.group(1).splitlines() if "=" in line)


def test_openai_provider_default_endpoint_is_openrouter():
    config = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
    assert config["providers"]["openai"]["base_url_default"] == OPENROUTER


def test_readme_endpoint_matches_config_default():
    config = json.loads((ROOT / "config/models.json").read_text(encoding="utf-8"))
    env = readme_env_block()
    assert env["CQA_BASE_URL"] == config["providers"]["openai"]["base_url_default"]
    assert "deepseek.com" not in env["CQA_BASE_URL"]


class _Stop(Exception):
    pass


@pytest.fixture
def intercepted(monkeypatch, tmp_path):
    import openai
    from cqa import llm

    captured = []

    class FakeOpenAI:
        def __init__(self, base_url=None, api_key=None, timeout=None, max_retries=None):
            def create(**kw):
                captured.append((str(base_url), kw.get("model")))
                raise _Stop("intercepted; no network")
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)
    monkeypatch.setattr(llm, "_env_loaded", True)  # clean checkout: no .env
    for name in ("CQA_BASE_URL", "CQA_MODEL", "CQA_PROFILE", "CQA_ARBITER_MODEL", "CQA_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CQA_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "none")
    monkeypatch.setenv("CQA_NO_CACHE", "1")
    monkeypatch.setenv("CQA_CACHE", str(tmp_path / "cache"))
    return llm, captured


def _pairs(llm, captured):
    out = {}
    for profile in ("cloud", "cascade", "routing"):
        preset = llm.preset("fast", profile)
        specs = [preset["model"]] + ([preset["escalate"]["model"]] if preset.get("escalate") else [])
        for spec in specs:
            captured.clear()
            with pytest.raises(Exception):
                llm.complete_json("system", "user", model=spec, temperature=0.0, retries=0)
            out[spec] = set(captured)
    return out


@pytest.mark.parametrize("readme_env", [False, True])
def test_clean_start_sends_openrouter_models_to_openrouter(intercepted, monkeypatch, readme_env):
    llm, captured = intercepted
    if readme_env:
        for name, value in readme_env_block().items():
            if name in ("CQA_PROFILE", "CQA_BASE_URL"):
                monkeypatch.setenv(name, value)
    pairs = _pairs(llm, captured)
    assert pairs["openai:google/gemini-4-argon"] == {(OPENROUTER, "google/gemini-4-argon")}
    assert pairs["openai:anthropic/claude-haiku-5.5"] == {(OPENROUTER, "anthropic/claude-haiku-5.5")}
    assert all(url == OPENROUTER for found in pairs.values() for url, _ in found)
    assert llm.health()["base_url"] == OPENROUTER


def test_explicit_deepseek_endpoint_still_honoured(intercepted, monkeypatch):
    llm, captured = intercepted
    monkeypatch.setenv("CQA_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("CQA_MODEL", "openai:deepseek-chat")
    with pytest.raises(Exception):
        llm.complete_json("system", "user", temperature=0.0, retries=0)
    assert set(captured) == {("https://api.deepseek.com", "deepseek-chat")}
