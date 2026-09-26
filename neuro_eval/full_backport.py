"""Full backport over the gap set, reported by the four transplant classes,
with an LLM (Claude Code) fallback on the cases the symbolic engine cannot close.

One command, two resumable phases:

  Phase 1 (symbolic): for every still-vulnerable (fix x branch x file) gap pair,
      fetch via the cached GitHub client, compile the master fix, apply, and run
      the four acceptance oracles. Records the transplant class (surgical /
      partial / restructure / no_security_edit) and whether it was accepted.

  Phase 2 (LLM fallback): for every symbolic-FAILED pair, run the oracle-gated
      CEGIS repair loop (backport_ir.llm_adapt.llm_backport). By default the LLM
      is driven through Claude Code headless (LLM_BACKEND=claude_code), so no
      external API key is needed.

Final report: a per-class table of symbolic-accepted, LLM-recovered, and combined
accepted rates. Acceptance is the same four-oracle criterion for both engines
(zizmor + actionlint + permissions + minimality), so the columns are comparable.

All GitHub reads go through common.cache (cache/github, cache/commit); a warm
cache means (near-)zero network. Both phases append rows and skip work already
done, so re-running resumes.

Usage:
    .venv/bin/python -m neuro_eval.full_backport                 # full run
    .venv/bin/python -m neuro_eval.full_backport --no-llm        # symbolic only
    .venv/bin/python -m neuro_eval.full_backport --limit 100     # smoke
    .venv/bin/python -m neuro_eval.full_backport --gaps output/50k/backport_gaps/gaps.jsonl
"""
import argparse
import json
import os
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Default the LLM fallback to Claude Code headless (no API key needed). Override
# by exporting LLM_BACKEND / LLM_MODEL before launching.
os.environ.setdefault("LLM_BACKEND", "claude_code")

