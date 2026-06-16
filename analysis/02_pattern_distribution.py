"""Distribution of the pattern catalog produced by `patterns`.

Reports:
  - total commits / level-1 buckets / level-2 sub-clusters
  - |V_fixed_idents| size breakdown (1-form / 2-form / ...) with commit weight
  - all level-1 buckets with n_commits, sub-cluster ratio
  - structural-uniqueness ratio (n_sub / n_commits) per bucket

Inputs:  output/patterns.jsonl
Outputs: stdout
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from common.dataset import output_dir

OUT = output_dir()


def main() -> None:
    patterns = [json.loads(l) for l in (OUT / "patterns.jsonl").open()]
    total_commits = sum(p["n_commits"] for p in patterns)
    total_sub = sum(p["n_subclusters"] for p in patterns)

    print(f"=== pattern catalog ===\n")
    print(f"total clean-fix commits clustered: {total_commits}")
    print(f"level-1 buckets (V_fixed_idents):  {len(patterns)}")
    print(f"level-2 sub-clusters (templates):  {total_sub}")
    print(f"avg subclusters per bucket:        {total_sub/max(len(patterns),1):.1f}")
    print(f"structural uniqueness ratio:       {total_sub/max(total_commits,1):.2f}  "
          "(near 1 = each commit is structurally unique)")

    print()
    # |form_set| size breakdown
    by_size = defaultdict(list)
    for p in patterns:
        by_size[len(p["fixes"])].append(p)
    print("=== distribution by |V_fixed_idents| ===")
    print(f'{"size":>4}  {"#buckets":>8}  {"#commits":>9}  {"% commits":>9}')
    for s in sorted(by_size):
        n_sets = len(by_size[s])
        n_c = sum(p["n_commits"] for p in by_size[s])
        print(f'{s:>4}  {n_sets:>8}  {n_c:>9}  {100*n_c/total_commits:>8.1f}%')

    print()
    print("=== all level-1 buckets, sorted by n_commits ===")
    patterns.sort(key=lambda p: -p["n_commits"])
    print(f'{"rk":>3}  {"n_commits":>9}  {"%":>5}  {"n_sub":>5}  {"sub/n":>5}  fixes')
    print('-' * 100)
    for i, p in enumerate(patterns, 1):
        ratio = p["n_subclusters"] / max(p["n_commits"], 1)
        pct = 100 * p["n_commits"] / total_commits
        fixes = ", ".join(p["fixes"])
        print(f'{i:>3}  {p["n_commits"]:>9}  {pct:>4.1f}%  '
              f'{p["n_subclusters"]:>5}  {ratio:>5.2f}  {{{fixes}}}')


if __name__ == "__main__":
    main()
