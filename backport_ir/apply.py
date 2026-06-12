"""Apply an IRProgram to a target workflow with format-preserving edits.

Uses ruamel.yaml in round-trip mode so comments, quoting, and indentation
survive — a backport PR full of reformatting noise won't get merged. ruamel is
imported lazily so the rest of the package (compile/match) stays usable without
it.

Edits are idempotent: an ensure that already holds is a no-op, so replaying a
program onto a partially-fixed branch converges instead of double-editing.

Pin resolution (tag/major -> SHA) is an injected callable so the core stays
offline-testable; the real backport pipeline passes a GitHub-backed resolver.
An unresolved pin is reported as `needs_review`, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Callable

from .ir import ENSURE_ABSENT, ENSURE_PRESENT, REWRITE_VALUE, Edit, IRProgram
from .match import _is_map, concrete_route, resolve

# (action, ref) -> 40-hex SHA, or None if it can't be resolved.
Resolver = Callable[[str, str], "str | None"]

_SHA_RE = re.compile(r"[0-9a-f]{40}")


def load(text: str):
    from ._yaml import rt_yaml

    y = rt_yaml()
    return y.load(text), y


def dump(data: Any, y) -> str:
    s = StringIO()
    y.dump(data, s)
    return s.getvalue()


def _new_map():
    from ruamel.yaml.comments import CommentedMap

    return CommentedMap()


def _realize_chain(container: Any, remaining: list) -> Any:
    """Create the missing mapping keys (all `key` Segs) and return the deepest map."""
    node = container
    for seg in remaining:               # guaranteed all kind=='key' by caller
        if seg.name not in node or not _is_map(node[seg.name]):
            node[seg.name] = _new_map()
        node = node[seg.name]
    return node


def _try_eol_comment(container: Any, key: str, text: str) -> None:
    if not text:
        return
    try:
        container.yaml_add_eol_comment(text, key)
    except Exception:                    # pragma: no cover - comments are best-effort
        pass


@dataclass
class EditOutcome:
    edit: str
    op: str
    status: str                          # applied|noop|created|inapplicable|needs_review
    sites: int = 0
    reason: str = ""
    site_paths: list[str] = field(default_factory=list)
    """Concrete YAML routes (zizmor format) this edit's anchor resolved to on
    the target. Populated for any non-inapplicable / non-review outcome so the
    per-edit-locality oracle can scope finding checks to where we actually
    operated. Empty list = anchor didn't resolve."""

    def to_dict(self) -> dict[str, Any]:
        d = {"edit": self.edit, "op": self.op, "status": self.status, "sites": self.sites}
        if self.reason:
            d["reason"] = self.reason
        if self.site_paths:
            d["site_paths"] = self.site_paths
        return d


@dataclass
class ApplyResult:
    patched_text: str
    target_idents: list[str]
    edits: list[EditOutcome] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(o.status in ("applied", "created") for o in self.edits)

    @property
    def needs_review(self) -> bool:
        return any(o.status == "needs_review" for o in self.edits)

    @property
    def fully_applied(self) -> bool:
        return bool(self.edits) and all(
            o.status in ("applied", "created", "noop") for o in self.edits
        )

    def summary(self) -> dict[str, Any]:
        from collections import Counter

        c = Counter(o.status for o in self.edits)
        return {
            "n_edits": len(self.edits),
            "by_status": dict(c),
            "changed": self.changed,
            "needs_review": self.needs_review,
            "fully_applied": self.fully_applied,
        }


def _site_for(m, edit: Edit) -> str:
    """Concrete YAML route of the edit's effective target on the patched tree.

    Combines the path actually walked during resolve (with metavariables
    bound to their concrete job names and list-segs resolved to indices)
    with any creatable mapping keys we would synthesise and finally the
    edit's own key, so the result names the exact leaf the edit affects.
    """
    full_path = list(m.path)
    for seg in m.remaining:              # creatable mapping chain (keys only)
        if seg.kind == "key":
            full_path.append(("key", seg.name))
    full_path.append(("key", edit.key))
    return concrete_route(full_path)


def _apply_edit(root: Any, edit: Edit, resolver: Resolver | None) -> EditOutcome:
    if edit.review:
        return EditOutcome(edit.describe(), edit.op, "needs_review", 0, edit.review)

    matches = [m for m in resolve(root, edit.anchor)
               if m.status in ("resolved", "creatable")]
    if not matches:
        return EditOutcome(edit.describe(), edit.op, "inapplicable", 0,
                           "anchor not found on target")

    sites = 0
    created = False
    reasons: list[str] = []
    review = False
    site_paths: list[str] = []

    for m in matches:
        cont = m.container
        if m.remaining:                  # creatable: only missing mapping keys
            if edit.op != ENSURE_PRESENT:
                # ensure_absent: already absent => satisfied; rewrite: nothing to do
                continue
            cont = _realize_chain(cont, m.remaining)
            created = True
        if m.weak:
            review = True
            reasons.append("weak anchor (run/anon/multi-match)")

        # Record the site we operated at (or considered) so the per-edit-
        # locality oracle can scope its finding checks to here.
        site_paths.append(_site_for(m, edit))

        if edit.op == ENSURE_PRESENT:
            if _is_map(cont) and cont.get(edit.key) != edit.value:
                cont[edit.key] = edit.value
                sites += 1

        elif edit.op == ENSURE_ABSENT:
            if _is_map(cont) and edit.key in cont:
                del cont[edit.key]
                sites += 1

        elif edit.op == REWRITE_VALUE:
            if not _is_map(cont) or edit.key not in cont:
                continue
            if edit.pin is not None:
                cur = cont.get(edit.key)
                ref = str(cur).rpartition("@")[2] if (isinstance(cur, str) and "@" in cur) else ""
                if _SHA_RE.fullmatch(ref):
                    continue            # already pinned on target -> nothing to do
                # Pin the TARGET's current ref to its SHA (zero version change),
                # not master's resolved SHA — that's the backport-safe semantics.
                sha = resolver(edit.pin.action, ref) if (resolver and ref) else None
                if not sha:
                    review = True
                    reasons.append(f"unresolved pin {edit.pin.action}@{ref or '?'}")
                    continue
                newval = f"{edit.pin.action}@{sha}"
                if cont.get(edit.key) != newval:
                    cont[edit.key] = newval
                    sites += 1
                    _try_eol_comment(cont, edit.key, ref)
            else:
                if cont.get(edit.key) != edit.value:
                    cont[edit.key] = edit.value
                    sites += 1

    if review:
        return EditOutcome(edit.describe(), edit.op, "needs_review", sites,
                           "; ".join(sorted(set(reasons))),
                           site_paths=site_paths)
    if sites and created:
        return EditOutcome(edit.describe(), edit.op, "created", sites,
                           site_paths=site_paths)
    if sites:
        return EditOutcome(edit.describe(), edit.op, "applied", sites,
                           site_paths=site_paths)
    return EditOutcome(edit.describe(), edit.op, "noop", 0, "already satisfied",
                       site_paths=site_paths)


def apply_program(
    program: IRProgram,
    target_text: str,
    resolver: Resolver | None = None,
) -> ApplyResult:
    """Replay `program` onto `target_text`. Returns patched text + per-edit report."""
    data, y = load(target_text)
    outcomes = [_apply_edit(data, e, resolver) for e in program.edits]
    patched = dump(data, y)
    return ApplyResult(patched_text=patched,
                       target_idents=list(program.target_idents),
                       edits=outcomes)
