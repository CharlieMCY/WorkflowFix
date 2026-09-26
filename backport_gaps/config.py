"""Configuration + secret loading.

The GitHub token is read from the .env file at the project root. We never
write it to disk or echo it. If absent we error out with setup instructions.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from common.dataset import output_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
OUTPUT_DIR = output_dir()
GAPS_DIR = OUTPUT_DIR / "backport_gaps"

# load .env at import time, but do not overwrite already-set environment vars
load_dotenv(ENV_PATH, override=False)


def get_github_tokens() -> list[str]:
    """Return the configured GitHub PATs.

    Reads tokens from .env in priority order:
      1. `GITHUB_TOKENS` — comma-separated list (e.g. `ghp_a,ghp_b,ghp_c`)
      2. `GITHUB_TOKEN`  — single token (backwards compatible)

    Returns the list of token strings. Raises with setup instructions if
    neither is set. Multiple tokens are useful for the streaming driver
    (`backport_gaps stream-all`): the GitHubClient round-robins across
    them and skips a session that's been rate-limited until its quota
    resets, multiplying effective throughput by the number of tokens.
    """
    multi = os.environ.get("GITHUB_TOKENS", "").strip()
    if multi:
        tokens = [t.strip() for t in multi.split(",") if t.strip()]
        if tokens:
            return tokens

    single = os.environ.get("GITHUB_TOKEN", "").strip()
    if single:
        return [single]

    raise RuntimeError(
        "No GitHub token configured.\n"
        f"Create {ENV_PATH} from .env.example and put at least one token in it:\n"
        f"    cp {ENV_PATH.parent / '.env.example'} {ENV_PATH}\n"
        f"    # then either:\n"
        f"    GITHUB_TOKEN=ghp_xxx                                  # single token\n"
        f"    GITHUB_TOKENS=ghp_xxx,ghp_yyy,ghp_zzz                 # multi-token\n"
        "A fine-grained PAT with public-read access (no scopes) is enough."
    )


def get_github_token() -> str:
    """Return one GitHub PAT — back-compat single-token entry point.

    Returns the first token from `get_github_tokens()`. Callers that
    benefit from multi-token rotation should call `get_github_tokens()`
    instead and pass the full list to `GitHubClient`.
    """
    return get_github_tokens()[0]
