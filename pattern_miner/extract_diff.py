"""Extract a YAML-aware structured diff between two workflow blobs.

We do *not* use a textual diff. Instead we parse both versions with PyYAML, walk
the AST to a flat (path -> leaf-value) form, and emit three lists:

  added    : paths present only in the after version
  removed  : paths present only in the before version
  changed  : paths whose leaf value differs (with both old and new value)

`path` is a dotted/keyed string like
  jobs.build.steps[uses=actions/checkout~0].with.fetch-depth
  on.push.branches['main'~0]
  permissions.contents

Lists are keyed by *identity* rather than positional index, so that inserting a
step in the middle of a job's `steps:` list does not shift all subsequent
paths and turn one insertion into many spurious modifications.

Identity rules:
  - For a `steps` element (dict), pick the first available of:
        id  ->  "id=<value>"
        uses (dropping @ref)  -> "uses=<action>"
        name -> "name=<value>"
        run -> "run=<blake2b4(script)>"
        else -> "anon"
    The action ref (@v3 / @sha) is intentionally stripped so a version bump
    leaves the identity unchanged and shows up as a `.uses` value change.
  - For a list of strings, the key is the string itself: "'main'".
  - For a list of scalars, the key is repr(value).
  - For other lists of dicts, fall back to the dict's id/name/key field, else index.

Each key is suffixed with `~N` where N is the 0-based appearance order of that
raw key within the list, so duplicates remain distinguishable and stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import BLOBS_DIR


# --- list-element identity --------------------------------------------------


def _step_identity(step: dict) -> str:
    """Stable identity for a step element.

    Order matters: the goal is to keep the SAME identity across versions even
    when the step's contents (uses ref, run script, etc.) are exactly what we
    want to *detect* as a change.

      - `id`/`uses`/`name` are user-supplied tags, stable across edits.
      - `uses` keeps just the action name without `@ref`, so a version bump
        leaves identity intact.
      - For `run`-only steps we deliberately avoid hashing the script; we use
        the bare token "run" so the identity disambiguator (`~N` suffix added
        by `_list_keys`) yields position-within-run-only-steps. Hashing here
        would make every script edit look like a remove+add pair and hide
        meaningful per-line changes (set-output, expression-to-env, etc.).
    """
    sid = step.get("id")
    if isinstance(sid, str) and sid:
        return f"id={sid}"
    uses = step.get("uses")
    if isinstance(uses, str) and uses:
        action = uses.partition("@")[0]
        return f"uses={action}"
    name = step.get("name")
    if isinstance(name, str) and name:
        return f"name={name}"
    if "run" in step:
        return "run"
    return "anon"


def _generic_dict_identity(elem: dict, fallback_idx: int) -> str:
    for cand in ("id", "name", "key"):
        v = elem.get(cand)
        if isinstance(v, str) and v:
            return f"{cand}={v}"
    return f"#{fallback_idx}"


def _list_keys(node: list, parent_path: str) -> list[str]:
    """Return a stable identity key for each element of `node`.

    Each returned key is suffixed with `~N` (per-identity appearance order) so
    elements with the same raw identity remain distinguishable.
    """
    is_steps = parent_path.endswith(".steps") or parent_path == "steps"
    raw: list[str] = []
    for i, elem in enumerate(node):
        if isinstance(elem, str):
            raw.append(f"'{elem}'")
        elif isinstance(elem, bool) or elem is None:
            raw.append(repr(elem))
        elif isinstance(elem, (int, float)):
            raw.append(repr(elem))
        elif isinstance(elem, dict):
            raw.append(_step_identity(elem) if is_steps else _generic_dict_identity(elem, i))
        else:
            raw.append(f"#{i}")

    counts: dict[str, int] = {}
    out: list[str] = []
    for k in raw:
        n = counts.get(k, 0)
        counts[k] = n + 1
        out.append(f"{k}~{n}")
    return out


# --- YAML -> flat path map ---------------------------------------------------


def _flatten(node: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _flatten(v, child, out)
    elif isinstance(node, list):
        keys = _list_keys(node, prefix)
        for key, v in zip(keys, node):
            child = f"{prefix}[{key}]"
            _flatten(v, child, out)
    else:
        # leaf: scalar, None, bool, etc. Stringify for stable comparison.
        out[prefix] = node


def flatten_yaml(text: str) -> dict[str, Any]:
    """Parse YAML text and return a flat path->leaf mapping.

    Returns {} on parse failure (the row was flagged valid_yaml=True in the CSV
    but YAML can still fail under stricter loaders, so we degrade gracefully).
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if doc is None:
        return {}
    out: dict[str, Any] = {}
    _flatten(doc, "", out)
    return out


# --- diff dataclass ---------------------------------------------------------


@dataclass
class WorkflowDiff:
    repository: str
    commit_hash: str
    file_path: str
    file_hash: str          # after
    previous_file_hash: str  # before
    added: dict[str, Any] = field(default_factory=dict)
    removed: dict[str, Any] = field(default_factory=dict)
    changed: dict[str, tuple] = field(default_factory=dict)  # path -> (old, new)
    parse_error: bool = False

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_record(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "commit_hash": self.commit_hash,
            "file_path": self.file_path,
            "file_hash": self.file_hash,
            "previous_file_hash": self.previous_file_hash,
            "added": self.added,
            "removed": self.removed,
            # tuple is not JSON-serializable; emit as 2-element list.
            "changed": {k: list(v) for k, v in self.changed.items()},
            "parse_error": self.parse_error,
        }


# --- main entry --------------------------------------------------------------


def _read_blob(blobs_dir: Path, file_hash: str) -> str | None:
    p = blobs_dir / file_hash
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def diff_workflow_versions(
    repository: str,
    commit_hash: str,
    file_path: str,
    file_hash: str,
    previous_file_hash: str,
    blobs_dir: Path = BLOBS_DIR,
) -> WorkflowDiff:
    diff = WorkflowDiff(
        repository=repository,
        commit_hash=commit_hash,
        file_path=file_path,
        file_hash=file_hash,
        previous_file_hash=previous_file_hash,
    )
    after_text = _read_blob(blobs_dir, file_hash)
    before_text = _read_blob(blobs_dir, previous_file_hash)
    if after_text is None or before_text is None:
        diff.parse_error = True
        return diff

    after = flatten_yaml(after_text)
    before = flatten_yaml(before_text)
    if not after and not before:
        diff.parse_error = True
        return diff

    after_keys = set(after)
    before_keys = set(before)
    diff.added = {k: after[k] for k in after_keys - before_keys}
    diff.removed = {k: before[k] for k in before_keys - after_keys}
    for k in after_keys & before_keys:
        if after[k] != before[k]:
            diff.changed[k] = (before[k], after[k])
    return diff
