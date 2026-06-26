"""Both LLM experiments, interleaved per case, on one frozen sample.

For each case in the proportional sample (output/full/llm_sample.jsonl), in a
single pass:

  1. symbolic   -- compile + apply + 4 oracles (to know if the case needs help).
  2. fallback   -- IF symbolic did not accept: llm_backport (oracle-gated CEGIS
                   WSP synthesis). Each round re-sends a fresh, stateless request
                   whose prompt embeds the previous round's counterexamples.
  3. baseline   -- ALWAYS: ask the LLM to rewrite the whole target file directly
                   (the naive pure-LLM baseline) + grade + SHA-pin correctness.

So one fallback and one baseline run back-to-back per case (not two separate full
passes), and a single resumable rows file keeps both experiments in lock-step.

LLM provider defaults to OpenRouter / openai/gpt-5.4-nano; every call is one
stateless [system,user] POST (empty context per call). Override via LLM_BACKEND /
LLM_MODEL.

Acceptance criteria (kept comparable to the rest of the study):
  symbolic / fallback : zizmor_local AND actionlint AND permissions AND minimality
  pure-LLM baseline   : route-level (>=1 target finding removed, none introduced)
                        AND actionlint  (IR-free, same as cp/dependabot)

Usage:
    .venv/bin/python -m neuro_eval.llm_experiments --limit 20        # smoke
    .venv/bin/python -m neuro_eval.llm_experiments                   # full sample
"""
import argparse
import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path

