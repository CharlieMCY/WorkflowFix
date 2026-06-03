"""Compile one master clean-fix commit's (before -> after) diff into an IRProgram.

Reuses `pattern_miner.extract_diff.flatten_yaml` to get the same identity-keyed
flat path map the miner already uses, then:

  * parses each flat path into structured anchor `Seg`s — the job name becomes a
    `$JOB` metavariable, steps stay matched by `uses=`/`id=`/`name=` identity;
  * routes  added -> ensure_present,  removed -> ensure_absent,
    changed -> rewrite_value;
  * recognises a `uses:` tag->SHA change as a version-aligned `Pin` (so the
    target gets pinned to *its own* major, not master's resolved SHA).

The compiler is intentionally lossless where it matters: it keeps action
identities and concrete leaf values, unlike `cluster._generalize_path` which
throws them away to hash patterns together.
"""
from __future__ import annotations

import re
from typing import Any

from pattern_miner.cluster import _value_sketch
from pattern_miner.extract_diff import _flatten

from ._yaml import load_safe

from .ir import (
    ENSURE_ABSENT,
    ENSURE_PRESENT,
    REWRITE_VALUE,
    Anchor,
    Edit,
    IRProgram,
    Pin,
    Seg,
)

_DISAMBIG = re.compile(r"~\d+$")        # the per-identity appearance suffix
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ANON_RE = re.compile(r"^#\d+$")


# --- flat-path -> structured segments ---------------------------------------


def _parse_list_inner(inner: str) -> Seg:
    """Turn a list-identity token (e.g. `uses=actions/checkout~0`) into a Seg."""
    inner = _DISAMBIG.sub("", inner)
    for k in ("uses", "id", "name"):
        if inner.startswith(k + "="):
            return Seg.listid(k, inner[len(k) + 1:])
    if inner == "run":
        return Seg.listid("run", "")
    if len(inner) >= 2 and inner[0] == "'" and inner[-1] == "'":
        return Seg.listid("str", inner[1:-1])
    if _ANON_RE.match(inner):
        return Seg.listid("anon", inner[1:])
    return Seg.listid("scalar", inner)   # int/float/bool/None repr


def parse_path(path: str) -> list[Seg]:
    """Tokenize an extract_diff flat path into Segs.

    Handles `.`-separated mapping keys and `[...]` list-identity brackets, with
    bracket-depth tracking so identity values that contain `.`/`[` survive.
    """
    segs: list[Seg] = []
    buf = ""
    i = 0
    n = len(path)
    while i < n:
        c = path[i]
        if c == ".":
            if buf:
                segs.append(Seg.key(buf))
                buf = ""
            i += 1
        elif c == "[":
            if buf:
                segs.append(Seg.key(buf))
                buf = ""
            j = i + 1
            depth = 1
            inner = ""
            while j < n and depth > 0:
                cj = path[j]
                if cj == "[":
                    depth += 1
                elif cj == "]":
                    depth -= 1
                    if depth == 0:
                        break
                inner += cj
                j += 1
            segs.append(_parse_list_inner(inner))
            i = j + 1
        else:
            buf += c
            i += 1
    if buf:
        segs.append(Seg.key(buf))
    return segs


def _metavar_jobs(segs: list[Seg]) -> list[Seg]:
    """Replace the dict key immediately under `jobs` with a `$JOB` metavariable."""
    out: list[Seg] = []
    prev_is_jobs = False
    for s in segs:
        if prev_is_jobs and s.kind == "key":
            out.append(Seg.keyvar("JOB"))
            prev_is_jobs = False
        else:
            out.append(s)
            prev_is_jobs = s.kind == "key" and s.name == "jobs"
    return out


# --- diff (from raw text, no blob dir needed) -------------------------------


def _flatten_text(text: str) -> dict:
    """Flatten YAML to identity-keyed leaf paths, via a YAML 1.2 safe load so
    `on`/`yes`/`no`/`off` stay strings — consistent with the ruamel loader
    `apply` uses (PyYAML 1.1 would turn `on:` into a boolean key)."""
    doc = load_safe(text)
    if doc is None:
        return {}
    out: dict = {}
    _flatten(doc, "", out)
    return out


def diff_texts(before_text: str, after_text: str):
    """(added, removed, changed) over flat identity-keyed paths.

    Same set algebra as extract_diff.diff_workflow_versions, but driven by text
    so the compiler is decoupled from the content-addressed blob store.
    """
    before = _flatten_text(before_text)
    after = _flatten_text(after_text)
    bk, ak = set(before), set(after)
    added = {k: after[k] for k in ak - bk}
    removed = {k: before[k] for k in bk - ak}
    changed = {k: (before[k], after[k]) for k in ak & bk if before[k] != after[k]}
    return added, removed, changed


# --- one leaf edit ----------------------------------------------------------


def _edit_for_leaf(path: str, op: str, value: Any = None, old: Any = None) -> Edit | None:
    segs = _metavar_jobs(parse_path(path))
    if not segs:
        return None
    last = segs[-1]
    anchor = Anchor(segs[:-1])

    # Editing a *list element itself* (adding/removing a list item, e.g. a whole
    # step or a branch string) is structural surgery we defer to v2.
    if last.kind != "key":
        return Edit(op=op, anchor=anchor, key=str(last), value=value,
                    expected_old=(_value_sketch(old) if old is not None else None),
                    review="list-element add/remove not yet supported")

    key = last.name

    # A `uses:` tag/branch -> 40-hex-SHA change is a version-aligned pin, not a
    # literal rewrite (we must pin the TARGET's major at apply time).
    if (op == REWRITE_VALUE and key == "uses"
            and isinstance(value, str) and isinstance(old, str)):
        action = value.rpartition("@")[0]
        new_ref = value.rpartition("@")[2]
        if action and _SHA_RE.match(new_ref):
            return Edit(op=REWRITE_VALUE, anchor=anchor, key=key,
                        pin=Pin(action=action),
                        expected_old=_value_sketch(old))

    return Edit(op=op, anchor=anchor, key=key, value=value,
                expected_old=(_value_sketch(old) if old is not None else None))


# --- top-level --------------------------------------------------------------


def compile_program(
    repository: str,
    commit_hash: str,
    source_file: str,
    before_text: str,
    after_text: str,
    target_idents: list[str],
    github_url: str = "",
) -> IRProgram:
    """Compile (before -> after) into an executable IRProgram."""
    added, removed, changed = diff_texts(before_text, after_text)
    edits: list[Edit] = []
    for p, v in sorted(added.items()):
        e = _edit_for_leaf(p, ENSURE_PRESENT, value=v)
        if e:
            edits.append(e)
    for p in sorted(removed):
        e = _edit_for_leaf(p, ENSURE_ABSENT)
        if e:
            edits.append(e)
    for p, (old, new) in sorted(changed.items()):
        e = _edit_for_leaf(p, REWRITE_VALUE, value=new, old=old)
        if e:
            edits.append(e)
    return IRProgram(
        repository=repository,
        commit_hash=commit_hash,
        source_file=source_file,
        target_idents=sorted(target_idents),
        edits=edits,
        github_url=github_url or (f"https://github.com/{repository}/commit/{commit_hash}"
                                  if repository and commit_hash else ""),
    )
