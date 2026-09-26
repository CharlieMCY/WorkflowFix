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


def _job_matches_fp(jobval: Any, fingerprint: tuple) -> bool:
    """Does a job satisfy a discriminating fingerprint — i.e. carry every `uses=`
    action the fingerprint names? Used to recover a renamed job on the target."""
    uses_terms = [v for (f, v) in fingerprint if f == "uses"]
    if not uses_terms or not _is_map(jobval):
        return False
    steps = jobval.get("steps")
    if not _is_seq(steps):
        return False
    have = set()
    for st in steps:
        if _is_map(st) and isinstance(st.get("uses"), str):
            have.add(st["uses"].partition("@")[0])
    return all(v in have for v in uses_terms)


@dataclass
class AnchorMatch:
    """One resolution candidate for an anchor against the target tree."""

    container: Any                       # deepest reached map/seq node
    remaining: list[Seg]                 # segs not descended ([] => fully resolved)
    matched_step: Any = None             # nearest uses-step passed through (for pin)
    bindings: dict[str, str] = field(default_factory=dict)
    weak: bool = False                   # matched via weak identity -> needs review
    path: list[tuple[str, Any]] = field(default_factory=list)
    """Concrete path walked through the target tree. Each step is
    ('key', name) for a mapping key (with $JOB metavariables resolved to the
    bound name) or ('idx', i) for a list index. Used by the per-edit-locality
    oracle to localise zizmor findings to the YAML subtree this match points
    at."""

    @property
    def status(self) -> str:
        if not self.remaining:
            return "resolved"
        if all(s.kind == "key" for s in self.remaining):
            return "creatable"           # only mapping keys missing -> can build
        return "unresolvable"            # would require inventing a job/step


def concrete_route(path: list[tuple[str, Any]]) -> str:
    """Render a path captured during resolve() in zizmor's route format.

    zizmor's `_route_to_str` produces e.g. 'jobs.publish.steps[2].run' —
    keys joined by '.', indices appended with no separator. We match that
    exactly so the per-edit-locality oracle can string-prefix-compare paths.
    """
    parts: list[str] = []
    for kind, val in path:
        if kind == "key":
            parts.append(str(val))
        elif kind == "idx":
            parts.append(f"[{val}]")
    return ".".join(parts).replace(".[", "[")


def _bind_jobvar(seg: Seg, node: dict, m: "AnchorMatch", rest: list,
                 nxt: list) -> None:
    """v2 job-metavariable binding: literal pin first, then fingerprint recovery,
    gated by cardinality. ``bind one`` (the compiled default; emitted whenever the
    metavar carries a ``pin``) binds exactly one job — or holds ambiguous matches
    for review (weak) — and NEVER fans out. A pin-less keyvar (legacy ``$JOB`` /
    ``bind each``) keeps the old fan-to-every-job behaviour."""
    def emit(jobname: str, jobval: Any, weak: bool) -> None:
        b = dict(m.bindings)
        b[seg.var or "JOB"] = jobname
        nxt.append(AnchorMatch(jobval, rest, m.matched_step, b, weak,
                               m.path + [("key", jobname)]))

    if seg.key_pin:                          # bind one (pinned)
        if seg.key_pin in node and _is_map(node[seg.key_pin]):
            emit(seg.key_pin, node[seg.key_pin], m.weak)   # literal pin hit
            return
        if seg.fingerprint:                  # pinned job renamed -> recover
            hits = [(k, v) for k, v in node.items()
                    if _is_map(v) and _job_matches_fp(v, seg.fingerprint)]
            weak = m.weak or len(hits) > 1   # ambiguous (>1) -> held for review
            for k, v in hits:
                emit(k, v, weak)
        # no fingerprint / zero hits -> drop: inapplicable, never invented
        return

    for jobname, jobval in node.items():     # legacy / bind each -> fan out
        if _is_map(jobval):
            emit(jobname, jobval, m.weak)


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
                                           dict(m.bindings), m.weak,
                                           m.path + [("key", seg.name)]))
                else:
                    # can't descend; freeze here (creatable/unresolvable by remaining)
                    results.append(m)

            elif seg.kind == "keyvar":
                if _is_map(node):
                    _bind_jobvar(seg, node, m, rest, nxt)
                # no job to bind -> candidate dies (never invent a job)

            elif seg.kind == "list":
                if _is_seq(node):
                    hits = [(i, e) for i, e in enumerate(node)
                            if step_identity_matches(e, seg.list_kind, seg.value)]
                    weak = m.weak or seg.list_kind in ("run", "anon") or len(hits) > 1
                    for i, e in hits:
                        ms = e if (seg.list_kind == "uses" and _is_map(e)) else m.matched_step
                        nxt.append(AnchorMatch(e, rest, ms, dict(m.bindings), weak,
                                               m.path + [("idx", i)]))
                # no hit -> candidate dies (never invent a step)

        frontier = nxt

    return results
