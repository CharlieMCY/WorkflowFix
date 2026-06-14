"""RQ6 (Reproducibility) — WORKFLOWBP vs. maintainer-written backport
on the 242 confirmed true backports.

For each (commit, release branch) classified as true_backport:
  1. Fetch the workflow file at the release branch state JUST BEFORE the
     maintainer's backport commit (= "target_before"), and at the
     backport commit itself (= "target_after" = the maintainer's
     ground-truth backport).
  2. Compile the master clean-fix into a WSP, apply to target_before
     to get WORKFLOWBP's "our_patched".
  3. Compare our_patched against target_after by four levels:
        byte_equal       byte-for-byte identical
        ast_equal        same when normalised through ruamel
        effect_equal     both pass zizmor_local + actionlint
                         on (target_before, candidate)
        divergent        otherwise (one passes, one doesn't, or both fail)
  4. Aggregate into a table.

Writes per-row outcomes + summary table to analysis_tools/reports/rq6_*.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator

from .common import (
    OUTPUT_DIR, REPORTS_DIR, iter_true_backports, pct,
    run_oracles, write_jsonl, write_table,
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


def _compile_from_clean_fix(repo: str, commit_hash: str, file_path: str):
    """Look up the precompiled (or freshly compile) IRProgram for the master
    fix at this (commit, file)."""
    from backport_ir.compile import compile_program
    from backport_ir.pipeline import iter_clean_fix_programs
    for _commit_dir, prog in iter_clean_fix_programs():
        if (prog.repository == repo and prog.commit_hash == commit_hash
                and prog.source_file == file_path):
            return prog
    return None


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


def classify_one(
    client, resolver, bp_row: dict, out_dir: Path,
) -> dict | None:
    """Return one classification row, or None on fetch failure."""
    from backport_ir.apply import apply_program

    repo = bp_row["repository"]
    bp_sha = bp_row["backport_commit_sha"]
    if not bp_sha:
        return {"repository": repo, "branch": bp_row["branch"],
                "outcome": "missing_backport_sha"}

    parent = _parent_sha(client, repo, bp_sha)
    if parent is None:
        return {"repository": repo, "branch": bp_row["branch"],
                "outcome": "no_parent_commit"}

    # Pre-/post-backport state on the release branch
    # NB. The master commit's clean fix may touch multiple files; we evaluate
    # one at a time. The clean-fix iter_programs yields per (commit, file).
    from backport_ir.pipeline import iter_clean_fix_programs
    for _commit_dir, prog in iter_clean_fix_programs():
        if prog.repository != repo or prog.commit_hash != bp_row["commit_hash"]:
            continue

        path = prog.source_file
        target_before = _fetch_text(client, repo, path, parent)
        target_after = _fetch_text(client, repo, path, bp_sha)
        if target_before is None or target_after is None:
            yield_outcome = "file_absent_at_parent_or_backport"
            yield {
                "repository": repo,
                "commit_hash": bp_row["commit_hash"],
                "branch": bp_row["branch"],
                "backport_commit_sha": bp_sha,
                "file": path,
                "outcome": yield_outcome,
            }
            continue

        # Apply WORKFLOWBP
        res = apply_program(prog, target_before, resolver=resolver)
        our_patched = res.patched_text

        # Byte equal?
        if our_patched == target_after:
            outcome = "byte_equal"
        elif _ast_normalise(our_patched) == _ast_normalise(target_after):
            outcome = "ast_equal"
        else:
            # Effect equal? both candidates accepted by both oracles
            verdict_ours = run_oracles(prog, target_before, our_patched, res)
            # For maintainer's patch we can't reuse the same `res`; build a
            # trivial stand-in result so the locality oracle has a scope.
            from backport_ir.apply import ApplyResult
            fake_result = ApplyResult(patched_text=target_after,
                                       target_idents=list(prog.target_idents),
                                       edits=res.edits)
            verdict_theirs = run_oracles(prog, target_before, target_after, fake_result)
            if verdict_ours.accepted and verdict_theirs.accepted:
                outcome = "effect_equal"
            else:
                outcome = "divergent"

        # Persist patched output for case-by-case inspection
        safe = (f"{repo.replace('/', '__')}__{bp_row['commit_hash'][:10]}"
                f"__{bp_row['branch'].replace('/', '__')}__"
                f"{path.replace('/', '__')}")
        case_dir = out_dir / "cases" / safe
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "target_before.yml").write_text(target_before)
        (case_dir / "target_after_maintainer.yml").write_text(target_after)
        (case_dir / "our_patched.yml").write_text(our_patched)
        (case_dir / "outcome.txt").write_text(outcome + "\n")

        yield {
            "repository": repo,
            "commit_hash": bp_row["commit_hash"],
            "branch": bp_row["branch"],
            "backport_commit_sha": bp_sha,
            "file": path,
            "outcome": outcome,
        }


def run(limit: int | None = None) -> dict:
    from backport_gaps.config import get_github_token
    from backport_gaps.github import GitHubClient
    from backport_ir.pipeline import make_github_resolver
    from common.cache import jsonl_already_done, jsonl_append

    client = GitHubClient(get_github_token())
    resolver = make_github_resolver(client)
    out_dir = REPORTS_DIR / "rq6"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = REPORTS_DIR / "rq6_rows.jsonl"

    # Resume: skip (repo, commit, branch, file) triples already in the JSONL.
    def _key(r: dict) -> tuple:
        return (r.get("repository", ""), r.get("commit_hash", ""),
                r.get("branch", ""), r.get("file", ""))
    done = jsonl_already_done(rows_path, _key)
    if done:
        print(f"resume: skipping {len(done)} rows already in {rows_path}")

    n_processed = 0
    for bp in iter_true_backports():
        if limit is not None and n_processed >= limit:
            break
        try:
            for r in classify_one(client, resolver, bp, out_dir):
                if r is None:
                    continue
                if _key(r) in done:
                    continue
                jsonl_append(rows_path, r)
                done.add(_key(r))
                print(f"  {r['repository']}@{r['commit_hash'][:8]} "
                      f"{r['branch']}  -> {r['outcome']}", flush=True)
        except Exception as e:
            err_row = {"repository": bp["repository"],
                       "commit_hash": bp["commit_hash"],
                       "branch": bp["branch"], "file": "",
                       "outcome": "error", "error": str(e)}
            if _key(err_row) not in done:
                jsonl_append(rows_path, err_row)
                done.add(_key(err_row))
        n_processed += 1

    # Re-read the JSONL so callers see every row (new + resumed)
    rows = [json.loads(line) for line in rows_path.open("r", encoding="utf-8")]
    return {"rows": rows}


def write_reports(data: dict, out_dir: Path = REPORTS_DIR) -> None:
    rows = data["rows"]
    # rows are already row-by-row appended to rq6_rows.jsonl by `run()`;
    # we only rewrite the summary table here.

    buckets: Counter[str] = Counter(r["outcome"] for r in rows)
    total = sum(buckets.values())
    order = ["byte_equal", "ast_equal", "effect_equal", "divergent",
             "no_parent_commit", "file_absent_at_parent_or_backport",
             "missing_backport_sha", "error"]
    summary_rows = []
    for b in order:
        c = buckets.get(b, 0)
        summary_rows.append((b, c, pct(c, total)))
    write_table(out_dir / "rq6_summary.md", summary_rows)

    # The headline reproducibility rate combines byte/ast/effect.
    repro = sum(buckets.get(b, 0)
                for b in ("byte_equal", "ast_equal", "effect_equal"))
    print(f"RQ6: {repro}/{total} reproduce maintainer's backport "
          f"({pct(repro, total)}); table -> {out_dir}/rq6_summary.md")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of true backports processed")
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
        data = run(limit=args.limit)
    write_reports(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
