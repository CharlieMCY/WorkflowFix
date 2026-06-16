"""Self-contained smoke test: compile -> match -> apply -> post-condition.

Runs entirely on embedded YAML (no Gigawork data, no GitHub, no zizmor), so
`python -m backport_ir selfcheck` proves the offline core works end to end:

  * job name matched as a metavariable ($JOB binds `publish`, not `build`);
  * checkout step matched by `uses=` identity despite drift (it's the 2nd step,
    after an unrelated setup-node);
  * missing containers created (`with:` on the step, top-level `permissions:`);
  * version-aligned pin resolved via an injected fake resolver;
  * idempotent replay (second apply changes nothing);
  * comments / structure preserved (ruamel round-trip).
"""
from __future__ import annotations

from .apply import apply_program
from .compile import compile_program
from .verify import (
    _scope_prefix,
    actionlint_oracle,
    check_postconditions,
    minimality_oracle,
    permissions_oracle,
)

_SHA = "a" * 40

BEFORE = """\
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: make
"""

AFTER = f"""\
name: CI
on: push
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{_SHA}
        with:
          persist-credentials: false
      - run: make
"""

# Drifted release branch: different job name, checkout no longer first, an extra
# step ahead of it, still on @v3, no top-level permissions, no `with:` block.
TARGET = """\
name: Release CI  # keep this comment
on:
  push:
    branches: [release/2.x]
jobs:
  publish:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/setup-node@v4
      - uses: actions/checkout@v3
      - run: make release
"""


def _fake_resolver(action: str, ref: str) -> "str | None":
    return _SHA if action == "actions/checkout" else None


