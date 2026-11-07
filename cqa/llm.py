"""LLM provider layer: one interface for every backend, JSON out, usage/cost record, disk cache.

    data, usage = complete_json(system, user, model="openai:deepseek-chat", temperature=0.0, run=0)

Model ids are "provider:model" (split on the first colon only: "ollama:qwen2.5:7b-instruct").
Providers
  mock        tests / offline UI; answers come from a handler registered with set_mock()
  claude_cli  `claude -p --output-format json` subprocess from an empty temp dir with --system-prompt and no tools
              (team subscription; cost = total_cost_usd reported by the CLI; temperature is NOT controllable there)
  openai      any OpenAI-compatible endpoint: base URL CQA_BASE_URL (default https://api.deepseek.com), key CQA_API_KEY
  ollama      local model through Ollama's OpenAI-compatible /v1 (CQA_OLLAMA_URL, e.g. http://<rtx-ip>:11434/v1)
  anthropic   Anthropic SDK (ANTHROPIC_API_KEY)

Cache: key = sha256(provider, model, system, user, temperature, run). The run number is part of the key, so three votes
at the same temperature are three real calls, not one cached answer. Cache is a convenience for re-running DEV only:
pass no_cache=True (or CQA_NO_CACHE=1) to skip it. CQA_OFFLINE=1 = cache only (raises OfflineCacheMiss on a miss).

Every call (cached or not) produces a usage record and is sent to listeners registered with add_listener():
  {provider, model, run, temperature, tokens_in, tokens_out, tokens_cached, cost_usd, cost_kzt, cost_usd_full,
   latency_ms, cache_hit, ts}
cost_usd is what this call actually cost (0 on a cache hit); cost_usd_full is what a live call costs (for "price per
evaluation" figures).

Provider failures (fallback chain, config/models.json "fallback", env CQA_FALLBACK_MODEL / CQA_BACKOFF_TOTAL_S):
  402 / no credits (insufficient balance/quota)  -> switch to the fallback model at once (default claude_cli:sonnet);
  429 / 5xx / rate limit / connection error      -> exponential backoff (1, 2, 4, ... s, ~60 s in total), then fallback;
  timeout                                        -> `retries` extra attempts with backoff, then fallback.
No fallback for the mock provider, when the fallback has the same provider as the failed model, or when it is disabled
(CQA_FALLBACK_MODEL=none). The usage of an answer given by the fallback carries fallback=True, fallback_from (the
failed "provider:model") and fallback_reason; provider/model are the ones that really answered.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "models.json"
PROVIDERS = ("mock", "claude_cli", "openai", "ollama", "anthropic")


class LLMError(RuntimeError):
    """Provider call failed after retries (network, bad JSON, CLI error)."""


class OfflineCacheMiss(LLMError):
    """CQA_OFFLINE=1 and the prompt is not in the cache."""


# ---------------------------------------------------------------- env / config
_env_loaded = False


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader: sets variables that are not already in the environment. Never prints values."""
    global _env_loaded
    p = path or ROOT / ".env"
    _env_loaded = True
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def env(name: str | None, default: str | None = None) -> str | None:
    if not name:
        return default
    if not _env_loaded:
        load_dotenv()
    return os.environ.get(name) or default


_cfg_cache: dict[str, Any] = {}


