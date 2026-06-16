# WSP v2 — design spec (review draft, pre-implementation)

> Status: **design proposal for author review.** No engine code is changed yet.
> Supersedes nothing until accepted; `GRAMMAR.md` (v1) remains normative for the
> shipped engine. This document was produced by a feature-by-feature design panel
> (6 SmPL features) each put through adversarial review against the **real**
> `demo_out/` fixtures. Every worked example below is derived from an actual
> `(source_before, source_after)` pair, not invented.

## 0. The headline conclusion (read this first)

The brief was "take WSP to the best degree, *full Coccinelle/SmPL parity*." After
designing all six SmPL constructs and adversarially reviewing each against the
corpus, the honest finding is:

**Full SmPL parity is the wrong target for this language.** WSP already
identity-keys every path (`jobs.X.steps[uses=actions/checkout]`), which absorbs
the structural drift that SmPL's C-AST machinery (`...` dots, position
metavariables, disjunction, isomorphisms) was invented to handle. On the 24-case
corpus, those constructs fire **zero** times, or are actively harmful (silent
no-ops, derivations that need ordering data the compiler discards). The entire
verified bug is fixed by a **small, mostly-non-SmPL core**.

So WSP v2 = **the genuinely SmPL-faithful parts WSP already has** (`@@`-head,
`+`/`-` lines, typed metavariables, identity matching) **+ exactly one new
load-bearing idea** (a *constrained, cardinality-bound* job metavariable) **+ a
typed permissions edit and a matching oracle**. Everything else from SmPL is
**evaluated and explicitly rejected with corpus evidence** in §4 — which is the
defensible "we considered the full design space" section, not a maximal grammar.

---

## 0.5 As-built grammar (implemented on branch `wsp-v2-job-binding-fix`)

What actually shipped is the §3.1–3.2 core (constrained job metavariable + explicit
binding cardinality) plus the §3.4 oracle. The heavier §3.3 `% set`/`% delta` typed
permissions op was **not** built: once each job is pinned to its literal key, plain
`+`/`-` leaf edits land only on that one job, so the juspay strip is fixed without a
new permissions sigil (YAGNI). The dots/disjunction/position/isomorphism features of
§4 were not built (evaluated and rejected).

The one new grammar production — a constrained job metavariable declared in the
`@@` head:

```ebnf
metavar_decl  ::= "metavariable" SP "job" SP metavar
                    ( SP "pin"     SP DQSTRING )?          (* literal job key master touched *)
                    ( SP "recover" SP recover_fp )?        (* rename recovery, optional *)
                    ( SP "bind"    SP CARD )?  EOL
recover_fp    ::= fp_term ( "," SP? fp_term )*
fp_term       ::= "uses" "=" NAME                          (* a step uses= unique to this job *)
CARD          ::= "one" | "each"                           (* default emit: one; absent == each *)
metavar       ::= "$" NAME                                 (* $J, $J2, ... — one per touched job *)
```

Anchors reference the metavar (`jobs.$J.permissions`). `pin` is the primary
identity; `recover` is consulted only when the pinned key is absent (job renamed);
`bind one` binds exactly one job — ambiguous recovery is held for review (weak),
never fanned out. A legacy bare `metavariable job $JOB` (no `pin`/`bind`) parses as
`bind each` (v1 fan-out), so old `.wsp` still apply.

**Real compiled output** (`srgn`, one job; `juspay`, three jobs each with a
*different* edit — note the per-metavar `bind one`):

```
@@
# source: alexpovel/srgn@9615cb2e96... .github/workflows/main.yml
fixes excessive-permissions
metavariable job $J pin "release-please" recover uses=google-github-actions/release-please-action bind one
@@

jobs.$J.permissions
+ contents: write
+ pull-requests: write
```

```
@@
# source: juspay/superposition@b87b524bd2... .github/workflows/release.yaml
fixes excessive-permissions, use-trusted-publishing
metavariable job $J  pin "generate-java-packages"   recover uses=actions/setup-java   bind one
metavariable job $J2 pin "generate-js-packages"     recover uses=actions/setup-node   bind one
metavariable job $J3 pin "generate-python-packages" recover uses=actions/setup-python, uses=astral-sh/setup-uv bind one
@@

jobs.$J.permissions
- packages
jobs.$J2.permissions
+ id-token: write
- packages
jobs.$J3.permissions
+ contents: read
+ id-token: write
```

