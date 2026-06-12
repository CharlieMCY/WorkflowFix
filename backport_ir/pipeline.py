"""End-to-end orchestration for the backport-IR pipeline.

Stages (each runnable from cli.py):

  compile   clean_fixes/<commit>/meta.json (+ before/after blobs) -> programs/*.wsp
  apply     program.wsp + a LOCAL target workflow                 -> patched + report
  backport  backport_gaps/gaps.jsonl gap tickets                  -> patches/ (needs GitHub)
  oracle    program + target-before + patched                     -> zizmor verdict (needs zizmor)

`compile` and `apply` are fully offline. `backport` fetches release-branch files
and resolves pins via the GitHub API (reusing backport_gaps' client). The oracle
needs zizmor installed (reuses pattern_miner.scan).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .apply import Resolver, apply_program
from .compile import compile_program
from .config import CLEAN_FIXES_DIR, GAPS_FILE, PATCHES_DIR, PROGRAMS_DIR
from .ir import IRProgram
from .verify import (
    actionlint_oracle,
    check_postconditions,
    zizmor_oracle,
    zizmor_oracle_local,
)
from .wsp import from_wsp, to_wsp


# --- compile ----------------------------------------------------------------


def iter_clean_fix_programs(
    clean_fixes_dir: Path | None = None,
    limit: int | None = None,
) -> Iterator[tuple[str, IRProgram]]:
    """Compile one IRProgram per (commit, fixed workflow file) in clean_fixes/."""
    clean_fixes_dir = clean_fixes_dir or CLEAN_FIXES_DIR
    metas = sorted(clean_fixes_dir.glob("*/meta.json"))
    if limit is not None:
        metas = metas[:limit]
    for mp in metas:
        meta = json.loads(mp.read_text())
        cdir = mp.parent
        for f in meta.get("files", []):
            if not f.get("V_fixed"):
                continue
            try:
                before = (cdir / f["before"]).read_text(encoding="utf-8", errors="replace")
                after = (cdir / f["after"]).read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, KeyError):
                continue
            idents = sorted({x["ident"] for x in f["V_fixed"]})
            prog = compile_program(
                repository=meta["repository"],
                commit_hash=meta["commit_hash"],
                source_file=f["file_path"],
                before_text=before,
                after_text=after,
                target_idents=idents,
                github_url=meta.get("github_url", ""),
            )
            yield cdir.name, prog


def _program_filename(commit_dir: str, prog: IRProgram) -> str:
    return f"{commit_dir}__{prog.source_file.replace('/', '__')}.wsp"


def run_compile(
    clean_fixes_dir: Path | None = None,
    out_dir: Path | None = None,
    limit: int | None = None,
) -> dict:
    out_dir = out_dir or PROGRAMS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    n_prog = n_edits = n_review = 0
    index: list[dict] = []
    for commit_dir, prog in iter_clean_fix_programs(clean_fixes_dir, limit):
        fn = _program_filename(commit_dir, prog)
        (out_dir / fn).write_text(to_wsp(prog))
        n_prog += 1
        n_edits += len(prog.edits)
        if not prog.is_fully_automatable():
            n_review += 1
        index.append({
            "program": fn,
            "repository": prog.repository,
            "commit_hash": prog.commit_hash,
            "source_file": prog.source_file,
            "target_idents": prog.target_idents,
            "n_edits": len(prog.edits),
            "fully_automatable": prog.is_fully_automatable(),
        })
    with (out_dir / "index.jsonl").open("w", encoding="utf-8") as fp:
        for r in index:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"n_programs": n_prog, "n_edits": n_edits,
            "n_need_review": n_review, "out_dir": str(out_dir)}


# --- apply (local, offline) -------------------------------------------------


def load_program(path: Path) -> IRProgram:
    return from_wsp(Path(path).read_text())


def run_apply_local(
    program_path: Path,
    target_path: Path,
    out_dir: Path | None = None,
    resolver: Resolver | None = None,
) -> dict:
    out_dir = out_dir or PATCHES_DIR
    prog = load_program(program_path)
    target_text = Path(target_path).read_text(encoding="utf-8", errors="replace")
    res = apply_program(prog, target_text, resolver=resolver)
    post = check_postconditions(prog, res.patched_text, res)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(target_path).name
    (out_dir / f"{stem}.patched").write_text(res.patched_text)
    report = {
        "program": str(program_path),
        "target": str(target_path),
        "summary": res.summary(),
        "edits": [o.to_dict() for o in res.edits],
        "postconditions": post,
    }
    (out_dir / f"{stem}.report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False))
    return report


# --- backport (network: GitHub) ---------------------------------------------


def make_github_resolver(client) -> Resolver:
    """ref -> SHA via GitHub commits API (a tag/branch ref resolves to its commit)."""
    from backport_gaps.github import GitHubError

    cache: dict[tuple[str, str], "str | None"] = {}

    def resolve(action: str, ref: str) -> "str | None":
        key = (action, ref)
        if key in cache:
            return cache[key]
        sha = None
        if ref:
            try:
                commit = client.get_commit(action, ref)
                sha = commit.get("sha") if commit else None
            except GitHubError:
                sha = None
        cache[key] = sha
        return sha

    return resolve


def run_backport(
    gaps_path: Path | None = None,
    clean_fixes_dir: Path | None = None,
    out_dir: Path | None = None,
    limit: int | None = None,
    oracle: bool = False,
) -> list[dict]:
    """For each gap branch, fetch its file, replay the matching IR, verify."""
    from backport_gaps.config import get_github_token
    from backport_gaps.github import GitHubClient

    gaps_path = gaps_path or GAPS_FILE
    out_dir = out_dir or PATCHES_DIR
    client = GitHubClient(get_github_token())
    resolver = make_github_resolver(client)
    out_dir.mkdir(parents=True, exist_ok=True)

    programs: dict[tuple[str, str, str], IRProgram] = {}
    for _commit_dir, prog in iter_clean_fix_programs(clean_fixes_dir):
        programs[(prog.repository, prog.commit_hash, prog.source_file)] = prog

    with open(gaps_path, encoding="utf-8") as fp:
        records = [json.loads(line) for line in fp]
    if limit is not None:
        records = records[:limit]

    rows: list[dict] = []
    for rec in records:
        if rec.get("status") != "ok" or not rec.get("gap_branches"):
            continue
        repo, sha = rec["repository"], rec["commit_hash"]
        for gb in rec["gap_branches"]:
            branch = gb["branch"]
            for f in gb.get("files", []):
                if f.get("status") != "ok" or not f.get("V_present_idents"):
                    continue
                path = f["file_path"]
                prog = programs.get((repo, sha, path))
                base = {"repository": repo, "commit_hash": sha,
                        "branch": branch, "file": path}
                if prog is None:
                    rows.append({**base, "status": "no_program"})
                    continue
                fetched = client.get_file_at_ref(repo, path, branch)
                if fetched is None:
                    rows.append({**base, "status": "target_absent"})
                    continue
                target_text = fetched[0].decode("utf-8", "replace")
                res = apply_program(prog, target_text, resolver=resolver)
                post = check_postconditions(prog, res.patched_text, res)

                safe = f"{repo.replace('/', '__')}__{sha[:10]}__{branch.replace('/', '__')}"
                d = out_dir / safe
                d.mkdir(parents=True, exist_ok=True)
                flat = path.replace("/", "__")
                (d / f"{flat}.patched").write_text(res.patched_text)

                row = {**base, "status": "patched",
                       "summary": res.summary(),
                       "postconditions_ok": post["ok"],
                       "edits": [o.to_dict() for o in res.edits]}
                if oracle:
                    # Three external checks:
                    #   zizmor_global  - symmetric to pattern_miner's clean-fix
                    #                    criterion (at least one targeted ident
                    #                    reduced anywhere, nothing new). Loose
                    #                    upper bound on the global effect.
                    #   zizmor_local   - per-edit-locality: every landed edit's
                    #                    scope must end up free of its targets,
                    #                    no new findings within those scopes.
                    #                    The honest "did the backport work at
                    #                    the constructs master targeted" check.
                    #   actionlint     - workflow still passes lint (no new
                    #                    lint findings).
                    # The headline `success` is zizmor_local AND actionlint —
                    # the strongest of the three combined.
                    z_global = zizmor_oracle(prog, target_text, res.patched_text)
                    z_local = zizmor_oracle_local(
                        prog, target_text, res.patched_text, res,
                    )
                    a = actionlint_oracle(target_text, res.patched_text)
                    row["oracle"] = {
                        "zizmor_global": z_global,
                        "zizmor_local": z_local,
                        "actionlint": a,
                        "success": bool(z_local.get("success")) and bool(a.get("success")),
                    }
                (d / f"{flat}.report.json").write_text(
                    json.dumps(row, indent=2, ensure_ascii=False))
                rows.append(row)

    with (out_dir / "backport_index.jsonl").open("w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


# --- oracle (single file, needs zizmor + actionlint) ------------------------


def run_oracle(program_path: Path, before_path: Path, patched_path: Path) -> dict:
    """Run both external oracles on one patched file.

    Acceptance requires BOTH to pass: zizmor confirms the security fix
    landed without introducing new findings, AND actionlint introduces no
    new lint findings (the strongest static "workflow still works" proxy).
    """
    prog = load_program(program_path)
    before = Path(before_path).read_text(encoding="utf-8", errors="replace")
    patched = Path(patched_path).read_text(encoding="utf-8", errors="replace")
    z = zizmor_oracle(prog, before, patched)
    a = actionlint_oracle(before, patched)
    return {
        "zizmor": z,
        "actionlint": a,
        "success": bool(z.get("success")) and bool(a.get("success")),
    }
