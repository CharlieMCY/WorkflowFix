"""Walk release-branch history to (a) confirm true backports and (b) compute
backport lag time.

For each `already_fixed` branch B from `gaps.jsonl` and each target workflow
file F, walk the file's history on B from newest to oldest. The findings of
interest are the SET OF zizmor RULE IDENTS that master commit C removed
(`V_fixed_idents` of C). Possible outcomes:

  confirmed_backport
        A historical version of F on B had at least one finding of an ident
        in `V_fixed_idents`. The next-newer commit on B that touched F is the
        backport commit. lag_days = T_backport - T_master.
  never_had_it
        Walking the whole capped history, no historical version of F on B
        ever had any of these idents → finding's absence on B is not a
        backport of C; it's a case where the release branch's code path
        never had the master-fixed issue.
  inconclusive
        Hit the history cap before deciding, or every history fetch errored.

The route (specific YAML location) is intentionally not compared — release
branches diverge structurally, so we match on rule ident only.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import datetime
from pathlib import Path
from typing import Any

from tqdm import tqdm

from pattern_miner.scan import scan_bytes

from .config import GAPS_DIR, get_github_tokens
from .github import GitHubClient, GitHubError


# How many historical versions of a file we'll scan per branch before giving
# up. Raised from 10 to 50 to recover the `inconclusive` cohort whose
# backport event landed deeper in the branch's file history.
MAX_HISTORY_COMMITS = 50

# Time budget per master commit. With per-record branch concurrency the wall
# clock is roughly (#branches / #workers) × per-branch-time. At 50k scale,
# under variable network conditions, 8 min wasn't enough for ~50 heavy
# records (40+ branches each). Raised to 16 min for the retry pass.
PER_RECORD_TIMEOUT_S = 16 * 60
MAX_WORKERS_PER_RECORD = 8


def _days_between(t_master: str, t_backport: str) -> float:
    a = datetime.fromisoformat(t_master.replace("Z", "+00:00"))
    b = datetime.fromisoformat(t_backport.replace("Z", "+00:00"))
    return (b - a).total_seconds() / 86400.0


def find_backport_event(
    client: GitHubClient,
    repo: str,
    branch: str,
    path: str,
    target_idents: set[str],
    master_commit_date: str | None,
    max_commits: int = MAX_HISTORY_COMMITS,
) -> dict[str, Any]:
    """Classify whether `branch`'s history backported C's V_fixed_idents."""
    last_clean: dict | None = None
    n_scanned = 0
    n_history_seen = 0

    try:
        commit_iter = client.iter_commits_touching_file(repo, branch, path)
    except GitHubError as e:
        return {"status": "error", "error": f"list_commits: {e}",
                "n_history_commits_scanned": 0}

    for commit in commit_iter:
        n_history_seen += 1
        if n_scanned >= max_commits:
            return {
                "status": "inconclusive",
                "reason": "history_cap_reached",
                "n_history_commits_seen": n_history_seen,
                "n_history_commits_scanned": n_scanned,
            }
        sha = commit["sha"]
        date = commit["commit"]["committer"]["date"]
        try:
            fetched = client.get_file_at_ref(repo, path, sha)
        except GitHubError:
            continue
        if fetched is None:
            continue
        content, _ = fetched
        res = scan_bytes(content)
        if not res.get("ok"):
            continue
        n_scanned += 1
        present = {f["ident"] for f in res["findings"]}
        if present & target_idents:
            # Found a version of F where the target ident(s) were present.
            # The most-recent already-clean commit above this is the backport.
            result: dict[str, Any] = {
                "status": "confirmed_backport",
                "n_history_commits_seen": n_history_seen,
                "n_history_commits_scanned": n_scanned,
                "fix_was_present_at_commit": sha,
                "fix_was_present_at_date": date,
                "fix_was_present_idents": sorted(present & target_idents),
            }
            if last_clean is not None:
                bk_sha = last_clean["sha"]
                bk_date = last_clean["commit"]["committer"]["date"]
                result["backport_commit_sha"] = bk_sha
                result["backport_commit_date"] = bk_date
                if master_commit_date:
                    result["lag_days"] = _days_between(master_commit_date, bk_date)
            else:
                # F present at the very newest history commit — impossible if
                # this branch was in `already_fixed` (HEAD must be clean).
                # Defensive: mark inconclusive.
                result["status"] = "inconclusive"
                result["reason"] = "fix_present_at_head_contradicts_already_fixed"
            return result
        last_clean = commit

    if n_scanned == 0:
        return {"status": "inconclusive",
                "reason": "no_scannable_history",
                "n_history_commits_seen": n_history_seen,
                "n_history_commits_scanned": 0}
    return {
        "status": "never_had_it",
        "n_history_commits_seen": n_history_seen,
        "n_history_commits_scanned": n_scanned,
    }


# --- driver ----------------------------------------------------------------


def _summarize_branch(history_classifications: list[dict]) -> dict:
    """Roll up per-file classifications into a single branch verdict."""
    statuses = {c["status"] for c in history_classifications}
    branch: dict[str, Any] = {}
    if "confirmed_backport" in statuses:
        branch["backport_status"] = "confirmed_backport"
        lags = [c["lag_days"] for c in history_classifications
                if c["status"] == "confirmed_backport" and "lag_days" in c]
        if lags:
            branch["lag_days"] = max(lags)
            branch["lag_days_per_file"] = lags
    elif statuses and all(s in ("never_had_it", "error", "inconclusive") for s in statuses):
        # If any file was never_had_it, we lean that way; only error/inconclusive
        # means we genuinely can't tell.
        if "never_had_it" in statuses:
            branch["backport_status"] = "never_had_it"
        else:
            branch["backport_status"] = "inconclusive"
    else:
        branch["backport_status"] = "inconclusive"
    return branch


def _classify_one_branch(
    client: GitHubClient,
    repo: str,
    branch: str,
    target_files: list[str],
    target_idents: set[str],
    master_date: str | None,
    max_commits: int,
) -> list[dict[str, Any]]:
    """Per-branch worker: walk every target_file's history on this branch.

    Used as the unit of concurrency inside `run`. requests.Session is
    thread-safe for concurrent GETs, so multiple instances of this function
    share the same `client` across threads safely.
    """
    per_file: list[dict[str, Any]] = []
    for path in target_files:
        try:
            cls = find_backport_event(
                client, repo, branch, path,
                target_idents, master_date,
                max_commits=max_commits,
            )
        except GitHubError as e:
            cls = {"status": "error", "error": str(e)[:200]}
        cls["file_path"] = path
        per_file.append(cls)
    return per_file


def classify_record(
    client: GitHubClient,
    rec: dict,
    master_date_cache: dict | None = None,
    max_workers: int = MAX_WORKERS_PER_RECORD,
) -> dict:
    """Augment one gap-audit record in place with per-branch history
    classification (the same logic the `run()` driver applies to each
    record in gaps.jsonl). Returns the augmented `rec`.

    Used by `backport_gaps.stream_all` so history classification can run
    immediately after each gap audit during a streaming end-to-end pass.
    """
    if master_date_cache is None:
        master_date_cache = {}

    if rec.get("status") != "ok" or not rec.get("already_fixed_branches"):
        return rec

    repo = rec["repository"]
    sha = rec["commit_hash"]
    target_idents = set(rec.get("V_fixed_idents") or [])
    target_files = rec.get("target_files") or []
    t_start = time.time()

    key = (repo, sha)
    if key not in master_date_cache:
        try:
            ci = client.get_commit(repo, sha)
            master_date_cache[key] = (
                ci["commit"]["committer"]["date"] if ci else None
            )
        except GitHubError:
            master_date_cache[key] = None
    master_date = master_date_cache[key]
    rec["master_commit_date"] = master_date

    timed_out = False
    executor = ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="hist"
    )
    try:
        futures = {
            executor.submit(
                _classify_one_branch,
                client, repo, br["branch"], target_files,
                target_idents, master_date, MAX_HISTORY_COMMITS,
            ): br
            for br in rec["already_fixed_branches"]
        }
        remaining = dict(futures)
        try:
            for fut in as_completed(futures, timeout=PER_RECORD_TIMEOUT_S):
                br = remaining.pop(fut, None)
                if br is None:
                    continue
                try:
                    per_file = fut.result()
                except Exception as e:
                    per_file = [{
                        "status": "error",
                        "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "file_path": "?",
                    }]
                br["history_classifications"] = per_file
                br.update(_summarize_branch(per_file))
        except FuturesTimeoutError:
            timed_out = True
            for fut, br in remaining.items():
                if fut.done():
                    try:
                        per_file = fut.result(timeout=0)
                        br["history_classifications"] = per_file
                        br.update(_summarize_branch(per_file))
                    except Exception:
                        br["history_classifications"] = []
                        br["backport_status"] = "timed_out"
                else:
                    br["history_classifications"] = []
                    br["backport_status"] = "timed_out"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    rec["record_timed_out"] = timed_out
    rec["record_duration_s"] = round(time.time() - t_start, 1)
    return rec


def run(
    in_path: Path | None = None,
    out_path: Path | None = None,
    limit: int | None = None,
    max_workers: int = MAX_WORKERS_PER_RECORD,
) -> Path:
    """Walk file history for each already_fixed branch and confirm/timestamp the backport.

    Resume-safe: if `out_path` already exists, rows whose (repository, commit_hash)
    pair is already present are skipped. This lets a killed/restarted run pick up
    where it left off without re-doing the expensive ones.

    Within each master commit, the per-branch history walks run on a
    ThreadPoolExecutor (default 8 workers). This is the main lever that lets
    heavy commits (40+ release branches) finish within the per-record budget.
    """
    in_path = in_path or (GAPS_DIR / "gaps.jsonl")
    out_path = out_path or (GAPS_DIR / "gaps_with_history.jsonl")
    client = GitHubClient(get_github_tokens())
    master_date_cache: dict[tuple[str, str], str | None] = {}

    # Build skip set from existing output (resume).
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        with out_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    r = json.loads(line)
                    done.add((r["repository"], r["commit_hash"]))
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"resume: skipping {len(done)} already-processed records")

    rows = [json.loads(line) for line in in_path.open()]
    if limit is not None:
        rows = rows[:limit]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fp:
        for rec in tqdm(rows, desc="history"):
            if (rec["repository"], rec["commit_hash"]) in done:
                continue
            if rec.get("status") != "ok" or not rec.get("already_fixed_branches"):
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fp.flush()
                continue
            repo = rec["repository"]
            sha = rec["commit_hash"]
            target_idents = set(rec["V_fixed_idents"])
            target_files = rec.get("target_files", [])
            t_start = time.time()
            timed_out = False

            # Cache master commit date (single call per master commit)
            key = (repo, sha)
            if key not in master_date_cache:
                try:
                    ci = client.get_commit(repo, sha)
                    master_date_cache[key] = (
                        ci["commit"]["committer"]["date"] if ci else None
                    )
                except GitHubError:
                    master_date_cache[key] = None
            master_date = master_date_cache[key]
            rec["master_commit_date"] = master_date

            # Per-branch concurrency. ThreadPoolExecutor wraps the per-branch
            # history walks; as_completed yields each branch's result as it
            # finishes. The (deadline-style) timeout on as_completed bounds
            # total wait — if it fires, unfinished futures are marked timed_out.
            executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="hist"
            )
            try:
                futures = {
                    executor.submit(
                        _classify_one_branch,
                        client, repo, br["branch"], target_files,
                        target_idents, master_date, MAX_HISTORY_COMMITS,
                    ): br
                    for br in rec["already_fixed_branches"]
                }
                remaining = dict(futures)

                try:
                    for fut in as_completed(futures, timeout=PER_RECORD_TIMEOUT_S):
                        br = remaining.pop(fut, None)
                        if br is None:
                            continue
                        try:
                            per_file = fut.result()
                        except Exception as e:
                            per_file = [{
                                "status": "error",
                                "error": f"{type(e).__name__}: {str(e)[:200]}",
                                "file_path": "?",
                            }]
                        br["history_classifications"] = per_file
                        br.update(_summarize_branch(per_file))
                except FuturesTimeoutError:
                    timed_out = True
                    # When `as_completed` raises, futures that had already
                    # finished but weren't yielded are still in `remaining`.
                    # Collect their results — only mark truly-unfinished
                    # futures as timed_out.
                    for fut, br in remaining.items():
                        if fut.done():
                            try:
                                per_file = fut.result(timeout=0)
                                br["history_classifications"] = per_file
                                br.update(_summarize_branch(per_file))
                            except Exception:
                                br["history_classifications"] = []
                                br["backport_status"] = "timed_out"
                        else:
                            br["history_classifications"] = []
                            br["backport_status"] = "timed_out"
            finally:
                # cancel_futures=True drops queued futures that haven't started;
                # running threads finish naturally (Python can't kill threads).
                executor.shutdown(wait=False, cancel_futures=True)

            rec["record_timed_out"] = timed_out
            rec["record_duration_s"] = round(time.time() - t_start, 1)
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fp.flush()
    return out_path


# --- post-hoc re-classification based on lag direction ---------------------
# A branch's `backport_status: confirmed_backport` only means "F was once
# present in the branch's recent history and isn't now". Whether that
# transition was caused by master's fix depends on temporal causality:
#   lag > +1 day  → release fixed AFTER master  → TRUE BACKPORT
#   |lag| <= 1 day → same day      → likely simultaneous / coincidental
#   lag < -1 day  → release fixed BEFORE master → INDEPENDENT prior fix
# Negative-lag cases also include "release branch barely touches the file,
# we picked up an unrelated old `clean` commit as `last_clean`" — which is
# why the strict `lag > 1` cut is so important.

SAME_DAY_THRESHOLD_DAYS = 1.0


def _refine_backport_status(br: dict) -> str:
    """Map the raw `backport_status` + lag onto a sharper bucket."""
    s = br.get("backport_status", "missing")
    if s != "confirmed_backport":
        return s
    lag = br.get("lag_days")
    if lag is None:
        return "confirmed_backport_no_lag"
    if lag > SAME_DAY_THRESHOLD_DAYS:
        return "true_backport"
    if lag < -SAME_DAY_THRESHOLD_DAYS:
        return "independent_prior_fix"
    return "same_day_fix"


def _inconclusive_subreason(br: dict) -> str:
    """For inconclusive branches, surface the dominant per-file sub-reason."""
    reasons = Counter()
    for c in br.get("history_classifications", []):
        if c.get("status") == "inconclusive":
            reasons[c.get("reason", "unknown")] += 1
        elif c.get("status") == "error":
            reasons["scan_error"] += 1
    return reasons.most_common(1)[0][0] if reasons else "unknown"


from collections import Counter


def summarize(in_path: Path | None = None) -> None:
    in_path = in_path or (GAPS_DIR / "gaps_with_history.jsonl")

    refined: Counter[str] = Counter()
    incon_subreasons: Counter[str] = Counter()
    branches_audited = 0
    lags_by_bucket: dict[str, list[float]] = {
        "true_backport": [], "same_day_fix": [], "independent_prior_fix": [],
    }
    per_ident: dict[str, Counter[str]] = {
        "true_backport": Counter(), "same_day_fix": Counter(),
        "independent_prior_fix": Counter(),
    }

    for line in in_path.open():
        rec = json.loads(line)
        for br in rec.get("already_fixed_branches", []):
            branches_audited += 1
            refined_s = _refine_backport_status(br)
            refined[refined_s] += 1
            if refined_s == "inconclusive":
                incon_subreasons[_inconclusive_subreason(br)] += 1
            if refined_s in lags_by_bucket:
                lag = br.get("lag_days")
                if lag is not None:
                    lags_by_bucket[refined_s].append(lag)
                for ident in rec.get("V_fixed_idents", []):
                    per_ident[refined_s][ident] += 1

    print(f"=== already_fixed branches re-classified (n={branches_audited}) ===")
    order = ["true_backport", "same_day_fix", "independent_prior_fix",
             "inconclusive", "never_had_it", "timed_out"]
    other = [k for k in refined if k not in order]
    for k in order + other:
        if k in refined:
            n = refined[k]
            print(f"  {k:>22}  {n:>5}  ({100*n/max(branches_audited,1):.1f}%)")

    if incon_subreasons:
        print()
        print("  inconclusive — by per-file sub-reason:")
        for r, n in incon_subreasons.most_common():
            print(f"    {r:>40}  {n:>4}")

    # Detailed lag distribution for the TRUE backport bucket
    true_lags = sorted(lags_by_bucket["true_backport"])
    if true_lags:
        n = len(true_lags)
        def pct(p): return true_lags[min(int(p * n), n - 1)]
        print()
        print(f"=== TRUE BACKPORT lag (days), n={n} ===")
        print(f"  min:    {true_lags[0]:>10.2f}")
        print(f"  p25:    {pct(0.25):>10.2f}")
        print(f"  median: {pct(0.50):>10.2f}")
        print(f"  p75:    {pct(0.75):>10.2f}")
        print(f"  p90:    {pct(0.90):>10.2f}")
        print(f"  max:    {true_lags[-1]:>10.2f}")
        print(f"  mean:   {sum(true_lags)/n:>10.2f}")
        print()
        print("  bucketed:")
        buckets = [(1, 7, "1-7 days"), (7, 30, "1-4 weeks"),
                   (30, 90, "1-3 months"), (90, 365, "3-12 months"),
                   (365, 1e9, "> 1 year")]
        for lo, hi, name in buckets:
            c = sum(1 for x in true_lags if lo < x <= hi)
            print(f"    {name:>14}:  {c:>4}")

    if any(per_ident.values()):
        print()
        print("=== zizmor idents — TRUE backports vs INDEPENDENT prior fixes ===")
        all_idents = set().union(*(c.keys() for c in per_ident.values()))
        print(f"  {'ident':>26}  {'true':>5}  {'sameday':>7}  {'indep_prior':>11}")
        for ident in sorted(all_idents,
                            key=lambda i: -per_ident["true_backport"][i]):
            tb = per_ident["true_backport"][ident]
            sd = per_ident["same_day_fix"][ident]
            ip = per_ident["independent_prior_fix"][ident]
            print(f"  {ident:>26}  {tb:>5}  {sd:>7}  {ip:>11}")
