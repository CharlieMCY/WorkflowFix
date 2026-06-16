"""Demo of the transplantability scan on a GitHub-fetched sample (this checkout
has no local clean_fixes/). Samples clean-fix commits from gaps.jsonl, fetches
each master (parent -> commit) workflow diff, compiles, and classifies surgical /
restructure / no_security_edit. Needs only master before/after — no target
branch, no resolver — so it is cheap (1 GitHub API call per commit).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from demo_backport import raw_file, parent_sha
from backport_ir.compile import compile_program, surgical_class, surgical_review_reasons


def sample(n: int) -> list[dict]:
    rows = [json.loads(l) for l in
            open("output/50k/backport_gaps/gaps.jsonl").read().splitlines() if l.strip()]
    by_combo: dict[tuple, list] = defaultdict(list)
    seen = set()
    for r in rows:
        if r.get("status") != "ok" or not r.get("target_files"):
            continue
        key = (r["repository"], r["commit_hash"])
        if key in seen:
            continue
        seen.add(key)
        by_combo[tuple(sorted(r["V_fixed_idents"]))].append(r)
    combos = sorted(by_combo, key=lambda c: -len(by_combo[c]))
    out, i = [], 0
    while len(out) < n and any(by_combo.values()):
        c = combos[i % len(combos)]
        if by_combo[c]:
            out.append(by_combo[c].pop(0))
        i += 1
    return out[:n]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    rows = []
    cands = sample(n)
    for i, r in enumerate(cands, 1):
        repo, sha, f = r["repository"], r["commit_hash"], r["target_files"][0]
        idents = r["V_fixed_idents"]
        after = raw_file(repo, sha, f)
        par = parent_sha(repo, sha)
        before = raw_file(repo, par, f) if par else None
        if not after or not before:
            print(f"[{i}/{len(cands)}] fetch_fail {repo}", file=sys.stderr)
            continue
        prog = compile_program(repo, sha, f, before, after, idents)
        cls = surgical_class(prog)
        rr = surgical_review_reasons(prog) if cls == "restructure" else []
        rows.append((f"{repo}@{sha[:8]}", idents, cls, rr))
        print(f"[{i}/{len(cands)}] {cls:16} {repo} ({'+'.join(idents)})", file=sys.stderr)
    print(file=sys.stderr)
    _report(rows)
    return 0


def _report(rows) -> None:
    from collections import Counter
    overall: Counter = Counter()
    per_ident: dict = defaultdict(Counter)
    reasons: Counter = Counter()
    for _k, idents, cls, rr in rows:
        overall[cls] += 1
        for it in idents:
            per_ident[it][cls] += 1
        if cls == "restructure":
            for r in rr:
                reasons[r] += 1
    n = sum(overall.values()) or 1
    transplantable = overall["surgical"] + overall["partial"]
    print(f"=== transplantability over {n} clean-fixes ===")
    for k in ("surgical", "partial", "restructure", "no_security_edit"):
        print(f"  {k:18} {overall[k]:4}  {100 * overall[k] / n:5.1f}%")
    print(f"  {'-> transplantable':18} {transplantable:4}  {100 * transplantable / n:5.1f}%"
          "  (surgical + partial)")
    print("\n=== per zizmor rule ===")
    for ident, c in sorted(per_ident.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(c.values()) or 1
        tp = c["surgical"] + c["partial"]
        print(f"  {ident:24} n={tot:3}  transplantable={tp:3} ({100 * tp // tot:3d}%)  "
              f"[surg={c['surgical']:3} part={c['partial']:3} restr={c['restructure']:3} "
              f"nosec={c['no_security_edit']:3}]")
    if reasons:
        print("\n=== restructure blocking reasons ===")
        for r, k in reasons.most_common():
            print(f"  {k:3}  {r[:78]}")


if __name__ == "__main__":
    raise SystemExit(main())
