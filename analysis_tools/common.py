"""Shared helpers: dataset loaders, oracle wrappers, output formatting.

Centralises the loading of:
  - clean-fix metadata + before/after blobs (output/clean_fixes/)
  - gap-audit records (output/backport_gaps/gaps.jsonl)
  - history-classified records (output/backport_gaps/gaps_with_history.jsonl)

and a thin wrapper that runs the two external oracles (zizmor_local +
actionlint) on a (target_before, patched) pair. Re-used by every RQ5/6/7
script so the success criterion is identical across baselines.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from common.dataset import output_dir, reports_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = output_dir()
CLEAN_FIXES_DIR = OUTPUT_DIR / "clean_fixes"
GAPS_FILE = OUTPUT_DIR / "backport_gaps" / "gaps.jsonl"
HISTORY_FILE = OUTPUT_DIR / "backport_gaps" / "gaps_with_history.jsonl"

REPORTS_DIR = reports_dir()


# ---------- dataset loaders -------------------------------------------------


@dataclass
class CleanFix:
    """One master clean-fix commit's metadata + before/after blob texts."""

    repository: str
    commit_hash: str
    target_idents: list[str]
    files: list[dict]                       # each: {file_path, before_text, after_text, V_fixed}

    @property
    def key(self) -> tuple[str, str]:
        return (self.repository, self.commit_hash)


def iter_clean_fixes(limit: int | None = None) -> Iterator[CleanFix]:
    """Yield every clean-fix commit with its blob texts loaded."""
    n = 0
    for meta_path in sorted(CLEAN_FIXES_DIR.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        cdir = meta_path.parent
        files = []
        for f in meta.get("files", []):
            if not f.get("V_fixed"):
                continue
            try:
                before = (cdir / f["before"]).read_text(encoding="utf-8", errors="replace")
                after = (cdir / f["after"]).read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, KeyError):
                continue
            files.append({
                "file_path": f["file_path"],
                "before_text": before,
                "after_text": after,
                "V_fixed": f["V_fixed"],
            })
        if not files:
            continue
        yield CleanFix(
            repository=meta["repository"],
            commit_hash=meta["commit_hash"],
            target_idents=meta.get("V_fixed_idents") or [],
            files=files,
        )
        n += 1
        if limit is not None and n >= limit:
            return


def iter_gap_pairs() -> Iterator[dict]:
    """Yield one row per (commit, gap_branch, file) triple from gaps.jsonl."""
    if not GAPS_FILE.exists():
        raise FileNotFoundError(
            f"{GAPS_FILE} missing — run `python -m backport_gaps find-gaps` first."
        )
    for line in GAPS_FILE.open("r", encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("status") != "ok":
            continue
        for gb in rec.get("gap_branches", []):
            for f in gb.get("files", []):
                if f.get("status") != "ok" or not f.get("V_present_idents"):
                    continue
                yield {
                    "repository": rec["repository"],
                    "commit_hash": rec["commit_hash"],
                    "target_idents": rec.get("V_fixed_idents", []),
                    "branch": gb["branch"],
                    "branch_head_sha": gb.get("branch_head_sha", ""),
                    "file_path": f["file_path"],
                    "v_present_on_target": f["V_present_idents"],
                }


def iter_true_backports() -> Iterator[dict]:
    """Yield each (commit, release_branch) that classified as true_backport."""
    if not HISTORY_FILE.exists():
        raise FileNotFoundError(
            f"{HISTORY_FILE} missing — run `python -m backport_gaps classify-history` first."
        )
    for line in HISTORY_FILE.open("r", encoding="utf-8"):
        rec = json.loads(line)
        for afb in rec.get("already_fixed_branches", []):
            hist = afb.get("history", {})
            if hist.get("refined_status") != "true_backport":
                continue
            yield {
                "repository": rec["repository"],
                "commit_hash": rec["commit_hash"],
                "target_idents": rec.get("V_fixed_idents", []),
                "branch": afb["branch"],
                "branch_head_sha": afb.get("branch_head_sha", ""),
                "backport_commit_sha": hist.get("removal_commit_sha", ""),
                "lag_days": hist.get("lag_days"),
            }


# ---------- oracle wrapper --------------------------------------------------


@dataclass
class OracleVerdict:
    """Combined verdict from the two external oracles. Keep raw subfields so
    individual baselines can be debugged."""

    target_idents_relevant: list[str] = field(default_factory=list)
    zizmor_local_ok: bool = False
    actionlint_ok: bool = False
    zizmor_local_detail: dict[str, Any] = field(default_factory=dict)
    actionlint_detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def accepted(self) -> bool:
        """The paper-claim-correct verdict: both external oracles pass."""
        return self.zizmor_local_ok and self.actionlint_ok


def run_oracles(
    program,
    target_before_text: str,
    patched_text: str,
    apply_result,
) -> OracleVerdict:
    """Run zizmor_local + actionlint on (target_before, patched).

    `program` and `apply_result` come from backport_ir; zizmor_local needs
    apply_result to scope its locality check to the edits that actually
    landed.
    """
    from backport_ir.verify import actionlint_oracle, zizmor_oracle_local

    z = zizmor_oracle_local(program, target_before_text, patched_text, apply_result)
    a = actionlint_oracle(target_before_text, patched_text)
    if z.get("status") != "ok":
        return OracleVerdict(error=f"zizmor: {z.get('error', z.get('status'))}")
    if a.get("status") != "ok":
        return OracleVerdict(error=f"actionlint: {a.get('error', a.get('status'))}")
    return OracleVerdict(
        target_idents_relevant=z.get("relevant_targets", []) or [],
        zizmor_local_ok=bool(z.get("success")),
        actionlint_ok=bool(a.get("success")),
        zizmor_local_detail=z,
        actionlint_detail=a,
    )


# ---------- output formatting ----------------------------------------------


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False))
            fp.write("\n")


def write_table(path: Path, rows: list[tuple[str, int, str]]) -> None:
    """Write a small (label, count, pct) summary table — Markdown-flavoured."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(c for _, c, _ in rows if isinstance(c, int))
    lines = ["| Bucket | Count | Share |", "|---|---:|---:|"]
    for label, count, pct in rows:
        lines.append(f"| {label} | {count:,} | {pct} |")
    if total:
        lines.append(f"| **Total** | **{total:,}** | 100% |")
    path.write_text("\n".join(lines) + "\n")


def pct(n: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{100*n/total:.1f}%"


def bucket_counts(rows: Iterator[dict], key: str) -> list[tuple[str, int, str]]:
    counts = Counter(r.get(key, "unknown") for r in rows)
    total = sum(counts.values())
    return [(label, n, pct(n, total)) for label, n in counts.most_common()]
