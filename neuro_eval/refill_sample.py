"""Replace the skipped_large cases in the LLM sample with same-class cases that
won't be skipped (target <= max-chars), keeping the sample at 12k with the same
four-class proportions and all entries actually LLM-processable.

new_sample = (old_sample - skipped_large keys) + per-class replacements drawn
from the symbolic ok pool (target_chars <= max-chars, not already in the sample).
"""
import argparse
import json
import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path


def key(r):
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="output/full/llm_sample.jsonl")
    ap.add_argument("--rows", default="output/full/llm_experiments/rows.jsonl",
                    help="a finished model's rows.jsonl (for the skipped_large set)")
    ap.add_argument("--sym", default="output/full/backport_run/symbolic_rows.jsonl")
    ap.add_argument("--max-chars", type=int, default=16000)
    ap.add_argument("--seed", type=int, default=23)
    args = ap.parse_args()

    sample = [json.loads(l) for l in open(args.sample) if l.strip()]
    sample_keys = {key(r) for r in sample}
    skipped = [json.loads(l) for l in open(args.rows)
               if l.strip() and json.loads(l).get("status") == "skipped_large"]
    skip_keys = {key(r) for r in skipped}
    need = Counter(r["klass"] for r in skipped)
    print("skipped_large to replace:", dict(need), "total", sum(need.values()))

    cand = defaultdict(list)
    for l in open(args.sym):
        if not l.strip():
            continue
        r = json.loads(l)
        if r.get("status") != "ok" or "klass" not in r:
            continue
        if r.get("target_chars", 10**9) > args.max_chars:
            continue
        if key(r) in sample_keys:
            continue
        cand[r["klass"]].append(r)

    rng = random.Random(args.seed)
    repl = []
    for kl, n in need.items():
        items = cand.get(kl, [])
        rng.shuffle(items)
        take = items[:n]
        if len(take) < n:
            print(f"  WARN {kl}: need {n} but only {len(take)} candidates available")
        repl += [{"repository": r["repository"], "commit_hash": r["commit_hash"],
                  "branch": r["branch"], "file": r["file"],
                  "idents": r["idents"], "klass": r["klass"]} for r in take]

    new = [r for r in sample if key(r) not in skip_keys] + repl
    rng.shuffle(new)
    shutil.copy(args.sample, args.sample + ".bak_pre_refill")
    with open(args.sample, "w", encoding="utf-8") as f:
        for r in new:
            f.write(json.dumps(r) + "\n")
    print(f"replacements drawn: {len(repl)}")
    print(f"new sample: {len(new)}  (backup at {args.sample}.bak_pre_refill)")
    print("new per-class:", dict(Counter(r['klass'] for r in new)))


if __name__ == "__main__":
    main()
