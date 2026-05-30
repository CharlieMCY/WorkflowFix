"""Evaluate the pattern catalog by matching against an independent commit sample.

Re-runs match on `output/eval_diffs.jsonl` (an out-of-sample 2000-commit pull
with `seed=99`) and reports level-1 / level-2 / miss outcomes plus a few
examples of each.

Inputs:  output/patterns.jsonl, output/eval_diffs.jsonl, output/scans.jsonl
Outputs: stdout

Prereq:
    .venv/bin/python -m pattern_miner sample --n-commits 2000 --seed 99 \
        --out output/eval_sampled.parquet
    .venv/bin/python -m pattern_miner diffs --sample output/eval_sampled.parquet \
        --out output/eval_diffs.jsonl
    .venv/bin/python -m pattern_miner scan --diffs output/eval_diffs.jsonl
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from pattern_miner.clean_fixes import aggregate_commits, filter_clean_fixes
from pattern_miner.match import load_pattern_index, match_commit
from pattern_miner.scan import load_scans

OUT = Path("output")


def main() -> None:
    eval_path = OUT / "eval_diffs.jsonl"
    if not eval_path.exists():
        print(f"missing {eval_path}")
        print("regenerate via the prereq steps in the module docstring.")
        return

    pattern_index = load_pattern_index(OUT / "patterns.jsonl")
    scans = load_scans(OUT / "scans.jsonl")
    print(f"loaded {len(pattern_index)} pattern types and {len(scans)} blob scans\n")

    commits = aggregate_commits(eval_path, scans)
    clean = filter_clean_fixes(commits)
    print(f"fresh clean-fix commits to match: {len(clean)}\n")

    counts: Counter[str] = Counter()
    examples: dict[str, list] = defaultdict(list)
    miss_fix_sets: Counter[tuple[str, ...]] = Counter()
    for c in clean:
        r = match_commit(c, pattern_index)
        counts[r["outcome"]] += 1
        if len(examples[r["outcome"]]) < 3:
            examples[r["outcome"]].append(
                (c["repository"], c["commit_hash"][:10],
                 r["fixes"], r["matched_pattern"])
            )
        if r["outcome"] == "miss":
            miss_fix_sets[tuple(r["fixes"])] += 1

    total = sum(counts.values())
    print("=== match outcomes ===")
    for k in ("full", "level-1", "miss"):
        v = counts[k]
        pct = 100 * v / max(total, 1)
        print(f"  {k:>10}: {v:>4}  ({pct:>5.1f}%)")

    print()
    print("=== examples (up to 3 per outcome) ===")
    for outcome in ("full", "level-1", "miss"):
        if not examples[outcome]:
            continue
        print(f"  {outcome}:")
        for repo, sha, fixes, matched in examples[outcome]:
            m = matched or "-"
            print(f"    {repo}@{sha}  fixes={fixes}  matched={m}")

    if miss_fix_sets:
        print()
        print("=== miss buckets (V_fixed_idents not in catalog), top 10 ===")
        for fixes, n in miss_fix_sets.most_common(10):
            print(f"  n={n:>3}  {{{', '.join(fixes)}}}")


if __name__ == "__main__":
    main()
