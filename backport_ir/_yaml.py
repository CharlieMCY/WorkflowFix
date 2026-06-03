"""Shared YAML loading for backport_ir — one agreed-upon semantics.

The notorious trap is YAML 1.1 (PyYAML's default), which parses
`on`/`off`/`yes`/`no` as booleans — that would turn a workflow's `on:` trigger
into a `True:` key and desync `compile` (which flattens the diff) from `apply`
(ruamel, YAML 1.2). So every loader here is ruamel / YAML 1.2.
"""
from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML


def load_safe(text: str) -> Any:
    """Parse to plain python (dict/list/scalars) as YAML 1.2. None on error.

    Used by compile (to flatten a diff) and verify (to re-read patched text),
    so both see the same keys ruamel gives apply.
    """
    y = YAML(typ="safe")
    y.version = (1, 2)
    try:
        return y.load(text)
    except Exception:
        return None


def rt_yaml() -> YAML:
    """A round-trip YAML for format-preserving edits (used by apply)."""
    y = YAML()                           # round-trip (default)
    y.preserve_quotes = True
    y.width = 4096                       # don't rewrap long lines
    y.indent(mapping=2, sequence=4, offset=2)
    return y
