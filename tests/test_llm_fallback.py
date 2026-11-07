"""cqa.llm fallback chain: 402 -> fallback at once, 429/5xx -> backoff (~60 s budget) then fallback, timeouts ->
retries then fallback; usage marks the model that really answered. Mock provider only, no network, no real sleep."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cqa import llm  # noqa: E402


class StatusError(Exception):
    """Shape of openai/anthropic/httpx status errors: .status_code and 'Error code: N' in the text."""

    def __init__(self, status: int, text: str = ""):
        super().__init__(f"Error code: {status} - {text}")
        self.status_code = status


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CQA_CACHE", str(tmp_path / "cache"))
    for k in ("CQA_NO_CACHE", "CQA_OFFLINE", "CQA_MODEL", "CQA_PROFILE", "CQA_FALLBACK_MODEL", "CQA_BACKOFF_TOTAL_S"):
        monkeypatch.delenv(k, raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    llm.set_mock(lambda system, user, model, temperature, run: {"answered_by": model})
    yield sleeps
    llm.set_mock(None)


def primary_fails(monkeypatch, errors, calls):
    """openai:* raises the queued errors (then answers); mock:* answers through the mock handler."""
    real = llm._dispatch
    queue = list(errors)

    def _d(provider, system, user, model, temperature, run, max_tokens, timeout, effort):
        calls.append(f"{provider}:{model}")
        if provider == "openai":
            if queue:
                raise queue.pop(0)
            return '{"answered_by": "primary"}', {"tokens_in": 1, "tokens_out": 1, "tokens_cached": 0, "cost_usd": 0.0}
        return real(provider, system, user, model, temperature, run, max_tokens, timeout, effort)

    monkeypatch.setattr(llm, "_dispatch", _d)


def test_error_kinds():
    assert llm.error_kind(StatusError(402, "Insufficient Balance")) == "credits"
    assert llm.error_kind(RuntimeError("This request requires more credits; exceed your available credits")) == "credits"
    assert llm.error_kind(StatusError(429, "insufficient_quota")) == "credits"       # OpenAI quota is a credits problem
    assert llm.error_kind(StatusError(429, "Rate limit reached")) == "transient"
    assert llm.error_kind(StatusError(503, "Service Unavailable")) == "transient"
    assert llm.error_kind(RuntimeError("Error code: 502 - bad gateway")) == "transient"
    assert llm.error_kind(subprocess.TimeoutExpired(["claude"], 5)) == "timeout"
    assert llm.error_kind(TimeoutError("read timed out")) == "timeout"
    assert llm.error_kind(StatusError(400, "bad request")) == "other"


def test_default_fallback_is_claude_sonnet_and_never_same_provider_or_mock(monkeypatch):
    assert llm.fallback_model("openai:deepseek-chat") == "claude_cli:sonnet"
    assert llm.fallback_model("claude_cli:haiku") is None            # same provider: nothing to switch to
    assert llm.fallback_model("mock:mock") is None
    assert llm.fallback_model("ollama:qwen3.5:9b-q4_K_M") is None  # local model: dialogues never silently go to the cloud
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "none")
    assert llm.fallback_model("openai:deepseek-chat") is None


def test_402_switches_to_fallback_at_once_and_marks_usage(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls, seen = [], []
    llm.add_listener(seen.append)
    try:
        primary_fails(monkeypatch, [StatusError(402, "Insufficient Balance")], calls)
        data, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True)
    finally:
        llm.remove_listener(seen.append)
    assert calls == ["openai:deepseek-chat", "mock:backup"] and _isolated == []    # no retry, no sleep
    assert data == {"answered_by": "backup"}
    assert u["provider"] == "mock" and u["model"] == "backup" and u["fallback"] is True
    assert u["fallback_from"] == "openai:deepseek-chat" and u["fallback_reason"].startswith("credits")
    assert seen[-1]["fallback"] is True and seen[-1]["provider"] == "mock"        # listeners (DB log, UI) see it too


def test_429_backs_off_exponentially_then_falls_back(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls = []
    primary_fails(monkeypatch, [StatusError(429, "Rate limit reached")] * 50, calls)
    data, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True)
    assert _isolated == [1, 2, 4, 8, 16, 29] and sum(_isolated) == 60             # ~60 s in total
    assert calls.count("openai:deepseek-chat") == 7 and calls[-1] == "mock:backup"
    assert u["fallback"] is True and u["fallback_reason"].startswith("transient")


def test_transient_error_recovers_within_budget_without_fallback(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls = []
    primary_fails(monkeypatch, [StatusError(503, "overloaded"), StatusError(429, "slow down")], calls)
    data, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True, retries=0)
    assert data == {"answered_by": "primary"} and u["attempts"] == 3 and "fallback" not in u
    assert _isolated == [1, 2] and calls == ["openai:deepseek-chat"] * 3


def test_timeouts_retry_once_then_fall_back(monkeypatch, _isolated):
    # a hung provider costs at most 2 x timeout_s (2 x 150 s = 300 s) per call before the fallback, not 3 x
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls = []
    primary_fails(monkeypatch, [subprocess.TimeoutExpired(["x"], 1)] * 5, calls)
    data, u = llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True, retries=2)
    assert calls == ["openai:deepseek-chat"] * 2 + ["mock:backup"]
    assert u["fallback"] is True and u["fallback_reason"].startswith("timeout")


def test_no_fallback_configured_raises_after_backoff(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "none")
    monkeypatch.setenv("CQA_BACKOFF_TOTAL_S", "5")
    calls = []
    primary_fails(monkeypatch, [StatusError(429, "Rate limit")] * 50, calls)
    with pytest.raises(llm.LLMError, match="429"):
        llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True)
    assert sum(_isolated) == 5 and set(calls) == {"openai:deepseek-chat"}


def test_bad_request_is_not_sent_to_fallback(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls = []
    primary_fails(monkeypatch, [StatusError(400, "bad request")] * 5, calls)
    with pytest.raises(llm.LLMError):
        llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True, retries=1)
    assert calls == ["openai:deepseek-chat"] * 2


def test_failing_fallback_reports_both(monkeypatch, _isolated):
    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    llm.set_mock(None)                                                              # the fallback fails as well
    calls = []
    primary_fails(monkeypatch, [StatusError(402, "no credits")], calls)
    with pytest.raises(llm.LLMError, match="fallback mock:backup failed too"):
        llm.complete_json("s", "u", "openai:deepseek-chat", 0.0, no_cache=True, retries=0)


def test_evaluator_shows_fallback_in_model_label_and_trace(monkeypatch, _isolated):
    from cqa import evaluator as E
    from tests.test_evaluator import CHECKLIST, CONV, Script

    monkeypatch.setenv("CQA_FALLBACK_MODEL", "mock:backup")
    calls = []
    primary_fails(monkeypatch, [StatusError(402, "Insufficient Balance")] * 100, calls)
    llm.set_mock(Script())
    preset = {"model": "openai:deepseek-chat", "votes": 1, "temperatures": [0.0]}
    out = E.evaluate(CONV, CHECKLIST, "fast", preset=preset, as_models=False, no_cache=True)
    assert all(r["answer"] is not None for r in out)
    assert all("фолбэк mock:backup" in r["model"] and r["raw"]["fallback"] == ["mock:backup"] for r in out)
    acc = E.evaluate(CONV, CHECKLIST, "accurate", as_models=False, no_cache=True,
                     preset={"model": "openai:deepseek-chat", "votes": 3, "temperatures": [0.0, 0.5, 0.9]})
    assert all(r["raw"]["fallback"] == ["mock:backup"] and "фолбэк" in r["model"] for r in acc)
