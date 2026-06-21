"""Round-robin pool of GitHub PATs, to multiply the per-token 5000/hr limit.

Tokens are discovered from the environment (and the repo .env) and merged,
de-duplicated, order preserved:

  GITHUB_TOKENS        comma / whitespace / newline separated list of tokens
  GITHUB_TOKEN         single token (back-compat)
  GITHUB_TOKEN_1..N    numbered tokens

If none are found and `allow_gh_cli` is set, `gh auth token` is the last resort.

`TokenPool` is thread-safe: `acquire()` hands out the next token that is not
currently rate-limited (round-robin); `block(token, reset_epoch)` parks a token
until its X-RateLimit-Reset; when every token is parked, `acquire()` sleeps
until the soonest reset. A single process-wide pool (`default_pool()`) lets all
clients/threads share one rate-limit view, so N tokens give ~N*5000/hr.

Nothing here is ever logged — tokens are secrets.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_SPLIT = re.compile(r"[\s,]+")


def _env_file_vars() -> dict[str, str]:
    """Minimal parse of .env for GITHUB_TOKEN* keys, so the pool works whether
    or not the shell sourced .env / dotenv ran. Real env vars take precedence."""
    out: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return out
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k.startswith("GITHUB_TOKEN"):
            out[k] = v.strip().strip('"').strip("'")
    return out


def load_tokens(allow_gh_cli: bool = False) -> list[str]:
    """Discover all configured GitHub tokens (env wins over .env file)."""
    merged = {**_env_file_vars(),
              **{k: v for k, v in os.environ.items() if k.startswith("GITHUB_TOKEN")}}
    toks: list[str] = []

    def _add(blob: str) -> None:
        for t in _SPLIT.split(blob or ""):
            t = t.strip()
            if t:
                toks.append(t)

    _add(merged.get("GITHUB_TOKENS", ""))
    _add(merged.get("GITHUB_TOKEN", ""))
    for key in sorted((k for k in merged if re.fullmatch(r"GITHUB_TOKEN_\d+", k)),
                      key=lambda k: int(k.rsplit("_", 1)[1])):
        _add(merged[key])

    toks = list(dict.fromkeys(toks))  # dedup, preserve order
    if not toks and allow_gh_cli:
        try:
            t = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
            if t:
                toks = [t]
        except Exception:
            pass
    return toks


class TokenPool:
    """Thread-safe round-robin pool with per-token rate-limit parking."""

    def __init__(self, tokens):
        self._tokens = [t for t in dict.fromkeys(tokens or []) if t]
        self._lock = threading.Lock()
        self._i = 0
        self._blocked: dict[str, float] = {}     # token -> epoch usable again

    def __len__(self) -> int:
        return len(self._tokens)

    def __bool__(self) -> bool:
        return bool(self._tokens)

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def acquire(self) -> str:
        """Return the next non-parked token (round-robin). If all are parked,
        sleep until the soonest reset and retry. Empty pool -> "" (unauth)."""
        if not self._tokens:
            return ""
        while True:
            with self._lock:
                now = time.time()
                n = len(self._tokens)
                for _ in range(n):
                    t = self._tokens[self._i % n]
                    self._i += 1
                    if self._blocked.get(t, 0.0) <= now:
                        return t
                soonest = min(self._blocked.values())
            time.sleep(max(1.0, min(soonest - time.time(), 120.0)))

    def block(self, token: str, reset_epoch: float) -> None:
        """Park `token` until `reset_epoch` (epoch seconds)."""
        if not token:
            return
        with self._lock:
            self._blocked[token] = max(self._blocked.get(token, 0.0), float(reset_epoch))

    def status(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            blocked = sum(1 for t in self._tokens if self._blocked.get(t, 0.0) > now)
        return {"total": len(self._tokens), "available": len(self._tokens) - blocked,
                "blocked": blocked}


_default: TokenPool | None = None
_default_lock = threading.Lock()


def default_pool() -> TokenPool:
    """Process-wide shared pool (tokens discovered once, incl. gh-cli fallback)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = TokenPool(load_tokens(allow_gh_cli=True))
    return _default


def coerce_pool(token) -> TokenPool:
    """Normalize a str | list | TokenPool | None into a TokenPool.
    None -> the shared default pool."""
    if isinstance(token, TokenPool):
        return token
    if token is None:
        return default_pool()
    if isinstance(token, (list, tuple, set)):
        return TokenPool(list(token))
    return TokenPool([token])
