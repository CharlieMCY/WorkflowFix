"""End-to-end neuro-symbolic backport demo on ONE real gap case.

Shows, for a single (main-fix, drifted-target) pair:
  1. the TARGET-INDEPENDENT semantic patch compiled from the main diff (.wsp);
  2. the symbolic apply result (why it can't finish on this drifted target);
  3. the MiMo LLM's TARGET-DEPENDENT patch, gated by the symbolic oracles;
  4. the final acceptance verdict and a diff of what actually changed.

Usage:
  .venv/bin/python neuro_eval/demo_neuro.py                      # default case
  .venv/bin/python neuro_eval/demo_neuro.py REPO SHA BRANCH FILE
"""
import difflib
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault(
    "GITHUB_TOKEN", subprocess.check_output(["gh", "auth", "token"]).decode().strip())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.llm_adapt import llm_backport  # noqa: E402
from backport_ir.neuro_backport import (  # noqa: E402
    compile_case, evaluate_symbolic, fetch_case, make_client, oracle_summary,
)
from backport_ir.pipeline import make_github_resolver, make_image_resolver  # noqa: E402
from backport_ir.wsp import to_wsp  # noqa: E402

# A restructure case (whole-step add/delete) the symbolic engine scores 0% on.
DEFAULT = ("jurajnyiri/homeassistant-tapo-control",
           "f11215566b", "5.2.2", ".github/workflows/issues.yml")


def main():
    if len(sys.argv) == 5:
        repo, sha, branch, path = sys.argv[1:5]
    else:
        repo, sha, branch, path = DEFAULT
    cl = make_client()
    resolver = make_github_resolver(cl)
    image_resolver = make_image_resolver()

    # resolve a short sha if needed
    if len(sha) < 40:
        commit = cl.get_commit(repo, sha) or {}
        sha = commit.get("sha", sha)

    # find idents from the gaps file
    import json
    idents = None
    for line in open("output/50k/backport_gaps/gaps_with_history.jsonl"):
        r = json.loads(line)
        if r["repository"] == repo and r["commit_hash"].startswith(sha[:10]):
            for gb in (r.get("gap_branches") or []):
                if gb["branch"] == branch:
                    for f in gb["files"]:
                        if f["file_path"] == path:
                            idents = f["V_present_idents"]
    idents = idents or ["excessive-permissions"]

    print(f"CASE  {repo}@{sha[:10]}  branch={branch}  file={path}\n"
          f"      vulnerability still on this branch: {idents}\n")

    c = fetch_case(cl, repo, sha, branch, path, idents)
    assert not c.fetch_error, c.fetch_error
    prog = compile_case(c)

    print("=" * 78)
    print("(1) TARGET-INDEPENDENT semantic patch compiled from the MAIN diff (.wsp)")
    print("=" * 78)
    print(to_wsp(prog))

    ev = evaluate_symbolic(prog, c.target_text, resolver)
    print("=" * 78)
    print("(2) SYMBOLIC apply onto the drifted target")
    print("=" * 78)
    print("class:", ev["klass"], "| apply:", ev["apply_summary"]["by_status"])
    print("blocking reasons:", ev["review_reasons"] or "(none)")
    print("symbolic accepted:", ev["accepted"], oracle_summary(ev["oracles"]))

    print("\n" + "=" * 78)
    print("(3) LLM concretization with symbolic feedback (MiMo, oracle-gated)")
    print("=" * 78)
    res = llm_backport(c, prog, resolver=resolver, image_resolver=image_resolver,
                       max_rounds=3, log=lambda m: print(m))
    print(f"\nLLM accepted: {res.accepted} in {res.rounds} round(s); "
          f"tokens in/out={res.input_tokens}/{res.output_tokens}")
    print("final oracles:", oracle_summary(res.oracles) if res.oracles else "{}")

    if res.patched_text:
        print("\n" + "=" * 78)
        print("(4) DIFF — target vs LLM backport (this is the target-dependent patch)")
        print("=" * 78)
        diff = difflib.unified_diff(
            c.target_text.splitlines(), res.patched_text.splitlines(),
            "target", "backported", lineterm="")
        print("\n".join(diff))


if __name__ == "__main__":
    main()
