"""Symbolic-only baseline on a stratified sample of the RQ5 gap set.

Stratifies the 8,734 (fix x branch x file) cases by their target-ident tuple so
rare-but-important groups (permissions, template-injection, multi-ident) are
represented, not just the unpinned-uses majority. For each sampled case it
fetches master before/after + the target file, compiles the target-independent
IRProgram, applies it symbolically, and runs the four acceptance oracles.

Writes one JSONL row per case (resumable) and prints a class x accepted crosstab
plus a failure taxonomy. This is the ground truth the LLM loop must improve on.
"""
import argparse
import json
import random
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.neuro_backport import (  # noqa: E402
    compile_case, evaluate_symbolic, fetch_case, iter_gap_cases, make_client,
    oracle_summary,
)
from backport_ir.pipeline import make_github_resolver  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402

GAPS = Path("output/50k/backport_gaps/gaps_with_history.jsonl")
OUT = Path("neuro_eval/baseline_rows.jsonl")
_tls = threading.local()


def client():
    if not hasattr(_tls, "client"):
        _tls.client = make_client()
        _tls.resolver = make_github_resolver(_tls.client)
    return _tls.client, _tls.resolver


def stratified_sample(per_group: int, seed: int = 7):
    cases = list(iter_gap_cases(GAPS))
    # dedup identical (repo, sha, branch, file)
    seen = set()
    uniq = []
    for c in cases:
        k = (c[0], c[1], c[2], c[3])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    groups = defaultdict(list)
    for c in uniq:
        groups[tuple(c[4])].append(c)
    rng = random.Random(seed)
    sample = []
    for g, items in groups.items():
        rng.shuffle(items)
        sample.extend(items[:per_group])
    rng.shuffle(sample)
    return sample


def process(case_tuple):
    repo, sha, branch, path, idents = case_tuple
    cl, resolver = client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, idents)
    except Exception as e:
        return {"repository": repo, "commit_hash": sha, "branch": branch,
                "file": path, "idents": idents, "status": "fetch_exc",
                "error": str(e)[:200]}
    if c.fetch_error:
        return {"repository": repo, "commit_hash": sha, "branch": branch,
                "file": path, "idents": idents, "status": c.fetch_error}
    try:
        prog = compile_case(c)
        ev = evaluate_symbolic(prog, c.target_text, resolver)
    except Exception as e:
        return {"repository": repo, "commit_hash": sha, "branch": branch,
                "file": path, "idents": idents, "status": "eval_exc",
                "error": str(e)[:200]}
    return {
        "repository": repo, "commit_hash": sha, "branch": branch,
        "file": path, "idents": idents, "status": "ok",
        "klass": ev["klass"],
        "review_reasons": ev["review_reasons"],
        "apply_summary": ev["apply_summary"],
        "n_edits": len(prog.edits),
        "n_auto": sum(1 for e in prog.edits if not e.review),
        "symbolic_accepted": ev["accepted"],
        "oracles": oracle_summary(ev["oracles"]),
        "zizmor_local_reason": ev["oracles"]["zizmor_local"].get("reason", ""),
    }


def all_cases():
    """Every unique (repo, sha, branch, file) gap case — the full RQ5 set."""
    seen, uniq = set(), []
    for c in iter_gap_cases(GAPS):
        k = (c[0], c[1], c[2], c[3])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-group", type=int, default=12)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="run the full 8,734-case RQ5 set")
    ap.add_argument("--out", default="", help="output rows path (default baseline_rows.jsonl)")
    args = ap.parse_args()

    if args.out:
        OUT = Path(args.out)
    sample = all_cases() if args.all else stratified_sample(args.per_group)
    if args.limit:
        sample = sample[:args.limit]
    done = jsonl_already_done(
        OUT, lambda r: (r["repository"], r["commit_hash"], r["branch"], r["file"]))
    todo = [c for c in sample
            if (c[0], c[1], c[2], c[3]) not in done]
    print(f"sample={len(sample)} done={len(done)} todo={len(todo)}", flush=True)

    n = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, c): c for c in todo}
        for fut in as_completed(futs):
            row = fut.result()
            jsonl_append(OUT, row)
            n += 1
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}", flush=True)

    # report
    rows = [json.loads(l) for l in OUT.read_text().splitlines()]
    ok = [r for r in rows if r["status"] == "ok"]
    print(f"\n=== {len(rows)} rows, {len(ok)} evaluated ===")
    print("statuses:", Counter(r["status"] for r in rows).most_common())
    crosstab = defaultdict(lambda: [0, 0])
    for r in ok:
        cell = crosstab[r["klass"]]
        cell[0] += 1
        cell[1] += int(r["symbolic_accepted"])
    print("\nclass            n   accepted   rate")
    for k in ("surgical", "partial", "restructure", "no_security_edit"):
        if k in crosstab:
            n_, a_ = crosstab[k]
            print(f"  {k:15s} {n_:4d}  {a_:4d}   {a_/n_*100:5.1f}%")
    # failure taxonomy among not-accepted ok rows
    fails = [r for r in ok if not r["symbolic_accepted"]]
    print(f"\nnot-accepted: {len(fails)}")
    why = Counter()
    for r in fails:
        o = r["oracles"]
        landed = r["apply_summary"].get("by_status", {})
        no_land = not any(landed.get(s) for s in ("applied", "created"))
        if no_land and not o.get("zizmor_global"):
            why["no_landed_edits (anchor/insert/all-review failed)"] += 1
        elif not o.get("zizmor_global"):
            why["zizmor_global fail (finding remained/introduced)"] += 1
        elif not o["actionlint"]:
            why["actionlint fail (broke workflow)"] += 1
        elif not o["permissions"]:
            why["permissions fail (collateral)"] += 1
        elif not o["minimality"]:
            why["minimality fail (non-security change)"] += 1
        else:
            why["other"] += 1
    for k, v in why.most_common():
        print(f"  {v:4d}  {k}")


if __name__ == "__main__":
    main()
