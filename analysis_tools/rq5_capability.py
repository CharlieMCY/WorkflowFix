"""RQ5 (Capability) — WORKFLOWBP on the 4,776 unpatched (fix, branch) pairs.

For each pair:
  1. Compile the master clean-fix into a WSP program.
  2. Fetch the release-branch file via GitHub API.
  3. Apply the program (with a target-ref-resolving pin resolver).
  4. Run zizmor_local + actionlint oracles on (target_before, patched).

This script can either:
  - Drive the run from scratch (calls backport_ir.pipeline.run_backport),
    which fetches everything and writes per-pair reports to
    output/backport_ir/patches/.
  - Or aggregate an already-completed run by reading
    output/backport_ir/patches/backport_index.jsonl.

Headline output table: per outcome bucket (accepted / failed by zizmor /
failed by actionlint / inapplicable / needs_review) with per-rule and
per-V1-V4 class breakdowns. Written to analysis_tools/reports/rq5_*.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .common import OUTPUT_DIR, REPORTS_DIR, pct, write_table

BACKPORT_INDEX = OUTPUT_DIR / "backport_ir" / "patches" / "backport_index.jsonl"


def run_backport_pipeline(limit: int | None = None) -> None:
    """Drive backport_ir to apply WSPs to every gap pair and run oracles."""
    from backport_ir.pipeline import run_backport

    print(f"running backport pipeline (limit={limit})...", flush=True)
    rows = run_backport(limit=limit, oracle=True)
    print(f"  produced {len(rows)} rows -> {BACKPORT_INDEX}")


def _classify_row(row: dict) -> str:
    """Bucket each attempt by why it succeeded or failed."""
    if row.get("status") != "patched":
        return "fetch_or_compile_error"

    summary = row.get("summary", {}) or {}
    statuses = summary.get("by_status", {}) or {}
    n_landed = statuses.get("applied", 0) + statuses.get("created", 0) + statuses.get("noop", 0)
    n_review = statuses.get("needs_review", 0)

    oracle = row.get("oracle", {}) or {}
    z = oracle.get("zizmor_local", {}) or {}
    a = oracle.get("actionlint", {}) or {}

    if z.get("success") and a.get("success"):
        return "accepted"
    if not z.get("landed_paths") and n_review:
        return "needs_review_only"
    if not z.get("landed_paths"):
        return "no_landed_edits"
    if not a.get("success"):
        return "failed_actionlint"
    if z.get("failed_edits"):
        return "failed_zizmor_local"
    if z.get("introduced_in_scope"):
        return "regression_in_scope"
    return "other"


def aggregate(index_path: Path = BACKPORT_INDEX) -> dict:
    """Read backport_index.jsonl and bucket each pair."""
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path} not found — run with --run first, or pass --index <path>."
        )

    buckets: Counter[str] = Counter()
    by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    by_class: Counter[str] = Counter()
    rows: list[dict] = []

    for line in index_path.open("r", encoding="utf-8"):
        row = json.loads(line)
        b = _classify_row(row)
        buckets[b] += 1
        rows.append({"repository": row.get("repository", ""),
                     "commit_hash": row.get("commit_hash", ""),
                     "branch": row.get("branch", ""),
                     "file": row.get("file", ""),
                     "bucket": b})

        # per-rule breakdown: each row's relevant_targets give the rule(s)
        # the patch was supposed to address.
        z = (row.get("oracle", {}) or {}).get("zizmor_local", {}) or {}
        for ident in z.get("landed_paths", []):
            pass  # site path, not the rule
        targets = (row.get("oracle", {}) or {}).get("zizmor_global", {}).get(
            "relevant_targets", [])
        # fall back to target_idents from the program if available
        if not targets:
            targets = []
        for rule in targets:
            by_rule[rule][b] += 1

    return {
        "n": sum(buckets.values()),
        "buckets": dict(buckets),
        "by_rule": {r: dict(cs) for r, cs in by_rule.items()},
        "rows": rows,
    }


def write_reports(agg: dict, out_dir: Path = REPORTS_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    total = agg["n"]

    # Headline outcome bucket table
    rows = []
    order = ["accepted", "needs_review_only", "no_landed_edits",
             "failed_zizmor_local", "regression_in_scope",
             "failed_actionlint", "fetch_or_compile_error", "other"]
    for b in order:
        c = agg["buckets"].get(b, 0)
        rows.append((b, c, pct(c, total)))
    write_table(out_dir / "rq5_outcome_buckets.md", rows)

    # Per-rule bucket counts
    per_rule_path = out_dir / "rq5_per_rule.md"
    lines = ["| Rule | Total | Accepted | Acc % |", "|---|---:|---:|---:|"]
    for rule, cs in sorted(agg["by_rule"].items(),
                           key=lambda kv: -sum(kv[1].values())):
        rule_total = sum(cs.values())
        accepted = cs.get("accepted", 0)
        lines.append(f"| `{rule}` | {rule_total} | {accepted} | {pct(accepted, rule_total)} |")
    per_rule_path.write_text("\n".join(lines) + "\n")

    # Raw row dump for downstream inspection
    (out_dir / "rq5_rows.jsonl").write_text(
        "\n".join(json.dumps(r) for r in agg["rows"]) + "\n"
    )

    # Console summary
    accepted = agg["buckets"].get("accepted", 0)
    print(f"RQ5: {accepted}/{total} accepted ({pct(accepted, total)}) "
          f"(zizmor_local AND actionlint pass)")
    print(f"     tables -> {out_dir}/rq5_*.md")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", action="store_true",
                   help="drive backport_ir to apply WSPs (requires GITHUB_TOKEN); "
                        "without --run, only aggregates the existing index.")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of clean fixes considered (for smoke runs)")
    p.add_argument("--index", type=Path, default=BACKPORT_INDEX,
                   help="path to backport_index.jsonl")
    args = p.parse_args()

    if args.run:
        run_backport_pipeline(limit=args.limit)

    agg = aggregate(index_path=args.index)
    write_reports(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
