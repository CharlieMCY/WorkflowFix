"""Draft (and optionally submit) GitHub issues proposing a *verified* backport
of a workflow-hardening fix to a still-vulnerable release branch.

Source of truth: the RQ5 results in
  analysis_tools/reports/50k/rq5_by_transplant_rows.jsonl
Only `surgical` + `accepted` pairs are used — i.e. the default branch already
fixed the issue, the release branch still carries it, AND WORKFLOWBP produces a
patch that passes the oracles (zizmor_local + actionlint). Each draft is
re-verified at generation time, so we never propose a patch that doesn't hold.

DEFAULT = dry-run: writes one Markdown draft per issue to issues_drafts/.
NOTHING is posted. Review every draft (and the target repo's CONTRIBUTING /
SECURITY policy) before considering submission — maintainers may not welcome
automated reports.

Submitting (opt-in, guarded):
    .venv/bin/python file_issues.py --limit 10                 # dry-run drafts
    .venv/bin/python file_issues.py --limit 10 --submit --yes  # actually POST

Submission needs a token with Issues:write on the target repos in
GITHUB_WRITE_TOKEN (a public-read PAT cannot create issues). One issue per repo,
hard-capped, with a pause between posts.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from backport_ir.apply import apply_program, dump, load
from backport_ir.compile import _edit_is_relevant, compile_program
from backport_ir.verify import (
    actionlint_oracle, minimality_oracle, permissions_oracle, zizmor_oracle_local,
)
from backport_ir.wsp import to_wsp
from demo_backport import make_resolver, parent_sha, raw_file

ROWS = Path("analysis_tools/reports/50k/rq5_by_transplant_rows.jsonl")
GAPS = Path("output/50k/backport_gaps/gaps.jsonl")
DRAFTS = Path("issues_drafts")
SUBMIT_HARD_CAP = 25                       # safety net even with --submit

# Only propose rules whose fix is a DISCRETE structural edit (permissions block,
# action/image pin, persist-credentials, secrets) — the patch is then provably
# minimal and reviewable. Excluded: template-injection / github-env / bot-
# conditions / dangerous-triggers, whose fix lives in free-form run/env/if/on
# scalars that the engine can over-replay and the minimality oracle can't vet
# (it counts `run`/`env` as security-relevant for those rules).
SAFE_RULES = frozenset({
    "excessive-permissions", "use-trusted-publishing", "unpinned-uses",
    "archived-uses", "unpinned-images", "artipacked", "secrets-inherit",
})

# short, neutral descriptions for the issue body
_RULE_DESC = {
    "unpinned-uses": "actions referenced by mutable tag/branch instead of a pinned commit SHA",
    "archived-uses": "use of an archived/abandoned action",
    "excessive-permissions": "workflow/job granted broader `permissions` than needed",
    "use-trusted-publishing": "publishing without OIDC trusted publishing / least-privilege token",
    "artipacked": "`actions/checkout` left `persist-credentials` enabled (token leaks into artifacts)",
    "unpinned-images": "container `image:` referenced by mutable tag instead of a digest",
    "secrets-inherit": "`secrets: inherit` passes all secrets to a called workflow",
    "template-injection": "untrusted `${{ ... }}` expansion in a `run:`/`env:` context",
    "github-env": "unsafe write to `$GITHUB_ENV` from untrusted input",
    "dangerous-triggers": "risky event trigger (e.g. `pull_request_target`)",
    "bot-conditions": "spoofable bot-actor condition",
}


def _select(limit: int) -> list[dict]:
    rows = [json.loads(l) for l in ROWS.read_text().splitlines() if l.strip()]
    good = [r for r in rows
            if r.get("cls") == "surgical" and r.get("bucket") == "accepted"
            and set(r.get("idents", [])) <= SAFE_RULES]
    # join gaps.jsonl for branch_head_sha + V_present_idents + github url
    meta: dict[tuple, dict] = {}
    for rr in (json.loads(l) for l in GAPS.read_text().splitlines() if l.strip()):
        if rr.get("status") != "ok":
            continue
        for gb in rr.get("gap_branches", []):
            for f in gb.get("files", []):
                if f.get("status") == "ok" and f.get("V_present_idents"):
                    meta[(rr["repository"], rr["commit_hash"], gb["branch"], f["file_path"])] = {
                        "branch_head_sha": gb.get("branch_head_sha", ""),
                        "v_present": f["V_present_idents"],
                    }
    seen, picked = set(), []
    for r in good:
        repo = r["repository"]
        if repo in seen:
            continue
        k = (repo, r["commit_hash"], r["branch"], r["file"])
        m = meta.get(k)
        if not m or not m["branch_head_sha"]:
            continue
        seen.add(repo)
        picked.append({**r, **m})
    return picked                          # all eligible repos; caller stops at N drafts


_PIN = re.compile(r"uses:\s*\S+@([0-9a-f]{40}|sha256:[0-9a-f]{64})\b")
_IMG_PIN = re.compile(r"image:\s*\S+@sha256:[0-9a-f]{64}\b")
_PERM = re.compile(r"^(permissions:|[A-Za-z][\w-]*:\s*(read|write|none|read-all|write-all))\b")
_BOOLKEY = re.compile(r"^(persist-credentials|id-token|contents|packages|pull-requests|"
                      r"issues|actions|deployments|checks|statuses|security-events|"
                      r"pages|discussions|repository-projects|id-token):\s*\S+")


def _added_line_ok(raw: str) -> bool:
    """A `+` diff line is acceptable only if it is an obvious hardening edit:
    a SHA/digest pin, a permissions entry, persist-credentials, or secrets.
    Rejects anything else (e.g. an unrelated `uses:`/`run:` rewrite)."""
    s = raw[1:].strip()
    if s.startswith("- "):            # YAML list dash before the key
        s = s[2:].strip()
    if not s or s.startswith("#"):
        return True
    if _PIN.search(s) or _IMG_PIN.search(s):
        return True
    if s.startswith(("permissions:", "secrets:")) or _PERM.match(s) or _BOOLKEY.match(s):
        return True
    return False


def _diff_clean(diff: str) -> bool:
    """Every ADDED line in the patch must be a recognized hardening shape."""
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            if not _added_line_ok(line):
                return False
    return True


def _build(case: dict, resolver) -> dict:
    """Fetch, compile, apply, RE-VERIFY. ALWAYS returns a dict carrying every
    intermediate artifact plus a `status` ("ok" or a skip reason), so the full
    chain (sources -> WSP -> patched -> oracle verdicts) can be recorded."""
    repo, sha, path = case["repository"], case["commit_hash"], case["file"]
    head, idents = case["branch_head_sha"], case["idents"]
    out = {**case, "fix_commit_url": f"https://github.com/{repo}/commit/{sha}"}

    after = raw_file(repo, sha, path)
    par = parent_sha(repo, sha) if after else None
    before = raw_file(repo, par, path) if par else None
    target = raw_file(repo, head, path)
    if not (after and before and target):
        return {**out, "status": "fetch_fail"}
    out["parent_sha"] = par

    prog = compile_program(repository=repo, commit_hash=sha, source_file=path,
                           before_text=before, after_text=after, target_idents=idents,
                           github_url=out["fix_commit_url"])
    if not prog.edits:
        return {**out, "status": "no_edits"}
    res = apply_program(prog, target, resolver=resolver)

    # Diff against a ruamel round-trip of the ORIGINAL (no edits), so the
    # serializer's whitespace re-rendering cancels out and only the real
    # security edits remain — otherwise the diff is buried in reindentation.
    data, y = load(target)
    baseline = dump(data, y)
    diff = "".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), res.patched_text.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    # all intermediates, kept regardless of outcome (for recording)
    out.update({
        "master_before": before, "master_after": after,
        "target_before": target, "patched": res.patched_text,
        "wsp": to_wsp(prog), "diff": diff,
        "edits": [e.describe() for e in prog.edits if _edit_is_relevant(e, idents)],
        "apply_summary": res.summary(),
    })

    if not diff.strip():
        return {**out, "status": "noop"}    # no real change on this branch

    # Gate on ALL FOUR oracles — crucially incl. minimality + permissions, which
    # reject patches that carry non-security changes (master run-body churn) or
    # touch untouched jobs' permissions.
    oracles = {
        "zizmor_local": bool(zizmor_oracle_local(prog, target, res.patched_text, res).get("success")),
        "actionlint": bool(actionlint_oracle(target, res.patched_text).get("success")),
        "permissions": bool(permissions_oracle(prog, target, res.patched_text).get("success")),
        "minimality": bool(minimality_oracle(prog, target, res.patched_text).get("success")),
    }
    out["oracles"] = oracles
    if not all(oracles.values()):
        return {**out, "status": "oracle_fail:" + ",".join(k for k, v in oracles.items() if not v)}
    # Conservative final guard, independent of the oracles: refuse a patch whose
    # diff has any added line that isn't an obvious hardening edit.
    if not _diff_clean(diff):
        return {**out, "status": "dirty_diff"}
    return {**out, "status": "ok"}


def _render(d: dict) -> tuple[str, str]:
    idents = d["idents"]
    title = f"Backport workflow-hardening fix ({', '.join(idents)}) to `{d['branch']}`"
    findings = "\n".join(f"- `{i}` — {_RULE_DESC.get(i, 'see zizmor docs')}" for i in idents)
    body = f"""\
