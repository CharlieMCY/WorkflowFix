"""Configuration + secret loading.

The GitHub token is read from the .env file at the project root. We never
write it to disk or echo it. If absent we error out with setup instructions.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
OUTPUT_DIR = REPO_ROOT / "output"
GAPS_DIR = OUTPUT_DIR / "backport_gaps"

# load .env at import time, but do not overwrite already-set environment vars
load_dotenv(ENV_PATH, override=False)


def get_github_token() -> str:
    """Return the GitHub PAT, or raise with setup instructions."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set.\n"
            f"Create {ENV_PATH} from .env.example and put your token in it:\n"
            f"    cp {ENV_PATH.parent / '.env.example'} {ENV_PATH}\n"
            f"    # then edit and add: GITHUB_TOKEN=ghp_...\n"
            "A fine-grained PAT with public-read access (no scopes) is enough."
        )
    return token
