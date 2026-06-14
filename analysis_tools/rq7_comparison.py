"""RQ7 (Comparison) — WORKFLOWBP vs. three baselines on the RQ5 pair set.

For each (commit, gap_branch, file) the four candidate generators are:

  workflowbp     compile + apply the WSP via backport_ir
  copy_paste     git-apply-style verbatim diff replay
  dependabot     extract only `uses:` upgrades from the source diff
  llm            Claude-driven generation (optional; flag --llm to enable)

Each produces a `patched_text`; we then run zizmor_local + actionlint on
(target_before, patched) and bucket the verdict. The LLM also reports
SHA fabrication rate as its own column.

Writes side-by-side comparison tables to analysis_tools/reports/rq7_*.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .common import (
    OUTPUT_DIR, REPORTS_DIR, iter_gap_pairs, pct,
    run_oracles, write_jsonl, write_table,
)
from .baselines import copy_paste, dependabot_style, llm


def _baseline_apply(name: str, prog, source_before: str, source_after: str,
                    target_before: str, *, resolver=None):
    """Dispatch to one baseline and normalise its output.

    Returns (patched_text, candidate_apply_result_for_locality_scope).
    The locality-scope ApplyResult is reused from the WORKFLOWBP apply
    because the baselines don't natively produce one; this is a sound
    upper bound (the oracle's locality scope is the set of edits
    WORKFLOWBP would have targeted).
    """
    from backport_ir.apply import apply_program

    if name == "workflowbp":
        res = apply_program(prog, target_before, resolver=resolver)
        return res.patched_text, res

    # All baselines reuse WORKFLOWBP's apply_result for locality scope.
    workflowbp_res = apply_program(prog, target_before, resolver=resolver)

    if name == "copy_paste":
        out = copy_paste.apply(source_before, source_after, target_before)
        return out.patched_text, workflowbp_res

    if name == "dependabot":
        out = dependabot_style.apply(source_before, source_after, target_before)
        return out.patched_text, workflowbp_res

    if name == "llm":
        out = llm.apply(source_before, source_after, target_before,
                         sha_resolver=resolver)
        return out.patched_text, workflowbp_res

    raise ValueError(f"unknown baseline {name!r}")


def _classify(prog, target_before: str, patched: str, apply_result) -> dict:
    """Run the two oracles and bucket."""
    if not patched.strip():
        return {"bucket": "no_output", "zizmor": False, "actionlint": False}
    verdict = run_oracles(prog, target_before, patched, apply_result)
    if verdict.error:
        return {"bucket": "oracle_error", "zizmor": False, "actionlint": False,
                "error": verdict.error}
    if verdict.accepted:
        bucket = "accepted"
    elif not verdict.actionlint_ok:
        bucket = "failed_actionlint"
    elif not verdict.zizmor_local_ok:
        bucket = "failed_zizmor"
    else:
        bucket = "other"
    return {"bucket": bucket, "zizmor": verdict.zizmor_local_ok,
            "actionlint": verdict.actionlint_ok}


def run(
    baselines: list[str],
    limit: int | None = None,
    out_dir: Path = REPORTS_DIR,
) -> list[dict]:
    """For each gap pair, run every baseline; record per-baseline bucket.

    Resume-safe: rows append to rq7_rows.jsonl as they finish, and on
    re-run we skip any (repo, commit, branch, file) already present in
    the file.
    """
    from backport_gaps.config import get_github_token
    from backport_gaps.github import GitHubClient
    from backport_ir.pipeline import iter_clean_fix_programs, make_github_resolver
    from common.cache import jsonl_already_done, jsonl_append

    client = GitHubClient(get_github_token())
    resolver = make_github_resolver(client)

    rows_path = out_dir / "rq7_rows.jsonl"

    def _key(r: dict) -> tuple:
        return (r.get("repository", ""), r.get("commit_hash", ""),
                r.get("branch", ""), r.get("file_path", "") or r.get("file", ""))
    done = jsonl_already_done(rows_path, _key)
    if done:
        print(f"resume: skipping {len(done)} rows already in {rows_path}")

    # Index clean-fix programs by (repo, sha, file_path) for fast lookup
    programs: dict[tuple[str, str, str], object] = {}
    program_blobs: dict[tuple[str, str, str], tuple[str, str]] = {}
    from .common import iter_clean_fixes
    for cf in iter_clean_fixes():
        for f in cf.files:
            programs.setdefault(
                (cf.repository, cf.commit_hash, f["file_path"]),
                None)
            program_blobs[(cf.repository, cf.commit_hash, f["file_path"])] = (
                f["before_text"], f["after_text"])
    for _commit_dir, prog in iter_clean_fix_programs():
        programs[(prog.repository, prog.commit_hash, prog.source_file)] = prog

    n_processed = 0
    for pair in iter_gap_pairs():
        if limit is not None and n_processed >= limit:
            break
        pair_key = (pair["repository"], pair["commit_hash"], pair["branch"],
                    pair["file_path"])
        if pair_key in done:
            continue

        key = (pair["repository"], pair["commit_hash"], pair["file_path"])
        prog = programs.get(key)
        blobs = program_blobs.get(key)
        if prog is None or blobs is None:
            continue
        source_before, source_after = blobs

        # Fetch target_before from the release branch (uses GitHub file cache)
        fetched = client.get_file_at_ref(pair["repository"], pair["file_path"],
                                          pair["branch"])
        if fetched is None:
            row = {**pair, "bucket": "target_absent"}
            jsonl_append(rows_path, row)
            done.add(pair_key)
            n_processed += 1
            continue
        target_before = fetched[0].decode("utf-8", "replace")

        # Run each baseline
        per_baseline: dict[str, dict] = {}
        for name in baselines:
            try:
                patched, apply_res = _baseline_apply(
                    name, prog, source_before, source_after, target_before,
                    resolver=resolver)
                per_baseline[name] = _classify(prog, target_before, patched, apply_res)
            except Exception as e:
                per_baseline[name] = {"bucket": "error", "error": str(e),
                                       "zizmor": False, "actionlint": False}

        row = {**pair, "per_baseline": per_baseline}
        jsonl_append(rows_path, row)
        done.add(pair_key)
        n_processed += 1
        if n_processed % 20 == 0:
            print(f"  processed {n_processed} pairs (skipped {len(done)-n_processed} from prior runs)...", flush=True)

    # Re-read the JSONL so callers see every row (new + resumed)
    rows = [json.loads(line) for line in rows_path.open("r", encoding="utf-8")]
    return rows


def write_reports(rows: list[dict], out_dir: Path = REPORTS_DIR) -> None:
    # Per-baseline accepted/failed summary
    baselines = sorted({b for r in rows for b in r.get("per_baseline", {})})
    table_lines = ["| Baseline | Accepted | Failed (zizmor) | Failed (actionlint) | Other |",
                   "|---|---:|---:|---:|---:|"]
    for b in baselines:
        c = Counter()
        for r in rows:
            v = r.get("per_baseline", {}).get(b, {})
            c[v.get("bucket", "no_data")] += 1
        total = sum(c.values())
        accepted = c.get("accepted", 0)
        fz = c.get("failed_zizmor", 0)
        fa = c.get("failed_actionlint", 0)
        other = total - accepted - fz - fa
        table_lines.append(f"| `{b}` | {accepted} ({pct(accepted, total)}) "
                            f"| {fz} | {fa} | {other} |")
    (out_dir / "rq7_summary.md").write_text("\n".join(table_lines) + "\n")

    # LLM-specific: SHA hallucination rate, if present
    llm_rows = [r for r in rows if "llm" in r.get("per_baseline", {})]
    if llm_rows:
        fab_count = sum(len(r["per_baseline"]["llm"].get("fabricated_shas", []))
                         for r in llm_rows)
        ver_count = sum(len(r["per_baseline"]["llm"].get("verified_shas", []))
                         for r in llm_rows)
        denom = fab_count + ver_count
        rate = pct(fab_count, denom) if denom else "—"
        (out_dir / "rq7_llm_hallucination.md").write_text(
            f"LLM SHA hallucination rate: {fab_count} fabricated / "
            f"{denom} total SHA pins ({rate})\n"
        )

    print(f"RQ7 tables -> {out_dir}/rq7_*.md")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baselines", nargs="+",
                   default=["workflowbp", "copy_paste", "dependabot"],
                   help="which baselines to run; add 'llm' for Claude")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--aggregate-only", action="store_true",
                   help="re-aggregate the existing rq7_rows.jsonl")
    args = p.parse_args()

    if args.aggregate_only:
        rows_path = REPORTS_DIR / "rq7_rows.jsonl"
        if not rows_path.exists():
            print(f"{rows_path} missing — run without --aggregate-only.")
            return 1
        rows = [json.loads(l) for l in rows_path.open("r")]
    else:
        rows = run(args.baselines, limit=args.limit)
    write_reports(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
