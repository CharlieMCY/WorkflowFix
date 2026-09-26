"""Feature ablation for Table VIII (WorkflowBP + Gemini-3.1-flash-lite, 12k sample).

For every case the full system ACCEPTED, reconstruct the accepted patch
(symbolic: recompile from the master commit; fallback: parse the saved
fallback.wsp), remove ONE feature by mutating the IRProgram, re-apply, and
re-run the same 4 oracles. LLM-free (no re-synthesis, no API): the features are
apply/compile-time, so removing them is a deterministic recompute.

  Constrained job binding  -> clear keyvar key_pin/fingerprint -> resolve fans out
  Edit-relevance filter    -> clear edit.review -> apply all changed leaves
Branch-local pin and CEGIS K=1 are handled separately (see notes printed at end).
"""
import argparse, copy, dataclasses as dc, json, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from neuro_eval.llm_experiments import _safe, _client, _accepted
from backport_ir.neuro_backport import fetch_case, compile_case, evaluate_symbolic, oracle_summary
from backport_ir.wsp import from_wsp
from common.cache import jsonl_already_done, jsonl_append

ART = Path("output/full/llm_experiments_gemini/artifacts")
ROWS = Path("output/full/llm_experiments_gemini/rows.jsonl")
CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
_tl = threading.local()


def _resolver():
    if not hasattr(_tl, "r"):
        _tl.c, _tl.r = _client()
    return _tl.c, _tl.r


def _accept(prog, target):
    _, resolver = _resolver()
    ev = evaluate_symbolic(prog, target, resolver)
    return _accepted(oracle_summary(ev["oracles"]))


def _jb_off(p):
    ne = []
    for e in p.edits:
        segs = [dc.replace(s, key_pin="", fingerprint=()) if s.kind == "keyvar" else s
                for s in e.anchor.segs]
        ne.append(dc.replace(e, anchor=dc.replace(e.anchor, segs=segs)))
    return dc.replace(p, edits=ne)


def _rel_off(p):
    return dc.replace(p, edits=[dc.replace(e, review="") for e in p.edits])


def process(r: dict) -> dict:
    base = {"repository": r["repository"], "commit_hash": r["commit_hash"],
            "branch": r["branch"], "file": r["file"], "klass": r["klass"],
            "symbolic": bool(r.get("symbolic_accepted"))}
    key = _safe(r)
    try:
        target = (ART / key / "target_before.yml").read_text()
        if r.get("symbolic_accepted"):
            cl, _ = _resolver()
            c = fetch_case(cl, r["repository"], r["commit_hash"], r["branch"], r["file"], r["idents"])
            prog = compile_case(c)
        else:
            prog = from_wsp((ART / key / "fallback.wsp").read_text())
    except Exception as e:
        return {**base, "status": "recon_fail", "error": str(e)[:120]}
    try:
        jb = _accept(_jb_off(prog), target)
        # relevance lives in compile; the fallback WSP has no review-marked edits,
        # so it is unaffected -> a fallback patch survives by construction.
        rel = True if not base["symbolic"] else _accept(_rel_off(prog), target)
    except Exception as e:
        return {**base, "status": "eval_fail", "error": str(e)[:120]}
    return {**base, "status": "ok", "jb_off_ok": bool(jb), "rel_off_ok": bool(rel)}


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    recon = sum(1 for r in rows if r["status"] != "ok")
    N = 12000
    # full-system accepted = every case here (we only processed combined-accepted)
    full = len(rows)
    jb = sum(1 for r in ok if r.get("jb_off_ok")) + recon          # recon_fail -> assume survives
    rel = sum(1 for r in ok if r.get("rel_off_ok")) + recon
    pct = lambda a: f"{100*a/N:.1f}%"
    out = ["# Table VIII feature ablation (Gemini-3.1-flash-lite, 12k; LLM-free re-apply)\n",
           f"full system accepted (re-checked set): {full}/{N} = {pct(full)}",
           f"  - Constrained job binding REMOVED : {jb}/{N} = {pct(jb)}   (drop {pct(full-jb)})",
           f"  - Edit-relevance filter REMOVED   : {rel}/{N} = {pct(rel)}   (drop {pct(full-rel)})",
           f"  (recon_fail treated as survives: {recon})\n",
           "per-class drop (accepted -> survives without feature):",
           f"{'class':16}{'full':>8}{'job-bind off':>14}{'relevance off':>15}"]
    per = defaultdict(lambda: dict(n=0, jb=0, rel=0))
    for r in ok:
        p = per[r["klass"]]; p["n"] += 1
        p["jb"] += int(bool(r.get("jb_off_ok"))); p["rel"] += int(bool(r.get("rel_off_ok")))
    for k in CLASSES:
        if k not in per:
            continue
        p = per[k]
        out.append(f"{k:16}{p['n']:>8}{p['jb']:>14}{p['rel']:>15}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="output/full/ablation_table")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    rp = out / "rows.jsonl"
    if not args.report_only:
        rows = [json.loads(l) for l in open(ROWS) if l.strip()
                and json.loads(l)["status"] == "ok" and json.loads(l).get("combined_accepted")]
        key = lambda r: (r["repository"], r["commit_hash"], r["branch"], r["file"])
        done = jsonl_already_done(rp, key)
        todo = [r for r in rows if key(r) not in done]
        print(f"accepted cases={len(rows)} done={len(done)} todo={len(todo)} workers={args.workers}", flush=True)
        lock = threading.Lock(); n = [0]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(process, r) for r in todo]
            for fut in as_completed(futs):
                with lock:
                    jsonl_append(rp, fut.result())
                    n[0] += 1
                    if n[0] % 200 == 0 or n[0] == len(todo):
                        print(f"  {n[0]}/{len(todo)}", flush=True)
    rep = report(rp)
    (out / "report.md").write_text(rep)
    print("\n" + rep + f"\n\nrows: {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
