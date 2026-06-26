"""Two RQ7 baselines on the full gap set, reported by transplant class.

Standalone (does NOT use analysis_tools). For every still-vulnerable
(fix x branch x file) gap pair it fetches master before/after + target (cached
GitHub client) and tries two naive transfers, then grades each with the same
route-level criterion used for the engine:

  copy-paste        Replay the master before->after textual diff onto the target.
                    Each changed/inserted block is located in the target by its
                    pre-image (or preceding-line anchor); if drift means the
                    pre-image is absent, the hunk cannot land and the baseline
                    fails -- the structural-divergence failure mode.

  dependabot-style  Take only the `uses:` version bumps from the master diff and
                    apply each as a single-dependency edit on the target (bump
                    that action's ref to master's new ref). Permissions, `with:`,
                    and persist-credentials changes are out of model by
                    construction, so coupled fixes cannot be expressed.

Accept (same for both, comparable to the engine's zizmor side): at least one
target_ident finding is removed AND no new finding is introduced (route-level),
AND actionlint reports nothing new. No IR, no artifacts written -- just rows.

Usage:
    .venv/bin/python -m neuro_eval.baselines_cp_dep \
        --gaps output/full/backport_gaps/gaps.jsonl \
        --out-dir output/full/baselines --workers 24
"""
import argparse
import difflib
import json
import os
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Cap the oracle scanners to one core per invocation (see full_backport.py): zizmor
# (rayon) and actionlint (Go) otherwise grab all cores each, oversubscribing the
# box under many concurrent workers.
os.environ.setdefault("RAYON_NUM_THREADS", "1")
os.environ.setdefault("GOMAXPROCS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backport_ir.compile import compile_program, surgical_class  # noqa: E402
from backport_ir.neuro_backport import fetch_case, make_client  # noqa: E402
from backport_ir.verify import actionlint_oracle  # noqa: E402
from common.cache import jsonl_already_done, jsonl_append  # noqa: E402
from pattern_miner.scan import diff_findings, scan_bytes  # noqa: E402

CLASSES = ("surgical", "partial", "restructure", "no_security_edit")
_tls = threading.local()
_USES = re.compile(r"uses:\s*([^\s@#]+)@([^\s#]+)")


def _client():
    if not hasattr(_tls, "client"):
        _tls.client = make_client()
    return _tls.client


def _key(r: dict) -> tuple:
    return (r["repository"], r["commit_hash"], r["branch"], r["file"])


# ---------- transforms ------------------------------------------------------

def copy_paste(before: str, after: str, target: str):
    """Replay the before->after diff onto target. Returns (patched|None, status)."""
    b = before.splitlines(keepends=True)
    a = after.splitlines(keepends=True)
    patched = target
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "equal":
            continue
        old = "".join(b[i1:i2])
        new = "".join(a[j1:j2])
        if tag in ("replace", "delete"):
            if old not in patched:
                return None, "preimage_absent"
            patched = patched.replace(old, new, 1)
        else:  # insert: anchor on the preceding before-line
            if i1 > 0:
                anchor = b[i1 - 1]
                if anchor not in patched:
                    return None, "anchor_absent"
                patched = patched.replace(anchor, anchor + new, 1)
            else:
                patched = new + patched
    if patched == target:
        return None, "no_change"
    return patched, "applied"


def _uses_map(text: str) -> dict:
    m = {}
    for mm in _USES.finditer(text):
        m.setdefault(mm.group(1), mm.group(2))   # first ref seen per action
    return m


def dependabot(before: str, after: str, target: str):
    """Apply only the master diff's uses-version bumps to target."""
    bm, am = _uses_map(before), _uses_map(after)
    ups = {act: am[act] for act in am if act in bm and bm[act] != am[act]}
    if not ups:
        return None, "no_dep_update"
    patched, changed = target, False
    for act, newref in ups.items():
        pat = re.compile(r"(uses:\s*" + re.escape(act) + r")@[^\s#]+")
        patched, n = pat.subn(r"\1@" + newref, patched)
        changed = changed or bool(n)
    if not changed:
        return None, "action_absent_on_target"
    return patched, "applied"


# ---------- grading ---------------------------------------------------------

def _judge(before_scan, patched, target_before, idents) -> bool:
    if patched is None:
        return False
    ps = scan_bytes(patched.encode("utf-8", "replace"))
    if not ps.get("ok"):
        return False
    fixed, introduced = diff_findings(before_scan["findings"], ps["findings"])
    if introduced or not any(f["ident"] in idents for f in fixed):
        return False
    a = actionlint_oracle(target_before, patched)
    return a.get("status") == "ok" and bool(a.get("success"))


# ---------- per-case --------------------------------------------------------

def process(case_tuple) -> dict:
    repo, sha, branch, path, idents = case_tuple
    base = {"repository": repo, "commit_hash": sha, "branch": branch,
            "file": path, "idents": idents}
    cl = _client()
    try:
        c = fetch_case(cl, repo, sha, branch, path, idents)
    except Exception as e:
        return {**base, "status": "fetch_exc", "error": str(e)[:200]}
    if c.fetch_error:
        return {**base, "status": c.fetch_error}
    try:
        klass = surgical_class(compile_program(
            repository=repo, commit_hash=sha, source_file=path,
            before_text=c.before_text, after_text=c.after_text, target_idents=idents))
        bscan = scan_bytes(c.target_text.encode("utf-8", "replace"))
        if not bscan.get("ok"):
            return {**base, "status": "target_scan_error", "klass": klass}
        cp_p, cp_s = copy_paste(c.before_text, c.after_text, c.target_text)
        dep_p, dep_s = dependabot(c.before_text, c.after_text, c.target_text)
        return {**base, "status": "ok", "klass": klass,
                "cp_status": cp_s, "cp_accepted": _judge(bscan, cp_p, c.target_text, idents),
                "dep_status": dep_s, "dep_accepted": _judge(bscan, dep_p, c.target_text, idents)}
    except Exception as e:
        return {**base, "status": "eval_exc", "error": str(e)[:200]}


# ---------- driver ----------------------------------------------------------

def _unique_cases(gaps_path: Path):
    from backport_ir.neuro_backport import iter_gap_cases
    seen, uniq = set(), []
    for cc in iter_gap_cases(gaps_path):
        k = (cc[0], cc[1], cc[2], cc[3])
        if k not in seen:
            seen.add(k)
            uniq.append(cc)
    return uniq


def report(rows_path: Path) -> str:
    rows = [json.loads(l) for l in rows_path.open()] if rows_path.exists() else []
    ok = [r for r in rows if r["status"] == "ok"]
    out = ["# RQ7 baselines on full gap set, by transplant class\n",
           "accept = >=1 target finding removed AND none introduced (route-level) "
           "AND actionlint clean.\n"]
    for name, acc_k, st_k in (("copy-paste", "cp_accepted", "cp_status"),
                              ("dependabot-style", "dep_accepted", "dep_status")):
        per = defaultdict(lambda: [0, 0])
        st = Counter()
        for r in ok:
            per[r["klass"]][0] += 1
            per[r["klass"]][1] += int(r.get(acc_k, False))
            st[r.get(st_k, "?")] += 1
        out.append(f"## {name}")
        out.append(f"{'class':16} {'n':>7} {'accepted':>9} {'rate':>7}")
        tn = ta = 0
        for k in CLASSES:
            if k in per:
                n, a = per[k]
                tn += n
                ta += a
                out.append(f"{k:16} {n:>7} {a:>9} {pct(a, n):>7}")
        out.append(f"{'TOTAL':16} {tn:>7} {ta:>9} {pct(ta, tn):>7}")
        out.append(f"  status: {st.most_common()}\n")
    out.append("overall statuses: " + str(Counter(r["status"] for r in rows).most_common()))
    return "\n".join(out)


def pct(a, b):
    return f"{100*a/b:.1f}%" if b else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", default="output/full/backport_gaps/gaps.jsonl")
    ap.add_argument("--out-dir", default="output/full/baselines")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "baseline_rows.jsonl"

    if not args.report_only:
        cases = _unique_cases(Path(args.gaps))
        if args.limit:
            cases = cases[:args.limit]
        done = jsonl_already_done(rows_path, _key)
        todo = [c for c in cases if (c[0], c[1], c[2], c[3]) not in done]
        print(f"cases={len(cases)} done={len(done)} todo={len(todo)} "
              f"workers={args.workers}", flush=True)
        lock = threading.Lock()
        n = [0]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(process, c) for c in todo]
            for fut in as_completed(futs):
                row = fut.result()
                with lock:
                    jsonl_append(rows_path, row)
                    n[0] += 1
                    if n[0] % 200 == 0 or n[0] == len(todo):
                        print(f"  {n[0]}/{len(todo)}", flush=True)

    rep = report(rows_path)
    (out / "report.md").write_text(rep)
    print("\n" + rep)
    print(f"\nrows: {rows_path}\nreport: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
