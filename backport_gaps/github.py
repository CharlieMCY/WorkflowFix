"""Minimal GitHub REST client for the operations we need.

Authenticated calls share a 5000/hr rate limit. We log rate-limit headers and
back off briefly on transient errors. Only public-read endpoints are used.
"""
from __future__ import annotations

import base64
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


class GitHubClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        })
        # urllib3 Retry: on transient connection errors, drop the broken
        # pooled connection and reconnect, instead of hanging in poll().
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=1.0,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10,
                              pool_maxsize=10)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # --- HTTP -----------------------------------------------------------

    def _get(self, path: str, params: dict | None = None,
             allow_404: bool = False) -> requests.Response:
        url = path if path.startswith("http") else f"{_BASE}{path}"
        for attempt in range(4):
            try:
                r = self._session.get(url, params=params, timeout=_TIMEOUT)
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
            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - int(time.time()), 1)
                time.sleep(min(wait, 120))
                continue
            if r.status_code in (502, 503, 504):
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
        r = self._get(f"/repos/{repo}/commits/{sha}", allow_404=True)
        return r.json() if r.status_code == 200 else None

    def get_file_at_ref(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        """Return (content_bytes, blob_sha) at `ref`, or None if 404."""
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
