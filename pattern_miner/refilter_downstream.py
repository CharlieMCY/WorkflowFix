"""Post-filter the existing backport_gaps artifacts to the in-scope
working set (clean fixes classified as `structural` in §III-B).

This DOES NOT re-run the GitHub audit — gaps.jsonl already audited every
clean-fix commit; we just drop rows for non-structural commits and
re-aggregate the headline numbers.

Outputs (under output/$DATASET_TAG/):

  backport_gaps/gaps_structural.jsonl
  backport_gaps/gaps_with_history_structural.jsonl
  backport_gaps/refilter_summary.md     before/after headline diff
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .config import OUTPUT_DIR


CLEAN_FIXES_DIR = OUTPUT_DIR / "clean_fixes"
GAPS_DIR = OUTPUT_DIR / "backport_gaps"


def _load_classification() -> tuple[set[tuple[str, str]], dict[str, int]]:
    """Return (structural_commits, kind_counts)."""
    path = CLEAN_FIXES_DIR / "classification.jsonl"
    if not path.exists():
        sys.exit(f"ERR: {path} missing — run classify_clean_fixes first.")
    structural: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    for line in path.open():
        r = json.loads(line)
        counts[r["kind"]] += 1
        if r["kind"] == "structural":
            structural.add((r["repository"], r["commit_hash"]))
    return structural, dict(counts)


def _filter_gaps(structural: set[tuple[str, str]],
                  in_path: Path, out_path: Path) -> tuple[int, int]:
    """Return (n_kept, n_total)."""
    kept = total = 0
    with out_path.open("w") as fp:
        for line in in_path.open():
            r = json.loads(line)
            total += 1
            if (r["repository"], r["commit_hash"]) in structural:
                fp.write(line)
                kept += 1
    return kept, total


def _aggregate_gaps(path: Path) -> dict:
    """Compute the headline gap counts on a (possibly filtered) gaps.jsonl."""
    branches = Counter()
    gap_repos = set()
    af_repos = set()
    inapplic_repos = set()
    n_commits_ok = 0
    n_commits_with_gap = 0
    for line in path.open():
        r = json.loads(line)
        if r.get("status") != "ok":
            continue
        n_commits_ok += 1
        if r.get("gap_branches"):
            n_commits_with_gap += 1
            gap_repos.add(r["repository"])
        for _gb in r.get("gap_branches", []) or []:
            branches["gap"] += 1
        for _afb in r.get("already_fixed_branches", []) or []:
            branches["already_fixed"] += 1
            af_repos.add(r["repository"])
        for _ib in r.get("inapplicable_branches", []) or []:
            branches["inapplicable"] += 1
            inapplic_repos.add(r["repository"])
    total_branches = sum(branches.values())
    return {
        "n_commits_ok": n_commits_ok,
        "n_commits_with_gap": n_commits_with_gap,
        "branches": dict(branches),
        "total_branches": total_branches,
        "gap_pct": branches["gap"] / total_branches if total_branches else 0,
        "gap_pct_actionable": (branches["gap"] /
            (branches["gap"] + branches["already_fixed"])
            if (branches["gap"] + branches["already_fixed"]) else 0),
        "n_repos_with_gap": len(gap_repos),
    }


def _aggregate_history(path: Path) -> dict:
    """Compute the refined-status counts on gaps_with_history.jsonl,
    using the same lag-sign refinement as analysis/05_history_lag_drill."""
    from backport_gaps.history import _refine_backport_status
    status = Counter()
    true_repos = set()
    for line in path.open():
        r = json.loads(line)
        for afb in r.get("already_fixed_branches", []) or []:
            s = _refine_backport_status(afb)
            status[s] += 1
            if s == "true_backport":
                true_repos.add(r["repository"])
    return {
        "by_status": dict(status),
        "n_true_backports": status.get("true_backport", 0),
        "n_true_backport_repos": len(true_repos),
    }


def run() -> None:
    structural, kind_counts = _load_classification()
    total_cf = sum(kind_counts.values())
    n_structural = kind_counts.get("structural", 0)
    print(f"§III-B classification: {kind_counts}")
    print(f"  structural / total = {n_structural} / {total_cf}"
          f" = {n_structural/total_cf*100:.1f}%\n")

    # --- gaps.jsonl filter -----------
    g_in = GAPS_DIR / "gaps.jsonl"
    g_out = GAPS_DIR / "gaps_structural.jsonl"
    kept, total = _filter_gaps(structural, g_in, g_out)
    print(f"gaps.jsonl: kept {kept}/{total} rows  -> {g_out}")

    g_before = _aggregate_gaps(g_in)
    g_after  = _aggregate_gaps(g_out)

    # --- history filter --------------
    h_in = GAPS_DIR / "gaps_with_history.jsonl"
    h_out = GAPS_DIR / "gaps_with_history_structural.jsonl"
    h_before = h_after = None
    if h_in.exists():
        kept_h, total_h = _filter_gaps(structural, h_in, h_out)
        print(f"gaps_with_history.jsonl: kept {kept_h}/{total_h} rows  -> {h_out}")
        h_before = _aggregate_history(h_in)
        h_after = _aggregate_history(h_out)

    # --- summary table -----------------
    lines = [
        "# Refilter to structural-only working set",
        "",
        f"clean fixes:  structural={kind_counts.get('structural',0)},"
        f" mixed={kind_counts.get('mixed',0)},"
        f" deletion={kind_counts.get('deletion',0)},"
        f" total={total_cf}",
        "",
        "## RQ2 (gap rate)",
        "",
        "| Metric | All clean fixes | Structural only | Δ |",
        "|---|---:|---:|---:|",
        f"| status=ok commits        | {g_before['n_commits_ok']} "
        f"| {g_after['n_commits_ok']} "
        f"| {g_after['n_commits_ok']-g_before['n_commits_ok']:+d} |",
        f"| total release branches   | {g_before['total_branches']} "
        f"| {g_after['total_branches']} "
        f"| {g_after['total_branches']-g_before['total_branches']:+d} |",
        f"| gap branches             | {g_before['branches']['gap']} "
        f"| {g_after['branches']['gap']} "
        f"| {g_after['branches']['gap']-g_before['branches']['gap']:+d} |",
        f"| already-fixed branches   | {g_before['branches']['already_fixed']} "
        f"| {g_after['branches']['already_fixed']} "
        f"| {g_after['branches']['already_fixed']-g_before['branches']['already_fixed']:+d} |",
        f"| inapplicable branches    | {g_before['branches']['inapplicable']} "
        f"| {g_after['branches']['inapplicable']} "
        f"| {g_after['branches']['inapplicable']-g_before['branches']['inapplicable']:+d} |",
        f"| **gap rate (of all)**    | **{g_before['gap_pct']*100:.1f}%** "
        f"| **{g_after['gap_pct']*100:.1f}%** "
        f"| {(g_after['gap_pct']-g_before['gap_pct'])*100:+.1f}pp |",
        f"| **gap rate (actionable)**| **{g_before['gap_pct_actionable']*100:.1f}%** "
        f"| **{g_after['gap_pct_actionable']*100:.1f}%** "
        f"| {(g_after['gap_pct_actionable']-g_before['gap_pct_actionable'])*100:+.1f}pp |",
        f"| repos with ≥1 gap        | {g_before['n_repos_with_gap']} "
        f"| {g_after['n_repos_with_gap']} "
        f"| {g_after['n_repos_with_gap']-g_before['n_repos_with_gap']:+d} |",
    ]
    if h_before is not None:
        lines += [
            "",
            "## RQ3 (true backports)",
            "",
            "| Refined status | All clean fixes | Structural only |",
            "|---|---:|---:|",
        ]
        all_statuses = sorted(set(h_before["by_status"]) | set(h_after["by_status"]))
        for s in all_statuses:
            lines.append(f"| {s} | {h_before['by_status'].get(s,0)} "
                          f"| {h_after['by_status'].get(s,0)} |")
        lines += [
            f"| **TRUE backports**       "
            f"| **{h_before['n_true_backports']}** "
            f"| **{h_after['n_true_backports']}** |",
            f"| repos with TRUE backports "
            f"| {h_before['n_true_backport_repos']} "
            f"| {h_after['n_true_backport_repos']} |",
        ]

    out_md = GAPS_DIR / "refilter_summary.md"
    out_md.write_text("\n".join(lines) + "\n")
    print(f"\nsummary -> {out_md}")
    print()
    print("\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.parse_args()
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
