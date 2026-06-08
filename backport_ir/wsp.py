"""WSP — a Coccinelle/SmPL-flavoured concrete syntax for the backport IR.

This is the IR's single serialization format: `compile` writes `.wsp`,
`apply`/`backport` read `.wsp`. There is no JSON form of a program — WSP is both
the on-disk format and the human-readable one, so a backport can be reviewed or
hand-edited as a semantic patch:

    @@
    # source: github/codeql-action@<sha> .github/workflows/post-release-mergeback.yml
    fixes excessive-permissions
    metavariable job $JOB
    @@

    jobs.$JOB.permissions
    + contents: write
    + pull-requests: write

Borrowing SmPL's two hallmarks: a `@@ ... @@` declaration head and `-`/`+` lines.
Each block is one anchor context (a bare semantic path; `$JOB` is a metavariable,
`[uses=...]` is identity matching) followed by indented edit lines:

    + key: val                       -> ensure_present
    - key                            -> ensure_absent
    - key: old  /  + key: new        -> rewrite_value
    - uses: a@<tag> / + uses: a@<sha: pin target_ref>  -> rewrite_value + Pin

Edits flagged for review (not auto-applicable) are emitted as a trailing comment
block. `to_wsp`/`from_wsp` round-trip on the executable edits: parsing a rendered
program yields an IRProgram that applies identically.
"""
from __future__ import annotations

import json
import re

from .compile import parse_path
from .ir import (
    ENSURE_ABSENT,
    ENSURE_PRESENT,
    REWRITE_VALUE,
    Anchor,
    Edit,
    IRProgram,
    Pin,
    Seg,
)

_PIN_MARK = "<sha: pin target_ref>"
_TAG_MARK = "<tag>"
_SOURCE_RE = re.compile(r"^# source:\s+(\S+)@(\S+)\s+(.+)$")


# --- scalar literals --------------------------------------------------------


def _lit(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (not s) or s != s.strip() or s.lower() in ("true", "false", "null") \
            or any(c in s for c in ":#") or s[0] in "[{'\"":
        return json.dumps(s)
    return s


def _parse_lit(s: str):
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        return s


# --- anchors ----------------------------------------------------------------


def _anchor_str(a: Anchor) -> str:
    return str(a) if a.segs else "."


def _parse_anchor(line: str) -> Anchor:
    line = line.strip()
    if line == ".":
        return Anchor([])
    out: list[Seg] = []
    for s in parse_path(line):
        if s.kind == "key" and s.name.startswith("$"):
            out.append(Seg.keyvar(s.name[1:]))
        else:
            out.append(s)
    return Anchor(out)


# --- render -----------------------------------------------------------------


def _render_edit(e: Edit) -> list[str]:
    if e.op == ENSURE_PRESENT:
        return [f"+ {e.key}: {_lit(e.value)}"]
    if e.op == ENSURE_ABSENT:
        return [f"- {e.key}"]
    if e.pin is not None:
        return [f"- {e.key}: {e.pin.action}@{_TAG_MARK}",
                f"+ {e.key}: {e.pin.action}@{_PIN_MARK}"]
    lines: list[str] = []
    if e.expected_old is not None:
        lines.append(f"- {e.key}: {e.expected_old}")
    lines.append(f"+ {e.key}: {_lit(e.value)}")
    return lines


def to_wsp(prog: IRProgram) -> str:
    head = ["@@"]
    if prog.repository and prog.commit_hash:
        head.append(f"# source: {prog.repository}@{prog.commit_hash} {prog.source_file}")
    if prog.target_idents:
        head.append("fixes " + ", ".join(prog.target_idents))
    seen_vars: list[str] = []
    for e in prog.edits:
        for s in e.anchor.segs:
            if s.kind == "keyvar" and s.var not in seen_vars:
                seen_vars.append(s.var)
    for v in seen_vars:
        head.append(f"metavariable job ${v}")
    head.append("@@")

    auto = [e for e in prog.edits if not e.review]
    review = [e for e in prog.edits if e.review]

    # group auto-applicable edits by anchor (preserve first-seen order)
    groups: dict[str, list[Edit]] = {}
    order: list[str] = []
    for e in auto:
        a = _anchor_str(e.anchor)
        if a not in groups:
            groups[a] = []
            order.append(a)
        groups[a].append(e)

    blocks = []
    for a in order:
        lines = [a]
        for e in groups[a]:
            lines.extend(_render_edit(e))
        blocks.append("\n".join(lines))

    out = "\n".join(head) + "\n\n" + "\n\n".join(blocks) + "\n"

    if review:
        rlines = ["", "# --- needs review (not auto-applied) ---"]
        for e in review:
            rlines.append(f"#   {_anchor_str(e.anchor)}.{e.key}  ->  {e.review}")
        out += "\n".join(rlines) + "\n"
    return out


# --- parse ------------------------------------------------------------------


def from_wsp(text: str) -> IRProgram:
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() != "@@":
        i += 1
    i += 1  # past opening @@

    repository = commit_hash = source_file = ""
    fixes: list[str] = []
    while i < len(lines) and lines[i].strip() != "@@":
        ln = lines[i].strip()
        m = _SOURCE_RE.match(ln)
        if m:
            repository, commit_hash, source_file = m.group(1), m.group(2), m.group(3)
        elif ln.startswith("fixes "):
            fixes = [x.strip() for x in ln[len("fixes "):].split(",") if x.strip()]
        i += 1
    i += 1  # past closing @@

    edits: list[Edit] = []
    cur: Anchor | None = None
    pending: list[tuple[str, str, str]] = []

    def flush() -> None:
        nonlocal pending
        if cur is not None and pending:
            by_key: dict[str, dict] = {}
            korder: list[str] = []
            for sign, key, val in pending:
                if key not in by_key:
                    by_key[key] = {}
                    korder.append(key)
                by_key[key][sign] = val
            for key in korder:
                sv = by_key[key]
                plus, minus = "+" in sv, "-" in sv
                if plus and minus:
                    newv = sv["+"]
                    if _PIN_MARK in newv:
                        action = newv.split("@", 1)[0].strip()
                        edits.append(Edit(REWRITE_VALUE, cur, key, pin=Pin(action=action),
                                          expected_old=sv["-"].strip() or None))
                    else:
                        edits.append(Edit(REWRITE_VALUE, cur, key, value=_parse_lit(newv),
                                          expected_old=sv["-"].strip() or None))
                elif plus:
                    edits.append(Edit(ENSURE_PRESENT, cur, key, value=_parse_lit(sv["+"])))
                else:
                    edits.append(Edit(ENSURE_ABSENT, cur, key))
        pending = []

    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue                       # blank or comment (incl. review notes)
        if raw.lstrip()[:1] in "+-":
            stripped = raw.lstrip()
            sign, rest = stripped[0], stripped[1:].strip()
            if ":" in rest:
                key, val = rest.split(":", 1)
                pending.append((sign, key.strip(), val.strip()))
            else:
                pending.append((sign, rest.strip(), ""))
        else:
            flush()
            cur = _parse_anchor(raw)
    flush()

    return IRProgram(
        repository=repository,
        commit_hash=commit_hash,
        source_file=source_file,
        target_idents=fixes,
        edits=edits,
    )
