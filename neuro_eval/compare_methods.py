"""Five-method comparison on the SAME cases (the LLM 12k sample).

copy-paste / dependabot ran on the full gap set; the LLM experiments ran on the
proportional 12k sample. Same (repo,commit,branch,file) key, so we join: restrict
the baselines to the sample, intersect with the cases the LLM run has finished,
and print one four-class table across all five methods on identical cases.

Accept criteria (as defined per method elsewhere):
  symbolic / combined : zizmor_local+actionlint+permissions+minimality (IR-local)
  pure_LLM / copy_paste / dependabot : route-level (>=1 finding removed, none
                                       introduced) + actionlint  (IR-free)
"""
import argparse
import json
from collections import defaultdict

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
# pureLLM_valid folds the SHA check INTO acceptance: a fabricated SHA passes
# zizmor (it only checks 40-hex shape) but breaks at runtime, so it must not count.
COLS = ("symbolic", "combined", "pureLLM_z", "pureLLM_valid", "copy_paste", "dependabot")


def load(p):
    return [json.loads(l) for l in open(p) if l.strip()]


def key(r):
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


def pct(a, b):
    return f"{100*a/b:.1f}%" if b else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="output/full/llm_sample.jsonl")
    ap.add_argument("--baselines", default="output/full/baselines/baseline_rows.jsonl")
    ap.add_argument("--llm", default="output/full/llm_experiments/rows.jsonl")
    args = ap.parse_args()

    sample = {key(r): r["klass"] for r in load(args.sample)}
    bl = {key(r): r for r in load(args.baselines)
          if r.get("status") == "ok" and key(r) in sample}
    lm = {key(r): r for r in load(args.llm)
          if r.get("status") == "ok" and key(r) in sample}
    inter = set(bl) & set(lm)

    print(f"sample={len(sample)}  cp/dep ok in-sample={len(bl)}  "
          f"LLM done ok={len(lm)}  five-way intersection={len(inter)}\n")

    def _bl_valid(k):     # route-level AND no fabricated SHA (actually runs)
        r = lm[k]
        return bool(r.get("bl_accepted")) and r.get("bl_fab", 0) == 0

    def _bl_faithful(k):  # also no wrong-version pin (same bar as WORKFLOWBP pin())
        r = lm[k]
        return _bl_valid(k) and r.get("bl_wrong", 0) == 0

    getters = {
        "symbolic": lambda k: lm[k].get("symbolic_accepted"),
        "combined": lambda k: lm[k].get("combined_accepted"),
        "pureLLM_z": lambda k: lm[k].get("bl_accepted"),   # zizmor-lenient (old)
        "pureLLM_valid": _bl_valid,
        "copy_paste": lambda k: bl[k].get("cp_accepted"),
        "dependabot": lambda k: bl[k].get("dep_accepted"),
    }
    per = defaultdict(lambda: defaultdict(int))
    cnt = defaultdict(int)
    for k in inter:
        kl = sample[k]
        cnt[kl] += 1
        for c in COLS:
            per[kl][c] += int(bool(getters[c](k)))

    hdr = f"{'class':16}{'n':>6}" + "".join(f"{c:>12}" for c in COLS)
    print("== five-way, identical cases (intersection) ==")
    print(hdr)
    tot = defaultdict(int)
    tn = 0
    for kl in CLASSES:
        n = cnt[kl]
        if not n:
            continue
        tn += n
        line = f"{kl:16}{n:>6}"
        for c in COLS:
            tot[c] += per[kl][c]
            line += f"{pct(per[kl][c], n):>12}"
        print(line)
    line = f"{'TOTAL':16}{tn:>6}"
    for c in COLS:
        line += f"{pct(tot[c], tn):>12}"
    print(line)

    # copy-paste / dependabot over the FULL 12k sample (they're complete), for
    # reference even where the LLM run hasn't reached yet.
    cnt2 = defaultdict(int)
    cp2 = defaultdict(int)
    dep2 = defaultdict(int)
    for k, kl in sample.items():
        if k in bl:
            cnt2[kl] += 1
            cp2[kl] += int(bool(bl[k].get("cp_accepted")))
            dep2[kl] += int(bool(bl[k].get("dep_accepted")))
    print("\n== copy_paste / dependabot over the full 12k sample (complete) ==")
    print(f"{'class':16}{'n':>7}{'copy_paste':>12}{'dependabot':>12}")
    tn2 = tcp = tdep = 0
    for kl in CLASSES:
        n = cnt2[kl]
        if not n:
            continue
        tn2 += n
        tcp += cp2[kl]
        tdep += dep2[kl]
        print(f"{kl:16}{n:>7}{pct(cp2[kl], n):>12}{pct(dep2[kl], n):>12}")
    print(f"{'TOTAL':16}{tn2:>7}{pct(tcp, tn2):>12}{pct(tdep, tn2):>12}")

    # pure-LLM acceptance under three honesty bars (over the five-way intersection)
    n = len(inter)
    route = sum(1 for k in inter if lm[k].get("bl_accepted"))
    valid = sum(1 for k in inter if _bl_valid(k))
    faithful = sum(1 for k in inter if _bl_faithful(k))
    print(f"\n== pure-LLM acceptance under 3 honesty bars (n={n}) ==")
    print(f"  zizmor-lenient (any 40-hex SHA passes): {route:>5} ({pct(route, n)})")
    print(f"  valid (no fabricated SHA -> actually runs): {valid:>5} ({pct(valid, n)})")
    print(f"  faithful (every pin = target's own ref): {faithful:>5} ({pct(faithful, n)})")
    print("  WORKFLOWBP combined is faithful by construction; cp/dependabot copy "
          "real refs (never fabricate) so their route-level already == valid.")


if __name__ == "__main__":
    main()
