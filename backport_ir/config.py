"""Paths for the backport-IR pipeline.

Reuses pattern_miner's OUTPUT_DIR / BLOBS_DIR so the IR stage plugs straight
into the same `output/` tree the miner already populates (clean_fixes/, and the
backport_gaps/ gap tickets it later consumes).
"""
from __future__ import annotations

from pattern_miner.config import BLOBS_DIR, OUTPUT_DIR, REPO_ROOT

IR_DIR = OUTPUT_DIR / "backport_ir"
PROGRAMS_DIR = IR_DIR / "programs"   # compiled IRProgram .json, one per source commit
PATCHES_DIR = IR_DIR / "patches"     # generated patched workflows + apply reports

# Inputs produced upstream (pattern_miner clean-fix dump; backport_gaps tickets).
CLEAN_FIXES_DIR = OUTPUT_DIR / "clean_fixes"
GAPS_FILE = OUTPUT_DIR / "backport_gaps" / "gaps.jsonl"

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
