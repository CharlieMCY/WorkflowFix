"""Pure-LLM baseline (RQ7): ask the model to rewrite the whole target file.

The naive "just prompt an LLM" approach, contrasted with WORKFLOWBP. Given the
master before/after fix and the divergent target file, the model is asked to
emit the FULLY PATCHED target YAML directly -- no WSP, no engine apply. Same
Claude Code config as the fallback (LLM_BACKEND=claude_code, stripped system
prompt, no session transcript, temp-0 cache, sonnet by default). Like the
fallback, it gets up to --rounds rounds (default 3): a rejected output is sent
back with the verifier's feedback (see rewrite()).

Two things are measured, both comparable across methods:
  * acceptance -- same route-level criterion as the copy-paste / dependabot
    baselines: the output must parse, remove >=1 target_ident finding, introduce
    none, and pass actionlint.
  * SHA hallucination -- for every `uses: action@<40hex>` the model NEWLY
    introduces, check on the live GitHub API whether that commit actually exists
    in that action's repo. Fabricated SHAs are the structural failure mode the
    paper attributes to naive LLM use; WORKFLOWBP's deterministic pin() avoids it
    by construction (it never writes a SHA it did not resolve).

Case universe + transplant class are read from the backport run's
symbolic_rows.jsonl, so the LLM baseline runs on the SAME cases as WORKFLOWBP and
is directly comparable. By default a stratified per-class sample is run (a full
run is up to --rounds LLM calls per case, the most expensive of all methods).

Usage:
    .venv/bin/python -m neuro_eval.baseline_llm --per-class 500       # sample
    .venv/bin/python -m neuro_eval.baseline_llm --all                 # everything
    .venv/bin/python -m neuro_eval.baseline_llm --per-class 3 --limit 9   # smoke
"""
import argparse
import json
import os
import random
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("LLM_BACKEND", "claude_code")
os.environ.setdefault("RAYON_NUM_THREADS", "1")
os.environ.setdefault("GOMAXPROCS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.neuro_backport import fetch_case, make_client  # noqa: E402
from backport_ir.verify import actionlint_oracle  # noqa: E402
from common import llm  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402
from pattern_miner.scan import diff_findings, scan_bytes  # noqa: E402

SYM_ROWS = Path("output/full/backport_run/symbolic_rows.jsonl")
OUT = Path("output/full/baseline_llm")
CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
_tls = threading.local()
_PIN40 = re.compile(r"uses:\s*([^\s@#]+)@([0-9a-fA-F]{40})\b")

_SYSTEM = (
    "You backport a security fix for a GitHub Actions workflow. You are given the "
    "fix as a before/after pair on the source branch and the current file on a "
    "divergent target branch. Output the FULLY PATCHED target file: apply the same "
    "security fix, adapted to the target's job names, action versions, and "
    "structure. Change only what the security fix requires; leave everything else "
    "unchanged. Output ONLY the patched YAML in a single ```yaml code block, with "
    "no explanation."
)


def _client():
    if not hasattr(_tls, "client"):
        _tls.client = make_client()
    return _tls.client


def _key(r: dict) -> tuple:
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


def _prompt(c) -> str:
    return (f"Security rules to fix: {', '.join(c.idents)}\n\n"
            f"=== SOURCE BEFORE FIX ===\n{c.before_text}\n"
            f"=== SOURCE AFTER FIX ===\n{c.after_text}\n"
            f"=== TARGET FILE TO PATCH ===\n{c.target_text}\n")


def _extract_yaml(text: str) -> str | None:
    """Largest fenced block that parses as YAML; falls back to the raw text."""
    from backport_ir.apply import load
    blocks = re.findall(r"```(?:[a-zA-Z]+)?\n(.*?)```", text, re.S) or [text]
    best = None
    for b in sorted(blocks, key=len, reverse=True):
        try:
            load(b)
            best = b
            break
        except Exception:
            continue
    return best


def _pins(text: str) -> set:
    return {(m.group(1), m.group(2).lower()) for m in _PIN40.finditer(text)}


def _judge_detail(target_before: str, patched: str, idents) -> list[str]:
    """Violations of the route-level criterion, phrased as feedback for the next
    round ([] = accepted)."""
    bs = scan_bytes(target_before.encode("utf-8", "replace"))
    ps = scan_bytes(patched.encode("utf-8", "replace"))
    if not bs.get("ok"):
        return [f"zizmor could not scan the target file: {str(bs.get('error'))[:160]}"]
    if not ps.get("ok"):
        return [f"zizmor could not scan your output: {str(ps.get('error'))[:160]} "
                "— it is not a valid workflow."]
    fixed, introduced = diff_findings(bs["findings"], ps["findings"])
    v: list[str] = []
    if introduced:
        v.append("SECURITY REGRESSION: your output INTRODUCED new findings "
                 f"{sorted({f['ident'] for f in introduced})}. Remove the cause.")
    if not any(f["ident"] in idents for f in fixed):
        v.append(f"FIX NOT ACHIEVED: no targeted finding {sorted(idents)} was removed. "
                 "Apply the security fix to this file.")
    a = actionlint_oracle(target_before, patched)
    if a.get("status") != "ok":
        v.append(f"actionlint could not check your output ({a.get('where')}): "
                 f"{str(a.get('error'))[:160]}")
    elif not a.get("success"):
        for f in (a.get("introduced") or [])[:6]:
            v.append(f"WORKFLOW BROKEN (actionlint): [{f.get('kind')}] {f.get('message')}")
    return v


def _judge(target_before: str, patched: str, idents) -> bool:
    return not _judge_detail(target_before, patched, idents)


def _classify_pins(cl, target_before: str, patched: str) -> dict:
    """Status of each action SHA the model NEWLY introduced (was a tag on the
    target, now a 40-hex SHA). correct = matches what the target's OWN ref
    resolves to (the backport-safe pin). wrong_version = a real SHA but not the
    target's ref (e.g. master's SHA copied -> silent upgrade). fabricated = no
    such commit in the action repo. WORKFLOWBP's pin() is correct by design."""
    tgt_ref = {m.group(1): m.group(2)
               for m in re.finditer(r"uses:\s*([^\s@#]+)@([^\s#]+)", target_before)}
    status = {}
    for action, psha in _pins(patched) - _pins(target_before):
        try:
            exists = cl.get_commit(action, psha) is not None
        except Exception:
            exists = False
        if not exists:
            status[(action, psha)] = "fabricated"
            continue
        ref = tgt_ref.get(action)
        csha = None
        if ref:
            try:
                cm = cl.get_commit(action, ref)
                csha = (cm or {}).get("sha")
            except Exception:
                csha = None
        status[(action, psha)] = ("correct" if csha and csha.lower() == psha
                                  else "wrong_version")
    return status


def rewrite(c, idents, *, cl=None, max_rounds: int = 3, max_tokens: int = 8192) -> dict:
    """Ask the LLM to rewrite the whole target file, up to `max_rounds` rounds.

    Round 1 sends the plain prompt. After a rejected round, the next stateless
    request re-sends it with the previous output and the verifier's feedback,
    the same protocol as the fallback's CEGIS loop. A round passes when its
    output meets the route-level criterion (_judge) and, given `cl`, pins no
    fabricated action SHA. The returned `patched` is the last round's output."""
    base_user = _prompt(c)
    user = base_user
    out = {"patched": None, "accepted": False, "rounds": 0,
           "input_tokens": 0, "output_tokens": 0, "history": []}
    for rnd in range(1, max_rounds + 1):
        out["rounds"] = rnd
        resp = llm.complete(_SYSTEM, user, temperature=0.0, max_tokens=max_tokens)
        out["input_tokens"] += int(resp.get("input_tokens", 0))
        out["output_tokens"] += int(resp.get("output_tokens", 0))
        text = resp.get("text", "")
        patched = _extract_yaml(text)
        out["patched"] = patched
        if not patched:
            viol = ["Your output was not parseable YAML. Output the complete patched "
                    "file in one ```yaml block."]
        else:
            viol = _judge_detail(c.target_text, patched, idents)
            if cl is not None:
                viol += [f"FABRICATED PIN: `{a}@{s}` is not a commit of `{a}`. Pin only "
                         "to commits that exist."
                         for (a, s), st in sorted(_classify_pins(cl, c.target_text,
                                                                 patched).items())
                         if st == "fabricated"]
        out["history"].append({"round": rnd, "accepted": not viol, "violations": viol})
        if not viol:
            out["accepted"] = True
            return out
        if rnd == max_rounds:
            break
        user = (
            base_user
            + "\n\nYOUR PREVIOUS OUTPUT was REJECTED:\n"
            + "```yaml\n" + (patched or text[:4000]).rstrip() + "\n```\n\n"
            + "FEEDBACK from the verifier (fix every one):\n"
            + "\n".join(f"- {x}" for x in viol)
            + "\n\nReturn the corrected COMPLETE patched file in one ```yaml block."
        )
    return out


def process(row: dict, max_chars: int, rounds: int = 3) -> dict:
    repo, sha, branch, path = (row["repository"], row["commit_hash"],
                               row["branch"], row["file"])
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": row["idents"], "klass": row["klass"]}
    cl = _client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, row["idents"])
        if c.fetch_error:
            return {**base, "status": c.fetch_error}
        if max_chars and len(c.target_text) > max_chars:
            return {**base, "status": "skipped_large", "target_chars": len(c.target_text)}
        rw = rewrite(c, row["idents"], cl=cl, max_rounds=rounds)
    except Exception as e:
        return {**base, "status": "exc", "error": str(e)[:200]}

    patched = rw["patched"]
    tokens = {"rounds": rw["rounds"], "input_tokens": rw["input_tokens"],
              "output_tokens": rw["output_tokens"]}
    if not patched:
        return {**base, "status": "ok", "parseable": False, "llm_accepted": False,
                "new_pins": 0, "pin_correct": 0, "pin_wrong_version": 0, "pin_fabricated": 0,
                **tokens}

    st = Counter(_classify_pins(cl, c.target_text, patched).values())
    accepted = _judge(c.target_text, patched, row["idents"])
    return {**base, "status": "ok", "parseable": True, "llm_accepted": accepted,
            "new_pins": sum(st.values()), "pin_correct": st["correct"],
            "pin_wrong_version": st["wrong_version"], "pin_fabricated": st["fabricated"],
            **tokens}


