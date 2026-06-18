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


def default_model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or _DEFAULT_MODEL


def _raw_chat(
    model: str, system: str, user: str,
    *, temperature: float = 0.0, max_tokens: int = 4096,
) -> dict[str, Any]:
    """One uncached POST to /chat/completions. Returns the cache record dict."""
    key = _load_env_key()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    last_err: Exception | None = None
    _MAX = 7
    for attempt in range(_MAX):
        req = urllib.request.Request(
            f"{_base_url()}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
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

    def invoke(m: str, s: str, u: str) -> dict[str, Any]:
        return _raw_chat(m, s, u, temperature=temperature, max_tokens=max_tokens)

    # Only cache deterministic (temperature 0) calls; sampling should re-run.
    if use_cache and temperature == 0.0:
        return llm_call_cached(invoke, model=model, system=system, user=user)
    rec = invoke(model, system, user)
    rec["_cache_hit"] = False
    return rec