# Cap the oracle scanners to one core PER invocation. zizmor (Rust/rayon) and
# actionlint (Go) otherwise each grab all cores; with many concurrent workers
# that oversubscribes the box (thread thrash, context-switch storm) and collapses
# throughput. One core per scan lets N workers map to ~N cores.
os.environ.setdefault("RAYON_NUM_THREADS", "1")
os.environ.setdefault("GOMAXPROCS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.llm_adapt import llm_backport  # noqa: E402
from backport_ir.neuro_backport import (  # noqa: E402
    compile_case, evaluate_symbolic, fetch_case, iter_gap_cases, make_client,
    oracle_summary,
)
from backport_ir.pipeline import make_github_resolver, make_image_resolver  # noqa: E402
from backport_ir.wsp import to_wsp  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402

GAPS = Path("output/50k/backport_gaps/gaps.jsonl")
OUT = Path("neuro_eval/full")
CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
ART_DIR = None        # set in main() when --artifacts != none
ART_MODE = "none"     # none | fail | all  (which cases to dump WSP/patched for)
SYM_MAX = 0           # skip symbolic on targets larger than this many chars (0=off)
_tls = threading.local()
_IMG = make_image_resolver()             # stateless, own cache; shared across threads


def _client():
    if not hasattr(_tls, "client"):
        _tls.client = make_client()
        _tls.resolver = make_github_resolver(_tls.client)
    return _tls.client, _tls.resolver


def _key(r: dict) -> tuple:
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


# Acceptance = the localized four-oracle criterion (paper §IV-D), identical to
# the shipped run_backport. We deliberately do NOT use zizmor_GLOBAL: a release
# branch routinely carries other instances of the same rule master never
# touched, so the file-level check fails even when the construct master targeted
# was correctly fixed. Computed from the stored oracle flags, so the criterion is
# recomputable from the rows without re-running.
_ACCEPT_KEYS = ("zizmor_local", "actionlint", "permissions", "minimality")


def _accepted(oracles: dict) -> bool:
    return bool(oracles) and all(oracles.get(k) for k in _ACCEPT_KEYS)


def _case_dir(r: dict) -> Path:
    name = (f"{r['repository'].replace('/', '__')}__{r['commit_hash'][:10]}"
            f"__{r['branch'].replace('/', '__')}__{r['file'].replace('/', '__')}")
    d = ART_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unique_cases():
    seen, uniq = set(), []
    for c in iter_gap_cases(GAPS):
        k = (c[0], c[1], c[2], c[3])
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


# ---------- phase 1: symbolic ----------------------------------------------

def _symbolic(case_tuple) -> dict:
    repo, sha, branch, path, idents = case_tuple
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": idents}
    cl, resolver = _client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, idents)
    except Exception as e:
        return {**base, "status": "fetch_exc", "error": str(e)[:200]}
    if c.fetch_error:
        return {**base, "status": c.fetch_error}
    if SYM_MAX and len(c.target_text) > SYM_MAX:
        # Giant outlier workflows (e.g. deckhouse's ~88KB e2e files, replicated
        # across 100+ branches) make single-threaded zizmor + 6 scans + pin
        # lookups crawl. Skip them rather than stall the whole run.
        return {**base, "status": "skipped_large", "target_chars": len(c.target_text)}
    try:
        prog = compile_case(c)
        ev = evaluate_symbolic(prog, c.target_text, resolver)
    except Exception as e:
        return {**base, "status": "eval_exc", "error": str(e)[:200]}
    orsum = oracle_summary(ev["oracles"])
    acc = _accepted(orsum)
    if ART_DIR is not None and (ART_MODE == "all" or (ART_MODE == "fail" and not acc)):
        try:
            d = _case_dir(base)
            (d / "patch.wsp").write_text(to_wsp(prog))
            (d / "patched.yml").write_text(ev["patched_text"])
            (d / "target_before.yml").write_text(c.target_text)
            (d / "meta.json").write_text(json.dumps({
                **base, "klass": ev["klass"], "symbolic_accepted": acc,
                "oracles": orsum, "apply_summary": ev["apply_summary"],
                "fix_commit_url":
                    f"https://github.com/{base['repository']}/commit/{base['commit_hash']}",
            }, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return {**base, "status": "ok", "klass": ev["klass"],
            "symbolic_accepted": acc,
            "oracles": orsum,
            "target_chars": len(c.target_text)}


# ---------- phase 2: LLM (Claude Code) fallback ----------------------------

def _llm(symrow: dict, rounds: int, max_chars: int) -> dict:
    repo, sha, branch, path = (symrow["repository"], symrow["commit_hash"],
                               symrow["branch"], symrow["file"])
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": symrow["idents"], "klass": symrow["klass"]}
    if max_chars and symrow.get("target_chars", 0) > max_chars:
        return {**base, "status": "skipped_large",
                "target_chars": symrow.get("target_chars")}
    cl, resolver = _client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, symrow["idents"])
        if c.fetch_error:
            return {**base, "status": c.fetch_error}
        prog = compile_case(c)
        res = llm_backport(c, prog, resolver=resolver, image_resolver=_IMG,
                           max_rounds=rounds)
    except Exception as e:
        return {**base, "status": "exc", "error": str(e)[:200]}
    orsum = oracle_summary(res.oracles) if res.oracles else {}
    if ART_DIR is not None and res.wsp:
        try:
            d = _case_dir(base)
            (d / "llm_patch.wsp").write_text(res.wsp)
            if res.patched_text:
                (d / "llm_patched.yml").write_text(res.patched_text)
            (d / "meta_llm.json").write_text(json.dumps({
                **base, "llm_accepted": _accepted(orsum), "rounds": res.rounds,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "oracles": orsum, "error": res.error,
            }, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return {**base, "status": "ok", "llm_accepted": _accepted(orsum),
            "rounds": res.rounds, "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
            "oracles": orsum, "error": res.error}


# ---------- driver ----------------------------------------------------------

def _run_phase(name, fn, work, rows_path, workers):
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    done = jsonl_already_done(rows_path, _key)
    todo = [w for w in work if _key_of(w) not in done]
    print(f"[{name}] total={len(work)} done={len(done)} todo={len(todo)} "
          f"workers={workers}", flush=True)
    lock = threading.Lock()
    n = [0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fn, w) for w in todo]
        for fut in as_completed(futs):
            row = fut.result()
            with lock:
                jsonl_append(rows_path, row)
                n[0] += 1
                if n[0] % 50 == 0 or n[0] == len(todo):
                    print(f"  [{name}] {n[0]}/{len(todo)}", flush=True)


def _key_of(w):
    # work item is either a gap tuple (phase 1) or a symbolic row dict (phase 2)
    if isinstance(w, dict):
        return _key(w)
    return (w[0], w[1], w[2], w[3])


def report(sym_path: Path, llm_path: Path) -> str:
    sym = [json.loads(l) for l in sym_path.open()] if sym_path.exists() else []
    llm = [json.loads(l) for l in llm_path.open()] if llm_path.exists() else []
    ok = [r for r in sym if r["status"] == "ok"]
    llm_by = {_key(r): r for r in llm if r.get("status") == "ok"}

    per = defaultdict(lambda: {"n": 0, "sym": 0, "llm_fix": 0, "llm_try": 0})
    for r in ok:
        cell = per[r["klass"]]
        cell["n"] += 1
        if _accepted(r["oracles"]):
            cell["sym"] += 1
        else:
            lr = llm_by.get(_key(r))
            if lr is not None:
                cell["llm_try"] += 1
                if _accepted(lr.get("oracles", {})):
                    cell["llm_fix"] += 1

    out = ["# Full backport by transplant class (Claude Code fallback)\n",
           "accept criterion = zizmor_local + actionlint + permissions + "
           "minimality (localized, per paper §IV-D; same for symbolic and LLM).\n",
           f"{'class':16} {'n':>6} {'symbolic':>16} {'LLM rec/try':>13} "
           f"{'combined':>16}"]
    tot = {"n": 0, "sym": 0, "llm_fix": 0, "llm_try": 0}
    for k in CLASSES:
        c = per.get(k)
        if not c:
            continue
        for f in tot:
            tot[f] += c[f]
        comb = c["sym"] + c["llm_fix"]
        out.append(f"{k:16} {c['n']:>6} {c['sym']:>6} {pct(c['sym'],c['n']):>8} "
                   f"{c['llm_fix']:>4}/{c['llm_try']:<4} "
                   f"{comb:>6} {pct(comb,c['n']):>8}")
    comb = tot["sym"] + tot["llm_fix"]
    out.append(f"{'TOTAL':16} {tot['n']:>6} {tot['sym']:>6} {pct(tot['sym'],tot['n']):>8} "
               f"{tot['llm_fix']:>4}/{tot['llm_try']:<4} "
               f"{comb:>6} {pct(comb,tot['n']):>8}")
    # diagnostics
    out.append("\nsymbolic statuses: " + str(Counter(r["status"] for r in sym).most_common()))
    if llm:
        out.append("LLM statuses:      " + str(Counter(r["status"] for r in llm).most_common()))
        toks = [r for r in llm if r.get("status") == "ok"]
        if toks:
            ai = sum(r.get("input_tokens", 0) for r in toks) / len(toks)
            ao = sum(r.get("output_tokens", 0) for r in toks) / len(toks)
            out.append(f"LLM avg tokens/case: in={ai:.0f} out={ao:.0f}")
    return "\n".join(out)


def pct(a, b):
    return f"{100*a/b:.1f}%" if b else "-"


def main() -> int:
    global GAPS, OUT, ART_DIR, ART_MODE, SYM_MAX
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", default=str(GAPS))
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--workers", type=int, default=16, help="symbolic phase workers")
    ap.add_argument("--llm-workers", type=int, default=4, help="LLM phase workers")
    ap.add_argument("--rounds", type=int, default=3, help="LLM CEGIS rounds")
    ap.add_argument("--max-chars", type=int, default=16000,
                    help="skip LLM on targets larger than this")
    ap.add_argument("--sym-max-chars", type=int, default=40000,
                    help="skip SYMBOLIC on targets larger than this (giant "
                         "outlier workflows that crawl); 0 disables")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-llm", action="store_true", help="symbolic only")
    ap.add_argument("--llm-cases", default="",
                    help="restrict the LLM fallback to symbolic-failures whose "
                         "(repo,commit,branch,file) key appears in this jsonl "
                         "(e.g. the pure-LLM baseline sample), for an apples-to-"
                         "apples subset")
    ap.add_argument("--artifacts", choices=("none", "fail", "all"), default="none",
                    help="dump per-case WSP/patched/target/meta: none, only "
                         "symbolic-failed (fail), or every case (all)")
    ap.add_argument("--artifacts-dir", default="",
                    help="where to write artifacts (default <out-dir>/artifacts)")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    GAPS = Path(args.gaps)
    OUT = Path(args.out_dir)
    sym_path = OUT / "symbolic_rows.jsonl"
    llm_path = OUT / "llm_rows.jsonl"
    SYM_MAX = args.sym_max_chars
    ART_MODE = args.artifacts
    if ART_MODE != "none":
        ART_DIR = Path(args.artifacts_dir) if args.artifacts_dir else (OUT / "artifacts")
        ART_DIR.mkdir(parents=True, exist_ok=True)

    if not args.report_only:
        cases = _unique_cases()
        if args.limit:
            cases = cases[:args.limit]
        print(f"backend={os.environ.get('LLM_BACKEND')} "
              f"model={os.environ.get('LLM_MODEL','(default)')} gaps={GAPS} "
              f"cases={len(cases)}", flush=True)
        _run_phase("symbolic", _symbolic, cases, sym_path, args.workers)
        (OUT / "report.md").write_text(report(sym_path, llm_path))  # symbolic view early

        if not args.no_llm:
            sym = [json.loads(l) for l in sym_path.open()]
            fails = [r for r in sym
                     if r["status"] == "ok" and not _accepted(r.get("oracles", {}))]
            if args.llm_cases:
                keys = set()
                for l in open(args.llm_cases):
                    try:
                        keys.add(_key(json.loads(l)))
                    except Exception:
                        pass
                fails = [r for r in fails if _key(r) in keys]
                print(f"[llm] restricted to {len(keys)} sample cases", flush=True)
            print(f"[llm] symbolic failures to attempt: {len(fails)}", flush=True)
            _run_phase("llm", lambda r: _llm(r, args.rounds, args.max_chars),
                       fails, llm_path, args.llm_workers)

    rep = report(sym_path, llm_path)
    (OUT / "report.md").write_text(rep)
    print("\n" + rep)
    print(f"\nrows: {sym_path} , {llm_path}\nreport: {OUT/'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
