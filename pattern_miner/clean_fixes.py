"""Identify clean-fix commits, dump them, and cluster them into patterns.

A clean-fix commit is one where the zizmor scanner reports a non-empty set of
findings disappearing (V_fixed) AND no new findings appearing (V_introduced).
This is the strictest, most defensible subset of "this commit removed
vulnerabilities" — no ambiguity from step-index drift or from new issues
inadvertently introduced alongside a fix.

Two outputs:
  1. clean_fixes/  — one directory per commit, with before/after workflow
                     blobs and a meta.json
  2. patterns.jsonl — pattern catalog, two-level clustering keyed by the SET
                      OF ZIZMOR RULES the commit removed (level 1) and by the
                      structural template of the diff (level 2)
"""
from __future__ import annotations

import json
from pathlib import Path

from .cluster import cluster_by_commit
from .config import BLOBS_DIR, OUTPUT_DIR
from .extract_diff import WorkflowDiff
from .scan import diff_findings, load_scans


# --- per-commit aggregation -------------------------------------------------


def _build_diff(rec: dict) -> WorkflowDiff:
    return WorkflowDiff(
        repository=rec["repository"],
        commit_hash=rec["commit_hash"],
        file_path=rec["file_path"],
        file_hash=rec["file_hash"],
        previous_file_hash=rec["previous_file_hash"],
        added=rec["added"],
        removed=rec["removed"],
        changed={k: tuple(v) for k, v in rec["changed"].items()},
        parse_error=False,
    )


def aggregate_commits(diffs_path: Path, scans: dict[str, list[dict]]) -> list[dict]:
    """Group `diffs.jsonl` rows by (repo, sha) and attach scanner V_fixed/V_introduced.

    Returns a list of per-commit dicts:
        {
            "repository": str,
            "commit_hash": str,
            "diffs": [WorkflowDiff, ...],     # one per modified workflow file
            "files": [<raw rec>, ...],        # raw diff records (kept for dump)
            "V_fixed":      {(ident, route), ...},
            "V_introduced": {(ident, route), ...},
        }
    """
    by_commit: dict[tuple[str, str], dict] = {}
    with diffs_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            r = json.loads(line)
            if r.get("parse_error"):
                continue
            key = (r["repository"], r["commit_hash"])
            agg = by_commit.setdefault(key, {
                "repository": r["repository"],
                "commit_hash": r["commit_hash"],
                "diffs": [],
                "files": [],
                "V_fixed": set(),
                "V_introduced": set(),
                "file_finding_details": [],
            })
            agg["diffs"].append(_build_diff(r))
            agg["files"].append(r)

            before = scans.get(r.get("previous_file_hash"))
            after = scans.get(r.get("file_hash"))
            if before is None or after is None:
                agg["file_finding_details"].append(
                    {"file_path": r["file_path"], "scan_status": "incomplete",
                     "V_fixed": [], "V_introduced": []}
                )
                continue
            fixed, introduced = diff_findings(before, after)
            for f in fixed:
                agg["V_fixed"].add((f["ident"], f["route"]))
            for f in introduced:
                agg["V_introduced"].add((f["ident"], f["route"]))
            agg["file_finding_details"].append(
                {"file_path": r["file_path"], "scan_status": "ok",
                 "V_fixed": fixed, "V_introduced": introduced}
            )
    return list(by_commit.values())


def filter_clean_fixes(commits: list[dict]) -> list[dict]:
    """Keep only commits with V_fixed != {} AND V_introduced == {}."""
    out = []
    for c in commits:
        if c["V_fixed"] and not c["V_introduced"]:
            c["V_fixed_idents"] = sorted({i for i, _ in c["V_fixed"]})
            out.append(c)
    return out


# --- dump --------------------------------------------------------------------


def _flatten_path(file_path: str) -> str:
    """`.github/workflows/build.yml` -> `.github__workflows__build`"""
    p = file_path.replace("/", "__")
    for ext in (".yml", ".yaml"):
        if p.endswith(ext):
            p = p[: -len(ext)]
    return p


