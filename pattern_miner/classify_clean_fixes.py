"""Run fix_classify over the existing clean_fixes/ tree.

Reads output/$DATASET_TAG/clean_fixes/index.jsonl (one row per commit) +
each commit's meta.json + its before/after YAML files, writes:

  output/$DATASET_TAG/clean_fixes/classification.jsonl   one row per commit
  output/$DATASET_TAG/clean_fixes/classification_summary.md   headline counts

The classification preserves the original 1,804 commits; downstream
gap/backport aggregation post-filters by joining on (repo, commit_hash).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .config import OUTPUT_DIR
from .fix_classify import classify_commit_meta


CLEAN_FIXES_DIR = OUTPUT_DIR / "clean_fixes"


def _read_text(meta: dict, side: str, file_record: dict) -> str:
    """Reader plugged into classify_commit_meta."""
    fname = file_record.get(side, "")
    if not fname:
        return ""
    p = CLEAN_FIXES_DIR / meta.get("_dir", "") / fname
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def run(limit: int | None = None) -> dict:
    index = CLEAN_FIXES_DIR / "index.jsonl"
    if not index.exists():
        print(f"ERR: {index} missing — run pattern_miner clean-fixes first.")
        return {}

    rows = []
    kind_counter: Counter[str] = Counter()
    n_processed = 0

    for line in index.open():
        idx = json.loads(line)
        commit_dir = idx["dir"]
        meta_path = CLEAN_FIXES_DIR / commit_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        meta["_dir"] = commit_dir
        verdict = classify_commit_meta(meta, _read_text)
        verdict["dir"] = commit_dir
        rows.append(verdict)
        kind_counter[verdict["kind"]] += 1
        n_processed += 1
        if limit is not None and n_processed >= limit:
            break
        if n_processed % 200 == 0:
            print(f"  classified {n_processed}/{1804}", flush=True)

    # Write per-commit classification
    out_path = CLEAN_FIXES_DIR / "classification.jsonl"
    with out_path.open("w") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows -> {out_path}")

    # Headline summary
    total = sum(kind_counter.values())
    summary = CLEAN_FIXES_DIR / "classification_summary.md"
    with summary.open("w") as fp:
        fp.write("| Kind | Count | Share |\n|---|---:|---:|\n")
        for k in ("structural", "mixed", "deletion"):
            c = kind_counter.get(k, 0)
            fp.write(f"| {k} | {c} | {c/total*100:.1f}% |\n")
        fp.write(f"| **Total** | **{total}** | 100% |\n")
    print(f"summary -> {summary}")

    print("\n=== distribution ===")
    for k in ("structural", "mixed", "deletion"):
        c = kind_counter.get(k, 0)
        print(f"  {k:12s}  {c:5d}  ({c/total*100:5.1f}%)")
    print(f"  {'total':12s}  {total:5d}")

    return {"rows": rows, "counts": dict(kind_counter)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    run(limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
