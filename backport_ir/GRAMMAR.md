# WSP — Workflow Semantic Patch grammar

WSP is the Coccinelle/SmPL-flavoured concrete syntax for the `backport_ir` patch
IR. It is the **single on-disk format**: `compile` writes `.wsp`,
`apply`/`backport` read it — there is no JSON form of a program. The same text is
what the engine executes and what a human reviews or hand-edits.

Reference implementation: [`wsp.py`](wsp.py) (`to_wsp` / `from_wsp`). This
document is normative for that implementation.

## At a glance

```
@@
# source: github/codeql-action@<sha> .github/workflows/post-release-mergeback.yml
fixes excessive-permissions
metavariable job $JOB
@@

jobs.$JOB.permissions
+ contents: write
+ pull-requests: write
```

## Concrete syntax (EBNF)

Names below correspond 1-to-1 with the implementation in
[`wsp.py`](wsp.py) and [`ir.py`](ir.py). UPPERCASE = lexical terminal
(table below). Lowercase = syntactic nonterminal. `(* ... *)` is an aside.
Every terminal that appears is defined in §"Lexical tokens"; every
nonterminal has a production.

```ebnf
program         ::=  header  body  review_section?

(* ---- header (between two LINE_AT_AT lines) ---------------------- *)

header          ::=  LINE_AT_AT  header_line*  LINE_AT_AT
header_line     ::=  source_line | fixes_line | metavar_line | OTHER_COMMENT
                                                  (* parser keeps the first three;
                                                     OTHER_COMMENT is silently dropped *)

source_line     ::=  "# source:" SP source_ref SP file_path EOL
source_ref      ::=  repo "@" rev
repo            ::=  NAME "/" NAME                                  (* "org/repo" *)
rev             ::=  HEX40 | PLACEHOLDER                            (* 40-hex SHA, or `<...>` *)
file_path       ::=  WORKFLOW_PATH                                  (* `.github/workflows/x.yml` *)

fixes_line      ::=  "fixes" SP ident ( "," SP? ident )* EOL
ident           ::=  NAME                                           (* a zizmor rule id *)

metavar_line    ::=  "metavariable" SP "job" SP metavar EOL
metavar         ::=  "$" NAME                                       (* binding name, e.g. "$JOB"
                                                                       —— informational; the parser
                                                                       infers metavars from anchors *)

(* ---- body (blocks separated by blank lines) -------------------- *)

body            ::=  block ( BLANK_LINE+ block )*
block           ::=  anchor EOL  ( edit_line EOL )+

anchor          ::=  ROOT_ANCHOR  |  anchor_seg+
ROOT_ANCHOR     ::=  "."                                            (* document root *)
anchor_seg      ::=  first_mapping_seg | dotted_mapping_seg | list_seg

first_mapping_seg   ::=  mapping_seg                                (* only valid as anchor's 1st seg *)
dotted_mapping_seg  ::=  "." mapping_seg                            (* every subsequent mapping seg *)
mapping_seg     ::=  NAME                                           (* literal mapping key (Seg.kind="key") *)
                  |  metavar                                        (*    metavariable (Seg.kind="keyvar") *)
list_seg        ::=  "[" field "=" context "]"                      (* attaches to the previous seg
                                                                        with NO separator (Seg.kind="list") *)
field           ::=  "uses" | "id" | "name" | "run" | "str" | "scalar" | "anon"
                                                                    (* identity kind for the list element *)
context         ::=  BRACKET_TEXT                                   (* matching value; any chars,
                                                                        nested [ ] balanced *)

edit_line       ::=  sign SP NAME ( ":" SP value_text )?
sign            ::=  "+" | "-"
value_text      ::=  SCALAR | JSON_FLOW | PIN_MARKER | TAG_MARKER | BAREWORD
                                                  (* alternatives are tried in order at parse
                                                     time by `_parse_lit`; on disk they are
                                                     simply the chars after ": " up to EOL *)

(* ---- review section (purely informational) --------------------- *)

review_section  ::=  REVIEW_HEADER  ( review_line EOL )*
review_line     ::=  "#" SP+ TEXT_TO_EOL
```

