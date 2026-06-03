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
from .verify import check_postconditions

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

    return ok, log


def main() -> int:
    ok, log = run()
    print("\n".join(log))
    print()
    print("SELFCHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
