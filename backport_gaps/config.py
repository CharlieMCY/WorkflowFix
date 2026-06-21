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
    """Return all configured GitHub PATs (a pool), or raise with setup hints.

    Reads GITHUB_TOKENS (comma/space/newline list), GITHUB_TOKEN, and
    GITHUB_TOKEN_1..N from the env / .env. Pass the result to GitHubClient to
    rotate across tokens and multiply the 5000/hr per-token rate limit.
    """
    from common.gh_tokens import load_tokens

    tokens = load_tokens(allow_gh_cli=False)
    if not tokens:
        raise RuntimeError(
            "No GitHub token configured.\n"
            f"Create {ENV_PATH} from .env.example and add a token:\n"
            f"    cp {ENV_PATH.parent / '.env.example'} {ENV_PATH}\n"
            f"    # single:  GITHUB_TOKEN=ghp_...\n"
            f"    # pool:    GITHUB_TOKENS=ghp_aaa,ghp_bbb,ghp_ccc\n"
            "A fine-grained PAT with public-read access (no scopes) is enough."
        )
    return tokens


def get_github_token() -> str:
    """Return the first configured GitHub PAT (back-compat single-token API)."""
    return get_github_tokens()[0]
