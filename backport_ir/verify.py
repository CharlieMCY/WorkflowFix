"""Verify a generated backport patch.

Two kinds of checks live here, with very different roles:

ENGINE SELF-TESTS (development / QA — NOT a patch-quality claim).
    `check_postconditions` re-parses the patched text and asserts each
    *applied* edit's target state actually landed. This catches apply-engine
    bugs (anchor matched nothing / wrong node / write silently dropped) but
    it cannot tell you whether the patch FIXES the vulnerability or keeps
    the workflow working — it only verifies that the engine did what it said
    it did. Keep this for regression-catching when changing the apply engine;
    do NOT report it as a paper-grade correctness number.

EXTERNAL ORACLES (the only judgments that don't know about backport_ir).
    Two scanners run on (target-before, patched), reused as backport acceptance:

      * `zizmor_oracle`     — security: are the targeted findings gone, and
                              no new findings introduced?  Reuses
                              pattern_miner's clean-fix criterion symmetrically.
      * `actionlint_oracle` — schema / liveness: does the patched workflow
                              still pass actionlint without introducing new
                              complaints?  This is the strongest static proxy
                              for "the workflow still works" — runtime CI
                              verification would require the actual project
                              infrastructure and is out of scope.

    These two are independent of each other and independent of the IR.
    A backport is paper-claim-correct iff BOTH pass.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ._yaml import load_safe
from .apply import ApplyResult
from .ir import ENSURE_ABSENT, ENSURE_PRESENT, REWRITE_VALUE, Edit, IRProgram
from .match import _is_map, resolve

_PINNED_RE = re.compile(r"@[0-9a-f]{40}$")

# actionlint entry point in the same venv as this module.
_ACTIONLINT = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "actionlint"


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

    Symmetric semantics with pattern_miner's clean-fix criterion.

    pattern_miner labels a master commit a "clean fix" iff it removes at
    least one finding (V_fixed != ∅) and introduces none (V_introduced = ∅).
    The backport oracle applies the *same* definition to the release-branch
    transition: the patch is accepted iff it removes at least one finding
    of a rule master targeted on the release branch, and introduces none.

    This matters because a single master commit often does not exhaustively
    eliminate every instance of every rule on its own master state — it
    counts as a "clean fix" by removing one instance. Asking the backport
    to do *more* on the release branch than master did on master is
    asymmetric and would penalise correctly-applied patches that simply
    leave behind release-branch-only instances of the same rule that master
    never targeted.

    The program's `target_idents` is the set of rules master fixed; the
    release branch typically carries only a subset of those (the rest were
    never introduced on that branch, or were already fixed independently).
    `relevant_targets` is the intersection — what the backport could
    actually act on. The patch is `success` iff at least one of those was
    reduced (`resolved` non-empty) and no new finding was introduced.

    `missed_idents` (relevant targets where no instance was removed) and
    `not_present_targets` (master targets the release branch never carried)
    are still reported as diagnostics, but they do NOT contribute to the
    success verdict.
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

    # Only judge against target idents the target actually carried — see docstring.
    present_before = {f["ident"] for f in before["findings"]}
    relevant_targets = targets & present_before
    not_present_targets = targets - present_before
    resolved = relevant_targets & fixed_idents
    missed = relevant_targets - fixed_idents

    return {
        "status": "ok",
        "target_idents": sorted(targets),
        "relevant_targets": sorted(relevant_targets),
        "not_present_targets": sorted(not_present_targets),
        "resolved_idents": sorted(resolved),
        "missed_idents": sorted(missed),
        "introduced_idents": sorted(introduced_idents),
        # Symmetric with pattern_miner's V_fixed != ∅ ∧ V_introduced = ∅:
        # at least one relevant target reduced, nothing new introduced.
        "success": bool(resolved) and not introduced_idents,
    }


# --- per-edit-locality oracle ----------------------------------------------


def _scope_prefix(route: str) -> str:
    """Return the deepest 'step / job / root' boundary for a YAML route.

    The locality oracle judges each landed edit only on findings within its
    workflow-semantic neighbourhood. zizmor's rules report at varying
    granularity — artipacked at a step, excessive-permissions at a job or
    root, unpinned-uses at a uses line — so we collapse a route to the
    nearest enclosing scope unit before comparison:

        jobs.X.steps[2].run            -> jobs.X.steps[2]
        jobs.X.steps[2]                -> jobs.X.steps[2]
        jobs.X.permissions.contents    -> jobs.X
        jobs.X                         -> jobs.X
        permissions.contents           -> '' (root)
        permissions                    -> '' (root)
    """
    # deepest .steps[N] gives the step scope
    m = re.search(r"^(.*?\.steps\[\d+\])", route)
    if m:
        return m.group(1)
    # else the deepest jobs.X gives the job scope
    m = re.match(r"^(jobs\.[^.\[]+)", route)
    if m:
        return m.group(1)
    # else everything's at the root
    return ""


def _route_in_scope(route: str, scope: str) -> bool:
    """True iff `route` lies within `scope` (root scope contains everything)."""
    if not scope:
        return True
    return route == scope or route.startswith(scope + ".") or route.startswith(scope + "[")


def zizmor_oracle_local(
    program: IRProgram,
    target_before_text: str,
    patched_text: str,
    apply_result: ApplyResult,
) -> dict[str, Any]:
    """Per-edit-locality oracle: did each landed edit fix its own site?

    Stricter than `zizmor_oracle` in one direction and looser in another:

      * stricter — every edit that landed (applied / created / noop, not
        weak) must leave its own scope free of any target_idents finding.
        A "noop because the target was already in the right state" only
        counts as success if the site is actually clean post-patch.

      * looser — findings at scopes the patch never operated on don't
        count against the verdict.  This matches the design intent: a
        backport reproduces master's fix at the master-targeted construct
        on the release branch; the release branch may carry additional
        independent instances of the same rule that master never addressed,
        and judging the patch on those would be a scope mismatch.

    Returns a verdict dict with diagnostic fields:
      success                — bool, at-least-one-edit landed AND no edit
                               failed locally AND no new findings appeared
                               within any landed-edit scope
      landed_paths           — concrete YAML routes the patch operated at
      failed_edits           — [(scope, finding)] pairs where a landed edit's
                               scope still carries one of its target_idents
      introduced_in_scope    — [(scope, finding)] pairs newly appearing within
                               a landed-edit scope (a regression caused by us)
    """
    from pattern_miner.scan import scan_bytes

    before = scan_bytes(target_before_text.encode("utf-8", "replace"))
    after = scan_bytes(patched_text.encode("utf-8", "replace"))
    if not before.get("ok"):
        return {"status": "scan_error", "where": "before", "error": before.get("error")}
    if not after.get("ok"):
        return {"status": "scan_error", "where": "after", "error": after.get("error")}

    targets = set(program.target_idents)

    # Only edits whose anchor actually resolved on the target are landed.
    # weak / inapplicable / review outcomes get no credit (and no blame).
    landed_scopes: list[str] = []
    for o in apply_result.edits:
        if o.status not in ("applied", "created", "noop"):
            continue
        for site in o.site_paths or []:
            landed_scopes.append(_scope_prefix(site))
    # De-dup adjacent identical scopes; preserve order otherwise.
    landed_scopes = list(dict.fromkeys(landed_scopes))

    if not landed_scopes:
        return {
            "status": "ok",
            "success": False,
            "reason": "no_landed_edits",
            "landed_paths": [],
            "failed_edits": [],
            "introduced_in_scope": [],
        }

    def _in_any_scope(route: str) -> str | None:
        for sc in landed_scopes:
            if _route_in_scope(route, sc):
                return sc
        return None

    # For each landed scope, does the patched state still carry one of the
    # target_idents within it?  If yes, that scope failed locally.
    failed: list[tuple[str, dict[str, Any]]] = []
    for f in after["findings"]:
        if f.get("ident") not in targets:
            continue
        sc = _in_any_scope(f.get("route", ""))
        if sc is not None:
            failed.append((sc, {"ident": f["ident"], "route": f.get("route", "")}))

    # Regression check, restricted to landed scopes. A finding outside any
    # landed scope is pre-existing release-branch state we never claimed to
    # touch and is not our responsibility.
    before_keys = {(f.get("ident"), f.get("route", "")) for f in before["findings"]}
    introduced_in_scope: list[tuple[str, dict[str, Any]]] = []
    for f in after["findings"]:
        key = (f.get("ident"), f.get("route", ""))
        if key in before_keys:
            continue
        sc = _in_any_scope(f.get("route", ""))
        if sc is not None:
            introduced_in_scope.append((sc, {"ident": key[0], "route": key[1]}))

    return {
        "status": "ok",
        "landed_paths": landed_scopes,
        "failed_edits": [{"scope": sc, "finding": f} for sc, f in failed],
        "introduced_in_scope": [{"scope": sc, "finding": f} for sc, f in introduced_in_scope],
        "success": not failed and not introduced_in_scope,
    }


# --- external oracle: actionlint (workflow still works) --------------------


def _actionlint_scan_bytes(content: bytes, timeout: int = 30) -> dict[str, Any]:
    """Run actionlint on an in-memory YAML byte string.

    Returns:
        {"ok": True,  "findings": [{"kind": ..., "message": ...}, ...]}  on success
        {"ok": False, "error": "..."}                                    on failure

    actionlint exit code is 0 even when findings exist (it uses stderr for
    real errors, stdout for results), so we treat any non-zero return code
    with no parseable JSON as an error.
    """
    try:
        proc = subprocess.run(
            [str(_ACTIONLINT), "-format", "{{json .}}", "-no-color", "-"],
            input=content,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": f"actionlint binary not found at {_ACTIONLINT}"}

    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        # actionlint may print nothing when there are no findings — still ok.
        if proc.returncode == 0:
            return {"ok": True, "findings": []}
        return {"ok": False,
                "error": (proc.stderr or b"").decode("utf-8", "replace")[:200]}

    try:
        raw = json.loads(out)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"json: {e}"}

    # Keep only the dimensions stable across edits: rule kind + message.
    # Line/column/snippet shift when we add lines, so they would create
    # false "introduced" matches; we deliberately drop them.
    findings = [{"kind": f.get("kind", ""), "message": f.get("message", "")}
                for f in raw]
    return {"ok": True, "findings": findings}


def _lint_key(f: dict) -> tuple[str, str]:
    return (f.get("kind", ""), f.get("message", ""))


def actionlint_oracle(
    target_before_text: str,
    patched_text: str,
) -> dict[str, Any]:
    """Schema / liveness acceptance via actionlint.

    Strongest static proxy for "the workflow still works at the GHA-schema
    level". A backport passes iff actionlint introduces no new findings
    relative to target_before — symmetric to the zizmor oracle's
    V_introduced = ∅ condition, but for workflow-level lint rather than
    security findings.

    Findings are identified by (kind, message) only: line/column shift when
    we insert lines so they would create spurious "introduced" entries.
    """
    before = _actionlint_scan_bytes(target_before_text.encode("utf-8", "replace"))
    after = _actionlint_scan_bytes(patched_text.encode("utf-8", "replace"))
    if not before.get("ok"):
        return {"status": "scan_error", "where": "before", "error": before.get("error")}
    if not after.get("ok"):
        return {"status": "scan_error", "where": "after", "error": after.get("error")}

    bset = {_lint_key(f) for f in before["findings"]}
    aset = {_lint_key(f) for f in after["findings"]}
    introduced = aset - bset
    removed = bset - aset

    return {
        "status": "ok",
        "n_before": len(before["findings"]),
        "n_after": len(after["findings"]),
        "introduced": [{"kind": k, "message": m} for (k, m) in sorted(introduced)],
        "removed": [{"kind": k, "message": m} for (k, m) in sorted(removed)],
        "success": not introduced,
    }
