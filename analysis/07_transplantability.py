"""Transplantability scan: which clean-fixes are surgically backportable?

A clean-fix (a zizmor finding disappeared on master AND none appeared) is only a
*transplantable* patch if its security-relevant edits are construct-local — not
step synthesis/deletion. This is a master-commit property, independent of any
target branch, computed purely from the (before -> after) diff. It scans
`clean_fixes/*/meta.json` (produced by `pattern_miner pipeline`) and reports the
surgical / restructure / no-security-edit split, overall and per zizmor rule —
the denominator the RQ5/RQ6 acceptance rate should be reported against.

    DATASET_TAG=50k .venv/bin/python -m analysis.07_transplantability
"""
from __future__ import annotations

from collections import Counter, defaultdict

from backport_ir.compile import surgical_class, surgical_review_reasons
from backport_ir.config import CLEAN_FIXES_DIR
from backport_ir.pipeline import iter_clean_fix_programs


def report(rows: list[tuple[str, list[str], str, list[str]]]) -> None:
    """rows = (repo_commit, idents, surgical_class, restructure_reasons)."""
    overall: Counter = Counter()
    per_ident: dict[str, Counter] = defaultdict(Counter)
    reasons: Counter = Counter()
    for _key, idents, cls, rr in rows:
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
        print(f"  {k:18} {overall[k]:6}  {100 * overall[k] / n:5.1f}%")
    print(f"  {'-> transplantable':18} {transplantable:6}  {100 * transplantable / n:5.1f}%"
          "  (surgical + partial)")
    print("\n=== per zizmor rule (transplantable % = engine can place the fix) ===")
    for ident, c in sorted(per_ident.items(), key=lambda x: -sum(x[1].values())):
        tot = sum(c.values()) or 1
        tp = c["surgical"] + c["partial"]
        print(f"  {ident:24} n={tot:5}  transplantable={tp:5} ({100 * tp // tot:3d}%)  "
              f"[surg={c['surgical']:4} part={c['partial']:4} restr={c['restructure']:4} "
              f"nosec={c['no_security_edit']:4}]")
    if reasons:
        print("\n=== restructure blocking reasons ===")
        for r, k in reasons.most_common():
            print(f"  {k:5}  {r}")


def main() -> int:
    if not CLEAN_FIXES_DIR.exists():
        print(f"no clean_fixes at {CLEAN_FIXES_DIR}")
        print("run `pattern_miner pipeline` first, or use scan_transplantability.py "
              "to demo on a GitHub-fetched sample.")
        return 1
    rows = []
    for cdir, prog in iter_clean_fix_programs():
        cls = surgical_class(prog)
        rr = surgical_review_reasons(prog) if cls == "restructure" else []
        rows.append((cdir, prog.target_idents, cls, rr))
    report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
