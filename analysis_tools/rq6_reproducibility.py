"""RQ6 (Reproducibility) — WORKFLOWBP vs. maintainer-written backport
on the confirmed true backports.

For each (commit, release branch, file) classified as true_backport:
  1. Fetch the workflow file at the release branch state JUST BEFORE the
     maintainer's backport commit (= "target_before"), and at the
     backport commit itself (= "target_after" = the maintainer's
     ground-truth backport).
  2. Look up the precompiled WSP for the master clean-fix and apply to
     target_before to get WORKFLOWBP's "our_patched".
  3. Compare our_patched against target_after by four levels:
        byte_equal       byte-for-byte identical
        ast_equal        same when normalised through ruamel
        effect_equal     both pass zizmor_local + actionlint
                         on (target_before, candidate)
        divergent        otherwise (one passes, one doesn't, or both fail)
  4. Aggregate into a table.

Writes per-row outcomes + summary table to analysis_tools/reports/$TAG/rq6_*.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import (
    OUTPUT_DIR, REPORTS_DIR, iter_true_backports, pct,
    run_oracles, write_table,
)


def _fetch_text(client, repo: str, path: str, ref: str) -> str | None:
    fetched = client.get_file_at_ref(repo, path, ref)
    if fetched is None:
        return None
    return fetched[0].decode("utf-8", "replace")


def _parent_sha(client, repo: str, sha: str) -> str | None:
    """The first-parent SHA of `sha` on `repo`, or None on failure."""
    from backport_gaps.github import GitHubError
    try:
        commit = client.get_commit(repo, sha)
    except GitHubError:
        return None
    if not commit:
        return None
    parents = commit.get("parents", []) or []
    return parents[0]["sha"] if parents else None


def _ast_normalise(text: str) -> str:
    """Round-trip text through ruamel for whitespace/format-insensitive compare."""
    from io import StringIO
    from backport_ir._yaml import rt_yaml
    try:
        y = rt_yaml()
        data = y.load(text)
        buf = StringIO()
        y.dump(data, buf)
        return buf.getvalue()
    except Exception:
        return text


def classify_one(client, resolver, bp_row: dict, programs: dict,
                  out_dir: Path) -> dict:
    """Return ONE classification row for one (repo, commit, branch, file)
    true-backport entry. Looks up the precompiled IRProgram in `programs`
    keyed by (repo, commit, file_path)."""
    from backport_ir.apply import ApplyResult, apply_program

    repo = bp_row["repository"]
    bp_sha = bp_row["backport_commit_sha"]
    file_path = bp_row["file_path"]
    base = {"repository": repo, "commit_hash": bp_row["commit_hash"],
            "branch": bp_row["branch"], "file": file_path,
            "backport_commit_sha": bp_sha}

    if not bp_sha:
        return {**base, "outcome": "missing_backport_sha"}

    prog = programs.get((repo, bp_row["commit_hash"], file_path))
    if prog is None:
        return {**base, "outcome": "no_program"}

    parent = _parent_sha(client, repo, bp_sha)
    if parent is None:
        return {**base, "outcome": "no_parent_commit"}

    target_before = _fetch_text(client, repo, file_path, parent)
    target_after = _fetch_text(client, repo, file_path, bp_sha)
    if target_before is None or target_after is None:
        return {**base, "outcome": "file_absent_at_parent_or_backport"}

    res = apply_program(prog, target_before, resolver=resolver)
    our_patched = res.patched_text

    if our_patched == target_after:
        outcome = "byte_equal"
    elif _ast_normalise(our_patched) == _ast_normalise(target_after):
        outcome = "ast_equal"
    else:
        verdict_ours = run_oracles(prog, target_before, our_patched, res)
        fake_result = ApplyResult(patched_text=target_after,
                                   target_idents=list(prog.target_idents),
                                   edits=res.edits)
        verdict_theirs = run_oracles(prog, target_before, target_after,
                                      fake_result)
        if verdict_ours.accepted and verdict_theirs.accepted:
            outcome = "effect_equal"
        else:
            outcome = "divergent"

    safe = (f"{repo.replace('/', '__')}__{bp_row['commit_hash'][:10]}"
            f"__{bp_row['branch'].replace('/', '__')}__"
            f"{file_path.replace('/', '__')}")
    case_dir = out_dir / "cases" / safe
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "target_before.yml").write_text(target_before)
    (case_dir / "target_after_maintainer.yml").write_text(target_after)
    (case_dir / "our_patched.yml").write_text(our_patched)
    (case_dir / "outcome.txt").write_text(outcome + "\n")

    return {**base, "outcome": outcome}


def run(limit: int | None = None, workers: int = 8) -> dict:
    from backport_gaps.config import get_github_token
    from backport_gaps.github import GitHubClient
    from backport_ir.pipeline import iter_clean_fix_programs, make_github_resolver
    from common.cache import jsonl_already_done, jsonl_append

    client = GitHubClient(get_github_token())
    resolver = make_github_resolver(client)
    out_dir = REPORTS_DIR / "rq6"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = REPORTS_DIR / "rq6_rows.jsonl"

    # One-shot index of compiled programs: (repo, sha, file_path) -> IRProgram.
    # Avoids re-scanning the programs/ directory per true-backport row.
    print("indexing compiled programs...", flush=True)
    programs: dict[tuple[str, str, str], object] = {}
    for _commit_dir, prog in iter_clean_fix_programs():
        programs[(prog.repository, prog.commit_hash, prog.source_file)] = prog
    print(f"  {len(programs)} programs indexed", flush=True)

    def _key(r: dict) -> tuple:
        return (r.get("repository", ""), r.get("commit_hash", ""),
                r.get("branch", ""), r.get("file", ""))
    done = jsonl_already_done(rows_path, _key)
    if done:
        print(f"resume: skipping {len(done)} rows already in {rows_path}")

    work = []
    for bp in iter_true_backports():
        bp_key = (bp["repository"], bp["commit_hash"],
                  bp["branch"], bp.get("file_path", ""))
        if bp_key in done:
            continue
        work.append(bp)
        if limit is not None and len(work) >= limit:
            break
    print(f"processing {len(work)} rows with {workers} workers", flush=True)

    write_lock = threading.Lock()
    counter = {"n": 0}

    def _process(bp: dict) -> dict:
        try:
            row = classify_one(client, resolver, bp, programs, out_dir)
        except Exception as e:
            row = {"repository": bp["repository"],
                   "commit_hash": bp["commit_hash"],
                   "branch": bp["branch"],
                   "file": bp.get("file_path", ""),
                   "outcome": "error",
                   "error": str(e)}
        with write_lock:
            jsonl_append(rows_path, row)
            counter["n"] += 1
            if counter["n"] % 10 == 0 or counter["n"] == len(work):
                print(f"  {counter['n']}/{len(work)} "
                      f"({row.get('outcome', '?')})", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_process, work))

    rows = [json.loads(line) for line in rows_path.open("r", encoding="utf-8")]
    return {"rows": rows}


def write_reports(data: dict, out_dir: Path = REPORTS_DIR) -> None:
    rows = data["rows"]
    buckets: Counter[str] = Counter(r["outcome"] for r in rows)
    total = sum(buckets.values())
    order = ["byte_equal", "ast_equal", "effect_equal", "divergent",
             "no_parent_commit", "file_absent_at_parent_or_backport",
             "missing_backport_sha", "no_program", "error"]
    summary_rows = []
    for b in order:
        c = buckets.get(b, 0)
        summary_rows.append((b, c, pct(c, total)))
    write_table(out_dir / "rq6_summary.md", summary_rows)

    repro = sum(buckets.get(b, 0)
                for b in ("byte_equal", "ast_equal", "effect_equal"))
    print(f"RQ6: {repro}/{total} reproduce maintainer's backport "
          f"({pct(repro, total)}); table -> {out_dir}/rq6_summary.md")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of true-backport rows processed")
    p.add_argument("--workers", type=int, default=8,
                   help="ThreadPoolExecutor worker count (default 8)")
    p.add_argument("--aggregate-only", action="store_true",
                   help="skip the GitHub fetch + apply; just re-aggregate "
                        "an existing rq6_rows.jsonl")
    args = p.parse_args()

    if args.aggregate_only:
        rows_path = REPORTS_DIR / "rq6_rows.jsonl"
        if not rows_path.exists():
            print(f"{rows_path} missing — run without --aggregate-only first.")
            return 1
        data = {"rows": [json.loads(l) for l in rows_path.open("r")]}
    else:
        data = run(limit=args.limit, workers=args.workers)
    write_reports(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
