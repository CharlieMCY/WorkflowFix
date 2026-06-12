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

_MISSING = object()


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


# --- type-change consolidation ----------------------------------------------


def _walk_mapping_path(doc: Any, path: str) -> Any:
    """Walk a dotted mapping path through `doc`. Return value or `_MISSING`.

    Restricted to pure-dot paths (no list brackets); type-change consolidation
    only applies to mapping-to-mapping/scalar transitions where a single key's
    value type flipped between scalar and complex.
    """
    if "[" in path:
        return _MISSING
    if path == "":
        return doc if doc is not None else _MISSING
    node = doc
    for seg in path.split("."):
        if not isinstance(node, dict) or seg not in node:
            return _MISSING
        node = node[seg]
    return node


def _consolidate_type_changes(
    added: dict, removed: dict, before_doc: Any, after_doc: Any,
):
    """Detect scalar<->complex type changes and rewrite as parent-level edits.

    Without this, `secrets: inherit` -> `secrets: {DEPLOY_TOKEN: ...}` decomposes
    into (ensure_present DEPLOY_TOKEN under secrets) + (ensure_absent secrets) —
    and the absent wins, deleting the whole key. Consolidating the change into a
    single rewrite at the parent fixes it.

    Returns (added', removed', extra_changed).
    """
    extra_changed: dict[str, tuple[Any, Any]] = {}
    suppress_added: set[str] = set()
    suppress_removed: set[str] = set()

    # scalar -> complex: P in removed AND P.foo / P[..] in added
    for rpath, rval in list(removed.items()):
        if "[" in rpath:
            continue
        prefix_dot, prefix_brk = rpath + ".", rpath + "["
        children = [p for p in added
                    if p.startswith(prefix_dot) or p.startswith(prefix_brk)]
        if not children:
            continue
        new_val = _walk_mapping_path(after_doc, rpath)
        if new_val is _MISSING:
            continue
        extra_changed[rpath] = (rval, new_val)
        suppress_removed.add(rpath)
        suppress_added.update(children)

    # complex -> scalar: P in added AND P.foo / P[..] in removed
    for apath, aval in list(added.items()):
        if apath in suppress_added or "[" in apath:
            continue
        prefix_dot, prefix_brk = apath + ".", apath + "["
        old_children = [p for p in removed
                        if p.startswith(prefix_dot) or p.startswith(prefix_brk)]
        if not old_children:
            continue
        old_val = _walk_mapping_path(before_doc, apath)
        if old_val is _MISSING:
            old_val = "<complex>"           # sketch only; apply ignores it
        extra_changed[apath] = (old_val, aval)
        suppress_added.add(apath)
        suppress_removed.update(old_children)

    added2 = {k: v for k, v in added.items() if k not in suppress_added}
    removed2 = {k: v for k, v in removed.items() if k not in suppress_removed}
    return added2, removed2, extra_changed


# --- list-element addition detection ----------------------------------------


def _seg_eq(a: Seg, b: Seg) -> bool:
    """Equality for the purpose of "did this segment appear in before?"."""
    if a.kind != b.kind:
        return False
    if a.kind == "key":
        return a.name == b.name
    if a.kind == "list":
        return a.list_kind == b.list_kind and a.value == b.value
    return True                              # keyvar — only emitted at compile-time


def _list_segs_existed(paths: list[str]) -> list[list[Seg]]:
    """Pre-parse every path into its seg sequence for prefix matching."""
    return [parse_path(p) for p in paths]


def _anchor_list_seg_present(
    path: str, ref_segs_list: list[list[Seg]],
) -> bool:
    """True iff every list-identity segment in `path`'s anchor appears on a
    matching prefix in at least one reference path. False iff some list-seg
    has no matching prefix — meaning the list element doesn't exist in the
    reference state.
    """
    segs = parse_path(path)
    for i, seg in enumerate(segs):
        if seg.kind != "list":
            continue
        prefix = segs[: i + 1]
        ok = False
        for rsegs in ref_segs_list:
            if len(rsegs) < len(prefix):
                continue
            if all(_seg_eq(prefix[j], rsegs[j]) for j in range(len(prefix))):
                ok = True
                break
        if not ok:
            return False
    return True


def _path_introduces_new_list_element(
    path: str, before_segs_list: list[list[Seg]],
) -> bool:
    """True iff this added path requires inventing a new list element (v1 can't)."""
    return not _anchor_list_seg_present(path, before_segs_list)


def _path_removes_whole_list_element(
    path: str, after_segs_list: list[list[Seg]],
) -> bool:
    """True iff this removed path is part of a list element the maintainer
    deleted in its entirety (v1 can't synthesize element-level deletion;
    naively removing each key leaves a husk step that actionlint will reject).
    """
    return not _anchor_list_seg_present(path, after_segs_list)


# --- one leaf edit ----------------------------------------------------------


def _edit_for_leaf(
    path: str, op: str, value: Any = None, old: Any = None,
    review_reason: str = "",
) -> Edit | None:
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
                expected_old=(_value_sketch(old) if old is not None else None),
                review=review_reason)


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

    # 1) Consolidate scalar<->complex type changes into parent-level rewrites.
    #    Without this, e.g. `secrets: inherit` -> `secrets: {DEPLOY_TOKEN: ...}`
    #    decomposes into (ensure_present DEPLOY_TOKEN) + (ensure_absent secrets)
    #    and the absent silently wins, deleting the whole `secrets` key.
    before_doc = load_safe(before_text) or {}
    after_doc = load_safe(after_text) or {}
    added, removed, extra_changed = _consolidate_type_changes(
        added, removed, before_doc, after_doc,
    )
    for k, v in extra_changed.items():
        changed[k] = v                       # overrides any scalar->scalar entry

    # 2) Pre-compute which paths cross list-element boundaries so we can flag
    #    them at compile time (otherwise the engine produces silently-broken
    #    output that zizmor accepts but actionlint catches as broken YAML).
    #
    #      - added: anchor's list-seg doesn't exist in before  -> v1 would have
    #        to invent a new list element; apply will be "inapplicable".
    #      - removed: anchor's list-seg doesn't exist in after -> the whole
    #        list element was deleted by the maintainer. Removing each key
    #        individually leaves a husk step ({} with no `uses`/`run`), which
    #        actionlint will reject. v1 can't synthesize element-level deletion.
    before_segs_list = _list_segs_existed(list(_flatten_text(before_text).keys()))
    after_segs_list = _list_segs_existed(list(_flatten_text(after_text).keys()))

    edits: list[Edit] = []
    for p, v in sorted(added.items()):
        reason = ""
        if _path_introduces_new_list_element(p, before_segs_list):
            reason = "adds a new list element; v1 cannot insert into target's list"
        e = _edit_for_leaf(p, ENSURE_PRESENT, value=v, review_reason=reason)
        if e:
            edits.append(e)
    for p in sorted(removed):
        reason = ""
        if _path_removes_whole_list_element(p, after_segs_list):
            reason = ("removes a whole list element; "
                      "v1 cannot delete steps without leaving a husk")
        e = _edit_for_leaf(p, ENSURE_ABSENT, review_reason=reason)
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
