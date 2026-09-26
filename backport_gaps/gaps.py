"""Find backport gaps: for each clean-fix commit, identify other branches of
the same repo where the fixed zizmor finding(s) are still present.

Flow for one clean-fix commit C (repo R, file F, V_fixed = {(ident, route), ...}):

  1. Ask GitHub for R's default branch and the list of all branches.
  2. Verify C is on the default branch's history (else skip — it's already
     a backport itself, or lives on a topic branch).
  3. Filter the other branches to release-style names (branches.py).
  4. For each release-style branch B:
        - Fetch F at B's HEAD.
        - If F doesn't exist on B  -> classify as "inapplicable".
        - Otherwise zizmor-scan F at B's HEAD.
        - Compute which of C's V_fixed (ident, route) pairs are present in B.
        - If any -> "gap"; if none -> "already_fixed".

Output: output/backport_gaps/gaps.jsonl, one row per clean-fix commit.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from tqdm import tqdm

from pattern_miner.scan import scan_bytes

from .branches import filter_release_branches
from .config import GAPS_DIR, OUTPUT_DIR, get_github_tokens
from .github import GitHubClient, GitHubError


def _clean_fix_meta_paths() -> list[Path]:
    """Locate every clean_fixes/<commit>/meta.json the patterns pipeline wrote."""
    base = OUTPUT_DIR / "clean_fixes"
    if not base.exists():
        raise FileNotFoundError(
            f"{base} not found — run `pattern_miner pipeline` first to produce "
            "the clean-fix dump."
        )
    return sorted(p for p in base.glob("*/meta.json"))


def _scan_idents(content: bytes) -> set[str] | None:
    """Run zizmor and return the set of finding `ident` (rule-name) values.

    `route` (YAML path) is intentionally not used: release branches diverge
    structurally so the same rule type fires at a different YAML location, and
    a strict (ident, route) match misses every such case. Matching on ident
    captures "the kind of vulnerability is still present" — the semantic the
    paper cares about.
    """
    res = scan_bytes(content)
    if not res.get("ok"):
        return None
    return {f["ident"] for f in res["findings"]}


def find_gap_for_commit(
    client: GitHubClient,
    meta: dict,
) -> dict | None:
    """Audit one clean-fix commit's release branches. Returns a gap record."""
    repo = meta["repository"]
    sha = meta["commit_hash"]

    try:
        repo_info = client.get_repo(repo)
    except GitHubError as e:
        return {"repository": repo, "commit_hash": sha,
                "status": "repo_error", "error": str(e)}

    default_branch = repo_info.get("default_branch") or "main"

    # confirm C is on master history
    try:
        on_default = client.commit_in_branch_history(repo, default_branch, sha)
    except GitHubError as e:
        return {"repository": repo, "commit_hash": sha,
                "status": "compare_error", "error": str(e)}
    if not on_default:
        return {"repository": repo, "commit_hash": sha,
                "status": "not_on_default", "default_branch": default_branch}

    # collect release-style branches
    try:
        all_branches = list(client.iter_branches(repo))
    except GitHubError as e:
        return {"repository": repo, "commit_hash": sha,
                "status": "branches_error", "error": str(e)}
    release_branches = filter_release_branches(all_branches, default_branch)

    # The SET OF IDENTS master fixed; this is the level-1 pattern key from the
    # patterns catalog, and is exactly what we'll look for on each release branch.
    target_idents: set[str] = set(meta.get("V_fixed_idents") or [])
    paths_to_audit: list[str] = [
        f["file_path"] for f in meta.get("files", []) if f.get("V_fixed")
    ]

    gap_branches: list[dict] = []
    already_fixed_branches: list[dict] = []
    inapplicable_branches: list[dict] = []

    for b in release_branches:
        branch_name = b["name"]
        branch_head_sha = b.get("commit", {}).get("sha", "")
        v_present: set[str] = set()
        files_scanned: list[dict] = []
        any_file_present = False

        for path in paths_to_audit:
            fetched = client.get_file_at_ref(repo, path, branch_name)
            if fetched is None:
                files_scanned.append({"file_path": path, "status": "absent"})
                continue
            any_file_present = True
            content, _blob_sha = fetched
            idents = _scan_idents(content)
            if idents is None:
                files_scanned.append({"file_path": path, "status": "scan_failed"})
                continue
            present = target_idents & idents
            v_present.update(present)
            files_scanned.append({
                "file_path": path,
                "status": "ok",
                "V_present_idents": sorted(present),
            })

        if not any_file_present:
            inapplicable_branches.append({
                "branch": branch_name,
                "branch_head_sha": branch_head_sha,
                "files": files_scanned,
            })
            continue

        if v_present:
            gap_branches.append({
                "branch": branch_name,
                "branch_head_sha": branch_head_sha,
                "V_present_idents": sorted(v_present),
                "files": files_scanned,
            })
        else:
            already_fixed_branches.append({
                "branch": branch_name,
                "branch_head_sha": branch_head_sha,
                "files": files_scanned,
            })

    return {
        "repository": repo,
        "commit_hash": sha,
        "default_branch": default_branch,
        "status": "ok",
        "n_release_branches": len(release_branches),
        "V_fixed_idents": meta.get("V_fixed_idents", []),
        "target_files": paths_to_audit,
        "gap_branches": gap_branches,
        "already_fixed_branches": already_fixed_branches,
        "inapplicable_branches": inapplicable_branches,
    }


