"""Drive backport_ir end-to-end on REAL gap tickets from output/50k/backport_gaps/gaps.jsonl.

The bundled `backport` subcommand reads compiled programs out of clean_fixes/,
which isn't present in this checkout. This standalone driver reconstructs the
same inputs straight from GitHub (public, no token needed):

  * master  after  = file at the clean-fix commit
  * master  before = file at the clean-fix commit's first parent
  * target         = file at the still-vulnerable release branch's HEAD

then runs the unmodified engine: compile_program -> to_wsp -> apply_program,
and grades the result with the project's own external oracles
(zizmor_local + actionlint) plus the engine self-test (postconditions).

Raw blobs come from raw.githubusercontent.com (CDN, effectively unlimited);
only parent-SHA lookups and pin resolution hit api.github.com (60/hr unauth),
so both are cached in-process.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import os
import urllib.request
import urllib.error

_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # optional; raises unauth 60/hr -> 5000/hr

from backport_ir.compile import compile_program
from backport_ir.apply import apply_program
from backport_ir.wsp import to_wsp
from backport_ir.verify import (
    check_postconditions,
    zizmor_oracle,
    zizmor_oracle_local,
    actionlint_oracle,
)

GAPS = Path("output/50k/backport_gaps/gaps.jsonl")
OUT = Path("demo_out")
UA = {"User-Agent": "workflowfix-demo"}

_parent_cache: dict[tuple[str, str], str | None] = {}
_ref_cache: dict[tuple[str, str], str | None] = {}


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes | None:
    headers = {**UA, "Accept": accept}
    if _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):           # rate-limited: back off once
                time.sleep(2 + attempt * 3)
                continue
            return None
        except Exception:
            time.sleep(1)
    return None


def raw_file(repo: str, sha: str, path: str) -> str | None:
    b = _get(f"https://raw.githubusercontent.com/{repo}/{sha}/{path}",
             accept="text/plain")
    return b.decode("utf-8", "replace") if b is not None else None


def parent_sha(repo: str, sha: str) -> str | None:
    key = (repo, sha)
    if key in _parent_cache:
        return _parent_cache[key]
    b = _get(f"https://api.github.com/repos/{repo}/commits/{sha}")
    out = None
    if b is not None:
        try:
            parents = json.loads(b).get("parents", [])
            out = parents[0]["sha"] if parents else None
        except Exception:
            out = None
    _parent_cache[key] = out
    return out


def make_resolver():
    def resolve(action: str, ref: str) -> str | None:
        key = (action, ref)
        if key in _ref_cache:
            return _ref_cache[key]
        sha = None
        if ref:
            b = _get(f"https://api.github.com/repos/{action}/commits/{ref}")
            if b is not None:
                try:
                    sha = json.loads(b).get("sha")
                except Exception:
                    sha = None
        _ref_cache[key] = sha
        return sha
    return resolve


def select_candidates(limit_per_combo: int = 4) -> list[dict]:
    """Flatten gaps.jsonl into (commit, branch, file) candidates, diverse by rule combo."""
    rows = [json.loads(l) for l in GAPS.read_text().splitlines() if l.strip()]
    # preference: simple, fully-automatable combos first, then the rich triple, then pins.
    pref = [
        ("excessive-permissions",),
        ("artipacked",),
        ("artipacked", "excessive-permissions", "unpinned-uses"),
        ("excessive-permissions", "use-trusted-publishing"),
        ("artipacked", "unpinned-uses"),
        ("unpinned-uses",),
    ]
    pref_rank = {c: i for i, c in enumerate(pref)}
    cands: list[dict] = []
    per_combo: dict[tuple, int] = {}
    # stable order: by preference rank
    def rank(r):
        combo = tuple(sorted(r.get("V_fixed_idents", [])))
        return pref_rank.get(combo, 99)
    for r in sorted(rows, key=rank):
        if r.get("status") != "ok" or not r.get("gap_branches"):
            continue
        combo = tuple(sorted(r.get("V_fixed_idents", [])))
        if combo not in pref_rank:
            continue
        if per_combo.get(combo, 0) >= limit_per_combo:
            continue
        gb = r["gap_branches"][0]
        files = [f for f in gb.get("files", [])
                 if f.get("status") == "ok" and f.get("V_present_idents")]
        if not files:
            continue
        f = files[0]
        per_combo[combo] = per_combo.get(combo, 0) + 1
        cands.append({
            "repository": r["repository"],
            "commit_hash": r["commit_hash"],
            "github_url": f"https://github.com/{r['repository']}/commit/{r['commit_hash']}",
            "file_path": f["file_path"],
            "branch": gb["branch"],
            "branch_head_sha": gb["branch_head_sha"],
            "target_idents": r["V_fixed_idents"],
            "present_idents": f["V_present_idents"],
        })
    return cands


def run_one(c: dict, resolver) -> dict:
    repo, sha, path = c["repository"], c["commit_hash"], c["file_path"]
    rec = {**c}

    after = raw_file(repo, sha, path)
    if after is None:
        return {**rec, "status": "after_absent"}
    p = parent_sha(repo, sha)
    if not p:
        return {**rec, "status": "parent_unknown"}
    before = raw_file(repo, p, path)
    if before is None:
        return {**rec, "status": "before_absent"}
    target = raw_file(repo, c["branch_head_sha"], path)
    if target is None:
        return {**rec, "status": "target_absent"}

    prog = compile_program(
        repository=repo, commit_hash=sha, source_file=path,
        before_text=before, after_text=after,
        target_idents=c["target_idents"], github_url=c["github_url"],
    )
    if not prog.edits:
        return {**rec, "status": "no_edits"}
    wsp = to_wsp(prog)

    res = apply_program(prog, target, resolver=resolver)
    post = check_postconditions(prog, res.patched_text, res)
    z_global = zizmor_oracle(prog, target, res.patched_text)
    z_local = zizmor_oracle_local(prog, target, res.patched_text, res)
    alint = actionlint_oracle(target, res.patched_text)
    accepted = bool(z_local.get("success")) and bool(alint.get("success"))

    key = f"{repo.replace('/', '__')}__{sha[:10]}__{c['branch'].replace('/', '__')}"
    d = OUT / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "source_before.yml").write_text(before)
    (d / "source_after.yml").write_text(after)
    (d / "patch.wsp").write_text(wsp)
    (d / "target_before.yml").write_text(target)
    (d / "target_patched.yml").write_text(res.patched_text)

    report = {
        **rec,
        "status": "done",
        "parent_sha": p,
        "n_edits": len(prog.edits),
        "fully_automatable": prog.is_fully_automatable(),
        "apply_summary": res.summary(),
        "postconditions_ok": post["ok"],
        "oracle": {
            "zizmor_global": z_global,
            "zizmor_local": z_local,
            "actionlint": alint,
        },
        "accepted": accepted,
        "artifact_dir": str(d),
    }
    (d / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    OUT.mkdir(exist_ok=True)
    resolver = make_resolver()
    cands = select_candidates()[:limit]
    print(f"selected {len(cands)} candidates", file=sys.stderr)
    rows = []
    for i, c in enumerate(cands, 1):
        try:
            r = run_one(c, resolver)
        except Exception as e:
            r = {**c, "status": "error", "error": f"{type(e).__name__}: {e}"}
        rows.append(r)
        tag = r.get("status")
        if tag == "done":
            tag = "ACCEPTED" if r.get("accepted") else f"applied/{'auto' if r.get('fully_automatable') else 'review'}"
        print(f"[{i:2d}/{len(cands)}] {tag:18s} {c['repository']} "
              f"({'+'.join(c['target_idents'])}) -> {c['branch']}", file=sys.stderr)
    (OUT / "index.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    n_acc = sum(1 for r in rows if r.get("accepted"))
    n_done = sum(1 for r in rows if r.get("status") == "done")
    print(f"\n{n_done} applied, {n_acc} ACCEPTED (zizmor_local AND actionlint)", file=sys.stderr)


if __name__ == "__main__":
    main()