os.environ.setdefault("LLM_BACKEND", "openrouter")
os.environ.setdefault("LLM_MODEL", "openai/gpt-5.4-mini")
os.environ.setdefault("RAYON_NUM_THREADS", "1")
os.environ.setdefault("GOMAXPROCS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.llm_adapt import llm_backport  # noqa: E402
from backport_ir.neuro_backport import (  # noqa: E402
    compile_case, evaluate_symbolic, fetch_case, make_client, oracle_summary,
)
from backport_ir.pipeline import make_github_resolver, make_image_resolver  # noqa: E402
from common import llm  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402
from neuro_eval.baseline_llm import _SYSTEM, _extract_yaml, _judge, _pins, _prompt  # noqa: E402

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
_ACCEPT_KEYS = ("zizmor_local", "actionlint", "permissions", "minimality")
_tls = threading.local()
_IMG = make_image_resolver()
ART_DIR = None      # per-case artifact dir (set in main unless --no-artifacts)


def _accepted(o: dict) -> bool:
    return bool(o) and all(o.get(k) for k in _ACCEPT_KEYS)


def _safe(r: dict) -> str:
    return (f"{r['repository'].replace('/', '__')}__{r['commit_hash'][:10]}"
            f"__{r['branch'].replace('/', '__')}__{r['file'].replace('/', '__')}")


def _client():
    if not hasattr(_tls, "c"):
        _tls.c = make_client()
        _tls.r = make_github_resolver(_tls.c)
    return _tls.c, _tls.r


def _key(r: dict) -> tuple:
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


def _pin_correctness(cl, target_before: str, patched: str):
    """(new, correct, wrong_version, fabricated) for action SHAs the LLM newly
    introduced vs the target's own ref. Same metric as baseline_llm."""
    tgt_ref = {m.group(1): m.group(2)
               for m in re.finditer(r"uses:\s*([^\s@#]+)@([^\s#]+)", target_before)}
    new = _pins(patched) - _pins(target_before)
    correct = wrong = fab = 0
    for action, psha in new:
        try:
            exists = cl.get_commit(action, psha) is not None
        except Exception:
            exists = False
        if not exists:
            fab += 1
            continue
        ref = tgt_ref.get(action)
        csha = None
        if ref:
            try:
                cm = cl.get_commit(action, ref)
                csha = (cm or {}).get("sha")
            except Exception:
                csha = None
        correct += int(bool(csha) and csha.lower() == psha)
        wrong += int(not (bool(csha) and csha.lower() == psha))
    return len(new), correct, wrong, fab


def process(row: dict, rounds: int, max_chars: int, max_tokens: int = 8192) -> dict:
    repo, sha, branch, path = row["repository"], row["commit_hash"], row["branch"], row["file"]
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": row["idents"], "klass": row["klass"]}
    cl, resolver = _client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, row["idents"])
    except Exception as e:
        return {**base, "status": "fetch_exc", "error": str(e)[:200]}
    if c.fetch_error:
        return {**base, "status": c.fetch_error}
    if max_chars and len(c.target_text) > max_chars:
        return {**base, "status": "skipped_large", "target_chars": len(c.target_text)}

    out = {**base, "status": "ok"}
    res = None        # fallback LLMResult (when symbolic failed)
    patched = None    # pure-LLM rewritten YAML
    try:
        # 1) symbolic
        prog = compile_case(c)
        ev = evaluate_symbolic(prog, c.target_text, resolver)
        sym_acc = _accepted(oracle_summary(ev["oracles"]))
        out["symbolic_accepted"] = sym_acc

        # 2) fallback (only when symbolic failed)
        if not sym_acc:
            res = llm_backport(c, prog, resolver=resolver, image_resolver=_IMG,
                               max_rounds=rounds, max_tokens=max_tokens)
            fb_acc = _accepted(oracle_summary(res.oracles) if res.oracles else {})
            out.update(fallback_run=True, fallback_accepted=fb_acc,
                       fb_rounds=res.rounds, fb_in=res.input_tokens,
                       fb_out=res.output_tokens, fb_error=res.error)
        else:
            out.update(fallback_run=False, fallback_accepted=None)
        out["combined_accepted"] = sym_acc or bool(out.get("fallback_accepted"))

        # 3) pure-LLM baseline (always)
        resp = llm.complete(_SYSTEM, _prompt(c), temperature=0.0, max_tokens=max_tokens)
        patched = _extract_yaml(resp.get("text", ""))
        out.update(bl_in=resp.get("input_tokens", 0), bl_out=resp.get("output_tokens", 0))
        if not patched:
            out.update(bl_parseable=False, bl_accepted=False,
                       bl_new_pins=0, bl_correct=0, bl_wrong=0, bl_fab=0)
        else:
            n, cor, wr, fb = _pin_correctness(cl, c.target_text, patched)
            out.update(bl_parseable=True,
                       bl_accepted=_judge(c.target_text, patched, row["idents"]),
                       bl_new_pins=n, bl_correct=cor, bl_wrong=wr, bl_fab=fb)
    except Exception as e:
        out["status"] = "exc"
        out["error"] = str(e)[:200]

    # save intermediate results (fallback WSP/patched + history, baseline YAML)
    if ART_DIR is not None and out["status"] in ("ok", "exc"):
        try:
            d = ART_DIR / _safe(base)
            d.mkdir(parents=True, exist_ok=True)
            d.joinpath("target_before.yml").write_text(c.target_text)
            if res is not None:
                d.joinpath("fallback.wsp").write_text(res.wsp or "")
                if res.patched_text:
                    d.joinpath("fallback_patched.yml").write_text(res.patched_text)
                d.joinpath("fallback_meta.json").write_text(json.dumps(
                    {"accepted": out.get("fallback_accepted"), "rounds": res.rounds,
                     "error": res.error, "history": res.history},
                    indent=2, ensure_ascii=False))
            if patched:
                d.joinpath("baseline_patched.yml").write_text(patched)
            d.joinpath("meta.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return out


# ---------- driver / report -------------------------------------------------

def pct(a, b):
    return f"{100*a/b:.1f}%" if b else "-"


def _row_tokens(r: dict) -> int:
    """All LLM tokens a case consumed (fallback + baseline, in + out)."""
    return sum(int(r.get(k) or 0) for k in ("fb_in", "fb_out", "bl_in", "bl_out"))


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open()] if rows_path.exists() else []
    ok = [r for r in rows if r["status"] == "ok"]
    per = defaultdict(lambda: dict(n=0, sym=0, fb_try=0, fb_ok=0, comb=0, bl=0))
    for r in ok:
        p = per[r["klass"]]
        p["n"] += 1
        p["sym"] += int(r.get("symbolic_accepted", False))
        if r.get("fallback_run"):
            p["fb_try"] += 1
            p["fb_ok"] += int(bool(r.get("fallback_accepted")))
        p["comb"] += int(bool(r.get("combined_accepted")))
        p["bl"] += int(bool(r.get("bl_accepted")))

    out = [f"# LLM experiments (OpenRouter / {os.environ.get('LLM_MODEL', '?')}), "
           "proportional 12k\n",
           "WORKFLOWBP accept = zizmor_local+actionlint+permissions+minimality; "
           "pure-LLM accept = route-level (>=1 finding removed, none introduced)+actionlint.\n",
           f"{'class':16}{'n':>7}{'symbolic':>10}{'fb rec/try':>13}"
           f"{'combined':>10}{'pureLLM':>10}"]
    tot = dict(n=0, sym=0, fb_try=0, fb_ok=0, comb=0, bl=0)
    for k in CLASSES:
        if k not in per:
            continue
        p = per[k]
        for f in tot:
            tot[f] += p[f]
        out.append(f"{k:16}{p['n']:>7}{pct(p['sym'],p['n']):>10}"
                   f"{p['fb_ok']:>4}/{p['fb_try']:<4}{'':>4}"
                   f"{pct(p['comb'],p['n']):>10}{pct(p['bl'],p['n']):>10}")
    out.append(f"{'TOTAL':16}{tot['n']:>7}{pct(tot['sym'],tot['n']):>10}"
               f"{tot['fb_ok']:>4}/{tot['fb_try']:<4}{'':>4}"
               f"{pct(tot['comb'],tot['n']):>10}{pct(tot['bl'],tot['n']):>10}")

    # pure-LLM SHA pin correctness
    tp = sum(r.get("bl_new_pins", 0) for r in ok)
    tc = sum(r.get("bl_correct", 0) for r in ok)
    tw = sum(r.get("bl_wrong", 0) for r in ok)
    tf = sum(r.get("bl_fab", 0) for r in ok)
    unparse = sum(1 for r in ok if r.get("bl_parseable") is False)
    out.append("\n## pure-LLM SHA pin correctness (vs WORKFLOWBP pin(), correct by design)")
    out.append(f"  new pins {tp}: correct {tc} ({pct(tc,tp)}), wrong_version {tw} "
               f"({pct(tw,tp)}), fabricated {tf} ({pct(tf,tp)})")
    out.append(f"  unparseable pure-LLM outputs: {unparse}")
    out.append(f"\nstatuses: {Counter(r['status'] for r in rows).most_common()}")
    toks = ok or [{}]
    out.append("avg tokens/case: "
               f"fallback in={sum(r.get('fb_in',0) for r in ok)/max(1,sum(1 for r in ok if r.get('fallback_run'))):.0f} "
               f"out={sum(r.get('fb_out',0) for r in ok)/max(1,sum(1 for r in ok if r.get('fallback_run'))):.0f} | "
               f"baseline in={sum(r.get('bl_in',0) for r in toks)/len(toks):.0f} "
               f"out={sum(r.get('bl_out',0) for r in toks)/len(toks):.0f}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="output/full/llm_sample.jsonl")
    ap.add_argument("--out-dir", default="output/full/llm_experiments")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3, help="fallback CEGIS rounds")
    ap.add_argument("--max-chars", type=int, default=16000)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-artifacts", action="store_true",
                    help="do not save per-case WSP/patched/history (default: save)")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--token-budget", type=int, default=0,
                    help="stop gracefully once cumulative in+out tokens reach this "
                         "(0=unlimited; counts tokens already in rows.jsonl on resume)")
    args = ap.parse_args()
    global ART_DIR
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"
    if not args.no_artifacts:
        ART_DIR = out / "artifacts"
        ART_DIR.mkdir(parents=True, exist_ok=True)

    if not args.report_only:
        sample = [json.loads(l) for l in open(args.sample) if l.strip()]
        if args.limit:
            sample = sample[:args.limit]
        done = jsonl_already_done(rows_path, _key)
        todo = [r for r in sample if _key(r) not in done]
        budget = args.token_budget
        spent = (sum(_row_tokens(json.loads(l)) for l in open(rows_path) if l.strip())
                 if budget and rows_path.exists() else 0)
        print(f"backend={os.environ.get('LLM_BACKEND')} model={os.environ.get('LLM_MODEL')} "
              f"sample={len(sample)} done={len(done)} todo={len(todo)} workers={args.workers} "
              f"token_budget={budget or 'unlimited'} spent={spent}", flush=True)
        lock = threading.Lock()
        n = [0]
        it = iter(todo)
        stopping = False
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            inflight = set()
            for _ in range(args.workers):           # prime the pool
                r = next(it, None)
                if r is None:
                    break
                inflight.add(ex.submit(process, r, args.rounds, args.max_chars))
            while inflight:
                ready, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in ready:
                    res = fut.result()
                    with lock:
                        jsonl_append(rows_path, res)
                        spent += _row_tokens(res)
                        n[0] += 1
                        if n[0] % 20 == 0 or n[0] == len(todo):
                            print(f"  {n[0]}/{len(todo)} tokens={spent:,}", flush=True)
                    if budget and spent >= budget and not stopping:
                        stopping = True
                        print(f"  token budget {budget:,} reached (spent {spent:,}); "
                              f"draining {len(inflight)} in-flight, no new cases", flush=True)
                if not stopping:                    # backfill only while running
                    while len(inflight) < args.workers:
                        r = next(it, None)
                        if r is None:
                            break
                        inflight.add(ex.submit(process, r, args.rounds, args.max_chars))
        if stopping:
            print(f"  stopped on token budget after {n[0]} new rows this run; "
                  f"re-run the same command to resume", flush=True)

    rep = report(rows_path)
    (out / "report.md").write_text(rep)
    print("\n" + rep)
    print(f"\nrows: {rows_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
