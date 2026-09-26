"""RQ5 at scale: scan N (clean-fix, gap-branch) pairs from gaps_with_history.jsonl,
classify transplantability, run the v2 backport, and grade with the full oracle
stack. Reports the transplantability split and the acceptance rate — overall and
on the transplantable subset (the clean denominator).
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from demo_backport import raw_file, parent_sha, make_resolver
from backport_ir.compile import compile_program, surgical_class
from backport_ir.apply import apply_program
from backport_ir.verify import (
    actionlint_oracle,
    minimality_oracle,
    permissions_oracle,
    zizmor_oracle_local,
)

GAPS = "output/50k/backport_gaps/gaps_with_history.jsonl"
OUT = Path("rq5_out")


def sample(n: int) -> list[dict]:
    rows = [json.loads(l) for l in open(GAPS).read().splitlines() if l.strip()]
    by_combo: dict[tuple, list] = defaultdict(list)
    seen = set()
    for r in rows:
        if r.get("status") != "ok":
            continue
        for gb in r.get("gap_branches") or []:
            for f in gb.get("files") or []:
                if f.get("status") != "ok" or not f.get("V_present_idents"):
                    continue
                key = (r["repository"], r["commit_hash"], gb["branch"], f["file_path"])
                if key in seen:
                    continue
                seen.add(key)
                by_combo[tuple(sorted(r["V_fixed_idents"]))].append({
                    "repo": r["repository"], "master_sha": r["commit_hash"],
                    "file": f["file_path"], "idents": r["V_fixed_idents"],
                    "branch": gb["branch"], "branch_sha": gb["branch_head_sha"]})
    combos = sorted(by_combo, key=lambda c: -len(by_combo[c]))
    out, i = [], 0
    while len(out) < n and any(by_combo.values()):
        c = combos[i % len(combos)]
        if by_combo[c]:
            out.append(by_combo[c].pop(0))
        i += 1
    return out[:n]


def run_one(c: dict, resolver) -> dict:
    repo, sha, f = c["repo"], c["master_sha"], c["file"]
    after = raw_file(repo, sha, f)
    par = parent_sha(repo, sha)
    before = raw_file(repo, par, f) if par else None
    target = raw_file(repo, c["branch_sha"], f)
    if not all([after, before, target]):
        return {**c, "status": "fetch_fail"}
    prog = compile_program(repo, sha, f, before, after, c["idents"])
    cls = surgical_class(prog)
    res = apply_program(prog, target, resolver=resolver)
    ours = res.patched_text
    zl = zizmor_oracle_local(prog, target, ours, res)
    al = actionlint_oracle(target, ours)
    po = permissions_oracle(prog, target, ours)
    mo = minimality_oracle(prog, target, ours)
    oracles = {"zizmor_local": bool(zl.get("success")), "actionlint": bool(al.get("success")),
               "permissions": bool(po.get("success")), "minimality": bool(mo.get("success"))}
    return {**c, "status": "ok", "transplant": cls,
            "apply": res.summary()["by_status"], "changed": ours != target,
            "oracles": oracles, "accepted": all(oracles.values())}


def summarize(rows: list[dict]) -> None:
    ok = [r for r in rows if r.get("status") == "ok"]
    N = len(ok) or 1
    print(f"\n{'='*60}\nscanned {len(rows)}  (ok={len(ok)}  "
          f"fetch_fail={sum(1 for r in rows if r.get('status')=='fetch_fail')}  "
          f"error={sum(1 for r in rows if r.get('status')=='error')})")

    tp = Counter(r["transplant"] for r in ok)
    transplantable = [r for r in ok if r["transplant"] in ("surgical", "partial")]
    print("\n=== transplantability ===")
    for k in ("surgical", "partial", "restructure", "no_security_edit"):
        print(f"  {k:18} {tp[k]:4}  {100*tp[k]/N:5.1f}%")
    print(f"  -> transplantable    {len(transplantable):4}  {100*len(transplantable)/N:5.1f}%")

    def rate(rs, key): return sum(1 for r in rs if r["oracles"][key])
    print("\n=== oracle pass rate (over all ok) ===")
    for k in ("zizmor_local", "actionlint", "permissions", "minimality"):
        nc = " [non-circular]" if k in ("permissions", "minimality") else ""
        print(f"  {k:14} {rate(ok,k):4}/{len(ok)}{nc}")
    print(f"  {'ACCEPTED':14} {sum(1 for r in ok if r['accepted']):4}/{len(ok)}  (all four)")

    if transplantable:
        T = len(transplantable)
        acc_t = sum(1 for r in transplantable if r["accepted"])
        zl_t = sum(1 for r in transplantable if r["oracles"]["zizmor_local"])
        print("\n=== ON THE TRANSPLANTABLE SUBSET (the clean denominator) ===")
        print(f"  zizmor_local (security closed): {zl_t}/{T}  ({100*zl_t/T:.0f}%)")
        print(f"  ACCEPTED (all four oracles):    {acc_t}/{T}  ({100*acc_t/T:.0f}%)")

    print("\n=== safety on EVERY case (incl. non-transplantable) ===")
    safe = sum(1 for r in ok if r["oracles"]["permissions"] and r["oracles"]["minimality"]
               and r["oracles"]["actionlint"])
    print(f"  no regression/collateral/lint-break (perm+minim+actionlint): {safe}/{len(ok)}")


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    OUT.mkdir(exist_ok=True)
    resolver = make_resolver()
    cands = sample(n)
    rows = []
    for i, c in enumerate(cands, 1):
        try:
            r = run_one(c, resolver)
        except Exception as e:
            r = {**c, "status": "error", "error": f"{type(e).__name__}: {e}"}
        rows.append(r)
        tag = r.get("transplant", r["status"])
        acc = "ACC" if r.get("accepted") else ""
        print(f"[{i}/{len(cands)}] {tag:16} {acc:4} {r['repo'][:30]:30} "
              f"({'+'.join(c['idents'])[:30]}) -> {c['branch'][:18]}", file=sys.stderr)
        if i % 20 == 0:
            (OUT / "rq5_index.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    (OUT / "rq5_index.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