# ---------- sampling + driver ----------------------------------------------

def _sample(per_class: int, all_: bool, seed: int = 11):
    rows = [json.loads(l) for l in SYM_ROWS.open()]
    ok = [r for r in rows if r.get("status") == "ok" and "klass" in r]
    seen, uniq = set(), []
    for r in ok:
        k = _key(r)
        if k not in seen:
            seen.add(k)
            uniq.append({"repository": r["repository"], "commit_hash": r["commit_hash"],
                         "branch": r["branch"], "file": r["file"],
                         "idents": r["idents"], "klass": r["klass"]})
    if all_:
        return uniq
    by = defaultdict(list)
    for r in uniq:
        by[r["klass"]].append(r)
    rng = random.Random(seed)
    out = []
    for k in CLASSES:
        items = by.get(k, [])
        rng.shuffle(items)
        out.extend(items[:per_class])
    rng.shuffle(out)
    return out


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open()] if rows_path.exists() else []
    ok = [r for r in rows if r["status"] == "ok"]
    per = defaultdict(lambda: [0, 0])
    for r in ok:
        per[r["klass"]][0] += 1
        per[r["klass"]][1] += int(r.get("llm_accepted", False))
    out = ["# Pure-LLM baseline (Claude Code), by transplant class\n",
           "accept = parses AND >=1 target finding removed AND none introduced "
           "AND actionlint clean (same route-level criterion as cp/dependabot).\n",
           f"{'class':16} {'n':>7} {'accepted':>9} {'rate':>7}"]
    tn = ta = 0
    for k in CLASSES:
        if k in per:
            n, a = per[k]
            tn += n
            ta += a
            out.append(f"{k:16} {n:>7} {a:>9} {pct(a, n):>7}")
    out.append(f"{'TOTAL':16} {tn:>7} {ta:>9} {pct(ta, tn):>7}")
    # SHA pin correctness (the headline contrast vs WORKFLOWBP pin())
    tot_pins = sum(r.get("new_pins", 0) for r in ok)
    tc = sum(r.get("pin_correct", 0) for r in ok)
    tw = sum(r.get("pin_wrong_version", 0) for r in ok)
    tf = sum(r.get("pin_fabricated", 0) for r in ok)
    withpins = [r for r in ok if r.get("new_pins", 0) > 0]
    bad_out = [r for r in withpins
               if r.get("pin_wrong_version", 0) + r.get("pin_fabricated", 0) > 0]
    unparse = sum(1 for r in ok if not r.get("parseable", True))
    out.append("\n## SHA pin correctness (vs WORKFLOWBP pin(), which is correct by design)")
    out.append(f"  new action SHA pins written:   {tot_pins}")
    out.append(f"    correct (target's own ref):  {tc} ({pct(tc, tot_pins)})")
    out.append(f"    wrong_version (real, not target's ref / upgrade): {tw} ({pct(tw, tot_pins)})")
    out.append(f"    fabricated (no such commit): {tf} ({pct(tf, tot_pins)})")
    out.append(f"  outputs that pinned >=1 action: {len(withpins)};  with >=1 "
               f"INCORRECT pin: {len(bad_out)} ({pct(len(bad_out), len(withpins))})")
    out.append(f"  unparseable outputs:           {unparse}")
    out.append(f"\nstatuses: {Counter(r['status'] for r in rows).most_common()}")
    toks = [r for r in ok]
    if toks:
        out.append(f"avg tokens/case: in={sum(r.get('input_tokens',0) for r in toks)/len(toks):.0f} "
                   f"out={sum(r.get('output_tokens',0) for r in toks)/len(toks):.0f}")
        out.append(f"avg rounds/case: {sum(r.get('rounds', 1) for r in toks)/len(toks):.2f} "
                   "(rows without `rounds` predate the multi-round loop: 1 call each)")
    return "\n".join(out)


