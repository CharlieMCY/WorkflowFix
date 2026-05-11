"""Sample candidate workflow-modifying commits from the Gigawork CSV.

The CSV has one row per (commit, workflow file) pair. We:
  1. Keep only modifications (git_change_type == 'M') of valid workflow YAML.
  2. Group rows by (repository, commit_hash) so each commit appears once even
     when it touches multiple workflow files; per-commit we keep the list of
     (file_path, file_hash, previous_file_hash) tuples.
  3. Stream-shuffle by hashing the commit id, so sampling is deterministic
     given a seed and we never need to load the full 1.5 GB CSV into memory.

Output: a parquet file with one row per sampled commit and a list-of-structs
column listing the workflow files modified in that commit.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import polars as pl

from .config import CSV_PATH, OUTPUT_DIR


def _hash_to_unit(commit_hash: str, seed: int) -> float:
    """Deterministic float in [0, 1) from (commit_hash, seed)."""
    h = hashlib.blake2b(f"{seed}:{commit_hash}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64


def sample_commits(
    n_commits: int,
    seed: int = 42,
    csv_path: Path = CSV_PATH,
    out_path: Path | None = None,
) -> Path:
    """Stream the CSV, keep modifications, group by commit, write `n_commits` rows.

    Returns the parquet path.
    """
    out_path = out_path or (OUTPUT_DIR / "sampled_commits.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # polars LazyFrame: scan_csv streams the file; we only materialize what we need.
    lf = (
        pl.scan_csv(
            csv_path,
            schema_overrides={
                "valid_yaml": pl.String,
                "valid_workflow": pl.String,
                "probably_workflow": pl.String,
            },
        )
        .filter(
            (pl.col("git_change_type") == "M")
            & (pl.col("valid_yaml") == "True")
            & (pl.col("valid_workflow") == "True")
            & (pl.col("file_hash").is_not_null())
            & (pl.col("previous_file_hash").is_not_null())
        )
        .select(
            "repository",
            "commit_hash",
            "committed_date",
            "file_path",
            "file_hash",
            "previous_file_hash",
        )
    )

    # Group by commit -> aggregate file list.
    grouped = (
        lf.group_by(["repository", "commit_hash"])
        .agg(
            pl.col("committed_date").min().alias("committed_date"),
            pl.struct(["file_path", "file_hash", "previous_file_hash"])
            .alias("files"),
        )
        .collect(engine="streaming")
    )

    # Deterministic sampling via blake2b(commit_hash) -> unit float, take smallest.
    bucket = grouped["commit_hash"].map_elements(
        lambda c: _hash_to_unit(c, seed), return_dtype=pl.Float64
    )
    grouped = grouped.with_columns(bucket.alias("_bucket"))
    sampled = grouped.sort("_bucket").head(n_commits).drop("_bucket")

    sampled.write_parquet(out_path)
    return out_path
