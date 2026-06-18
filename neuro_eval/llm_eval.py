"""Neuro-symbolic validation: run the LLM loop on every symbolic-FAILED case
and measure the acceptance lift over the symbolic-only baseline.

For each baseline row where the symbolic engine did NOT get an accepted patch,
re-fetch, recompile the target-independent program, re-run the symbolic apply
(to rebuild the engine diagnosis), then run `llm_backport` (oracle-gated repair
loop). Records the LLM verdict, rounds, tokens, and final oracle summary.

Uniform acceptance oracle (zizmor_global AND actionlint AND permissions AND
minimality) so symbolic and LLM verdicts are directly comparable.
"""
import argparse
import json
import random
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.llm_adapt import llm_backport  # noqa: E402
from backport_ir.neuro_backport import (  # noqa: E402
    compile_case, fetch_case, make_client, oracle_summary,
)
from backport_ir.pipeline import make_github_resolver, make_image_resolver  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402

BASE = Path("neuro_eval/baseline_rows.jsonl")
OUT = Path("neuro_eval/llm_rows.jsonl")
PATCH_DIR = Path("neuro_eval/llm_patches")
_tls = threading.local()
_IMG = make_image_resolver()  # stateless w/ own cache; shared across threads


def client():
    if not hasattr(_tls, "client"):
        _tls.client = make_client()
        _tls.resolver = make_github_resolver(_tls.client)
    return _tls.client, _tls.resolver


def process(r, rounds, max_chars):
    cl, resolver = client()
    repo, sha, branch, path = r["repository"], r["commit_hash"], r["branch"], r["file"]
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": r["idents"], "klass": r["klass"]}
    c = fetch_case(cl, repo, sha, branch, path, r["idents"])
    if c.fetch_error:
        return {**base, "status": c.fetch_error}
    if max_chars and len(c.target_text) > max_chars:
        # full-file regeneration is impractical for very large workflows;
        # honest skip (a diff-based output strategy is future work).
        return {**base, "status": "skipped_large", "target_chars": len(c.target_text)}
    try:
        prog = compile_case(c)
        res = llm_backport(c, prog, resolver=resolver, image_resolver=_IMG,
                           max_rounds=rounds)
    except Exception as e:
        return {**base, "status": "exc", "error": str(e)[:200]}
    if res.accepted:
        safe = f"{repo.replace('/','__')}__{sha[:10]}__{branch.replace('/','__')}__{path.replace('/','__')}"
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        (PATCH_DIR / f"{safe}.patched.yml").write_text(res.patched_text)
        (PATCH_DIR / f"{safe}.wsp").write_text(res.wsp)
    return {
        **base, "status": "ok",
        "symbolic_accepted": False,
        "llm_accepted": res.accepted,
        "rounds": res.rounds,
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "oracles": oracle_summary(res.oracles) if res.oracles else {},
        "history": res.history,
        "error": res.error,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-chars", type=int, default=16000,
                    help="skip targets larger than this (full-file strategy limit)")
    ap.add_argument("--klass", default="")
    ap.add_argument("--supported-only", action="store_true",
                    help="only cases whose idents are all modeled constructs "
                         "(well-calibrated oracles)")
    ap.add_argument("--base", default="", help="baseline rows path (default baseline_rows.jsonl)")
    ap.add_argument("--out", default="", help="output rows path (default llm_rows.jsonl)")
    args = ap.parse_args()

    global BASE, OUT
    if args.base:
        BASE = Path(args.base)
    if args.out:
        OUT = Path(args.out)

    from backport_ir.compile import _IDENT_CONSTRUCTS
    supported = set(_IDENT_CONSTRUCTS)

    rows = [json.loads(l) for l in BASE.read_text().splitlines()]

    def _rq5_accepted(r):  # the RQ5-slide acceptance: zizmor_local AND actionlint
        o = r.get("oracles", {}) or {}
        return bool(o.get("zizmor_local")) and bool(o.get("actionlint"))

    fails = [r for r in rows if r["status"] == "ok" and not _rq5_accepted(r)]
    if args.klass:
        fails = [r for r in fails if r["klass"] == args.klass]
    if args.supported_only:
        fails = [r for r in fails if set(r["idents"]) <= supported]
    random.Random(13).shuffle(fails)  # spread classes for early coverage
    if args.limit:
        fails = fails[:args.limit]

    done = jsonl_already_done(
        OUT, lambda r: (r["repository"], r["commit_hash"], r["branch"], r["file"]))
    todo = [r for r in fails
            if (r["repository"], r["commit_hash"], r["branch"], r["file"]) not in done]
    print(f"symbolic-failed={len(fails)} done={len(done)} todo={len(todo)}", flush=True)

    n = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, r, args.rounds, args.max_chars): r for r in todo}
        for fut in as_completed(futs):
            jsonl_append(OUT, fut.result())
            n += 1
            if n % 10 == 0:
                print(f"  {n}/{len(todo)}", flush=True)

    report()


def report():
    rows = [json.loads(l) for l in OUT.read_text().splitlines()]
    ok = [r for r in rows if r["status"] == "ok"]
    print(f"\n=== LLM on {len(rows)} symbolic-failed cases ({len(ok)} evaluated) ===")
    print("statuses:", Counter(r["status"] for r in rows).most_common())
    ct = defaultdict(lambda: [0, 0])
    for r in ok:
        cell = ct[r["klass"]]
        cell[0] += 1
        cell[1] += int(r["llm_accepted"])
    print("\nclass            n   LLM-fixed   rate")
    tot = [0, 0]
    for k in ("surgical", "partial", "restructure", "no_security_edit"):
        if k in ct:
            n_, a_ = ct[k]
            tot[0] += n_
            tot[1] += a_
            print(f"  {k:15s} {n_:4d}  {a_:4d}   {a_/n_*100:5.1f}%")
    if tot[0]:
        print(f"  {'TOTAL':15s} {tot[0]:4d}  {tot[1]:4d}   {tot[1]/tot[0]*100:5.1f}%")
    fixed = [r for r in ok if r["llm_accepted"]]
    if fixed:
        print(f"\nrounds-to-fix: {Counter(r['rounds'] for r in fixed).most_common()}")
        avg_in = sum(r["input_tokens"] for r in ok) / len(ok)
        avg_out = sum(r["output_tokens"] for r in ok) / len(ok)
        print(f"avg tokens/case: in={avg_in:.0f} out={avg_out:.0f}")


if __name__ == "__main__":
    main()