def config() -> dict:
    """config/models.json (re-read when the file changes)."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {"usd_kzt": 500.0, "providers": {}, "pricing_usd_per_mtok": {}, "profiles": {}}
    if _cfg_cache.get("mtime") != mtime:
        _cfg_cache["data"] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        _cfg_cache["mtime"] = mtime
    return _cfg_cache["data"]


def provider_cfg(provider: str) -> dict:
    return dict(config().get("providers", {}).get(provider, {}))


def usd_kzt() -> float:
    return float(env("CQA_USD_KZT") or config().get("usd_kzt", 500.0))


def parse_model(spec: str | None, default_provider: str | None = None) -> tuple[str, str]:
    """'openai:deepseek-chat' -> ('openai', 'deepseek-chat'); 'ollama:qwen2.5:7b' -> ('ollama', 'qwen2.5:7b')."""
    spec = (spec or "").strip()
    if ":" in spec:
        head, rest = spec.split(":", 1)
        if head in PROVIDERS:
            return head, rest
    return (default_provider or "mock"), spec


def price_usd(model: str, tokens_in: int, tokens_out: int, tokens_cached: int = 0) -> float:
    """Cost from the pricing table (USD per 1M tokens). tokens_in includes cached tokens."""
    table = config().get("pricing_usd_per_mtok", {})
    row = table.get(model)
    if row is None:
        row = next((v for k, v in table.items() if not k.startswith("_") and model.startswith(k)), None)
    if not row:
        return 0.0
    fresh = max(tokens_in - tokens_cached, 0)
    return (fresh * row.get("in", 0) + tokens_cached * row.get("in_cached", row.get("in", 0)) + tokens_out * row.get("out", 0)) / 1e6


# ---------------------------------------------------------------- JSON extraction
def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in text (handles ```json fences, prose around it, <think> blocks)."""
    t = (text or "").strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in model output")
    start = min(starts)
    depth, instr, esc = 0, False, False
    open_c = t[start]
    close_c = "}" if open_c == "{" else "]"
    for i in range(start, len(t)):
        ch = t[i]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == open_c:
            depth += 1
        elif ch == close_c:
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("unbalanced JSON in model output")


# ---------------------------------------------------------------- listeners, mock, cache
_listeners: list[Callable[[dict], None]] = []
_mock_handler: Callable[..., Any] | None = None
_sem_lock = threading.Lock()
_semaphores: dict[str, threading.BoundedSemaphore] = {}


def add_listener(fn: Callable[[dict], None]) -> None:
    _listeners.append(fn)


