"""How many commits survive each "clean fix" filter variant?

The strictest filter (V_introduced == ∅) discards step-index-drift artifacts
but also discards legitimate fixes that happen to introduce ANY new finding.
We tested four progressively looser filters; this prints the commit counts
under each so the precision/recall trade-off is visible.

Inputs:  output/diffs.jsonl, output/scans.jsonl
Outputs: stdout
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from pattern_miner.scan import diff_findings, load_scans

OUT = Path("output")


def main() -> None:
    scans = load_scans(OUT / "scans.jsonl")

    # Aggregate per commit
    by_commit = defaultdict(lambda: {
        "V_fixed": set(),
        "V_introduced": set(),
        "V_fixed_cnt": Counter(),
        "V_introduced_cnt": Counter(),
    })
    for line in (OUT / "diffs.jsonl").open():
        r = json.loads(line)
        key = (r["repository"], r["commit_hash"])
        cd = by_commit[key]
        before = scans.get(r.get("previous_file_hash"))
        after = scans.get(r.get("file_hash"))
        if before is None or after is None:
            continue
        fixed, introduced = diff_findings(before, after)
        for f in fixed:
            cd["V_fixed"].add((f["ident"], f["route"]))
            cd["V_fixed_cnt"][f["ident"]] += 1
        for f in introduced:
            cd["V_introduced"].add((f["ident"], f["route"]))
            cd["V_introduced_cnt"][f["ident"]] += 1

    total_with_vfixed = sum(1 for c in by_commit.values() if c["V_fixed"])
    strict = sum(
        1 for c in by_commit.values()
        if c["V_fixed"] and not c["V_introduced"]
    )
    loose_A = sum(
        1 for c in by_commit.values()
        if c["V_fixed"]
        and set(c["V_introduced_cnt"]).issubset(set(c["V_fixed_cnt"]))
    )
    loose_B = sum(
        1 for c in by_commit.values()
        if c["V_fixed"]
        and all(
            c["V_introduced_cnt"][i] <= c["V_fixed_cnt"].get(i, 0)
            for i in c["V_introduced_cnt"]
        )
    )
    loose_C = total_with_vfixed

    print("=== clean-fix filter comparison ===\n")
    print(f"commits with V_fixed != ∅ (any fix at all):      {total_with_vfixed}")
    print()
    print(f"strict  (V_introduced == ∅):                     {strict}")
    print(f"loose_A (V_introduced_idents ⊆ V_fixed_idents):  {loose_A}  "
          f"({loose_A/strict:.2f}× of strict)")
    print(f"loose_B (no ident's count went up):              {loose_B}  "
          f"({loose_B/strict:.2f}× of strict)")
    print(f"loose_C (no constraint on V_introduced):         {loose_C}  "
          f"({loose_C/strict:.2f}× of strict)")


if __name__ == "__main__":
    main()