`to_wsp`/`from_wsp` round-trip byte-identical, and a hand-authored declaration
parses and executes (verified: a `pin`ned job renamed on the target is recovered by
its `recover uses=` fingerprint and bound alone).

**Edit-relevance filter (also shipped).** A mined "clean-fix" commit is rarely a
*surgical* security change — it often also bumps a `run:` script, a `with:` arg, or
a matrix entry. v1 compiled a `rewrite_value` for **every** changed leaf and
replayed all of them, regressing a release branch's independently-evolved content
(RQ6: `foundry-rs/foundry` had 3 `run:` bodies reverted to master's older versions;
`dogtagpki/pki` got a stray `with.os` line) while *missing* the actual fix. v2
auto-applies **only** edits attributable to a known security construct of a
*targeted* rule (`_IDENT_CONSTRUCTS` in `compile.py`: `excessive-permissions` →
`permissions`, `artipacked` → `persist-credentials`, `unpinned-uses`/`archived-uses`
→ `uses`, `secrets-inherit` → `secrets`, `template-injection` → `run`/`env`, …);
every other edit is flagged `needs_review` and never lands. Net effect: where master's
fix doesn't transplant, v2 no-ops honestly instead of damaging the target. Verified:
foundry/dogtagpki regressions eliminated (output == `target_before`), eclipse-kura's
permissions fix still `ast_equal`, the 24-case demo oracle stays 24/24.

## 1. Goals and invariants

Every v2 construct must satisfy three invariants (unchanged from v1):

- **(a) Auto-derivable** — computable by `compile.py` from one
  `(before_doc, after_doc, diff)` of the **master** file. WSP is auto-compiled,
  not hand-authored. If a construct is not derivable in the common case, it must
  fall back to a `needs-review` item — never guess.
- **(b) Reviewable** — the `.wsp` a human reads is exactly what the engine runs.
  No hidden semantics beyond documented op-derivation.
- **(c) Round-trippable** — `from_wsp(to_wsp(p))` re-renders identical text and
  byte-identical `apply` output.

A fourth invariant is **added** in v2, because the bug taught us it was missing:

- **(d) Verifiable** — every correctness property a construct claims must be
  checkable by an *external oracle that does not know about backport_ir*.
  The v1 over-grant bug was invisible to `zizmor_local`+`actionlint`, so it could
  not even be regression-tested. v2 adds the permissions oracle (§3.4) precisely
  to restore this.

---

## 2. The verified bug v2 must fix

v1 abstracts **every** job key to a bare, unconstrained `$JOB`
([`compile.py` `_metavar_jobs`](compile.py)), which at apply time binds **every
job** ([`match.py:118`](match.py) — the `keyvar` branch iterates all jobs and
never sets `weak`, asymmetric with the `list` branch at [`match.py:133`](match.py)
which *does* flag multi-hit). A master fix scoped to one (or a few) specific jobs
therefore fans out to all jobs. Verified on real fixtures:

| fixture | master touched | v1 result on target | harm |
|---|---|---|---|
| `alexpovel/srgn` | **1 job** (`release-please`: `<none>` → `{contents:write, pull-requests:write}`) | `permissions:` blocks **2 → 13**, `pull-requests:write` **1 → 14** | write spread to 12 innocent jobs |
| `pikepdf/pikepdf` | **1 job** (`upload_pypi`: `<none>` → `{id-token:write}`) | `id-token:write` **1 → 9**, `environment:release` **1 → 9** | OIDC token + prod env on 8 build jobs |
| `juspay/superposition` | **3 jobs, 3 *different* edits** (see below) | `packages:write` **4 → 0** | GHCR push 403 at runtime |

`juspay` is the crux — the three touched jobs got **non-identical** edits:

```
generate-java-packages:   {contents:read, id-token:write, packages:write} -> {contents:read, id-token:write}   # strip packages only
generate-js-packages:     {contents:read,                 packages:write} -> {contents:read, id-token:write}   # strip packages + add id-token
generate-python-packages: <none>                                          -> {contents:read, id-token:write}   # whole new block
```

