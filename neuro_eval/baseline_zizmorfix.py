"""Baseline: run zizmor's own auto-fixer (`zizmor --fix`) directly on each
still-vulnerable target branch file. This answers "why backport at all -- just
run zizmor --fix on the branch". Pinning needs the GitHub API, so a token is
passed per call (rotated over the pool).

Each output is judged by the same four oracles as WorkflowBP:
  security     >=1 targeted finding removed, none introduced (route-level)
  validity     actionlint reports no new finding
  permissions  no untouched job's own permissions block changes
  minimality   every changed leaf belongs to a targeted rule's construct. zizmor's
               output is not written by ruamel, so a leaf counts as changed only
               if it differs from both the raw target and its ruamel round-trip.
zfix_accepted is the four-oracle verdict; zfix_route keeps the security +
validity check alone.

Target files come from the 12k run's saved artifacts (target_before.yml), so no
re-fetch is needed. Resumable via rows.jsonl.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path

from backport_ir.apply import dump, load
from backport_ir.compile import _flatten_text, path_is_security_relevant
from backport_ir.neuro_backport import compile_case, fetch_case, make_client
from backport_ir.verify import permissions_oracle
from neuro_eval.baseline_llm import _judge
from neuro_eval.llm_experiments import _safe, _key
from common.gh_tokens import default_pool
from common.cache import jsonl_already_done, jsonl_append

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
ZIZMOR = shutil.which("zizmor") or str(Path(".venv/bin/zizmor").resolve())
ART = Path("output/full/llm_experiments/artifacts")
_pool = default_pool()
_tls = threading.local()


def _client():
    if not hasattr(_tls, "c"):
        _tls.c = make_client()
    return _tls.c


def _minimal(target: str, patched: str, idents) -> bool:
    """Minimality for output not written by ruamel: a leaf counts as changed
    only if it differs from both the raw target and its ruamel round-trip."""
    try:
        d, y = load(target)
        rt = dump(d, y)
    except Exception:
        rt = target
    raw, rtf, out = _flatten_text(target), _flatten_text(rt), _flatten_text(patched)

    def changed(a, b):
        return (set(a) ^ set(b)) | {k for k in set(a) & set(b) if a[k] != b[k]}
    return all(path_is_security_relevant(p, idents)
               for p in changed(raw, out) & changed(rtf, out))


def process(row: dict, fix_mode: str) -> dict:
    base = {"repository": row["repository"], "commit_hash": row["commit_hash"],
            "branch": row["branch"], "file": row["file"],
            "idents": row["idents"], "klass": row["klass"]}
    tgt = ART / _safe(row) / "target_before.yml"
    if not tgt.exists():
        return {**base, "status": "no_target"}
    target = tgt.read_text()
    tok = _pool.acquire()
    try:
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / "wf.yml"
            wf.write_text(target)
            try:
                subprocess.run([ZIZMOR, f"--fix={fix_mode}", str(wf)],
                               env={**os.environ, "GH_TOKEN": tok, "GITHUB_TOKEN": tok,
                                    "RAYON_NUM_THREADS": "1"},
                               capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                return {**base, "status": "ok", "zfix_changed": False, "zfix_route": False,
                        "zfix_accepted": False, "error": "timeout"}
            fixed = wf.read_text()
    except Exception as e:
        return {**base, "status": "exc", "error": str(e)[:200]}
    changed = fixed != target
    route = bool(changed and _judge(target, fixed, row["idents"]))
    accepted = route
    if route:
        # permissions needs the jobs the fix touched, i.e. the compiled program
        try:
            c = fetch_case(_client(), row["repository"], row["commit_hash"], row["branch"],
                           row["file"], row["idents"])
            if c.fetch_error:
                return {**base, "status": c.fetch_error}
            prog = compile_case(c)
        except Exception as e:
            return {**base, "status": "exc", "error": str(e)[:200]}
        accepted = (bool(permissions_oracle(prog, target, fixed).get("success"))
                    and _minimal(target, fixed, row["idents"]))
    return {**base, "status": "ok", "zfix_changed": changed, "zfix_route": route,
            "zfix_accepted": accepted}


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    per = defaultdict(lambda: dict(n=0, changed=0, route=0, acc=0))
    for r in ok:
        p = per[r["klass"]]; p["n"] += 1
        p["changed"] += int(bool(r.get("zfix_changed")))
        p["route"] += int(bool(r.get("zfix_route")))
        p["acc"] += int(bool(r.get("zfix_accepted")))
    def pct(a, b): return f"{100*a/b:.1f}%" if b else "-"
    out = ["# zizmor --fix baseline, 12k sample\n",
           "route = security + validity only; accepted = all four oracles\n",
           f"{'class':16}{'n':>7}{'changed':>10}{'route':>10}{'accepted':>10}"]
    tot = dict(n=0, changed=0, route=0, acc=0)
    for k in CLASSES:
        if k not in per:
            continue
        p = per[k]
        for f in tot: tot[f] += p[f]
        out.append(f"{k:16}{p['n']:>7}{pct(p['changed'],p['n']):>10}"
                   f"{pct(p['route'],p['n']):>10}{pct(p['acc'],p['n']):>10}")
    out.append(f"{'TOTAL':16}{tot['n']:>7}{pct(tot['changed'],tot['n']):>10}"
               f"{pct(tot['route'],tot['n']):>10}{pct(tot['acc'],tot['n']):>10}")
    from collections import Counter
    out.append("\nstatuses: " + str(dict(Counter(r["status"] for r in rows))))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="output/full/llm_sample.jsonl")
    ap.add_argument("--out-dir", default="output/full/baseline_zizmorfix")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--fix-mode", default="all", choices=["safe", "unsafe-only", "all"])
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "rows.jsonl"

    if not args.report_only:
        sample = [json.loads(l) for l in open(args.sample) if l.strip()]
        done = jsonl_already_done(rows_path, _key)
        todo = [r for r in sample if _key(r) not in done]
        print(f"zizmor={ZIZMOR} fix-mode={args.fix_mode} sample={len(sample)} "
              f"done={len(done)} todo={len(todo)} workers={args.workers}", flush=True)
        lock = threading.Lock(); n = [0]; it = iter(todo)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            inflight = set()
            for _ in range(args.workers):
                r = next(it, None)
                if r is None:
                    break
                inflight.add(ex.submit(process, r, args.fix_mode))
            while inflight:
                ready, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in ready:
                    with lock:
                        jsonl_append(rows_path, fut.result())
                        n[0] += 1
                        if n[0] % 50 == 0 or n[0] == len(todo):
                            print(f"  {n[0]}/{len(todo)}", flush=True)
                while len(inflight) < args.workers:
                    r = next(it, None)
                    if r is None:
                        break
                    inflight.add(ex.submit(process, r, args.fix_mode))

    rep = report(rows_path)
    (out / "report.md").write_text(rep)
    print("\n" + rep)
    print(f"\nrows: {rows_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