def run() -> tuple[bool, list[str]]:
    log: list[str] = []
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and bool(cond)
        log.append(("  PASS  " if cond else "  FAIL  ") + msg)

    # ---- core scenario: V1+V2 coupled fix on a drifted release branch ----
    prog = compile_program(
        repository="acme/demo",
        commit_hash="deadbeef" * 5,
        source_file=".github/workflows/ci.yml",
        before_text=BEFORE,
        after_text=AFTER,
        target_idents=["artipacked", "excessive-permissions", "unpinned-uses"],
    )
    log.append(f"compiled {len(prog.edits)} edits:")
    for e in prog.edits:
        log.append(f"      {e.describe()}")

    check(any("persist-credentials" in e.describe() for e in prog.edits),
          "compiled ensure_present for persist-credentials (artipacked)")
    check(any(".permissions.contents" in e.describe() or e.key == "contents"
              for e in prog.edits),
          "compiled ensure_present for top-level permissions (excessive-permissions)")
    check(any(e.pin is not None for e in prog.edits),
          "compiled a pin() for the uses tag->sha change (unpinned-uses)")

    res = apply_program(prog, TARGET, resolver=_fake_resolver)
    patched = res.patched_text
    log.append("")
    log.append("patched output:")
    for ln in patched.splitlines():
        log.append("      " + ln)
    log.append("")

    check("persist-credentials: false" in patched, "persist-credentials landed on checkout")
    check("contents: read" in patched, "top-level permissions block created")
    check(f"actions/checkout@{_SHA}" in patched, "checkout pinned to resolved SHA")
    check("actions/setup-node@v4" in patched, "unrelated setup-node left untouched")
    check("# keep this comment" in patched, "comment preserved (ruamel round-trip)")
    check("Release CI" in patched and "release/2.x" in patched, "target structure preserved")
    check(res.fully_applied, f"all edits applied/noop (by_status={res.summary()['by_status']})")

    post = check_postconditions(prog, patched, res)
    check(post["ok"], "structural post-conditions hold on patched text")

    res2 = apply_program(prog, patched, resolver=_fake_resolver)
    check(not res2.changed, "idempotent replay (second apply changes nothing)")

    # ---- regression scenario: scalar -> map type change (secrets-inherit) ----
    # Without type-change consolidation, this collapses to ensure_absent secrets
    # and silently deletes the whole key. See `_consolidate_type_changes`.
    sec_before = (
        "jobs:\n"
        "  deploy:\n"
        "    uses: ./.github/workflows/deploy.yml\n"
        "    secrets: inherit\n"
    )
    sec_after = (
        "jobs:\n"
        "  deploy:\n"
        "    uses: ./.github/workflows/deploy.yml\n"
        "    secrets:\n"
        "      DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"
        "      AWS_KEY: ${{ secrets.AWS_KEY }}\n"
    )
    sec_prog = compile_program(
        repository="acme/secrets-demo",
        commit_hash="c0ffee" * 7 + "ab",
        source_file=".github/workflows/deploy.yml",
        before_text=sec_before,
        after_text=sec_after,
        target_idents=["secrets-inherit"],
    )
    sec_res = apply_program(sec_prog, sec_before)
    sec_patched = sec_res.patched_text
    log.append("")
    log.append("secrets-inherit patched output:")
    for ln in sec_patched.splitlines():
        log.append("      " + ln)
    log.append("")
    check("DEPLOY_TOKEN" in sec_patched,
          "secrets-inherit: explicit secret map present after rewrite")
    check("inherit" not in sec_patched,
          "secrets-inherit: 'inherit' keyword removed")
    check("secrets:" in sec_patched,
          "secrets-inherit: 'secrets:' key survives (not deleted)")

    # ---- regression: v2 job-scoped permissions must NOT fan out (the $JOB bug) ----
    # Master adds a permissions block to ONE job of a multi-job workflow. v1's
    # unconstrained $JOB fanned it onto EVERY job (srgn 2->13; juspay stripped
    # packages from 4 untouched jobs). v2 binds the literal job key, so exactly
    # the touched job changes and the permissions oracle stays green.
    multi_before = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "  release-please:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: googleapis/release-please-action@v4\n"
        "  docker:\n"
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      packages: write\n"
        "    steps:\n"
        "      - uses: docker/build-push-action@v5\n"
    )
    multi_after = multi_before.replace(
        "  release-please:\n    runs-on: ubuntu-latest\n",
        "  release-please:\n    runs-on: ubuntu-latest\n"
        "    permissions:\n      contents: write\n",
    )
    multi_prog = compile_program(
        repository="acme/multi", commit_hash="ab" * 20,
        source_file=".github/workflows/ci.yml",
        before_text=multi_before, after_text=multi_after,
        target_idents=["excessive-permissions"],
    )
    multi_patched = apply_program(multi_prog, multi_before).patched_text
    check(multi_patched.count("permissions:") == 2,
          f"v2 no-fan-out: exactly 2 permissions blocks (docker + release-please), "
          f"got {multi_patched.count('permissions:')}")
    check("packages: write" in multi_patched,
          "v2 no-strip: untouched docker job keeps its packages:write")
    mperm = permissions_oracle(multi_prog, multi_before, multi_patched)
    check(mperm["success"],
          f"v2 permissions oracle green on faithful backport "
          f"(collateral={mperm.get('collateral_perm_changes')})")
    # And the oracle must FAIL the v1-style fan-out (sanity: it actually catches it).
    fanned = multi_before.replace(
        "  build:\n    runs-on: ubuntu-latest\n",
        "  build:\n    runs-on: ubuntu-latest\n"
        "    permissions:\n      contents: write\n",
    )
    check(not permissions_oracle(multi_prog, multi_before, fanned)["success"],
          "v2 permissions oracle FAILS an over-grant to an untouched job")

    # ---- regression: edit-relevance filter (don't replay bundled non-security) ----
    # A clean-fix commit adds persist-credentials (artipacked) AND incidentally
    # rewrites a run: body. v1 replayed BOTH, regressing the target's evolved run
    # script (RQ6 foundry). v2 auto-applies only the security construct; the run
    # change is flagged for review and left as the target's.
    rel_before = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: build\n"
        "        run: make TARGET_VERSION\n"
    )
    rel_after = (
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          persist-credentials: false\n"
        "      - name: build\n"
        "        run: make MASTER_VERSION\n"
    )
    rel_prog = compile_program(
        repository="acme/rel", commit_hash="ee" * 20,
        source_file=".github/workflows/ci.yml",
        before_text=rel_before, after_text=rel_after, target_idents=["artipacked"],
    )
    rel_patched = apply_program(rel_prog, rel_before).patched_text
    check("persist-credentials: false" in rel_patched,
          "filter: security construct (persist-credentials) still auto-applied")
    check("make TARGET_VERSION" in rel_patched and "make MASTER_VERSION" not in rel_patched,
          "filter: bundled non-security run: change NOT replayed (no regression)")

    # ---- minimality oracle: NON-circular check that only security changed ----
    mo_good = minimality_oracle(rel_prog, rel_before, rel_patched)
    check(mo_good["success"],
          f"minimality: clean patch changes only security constructs "
          f"(non_security={mo_good.get('non_security_changes')})")
    # And it must FLAG a non-security run: change (the regression vector).
    rel_bad = rel_before.replace("run: make TARGET_VERSION", "run: make MASTER_VERSION")
    mo_bad = minimality_oracle(rel_prog, rel_before, rel_bad)
    check(not mo_bad["success"],
          "minimality: a non-security run: change IS flagged (catches regression)")

    # ---- unit: _scope_prefix boundaries (steps / services / job / root) ----
    # Regression guard for the spanner-migration-tool failure: an edit on
    # `services.dynamodb_emulator.image` MUST scope to that service, not
    # the whole job, so an unrelated `services.oracle.image` finding on a
    # sibling service can't fail the oracle.
    scope_cases = [
        ("jobs.X.steps[2].run",                  "jobs.X.steps[2]"),
        ("jobs.X.steps[2]",                      "jobs.X.steps[2]"),
        ("jobs.integration-tests.services.dynamodb_emulator.image",
                                                  "jobs.integration-tests.services.dynamodb_emulator"),
        ("jobs.integration-tests.services.oracle.image",
                                                  "jobs.integration-tests.services.oracle"),
        ("jobs.X.permissions.contents",          "jobs.X"),
        ("jobs.X",                               "jobs.X"),
        ("permissions.contents",                 ""),
        ("permissions",                          ""),
    ]
    for route, want in scope_cases:
        got = _scope_prefix(route)
        check(got == want, f"_scope_prefix({route!r}) -> {got!r} (want {want!r})")

    # ---- external oracle: actionlint on the V1+V2 patched output ----
    # The patched workflow from the core scenario must still pass actionlint
    # cleanly (no new lint findings vs. its target_before). If actionlint isn't
    # installed, we report and skip — the engine works fine without it.
    lint = actionlint_oracle(TARGET, patched)
    if lint.get("status") == "ok":
        check(lint["success"],
              f"actionlint: no new findings introduced "
              f"(before={lint['n_before']}, after={lint['n_after']}, "
              f"introduced={len(lint['introduced'])})")
    else:
        log.append(f"  SKIP  actionlint not available ({lint.get('error')})")

    return ok, log


def main() -> int:
    ok, log = run()
    print("\n".join(log))
    print()
    print("SELFCHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
