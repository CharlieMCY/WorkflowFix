"""Paths for the backport-IR pipeline.

Reuses pattern_miner's OUTPUT_DIR / BLOBS_DIR so the IR stage plugs straight
into the same `output/` tree the miner already populates (clean_fixes/, and the
backport_gaps/ gap tickets it later consumes). Because pattern_miner.config's
OUTPUT_DIR already honours DATASET_TAG, the IR stage automatically lands under
the same per-dataset subtree (e.g. output/10k/backport_ir/ vs
output/50k/backport_ir/).
"""
from __future__ import annotations

import os

from pattern_miner.config import BLOBS_DIR, OUTPUT_DIR, REPO_ROOT

IR_DIR = OUTPUT_DIR / "backport_ir"
PROGRAMS_DIR = IR_DIR / "programs"   # compiled IRProgram .json, one per source commit
PATCHES_DIR = IR_DIR / "patches"     # generated patched workflows + apply reports

# Inputs produced upstream (pattern_miner clean-fix dump; backport_gaps tickets).
# GAPS_VARIANT (env var) selects the gap-audit variant; default = full audit set,
# "structural" picks the §III-B-filtered subset (gaps_structural.jsonl).
CLEAN_FIXES_DIR = OUTPUT_DIR / "clean_fixes"
_GAPS_VARIANT = os.environ.get("GAPS_VARIANT", "").strip()
_SUFFIX = f"_{_GAPS_VARIANT}" if _GAPS_VARIANT else ""
GAPS_FILE = OUTPUT_DIR / "backport_gaps" / f"gaps{_SUFFIX}.jsonl"

__all__ = [
    "BLOBS_DIR",
    "OUTPUT_DIR",
    "REPO_ROOT",
    "IR_DIR",
    "PROGRAMS_DIR",
    "PATCHES_DIR",
    "CLEAN_FIXES_DIR",
    "GAPS_FILE",
]
