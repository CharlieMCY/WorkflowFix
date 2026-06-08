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

## Grammar (EBNF)

```ebnf
program        ::= header  block ( BLANK block )*  review_section?

header         ::= "@@" NL
                   source_line?
                   fixes_line?
                   metavar_decl*
                   "@@" NL
source_line    ::= "# source:" SP repo "@" sha SP filepath NL
fixes_line     ::= "fixes" SP ident ( "," SP? ident )* NL
metavar_decl   ::= "metavariable" SP "job" SP "$" NAME NL

block          ::= anchor NL  edit_line+
anchor         ::= "."  |  seg ( "." seg | list_seg )*
seg            ::= NAME              (* literal mapping key *)
                 | "$" NAME          (* metavariable        *)
list_seg       ::= "[" list_kind "=" ident_value "]"
list_kind      ::= "uses" | "id" | "name" | "run" | "str" | "scalar" | "anon"

edit_line      ::= ( "+" | "-" ) SP key ( ":" SP value )?
key            ::= NAME

review_section ::= review_header NL comment_line*
review_header  ::= "# --- needs review (not auto-applied) ---"
comment_line   ::= "#" TEXT NL

value          ::= "true" | "false" | "null"
                 | NUMBER | DQSTRING | BAREWORD
```

## Lexical tokens

| token | rule |
|---|---|
| `NAME` | a YAML mapping key: characters other than `.` `[` `]` `:` and whitespace (so `persist-credentials`, `pull-requests`, `runs-on` are valid) |
| `ident` | a zizmor rule id, e.g. `unpinned-uses` |
| `NUMBER` | a JSON number |
| `DQSTRING` | a JSON double-quoted string (emitted only when a bareword would be ambiguous) |
| `BAREWORD` | any other scalar text; may contain `@`, spaces, and `:` (see note) |
| `SP` | one or more spaces · `NL` newline · `BLANK` one or more blank lines · `TEXT` arbitrary text to end of line |

**Value parsing** (`_parse_lit`): the text after `: ` is parsed as JSON first
(`true`/`false`/`null`/number/quoted string); on failure it is taken verbatim as
a bareword string. A `key: value` line is split on the **first** `:` only, so a
value may itself contain `:` (e.g. the pin marker).

## Anchors

An anchor is a path to the **parent container** under which the edit's `key`
lives. Segments compose exactly like the IR's `Anchor`:

- mapping-key and metavariable segments are joined with `.`;
- a `list_seg` (`[...]`) is appended directly to the previous segment, **no `.`**.

So `jobs.$JOB.steps[uses=actions/checkout].with` parses as: key `jobs` · metavar
`$JOB` · key `steps` · list `[uses=actions/checkout]` · key `with`.

| segment | meaning |
|---|---|
| `NAME` | literal mapping key, matched exactly |
| `$NAME` | metavariable — matches any key at that position (currently job names) |
| `[uses=V]` / `[id=V]` / `[name=V]` | list element matched by **step identity** |
| `[str=V]` / `[scalar=V]` | list element matched by its value |
| `[run=…]` / `[anon=…]` | weak identity — a match is flagged for review |
| `.` (alone) | the document root |

## Edit lines → operations

WSP borrows Coccinelle's `+`/`-`. The **operation is derived**, per block, by
grouping `+`/`-` lines that share the same `key`:

| lines for a key (within one anchor block) | operation |
|---|---|
| `+ key: v` only | `ensure_present` — key must exist with value `v` |
| `- key` only | `ensure_absent` — key must not exist |
| `- key: old` **and** `+ key: new` | `rewrite_value` — value must become `new` |
| …where `new` is `ACTION@<sha: pin target_ref>` | `rewrite_value` with a **pin** |

Edits are **idempotent ensures**, not imperative hunks: they declare the required
end state, so replaying a patch onto a partially-fixed or drifted target
converges instead of double-applying.

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

Edits that cannot be auto-applied (e.g. inserting a whole new list element, which
v1 does not support) are emitted as a trailing comment block:

```
# --- needs review (not auto-applied) ---
#   on.push.branches.[str=release]  ->  list-element add/remove not yet supported
```

The whole section is comments. `from_wsp` ignores `#` lines in the body, so
review items survive as human guidance without affecting `apply` (they are not
executable edits).

## Parsing conventions

- The text **between the two `@@` lines** is the header. `metavariable` lines are
  rendered for the reader but **ignored** by the parser (a `$JOB` in an anchor is
  self-describing).
- **Blank lines separate blocks.** A block's first non-blank, non-`#`,
  non-`+`/`-` line is its anchor; the indented `+`/`-` lines under it are edits.
- **`#`-comment lines inside the body are ignored** — this is how the review
  section survives a round-trip.
- **Indentation is not significant**: `+`/`-` lines may be indented (parsed with
  leading whitespace stripped).

## Mapping to the IR

| WSP | `IRProgram` / `Edit` field |
|---|---|
| `# source: r@s f` | `repository`, `commit_hash`, `source_file` |
| `fixes a, b` | `target_idents` |
| anchor | `Edit.anchor` (a list of `Seg`: key / keyvar / list) |
| `key` on a `+`/`-` line | `Edit.key` |
| value | `Edit.value` |
| `<sha: pin target_ref>` | `Edit.pin = Pin(action, align="target_ref")` |
| review item | `Edit.review` (non-empty) |

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
```