### Summary
The default branch already hardened `{d['file']}` against the issue(s) below, but \
the release branch **`{d['branch']}`** still carries it. This proposes the same, \
minimal fix for that branch.

### Affected branch / file
- branch: **`{d['branch']}`** (HEAD `{d['branch_head_sha'][:8]}`)
- file: `{d['file']}`

### What's flagged (by [zizmor](https://github.com/woodruffw/zizmor))
{findings}

These are already resolved on the default branch in {d['fix_commit_url']} but the \
fix was not backported to `{d['branch']}`.

### Suggested fix
Concretely:
{chr(10).join(f"- {e}" for e in d['edits']) or "- (see diff)"}

```diff
{d['diff'].rstrip()}
```

*(Whitespace is normalized in the diff above; only the security-relevant lines \
change.)* This patch was checked locally with **zizmor** and **actionlint**: the \
flagged finding(s) are cleared on the affected construct and no new lint or \
security findings are introduced.

---
*This issue was prepared by an automated workflow-hardening analysis and \
double-checked against the two scanners above. Please review before merging — \
happy to send a pull request instead if that's preferred.*
"""
    return title, body


def _submit(repo: str, title: str, body: str, token: str) -> str:
    import requests
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        json={"title": title, "body": body}, timeout=30)
    if r.status_code != 201:
        raise RuntimeError(f"{repo}: {r.status_code} {r.text[:200]}")
    return r.json().get("html_url", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--submit", action="store_true", help="actually POST issues")
    ap.add_argument("--yes", action="store_true", help="required confirmation for --submit")
    args = ap.parse_args()

    DRAFTS.mkdir(exist_ok=True)
    ART = DRAFTS / "artifacts"
    ART.mkdir(exist_ok=True)
    manifest = (DRAFTS / "manifest.jsonl").open("w", encoding="utf-8")
    resolver = make_resolver()
    cases = _select(args.limit)
    print(f"{len(cases)} eligible repos (surgical + accepted); building up to "
          f"{args.limit} drafts...", file=sys.stderr)

    def _safe(d: dict) -> str:
        return f"{d['repository'].replace('/', '__')}__{d['branch'].replace('/', '__')}"

    def _record(d: dict, safe: str) -> None:
        """Persist the full intermediate chain for one case under artifacts/."""
        if "patched" not in d:                 # fetch_fail / no_edits: nothing to dump
            return
        adir = ART / safe
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "master_before.yml").write_text(d["master_before"])
        (adir / "master_after.yml").write_text(d["master_after"])
        (adir / "target_before.yml").write_text(d["target_before"])
        (adir / "patched.yml").write_text(d["patched"])
        (adir / "patch.wsp").write_text(d["wsp"])
        (adir / "patch.diff").write_text(d["diff"])
        meta = {k: d.get(k) for k in (
            "repository", "commit_hash", "branch", "branch_head_sha", "file",
            "idents", "parent_sha", "fix_commit_url", "status", "oracles",
            "edits", "apply_summary")}
        (adir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    drafts = []
    for c in cases:
        if len(drafts) >= args.limit:
            break
        d = _build(c, resolver)
        safe = _safe(d)
        _record(d, safe)
        manifest.write(json.dumps({
            "repository": d["repository"], "branch": d["branch"],
            "commit_hash": d["commit_hash"], "file": d.get("file"),
            "idents": d.get("idents"), "status": d["status"],
            "oracles": d.get("oracles"),
            "artifacts": f"artifacts/{safe}" if "patched" in d else None,
        }, ensure_ascii=False) + "\n")
        manifest.flush()
        if d["status"] != "ok":
            print(f"  skip [{d['status']}]: {d['repository']}", file=sys.stderr)
            continue
        title, body = _render(d)
        (DRAFTS / f"{safe}.md").write_text(f"# {title}\n\n{body}")
        drafts.append((d["repository"], title, body))
        print(f"  [{len(drafts)}] draft -> issues_drafts/{safe}.md "
              f"(+ artifacts/{safe}/)  ({d['repository']})", file=sys.stderr)

    manifest.close()
    print(f"\n{len(drafts)} drafts written to {DRAFTS}/  | full intermediates in "
          f"{ART}/  | audit log: {DRAFTS}/manifest.jsonl", file=sys.stderr)

    if not args.submit:
        print("DRY-RUN: nothing submitted. Review drafts, then re-run with "
              "--submit --yes to post.", file=sys.stderr)
        return 0

    # ---- submission path (guarded) ----
    if not args.yes:
        print("Refusing to submit without --yes.", file=sys.stderr)
        return 1
    token = os.environ.get("GITHUB_WRITE_TOKEN", "").strip()
    if not token:
        print("Set GITHUB_WRITE_TOKEN (a PAT with Issues:write on the targets). "
              "A public-read PAT cannot create issues.", file=sys.stderr)
        return 1
    if len(drafts) > SUBMIT_HARD_CAP:
        print(f"Refusing to submit {len(drafts)} (> hard cap {SUBMIT_HARD_CAP}).",
              file=sys.stderr)
        return 1
    for repo, title, body in drafts:
        try:
            url = _submit(repo, title, body, token)
            print(f"  submitted: {url}", file=sys.stderr)
        except Exception as e:
            print(f"  FAILED {repo}: {e}", file=sys.stderr)
        time.sleep(3)                      # be gentle
    return 0


if __name__ == "__main__":
    sys.exit(main())
