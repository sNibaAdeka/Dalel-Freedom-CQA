"""cqa.llm: providers, cache key (run/temperature/model), usage & cost, retries, offline mode. No network."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa import llm  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_CACHE", str(tmp_path / "cache"))
    for k in ("CQA_NO_CACHE", "CQA_OFFLINE", "CQA_MODEL", "CQA_ARBITER_MODEL", "CQA_PROFILE", "CQA_USD_KZT"):
        monkeypatch.delenv(k, raising=False)
    llm.set_mock(None)
    yield
    llm.set_mock(None)


def fake_dispatch(calls, text='{"ok": true}', usage=None):
    def _d(provider, system, user, model, temperature, run, max_tokens, timeout, effort):
        calls.append({"provider": provider, "model": model, "temperature": temperature, "run": run, "user": user})
        return (text(calls) if callable(text) else text), dict(usage or {"tokens_in": 1000, "tokens_out": 200, "tokens_cached": 400, "cost_usd": 0.001})
    return _d


def test_parse_model_splits_on_first_colon_only():
    assert llm.parse_model("ollama:qwen2.5:7b-instruct") == ("ollama", "qwen2.5:7b-instruct")
    assert llm.parse_model("openai:deepseek-chat") == ("openai", "deepseek-chat")
    assert llm.parse_model("claude_cli:claude-opus-5-5") == ("claude_cli", "claude-opus-5-5")
    assert llm.parse_model("gpt-x", default_provider="openai") == ("openai", "gpt-x")


def test_extract_json_variants():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Вот ответ: {"a": {"b": "}"}} спасибо') == {"a": {"b": "}"}}
    assert llm.extract_json('<think>hmm {"x": 0}</think>{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        llm.extract_json("нет json")


def test_cache_key_contains_run_temperature_and_model(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_dispatch", fake_dispatch(calls))
    a, u1 = llm.complete_json("sys", "user", "openai:deepseek-chat", 0.5, run=0)
    b, u2 = llm.complete_json("sys", "user", "openai:deepseek-chat", 0.5, run=0)
    assert a == b == {"ok": True} and len(calls) == 1 and u2["cache_hit"] and not u1["cache_hit"]
    llm.complete_json("sys", "user", "openai:deepseek-chat", 0.5, run=1)      # another vote -> real call
    llm.complete_json("sys", "user", "openai:deepseek-chat", 0.9, run=0)      # another temperature -> real call
    llm.complete_json("sys", "user", "openai:deepseek-reasoner", 0.5, run=0)  # another model -> real call
    assert len(calls) == 4
    llm.complete_json("sys", "user", "openai:deepseek-chat", 0.5, run=0, no_cache=True)
    assert len(calls) == 5


def test_usage_cost_and_listener(monkeypatch):
    calls, seen = [], []
    monkeypatch.setattr(llm, "_dispatch", fake_dispatch(calls))
    monkeypatch.setenv("CQA_USD_KZT", "500")
    llm.add_listener(seen.append)
    try:
        _, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0)
        _, hit = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0)
    finally:
        llm.remove_listener(seen.append)
    assert u["cost_usd"] == pytest.approx(0.001) and u["cost_kzt"] == pytest.approx(0.5)
    assert u["tokens_in"] == 1000 and u["tokens_out"] == 200 and u["latency_ms"] >= 0
    assert hit["cache_hit"] and hit["cost_usd"] == 0 and hit["cost_usd_full"] == pytest.approx(0.001)
    assert [r["cache_hit"] for r in seen] == [False, True]


def test_price_table_for_deepseek():
    usd = llm.price_usd("deepseek-chat", tokens_in=1_000_000, tokens_out=1_000_000, tokens_cached=500_000)
    row = llm.config()["pricing_usd_per_mtok"]["deepseek-chat"]
    assert usd == pytest.approx(0.5 * row["in"] + 0.5 * row["in_cached"] + row["out"])
    assert llm.price_usd("unknown-model", 10, 10) == 0.0


def test_offline_mode_only_reads_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_dispatch", fake_dispatch(calls))
    llm.complete_json("s", "u", "openai:m", 0.0)
    monkeypatch.setenv("CQA_OFFLINE", "1")
    data, u = llm.complete_json("s", "u", "openai:m", 0.0)
    assert u["cache_hit"] and len(calls) == 1
    with pytest.raises(llm.OfflineCacheMiss):
        llm.complete_json("s", "other", "openai:m", 0.0)


def test_retry_on_invalid_json(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_dispatch", fake_dispatch(calls, text=lambda c: "не json" if len(c) == 1 else '{"fixed": 1}'))
    data, u = llm.complete_json("s", "u", "openai:m", 0.0, retries=2)
    assert data == {"fixed": 1} and u["attempts"] == 2
    assert "ТОЛЬКО" in calls[1]["user"]


def test_failure_raises_llm_error(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_dispatch", fake_dispatch(calls, text="never json"))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(llm.LLMError):
        llm.complete_json("s", "u", "openai:m", 0.0, retries=1)
    assert len(calls) == 2


def test_mock_provider_uses_handler():
    llm.set_mock(lambda system, user, model, temperature, run: {"run": run, "t": temperature})
    data, u = llm.complete_json("s", "u", "mock:mock", 0.7, run=2)
    assert data == {"run": 2, "t": 0.7} and u["provider"] == "mock" and u["cost_usd"] == 0


def test_claude_cli_command_and_parsing(monkeypatch):
    seen = {}

    def fake_run(cmd, input, capture_output, text, timeout, cwd):
        seen.update(cmd=cmd, input=input, cwd=cwd, cwd_files=list(Path(cwd).iterdir()))
        out = {"type": "result", "subtype": "success", "is_error": False, "result": '{"answer": "yes"}', "total_cost_usd": 0.0123,
               "duration_ms": 900, "modelUsage": {"synthetic-resolved-model":{}},
               "usage": {"input_tokens": 10, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 50, "output_tokens": 40}}
        return SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    data, u = llm.complete_json("SYSTEM PROMPT", "USER PROMPT", "claude_cli:claude-haiku-5-5", 0.0)
    cmd = seen["cmd"]
    assert data == {"answer": "yes"}
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM PROMPT" and cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--output-format") + 1] == "json" and cmd[cmd.index("--tools") + 1] == ""
    assert "-p" in cmd and "--no-session-persistence" in cmd
    assert seen["input"] == "USER PROMPT" and seen["cwd_files"] == []  # prompt via stdin, empty cwd (no CLAUDE.md)
    assert u["cost_usd"] == pytest.approx(0.0123) and u["tokens_in"] == 660 and u["tokens_out"] == 40
    assert u['resolved_models'] == ['synthetic-resolved-model']
    assert u['cost_basis'] == 'cli_reported_estimate'


def test_claude_cli_error_is_reported(monkeypatch):
    def fake_run(cmd, input, capture_output, text, timeout, cwd):
        return SimpleNamespace(stdout=json.dumps({"is_error": True, "subtype": "error", "result": "rate limited"}), stderr="", returncode=1)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(llm.LLMError, match="rate limited"):
        llm.complete_json("s", "u", "claude_cli:claude-haiku-5-5", 0.0, retries=0)


def test_claude_cli_timeout_retries(monkeypatch):
    n = {"k": 0}

    def fake_run(cmd, input, capture_output, text, timeout, cwd):
        n["k"] += 1
        if n["k"] == 1:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return SimpleNamespace(stdout=json.dumps({"is_error": False, "subtype": "success", "result": "{\"a\":1}", "total_cost_usd": 0, "usage": {}}), stderr="", returncode=0)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    data, u = llm.complete_json("s", "u", "claude_cli:claude-haiku-5-5", 0.0, retries=1)
    assert data == {"a": 1} and n["k"] == 2


class _FakeCompletions:
    def __init__(self, store, fail_json_mode=False):
        self.store, self.fail = store, fail_json_mode

    def create(self, **kw):
        self.store.append(kw)
        if self.fail and "response_format" in kw:
            raise ValueError("response_format json_object is not supported")
        usage = SimpleNamespace(prompt_tokens=2000, completion_tokens=300, model_extra={"prompt_cache_hit_tokens": 1500}, prompt_tokens_details=None)
        msg = SimpleNamespace(content='{"answer": "no"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def test_openai_compatible_provider_deepseek(monkeypatch):
    sent = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(sent)))
    monkeypatch.setattr(llm, "_openai_client", lambda base_url, api_key, timeout: client)
    monkeypatch.setenv("CQA_API_KEY", "test-key")
    data, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.3)
    assert data == {"answer": "no"} and sent[0]["temperature"] == 0.3 and sent[0]["response_format"] == {"type": "json_object"}
    row = llm.config()["pricing_usd_per_mtok"]["deepseek-chat"]
    assert u["tokens_cached"] == 1500
    assert u["cost_usd"] == pytest.approx((500 * row["in"] + 1500 * row["in_cached"] + 300 * row["out"]) / 1e6)
    llm.complete_json("s", "u2", "openai:deepseek-reasoner", 0.3)
    assert "temperature" not in sent[-1]  # reasoner: no sampling params


def test_openai_provider_falls_back_without_json_mode(monkeypatch):
    sent = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(sent, fail_json_mode=True)))
    monkeypatch.setattr(llm, "_openai_client", lambda base_url, api_key, timeout: client)
    monkeypatch.setenv("CQA_API_KEY", "test-key")
    data, _ = llm.complete_json("s", "u", "openai:some-model", 0.0)
    assert data == {"answer": "no"} and "response_format" not in sent[-1]


def test_openai_requires_key(monkeypatch):
    monkeypatch.setenv("CQA_API_KEY", "")
    monkeypatch.setattr(llm, "_env_loaded", True)
    with pytest.raises(llm.LLMError, match="no API key"):
        llm.complete_json("s", "u", "openai:deepseek-chat", 0.0)


def test_preset_votes_temperatures_and_env_override(monkeypatch):
    p = llm.preset("accurate", "mock")
    assert p["votes"] == 3 and len(p["temperatures"]) == 3 and p["arbiter"]["model"] == "mock:mock-arbiter"
    monkeypatch.setenv("CQA_MODEL", "openai:deepseek-chat")
    assert llm.preset("fast", "mock")["model"] == "openai:deepseek-chat"


def test_ollama_native_api_with_context_window(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen.update(url=url, body=json)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": '{"answer": "yes"}'}, "prompt_eval_count": 9000, "eval_count": 120})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("CQA_OLLAMA_URL", "http://10.0.0.7:11434/v1")
    data, u = llm.complete_json("s", "u", "ollama:qwen2.5:7b-instruct", 0.2)
    assert data == {"answer": "yes"} and seen["url"] == "http://10.0.0.7:11434/api/chat"
    assert seen["body"]["options"]["num_ctx"] >= 16384 and seen["body"]["options"]["temperature"] == 0.2 and seen["body"]["format"] == "json"
    assert seen["body"]["model"] == "qwen2.5:7b-instruct" and u["tokens_in"] == 9000 and u["cost_usd"] == 0
    assert seen["body"]["think"] is False  # hybrid reasoning models must not spend the output budget on hidden thinking
