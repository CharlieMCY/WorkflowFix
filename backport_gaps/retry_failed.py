"""Recover commits left as `audit_failed` by a streaming run
(`backport_gaps stream-all`) and reconcile all three artifacts.

`audit_failed` means the clean-fix was identified and its
clean_fixes/<dir>/meta.json + diffs rows were written, but
`find_gap_for_commit` raised (typically a transient `GitHubError:
... exhausted retries`). The audit step is AFTER the clean_fixes/diffs
writes, so those artifacts are already correct — only the gaps.jsonl +
gaps_with_history.jsonl rows are missing and the streaming.jsonl row is
stuck at `audit_failed`.

This tool reconciles all three artifacts and is fully idempotent:

  For each audit_failed (repo, sha) in streaming.jsonl:
    - if not yet in gaps.jsonl  -> re-audit from on-disk meta.json,
      append gaps + history.
    - if already in gaps.jsonl (status=ok) but missing from history
      -> backfill the history row.
    - in all recovered cases -> flip the streaming.jsonl row to
      `audited`.

Re-running after a partial/interrupted run finishes the reconciliation
without re-doing completed audits. streaming.jsonl is rewritten
atomically at the end.
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from common.cache import jsonl_append
from common.dataset import output_dir

from .config import get_github_tokens
from .gaps import find_gap_for_commit
from .github import GitHubClient
from .history import classify_record as classify_history_record


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cf_dir(out_root: Path, repo: str, sha: str) -> Path:
    return out_root / "clean_fixes" / f"{repo.replace('/', '__')}__{sha[:10]}"


def run(workers: int = 8) -> None:
    out_root = output_dir()
    streaming_path = out_root / "streaming.jsonl"
    gaps_path = out_root / "backport_gaps" / "gaps.jsonl"
    history_path = out_root / "backport_gaps" / "gaps_with_history.jsonl"

    if not streaming_path.exists():
        sys.exit(f"{streaming_path} not found")

    # ---- index existing artifacts -------------------------------------
    failed: list[tuple[str, str]] = []
    for line in streaming_path.open():
        r = json.loads(line)
        if r.get("outcome") == "audit_failed":
            failed.append((r["repository"], r["commit_hash"]))

    gaps_by_key: dict[tuple[str, str], dict] = {}
    if gaps_path.exists():
        for line in gaps_path.open():
            g = json.loads(line)
            gaps_by_key[(g["repository"], g["commit_hash"])] = g

    history_keys: set[tuple[str, str]] = set()
    if history_path.exists():
        for line in history_path.open():
            h = json.loads(line)
            history_keys.add((h["repository"], h["commit_hash"]))

    # Classify each audit_failed into the work it still needs.
    need_audit = [k for k in failed if k not in gaps_by_key]
    need_history = [k for k in failed
                    if k in gaps_by_key
                    and gaps_by_key[k].get("status") == "ok"
                    and k not in history_keys]
    recovered_keys = {k for k in failed
                      if k in gaps_by_key and gaps_by_key[k].get("status") == "ok"}

    print(f"audit_failed: {len(failed)}")
    print(f"  need fresh audit:     {len(need_audit)}")
    print(f"  need history backfill:{len(need_history)}")
    print(f"  already recovered:    {len(recovered_keys)}")

    tokens = get_github_tokens()
    client = GitHubClient(tokens)
    print(f"GitHub client: {len(tokens)} token(s)")

    gaps_lock = threading.Lock()
    hist_lock = threading.Lock()
    result_lock = threading.Lock()
    master_date_cache: dict = {}
    counter = {"audited": 0, "audit_failed": 0, "no_meta": 0,
               "history_backfilled": 0, "done": 0}

    # ---- fresh audits --------------------------------------------------
    def _audit(key: tuple[str, str]) -> None:
        repo, sha = key
        meta_path = _cf_dir(out_root, repo, sha) / "meta.json"
        if not meta_path.exists():
            with result_lock:
                counter["no_meta"] += 1; counter["done"] += 1
            return
        meta = json.loads(meta_path.read_text())
        try:
            audit = find_gap_for_commit(client, meta)
        except Exception:
            with result_lock:
                counter["audit_failed"] += 1; counter["done"] += 1
            return
        with gaps_lock:
            jsonl_append(gaps_path, audit)
        try:
            augmented = classify_history_record(
                client, dict(audit), master_date_cache=master_date_cache,
                max_workers=4)
        except Exception as e:
            augmented = {**audit, "history_error": f"{type(e).__name__}: {e}"}
        with hist_lock:
            jsonl_append(history_path, augmented)
        with result_lock:
            recovered_keys.add(key)
            counter["audited"] += 1; counter["done"] += 1

    # ---- history backfill (gaps already present) ----------------------
    def _backfill_history(key: tuple[str, str]) -> None:
        audit = gaps_by_key[key]
        try:
            augmented = classify_history_record(
                client, dict(audit), master_date_cache=master_date_cache,
                max_workers=4)
        except Exception as e:
            augmented = {**audit, "history_error": f"{type(e).__name__}: {e}"}
        with hist_lock:
            jsonl_append(history_path, augmented)
        with result_lock:
            counter["history_backfilled"] += 1

    if need_audit:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_audit, need_audit))
    if need_history:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_backfill_history, need_history))

    print(f"\nfresh audits: audited={counter['audited']} "
          f"still_failed={counter['audit_failed']} no_meta={counter['no_meta']}")
    print(f"history backfilled: {counter['history_backfilled']}")

    # ---- flip streaming.jsonl for every recovered key -----------------
    if recovered_keys:
        # build the audited replacement rows from gaps status
        tmp = streaming_path.with_suffix(".jsonl.tmp")
        n_flipped = 0
        with streaming_path.open() as fin, tmp.open("w", encoding="utf-8") as fout:
            for line in fin:
                r = json.loads(line)
                key = (r.get("repository"), r.get("commit_hash"))
                if r.get("outcome") == "audit_failed" and key in recovered_keys:
                    g = gaps_by_key.get(key)
                    status = g.get("status", "ok") if g else "ok"
                    new_row = {
                        "repository": key[0], "commit_hash": key[1],
                        "processed_at": _iso_now(), "source": "retry_failed",
                        "outcome": "audited",
                        "v_fixed_idents": r.get("v_fixed_idents",
                                                (g or {}).get("V_fixed_idents", [])),
                        "gap_audit_status": status,
                    }
                    fout.write(json.dumps(new_row, ensure_ascii=False) + "\n")
                    n_flipped += 1
                else:
                    fout.write(line)
        tmp.replace(streaming_path)
        print(f"streaming.jsonl: flipped {n_flipped} audit_failed -> audited")
    else:
        print("streaming.jsonl: nothing to flip")


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    run(workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
