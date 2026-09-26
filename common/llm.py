"""Minimal client for the Xiaomi MiMo LLM (OpenAI-compatible).

The key in `.env` (LLM_API) is a Token Plan key (`tp-` prefix), so the base URL
is the Token Plan endpoint, not the pay-as-you-go one:

    Token Plan      https://token-plan-sgp.xiaomimimo.com/v1   (tp-... keys)
    pay-as-you-go   https://api.xiaomimimo.com/v1              (sk-... keys)

Both speak the OpenAI `/chat/completions` schema. Calls route through
`common.cache.llm_call_cached`, keyed by sha256(model || system || user), so a
repeated (model, system, user) triple is served from `cache/llm/` — deterministic
re-runs cost nothing and the symbolic-feedback repair loop stays reproducible.

Network access only; no third-party SDK needed (uses urllib from stdlib so the
package keeps its small dependency surface).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .cache import llm_call_cached

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE = "https://token-plan-sgp.xiaomimimo.com/v1"
_DEFAULT_MODEL = "mimo-v2.5-pro"
_TIMEOUT = 180

# Backend selection. LLM_BACKEND=claude_code drives the Claude Code CLI in
# headless mode instead of the MiMo HTTP API; anything else uses the HTTP API.
_CC_TIMEOUT = 300
_CC_DEFAULT_MODEL = "sonnet"

# OpenRouter backend (LLM_BACKEND=openrouter): OpenAI-compatible, stateless HTTP,
# one [system,user] POST per call -> empty context per call by construction.
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_DEFAULT_MODEL = "openai/gpt-5.4-mini"
_OPENROUTER_HEADERS = {"HTTP-Referer": "https://github.com/workflowbp",
                       "X-Title": "WorkflowBP-eval"}


class LLMError(RuntimeError):
    pass


def _load_env_key() -> str:
    """LLM_API from the process env, falling back to the repo's .env file."""
    key = os.environ.get("LLM_API", "").strip()
    if key:
        return key
    env = _REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("LLM_API") and "=" in line:
                return line.split("=", 1)[1].strip()
    raise LLMError("LLM_API not set (env or .env)")


def _base_url() -> str:
    return os.environ.get("LLM_BASE_URL", "").strip() or _DEFAULT_BASE


def _backend() -> str:
    return os.environ.get("LLM_BACKEND", "").strip().lower() or "http"


def _no_think() -> bool:
    """LLM_NO_THINK truthy -> ask MiMo to skip its reasoning pass."""
    return os.environ.get("LLM_NO_THINK", "").strip().lower() not in ("", "0", "false", "no")


def default_model() -> str:
    m = os.environ.get("LLM_MODEL", "").strip()
    if m:
        return m
    b = _backend()
    if b == "claude_code":
        return _CC_DEFAULT_MODEL
    if b == "openrouter":
        return _OPENROUTER_DEFAULT_MODEL
    return _DEFAULT_MODEL


def _openrouter_key() -> str:
    """OPENROUTER_API_KEY from the process env, falling back to .env."""
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if k:
        return k
    env = _REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise LLMError("OPENROUTER_API_KEY not set (env or .env)")


