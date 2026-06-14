"""Content-addressed caches for expensive external calls.

Three pluggable caches, all file-based, all dataset-independent
(several DATASET_TAG runs share the same cache hits):

  github_file_cache    keyed by (repo, ref, path), stores the bytes
                       returned by GitHub's contents API and the file's
                       blob_sha. Marks missing files so we don't re-ask.
  github_commit_cache  keyed by (repo, sha), stores the full commit JSON
                       from GitHub's commits API. A commit at a given
                       SHA is immutable, so cache entries never expire.
  llm_call_cache       keyed by sha256(model || system || user), stores
                       the response text + token counts.

All layouts use 2-level sharded directories under cache/ to keep any
one directory from holding more than ~10k entries.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .dataset import cache_dir


# --- helpers --------------------------------------------------------------


def _sha(text: str, n: int = 32) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _shard(key: str, depth: int = 2) -> Path:
    """Take key 'abcdef...' and return Path('ab/cd/abcdef...') so any
    single directory holds at most ~256 children at each shard level."""
    parts = [key[i:i + 2] for i in range(0, depth * 2, 2)] + [key]
    return Path(*parts)


# --- GitHub file cache ----------------------------------------------------


def _github_path(repo: str, ref: str, path: str) -> Path:
    key = _sha(f"{repo}\0{ref}\0{path}")
    return cache_dir() / "github" / _shard(key)


def github_file_get(repo: str, ref: str, path: str) -> tuple[bytes, str] | None:
    """Return (content_bytes, blob_sha) from cache, or None if not present.
    A returned None covers both 'never cached' AND 'cached as missing'.

    Callers should distinguish via `github_file_cached_missing()` if needed.
    """
    base = _github_path(repo, ref, path)
    blob = base.with_suffix(".bin")
    sha = base.with_suffix(".sha")
    if blob.exists() and sha.exists():
        return blob.read_bytes(), sha.read_text().strip()
    return None


def github_file_cached_missing(repo: str, ref: str, path: str) -> bool:
    """True iff we cached a 404 for this triple (so we can skip re-fetching)."""
    return _github_path(repo, ref, path).with_suffix(".missing").exists()


def github_file_put(repo: str, ref: str, path: str,
                    content: bytes, blob_sha: str) -> None:
    base = _github_path(repo, ref, path)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".bin").write_bytes(content)
    base.with_suffix(".sha").write_text(blob_sha)


def github_file_put_missing(repo: str, ref: str, path: str) -> None:
    base = _github_path(repo, ref, path)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".missing").touch()


def github_file_cached_fetch(
    client, repo: str, path: str, ref: str,
) -> tuple[bytes, str] | None:
    """Serve a GitHub file fetch from cache, falling back to the network.

    Calls `client._get_file_at_ref_uncached` on miss (NOT `get_file_at_ref`,
    which would recurse through this very wrapper). The convention is:

      - public `client.get_file_at_ref` -> goes through this cache
      - private `client._get_file_at_ref_uncached` -> raw API call

    Cache layout:
        cache/github/<2>/<2>/<sha>.bin       file bytes
        cache/github/<2>/<2>/<sha>.sha       blob sha (sidecar)
        cache/github/<2>/<2>/<sha>.missing   negative-cache marker
    """
    hit = github_file_get(repo, ref, path)
    if hit is not None:
        return hit
    if github_file_cached_missing(repo, ref, path):
        return None
    fetched = client._get_file_at_ref_uncached(repo, path, ref)
    if fetched is None:
        github_file_put_missing(repo, ref, path)
        return None
    content, blob_sha = fetched
    github_file_put(repo, ref, path, content, blob_sha)
    return fetched


# --- GitHub commit cache --------------------------------------------------


def _commit_path(repo: str, sha: str) -> Path:
    key = _sha(f"{repo}\0{sha}")
    return cache_dir() / "commit" / _shard(key)


def github_commit_get(repo: str, sha: str) -> dict | None:
    """Return the cached commit dict, or None if not present (covers both
    'never cached' AND 'cached as missing' — use `github_commit_cached_missing`
    to distinguish)."""
    p = _commit_path(repo, sha).with_suffix(".json")
    if p.exists():
        return json.loads(p.read_text())
    return None


def github_commit_cached_missing(repo: str, sha: str) -> bool:
    """True iff we cached a 404 for this (repo, sha)."""
    return _commit_path(repo, sha).with_suffix(".missing").exists()


def github_commit_put(repo: str, sha: str, commit: dict) -> None:
    p = _commit_path(repo, sha).with_suffix(".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(commit, ensure_ascii=False))


def github_commit_put_missing(repo: str, sha: str) -> None:
    p = _commit_path(repo, sha).with_suffix(".missing")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def github_commit_cached_fetch(client, repo: str, sha: str) -> dict | None:
    """Serve a `get_commit` call from cache, falling back to the network.

    Calls `client._get_commit_uncached` on miss. Same convention as the
    file cache:

      - public `client.get_commit` -> goes through this cache
      - private `client._get_commit_uncached` -> raw API call

    Cache layout:
        cache/commit/<2>/<2>/<sha>.json     full commit dict
        cache/commit/<2>/<2>/<sha>.missing  negative-cache marker

    Commits at a given SHA are immutable in git, so cache entries never
    expire and never need invalidation.
    """
    hit = github_commit_get(repo, sha)
    if hit is not None:
        return hit
    if github_commit_cached_missing(repo, sha):
        return None
    fetched = client._get_commit_uncached(repo, sha)
    if fetched is None:
        github_commit_put_missing(repo, sha)
        return None
    github_commit_put(repo, sha, fetched)
    return fetched


# --- LLM call cache -------------------------------------------------------


def _llm_path(model: str, system: str, user: str) -> Path:
    key = _sha(f"{model}\n\n{system}\n\n{user}", n=48)
    return cache_dir() / "llm" / _shard(key) / "response.json"


def llm_call_cached(
    invoke: Callable[[str, str, str], dict[str, Any]],
    *, model: str, system: str, user: str,
) -> dict[str, Any]:
    """Cache an LLM completion keyed by (model, system, user).

    `invoke(model, system, user)` is the function that makes the actual
    API call; it should return a dict with at least {"text", "input_tokens",
    "output_tokens"}. The cache layer never makes API calls on its own;
    on cache hit it returns the stored dict, on miss it calls `invoke`
    and stores the result.
    """
    p = _llm_path(model, system, user)
    if p.exists():
        record = json.loads(p.read_text())
        record["_cache_hit"] = True
        return record
    record = invoke(model, system, user)
    record.setdefault("model", model)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, ensure_ascii=False))
    record["_cache_hit"] = False
    return record


# --- generic JSONL row cache (for analysis scripts) -----------------------


def jsonl_already_done(path: Path, key_fn: Callable[[dict], tuple]) -> set[tuple]:
    """Read a JSONL of result rows and return the set of keys already
    processed. Use to skip work on resume:

        done = jsonl_already_done(out_path, lambda r: (r["repository"],
                                                       r["commit_hash"],
                                                       r["branch"]))
        for item in work:
            if (item["repository"], ...) in done:
                continue
            ...
    """
    if not path.exists():
        return set()
    done: set[tuple] = set()
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            try:
                done.add(key_fn(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def jsonl_append(path: Path, row: dict) -> None:
    """Append one row to a JSONL file, flushing immediately so a crash
    mid-pipeline doesn't lose recent work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False))
        fp.write("\n")
        fp.flush()
