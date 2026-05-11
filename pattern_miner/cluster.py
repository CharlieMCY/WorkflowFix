"""Stage-1 clustering by commit, two-level: fix-form set x structural template.

A pattern is "the bundle of fix forms that co-occurred in one commit", not a
single fix form. So the unit of clustering is a *commit*, after merging all of
its workflow-file diffs and unioning all of its fix-form labels.

Two-level grouping:
  Level 1 (form-set bucket):
    key = frozenset of fix-form categories applied in the commit
    captures "this commit pinned an action AND tightened permissions" as a
    distinct pattern from "this commit only pinned an action"
  Level 2 (structural sub-cluster, within a form-set bucket):
    key = blake2b hash of the commit's combined structural template
    captures *where in the workflow* the bundle lands

Vulnerabilities are deliberately NOT mined here. Linking a pattern back to a
specific CVE / weakness is an evaluation-stage concern (Contribution 4), not a
mining-stage concern. At mining time we only observe what was changed.

The structural template intentionally throws away action versions, SHAs, and
repo-specific names so that
    actions/checkout@v3 -> @v4
    actions/setup-node@v3 -> @v4
end up under the same template (`.uses` changed from a tag to a tag).
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable

from .extract_diff import WorkflowDiff

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v?\d+(\.\d+){0,2}$")

# Path-key generalization. Identity keys produced by extract_diff have the form
#   [<kind>=<value>~<n>]    (kind in id/uses/name/run, plus 'value~n for strings)
# We strip the per-identity disambiguation suffix and collapse repo-specific
# values so that semantically identical edits hash to the same template.
_DISAMBIG_RE = re.compile(r"~\d+(?=\])")           # the ~N right before ]
_USES_RE = re.compile(r"\[uses=[^\]]+\]")          # KEEP action identity meaningful
_STEP_ID_RE = re.compile(r"\[id=[^\]]+\]")
_STEP_NAME_RE = re.compile(r"\[name=[^\]]+\]")
_STEP_RUN_RE = re.compile(r"\[run=[0-9a-f]+\]")
_ANON_RE = re.compile(r"\[#\d+\]")
_LIST_STR_RE = re.compile(r"\['[^']*'\]")
_LIST_SCALAR_RE = re.compile(r"\[(?:-?\d+(?:\.\d+)?|True|False|None)\]")
_LEGACY_INDEX_RE = re.compile(r"\[\d+\]")           # pre-realignment data


def _generalize_path(path: str) -> str:
    # 1. drop disambiguation suffix
    path = _DISAMBIG_RE.sub("", path)
    # 2. collapse repo-specific identity values; KEEP `[uses=...]` since the
    #    action name is the actual unit of clustering.
    path = _STEP_ID_RE.sub("[id=*]", path)
    path = _STEP_NAME_RE.sub("[name=*]", path)
    path = _STEP_RUN_RE.sub("[run=*]", path)
    path = _ANON_RE.sub("[#*]", path)
    path = _LIST_STR_RE.sub("[str=*]", path)
    path = _LIST_SCALAR_RE.sub("[num=*]", path)
    path = _LEGACY_INDEX_RE.sub("[*]", path)
    return path


def _value_sketch(value: Any) -> str:
    """A coarse description of a leaf value so equivalent shapes hash the same."""
    if isinstance(value, bool):
        return f"bool:{value}"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        if "@" in value and "/" in value.split("@", 1)[0]:
            action, _, ref = value.rpartition("@")
            if _SHA_RE.match(ref):
                kind = "sha"
            elif _TAG_RE.match(ref):
                kind = "tag"
            else:
                kind = "ref"
            return f"uses:{action}@<{kind}>"
        if "${{" in value:
            return "expr"
        if value in {"read", "write", "none", "read-all", "write-all", "inherit"}:
            return f"perm:{value}"
        return "str"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"obj[{len(value)}]"
    return type(value).__name__


def diff_template(diff: WorkflowDiff) -> list[str]:
    """Sorted list of canonical edit descriptors for the diff."""
    parts: list[str] = []
    for p, v in diff.added.items():
        parts.append(f"+ {_generalize_path(p)} = {_value_sketch(v)}")
    for p, v in diff.removed.items():
        parts.append(f"- {_generalize_path(p)} = {_value_sketch(v)}")
    for p, (old, new) in diff.changed.items():
        parts.append(
            f"~ {_generalize_path(p)} : {_value_sketch(old)} -> {_value_sketch(new)}"
        )
    parts.sort()
    return parts


def template_hash(template: list[str]) -> str:
    h = hashlib.blake2b(digest_size=10)
    for line in template:
        h.update(line.encode())
        h.update(b"\n")
    return h.hexdigest()


def commit_template(diffs: Iterable[WorkflowDiff]) -> list[str]:
    """Combined structural template for *all* file-diffs of one commit.

    The lines from each file's template are concatenated and globally sorted,
    so the template is stable regardless of file ordering and any cross-file
    duplicate edits collapse naturally when generalized.
    """
    parts: list[str] = []
    for d in diffs:
        if d.parse_error or d.is_empty():
            continue
        parts.extend(diff_template(d))
    parts.sort()
    return parts


def cluster_by_commit(
    commits: Iterable[dict[str, Any]],
    max_exemplars: int = 5,
    key_field: str = "labels",
    key_name: str = "form_set",
) -> list[dict[str, Any]]:
    """Two-level cluster commits by (level-1 key, structural template).

    Each input element is a per-commit aggregate:
        {
            "repository": str,
            "commit_hash": str,
            "diffs":  list[WorkflowDiff],   # one per modified workflow file
            <key_field>: list[str],         # the primary cluster key
            ...                              # any extra fields propagate to exemplars
        }

    Level 1 buckets commits by `frozenset(c[key_field])`. Two natural choices:
      - key_field="labels" (default): cluster by our heuristic fix-form taxonomy
      - key_field="V_fixed_idents":  cluster by zizmor rule names that disappeared
        (scanner-grounded patterns)

    Returns a list of patterns sorted by n_commits desc:
        {
            <key_name>:               ["A", "B", ...],   # sorted
            "n_commits":              int,
            "n_subclusters":          int,
            "structural_subclusters": [
                {
                    "template_hash":  str,
                    "template_lines": [str, ...],
                    "n_commits":      int,
                    "exemplars":      [{repository, commit_hash, files, ...}],
                },
                ...
            ],
        }
    """
    # ---- level 1: bucket commits by primary key ---------------------------
    buckets: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    for c in commits:
        k = frozenset(c.get(key_field) or [])
        buckets[k].append(c)

    out: list[dict[str, Any]] = []

    for primary_key, group in buckets.items():
        # ---- level 2: within a bucket, sub-cluster by structural template
        sub: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "template_hash": "",
                "template_lines": [],
                "n_commits": 0,
                "exemplars": [],
            }
        )
        for c in group:
            tmpl = commit_template(c["diffs"])
            h = template_hash(tmpl)
            b = sub[h]
            if not b["template_lines"]:
                b["template_lines"] = tmpl
                b["template_hash"] = h
            b["n_commits"] += 1
            if len(b["exemplars"]) < max_exemplars:
                exemplar = {
                    "repository": c["repository"],
                    "commit_hash": c["commit_hash"],
                    "files": [d.file_path for d in c["diffs"]],
                }
                # propagate any extra metadata the caller attached
                for extra in ("V_fixed_idents", "labels", "form_set", "message_head"):
                    if extra in c and extra != key_field:
                        exemplar[extra] = c[extra]
                b["exemplars"].append(exemplar)
        subclusters = sorted(sub.values(), key=lambda r: -r["n_commits"])

        out.append(
            {
                key_name: sorted(primary_key),
                "n_commits": len(group),
                "n_subclusters": len(subclusters),
                "structural_subclusters": subclusters,
            }
        )

    out.sort(key=lambda r: -r["n_commits"])
    return out