def _raw_chat(
    model: str, system: str, user: str,
    *, temperature: float = 0.0, max_tokens: int = 4096,
    base: str | None = None, key: str | None = None,
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One uncached POST to an OpenAI-compatible /chat/completions endpoint.
    base/key default to the MiMo HTTP config; the OpenRouter backend passes its
    own. Only [system, user] are sent, so the call carries no prior context."""
    base = base or _base_url()
    key = key or _load_env_key()
    req_headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json", **(extra_headers or {})}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        body.update(extra_body)
    payload = json.dumps(body).encode("utf-8")

    last_err: Exception | None = None
    _MAX = 7
    for attempt in range(_MAX):
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=payload,
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            # 429 (rate limit) / 5xx are transient; back off generously and retry.
            # 429 in particular needs a longer wait than a couple seconds — the
            # token-plan rate window is per-minute, so escalate to ~tens of seconds.
            if e.code in (429, 500, 502, 503, 504) and attempt < _MAX - 1:
                wait = min(5 * (2 ** attempt), 60) if e.code == 429 else 2 ** attempt
                time.sleep(wait)
                last_err = LLMError(f"{e.code}: {body}")
                continue
            raise LLMError(f"HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < _MAX - 1:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise LLMError(f"network error after retries: {e}") from e
    else:  # pragma: no cover - loop always breaks or raises
        raise LLMError(f"exhausted retries: {last_err}")

    choice = (d.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = d.get("usage") or {}
    return {
        "text": text,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "finish_reason": choice.get("finish_reason", ""),
        "model": model,
    }


def _claude_code_bin() -> str:
    """Locate the Claude Code executable: CLAUDE_CODE_BIN, then the launching
    binary (CLAUDE_CODE_EXECPATH), then `claude` on PATH."""
    for cand in (os.environ.get("CLAUDE_CODE_BIN"),
                 os.environ.get("CLAUDE_CODE_EXECPATH"),
                 shutil.which("claude")):
        if cand and Path(cand).exists():
            return cand
    raise LLMError("Claude Code binary not found "
                   "(set CLAUDE_CODE_BIN or CLAUDE_CODE_EXECPATH)")


def _claude_code_chat(
    model: str, system: str, user: str,
    *, temperature: float = 0.0, max_tokens: int = 4096,
) -> dict[str, Any]:
    """One headless Claude Code call (`claude -p`). Same return shape as
    `_raw_chat`. The system prompt is appended via --append-system-prompt and the
    user prompt is fed on stdin; tools are disabled so it only generates text.
    `temperature`/`max_tokens` have no CLI knob and are accepted for signature
    parity (determinism is provided by the temperature-0 cache in `complete`)."""
    # Strip all Claude Code agent baggage so a call carries only our own prompt.
    # --system-prompt REPLACES the default agent prompt (vs --append), and the
    # rest drop dynamic sections, built-in tools, MCP servers, skills, and
    # settings/CLAUDE.md. This cuts ~25k tokens of per-call overhead to ~0.
    cmd = [_claude_code_bin(), "-p", "--output-format", "json", "--model", model,
           "--system-prompt", system or "",
           "--exclude-dynamic-system-prompt-sections",
           "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "",
           "--disable-slash-commands",
           "--no-session-persistence"]   # don't write a chat transcript per call

    last_err: Exception | None = None
    _MAX = 4
    for attempt in range(_MAX):
        try:
            proc = subprocess.run(cmd, input=user, capture_output=True,
                                  text=True, timeout=_CC_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            last_err = e
            if attempt < _MAX - 1:
                time.sleep(2 ** attempt)
                continue
            raise LLMError(f"claude code timeout after retries: {e}") from e
        if proc.returncode != 0:
            last_err = LLMError(f"exit {proc.returncode}: {proc.stderr[:300]}")
            if attempt < _MAX - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_err
        try:
            d = json.loads(proc.stdout)
        except Exception as e:
            raise LLMError(f"claude code: non-JSON output: "
                           f"{proc.stdout[:300]}") from e
        if d.get("is_error"):
            last_err = LLMError(f"api_error: {str(d.get('result',''))[:200]}")
            if attempt < _MAX - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_err
        usage = d.get("usage") or {}
        return {
            "text": d.get("result") or "",
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "finish_reason": d.get("stop_reason", ""),
            "model": model,
        }
    raise LLMError(f"claude code failed after retries: {last_err}")


def complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Run one chat completion. Cached by (model, system, user) when temp==0.

    Returns {"text", "input_tokens", "output_tokens", "finish_reason",
    "model", "_cache_hit"}.
    """
    model = model or default_model()
    backend = _backend()
    # MiMo (http) reasoning models think by default; LLM_NO_THINK=1 turns the
    # reasoning pass off via MiMo's `thinking` request field, which cuts output
    # tokens (and latency) several-fold. Only valid on the MiMo endpoint.
    no_think = backend == "http" and _no_think()
    nt_body = {"thinking": {"type": "disabled"}} if no_think else None

    def invoke(m: str, s: str, u: str) -> dict[str, Any]:
        if backend == "claude_code":
            return _claude_code_chat(m, s, u, temperature=temperature,
                                     max_tokens=max_tokens)
        if backend == "openrouter":
            return _raw_chat(m, s, u, temperature=temperature, max_tokens=max_tokens,
                             base=_OPENROUTER_BASE, key=_openrouter_key(),
                             extra_headers=_OPENROUTER_HEADERS)
        return _raw_chat(m, s, u, temperature=temperature, max_tokens=max_tokens,
                         extra_body=nt_body)

    # Only cache deterministic (temperature 0) calls; sampling should re-run.
    if use_cache and temperature == 0.0:
        return llm_call_cached(invoke, model=model, system=system, user=user,
                               salt=("nothink" if no_think else ""))
    rec = invoke(model, system, user)
    rec["_cache_hit"] = False
    return rec
