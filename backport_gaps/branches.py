"""Decide which branches look like maintained release / stable lines.

Conservative bias — false negatives (missing a release branch) are cheap
because we just don't audit it; false positives (treating a feature branch as
a release line) are expensive because we waste API calls fetching files that
will never be backport targets.
"""
from __future__ import annotations

import re

_RELEASE_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"^release[/_-].+",
        r"^releases?[/_-].+",
        r"^v\d+(\.\d+)*(?:\.x)?$",
        r"^v\d+(\.\d+)*-stable$",
        r"^stable$",
        r"^stable[/_-].+",
        r"^maint(enance)?[/_-].+",
        r"^maint(enance)?$",
        r"^\d+(\.\d+)*(\.x)?$",
        r"^[\d.x]+-stable$",
        r"^lts$",
        r"^lts[/_-].+",
        r"^support[/_-].+",
    ]
]


def is_release_branch(name: str, default_branch: str) -> bool:
    """Is `name` a release-style maintenance branch (and not the default)?"""
    if name == default_branch:
        return False
    return any(p.match(name) for p in _RELEASE_PATTERNS)


def filter_release_branches(
    branches: list[dict],
    default_branch: str,
) -> list[dict]:
    """Drop the default branch and anything that doesn't match a release pattern."""
    return [b for b in branches if is_release_branch(b["name"], default_branch)]
