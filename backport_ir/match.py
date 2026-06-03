"""Resolve an IR `Anchor` against a (drifted) target workflow tree.

Pure structural navigation over plain dict/list, so it runs identically on
ruamel `CommentedMap`/`CommentedSeq` (used when we actually edit) and on PyYAML
dict/list (used in tests). This is the piece that absorbs structural drift
between master and a release branch:

  * a `$JOB` metavariable tries *every* job;
  * a list `Seg` matches steps by `uses=`/`id=`/`name=` identity, so reordering
    and unrelated step insertions don't break the match;
  * a missing mapping key stops descent but is recoverable (`creatable`) for
    `ensure_present`; a missing job/step is not (`unresolvable`) — we never
    invent a job or a step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ir import Anchor, Seg


def _is_map(x: Any) -> bool:
    return isinstance(x, dict)


def _is_seq(x: Any) -> bool:
    return isinstance(x, list)


def _repr_scalar(x: Any) -> str:
    return repr(x)


def step_identity_matches(elem: Any, kind: str, value: str) -> bool:
    """Does a list element match a list-identity Seg? Mirrors extract_diff keys."""
    if kind in ("uses", "id", "name"):
        if not _is_map(elem):
            return False
        v = elem.get(kind)
        if not isinstance(v, str):
            return False
        if kind == "uses":
            v = v.partition("@")[0]
        return v == value
    if kind == "run":
        return _is_map(elem) and "run" in elem
    if kind == "str":
        return isinstance(elem, str) and elem == value
    if kind == "scalar":
        return not _is_map(elem) and not _is_seq(elem) and _repr_scalar(elem) == value
    return False  # anon — not reliably matchable across drift


@dataclass
class AnchorMatch:
    """One resolution candidate for an anchor against the target tree."""

    container: Any                       # deepest reached map/seq node
    remaining: list[Seg]                 # segs not descended ([] => fully resolved)
    matched_step: Any = None             # nearest uses-step passed through (for pin)
    bindings: dict[str, str] = field(default_factory=dict)
    weak: bool = False                   # matched via weak identity -> needs review

    @property
    def status(self) -> str:
        if not self.remaining:
            return "resolved"
        if all(s.kind == "key" for s in self.remaining):
            return "creatable"           # only mapping keys missing -> can build
        return "unresolvable"            # would require inventing a job/step


def resolve(root: Any, anchor: Anchor) -> list[AnchorMatch]:
    """Return every terminal resolution candidate for `anchor` under `root`."""
    results: list[AnchorMatch] = []
    frontier: list[AnchorMatch] = [AnchorMatch(container=root, remaining=list(anchor.segs))]

    while frontier:
        nxt: list[AnchorMatch] = []
        for m in frontier:
            if not m.remaining:
                results.append(m)
                continue
            seg, rest, node = m.remaining[0], m.remaining[1:], m.container

            if seg.kind == "key":
                if _is_map(node) and seg.name in node:
                    nxt.append(AnchorMatch(node[seg.name], rest, m.matched_step,
                                           dict(m.bindings), m.weak))
                else:
                    # can't descend; freeze here (creatable/unresolvable by remaining)
                    results.append(m)

            elif seg.kind == "keyvar":
                if _is_map(node):
                    for jobname, jobval in node.items():
                        if _is_map(jobval):
                            b = dict(m.bindings)
                            b[seg.var] = jobname
                            nxt.append(AnchorMatch(jobval, rest, m.matched_step, b, m.weak))
                # no job to bind -> candidate dies (never invent a job)

            elif seg.kind == "list":
                if _is_seq(node):
                    hits = [e for e in node
                            if step_identity_matches(e, seg.list_kind, seg.value)]
                    weak = m.weak or seg.list_kind in ("run", "anon") or len(hits) > 1
                    for e in hits:
                        ms = e if (seg.list_kind == "uses" and _is_map(e)) else m.matched_step
                        nxt.append(AnchorMatch(e, rest, ms, dict(m.bindings), weak))
                # no hit -> candidate dies (never invent a step)

        frontier = nxt

    return results
