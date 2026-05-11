"""End-to-end CLI for the workflow fix-pattern miner.

Pipeline (5 stages, each writes to output/):

    sample        CSV               -> sampled_commits.parquet
    diffs         blobs (per commit)-> diffs.jsonl
    scan          blobs (unique)    -> scans.jsonl
    clean-fixes   diffs + scans     -> clean_fixes/
    patterns      diffs + scans     -> patterns.jsonl

Run all five with `pipeline`:

    .venv/bin/python -m pattern_miner pipeline --n-commits 10000

Or run them individually:

    .venv/bin/python -m pattern_miner sample --n-commits 10000
    .venv/bin/python -m pattern_miner diffs
    .venv/bin/python -m pattern_miner scan
    .venv/bin/python -m pattern_miner clean-fixes
    .venv/bin/python -m pattern_miner patterns
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl
from tqdm import tqdm

from .clean_fixes import (
    aggregate_commits,
    filter_clean_fixes,
    run_clean_fixes,
    run_patterns,
)
from .config import OUTPUT_DIR
from .extract_diff import diff_workflow_versions
from .match import load_pattern_index, match_commit
from .sample import sample_commits
from .scan import load_scans, scan_blobs


# --- helpers ----------------------------------------------------------------


def _iter_diffs_from_sample(parquet_path: Path):
    """Yield WorkflowDiff for every workflow-file edit in every sampled commit."""
    df = pl.read_parquet(parquet_path)
    for row in df.iter_rows(named=True):
        repo = row["repository"]
        sha = row["commit_hash"]
        for f in row["files"]:
            yield diff_workflow_versions(
                repository=repo,
                commit_hash=sha,
                file_path=f["file_path"],
                file_hash=f["file_hash"],
                previous_file_hash=f["previous_file_hash"],
            )


# --- subcommands ------------------------------------------------------------


def cmd_sample(args):
    out = sample_commits(
        n_commits=args.n_commits,
        seed=args.seed,
        out_path=args.out,
    )
    print(f"sampled commits -> {out}")


def cmd_diffs(args):
    in_path = args.sample or (OUTPUT_DIR / "sampled_commits.parquet")
    out_path = args.out or (OUTPUT_DIR / "diffs.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_total = 0
    n_kept = 0
    with out_path.open("w", encoding="utf-8") as fp:
        for diff in tqdm(_iter_diffs_from_sample(in_path), desc="diff"):
            n_total += 1
            if diff.parse_error or diff.is_empty():
                continue
            n_kept += 1
            fp.write(json.dumps(diff.to_record(), default=str))
            fp.write("\n")
    print(f"diffs written -> {out_path}")
    print(f"  files inspected: {n_total}")
    print(f"  non-empty diffs: {n_kept}")


def cmd_scan(args):
    in_path = args.diffs or (OUTPUT_DIR / "diffs.jsonl")
    out_path = args.out or (OUTPUT_DIR / "scans.jsonl")

    hashes: set[str] = set()
    with in_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            r = json.loads(line)
            if r.get("file_hash"):
                hashes.add(r["file_hash"])
            if r.get("previous_file_hash"):
                hashes.add(r["previous_file_hash"])

    print(f"unique blob hashes to scan: {len(hashes)}")
    out = scan_blobs(hashes, out_path=out_path, n_workers=args.workers)
    print(f"scans written -> {out}")


def cmd_clean_fixes(args):
    clean, n_dirs = run_clean_fixes(
        diffs_path=args.diffs,
        scans_path=args.scans,
        dest=args.out,
    )
    dest = args.out or (OUTPUT_DIR / "clean_fixes")
    print(f"clean-fix commits: {len(clean)}")
    print(f"wrote {n_dirs} commit directories to {dest}")


def cmd_patterns(args):
    out = args.out or (OUTPUT_DIR / "patterns.jsonl")
    patterns = run_patterns(
        diffs_path=args.diffs,
        scans_path=args.scans,
        out_path=out,
        max_exemplars=args.max_exemplars,
    )
    n_commits = sum(p["n_commits"] for p in patterns)
    n_sub = sum(p["n_subclusters"] for p in patterns)
    print(f"patterns written -> {out}")
    print(f"  level-1 buckets (V_fixed_idents):     {len(patterns)}")
    print(f"  level-2 sub-clusters (templates):     {n_sub}")
    print(f"  total clean-fix commits clustered:    {n_commits}")
    print()
    print("  top 10 patterns by n_commits:")
    for p in patterns[:10]:
        fixes = ", ".join(p["fixes"])
        pct = 100 * p["n_commits"] / max(n_commits, 1)
        print(f"    n_commits={p['n_commits']:5d}  ({pct:>5.1f}%)  "
              f"n_sub={p['n_subclusters']:4d}  {{{fixes}}}")


def cmd_match(args):
    patterns_path = args.patterns or (OUTPUT_DIR / "patterns.jsonl")
    diffs_path = args.diffs or (OUTPUT_DIR / "diffs.jsonl")
    scans_path = args.scans or (OUTPUT_DIR / "scans.jsonl")

    pattern_index = load_pattern_index(patterns_path)
    print(f"loaded {len(pattern_index)} pattern types from {patterns_path}")

    scans = load_scans(scans_path)
    commits = aggregate_commits(diffs_path, scans)
    clean = filter_clean_fixes(commits)
    print(f"new clean-fix commits to match: {len(clean)}")
    if not clean:
        return

    counts = {"full": 0, "level-1": 0, "miss": 0}
    examples = {"full": [], "level-1": [], "miss": []}
    miss_fix_sets: dict[tuple[str, ...], int] = {}
    for c in clean:
        result = match_commit(c, pattern_index)
        counts[result["outcome"]] += 1
        if len(examples[result["outcome"]]) < 3:
            examples[result["outcome"]].append({
                "repo": c["repository"],
                "sha": c["commit_hash"],
                "fixes": result["fixes"],
                "matched": result.get("matched_pattern"),
            })
        if result["outcome"] == "miss":
            k = tuple(result["fixes"])
            miss_fix_sets[k] = miss_fix_sets.get(k, 0) + 1

    total = sum(counts.values())
    print()
    print("=== match outcomes ===")
    for k in ("full", "level-1", "miss"):
        v = counts[k]
        pct = 100 * v / max(total, 1)
        label = {
            "full":    "full match  (known pattern, known shape) ",
            "level-1": "level-1     (known pattern, new shape)   ",
            "miss":    "miss        (new pattern type entirely)  ",
        }[k]
        print(f"  {label}: {v:>4}  ({pct:>5.1f}%)")

    print()
    print("=== examples (first 3 of each outcome) ===")
    for outcome in ("full", "level-1", "miss"):
        if not examples[outcome]:
            continue
        print(f"  {outcome}:")
        for e in examples[outcome]:
            matched = e["matched"] or "-"
            print(f"    {e['repo']}@{e['sha'][:10]}")
            print(f"      fixes:   {e['fixes']}")
            print(f"      matched: {matched}")

    if miss_fix_sets:
        print()
        print("=== miss buckets (V_fixed_idents not in catalog), top 10 ===")
        for fixes, n in sorted(miss_fix_sets.items(), key=lambda x: -x[1])[:10]:
            print(f"  n={n:>3}  {{{', '.join(fixes)}}}")


def cmd_pipeline(args):
    """Run all five stages end-to-end."""
    cmd_sample(args)
    cmd_diffs(argparse.Namespace(sample=None, out=None))
    cmd_scan(argparse.Namespace(diffs=None, out=None, workers=args.workers))
    cmd_clean_fixes(argparse.Namespace(diffs=None, scans=None, out=None))
    cmd_patterns(argparse.Namespace(diffs=None, scans=None, out=None,
                                     max_exemplars=args.max_exemplars))


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pattern_miner")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sample", help="sample candidate commits from CSV")
    sp.add_argument("--n-commits", type=int, default=10000)
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--out", type=Path, default=None,
                    help="output parquet path (default: output/sampled_commits.parquet)")
    sp.set_defaults(func=cmd_sample)

    sp = sub.add_parser("diffs", help="extract YAML-aware structural diffs")
    sp.add_argument("--sample", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=cmd_diffs)

    sp = sub.add_parser("scan", help="run zizmor on every unique blob in diffs.jsonl")
    sp.add_argument("--diffs", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None)
    sp.add_argument("--workers", type=int, default=None,
                    help="number of parallel scanner processes (default: cpu count)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("clean-fixes",
                        help="dump every clean-fix commit (V_fixed!=∅ AND V_introduced==∅)")
    sp.add_argument("--diffs", type=Path, default=None)
    sp.add_argument("--scans", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None,
                    help="destination directory (default: output/clean_fixes/)")
    sp.set_defaults(func=cmd_clean_fixes)

    sp = sub.add_parser("patterns",
                        help="cluster clean-fix commits into a pattern catalog")
    sp.add_argument("--diffs", type=Path, default=None)
    sp.add_argument("--scans", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None,
                    help="output JSONL (default: output/patterns.jsonl)")
    sp.add_argument("--max-exemplars", type=int, default=5)
    sp.set_defaults(func=cmd_patterns)

    sp = sub.add_parser("match",
                        help="match new commits in --diffs against existing pattern catalog")
    sp.add_argument("--patterns", type=Path, default=None,
                    help="catalog file (default: output/patterns.jsonl)")
    sp.add_argument("--diffs", type=Path, default=None,
                    help="diffs.jsonl of NEW commits to evaluate")
    sp.add_argument("--scans", type=Path, default=None,
                    help="scans.jsonl (must include all blobs referenced by --diffs)")
    sp.set_defaults(func=cmd_match)

    sp = sub.add_parser("pipeline", help="run sample + diffs + scan + clean-fixes + patterns")
    sp.add_argument("--n-commits", type=int, default=10000)
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--max-exemplars", type=int, default=5)
    sp.set_defaults(func=cmd_pipeline)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
