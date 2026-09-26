"""Baseline: run zizmor's own auto-fixer (`zizmor --fix`) directly on each
still-vulnerable target branch file, and judge it the same route-level way as the
other baselines (targeted finding(s) removed, none introduced, + actionlint). This
answers "why backport at all -- just run zizmor --fix on the branch". Pinning needs
the GitHub API, so a token is passed per call (rotated over the pool).

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

from neuro_eval.baseline_llm import _judge
from neuro_eval.llm_experiments import _safe, _key
from common.gh_tokens import default_pool
from common.cache import jsonl_already_done, jsonl_append

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
ZIZMOR = shutil.which("zizmor") or str(Path(".venv/bin/zizmor").resolve())
ART = Path("output/full/llm_experiments/artifacts")
_pool = default_pool()


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
                return {**base, "status": "ok", "zfix_changed": False,
                        "zfix_accepted": False, "error": "timeout"}
            fixed = wf.read_text()
    except Exception as e:
        return {**base, "status": "exc", "error": str(e)[:200]}
    changed = fixed != target
    accepted = bool(changed and _judge(target, fixed, row["idents"]))
    return {**base, "status": "ok", "zfix_changed": changed, "zfix_accepted": accepted}


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    per = defaultdict(lambda: dict(n=0, changed=0, acc=0))
    for r in ok:
        p = per[r["klass"]]; p["n"] += 1
        p["changed"] += int(bool(r.get("zfix_changed")))
        p["acc"] += int(bool(r.get("zfix_accepted")))
    def pct(a, b): return f"{100*a/b:.1f}%" if b else "-"
    out = ["# zizmor --fix baseline (route-level accept), 12k sample\n",
           f"{'class':16}{'n':>7}{'changed':>10}{'accepted':>10}"]
    tot = dict(n=0, changed=0, acc=0)
    for k in CLASSES:
        if k not in per:
            continue
        p = per[k]
        for f in tot: tot[f] += p[f]
        out.append(f"{k:16}{p['n']:>7}{pct(p['changed'],p['n']):>10}{pct(p['acc'],p['n']):>10}")
    out.append(f"{'TOTAL':16}{tot['n']:>7}{pct(tot['changed'],tot['n']):>10}{pct(tot['acc'],tot['n']):>10}")
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