v1 collapses all three to one `$JOB` anchor; the merged `- packages` ensure_absent
then fans onto the **4 docker jobs** that legitimately push to GHCR. And the
**oracle reported success=true / accepted=true on all of this** — both
`zizmor_local` and `actionlint` are structurally blind to over-granting (any
explicit `permissions:` block silences `excessive-permissions`; a *grant* never
trips the rule it was told to clear).

**Two independent defects, two fixes:** (i) job identity is thrown away → fix
with literal-key binding + cardinality (§3.1–3.2); (ii) permissions decompose into
scatter-able per-key leaf edits and the result is unverifiable → fix with a typed
permissions block + oracle (§3.3–3.4).

---

## 3. The adopted core (recommended v2)

### 3.1 Typed, constrained metavariables — carried on the `Seg`, not a side table

v1's `metavariable job $JOB` header line is **dead syntax** (parser-ignored). v2
makes the binding identity **executable** and — per adversarial correction —
carries it **on the anchor `Seg` itself**, so `match.py:resolve()` stays a pure
function of `(root, anchor)` (its drift-absorption purity is load-bearing; a
program-level MetaVar table would force a signature/contract change).

`ir.py` change (additive):

```python
@dataclass(frozen=True)
class Seg:
    kind: str                 # 'key' | 'keyvar' | 'list'
    name: str = ""
    var: str = ""             # keyvar binding name, e.g. 'J'
    list_kind: str = ""       # list identity field
    value: str = ""           # list identity value / key name
    # --- v2 additions, only meaningful when kind == 'keyvar' ---
    key_pin: str = ""         # the literal job key the master fix touched (primary identity)
    card: str = "one"         # 'one' (default, fail-closed) | 'each' (opt-in generalization)
    fingerprint: tuple = ()   # optional rename-recovery: tuple of (field, value) step identities
```

Declaration syntax (header) and anchor reference (body):

```ebnf
metavar_decl  ::= "metavariable" SP TYPE SP metavar
                    ( SP "pin" SP DQSTRING )?               (* literal job key master touched *)
                    ( SP "recover" SP fingerprint )?        (* optional drift/rename recovery *)
                    SP "bind" SP CARD  EOL
TYPE          ::= "job" | "step"
CARD          ::= "one" | "each"
fingerprint   ::= "steps" list_seg ( SP? "&" SP? "steps" list_seg )*
metavar       ::= "$" NAME
mapping_seg   ::= NAME | metavar                            (* metavar now carries its identity via the decl *)
```

### 3.2 Binding cardinality — the actual bug fix

The verified-bug insight, stated precisely: **a faithful backport binds exactly
the entities the master commit touched, identified by their literal key.** There
is essentially **no legitimately-derivable `bind each`** from a single master
commit — master edited *specific* jobs, not "all jobs." v1's fan-out was always a
bug; it was merely *harmless* for `persist-credentials` (adding it to an extra
checkout is safe) and *harmful* for `permissions` (adding write to an extra job is
not).

Therefore:

- **`bind one` is the default and is what `compile` emits for every faithful
  backport.** Resolution order (fail-closed):
  1. **literal key pin** — `target["jobs"][key_pin]`. The touched job key is always
     known at compile time; this is deterministic and handles the common case
     (target did not rename the job).
  2. **fingerprint recovery** — *only if* the literal key is absent (job renamed):
     keep jobs satisfying every `recover steps[...]` corroborator.
     - exactly one survives → bind it.
     - zero → emit `needs-review` ("job absent on target"); never invent.
     - **>1 → HOLD FOR REVIEW (weak), per the *colliding sites only*** — not the
       whole edit. (Adversarial correction: a twin must not poison a correctly
       bound sibling. `match.py` marks only the ambiguous candidates weak.)
- **`bind each` is an explicit opt-in generalization** (apply to every matching
  entity, even ones master never touched). It is **never auto-emitted for
  job-scoped grants**. Reserved for a future "harden all checkouts" policy mode;
  out of scope for faithful backport.

