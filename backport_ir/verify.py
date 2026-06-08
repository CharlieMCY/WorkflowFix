"""Verify a generated backport patch — two layers, mirroring the design split.

RUNTIME (cheap, no scanner) — structural post-conditions.
    After apply, re-parse the patched text and assert each *applied* edit's
    target state actually landed. This catches apply-engine bugs (anchor matched
    nothing / wrong node / write silently dropped) WITHOUT a semantic rescan,
    because "did this edit land" is structurally decidable. Whether landing it
    fixes the vulnerability is already guaranteed by the source being a clean
    fix, so re-running zizmor here would be redundant.

EVAL (the zizmor oracle) — semantic acceptance, for measuring the engine.
    Rescan (target-before, patched) and confirm the program's `target_idents`
    disappear (V_fixed) and nothing new appears (V_introduced == ∅) — exactly
    pattern_miner's clean-fix criterion, reused as automated acceptance. This is
    the only *independent* evidence the backport is semantically correct, and
    what engine accuracy should be reported against. It is NOT run per-patch at
    generation time; it runs when you score the engine on a benchmark.
"""
from __future__ import annotations

import re
from typing import Any

from ._yaml import load_safe
from .apply import ApplyResult
from .ir import ENSURE_ABSENT, ENSURE_PRESENT, REWRITE_VALUE, Edit, IRProgram
from .match import _is_map, resolve

_PINNED_RE = re.compile(r"@[0-9a-f]{40}$")


# --- layer 1: structural post-conditions ------------------------------------


def _holds(edit: Edit, root: Any) -> bool:
    """Does the patched tree satisfy `edit`'s intended end-state?"""
    matches = [m for m in resolve(root, edit.anchor) if m.status == "resolved"]
    if edit.op == ENSURE_PRESENT:
        if not matches:
            return False
        return all(_is_map(m.container) and m.container.get(edit.key) == edit.value
                   for m in matches)
    if edit.op == ENSURE_ABSENT:
        # Satisfied if no resolved container still carries the key.
        return all(not (_is_map(m.container) and edit.key in m.container)
                   for m in matches)
    if edit.op == REWRITE_VALUE:
        if not matches:
            return False
        if edit.pin is not None:
            return all(
                isinstance(m.container.get(edit.key), str)
                and m.container.get(edit.key).startswith(edit.pin.action + "@")
                and bool(_PINNED_RE.search(m.container.get(edit.key)))
                for m in matches if _is_map(m.container)
            )
        return all(_is_map(m.container) and m.container.get(edit.key) == edit.value
                   for m in matches)
    return False


def check_postconditions(
    program: IRProgram,
    patched_text: str,
    apply_result: ApplyResult,
) -> dict[str, Any]:
    """Assert every *applied* edit landed in the patched text. Cheap, no zizmor."""
    root = load_safe(patched_text)
    if root is None:
        return {"ok": False, "error": "patched YAML did not parse", "checks": []}

    checks: list[dict[str, Any]] = []
    all_pass = True
    for edit, outcome in zip(program.edits, apply_result.edits):
        if outcome.status in ("needs_review", "inapplicable"):
            checks.append({"edit": edit.describe(), "status": "skipped",
                           "reason": outcome.status})
            continue
        ok = _holds(edit, root)
        all_pass = all_pass and ok
        checks.append({"edit": edit.describe(), "status": "pass" if ok else "FAIL"})

    return {"ok": all_pass, "checks": checks}


# --- layer 2: zizmor oracle (eval / benchmark) ------------------------------


def zizmor_oracle(
    program: IRProgram,
    target_before_text: str,
    patched_text: str,
) -> dict[str, Any]:
    """Semantic acceptance: did the patch remove the target idents w/o new ones?

    Reuses pattern_miner.scan (needs zizmor installed). Returns a verdict dict;
    `success` is the strict clean-backport criterion.
    """
    from pattern_miner.scan import diff_findings, scan_bytes

    before = scan_bytes(target_before_text.encode("utf-8", "replace"))
    after = scan_bytes(patched_text.encode("utf-8", "replace"))
    if not before.get("ok"):
        return {"status": "scan_error", "where": "before", "error": before.get("error")}
    if not after.get("ok"):
        return {"status": "scan_error", "where": "after", "error": after.get("error")}

    fixed, introduced = diff_findings(before["findings"], after["findings"])
    fixed_idents = {f["ident"] for f in fixed}
    introduced_idents = {f["ident"] for f in introduced}
    targets = set(program.target_idents)

    resolved = targets & fixed_idents
    missed = targets - fixed_idents
    return {
        "status": "ok",
        "target_idents": sorted(targets),
        "resolved_idents": sorted(resolved),
        "missed_idents": sorted(missed),
        "introduced_idents": sorted(introduced_idents),
        "success": bool(resolved) and not missed and not introduced_idents,
    }
