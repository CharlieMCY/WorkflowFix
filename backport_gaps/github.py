"""Minimal GitHub REST client for the operations we need.

Single-token mode: 5000 GET/hr per PAT. When constructed with multiple
tokens (the streaming driver does this), the client round-robins requests
across an internal pool of `requests.Session`s — one per token — and
parks any session that hits the 403 rate-limit response in a cooldown
list until its quota resets. Effective ceiling is therefore N × 5000/hr
for N tokens, modulo GitHub's per-account secondary rate limit (which is
the practical reason multi-account tokens scale further than multi-PAT-
single-account tokens). Only public-read endpoints are used; we log
rate-limit headers and back off briefly on transient errors.
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_BASE = "https://api.github.com"
# Tuple: (connect_timeout, read_timeout). Both enforced — protects against
# half-dead keep-alive sockets that hang in poll() indefinitely.
_TIMEOUT = (10, 30)
_USER_AGENT = "workflow-backport-gaps/0.1"


class GitHubError(RuntimeError):
    pass


def _make_session(token: str) -> requests.Session:
    """Build a requests.Session authenticated for one PAT, with the usual
    transient-retry adapter."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    })
    retry = Retry(
        total=3, connect=3, read=2, backoff_factor=1.0,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry,
                          pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class GitHubClient:
    def __init__(self, tokens):
        """Accepts a single PAT string or a list of PATs.

        With a list, the client round-robins requests across one session
        per token; rate-limited sessions park in a cooldown set until
        their `X-RateLimit-Reset` passes, so the other tokens keep
        serving requests in parallel.
        """
        if isinstance(tokens, str):
            tokens = [tokens]
        if not tokens:
            raise ValueError("GitHubClient requires at least one token")
        self._sessions = [_make_session(t) for t in tokens]
        self._cooldown_until = [0.0] * len(self._sessions)
        self._next_idx = 0
        self._lock = threading.Lock()
        # back-compat alias for any external code that previously poked at
        # `client._session` directly
        self._session = self._sessions[0]

    # --- session picking ------------------------------------------------

    def _pick_session(self) -> tuple[int, requests.Session]:
        """Return (idx, session) for the next non-cooldown session. If
        every session is cooled down, sleep until the earliest reset and
        return that one."""
        while True:
            with self._lock:
                now = time.time()
                n = len(self._sessions)
                for offset in range(n):
                    idx = (self._next_idx + offset) % n
                    if self._cooldown_until[idx] <= now:
                        self._next_idx = (idx + 1) % n
                        return idx, self._sessions[idx]
                # everything cooled — find earliest reset
                idx = min(range(n), key=lambda i: self._cooldown_until[i])
                wait = max(self._cooldown_until[idx] - now, 1.0)
                # bound the sleep so a misreported reset doesn't stall us
                wait = min(wait, 120.0)
            time.sleep(wait)

    # --- HTTP -----------------------------------------------------------

    def _get(self, path: str, params: dict | None = None,
             allow_404: bool = False, allow_422: bool = False) -> requests.Response:
        url = path if path.startswith("http") else f"{_BASE}{path}"
        for attempt in range(4):
            idx, session = self._pick_session()
            try:
                r = session.get(url, params=params, timeout=_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                # urllib3 Retry already retried adapter-level; this is a
                # final fallback so a stubborn network blip doesn't crash.
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"GET {url} network error after retries: {e}")
            if r.status_code == 200:
                return r
            if r.status_code == 404 and allow_404:
                return r
            if r.status_code == 422 and allow_422:
                return r
            if r.status_code == 403 and "rate limit" in r.text.lower():
                # Park THIS session until its quota resets; loop will pick
                # the next available session on the next attempt.
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                with self._lock:
                    self._cooldown_until[idx] = max(
                        self._cooldown_until[idx], float(reset)
                    )
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401 and attempt < 3:
                # Token is valid (verified separately), so 401 here is
                # transient — possibly a brief auth-cache desync on the
                # GitHub side. Back off and try again.
                time.sleep(2 ** attempt)
                continue
            raise GitHubError(f"GET {url} -> {r.status_code}: {r.text[:200]}")
        raise GitHubError(f"GET {url} exhausted retries")

    # --- endpoints ------------------------------------------------------

    def get_repo(self, repo: str) -> dict:
        return self._get(f"/repos/{repo}").json()

    def iter_branches(self, repo: str) -> Iterator[dict]:
        """Yield every branch dict for a repo (paginates)."""
        url = f"{_BASE}/repos/{repo}/branches"
        params = {"per_page": 100}
        while url:
            r = self._get(url, params=params)
            for b in r.json():
                yield b
            link = r.headers.get("Link") or ""
            url = _next_link(link)
            params = None   # subsequent pages already encode params in url

    def commit_in_branch_history(self, repo: str, branch: str, sha: str) -> bool:
        """True iff `sha` is an ancestor of (or equal to) the branch's HEAD."""
        r = self._get(
            f"/repos/{repo}/compare/{branch}...{sha}",
            allow_404=True,
        )
        if r.status_code == 404:
            return False
        status = r.json().get("status", "")
        # status="behind" means sha is older than branch HEAD (reachable from HEAD).
        # status="identical" means sha is branch HEAD.
        return status in ("behind", "identical")

    def get_commit(self, repo: str, sha: str) -> dict | None:
        """Return the commit dict at `(repo, sha)`, or None if 404.

        Routes through `common.cache.github_commit_cached_fetch` so a
        commit fetched once (possibly under a different DATASET_TAG) is
        served from local cache. Commits are immutable, so cache entries
        never expire. On miss the cache layer calls
        `_get_commit_uncached` below.
        """
        from common.cache import github_commit_cached_fetch
        return github_commit_cached_fetch(self, repo, sha)

    def _get_commit_uncached(self, repo: str, sha: str) -> dict | None:
        """The bare GitHub API call. Cache layer invokes this on miss.

        Treats both 404 and 422 as "no such commit in this repo" — GitHub
        returns 422 for valid-format SHAs not found in the repo (vs. 404
        for malformed paths). Either way the right answer is None, which
        the cache layer persists as a `.missing` marker.
        """
        r = self._get(f"/repos/{repo}/commits/{sha}", allow_404=True,
                       allow_422=True)
        return r.json() if r.status_code == 200 else None

    def get_file_at_ref(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        """Return (content_bytes, blob_sha) at `ref`, or None if 404.

        Routes through `common.cache.github_file_cached_fetch` so a
        previous run (possibly under a different DATASET_TAG) for the
        same (repo, ref, path) is served from local cache instead of
        re-hitting GitHub. On miss the cache layer calls
        `_get_file_at_ref_uncached` below.
        """
        from common.cache import github_file_cached_fetch
        return github_file_cached_fetch(self, repo, path, ref)

    def _get_file_at_ref_uncached(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        """The bare GitHub API call. Cache layer invokes this on miss."""
        r = self._get(
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
            allow_404=True,
        )
        if r.status_code == 404:
            return None
        j = r.json()
        if isinstance(j, list) or j.get("type") != "file":
            return None
        content = base64.b64decode(j.get("content", ""))
        return content, j.get("sha", "")

    def iter_commits_touching_file(
        self, repo: str, branch: str, path: str, max_pages: int = 5,
    ) -> Iterator[dict]:
        """Yield commits (newest first) that modified `path` on `branch`.

        Caps at `max_pages` of 100 commits each (= 500 commits max) so we don't
        chase decades of history per file.
        """
        url = f"{_BASE}/repos/{repo}/commits"
        params = {"sha": branch, "path": path, "per_page": 100}
        pages = 0
        while url and pages < max_pages:
            r = self._get(url, params=params)
            for c in r.json():
                yield c
            link = r.headers.get("Link") or ""
            url = _next_link(link)
            params = None
            pages += 1


def _next_link(link_header: str) -> str | None:
    """Parse a Link header for the rel='next' URL, if any."""
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            return part.split(";", 1)[0].strip().strip("<>")
    return None
