"""Detailed drill into the gap-audit output.

Reports:
  - overall counts (status, gap / already / inapplicable)
  - per-commit gap distribution (most-affected commits, long tail)
  - per-ident gap occurrences (how often each rule is left unpatched)
  - repo coverage (unique repos w/ gap vs. audited)
  - mirror-commit duplication (same commit_hash in multiple repos)

Inputs:  output/backport_gaps/gaps.jsonl
Outputs: stdout
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

OUT = Path("output")
GAPS = OUT / "backport_gaps" / "gaps.jsonl"


def main() -> None:
    rows = [json.loads(l) for l in GAPS.open()]
    ok = [r for r in rows if r["status"] == "ok"]

    n_gap_pairs = sum(len(r["gap_branches"]) for r in ok)
    n_af_pairs = sum(len(r["already_fixed_branches"]) for r in ok)
    n_ia_pairs = sum(len(r["inapplicable_branches"]) for r in ok)
    n_with_gap = sum(1 for r in ok if r["gap_branches"])

    print("=== overall ===")
    print(f"audited commits (status=ok):              {len(ok)}")
    print(f"commits with >=1 gap branch:              {n_with_gap}"
          f"  ({100*n_with_gap/max(len(ok),1):.1f}%)")
    print(f"total release branches checked:           "
          f"{n_gap_pairs + n_af_pairs + n_ia_pairs}")
    print(f"  → gap branches:                         {n_gap_pairs}")
    print(f"  → already_fixed branches:               {n_af_pairs}")
    print(f"  → inapplicable (file absent on branch): {n_ia_pairs}")

    # Per-commit gap distribution
    print()
    print("=== gap-branches per commit ===")
    gap_count = Counter(len(r["gap_branches"]) for r in ok)
    for n in sorted(gap_count):
        print(f"  {n:>3} gaps:  {gap_count[n]} commits")

    # Top-K most-affected commits
    print()
    print("=== top 15 commits by #gap_branches ===")
    top = sorted(ok, key=lambda r: -len(r["gap_branches"]))[:15]
    for r in top:
        print(f"  {len(r['gap_branches']):>3} gaps  "
              f"{r['repository']:<55}  V_fixed={r['V_fixed_idents']}")

    # Per-ident
    print()
    print("=== zizmor idents most often left unpatched on release branches ===")
    by_ident: Counter[str] = Counter()
    for r in ok:
        for gb in r["gap_branches"]:
            for ident in gb["V_present_idents"]:
                by_ident[ident] += 1
    for ident, n in by_ident.most_common():
        print(f"  {n:>5}  {ident}")

    # Repo coverage
    print()
    print("=== repo coverage ===")
    repos_all = set(r["repository"] for r in ok)
    repos_with_gap = set(r["repository"] for r in ok if r["gap_branches"])
    repos_with_af = set(r["repository"]
                        for r in ok if r["already_fixed_branches"])
    repos_with_both = repos_with_gap & repos_with_af
    repos_with_either = repos_with_gap | repos_with_af
    repos_with_neither = repos_all - repos_with_either
    n_all = len(repos_all)

    def pct(n): return f"{100*n/max(n_all,1):>5.1f}%"

    print(f"unique repos audited:                  {n_all:>4}")
    print(f"  with at least one gap:               {len(repos_with_gap):>4}  ({pct(len(repos_with_gap))})")
    print(f"  with prior backport (already_fixed): {len(repos_with_af):>4}  ({pct(len(repos_with_af))})")
    print(f"  with both gap AND already_fixed:     {len(repos_with_both):>4}  ({pct(len(repos_with_both))})")
    print(f"  with either (any backport activity): {len(repos_with_either):>4}  ({pct(len(repos_with_either))})")
    print(f"  with neither (no actionable signal): {len(repos_with_neither):>4}  ({pct(len(repos_with_neither))})")

    # Per affected-repo: gap density
    if repos_with_gap:
        per_repo_gap = {}
        for r in ok:
            if r["gap_branches"]:
                per_repo_gap[r["repository"]] = per_repo_gap.get(r["repository"], 0) + len(r["gap_branches"])
        total_gap_in_affected = sum(per_repo_gap.values())
        print()
        print(f"of the {len(repos_with_gap)} repos with at least one gap:")
        print(f"  total gap branches:          {total_gap_in_affected}")
        print(f"  avg gap branches / repo:     {total_gap_in_affected / len(repos_with_gap):>5.1f}")
        print(f"  max gap branches in one repo: {max(per_repo_gap.values())}"
              f"  ({max(per_repo_gap, key=per_repo_gap.get)})")

    # Mirror commits
    print()
    print("=== mirror commits (same commit_hash in multiple repos) ===")
    hash_to_repos: dict[str, set] = {}
    for r in ok:
        hash_to_repos.setdefault(r["commit_hash"], set()).add(r["repository"])
    mirrored = {h: rs for h, rs in hash_to_repos.items() if len(rs) > 1}
    extra = sum(len(rs) - 1 for rs in mirrored.values())
    print(f"unique commit hashes:                  {len(hash_to_repos)}")
    print(f"hashes in >=2 repos:                   {len(mirrored)}")
    print(f"mirror copies (excess beyond first):   {extra}")
    for h, rs in sorted(mirrored.items(), key=lambda kv: -len(kv[1]))[:3]:
        print(f"  top: {h[:10]} in {len(rs)} repos: {sorted(rs)[:3]}...")


if __name__ == "__main__":
    main()
