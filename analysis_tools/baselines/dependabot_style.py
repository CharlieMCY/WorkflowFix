"""Dependabot-style baseline: extract only uses: upgrades from the source
diff and apply each as a single-dependency update on the target.

This mirrors what an updater bot would do: see master pinned
`actions/checkout` to a SHA, generate a PR that pins the target's
checkout the same way --- and nothing else. Couplings to permissions
or persist-credentials fall outside its model.

The implementation walks the source `(before, after)` diff at the YAML
level (via pattern_miner._flatten), filters to changes whose terminal
key is `uses`, and for each one looks up the same `uses` field on the
target. Because this baseline can only express dep-bumps, it never
adds new keys (`permissions:`, `with:`, etc.) and never deletes
existing ones --- those are silently ignored, exactly as a real
single-dep updater would.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DependabotResult:
    patched_text: str
    n_uses_changes_in_source: int
    n_applied_on_target: int
    skipped_non_uses: int = 0
    notes: list[str] = field(default_factory=list)


def apply(source_before: str, source_after: str, target_before: str) -> DependabotResult:
    """Apply only the `uses:` field upgrades from source onto target."""
    from backport_ir._yaml import load_safe, rt_yaml
    from io import StringIO

    sb = load_safe(source_before) or {}
    sa = load_safe(source_after) or {}

    # Collect (action_name, old_ref, new_ref) for every step whose uses changed
    uses_changes = _collect_uses_changes(sb, sa)
    n_uses = len(uses_changes)

    # Count source diffs that AREN'T uses changes -> the baseline ignores them
    skipped = _count_non_uses_changes(sb, sa)

    # Apply on the target with a format-preserving round-trip
    y = rt_yaml()
    target_tree = y.load(target_before)
    applied = _apply_uses_changes_inplace(target_tree, uses_changes)

    buf = StringIO()
    y.dump(target_tree, buf)
    patched = buf.getvalue()

    return DependabotResult(
        patched_text=patched,
        n_uses_changes_in_source=n_uses,
        n_applied_on_target=applied,
        skipped_non_uses=skipped,
    )


def _collect_uses_changes(before, after) -> list[tuple[str, str, str]]:
    """Return [(action_without_ref, before_ref, after_ref), ...] for every
    `uses:` field whose value changed across (before, after) anywhere in the
    workflow tree."""
    before_uses = dict(_iter_uses(before))
    after_uses = dict(_iter_uses(after))
    out = []
    for path, after_val in after_uses.items():
        before_val = before_uses.get(path)
        if before_val is None or before_val == after_val:
            continue
        a_action, _, a_ref = after_val.partition("@")
        b_action, _, b_ref = before_val.partition("@")
        if a_action != b_action:
            continue  # the action itself was renamed, not a pure ref upgrade
        out.append((a_action, b_ref, a_ref))
    return out


def _iter_uses(node, path=()):
    """Yield (path_tuple, uses_value) for every `uses:` field in a workflow."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "uses" and isinstance(v, str):
                yield (path + ("uses",)), v
            else:
                yield from _iter_uses(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_uses(v, path + (i,))


def _count_non_uses_changes(before, after) -> int:
    """Cheap heuristic: count diff paths whose terminal key is NOT uses."""
    from pattern_miner.extract_diff import _flatten
    bf, af = {}, {}
    _flatten(before, "", bf)
    _flatten(after, "", af)
    bk, ak = set(bf), set(af)
    n = 0
    for p in (ak - bk) | (bk - ak) | {p for p in ak & bk if bf.get(p) != af.get(p)}:
        if not p.endswith(".uses"):
            n += 1
    return n


def _apply_uses_changes_inplace(node, changes):
    """Walk the target tree; replace every `uses:` whose action matches a
    source-side change so that the target's own ref gets the SHA-pin."""
    if not changes:
        return 0
    actions_to_new_ref = {action: new_ref for action, _, new_ref in changes}
    applied = 0

    def walk(n):
        nonlocal applied
        if isinstance(n, dict):
            for k, v in list(n.items()):
                if k == "uses" and isinstance(v, str):
                    action, _, ref = v.partition("@")
                    new_ref = actions_to_new_ref.get(action)
                    if new_ref and ref != new_ref:
                        n[k] = f"{action}@{new_ref}"
                        applied += 1
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return applied