**What is NOT in the syntax (and why).** Two things are *semantic* and live
outside the EBNF on purpose:

1. **Op derivation** (`+ k: v` alone vs. `- k` alone vs. `- k: ... / + k: ...`)
   is computed by grouping a block's `edit_line`s by their NAME field — see
   §"Edit derivation" below. Two syntactically valid edit lines have no fixed
   op until they are grouped.

2. **Pin recognition** is a check on the `value_text` of the `+`-line of a
   `rewrite_value` pair: if `value_text` ends in `PIN_MARKER`, the IR carries
   an `Edit.pin = Pin(action=...)` instead of a literal `Edit.value`.

## Lexical tokens

All terminals appearing in the EBNF above:

| Terminal | Definition |
|---|---|
| `NAME` | One or more characters from the set excluding `.` `[` `]` `:` `,` `=` `$` whitespace and newline. (`actions/checkout`'s constituent parts split on `/` are NAMEs; the slash itself is allowed in mapping keys but org/repo splits it explicitly.) |
| `HEX40` | Exactly 40 lowercase hex characters (a git commit SHA). |
| `PLACEHOLDER` | `"<"` followed by any text not containing `>`, followed by `">"`. Used for `<sha>`, `<tag>`, etc. in placeholders the human can read. |
| `WORKFLOW_PATH` | Any text to end of line; conventionally `.github/workflows/<filename>.yml`. |
| `SCALAR` | A YAML 1.2 scalar literal: `"true"`, `"false"`, `"null"`, `NUMBER`, or `DQSTRING`. |
| `NUMBER` | A JSON number. |
| `DQSTRING` | A JSON double-quoted string. Emitted by `_lit` when a bareword would parse ambiguously (contains `:` or `#`, starts with `[` / `{` / `'` / `"`, has trailing whitespace, etc.). |
| `JSON_FLOW` | A single-line JSON object (`{ ... }`) or array (`[ ... ]`). Used for complex `rewrite_value` payloads (e.g. consolidating `secrets: inherit` → `secrets: {DEPLOY_TOKEN: ...}` into one parent-level edit). JSON flow is valid YAML flow, so a complex value parses identically as YAML in the patched file. |
| `PIN_MARKER` | The literal string `<sha: pin target_ref>`, appended after `ACTION@` on the `+`-line of a uses-pin rewrite_value. |
| `TAG_MARKER` | The literal string `<tag>`, used after `ACTION@` on the `-`-line of a uses-pin rewrite_value as a placeholder for "whatever ref the target currently uses". |
| `BAREWORD` | Any text to EOL that is not parseable as SCALAR / JSON_FLOW / PIN_MARKER / TAG_MARKER. Treated as a literal string by `_parse_lit`. |
| `BRACKET_TEXT` | Any chars until a matching `]` at bracket depth 0 (the parser tracks `[`/`]` depth so identity values may themselves contain `[` `]`). |
| `LINE_AT_AT` | A line containing exactly `@@` after whitespace trim. |
| `BLANK_LINE` | A line containing only whitespace. |
| `OTHER_COMMENT` | A line starting with `#` that is not `source_line` and not `REVIEW_HEADER`. Ignored by the parser. |
| `REVIEW_HEADER` | The literal line `# --- needs review (not auto-applied) ---` followed by EOL. |
| `TEXT_TO_EOL` | Any chars until EOL. |
| `TEXT` | Arbitrary characters. |
| `SP` | One or more spaces. |
| `SP?` | Zero or one space. |
| `EOL` | A single newline. |

**`_parse_lit` precedence on `value_text`.** When `from_wsp` sees the text
after `": "`, it tries — in this order: (a) `json.loads` (catches `SCALAR`
and `JSON_FLOW`); (b) the literal `PIN_MARKER` substring check (turns the
edit into a `Pin`); on failure of both, (c) the text is taken verbatim as a
`BAREWORD`. The key:value split inside an `edit_line` uses the **first**
`:`, so a value may itself contain `:` (e.g. `perm:inherit` or
`<sha: pin target_ref>`).

## Anchors

An `anchor` names the **parent container** under which an `edit_line`'s NAME
key lives. The three `anchor_seg` forms map 1-to-1 with `ir.Seg` kinds:

| Concrete syntax | `ir.Seg` | Apply-time semantics |
|---|---|---|
| `NAME` (as `mapping_seg`) | `Seg(kind="key", name=NAME)` | descend into mapping by literal key |
| `$NAME` (as `mapping_seg`) | `Seg(kind="keyvar", var=NAME)` | bind metavariable to every mapping key at this position (the compiler currently emits only `$JOB`; in paper-level prose this slot is the `job_name` placeholder) |
| `[uses=V]` / `[id=V]` / `[name=V]` (as `list_seg`) | `Seg(kind="list", list_kind=K, value=V)` | descend into list to the element whose `field` (here `K`) equals `context` (here `V`) |
| `[str=V]` / `[scalar=V]` (as `list_seg`) | same, with `field` = "str" / "scalar" | descend into list to a primitive element by literal value |
| `[run=...]` / `[anon=...]` (as `list_seg`) | same, with `field` = "run" / "anon" | weak identity (no cross-branch stability) — match resolves but the whole edit is flagged for human review |
| `.` (the `ROOT_ANCHOR`) | empty `Anchor.segs` list | document root (edit is on a top-level key) |

The composition rule baked into the EBNF (`anchor_seg+` with
`dotted_mapping_seg` introducing a `.` separator and `list_seg` attaching
with no separator) means `jobs.$JOB.steps[uses=actions/checkout].with`
parses as five segments:

```
NAME "jobs"  ·  metavar "$JOB"  ·  NAME "steps"  ·  list [uses=actions/checkout]  ·  NAME "with"
```

## Edit derivation

The three IR ops are derived from the `edit_line`s **within a single block**
by grouping them on their NAME field:

| edit_lines for one NAME in one block | derived `ir.Edit` |
|---|---|
| `+ key: v` only | `ENSURE_PRESENT`, value = `_parse_lit(v)` |
| `- key` only (no value) | `ENSURE_ABSENT` |
| `- key: old` and `+ key: new` | `REWRITE_VALUE`, value = `_parse_lit(new)`, `expected_old` = old text |
| `- key: <action>@<tag>` and `+ key: <action>@<sha: pin target_ref>` | `REWRITE_VALUE` with `pin = Pin(action=<action>)` (literal `value` not stored) |

Edits are **idempotent ensures**, not imperative hunks: they declare the
required end state, so replaying a patch onto a partially-fixed or drifted
target converges instead of double-applying.

## Pin (version-aligned action ref)

A `uses:` tag→SHA change is not a literal rewrite — the SHA must be the
**target's own** ref, resolved at apply time:

```
jobs.$JOB.steps[uses=actions/checkout]
- uses: actions/checkout@<tag>
+ uses: actions/checkout@<sha: pin target_ref>
```

`<tag>` is a placeholder for "whatever ref the target currently uses";
`<sha: pin target_ref>` instructs `apply` to resolve that ref to a commit SHA
(via an injected resolver) and pin it. An unresolved pin becomes a review item,
never a guess.

## Review section

Edits the compiler flagged as not auto-applicable are emitted as a
trailing comment block. Three classes get flagged at compile time:

| Class | Why flagged |
|---|---|
| `list-element add/remove not yet supported` | edit's leaf is itself a list element (e.g. a `[str=release]` step) — the engine can edit *inside* a list element but does not create or remove the element itself |
| `adds a new list element; cannot insert into target's list` | added path's anchor contains a list-identity (`[uses=X]`) that does not exist in the source's before-state — applying would silently fail as "inapplicable" |
| `removes a whole list element; cannot delete steps without leaving a husk` | removed path's anchor contains a list-identity that no longer exists in the source's after-state — naïvely deleting each child key would leave a step with no `uses`/`run`, which `actionlint` rejects |

Rendered as:

```
# --- needs review (not auto-applied) ---
#   on.push.branches.[str=release]  ->  list-element add/remove not yet supported
#   jobs.$JOB.steps[uses=goto-bus-stop/setup-zig].uses  ->  removes a whole list element; ...
```

The whole section is comments. `from_wsp` ignores `#` lines in the body, so
review items survive as human guidance without affecting `apply` (they are not
executable edits). Review-flagged edits never land — the per-edit-locality
oracle gives them neither credit nor blame.

## Parser tolerances

The EBNF above describes what `to_wsp` emits. `from_wsp` is intentionally
slightly more permissive than the strict grammar:

- **Indentation is not significant** on `edit_line`s. `to_wsp` does not
  indent them, but `from_wsp` strips leading whitespace before checking
  for the `+`/`-` sign, so a hand-edited WSP with indented edits parses
  the same.
- **`OTHER_COMMENT` and `BLANK_LINE` may appear anywhere in `body`** and
  are silently dropped. This is what makes the `review_section` survive a
  round-trip even though `from_wsp` does not represent it explicitly — its
  lines are simply comments in the body.
- **Block boundaries are determined by encountering an `anchor`**, not by
  `BLANK_LINE`s. Multiple blank lines, or none at all, between blocks parse
  the same; `to_wsp` always emits exactly one blank line for readability.

## Mapping to the IR

| WSP nonterminal | Field in `ir.IRProgram` / `ir.Edit` |
|---|---|
| `source_line` (`repo`, `rev`, `file_path`) | `IRProgram.repository`, `IRProgram.commit_hash`, `IRProgram.source_file` |
| `fixes_line` (each `ident`) | `IRProgram.target_idents` |
| `metavar_line` | (parser-ignored — informational only) |
| `anchor` (each `anchor_seg`) | `Edit.anchor` = `Anchor(segs=[Seg, ...])`; `mapping_seg NAME` → `Seg.kind="key"`; `mapping_seg metavar` → `Seg.kind="keyvar"`; `list_seg` → `Seg.kind="list"` |
| `edit_line` NAME field | `Edit.key` |
| `value_text` parsed as SCALAR / JSON_FLOW / BAREWORD | `Edit.value` |
| `value_text` containing `PIN_MARKER` | `Edit.pin = Pin(action=..., align="target_ref")` |
| `-`-line of a `REWRITE_VALUE` pair | `Edit.expected_old` (sketch, not enforced at apply) |
| Comment line inside `review_section` | `Edit.review` non-empty (carries the human-readable reason) |

## Round-trip guarantee

For executable edits, `from_wsp(to_wsp(p))` yields a program that re-renders to
identical text and whose `apply` output is byte-identical. (Review items render
as comments and are not parsed back, since they are not executable; they do not
affect `apply`.)

## Relation to Coccinelle SmPL

WSP reuses SmPL's `@@ … @@` declaration head, `-`/`+` lines, and metavariables,
but differs in three ways:

1. a WSP is **compiled automatically** from one `(before, after)` diff, not
   hand-written;
2. its edits are **idempotent ensures** (drift-tolerant), not imperative hunks;
3. it targets **YAML / GitHub Actions semantics** (jobs, steps, `uses=` identity)
   rather than C AST/CFG.

The shared trait — metavariable + identity matching — is *semantic
parameterization*; the novelty here is automating it and giving it backport
execution semantics (idempotent ensure, apply-time pin resolution, scanner
oracle).
