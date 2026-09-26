"""Freeze a stratified sample of the symbolic results, sized PROPORTIONALLY to
the four-class distribution, for the LLM experiments (fallback + pure-LLM
baseline). Both read this one file (full_backport --llm-cases / baseline_llm
--cases) so they run on identical cases.

Proportional = each class's share of the sample matches its share of the ok
symbolic cases (vs the old equal per-class sampling). Largest-remainder rounding
hits the target size exactly.
"""
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="output/full/backport_run/symbolic_rows.jsonl")
    ap.add_argument("--size", type=int, default=12000)
    ap.add_argument("--out", default="output/full/llm_sample.jsonl")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.sym) if l.strip()]
    seen, uniq = set(), []
    for r in rows:
        if r.get("status") != "ok" or "klass" not in r:
            continue
        k = (r["repository"], r["commit_hash"], r["branch"], r["file"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    by = defaultdict(list)
    for r in uniq:
        by[r["klass"]].append(r)
    pool = len(uniq)

    # proportional allocation + largest-remainder to total exactly --size
    raw = {k: args.size * len(by[k]) / pool for k in CLASSES if by[k]}
    alloc = {k: int(v) for k, v in raw.items()}
    remainder = args.size - sum(alloc.values())
    frac_order = sorted(((raw[k] - alloc[k], k) for k in raw), reverse=True)
    for i in range(remainder):
        alloc[frac_order[i % len(frac_order)][1]] += 1

    rng = random.Random(args.seed)
    out = []
    for k in CLASSES:
        items = by.get(k, [])
        rng.shuffle(items)
        for r in items[:min(alloc.get(k, 0), len(items))]:
            out.append({"repository": r["repository"], "commit_hash": r["commit_hash"],
                        "branch": r["branch"], "file": r["file"],
                        "idents": r["idents"], "klass": r["klass"]})
    rng.shuffle(out)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    print(f"pool ok={pool}  target={args.size}  wrote={len(out)} -> {args.out}")
    for k in CLASSES:
        share = len(by.get(k, [])) / pool if pool else 0
        print(f"  {k:16} pool={len(by.get(k, [])):>7} ({share*100:4.1f}%)  "
              f"alloc={alloc.get(k, 0):>5}  taken={sum(1 for r in out if r['klass']==k):>5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