def pct(a, b):
    return f"{100*a/b:.1f}%" if b else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=500)
    ap.add_argument("--all", action="store_true", help="every case (very expensive)")
    ap.add_argument("--cases", default="",
                    help="read the case list from this jsonl (a frozen shared "
                         "sample) instead of sampling; the same file is passed to "
                         "full_backport --llm-cases so both run on identical cases")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3,
                    help="max rewrite rounds per case (feedback after a rejected round)")
    ap.add_argument("--max-chars", type=int, default=16000,
                    help="skip targets larger than this (full-file regen impractical)")
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"

    if not args.report_only:
        if args.cases:
            sample = [json.loads(l) for l in open(args.cases) if l.strip()]
        else:
            sample = _sample(args.per_class, args.all)
        if args.limit:
            sample = sample[:args.limit]
        done = jsonl_already_done(rows_path, _key)
        todo = [r for r in sample if _key(r) not in done]
        print(f"backend={os.environ.get('LLM_BACKEND')} sample={len(sample)} "
              f"done={len(done)} todo={len(todo)} workers={args.workers}", flush=True)
        lock = threading.Lock()
        n = [0]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(process, r, args.max_chars, args.rounds) for r in todo]
            for fut in as_completed(futs):
                with lock:
                    jsonl_append(rows_path, fut.result())
                    n[0] += 1
                    if n[0] % 20 == 0 or n[0] == len(todo):
                        print(f"  {n[0]}/{len(todo)}", flush=True)

    rep = report(rows_path)
    (out / "report.md").write_text(rep)
    print("\n" + rep)
    print(f"\nrows: {rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
