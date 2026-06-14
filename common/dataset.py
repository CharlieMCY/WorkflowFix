"""Dataset-tagged output directory resolution.

Reads the env var DATASET_TAG (or accepts an explicit override) and
returns the directory each pipeline stage should write to.

  DATASET_TAG unset       output_dir() -> <repo>/output
  DATASET_TAG=10k         output_dir() -> <repo>/output/10k
  DATASET_TAG=50k         output_dir() -> <repo>/output/50k

This keeps the historical layout (everything directly under output/)
working by default, and only nests when the user opts into a tag. To
preserve an existing untagged run as `50k`, the safe migration is to
set DATASET_TAG=50k from now on for the new dataset and leave the
existing untagged run where it is, or do an explicit `mv` once:

  mv output output_default && mkdir output && \
      mv output_default output/default

Stage modules call output_dir()/reports_dir() at import time, so the
DATASET_TAG must be set BEFORE the pipeline command starts:

  DATASET_TAG=10k .venv/bin/python -m pattern_miner pipeline ...

Changing it mid-run does not affect already-imported modules.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_VAR = "DATASET_TAG"


def current_tag() -> str | None:
    """Return the current DATASET_TAG, or None if unset / empty."""
    tag = os.environ.get(ENV_VAR, "").strip()
    return tag or None


def output_dir(tag: str | None = None) -> Path:
    """Directory all pipeline stages write to for this dataset.

    `tag` overrides the env var. Created on demand.
    """
    tag = tag if tag is not None else current_tag()
    p = REPO_ROOT / "output"
    if tag:
        p = p / tag
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir(tag: str | None = None) -> Path:
    """Directory analysis_tools writes evaluation tables to."""
    tag = tag if tag is not None else current_tag()
    p = REPO_ROOT / "analysis_tools" / "reports"
    if tag:
        p = p / tag
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    """Root of the dataset-independent cache (see common.cache)."""
    p = REPO_ROOT / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p