def remove_listener(fn: Callable[[dict], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def set_mock(handler: Callable[..., Any] | None) -> None:
    """handler(system, user, model, temperature, run) -> dict or JSON string."""
    global _mock_handler
    _mock_handler = handler


def _semaphore(provider: str) -> threading.BoundedSemaphore:
    with _sem_lock:
        if provider not in _semaphores:
            n = int(provider_cfg(provider).get("concurrency", 4))
            _semaphores[provider] = threading.BoundedSemaphore(max(1, n))
        return _semaphores[provider]


def cache_dir() -> Path:
    return Path(env("CQA_CACHE") or ROOT / ".cache" / "llm")


def cache_key(provider: str, model: str, system: str, user: str, temperature: float, run: int) -> str:
    blob = json.dumps([provider, model, system, user, round(float(temperature), 4), int(run)], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_read(key: str) -> dict | None:
    p = cache_dir() / f"{key}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, entry: dict) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{key}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{key}.json")


# ---------------------------------------------------------------- providers
_clients: dict[tuple, Any] = {}
_client_lock = threading.Lock()


def _openai_client(base_url: str | None, api_key: str | None, timeout: float):
    from openai import OpenAI

    key = (base_url, api_key)
    with _client_lock:
        if key not in _clients:
            _clients[key] = OpenAI(base_url=base_url, api_key=api_key or "none", timeout=timeout, max_retries=0)
        return _clients[key]


def _call_openai_like(provider: str, system: str, user: str, model: str, temperature: float, max_tokens: int, timeout: float) -> tuple[str, dict]:
    pc = provider_cfg(provider)
    base_url = env(pc.get("base_url_env"), pc.get("base_url_default"))
    api_key = env(pc.get("api_key_env")) if pc.get("api_key_env") else "ollama"
    if provider == "openai" and not api_key:
        raise LLMError(f"no API key: set {pc.get('api_key_env', 'CQA_API_KEY')} in .env")
    client = _openai_client(base_url, api_key, timeout)
    kw: dict[str, Any] = dict(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=max_tokens)
    if "reasoner" not in model:  # deepseek-reasoner ignores/rejects sampling params
        kw["temperature"] = temperature
    else:  # thinking models spend output tokens on reasoning first: leave room so the JSON is not truncated
        kw["max_tokens"] = max(max_tokens, 16000)
    r = None
    if pc.get("json_mode", True):
        try:
            r = client.chat.completions.create(response_format={"type": "json_object"}, **kw)
        except Exception as e:  # endpoint without JSON mode -> plain call
            if "response_format" not in str(e) and "json" not in str(e).lower():
                raise
    if r is None:
        r = client.chat.completions.create(**kw)
    ch = r.choices[0]
    text = ch.message.content or ""
    if ch.finish_reason == "length":
        raise LLMError(f"output truncated at max_tokens={max_tokens}")
    u = r.usage
    extra = getattr(u, "model_extra", None) or {}
    cached = int(extra.get("prompt_cache_hit_tokens") or 0)
    if not cached:
        det = getattr(u, "prompt_tokens_details", None)
        cached = int(getattr(det, "cached_tokens", 0) or 0) if det else 0
    tin, tout = int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
    cost = 0.0 if provider == "ollama" else price_usd(model, tin, tout, cached)
    return text, {"tokens_in": tin, "tokens_out": tout, "tokens_cached": cached, "cost_usd": cost}


def ollama_base_url() -> str:
    pc = provider_cfg("ollama")
    base = (env(pc.get("base_url_env"), pc.get("base_url_default")) or "http://localhost:11434").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def ollama_problem(model: str, timeout: float = 4.0) -> str | None:
    """None if the Ollama server answers and has `model`; otherwise a short human-readable reason (pre-flight check)."""
    import httpx

    base = ollama_base_url()
    try:
        r = httpx.get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        names = {m.get("name") for m in (r.json().get("models") or [])} | {m.get("model") for m in (r.json().get("models") or [])}
    except Exception as exc:
        return f"Ollama недоступна по {base} ({type(exc).__name__}: {str(exc)[:120]})"
    if model not in names and f"{model}:latest" not in names:
        return f"в Ollama ({base}) нет модели {model}; есть: {', '.join(sorted(n for n in names if n)) or 'ничего'}"
    return None


def _call_ollama(system: str, user: str, model: str, temperature: float, max_tokens: int, timeout: float) -> tuple[str, dict]:
    """Native Ollama /api/chat: unlike the OpenAI-compatible /v1 it accepts options.num_ctx, so long transcripts are
    not silently truncated to the default 2-4k context. Base URL from CQA_OLLAMA_URL (a trailing /v1 is ignored)."""
    import httpx

    pc = provider_cfg("ollama")
    base = ollama_base_url()
    # think=False: hybrid reasoning models (qwen3.x) otherwise spend the output budget on hidden reasoning first,
    # which is ~2x slower on an 8 GB GPU and can truncate the JSON. Models without the switch reject it -> retry without.
    body = {"model": model, "stream": False, "format": "json", "think": bool(pc.get("think", False)),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_ctx": int(pc.get("num_ctx", 16384)), "num_predict": max_tokens}}
    r = httpx.post(f"{base}/api/chat", json=body, timeout=timeout)
    if getattr(r, "status_code", 200) == 400 and "think" in r.text:
        body.pop("think")
        r = httpx.post(f"{base}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return (d.get("message") or {}).get("content") or "", {"tokens_in": int(d.get("prompt_eval_count") or 0),
                                                           "tokens_out": int(d.get("eval_count") or 0), "tokens_cached": 0, "cost_usd": 0.0}


def _call_claude_cli(system: str, user: str, model: str, temperature: float, max_tokens: int, timeout: float, effort: str | None = None) -> tuple[str, dict]:
    pc = provider_cfg("claude_cli")
    binary = env(pc.get("binary_env"), pc.get("binary_default", "claude")) or "claude"
    cmd = [binary, "-p", "--system-prompt", system, "--model", model, "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", ""]
    if effort:
        cmd += ["--effort", effort]
    with tempfile.TemporaryDirectory(prefix="cqa-cli-") as d:  # empty cwd: no project CLAUDE.md is pulled in
        p = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd=d)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude CLI rc={p.returncode}: {(p.stderr or p.stdout)[-400:]}") from e
    if out.get("is_error") or out.get("subtype") not in (None, "success"):
        raise LLMError(f"claude CLI error: {str(out.get('result') or out.get('api_error_status'))[:400]}")
    u = out.get("usage") or {}
    tin = int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
    return out.get("result") or "", {"tokens_in": tin, "tokens_out": int(u.get("output_tokens", 0)),
                                     "tokens_cached": int(u.get("cache_read_input_tokens", 0)),
                                     "cost_usd": float(out.get("total_cost_usd") or 0.0), "cli_ms": out.get("duration_ms"),
                                     "resolved_models": sorted((out.get("modelUsage") or {}).keys()),
                                     "cost_basis": "cli_reported_estimate"}


def _call_anthropic(system: str, user: str, model: str, temperature: float, max_tokens: int, timeout: float) -> tuple[str, dict]:
    import anthropic

    key = ("anthropic", None)
    with _client_lock:
        if key not in _clients:
            _clients[key] = anthropic.Anthropic(timeout=timeout, max_retries=0)
    msg = _clients[key].messages.create(model=model, max_tokens=max_tokens, temperature=temperature, system=system,
                                        messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    tin, tout = int(msg.usage.input_tokens), int(msg.usage.output_tokens)
    cached = int(getattr(msg.usage, "cache_read_input_tokens", 0) or 0)
    return text, {"tokens_in": tin + cached, "tokens_out": tout, "tokens_cached": cached, "cost_usd": price_usd(model, tin + cached, tout, cached)}


def _call_mock(system: str, user: str, model: str, temperature: float, run: int) -> tuple[str, dict]:
    if _mock_handler is None:
        raise LLMError("mock provider: no handler registered (llm.set_mock)")
    out = _mock_handler(system, user, model, temperature, run)
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    return text, {"tokens_in": (len(system) + len(user)) // 4, "tokens_out": len(text) // 4, "tokens_cached": 0, "cost_usd": 0.0}


def _dispatch(provider: str, system: str, user: str, model: str, temperature: float, run: int, max_tokens: int, timeout: float, effort: str | None) -> tuple[str, dict]:
    if provider == "mock":
        return _call_mock(system, user, model, temperature, run)
    if provider == "claude_cli":
        return _call_claude_cli(system, user, model, temperature, max_tokens, timeout, effort)
    if provider == "ollama":
        return _call_ollama(system, user, model, temperature, max_tokens, timeout)
    if provider == "openai":
        return _call_openai_like(provider, system, user, model, temperature, max_tokens, timeout)
    if provider == "anthropic":
        return _call_anthropic(system, user, model, temperature, max_tokens, timeout)
    raise LLMError(f"unknown provider {provider!r}")


# ---------------------------------------------------------------- failure classes / fallback
_CREDIT_MARKERS = ("insufficient balance", "insufficient credits", "insufficient_quota", "exceed your available credits",
                   "credit balance is too low", "payment required", "requires more credits", "out of credits")
_TRANSIENT_MARKERS = ("rate limit", "rate_limit", "rate limited", "too many requests", "overloaded", "server error",
                      "service unavailable", "bad gateway", "gateway timeout", "temporarily unavailable",
                      "connection error", "connection reset", "connection refused")
def _sleep(seconds: float) -> None:  # looked up at call time, so tests can patch llm.time.sleep
    time.sleep(seconds)


def _status_of(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        for attr in ("status_code", "status"):
            v = getattr(obj, attr, None)
            if isinstance(v, int) and 100 <= v < 600:
                return v
    m = re.search(r"(?:error code|status(?: code)?|http)\W{0,3}(\d{3})\b", str(e), re.I)
    return int(m.group(1)) if m else None


def error_kind(e: BaseException) -> str:
    """'credits' (402 / no balance), 'transient' (429, 5xx, rate limit, connection), 'timeout' or 'other'."""
    msg, status = str(e).lower(), _status_of(e)
    if status == 402 or any(x in msg for x in _CREDIT_MARKERS):
        return "credits"
    name = type(e).__name__.lower()
    if isinstance(e, (subprocess.TimeoutExpired, TimeoutError)) or "timeout" in name or status == 408:
        return "timeout"
    if status == 429 or (status is not None and 500 <= status < 600) or "connection" in name \
            or any(x in msg for x in _TRANSIENT_MARKERS):
        return "transient"
    return "other"


def fallback_model(failed_spec: str | None = None) -> str | None:
    """Fallback "provider:model" for a failed model, or None (disabled / same provider / mock)."""
    spec = env("CQA_FALLBACK_MODEL")
    if spec is None:
        prof = config().get("profiles", {}).get(active_profile()) or {}
        cfg = prof.get("fallback", config().get("fallback"))
        spec = (cfg.get("model") if isinstance(cfg, dict) else cfg) if cfg is not None else None
    if not spec or str(spec).strip().lower() in ("none", "off", "0", "false"):
        return None
    if failed_spec:
        failed_provider = parse_model(failed_spec)[0]
        # ollama = the model inside the bank's perimeter: never send its dialogues to a cloud model behind the user's back
        if failed_provider in ("mock", "ollama") or parse_model(spec)[0] == failed_provider:
            return None
    return spec


def backoff_total_s() -> float:
    v = env("CQA_BACKOFF_TOTAL_S")
    cfg = config().get("fallback")
    return float(v if v not in (None, "") else (cfg.get("backoff_total_s", 60) if isinstance(cfg, dict) else 60))


# ---------------------------------------------------------------- public API
def complete_json(system: str, user: str, model: str | None = None, temperature: float = 0.0, *, run: int = 0,
                  max_tokens: int = 4096, no_cache: bool | None = None, timeout: float | None = None,
                  retries: int = 2, effort: str | None = None, mock_fn: Callable[[], Any] | None = None,
                  _fallback: dict | None = None):
    """Call the model and return (parsed_json, usage).

    model: "provider:model"; None -> main model of the active profile/mode "fast".
    run: vote number (part of the cache key). retries: extra attempts on invalid JSON / timeouts / other errors;
    rate limits and 5xx are retried with backoff (backoff_total_s), 402 goes to the fallback model at once.
    Legacy: cqa/judge.py passes mock_fn and expects only the dict (kept until the old prototype is removed).
    """
    if mock_fn is not None:  # legacy prototype path (cqa/judge.py)
        prov, _ = parse_model(model or default_model())
        if prov == "mock":
            return mock_fn()
        data, _usage = complete_json(system, user, model, temperature, run=run, max_tokens=max_tokens, no_cache=no_cache)
        return data
    spec = model or default_model()
    provider, mname = parse_model(spec, default_provider=parse_model(default_model())[0])
    if no_cache is None:
        no_cache = env("CQA_NO_CACHE") == "1"
    offline = env("CQA_OFFLINE") == "1"
    key = cache_key(provider, mname, system, user, temperature, run)
    base = {"provider": provider, "model": mname, "run": run, "temperature": temperature, "ts": time.time(), "cache_key": key[:16]}
    if _fallback:
        base.update(fallback=True, fallback_from=_fallback.get("from"), fallback_reason=_fallback.get("reason"))

    if (not no_cache or offline) and provider != "mock":
        hit = _cache_read(key)
        if hit is not None:
            u = dict(hit.get("usage") or {})
            usage = {**base, "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0), "tokens_cached": u.get("tokens_cached", 0),
                     "cost_usd": 0.0, "cost_kzt": 0.0, "cost_usd_full": u.get("cost_usd", 0.0), "cost_kzt_full": round(u.get("cost_usd", 0.0) * usd_kzt(), 4),
                     "latency_ms": 0, "latency_ms_full": u.get("latency_ms", 0), "cache_hit": True}
            usage.update({k: u[k] for k in ("resolved_models", "cost_basis") if k in u})
            _notify(usage)
            return hit["json"], usage
        if offline:
            raise OfflineCacheMiss(f"CQA_OFFLINE=1 and no cached answer for {provider}:{mname} (key {key[:12]})")

    pc = provider_cfg(provider)
    timeout = float(timeout or pc.get("timeout_s", 240))
    last: Exception | None = None
    last_kind = "other"
    prompt = user
    attempt = failures = 0
    waited, delay, budget = 0.0, 1.0, backoff_total_s()
    while True:
        attempt += 1
        t0 = time.perf_counter()
        try:
            with _semaphore(provider):
                text, u = _dispatch(provider, system, prompt, mname, temperature, run, max_tokens, timeout, effort)
            data = extract_json(text)
        except (ValueError, json.JSONDecodeError) as e:  # model answered, but not valid JSON
            last, last_kind = e, "other"
            failures += 1
            if failures > retries:
                break
            prompt = user + "\n\nВажно: верни ТОЛЬКО один валидный JSON-объект по схеме, без текста вокруг."
            continue
        except Exception as e:  # network, rate limit, no credits, timeout, CLI failure
            last, last_kind = e, error_kind(e)
            if isinstance(e, LLMError) and "no API key" in str(e):
                raise
            if last_kind == "credits":  # retrying cannot help: go to the fallback model now
                break
            if last_kind == "transient":  # rate limit / 5xx: exponential backoff within the budget
                step = min(delay, budget - waited)
                if step <= 0:
                    break
                _sleep(step)
                waited, delay = waited + step, delay * 2
                continue
            failures += 1
            # a hung provider burns a whole timeout_s per attempt: one retry at most, then the fallback
            if failures > (min(retries, 1) if last_kind == "timeout" else retries):
                break
            if last_kind != "timeout" or failures > 1:
                _sleep(min(2 * failures, 8))
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        cost = float(u.get("cost_usd", 0.0))
        usage = {**base, "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0), "tokens_cached": u.get("tokens_cached", 0),
                 "cost_usd": cost, "cost_kzt": round(cost * usd_kzt(), 4), "cost_usd_full": cost, "cost_kzt_full": round(cost * usd_kzt(), 4),
                 "latency_ms": ms, "latency_ms_full": ms, "cache_hit": False, "attempts": attempt}
        usage.update({k: u[k] for k in ("resolved_models", "cost_basis") if k in u})
        if provider != "mock":
            _cache_write(key, {"json": data, "text": text, "usage": {**u, "latency_ms": ms}, "created": time.time(), "provider": provider, "model": mname})
        _notify(usage)
        return data, usage
    failed = f"{provider}:{mname}"
    reason = f"{last_kind}: {type(last).__name__}: {str(last)[:200]}"
    fb = None if _fallback or last_kind not in ("credits", "transient", "timeout") else fallback_model(failed)
    if fb:
        try:
            return complete_json(system, user, fb, temperature, run=run, max_tokens=max_tokens, no_cache=no_cache,
                                 retries=retries, effort=effort, _fallback={"from": failed, "reason": reason})
        except LLMError as e2:
            raise LLMError(f"{failed} failed after {attempt} attempts ({reason}); fallback {fb} failed too: {str(e2)[:300]}") from e2
    raise LLMError(f"{failed} failed after {attempt} attempts: {type(last).__name__}: {str(last)[:300]}")


def _notify(usage: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(dict(usage))
        except Exception:
            pass


# ---------------------------------------------------------------- profiles / presets
def active_profile() -> str:
    return env("CQA_PROFILE") or config().get("default_profile", "cloud")


def preset(mode: str = "fast", profile: str | None = None) -> dict:
    """Evaluation preset {model, votes, temperatures, arbiter} for a mode of a profile, with env overrides."""
    prof = config().get("profiles", {}).get(profile or active_profile()) or {}
    p = dict(prof.get(mode) or prof.get("fast") or {"model": "mock:mock", "votes": 1, "temperatures": [0.0]})
    if env("CQA_MODEL"):
        p["model"] = env("CQA_MODEL")
    if env("CQA_ARBITER_MODEL") and p.get("arbiter"):
        p["arbiter"] = {**p["arbiter"], "model": env("CQA_ARBITER_MODEL")}
    p.setdefault("votes", 1)
    temps = list(p.get("temperatures") or [0.0])
    p["temperatures"] = [temps[i % len(temps)] for i in range(int(p["votes"]))]
    return p


def default_model() -> str:
    return preset("fast")["model"]


def provider() -> str:
    """Provider of the active fast preset (legacy helper used by server.py / health)."""
    return parse_model(default_model())[0]


def model() -> str:
    return parse_model(default_model())[1]


def health() -> dict:
    prov, m = parse_model(default_model())
    pc = provider_cfg(prov)
    key_env = pc.get("api_key_env")
    return {"profile": active_profile(), "provider": prov, "model": m, "offline": env("CQA_OFFLINE") == "1",
            "key_present": bool(env(key_env)) if key_env else prov in ("mock", "claude_cli", "ollama"),
            "base_url": env(pc.get("base_url_env"), pc.get("base_url_default")) if prov in ("openai", "ollama") else None}


def list_models(provider_name: str = "openai") -> list[str]:
    """GET /models of an OpenAI-compatible endpoint (DeepSeek, Ollama)."""
    pc = provider_cfg(provider_name)
    base_url = env(pc.get("base_url_env"), pc.get("base_url_default"))
    api_key = env(pc.get("api_key_env")) if pc.get("api_key_env") else "ollama"
    client = _openai_client(base_url, api_key, float(pc.get("timeout_s", 60)))
    return sorted(m.id for m in client.models.list().data)