> **SmPL fidelity note (honest).** Coccinelle's `exists`/`forall` quantify over
> *control-flow paths* ("does a path exist where P holds" / "P on all paths").
> WSP's `bind one`/`each` quantify over *entity bindings* of a metavariable —
> related but **not** the same thing. We deliberately use `bind one`/`each` rather
> than `exists`/`forall` so Coccinelle-literate readers are not misled. This is
> the one place we diverge from SmPL spelling on purpose.

**Compile derivation.** Stop collapsing job keys. Compute the touched-job set
`T = { J : jobs.J.* changed in the diff }` *before* metavar substitution. Emit one
`metavariable job $J pin "J" bind one` per `J ∈ T`, and anchor that job's edits
under `jobs.$J.…`. Each `J`'s edits stay separate (no cross-job dedup), which is
exactly what `juspay` needs.

#### Worked example — `srgn` (1 job, was over-applied to 13)

```
@@
# source: alexpovel/srgn@9615cb2e96... .github/workflows/main.yml
fixes excessive-permissions
metavariable job $J pin "release-please" recover steps[id=release] bind one
@@

jobs.$J.permissions
% set { contents: write, pull-requests: write }
```

Binds `release-please` alone (literal key). If the release branch renamed it,
`recover steps[id=release]` finds it by its release-please-action step; if two
jobs match, held for review — **never** the 13-job fan-out.

#### Worked example — `juspay` (3 jobs, 3 different edits)

```
@@
# source: juspay/superposition@b87b524bd2... .github/workflows/release.yml
fixes excessive-permissions, use-trusted-publishing
metavariable job $J1 pin "generate-java-packages"   recover steps[uses=actions/setup-java]   bind one
metavariable job $J2 pin "generate-js-packages"     recover steps[uses=actions/setup-node]   bind one
metavariable job $J3 pin "generate-python-packages" recover steps[uses=actions/setup-python] bind one
@@

jobs.$J1.permissions
% delta { -packages }

jobs.$J2.permissions
% delta { -packages, +id-token: write }

jobs.$J3.permissions
% set   { contents: read, id-token: write }
```

Each block binds its own literal job. The `-packages` lands only on java/js —
**never** on the docker jobs. (`% set` vs `% delta` defined in §3.3.) Note the
distinct `recover` fingerprints: the discriminating step is each job's
`setup-<lang>` action — the *shared* `actions/checkout` / `configure-aws` would
**not** discriminate and the compiler must not pick them (open issue O1).

### 3.3 Typed permissions — atomic per-job block, **floor-monotone delta**

The fix for defect (ii). A permissions edit is no longer a scatter of per-key
`+`/`-` leaves (which fan and strip independently). It is **one block op per job**,
derived by extending the existing, tested
[`_consolidate_type_changes`](compile.py) consolidation to "any `permissions` map
that changed → one parent-level edit." Two forms:

```ebnf
perm_op    ::= "%" SP ( "set" | "delta" ) SP perm_body
perm_body  ::= "{" SP? ( perm_term ( "," SP? perm_term )* )? SP? "}"
perm_term  ::= ( "+" | "-" )? NAME ( ":" SP level )?     (* +/- only inside % delta *)
level      ::= "read" | "write" | "none"
```

- `% set { … }` — the target job had **no** block (or master replaced it wholesale):
  create exactly this map. Used for `srgn` (`release-please` had none) and
  `juspay` `generate-python-packages`.
- `% delta { +k: lvl, -k }` — reproduce **master's per-scope change** on the
  target's existing block: add/raise the scopes master added, remove the scopes
  master removed, **leave every other scope of the target's block untouched.**
  Used for `juspay` java (`-packages`) and js (`-packages, +id-token: write`).

