"""Neuro-symbolic backport runner: shared fetch / compile / apply / oracle core.

The symbolic engine (`compile.py` -> `apply.py`) turns ONE master clean-fix
commit into a *target-independent* IRProgram and replays it onto a drifted
release branch. That is exact and sound, but it can only place edits whose
anchors resolve and whose ops are construct-local (the `surgical` class). For
the rest — a renamed/absent job, a fix that lives in adding/removing whole
steps (`restructure`), or edits that land but break the target (`partial`) —
the program legitimately bails to `needs_review`.

This module provides the pieces both the symbolic baseline and the LLM-assisted
loop (`llm_adapt.py`) build on:

  * `make_client()` / `fetch_case()`  — pull master before/after + target file
  * `compile_case()`                  — target-independent IRProgram + class
  * `run_oracles()`                   — the four non-circular acceptance oracles
  * `evaluate_symbolic()`             — apply + oracle, one verdict dict
  * `iter_gap_cases()`                — stream (repo, sha, branch, file, idents)
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .apply import ApplyResult, apply_program
from .compile import compile_program, surgical_class, surgical_review_reasons
from .ir import IRProgram
from .pipeline import make_github_resolver
from .verify import (
    actionlint_oracle,
    minimality_oracle,
    permissions_oracle,
    zizmor_oracle,
    zizmor_oracle_local,
)


# --- GitHub access ----------------------------------------------------------


def _gh_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        tok = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
    except Exception as e:  # pragma: no cover
        raise RuntimeError("no GITHUB_TOKEN and `gh auth token` failed") from e
    os.environ["GITHUB_TOKEN"] = tok
    return tok


def make_client():
    from backport_gaps.github import GitHubClient

    return GitHubClient(_gh_token())


@dataclass
class Case:
    repository: str
    commit_hash: str
    branch: str
    file_path: str
    idents: list[str]
    before_text: str = ""
    after_text: str = ""
    target_text: str = ""
    fetch_error: str = ""


def fetch_case(client, repo: str, sha: str, branch: str, path: str,
               idents: list[str]) -> Case:
    """Fetch master (before@parent, after@sha) and the target (file@branch)."""
    c = Case(repo, sha, branch, path, sorted(idents))
    commit = client.get_commit(repo, sha)
    if not commit or not commit.get("parents"):
        c.fetch_error = "no_commit_or_parent"
        return c
    parent = commit["parents"][0]["sha"]
    after_b = client.get_file_at_ref(repo, path, sha)
    before_b = client.get_file_at_ref(repo, path, parent)
    target_b = client.get_file_at_ref(repo, path, branch)
    if not after_b:
        c.fetch_error = "master_after_absent"
        return c
    if not before_b:
        c.fetch_error = "master_before_absent"
        return c
    if not target_b:
        c.fetch_error = "target_absent"
        return c
    c.before_text = before_b[0].decode("utf-8", "replace")
    c.after_text = after_b[0].decode("utf-8", "replace")
    c.target_text = target_b[0].decode("utf-8", "replace")
    return c


def compile_case(c: Case) -> IRProgram:
    return compile_program(
        c.repository, c.commit_hash, c.file_path,
        c.before_text, c.after_text, c.idents,
        github_url=f"https://github.com/{c.repository}/commit/{c.commit_hash}",
    )


# --- oracles ----------------------------------------------------------------


def run_oracles(program: IRProgram, target_text: str, patched_text: str,
                apply_result: ApplyResult | None = None) -> dict[str, Any]:
    """The acceptance oracles + the headline verdict.

    Two zizmor lenses are recorded:

      * `zizmor_global` — file-level: at least one targeted ident the target
        carried was removed, and NO new finding (any ident) was introduced.
        Symmetric with pattern_miner's clean-fix definition. Needs no
        ApplyResult, so it is computed identically for symbolic output and for
        a full-file LLM rewrite — this is what the symbolic-vs-LLM comparison
        uses.
      * `zizmor_local` — per landed-edit scope (only when an ApplyResult is
        given). Stricter on the symbolic path; recorded for continuity with
        the shipped `pipeline.run_backport`.

    Headline `accepted` = zizmor_global AND actionlint AND permissions AND
    minimality, so it applies uniformly to both engines.
    """
    zg = zizmor_oracle(program, target_text, patched_text)
    al = actionlint_oracle(target_text, patched_text)
    pm = permissions_oracle(program, target_text, patched_text)
    mn = minimality_oracle(program, target_text, patched_text)
    out: dict[str, Any] = {
        "zizmor_global": zg,
        "actionlint": al,
        "permissions": pm,
        "minimality": mn,
        "accepted": (bool(zg.get("success")) and bool(al.get("success"))
                     and bool(pm.get("success")) and bool(mn.get("success"))),
    }
    if apply_result is not None:
        out["zizmor_local"] = zizmor_oracle_local(
            program, target_text, patched_text, apply_result)
    return out


def oracle_summary(oracles: dict[str, Any]) -> dict[str, bool]:
    keys = ("zizmor_global", "zizmor_local", "actionlint", "permissions",
            "minimality")
    return {k: bool(oracles[k].get("success"))
            for k in keys if k in oracles}


def evaluate_symbolic(program: IRProgram, target_text: str,
                      resolver=None) -> dict[str, Any]:
    """Symbolic apply + oracle for one program/target. The baseline verdict."""
    res = apply_program(program, target_text, resolver=resolver)
    oracles = run_oracles(program, target_text, res.patched_text, res)
    return {
        "klass": surgical_class(program),
        "review_reasons": surgical_review_reasons(program),
        "apply_summary": res.summary(),
        "patched_text": res.patched_text,
        "apply_result": res,
        "oracles": oracles,
        "accepted": oracles["accepted"],
    }


# --- gap-case stream --------------------------------------------------------


def iter_gap_cases(gaps_path: Path) -> Iterator[tuple[str, str, str, str, list[str]]]:
    """Yield (repo, sha, branch, file_path, idents) for every gap-branch file
    that still carries a finding master fixed."""
    with open(gaps_path, encoding="utf-8") as fp:
        for line in fp:
            r = json.loads(line)
            if r.get("status") != "ok":
                continue
            repo, sha = r["repository"], r["commit_hash"]
            for gb in (r.get("gap_branches") or []):
                branch = gb["branch"]
                for f in (gb.get("files") or []):
                    if f.get("status") != "ok" or not f.get("V_present_idents"):
                        continue
                    yield repo, sha, branch, f["file_path"], f["V_present_idents"]
