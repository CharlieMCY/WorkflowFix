"""Screenshot-comparable RQ5 table: symbolic-only vs neuro-symbolic (CEGIS).

Acceptance matches the original RQ5 slide exactly:
    accepted = zizmor_local AND actionlint   (on the still-vulnerable branch)

Per surgical class (surgical / partial / restructure / no_security_edit):
    symbolic    = baseline row accepted
    neuro-sym   = symbolic accepted OR (CEGIS recovered it on failure)

Usage:
    .venv/bin/python neuro_eval/combined.py \
        --base neuro_eval/baseline_full_rows.jsonl \
        --llm  neuro_eval/llm_rows.jsonl
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def accepted_rq5(row: dict) -> bool:
    """The slide's definition: zizmor_local AND actionlint."""
    o = row.get("oracles", {}) or {}
    return bool(o.get("zizmor_local")) and bool(o.get("actionlint"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="neuro_eval/baseline_rows.jsonl")
    ap.add_argument("--llm", default="neuro_eval/llm_rows.jsonl")
    args = ap.parse_args()

    base = [json.loads(l) for l in Path(args.base).read_text().splitlines()]
    base_ok = [r for r in base if r["status"] == "ok"]
    llm = []
    if Path(args.llm).exists():
        llm = [json.loads(l) for l in Path(args.llm).read_text().splitlines()]
    llm_ok = {(r["repository"], r["commit_hash"], r["branch"], r["file"]): r
              for r in llm if r["status"] == "ok"}

    # coverage of CEGIS over the symbolic failures
    fails = [r for r in base_ok if not accepted_rq5(r)]
    attempted = sum(1 for r in fails
                    if (r["repository"], r["commit_hash"], r["branch"], r["file"]) in llm_ok)

    cls = defaultdict(lambda: {"n": 0, "sym": 0, "neuro": 0})
    for r in base_ok:
        c = cls[r["klass"]]
        c["n"] += 1
        s = accepted_rq5(r)
        c["sym"] += int(s)
        if s:
            c["neuro"] += 1
        else:
            k = (r["repository"], r["commit_hash"], r["branch"], r["file"])
            lr = llm_ok.get(k)
            c["neuro"] += int(bool(lr and lr.get("llm_accepted")))

    print(f"dataset rows: base_evaluated={len(base_ok)}  "
          f"symbolic_failures={len(fails)}  CEGIS_attempted={attempted}"
          + ("" if attempted == len(fails)
             else f"  (coverage {attempted}/{len(fails)} = {attempted/max(len(fails),1)*100:.0f}%)"))
    print("\nacceptance = zizmor_local AND actionlint (matches the RQ5 slide)\n")
    print(f"{'class':16} {'n':>5} {'symbolic':>10} {'neuro-sym':>11}  lift")
    tot = {"n": 0, "sym": 0, "neuro": 0}
    for k in ("surgical", "partial", "restructure", "no_security_edit"):
        if k not in cls:
            continue
        c = cls[k]
        for f in tot:
            tot[f] += c[f]
        print(f"{k:16} {c['n']:>5} {c['sym']:>4} {c['sym']/c['n']*100:>4.0f}% "
              f"{c['neuro']:>4} {c['neuro']/c['n']*100:>4.0f}%  "
              f"+{(c['neuro']-c['sym'])/c['n']*100:.0f}pp")
    if tot["n"]:
        print(f"{'TOTAL':16} {tot['n']:>5} {tot['sym']:>4} {tot['sym']/tot['n']*100:>4.0f}% "
              f"{tot['neuro']:>4} {tot['neuro']/tot['n']*100:>4.0f}%  "
              f"+{(tot['neuro']-tot['sym'])/tot['n']*100:.0f}pp")


if __name__ == "__main__":
    main()