**Monotonicity = FLOOR only (no ceiling).** The result must include at least the
scopes master ended with for *that* job (never strip below master's grant). There
is **no** upper bound vs `target_before` — an `excessive-permissions` remediation
is *by construction* a widen over a target that lacked the scoped grant (srgn:
`<none>` → write; pikepdf: `<none>` → id-token). The adversarially-killed "ceil
≤ target_before" clamp would reject every legitimate fix. The over-*grant* bug is
already prevented upstream by literal-key binding (§3.2), not by a ceiling.

Why `% delta` over `% set` for java/js: a whole-block `set` to master's after-map
would also strip any *target-only* scope (e.g. an `attestations:write` the release
branch added independently). `delta` reproduces only master's decision, preserving
target-only scopes. This is the faithful-backport semantics.

Shapes **out of scope** for v2 (zero corpus occurrences, adversarially cut):
`read-all`/`write-all` shorthand and `${{ }}` expression-valued permissions →
emit `needs-review`. Shorthand normalization, if ever needed, lives in the
oracle's read-side only (§3.4), never in the grammar.

### 3.4 The permissions oracle — restores invariant (d)

The single most load-bearing deliverable, because **both existing oracles report
success on the broken v1 output** (verified in `juspay` `report.json`:
`landed_paths` = all 11 jobs, `zizmor_local.success = true`,
`actionlint.success = true`, `accepted = true`). New check in `verify.py`:

```
permissions_oracle(program, target_before, patched):
  let touched = { master job keys in program }
  for every job j in patched:
    eff_b = effective_perms(target_before, j)   # fold in GHA root/default override
    eff_a = effective_perms(patched,       j)
    if j not in touched:        require eff_a == eff_b            # no fan-out to untouched jobs
    if j in touched:            require master_floor(j) ⊆ eff_a   # master's grant reproduced
                                require eff_a changed only the scopes master changed
  success = all requirements hold
```

This makes the over-grant (srgn/pikepdf: untouched jobs change → fail) and the
strip (juspay: docker jobs lose `packages` → fail) **finally visible**, and is the
acceptance signal the v2 fix is regression-tested against. `effective_perms` folds
the GHA rule that a job with no block inherits the workflow-level/default token.

---

## 4. Evaluated SmPL features and adoption decisions

All six were designed to full SmPL faithfulness and adversarially reviewed against
the corpus. Verdicts:

| SmPL feature | corpus payoff | decision | why (with evidence) |
|---|---|---|---|
| **typed constrained metavar + cardinality** (§3.1–3.2) | high | **ADOPT** | the entire fix for the verified bug; derivable from the touched-job set |
| **typed permissions block + oracle** (§3.3–3.4) | high | **ADOPT** | only construct that fixes the juspay strip *and* makes it verifiable |
| **`when` guards** (`+k when absent`, `when !=`) | low | **REJECT** | `apply.py` already does every check (`!= edit.value`, `key in cont`, SHA match) idempotently. Lifting into a text DSL adds a parser + Guard type + round-trip risk for **zero** behavior change. Cardinality belongs **once** on the metavar decl, not as a per-edit `when each` suffix. |
| **disjunction `( P1 \| P2 )`** | low | **REJECT** | the list-disjunction trigger ("step identifiable two ways at once") fires in **0 of 4** headline fixtures; every positive example the designer gave was explicitly hypothetical. Job-disjunction conflicts with the cardinality rule and needs the constraints feature anyway. The drift it targets is already absorbed by identity-keying + `recover`. |
| **dots `...` / `<+...+>`** | none | **REJECT** | **0/24** corpus cases are a clean security insert. The only realistic anchor (`[uses=actions/checkout]`) is non-unique in essentially every multi-job file. Derivation needs list *ordering* that `_flatten` deliberately discards. Keep v1's `needs-review` for new-list-element; if harden-runner injection is ever needed, model it as a **named op** (`insert_security_step before=…`, review-by-default), not SmPL dots. |
| **position metavar `@p`** | none | **REJECT** | falsified by fixtures: `@p` binds the exact route set the locality oracle **already reads** (`o.site_paths`), and that oracle already returns success on the fanned-out srgn/juspay output. Naming the route changes no verdict. Over-grant must be caught by the cardinality fix + permissions oracle, not by a label. |
| **isomorphisms** (spelling equivalences) | low | **REJECT as grammar** | the flagship `on:`-listform iso produces a **silent no-op** (normalizes the match tree but `apply` writes the un-normalized ruamel tree → nothing inserted). The compiler only ever diffs master-before vs master-after, so the "stabilize source-vs-target spelling" rationale describes a comparison the engine never does. **Survivors, demoted out of the grammar:** (i) a YAML-1.1 bool value-comparator shared by `apply`+`verify` (kills `false` vs `"false"` churn) — an `apply.py` fix; (ii) a real scalar/list→map promotion for `on:` — an `apply.py` fix. Neither is an iso or a grammar construct. |

**Net:** of six SmPL constructs, **two adopted, four rejected**, with two small
`apply.py`-side survivors rescued from isomorphisms. The rejected four are not
"future work" — they are evaluated-and-declined, because identity-keyed YAML
removes the structural-drift problem they exist to solve.

---

## 5. SmPL fidelity statement

WSP v2 **keeps** from Coccinelle/SmPL: the `@@`-declaration head, `+`/`-` edit
lines, **typed metavariables with identity constraints**, and semantic (not
textual) matching. These are the parts that map cleanly onto "patch by meaning,
not by line."

WSP v2 **deliberately rejects**: control-flow dots `...`, position metavariables,
disjunction, and isomorphisms. Rationale: SmPL operates on C **AST/CFG**, where
position and intervening code are unavoidable and equivalences are rife. WSP
operates on **already-normalized, identity-keyed YAML**, where a step *is* its
`uses=` identity regardless of position — so the machinery SmPL needs to recover
structure is redundant here. WSP also **diverges in spelling** on cardinality
(`bind one`/`each`, not `exists`/`forall`) to avoid implying C-SmPL path-quantifier
semantics it does not have.

This is the defensible position for the paper: *WSP is SmPL-inspired, not
SmPL-complete, and the completeness gap is a deliberate consequence of the YAML
domain — quantified against a corpus.*

---

## 6. v1 → v2 migration

- A legacy `metavariable job $JOB` line parses as `bind each` with no constraint
  (preserves v1 fan-out semantics for already-on-disk `.wsp`), and `to_wsp` emits a
  review note recommending recompilation. No silent behavior change on old files.
- Recompiling any v1 program with the new `compile` yields literal-key-pinned,
  `bind one`, per-job-separated permissions blocks. The `demo_out/` fixtures are the
  migration regression set (srgn/juspay/pikepdf/tfaction before/after are saved).

---

## 7. Implementation surface (for the build phase, after review)

Ordered so each step is independently testable:

1. **`ir.py`** — extend `Seg` (`key_pin`, `card`, `fingerprint`); add a `PermOp`
   (`set`/`delta`) edit payload. (~40 LOC)
2. **`compile.py`** — (a) compute touched-job set without collapsing; emit one
   pinned `bind one` metavar per touched job; (b) extend
   `_consolidate_type_changes` to coalesce any changed `permissions` map into one
   `% set`/`% delta`; (c) derive `recover` fingerprint = a step identity unique
   across sibling jobs (open issue O1 for the no-unique-step case → `needs-review`).
3. **`match.py`** — `keyvar` branch: literal-key first → `recover` → cardinality
   gate; mark **only** ambiguous candidates `weak` (per-candidate, not per-edit).
4. **`apply.py`** — apply `% set`/`% delta` as one block op; the bool comparator +
   `on:` scalar→map promotion survivors from §4.
5. **`verify.py`** — the permissions oracle (§3.4); fold GHA root-override into
   `effective_perms`.
6. **`wsp.py` / this grammar** — render/parse `metavariable … pin … recover …
   bind …`, `% set`/`% delta`; round-trip tests.
7. **`selfcheck.py`** — add srgn (single-job no-fan-out), juspay (3-job, no docker
   strip), pikepdf (no id-token spread) as regression cases.

### Open issues (must resolve during build)

- **O1 — discriminating fingerprint.** When sibling jobs share all step `uses=`
  (twin/matrix jobs), `recover` cannot uniquely identify a renamed job. Resolution:
  fingerprint must pick a step identity *unique across siblings*; if none exists,
  emit `needs-review` on rename. Do **not** ship a fingerprint that over-binds.
- **O2 — matrix legs.** A `strategy.matrix` job is one YAML key; a master fix scoped
  to one leg via `if:` is invisible to a static `(before,after)` diff. v2 binds the
  whole job (correct at YAML granularity) and documents the limit.
- **O3 — stepless reusable-workflow jobs.** `job: {uses: ./x.yml}` has no steps to
  fingerprint; on rename, `recover` is impossible → `needs-review`. Common shape for
  publish/release jobs — name it as a known coverage gap, fail closed.