def dump_clean_fixes(
    clean: list[dict],
    dest: Path,
    blobs_dir: Path = BLOBS_DIR,
) -> int:
    """Write each clean-fix commit to its own directory under `dest/`.

    Returns the number of commit directories written.
    """
    dest.mkdir(parents=True, exist_ok=True)
    n_blobs_missing = 0
    index_rows: list[dict] = []

    for c in clean:
        repo = c["repository"]
        sha = c["commit_hash"]
        cdir = dest / f"{repo.replace('/', '__')}__{sha[:10]}"
        cdir.mkdir(parents=True, exist_ok=True)

        per_file_meta: list[dict] = []
        used_names: dict[str, int] = {}
        for f, finding_rec in zip(c["files"], c["file_finding_details"]):
            base = _flatten_path(f["file_path"])
            n = used_names.get(base, 0)
            used_names[base] = n + 1
            flat = base if n == 0 else f"{base}.{n}"

            for src_hash, dst in (
                (f["previous_file_hash"], cdir / f"{flat}.before.yml"),
                (f["file_hash"], cdir / f"{flat}.after.yml"),
            ):
                src = blobs_dir / src_hash
                try:
                    dst.write_bytes(src.read_bytes())
                except FileNotFoundError:
                    n_blobs_missing += 1
                    dst.write_text(f"# blob {src_hash} not found in workflows/\n")

            per_file_meta.append({
                "file_path": f["file_path"],
                "before": f"{flat}.before.yml",
                "after": f"{flat}.after.yml",
                "scan_status": finding_rec["scan_status"],
                "V_fixed": [
                    {"ident": x["ident"], "route": x["route"], "severity": x["severity"]}
                    for x in finding_rec["V_fixed"]
                ],
                "V_introduced": [
                    {"ident": x["ident"], "route": x["route"], "severity": x["severity"]}
                    for x in finding_rec["V_introduced"]
                ],
            })

        meta = {
            "repository": repo,
            "commit_hash": sha,
            "github_url": f"https://github.com/{repo}/commit/{sha}",
            "V_fixed_count": len(c["V_fixed"]),
            "V_fixed_idents": c["V_fixed_idents"],
            "n_files_modified": len(c["files"]),
            "files": per_file_meta,
        }
        (cdir / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False)
        )

        index_rows.append({
            "dir": cdir.name,
            "repository": repo,
            "commit_hash": sha,
            "V_fixed_count": len(c["V_fixed"]),
            "V_fixed_idents": c["V_fixed_idents"],
            "n_files_modified": len(c["files"]),
        })

    # write index.jsonl, sorted by V_fixed_count desc
    index_rows.sort(key=lambda r: (-r["V_fixed_count"], r["repository"]))
    with (dest / "index.jsonl").open("w", encoding="utf-8") as fp:
        for r in index_rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")

    if n_blobs_missing:
        print(f"  warning: {n_blobs_missing} blobs missing on disk "
              f"(placeholder file written)")
    return len(clean)


# --- pattern catalog --------------------------------------------------------


def cluster_clean_fixes(
    clean: list[dict],
    out_path: Path,
    max_exemplars: int = 5,
) -> list[dict]:
    """Two-level cluster the clean-fix commits, keyed by V_fixed_idents.

    Writes patterns.jsonl, returns the list of patterns.
    """
    patterns = cluster_by_commit(
        clean,
        key_field="V_fixed_idents",
        key_name="fixes",
        max_exemplars=max_exemplars,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fp:
        for p in patterns:
            fp.write(json.dumps(p, default=str, ensure_ascii=False) + "\n")
    return patterns


# --- top-level entry points (used by cli.py) --------------------------------


def run_clean_fixes(
    diffs_path: Path | None = None,
    scans_path: Path | None = None,
    dest: Path | None = None,
) -> tuple[list[dict], int]:
    """Aggregate, filter, dump. Returns (clean_fix_commits, n_dirs_written)."""
    diffs_path = diffs_path or (OUTPUT_DIR / "diffs.jsonl")
    scans = load_scans(scans_path or (OUTPUT_DIR / "scans.jsonl"))
    dest = dest or (OUTPUT_DIR / "clean_fixes")

    commits = aggregate_commits(diffs_path, scans)
    clean = filter_clean_fixes(commits)
    n_dirs = dump_clean_fixes(clean, dest)
    return clean, n_dirs


def run_patterns(
    diffs_path: Path | None = None,
    scans_path: Path | None = None,
    out_path: Path | None = None,
    max_exemplars: int = 5,
) -> list[dict]:
    """Aggregate, filter, cluster. Returns the list of patterns."""
    diffs_path = diffs_path or (OUTPUT_DIR / "diffs.jsonl")
    scans = load_scans(scans_path or (OUTPUT_DIR / "scans.jsonl"))
    out_path = out_path or (OUTPUT_DIR / "patterns.jsonl")

    commits = aggregate_commits(diffs_path, scans)
    clean = filter_clean_fixes(commits)
    return cluster_clean_fixes(clean, out_path, max_exemplars=max_exemplars)