def run(
    out_path: Path | None = None,
    metas: Iterable[Path] | None = None,
    limit: int | None = None,
) -> Path:
    """Audit each clean-fix commit's release branches.

    Resume-safe: if `out_path` already exists, rows whose
    (repository, commit_hash) pair is already present are skipped. Lets a
    killed run (network blip, token rotation, etc.) pick up cleanly.
    """
    out_path = out_path or (GAPS_DIR / "gaps.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = GitHubClient(get_github_tokens())

    metas = list(metas if metas is not None else _clean_fix_meta_paths())
    if limit is not None:
        metas = metas[:limit]

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

    with out_path.open("a", encoding="utf-8") as fp:
        for meta_path in tqdm(metas, desc="audit"):
            meta = json.loads(meta_path.read_text())
            key = (meta["repository"], meta["commit_hash"])
            if key in done:
                continue
            rec = find_gap_for_commit(client, meta)
            fp.write(json.dumps(rec, ensure_ascii=False))
            fp.write("\n")
            fp.flush()
    return out_path


# --- summary --------------------------------------------------------------


def summarize(in_path: Path | None = None) -> None:
    in_path = in_path or (GAPS_DIR / "gaps.jsonl")
    statuses: Counter[str] = Counter()
    n_release_branches_total = 0
    n_gap_branches_total = 0
    n_already_fixed_total = 0
    n_inapplicable_total = 0
    commits_with_any_gap = 0
    gap_branches_per_commit: Counter[int] = Counter()
    by_ident: Counter[str] = Counter()

    with in_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            r = json.loads(line)
            statuses[r["status"]] += 1
            if r["status"] != "ok":
                continue
            n_release_branches_total += r["n_release_branches"]
            n_gap_branches_total += len(r["gap_branches"])
            n_already_fixed_total += len(r["already_fixed_branches"])
            n_inapplicable_total += len(r["inapplicable_branches"])
            gap_branches_per_commit[len(r["gap_branches"])] += 1
            if r["gap_branches"]:
                commits_with_any_gap += 1
                for gb in r["gap_branches"]:
                    for ident in gb["V_present_idents"]:
                        by_ident[ident] += 1

    print("=== per-commit status ===")
    for k, v in statuses.most_common():
        print(f"  {k:>20}  {v}")
    print()
    n_ok = statuses["ok"]
    print(f"audited commits (status=ok):        {n_ok}")
    print(f"commits with >=1 gap branch:        {commits_with_any_gap}"
          f"  ({100*commits_with_any_gap/max(n_ok,1):.1f}%)")
    print(f"total release branches inspected:   {n_release_branches_total}")
    print(f"  -> gap branches:                  {n_gap_branches_total}")
    print(f"  -> already-fixed branches:        {n_already_fixed_total}")
    print(f"  -> inapplicable (file absent):    {n_inapplicable_total}")
    print()
    print("=== gap-branches per commit ===")
    for k in sorted(gap_branches_per_commit):
        print(f"  {k:>3} gaps:  {gap_branches_per_commit[k]} commits")
    print()
    print("=== zizmor idents most often left unpatched on release branches ===")
    for ident, n in by_ident.most_common():
        print(f"  {n:>5}  {ident}")
