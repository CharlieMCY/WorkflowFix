"""RQ6-style historical reproducibility on true_backport cases from
gaps_with_history.jsonl: does WORKFLOWBP v2 reproduce what the maintainer
ACTUALLY backported?

For each (repo, master clean-fix, branch, maintainer backport commit):
  * compile master's (parent -> commit) fix into a v2 IR program,
  * fetch target_before = file just before the maintainer's backport commit,
  * fetch target_after  = file AT the maintainer's backport commit (ground truth),
  * apply our program to target_before with a live GitHub pin resolver,
  * classify: byte_equal / ast_equal (structural) / divergent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from demo_backport import raw_file, parent_sha, make_resolver
from backport_ir.compile import compile_program
from backport_ir.apply import apply_program
from backport_ir.wsp import to_wsp
from backport_ir._yaml import load_safe
from backport_ir.verify import (
    actionlint_oracle,
    minimality_oracle,
    permissions_oracle,
    zizmor_oracle_local,
)

CANDS = json.loads(Path("rq6_cands.json").read_text())
OUT = Path("rq6_out")


def run_one(c: dict, resolver) -> dict:
    repo, f = c["repo"], c["file"]
    m_after = raw_file(repo, c["master_sha"], f)
    m_par = parent_sha(repo, c["master_sha"])
    m_before = raw_file(repo, m_par, f) if m_par else None
    t_after = raw_file(repo, c["backport_sha"], f)          # maintainer ground truth
    b_par = parent_sha(repo, c["backport_sha"])
    t_before = raw_file(repo, b_par, f) if b_par else None
    if not all([m_after, m_before, t_after, t_before]):
        return {**c, "status": "fetch_fail"}

    prog = compile_program(repo, c["master_sha"], f, m_before, m_after, c["idents"])
    res = apply_program(prog, t_before, resolver=resolver)
    ours = res.patched_text

    # RQ6 reproducibility class (vs maintainer ground truth)
    byte_eq = ours == t_after
    ast_eq = (load_safe(ours) == load_safe(t_after))
    cls = "byte_equal" if byte_eq else ("ast_equal" if ast_eq else "divergent")

    # full oracle stack on (target_before -> our_patched)
    zl = zizmor_oracle_local(prog, t_before, ours, res)
    al = actionlint_oracle(t_before, ours)
    po = permissions_oracle(prog, t_before, ours)
    mo = minimality_oracle(prog, t_before, ours)
    oracles = {
        "zizmor_local": bool(zl.get("success")),
        "actionlint": bool(al.get("success")),
        "permissions": bool(po.get("success")),
        "minimality": bool(mo.get("success")),
    }
    accepted = all(oracles.values())

    d = OUT / (repo.replace("/", "__") + "__" + c["branch"].replace("/", "__"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "target_before.yml").write_text(t_before)
    (d / "maintainer_after.yml").write_text(t_after)
    (d / "our_patched.yml").write_text(ours)
    (d / "program.wsp").write_text(to_wsp(prog))

    return {**c, "status": "ok", "class": cls,
            "apply": res.summary()["by_status"],
            "changed": ours != t_before,
            "oracles": oracles, "accepted": accepted,
            "minimality_nonsec": mo.get("n_non_security_changes"),
            "dir": str(d)}


def main():
    OUT.mkdir(exist_ok=True)
    resolver = make_resolver()
    rows = []
    for i, c in enumerate(CANDS, 1):
        try:
            r = run_one(c, resolver)
        except Exception as e:
            r = {**c, "status": "error", "error": f"{type(e).__name__}: {e}"}
        rows.append(r)
        tag = r.get("class", r["status"])
        print(f"[{i}/{len(CANDS)}] {tag:12} {r['repo']:28} ({'+'.join(c['idents'])}) "
              f"-> {c['branch']}  apply={r.get('apply')}", file=sys.stderr)
    (OUT / "rq6_index.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    from collections import Counter
    cc = Counter(r.get("class", r["status"]) for r in rows)
    print("\nRQ6 classification:", dict(cc), file=sys.stderr)


if __name__ == "__main__":
    main()
