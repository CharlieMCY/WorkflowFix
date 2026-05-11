"""Match candidate commits against the pattern catalog.

Given a clean-fix commit and a pattern catalog (output of `patterns`
subcommand), determine whether the commit is an instance of a known pattern.

A match is two-leveled, mirroring how the catalog was built:

  - LEVEL 1 (V_fixed_idents): does the commit's fixed-rule set equal any
    pattern's `fixes`? Two commits match at level 1 iff zizmor reports the
    SAME SET of rule types disappearing.
  - LEVEL 2 (template_hash): if level 1 matches, does the commit's combined
    structural template hash equal any of that pattern's sub-cluster hashes?

Three outcomes per commit:
    "full"     level 1 + level 2 both match (a known pattern's known variant)
    "level-1"  level 1 hits, level 2 misses (known pattern type, new shape)
    "miss"     level 1 misses                (new pattern type entirely)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cluster import commit_template, template_hash


def load_pattern_index(patterns_path: Path) -> dict[frozenset[str], dict]:
    """Read patterns.jsonl into a lookup keyed by frozenset(fixes)."""
    index: dict[frozenset[str], dict] = {}
    with patterns_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            p = json.loads(line)
            index[frozenset(p["fixes"])] = p
    return index


def match_commit(commit: dict[str, Any],
                 pattern_index: dict[frozenset[str], dict]) -> dict[str, Any]:
    """Match one per-commit aggregate against the pattern index.

    `commit` must have:
      - V_fixed_idents: list[str]  (sorted unique zizmor rule names)
      - diffs:          list[WorkflowDiff]
    """
    fixes = frozenset(commit.get("V_fixed_idents") or [])
    tmpl = commit_template(commit["diffs"])
    h = template_hash(tmpl)

    if fixes not in pattern_index:
        return {
            "outcome": "miss",
            "fixes": sorted(fixes),
            "template_hash": h,
            "matched_pattern": None,
            "matched_subcluster": None,
        }

    pattern = pattern_index[fixes]
    for sub in pattern["structural_subclusters"]:
        if sub["template_hash"] == h:
            return {
                "outcome": "full",
                "fixes": sorted(fixes),
                "template_hash": h,
                "matched_pattern": list(pattern["fixes"]),
                "matched_subcluster": sub["template_hash"],
            }
    return {
        "outcome": "level-1",
        "fixes": sorted(fixes),
        "template_hash": h,
        "matched_pattern": list(pattern["fixes"]),
        "matched_subcluster": None,
    }
