"""Verbatim copy-paste baseline: apply master's textual diff to target.

The naive thing a maintainer would try first: `git diff` master's fix,
then `git apply --3way` against the release branch's current state.
This baseline measures how often that just works.

The implementation uses Python's `difflib` to compute a unified diff
from (source_before, source_after) and then a tiny 3-way merge by
context lines. We don't shell out to `git` because we want the same
oracle interface as the other baselines and need to handle non-Git
inputs (raw strings).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class CopyPasteResult:
    patched_text: str
    applied: bool
    reason: str = ""


def apply(source_before: str, source_after: str, target_before: str) -> CopyPasteResult:
    """Apply the (source_before -> source_after) diff to target_before.

    Strategy: extract the lines that changed (context + replacements) from
    the source diff, and for each removed-line block find the same content
    in target_before and substitute the added lines.

    Returns CopyPasteResult.applied=True iff every changed block found a
    unique pre-image in target_before. Otherwise reports the first failure
    reason.
    """
    if source_before == source_after:
        return CopyPasteResult(target_before, True, "no source change")

    src_before_lines = source_before.splitlines(keepends=True)
    src_after_lines = source_after.splitlines(keepends=True)
    tgt_lines = list(target_before.splitlines(keepends=True))

    # Iterate the diff opcodes; for each delete+insert block, locate the
    # delete sequence in the current target and replace.
    matcher = difflib.SequenceMatcher(None, src_before_lines, src_after_lines)
    cursor = 0  # current write position in tgt_lines after prior splices

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_block = src_before_lines[i1:i2]
        new_block = src_after_lines[j1:j2]

        if not old_block:
            # Pure insertion. Without alignment context, we can't safely
            # decide where the new lines belong on a drifted target. Bail.
            return CopyPasteResult(target_before, False,
                                   "pure insertion without context anchor")

        # Find the old block in the remaining tail of the target.
        found = -1
        for k in range(cursor, len(tgt_lines) - len(old_block) + 1):
            if tgt_lines[k:k + len(old_block)] == old_block:
                if found >= 0:
                    return CopyPasteResult(target_before, False,
                                           "ambiguous: multiple matches for hunk")
                found = k
        if found < 0:
            return CopyPasteResult(target_before, False,
                                   "no match for hunk on target (drift)")

        tgt_lines[found:found + len(old_block)] = new_block
        cursor = found + len(new_block)

    return CopyPasteResult("".join(tgt_lines), True)
