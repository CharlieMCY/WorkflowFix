"""RQ7 (Comparison) — WORKFLOWBP (candidate) vs. baseline generators
on the RQ5 pair set.

For each (commit, gap_branch, file) the generators are:

  workflowbp     compile + apply the WSP via backport_ir (the candidate)
  copy_paste     unified-diff replay (baseline)
  dependabot     extract only `uses:` upgrades from the source diff (baseline)
  llm            Claude-driven generation (optional baseline; opt-in via --baselines)

Each generator produces a `patched_text`; we then run zizmor_local +
actionlint on (target_before, patched) and bucket the verdict. The LLM
also reports SHA fabrication rate as its own column.

Writes side-by-side comparison tables to analysis_tools/reports/$TAG/rq7_*.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .baselines import copy_paste, dependabot_style, llm
from .common import (
    REPORTS_DIR, iter_gap_pairs, pct,
    run_oracles,
)


def _baseline_patched(name: str, prog, source_before: str, source_after: str,
                       target_before: str, workflowbp_res) -> str:
    """Produce a patched_text from one generator. workflowbp_res is the
    already-computed apply_program result; baselines re-use it as their
    locality-scope reference (the oracle's scope is the set of edits the
    candidate would have targeted)."""
    if name == "workflowbp":
        return workflowbp_res.patched_text
    if name == "copy_paste":
        return copy_paste.apply(source_before, source_after, target_before).patched_text
    if name == "dependabot":
        return dependabot_style.apply(source_before, source_after, target_before).patched_text
    if name == "llm":
        return llm.apply(source_before, source_after, target_before,
                          sha_resolver=None).patched_text
    raise ValueError(f"unknown generator {name!r}")


def _classify(prog, target_before: str, patched: str, apply_result) -> dict:
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


def _process_pair(pair: dict, client, resolver, programs: dict,
                   program_blobs: dict, baselines: list[str]) -> dict:
    """Process one (commit, gap_branch, file) pair: compile-or-skip,
    fetch target, run every generator + classifier, return a row dict."""
    from backport_ir.apply import apply_program

    base = dict(pair)
    key = (pair["repository"], pair["commit_hash"], pair["file_path"])
    prog = programs.get(key)
    blobs = program_blobs.get(key)
    if prog is None or blobs is None:
        return {**base, "bucket": "no_program"}
    source_before, source_after = blobs

    fetched = client.get_file_at_ref(pair["repository"], pair["file_path"],
                                      pair["branch"])
    if fetched is None:
        return {**base, "bucket": "target_absent"}
    target_before = fetched[0].decode("utf-8", "replace")

    # Compute the WORKFLOWBP apply ONCE per pair; reuse for every generator.
    try:
        workflowbp_res = apply_program(prog, target_before, resolver=resolver)
    except Exception as e:
        return {**base, "bucket": "apply_error", "error": str(e)}

    per_baseline: dict[str, dict] = {}
    for name in baselines:
        try:
            patched = _baseline_patched(name, prog, source_before, source_after,
                                          target_before, workflowbp_res)
            per_baseline[name] = _classify(prog, target_before, patched,
                                            workflowbp_res)
        except Exception as e:
            per_baseline[name] = {"bucket": "error", "error": str(e),
                                   "zizmor": False, "actionlint": False}

    return {**base, "per_baseline": per_baseline}


def run(
    baselines: list[str],
    limit: int | None = None,
    workers: int = 8,
    out_dir: Path = REPORTS_DIR,
) -> list[dict]:
    """For each gap pair, run every generator; record per-generator bucket.

    Resume-safe and concurrent: rows append to rq7_rows.jsonl as they
    finish; on re-run we skip any (repo, commit, branch, file) already
    present; the outer loop runs `workers` pairs concurrently.
    """
    from backport_gaps.config import get_github_token
    from backport_gaps.github import GitHubClient
    from backport_ir.pipeline import iter_clean_fix_programs, make_github_resolver
    from common.cache import jsonl_already_done, jsonl_append

    from .common import iter_clean_fixes

    client = GitHubClient(get_github_token())
    resolver = make_github_resolver(client)
    rows_path = out_dir / "rq7_rows.jsonl"

    def _key(r: dict) -> tuple:
        return (r.get("repository", ""), r.get("commit_hash", ""),
                r.get("branch", ""), r.get("file_path", "") or r.get("file", ""))
    done = jsonl_already_done(rows_path, _key)
    if done:
        print(f"resume: skipping {len(done)} rows already in {rows_path}")

    # Pre-index clean-fix programs + source blobs ONCE.
    print("indexing programs + source blobs...", flush=True)
    programs: dict[tuple[str, str, str], object] = {}
    program_blobs: dict[tuple[str, str, str], tuple[str, str]] = {}
    for cf in iter_clean_fixes():
        for f in cf.files:
            program_blobs[(cf.repository, cf.commit_hash, f["file_path"])] = (
                f["before_text"], f["after_text"])
    for _commit_dir, prog in iter_clean_fix_programs():
        programs[(prog.repository, prog.commit_hash, prog.source_file)] = prog
    print(f"  {len(programs)} programs, {len(program_blobs)} blobs indexed",
          flush=True)

    work: list[dict] = []
    for pair in iter_gap_pairs():
        pair_key = (pair["repository"], pair["commit_hash"], pair["branch"],
                    pair["file_path"])
        if pair_key in done:
            continue
        work.append(pair)
        if limit is not None and len(work) >= limit:
            break
    print(f"processing {len(work)} pairs with {workers} workers across "
          f"{len(baselines)} generators ({', '.join(baselines)})", flush=True)

    write_lock = threading.Lock()
    counter = {"n": 0}

    def _process(pair: dict) -> dict:
        try:
            row = _process_pair(pair, client, resolver, programs,
                                 program_blobs, baselines)
        except Exception as e:
            row = {**pair, "bucket": "fatal_error", "error": str(e)}
        with write_lock:
            jsonl_append(rows_path, row)
            counter["n"] += 1
            if counter["n"] % 50 == 0 or counter["n"] == len(work):
                print(f"  {counter['n']}/{len(work)}", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_process, work))

    rows = [json.loads(line) for line in rows_path.open("r", encoding="utf-8")]
    return rows


def write_reports(rows: list[dict], out_dir: Path = REPORTS_DIR) -> None:
    generators = sorted({b for r in rows for b in r.get("per_baseline", {})})
    table_lines = ["| Generator | Accepted | Failed (zizmor) | Failed (actionlint) | Other |",
                   "|---|---:|---:|---:|---:|"]
    for b in generators:
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
                   help="generators to run; workflowbp is the candidate, the "
                        "rest are baselines; add 'llm' for the Claude baseline")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=8,
                   help="ThreadPoolExecutor worker count (default 8)")
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
        rows = run(args.baselines, limit=args.limit, workers=args.workers)
    write_reports(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
