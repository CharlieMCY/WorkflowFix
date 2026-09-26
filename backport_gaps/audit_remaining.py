"""Gap-audit every clean fix that does not yet have a gaps.jsonl row.

The streaming run (`backport_gaps stream-all`) gap-audits only the
host-preserving ("structural") clean fixes; the host-removed ones (where
the master commit deleted the vulnerable construct entirely) get a
clean_fixes/<dir>/meta.json but no gap audit. This tool closes that gap
so the cross-branch audit (RQ2-RQ4) covers ALL clean fixes under the
plain V_fixed!=0 & V_introduced=0 criterion.

For each clean fix in clean_fixes/index.jsonl not already present in
gaps.jsonl, it loads the on-disk meta.json, runs find_gap_for_commit,
appends to gaps.jsonl, runs classify_record, and appends to
gaps_with_history.jsonl. Resume-safe (skips any commit already in
gaps.jsonl) and concurrent (rotates the multi-token GitHub client).

It does NOT touch streaming.jsonl: the audited/host_removed outcome there
remains the *structural* label. After this run, gaps.jsonl simply covers
every clean fix; the structural distinction lives in
clean_fixes/classification.jsonl.
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.cache import jsonl_append
from common.dataset import output_dir

from .config import get_github_tokens
from .gaps import find_gap_for_commit
from .github import GitHubClient
from .history import classify_record as classify_history_record


def run(workers: int = 12, limit: int | None = None) -> None:
    out_root = output_dir()
    cf_index = out_root / "clean_fixes" / "index.jsonl"
    gaps_path = out_root / "backport_gaps" / "gaps.jsonl"
    history_path = out_root / "backport_gaps" / "gaps_with_history.jsonl"

    if not cf_index.exists():
        sys.exit(f"{cf_index} not found")

    cf: dict[tuple[str, str], str] = {}
    for line in cf_index.open():
        r = json.loads(line)
        cf[(r["repository"], r["commit_hash"])] = r["dir"]

    audited: set[tuple[str, str]] = set()
    if gaps_path.exists():
        for line in gaps_path.open():
            g = json.loads(line)
            audited.add((g["repository"], g["commit_hash"]))

    todo = [(k, d) for k, d in cf.items() if k not in audited]
    if limit is not None:
        todo = todo[:limit]
    print(f"clean fixes: {len(cf):,}; already audited: {len(audited):,}; "
          f"to audit: {len(todo):,}", flush=True)
    if not todo:
        print("nothing to audit.")
        return

    tokens = get_github_tokens()
    client = GitHubClient(tokens)
    print(f"GitHub client: {len(tokens)} token(s)", flush=True)

    gaps_lock = threading.Lock()
    hist_lock = threading.Lock()
    res_lock = threading.Lock()
    master_date_cache: dict = {}
    counter = {"ok": 0, "non_ok": 0, "no_meta": 0, "error": 0, "done": 0}
    import time
    t0 = time.time()

    def _audit(item) -> None:
        (repo, sha), d = item
        meta_path = out_root / "clean_fixes" / d / "meta.json"
        if not meta_path.exists():
            with res_lock:
                counter["no_meta"] += 1; counter["done"] += 1
            return
        meta = json.loads(meta_path.read_text())
        try:
            audit = find_gap_for_commit(client, meta)
        except Exception as e:
            with res_lock:
                counter["error"] += 1; counter["done"] += 1
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
        with res_lock:
            if audit.get("status") == "ok":
                counter["ok"] += 1
            else:
                counter["non_ok"] += 1
            counter["done"] += 1
            if counter["done"] % 200 == 0:
                el = time.time() - t0
                rate = counter["done"] / el if el else 0
                rem = (len(todo) - counter["done"]) / rate / 3600 if rate else 0
                print(f"  {counter['done']:,}/{len(todo):,}  "
                      f"rate {rate*60:.0f}/min  ETA {rem:.1f}h  "
                      f"(ok={counter['ok']:,} non_ok={counter['non_ok']:,} "
                      f"err={counter['error']})", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_audit, todo))

    print(f"\ndone: ok={counter['ok']:,} non_ok={counter['non_ok']:,} "
          f"no_meta={counter['no_meta']} error={counter['error']}", flush=True)
    print(f"gaps.jsonl now: {sum(1 for _ in gaps_path.open()):,} rows", flush=True)


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)
    run(workers=args.workers, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
