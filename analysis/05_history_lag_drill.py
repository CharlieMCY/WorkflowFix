"""Drill into the history-classified backport events.

Reports:
  - refined buckets (true_backport / same_day_fix / independent_prior_fix /
    inconclusive / never_had_it / timed_out)
  - inconclusive by sub-reason
  - lag distribution (percentiles + bucketed) for TRUE backports
  - full list of TRUE backports with their lags (for case-study selection)
  - drill into the 1-3 month "cluster"

Inputs:  output/backport_gaps/gaps_with_history.jsonl
Outputs: stdout
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from common.dataset import output_dir

from backport_gaps.history import _inconclusive_subreason, _refine_backport_status

OUT = output_dir()
HIST = OUT / "backport_gaps" / "gaps_with_history.jsonl"


def main() -> None:
    refined: Counter[str] = Counter()
    incon_sub: Counter[str] = Counter()
    branches_audited = 0
    true_records: list[dict] = []
    sameday_count = 0
    indep_count = 0
    for line in HIST.open():
        rec = json.loads(line)
        for br in rec.get("already_fixed_branches", []):
            branches_audited += 1
            s = _refine_backport_status(br)
            refined[s] += 1
            if s == "inconclusive":
                incon_sub[_inconclusive_subreason(br)] += 1
            if s == "true_backport":
                true_records.append({
                    "repo": rec["repository"],
                    "master_sha": rec["commit_hash"][:10],
                    "branch": br["branch"],
                    "lag_days": br.get("lag_days"),
                    "V_fixed_idents": rec.get("V_fixed_idents", []),
                })

    print(f"=== refined status (n={branches_audited}) ===")
    order = ["true_backport", "same_day_fix", "independent_prior_fix",
             "inconclusive", "never_had_it", "timed_out"]
    other = [k for k in refined if k not in order]
    for k in order + other:
        if k in refined:
            n = refined[k]
            print(f"  {k:>22}  {n:>5}  ({100*n/max(branches_audited,1):.1f}%)")

    if incon_sub:
        print()
        print("inconclusive — by sub-reason:")
        for r, n in incon_sub.most_common():
            print(f"  {r:>42}  {n:>4}")

    # Lag distribution for true backports
    lags = sorted(t["lag_days"] for t in true_records if t["lag_days"] is not None)
    if lags:
        n = len(lags)
        def pct(p): return lags[min(int(p * n), n - 1)]
        print()
        print(f"=== TRUE backport lag (days), n={n} ===")
        print(f"  min:    {lags[0]:>10.2f}")
        print(f"  p25:    {pct(0.25):>10.2f}")
        print(f"  median: {pct(0.50):>10.2f}")
        print(f"  p75:    {pct(0.75):>10.2f}")
        print(f"  p90:    {pct(0.90):>10.2f}")
        print(f"  max:    {lags[-1]:>10.2f}")
        print(f"  mean:   {sum(lags)/n:>10.2f}")
        print()
        print("  bucketed:")
        buckets = [(1, 7, "1-7 days"), (7, 30, "1-4 weeks"),
                   (30, 90, "1-3 months"), (90, 365, "3-12 months"),
                   (365, 1e9, "> 1 year")]
        for lo, hi, name in buckets:
            c = sum(1 for x in lags if lo < x <= hi)
            print(f"    {name:>14}:  {c:>4}")

    # Full list of true backports
    print()
    print(f"=== all {len(true_records)} TRUE backports (sorted by lag) ===")
    true_records.sort(key=lambda t: t["lag_days"] or 0)
    for t in true_records:
        idents = ",".join(t["V_fixed_idents"])
        print(f"  lag={t['lag_days']:>6.1f}d  {t['repo']}@{t['master_sha']}  "
              f"branch={t['branch']:<30}  fixed={idents}")

    # 1-3 month cluster drill
    cluster = [t for t in true_records
               if t["lag_days"] is not None and 30 < t["lag_days"] <= 90]
    if cluster:
        print()
        print(f"=== 1-3 month cluster (n={len(cluster)}) ===")
        repo_counts: Counter[str] = Counter()
        for t in cluster:
            repo_counts[t["repo"]] += 1
        for repo, n in repo_counts.most_common():
            print(f"  {n:>3}× {repo}")


if __name__ == "__main__":
    main()
