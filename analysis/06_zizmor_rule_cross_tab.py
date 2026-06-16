"""Cross-tabulate zizmor rules against backport status and gap presence.

For each zizmor rule the master commit fixed, how often does it:
  - have a TRUE backport on a release branch?
  - appear as same-day fix (suspicious — likely merge sync)?
  - appear as independent prior fix on the release branch?
  - remain present (gap) on at least one release branch?

Also reports per-rule frequencies across the 364 clean-fixes and the rule
co-occurrence matrix (top pairs).

Inputs:  output/clean_fixes/*/meta.json,
         output/backport_gaps/gaps.jsonl,
         output/backport_gaps/gaps_with_history.jsonl
Outputs: stdout
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from common.dataset import output_dir

from backport_gaps.history import _refine_backport_status

OUT = output_dir()
CLEAN_FIXES = OUT / "clean_fixes"
GAPS = OUT / "backport_gaps" / "gaps.jsonl"
HIST = OUT / "backport_gaps" / "gaps_with_history.jsonl"


def main() -> None:
    # 1. per-rule commit counts + co-occurrence (from clean_fixes metadata)
    rule_commit_count: Counter[str] = Counter()
    cooccur: Counter[tuple[str, str]] = Counter()
    n_commits = 0
    for meta_path in sorted(CLEAN_FIXES.glob("*/meta.json")):
        m = json.loads(meta_path.read_text())
        n_commits += 1
        idents = sorted(m["V_fixed_idents"])
        for r in idents:
            rule_commit_count[r] += 1
        for i, a in enumerate(idents):
            for b in idents[i + 1:]:
                cooccur[(a, b)] += 1

    print(f"=== per-rule commit count (across {n_commits} clean-fix commits) ===")
    print(f'{"rule":>26}  {"#commits":>9}  {"% of commits":>13}')
    for r, n in rule_commit_count.most_common():
        print(f"  {r:>24}  {n:>9}  {100*n/max(n_commits,1):>12.1f}%")

    print()
    print("=== top rule co-occurrence (commits where both appeared) ===")
    print(f'{"rule_a":>26}  ×  {"rule_b":<26}  {"#both":>6}')
    for (a, b), n in sorted(cooccur.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {a:>24}  ×  {b:<24}  {n:>6}")

    # 2. cross-tab against backport status (from gaps_with_history)
    per_ident: dict[str, Counter[str]] = {
        s: Counter()
        for s in ("true_backport", "same_day_fix", "independent_prior_fix",
                  "inconclusive", "never_had_it", "timed_out")
    }
    for line in HIST.open():
        rec = json.loads(line)
        for br in rec.get("already_fixed_branches", []):
            s = _refine_backport_status(br)
            if s in per_ident:
                for ident in rec.get("V_fixed_idents", []):
                    per_ident[s][ident] += 1

    print()
    print("=== rule × backport status (each row = #already_fixed branches per rule) ===")
    print("    rightmost two columns: row total, then % that are TRUE backport")
    all_rules = sorted(rule_commit_count.keys())
    headers = ("true", "sameday", "indep", "inconcl", "never", "timeout")
    keys = ("true_backport", "same_day_fix", "independent_prior_fix",
            "inconclusive", "never_had_it", "timed_out")
    print(f'  {"ident":>26}  ' + "  ".join(f"{h:>8}" for h in headers)
          + f"  {'total':>6}  {'true%':>6}")
    for r in all_rules:
        vals = [per_ident[k][r] for k in keys]
        total = sum(vals)
        true_pct = (vals[0] / total * 100) if total else 0
        print(f"  {r:>26}  " + "  ".join(f"{v:>8}" for v in vals)
              + f"  {total:>6}  {true_pct:>5.1f}%")

    # 3. cross-tab against gap presence (from gaps.jsonl)
    gap_per_ident: Counter[str] = Counter()
    af_per_ident: Counter[str] = Counter()
    for line in GAPS.open():
        rec = json.loads(line)
        if rec.get("status") != "ok":
            continue
        for gb in rec["gap_branches"]:
            for ident in gb["V_present_idents"]:
                gap_per_ident[ident] += 1
        for ident in rec.get("V_fixed_idents", []):
            # count per already_fixed branch
            af_per_ident[ident] += len(rec.get("already_fixed_branches", []))

    print()
    print("=== rule × gap (release HEAD still vulnerable) ===")
    print(f'  {"ident":>26}  {"#gap":>6}  {"#af":>6}  '
          f'{"gap_rate":>9}')
    for r in all_rules:
        g = gap_per_ident[r]
        a = af_per_ident[r]
        total = g + a
        rate = g / total if total else 0
        print(f"  {r:>26}  {g:>6}  {a:>6}  {rate:>9.1%}")


if __name__ == "__main__":
    main()
