# Workflow Fix Pattern Miner

Mine fix patterns from GitHub Actions workflow histories, with ground truth
provided by [zizmor](https://github.com/woodruffw/zizmor) static analysis.

The pipeline does two things:

1. **Identify clean-fix commits**: across thousands of repos, find the commits
   where zizmor reports a non-empty set of findings disappearing
   (`V_fixed != ∅`) AND no new findings appearing (`V_introduced == ∅`).
2. **Cluster them into patterns**: group those commits by the SET OF ZIZMOR
   RULES they removed (level 1) and by the structural shape of the diff
   (level 2). The result is a catalog of recurring fix-pattern types.

## Inputs

The pipeline expects the [Gigawork dataset (MSR'24)](https://doi.org/10.1145/3643991.3644908)
unpacked at the project root:

```
/your_folder/WorkflowFix/
├── workflows.csv           # 1.5 GB index: one row per (commit, workflow file) pair
└── workflows/              # ~3M content-addressed YAML blobs, named by file_hash
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`zizmor` is installed as a Python package; the binary lives at
`.venv/bin/zizmor`. No GitHub authentication is needed at any stage.

## Pipeline

Five stages, each writes to `output/`:

```
sample        CSV               -> sampled_commits.parquet
diffs         blobs (per commit)-> diffs.jsonl     (added/removed/changed paths)
scan          blobs (unique)    -> scans.jsonl    (zizmor findings per blob)
clean-fixes   diffs + scans     -> clean_fixes/   (per-commit dump w/ before+after)
patterns      diffs + scans     -> patterns.jsonl (pattern catalog)
```

`clean-fixes` and `patterns` both consume `diffs.jsonl + scans.jsonl` but
serve different purposes: `clean-fixes` is a human-inspectable dump, `patterns`
is the programmatic catalog.

## Run

End-to-end:

```bash
.venv/bin/python -m pattern_miner pipeline --n-commits 10000
```

Or stage by stage:

```bash
.venv/bin/python -m pattern_miner sample --n-commits 10000
.venv/bin/python -m pattern_miner diffs
.venv/bin/python -m pattern_miner scan
.venv/bin/python -m pattern_miner clean-fixes
.venv/bin/python -m pattern_miner patterns
```

Cost on a workstation (Linux, 12 cores), 10k sample:

| Stage | Time |
|---|---|
| sample | <30 s (streaming over 1.5 GB CSV) |
| diffs | ~3 min |
| scan | ~2 min (parallel zizmor across ~28k blobs) |
| clean-fixes | <10 s |
| patterns | <5 s |

## Clustering method

`patterns` produces the catalog by a two-level grouping over the clean-fix
commits.

**Level 1 — `frozenset(V_fixed_idents)`**
The set of zizmor rule names that disappeared between before and after. Two
commits share a level-1 bucket iff they removed exactly the same set of rule
types — e.g., `{unpinned-uses}` vs `{unpinned-uses, artipacked,
excessive-permissions}` are different buckets.

**Level 2 — structural template hash**
Within each level-1 bucket, each commit's combined diff is canonicalized into
a sorted list of edit descriptors and hashed. Commits with the same template
hash represent the same DIFF SHAPE applied to different repos. The template
generalizes job names, action SHAs, etc., but keeps action identities (such as
`[uses=actions/checkout]`) so that the same fix shape applied to different
actions remains distinguishable.

For each level-1 bucket the catalog reports:

```json
{
  "fixes": ["artipacked", "excessive-permissions", "unpinned-uses"],
  "n_commits": 57,
  "n_subclusters": 54,
  "structural_subclusters": [
    {
      "template_hash": "...",
      "template_lines": ["~ jobs.<JOB>.steps[uses=...].uses : tag -> sha", ...],
      "n_commits": 2,
      "exemplars": [
        {
          "repository": "...",
          "commit_hash": "...",
          "files": [".github/workflows/...yml"]
        }
      ]
    }
  ]
}
```

The clustering function `cluster_by_commit` in `pattern_miner/cluster.py` is
generic — it accepts any string-list field as the level-1 key, so a downstream
caller can swap `V_fixed_idents` for any other classification.

## Output layout

```
output/
├── sampled_commits.parquet   # input: commit-level sample with file lists
├── diffs.jsonl               # intermediate: one row per (commit, workflow file)
├── scans.jsonl               # intermediate: one row per unique blob hash
├── patterns.jsonl            # FINAL: pattern catalog
└── clean_fixes/              # FINAL: human-inspectable dump
    ├── index.jsonl           # summary index, one row per commit
    └── <repo_safe>__<sha10>/ # one directory per commit
        ├── meta.json
        ├── <flat-file>.before.yml
        └── <flat-file>.after.yml
```

`meta.json` per clean-fix commit:

```json
{
  "repository": "...",
  "commit_hash": "...",
  "github_url": "https://github.com/.../commit/...",
  "V_fixed_count": 86,
  "V_fixed_idents": ["excessive-permissions", "unpinned-uses"],
  "n_files_modified": 10,
  "files": [
    {
      "file_path": ".github/workflows/build.yml",
      "before": ".github__workflows__build.before.yml",
      "after":  ".github__workflows__build.after.yml",
      "scan_status": "ok",
      "V_fixed":      [{"ident": "...", "route": "...", "severity": "..."}],
      "V_introduced": []
    }
  ]
}
```

## Backport-gap audit (separate module)

[`backport_gaps/`](backport_gaps/) is a separate module that consumes
`output/clean_fixes/` (produced by `pattern_miner`) and, for each clean-fix
commit on a project's default branch, audits the project's release-style
branches via the GitHub API to find ones where the fixed zizmor finding is
**still present**. These are the backporting opportunities.

### Setup (GitHub token via `.env`)

```bash
cp .env.example .env
# edit .env and set:
#   GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

A fine-grained PAT with read-only access to public repositories is enough.
`.env` is gitignored; the token is never written elsewhere.

### Run

```bash
# Audit every clean-fix commit (uses output/clean_fixes/*/meta.json as input)
.venv/bin/python -m backport_gaps find-gaps

# Summarize the resulting gaps.jsonl
.venv/bin/python -m backport_gaps summary
```

Smoke test with a small subset first:

```bash
.venv/bin/python -m backport_gaps find-gaps --limit 10
```

### What it does, per clean-fix commit C in repo R on file F

1. Confirm C is on R's default-branch history (else skip).
2. List R's branches, keep only release-style names (`release/*`, `v1.x`,
   `stable`, `maintenance/*`, etc.).
3. For each release branch B:
   - Fetch F at B's HEAD via the GitHub Contents API.
   - If absent, mark inapplicable.
   - Otherwise zizmor-scan it. Match the SET of `ident`s C fixed against the
     set of `ident`s still present on B. (Strict `(ident, route)` matching
     was tried first and discarded — release-branch YAML diverges
     structurally, so routes almost never line up; ident-set matching is the
     defensible criterion.)
4. Classify each branch as `gap` (any ident still present),
   `already_fixed` (none present), or `inapplicable` (no such file).

### Classifying `already_fixed` further — history scan + lag

`already_fixed` conflates two genuinely different cases: the release branch
truly backported C's fix at some commit, versus the release branch never
had the vulnerability in the first place (different code path, file added
later, etc.). To split them, `classify-history` walks the file's history on
each `already_fixed` branch via the GitHub commits API:

- For each historical commit of F on B (up to `MAX_HISTORY_COMMITS=10`),
  fetch the file and zizmor-scan it.
- Find the boundary commit where the master-fixed idents transition from
  present to absent. That commit's date is the backport date; `lag_days =
  backport_date - master_commit_date`.

Final post-hoc refinement by lag sign:

- `lag > +1 day`  → `true_backport`           (release applied master's fix later)
- `|lag| <= 1`    → `same_day_fix`            (likely a merge from master, not a deliberate backport)
- `lag < -1 day`  → `independent_prior_fix`   (release fixed independently before master)

Per-record time-budget (`PER_RECORD_TIMEOUT_S = 8 min`) and history cap keep
worst-case audit time bounded. Anything beyond becomes `inconclusive` /
`timed_out`.

```bash
.venv/bin/python -m backport_gaps classify-history
.venv/bin/python -m backport_gaps history-summary
```

### Output layout

```
output/backport_gaps/
├── gaps.jsonl                  # one row per clean-fix commit (gap audit)
└── gaps_with_history.jsonl     # gap audit + per-branch history classification
```

One row schema (abbreviated):

```json
{
  "repository": "...",
  "commit_hash": "...",
  "default_branch": "main",
  "status": "ok",
  "V_fixed_idents": ["unpinned-uses", "excessive-permissions"],
  "target_files": [".github/workflows/release.yml"],
  "gap_branches": [
    {
      "branch": "release/2.x",
      "branch_head_sha": "...",
      "V_present_idents": ["unpinned-uses"],
      "n_findings_present": 5,
      "files": [{"file_path": "...", "status": "ok",
                 "n_findings_present_from_master_fix": 5}]
    }
  ],
  "already_fixed_branches": [...],
  "inapplicable_branches": [...]
}
```

`status` values: `ok`, `not_on_default`, `repo_error`, `compare_error`,
`branches_error` — non-`ok` rows are kept so failure modes can be quantified.

## Backport-IR patch generation (`backport_ir/`)

[`backport_ir/`](backport_ir/) closes the loop: it frames the fix task as
**backporting** and turns each master clean-fix into an executable,
drift-tolerant patch that can be replayed onto a release branch where the gap is
still open. It is a self-built semantic-patch engine (no ast-grep / Semgrep): a
master commit's `(before -> after)` diff is *compiled* into an `IRProgram` of
anchored, idempotent edits, then *applied* to a structurally drifted target.

**Why an IR, not a line patch.** A release branch's YAML has diverged, so a
textual patch won't apply. The IR locates each edit by YAML *semantic identity*
— the job is a `$JOB` metavariable, steps are matched by `uses=`/`id=`/`name=` —
the same identity keying `extract_diff` already uses, which absorbs reordering
and unrelated insertions.

Three ops, one per diff bucket:

```
ensure_present   key must exist with value     (from diff.added)
ensure_absent    key must not exist            (from diff.removed)
rewrite_value    value must become X / a pin   (from diff.changed)
```

Edits are **idempotent ensures**, not imperative hunks, so replaying onto a
partially-fixed branch converges (the same state-based philosophy as the gap
audit). A `uses:` tag->SHA change compiles to a `pin()` that resolves to the
*target's* current ref — zero version bump, since a security backport must not
smuggle a feature upgrade; an unresolved pin becomes `needs_review`, never a
guess.

**Verification is two-layered, on purpose:**
- *runtime* (cheap, no scanner): structural post-conditions assert each applied
  edit actually landed in the patched text — "did the edit land" is structurally
  decidable, so re-scanning here would be redundant.
- *eval* (the zizmor oracle): rescan `(target-before, patched)` and require the
  target idents to disappear with `V_introduced == ∅` — pattern_miner's
  clean-fix criterion, reused as automated acceptance. This is what engine
  accuracy is reported against; it is NOT run per patch.

### Run

```bash
# offline smoke test — needs no Gigawork data, no GitHub, no zizmor
.venv/bin/python -m backport_ir selfcheck

# compile clean-fix commits into .wsp programs (output/backport_ir/programs/)
.venv/bin/python -m backport_ir compile [--limit N]

# apply one program to a LOCAL target workflow (offline)
.venv/bin/python -m backport_ir apply <program.wsp> <target.yml>

# replay onto every still-open release-branch gap (needs GITHUB_TOKEN; --oracle adds zizmor)
.venv/bin/python -m backport_ir backport [--limit N] [--oracle]
```

`apply`/`backport` need `ruamel.yaml` (format-preserving round-trip, so a PR
carries only the intended diff). The oracle and `backport` reuse
`pattern_miner.scan` and `backport_gaps`' GitHub client respectively.

### Try it on the bundled example

[`backport_ir/examples/`](backport_ir/examples/) has a self-contained clean-fix
you can run by hand — no Gigawork dataset needed. Its master commit hardens one
workflow three ways (`{artipacked, excessive-permissions, unpinned-uses}`), plus
a drifted release-branch file to replay the patch onto:

```bash
# compile the example clean-fix into a .wsp patch
.venv/bin/python -m backport_ir compile \
    --clean-fixes backport_ir/examples/clean_fixes --out /tmp/wsp
cat /tmp/wsp/*.wsp

# apply that patch to the drifted release-branch file
.venv/bin/python -m backport_ir apply \
    "$(ls /tmp/wsp/*.wsp | grep -v index)" \
    backport_ir/examples/target-release-branch.yml --out /tmp/patched
cat /tmp/patched/target-release-branch.yml.patched
```

`persist-credentials: false` and a top-level `permissions:` block get added on
the drifted target (`$JOB` binds the renamed job; `checkout` is matched despite
being the 2nd step; comments are preserved). The pin is left `needs_review`
offline, since it needs a GitHub SHA resolver.

### The patch format is WSP (a semantic patch, not JSON)

A compiled program is stored — and read back — as a Coccinelle/SmPL-style
**Workflow Semantic Patch** (`.wsp`). There is no JSON form of a program:
`compile` writes `.wsp`, `apply`/`backport` read it, so the on-disk artifact is
the very thing a human reviews or hand-edits.

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

`+ k: v` = ensure_present, `- k` = ensure_absent, a `-`/`+` pair = rewrite_value
(a pinned `uses:` shows as `@<sha: pin target_ref>`); edits that still need a
human appear in a trailing `# needs review` block. `wsp.to_wsp`/`from_wsp`
round-trip, so a hand-edited patch parses straight back into an executable IR.
(Run reports and the program index stay JSON — they're diagnostics, not the IR.)

The full grammar — EBNF, lexical tokens, op derivation, IR mapping — is in
[`backport_ir/GRAMMAR.md`](backport_ir/GRAMMAR.md).

### Current limits

- A new *top-level* key (e.g. an added `permissions:` block) is appended at
  end-of-file — semantically correct, but not position-matched to the source.
- Adding/removing a whole list element (e.g. inserting a new step) is deferred;
  such edits compile but are flagged `needs_review`.
- Pin resolution needs a GitHub-backed resolver; offline, pins are
  `needs_review`.

## Analysis scripts

[`analysis/`](analysis/) holds the standalone analysis scripts used to derive
the numbers we report. Each script is independent, reads from `output/`, and
prints to stdout — no figures, no API calls, no side effects. They are the
authoritative source for the numbers; the per-CLI `summary` subcommands are
deliberately compact and don't show all of these.

| Script | What it reports |
|---|---|
| [`01_clean_fix_filter_comparison.py`](analysis/01_clean_fix_filter_comparison.py) | Commit counts under strict vs. three looser definitions of "clean fix" |
| [`02_pattern_distribution.py`](analysis/02_pattern_distribution.py) | All level-1 buckets, `|V_fixed_idents|` size breakdown, sub-cluster uniqueness ratio |
| [`03_match_eval.py`](analysis/03_match_eval.py) | Match outcome on an out-of-sample 2 000-commit pull (`seed=99`): full / level-1 / miss |
| [`04_gap_audit_drill.py`](analysis/04_gap_audit_drill.py) | Per-commit gap distribution, long tail, per-ident gap counts, repo coverage, mirror-commit stats |
| [`05_history_lag_drill.py`](analysis/05_history_lag_drill.py) | Refined backport status (true / same-day / independent / inconclusive / never / timed-out), lag distribution, full TRUE-backport list, 1-3 month cluster drill |
| [`06_zizmor_rule_cross_tab.py`](analysis/06_zizmor_rule_cross_tab.py) | Per-rule commit counts, top rule co-occurrence, rule × backport-status table, rule × gap-presence table |

Run any one:

```bash
.venv/bin/python -m analysis.05_history_lag_drill
```

### Results walkthrough (10k sample)

Every number reported below is reproducible by running the corresponding
analysis script on the current `output/`. Each entry below states:
**what the number means**, **what filter produced it**, **which input file
it comes from**, and **which script prints it**.

---

#### A. Foundational counts — from raw CSV to clean fixes

**`sampled commits = 10 000`**
Deterministic blake2b sample over `workflows.csv` (`pattern_miner sample
--n-commits 10000 --seed 42`). Filter: `git_change_type == 'M'`,
`valid_yaml == 'True'`, `valid_workflow == 'True'`.

**`file-diffs = 14 823` (non-empty: 14 823)**
For each sampled commit, every workflow file modified by it produces one
file-diff (so most commits contribute 1, some contribute several).
Source: `output/diffs.jsonl`.

**`scanned blobs = 28 357` (ok = 28 268, err = 89)**
The union of `file_hash` and `previous_file_hash` across all file-diffs.
Each blob is fed to zizmor via stdin once. Source: `output/scans.jsonl`.

**`commits with V_fixed ≠ ∅ = 1 524`**
A commit's `V_fixed` is the set of `(rule_ident, yaml_route)` pairs that
were in the before scan and gone in the after scan. Any non-empty `V_fixed`
counts here. Script: **`analysis.01_clean_fix_filter_comparison`**.

**`clean-fix commits (strict) = 364`**
`V_fixed ≠ ∅` AND `V_introduced == ∅`. The strict-empty `V_introduced`
filter rejects step-index drift artifacts (a step inserted in the middle
shifts subsequent indices, so the same logical finding appears once in
V_fixed and once in V_introduced — a false "moved"). Script: **01**.

**`loose-A = 1 274`, `loose-B = 1 034`, `loose-C = 1 524`**
Three progressively looser definitions, shown so the precision/recall
trade-off is visible. `loose_B` (every ident's count strictly non-increasing)
is the principled middle ground that recovers ~3× the strict count. Pipeline
currently uses **strict**. Script: **01**.

---

#### B. Pattern catalog — from clean fixes to a matchable library

**`43 level-1 buckets (V_fixed_idents)` / `346 level-2 sub-clusters`**
Two-level clustering over the 364 clean fixes (script: **02**):

- **Level 1 key** = `frozenset(V_fixed_idents)` — the SET of zizmor rule
  names that disappeared. Two commits land in the same bucket iff they
  removed exactly the same set of rule types.
- **Level 2 key** = blake2b hash of a globally-sorted list of edit
  descriptors `<+/-/~>  <generalized_path> = <value_sketch>`. Path
  generalization replaces repo-private list-element keys (`[id=…]`,
  `[name=…]`, `[run=hex]`, string entries) with wildcards but keeps
  `[uses=<action>]` so different actions remain distinguishable. Value
  sketch collapses specific SHAs / tags to `<sha>` / `<tag>` so a version
  bump on different versions is the same sub-cluster.

Source: `output/patterns.jsonl`.

**Structural uniqueness ratio = 0.95**
Defined as `n_subclusters / n_commits` over the whole catalog. Near 1
means nearly every commit has its own unique structural template — proves
structural templates are too repo-specific to be reused as-is for backport
rewrite. Script: **02**.

**|V_fixed_idents| distribution**

| size | #buckets | #commits | % commits |
|---:|---:|---:|---:|
| 1 | 15 | 227 | 62.4% |
| 2 | 12 |  49 | 13.5% |
| 3 |  3 |  59 | 16.2% |
| 4 | 10 |  25 |  6.9% |
| 5 |  3 |   4 |  1.1% |

Zipf — single-rule fixes dominate, but the three 3-rule buckets account
for 16% because StepSecurity bots reliably emit the `{artipacked,
excessive-permissions, unpinned-uses}` triple. Script: **02**.

---

#### C. Match generalization — does the catalog cover unseen commits?

Built an out-of-sample evaluation set: `sample --n-commits 2000 --seed 99`
→ `diffs` → `scan` → 68 clean-fix commits (`output/eval_diffs.jsonl`).

| Outcome | Definition | Count | % |
|---|---|---:|---:|
| `full` | V_fixed_idents in catalog AND structural hash in some sub-cluster | 1 | 1.5% |
| `level-1` | V_fixed_idents in catalog but structural hash unseen | 64 | 94.1% |
| `miss` | V_fixed_idents not in catalog | 3 | 4.4% |

Script: **03**. **Level-1 hit ≈ 96 % means the semantic taxonomy is
near-saturated**; level-2 hit ≈ 1.5 % means structural templates don't
transfer between repos — Stage 2 metavariable parameterization is
required for usable rewrite.

The 3 misses are all rare 4-/5-rule combinations not present in training
(e.g. `{artipacked, unpinned-images, unpinned-uses}`).

---

#### D. Backport-gap audit — which release branches are still vulnerable?

For each of the 364 clean-fix commits on master, query GitHub for the
project's release-style branches and zizmor-scan the same workflow file
on each branch's HEAD. Branch classification (`backport_gaps find-gaps`):

- **gap**: branch has any ident from master's V_fixed_idents still present
- **already_fixed**: branch has none of those idents (file is "clean")
- **inapplicable**: file does not exist on the branch

Source: `output/backport_gaps/gaps.jsonl`. Script: **04**.

**Branch-level counts (2 546 release branches across 364 commits)**

| Bucket | Count | % of branches |
|---|---:|---:|
| `inapplicable` (file absent) | 1 239 | 48.7% |
| `already_fixed` | 472 | 18.5% |
| **`gap` — still vulnerable** | **835** | **32.8%** |

So **33 % of audited (commit, release-branch) pairs are unpatched
backporting opportunities**. Among only the actionable subset
(`gap + already_fixed = 1 307`), gap rate is **63.9 %**.

**Commit-level counts**

- 101 / 364 commits (27.7%) have at least one gap branch.
- Per-commit gap distribution is long-tailed (top: 80 gaps in
  `archesproject/arches`, then 44 in `realm/realm-dotnet`, 37 in
  `datadog/integrations-core`, 33 in `micronaut-projects/micronaut-data`).

**Repo-level coverage (359 unique repos)**

| Category | #repos | % of audited repos |
|---|---:|---:|
| at least one gap | 98 | 27.3% |
| any prior backport (already_fixed) | 46 | 12.8% |
| both gap AND prior backport | 22 | 6.1% |
| any backport activity (either) | 122 | 34.0% |
| **neither (no actionable signal)** | **237** | **66.0%** |

Two-thirds of repos have no actionable signal — meaning the workflow file
either isn't on their release branches at all, or they don't maintain
release-style branches in the first place. Backporting as a problem only
applies to the remaining 34%.

**Mirror commits**: 17 of 347 unique commit hashes appear in ≥2 repos
(4.9% mirror rate). Doesn't bias gap counts much but should be deduped
for any maintainer-distinct claim.

**Most-often-unpatched zizmor rules** (counted per (commit, gap-branch, ident)):

| Rule | #gap occurrences |
|---|---:|
| `unpinned-uses` | 675 |
| `excessive-permissions` | 316 |
| `artipacked` | 285 |
| `archived-uses` | 39 |
| `template-injection` | 29 |
| `unpinned-images` | 16 |
| `cache-poisoning` | 15 |
| `obfuscation` | 4 |
| `superfluous-actions` | 1 |

---

#### E. History classification — of `already_fixed`, which are TRUE backports?

`already_fixed` conflates "release branch backported master's fix" with
"release branch never had the issue". `backport_gaps classify-history`
walks each branch's file history (capped at 10 commits) and locates the
boundary commit where the master-fixed idents transitioned from present
to absent. Then `lag = backport_commit_date - master_commit_date` and we
refine the status by lag sign:

| Refined status | Definition | Count | % of 472 |
|---|---|---:|---:|
| **`true_backport`** | confirmed backport AND `lag > +1 day` | **27** | 5.7% |
| `same_day_fix` | confirmed backport AND `|lag| ≤ 1 day` (likely merge sync) | 118 | 25.0% |
| `independent_prior_fix` | confirmed backport AND `lag < -1 day` (release fixed first) | 6 | 1.3% |
| `inconclusive` | history cap reached without resolution | 256 | 54.2% |
| `never_had_it` | scanned full history; F never present | 17 | 3.6% |
| `timed_out` | per-record 8-min budget exhausted | 48 | 10.2% |

Script: **05**. Source: `output/backport_gaps/gaps_with_history.jsonl`.

**TRUE backport lag distribution (n = 27)**

| Bucket | Count |
|---|---:|
| 1-7 days | 0 |
| 1-4 weeks | 0 |
| **1-3 months** | **17** |
| 3-12 months | 3 |
| **> 1 year** | **7** |

Bimodal: a cluster around ~51 days and a long tail beyond 1 year, with
**nothing in the 1-30 day band**. When release branches do backport, they
take at least ~2 months — there is no "quick patch" cohort.

**Crucial caveat: TRUE backport diversity is much lower than 27**

The 17 cases in the 1-3 month bucket are all from **one repo**
(`hyperledger/besu`) — one master commit propagated to 17 release
branches all at ~51-day lag. The 27 `true_backport` pairs reduce to only
**about 7 distinct master commits / projects**:

| Project | #branches (= #true_backport pairs) |
|---|---:|
| `hyperledger/besu` | 17 |
| `bitwarden/sdk-sm` + `bitwarden/sdk` | 4 |
| `kumahq/kuma` | 1 |
| `kubernetes/minikube` | 1 |
| `stac-utils/rustac` | 1 |
| `assertj/assertj` | 1 |
| `apache/camel-quarkus` | 1 |
| `matplotlib/matplotlib` | 1 |

Both numbers (27 per-branch pairs, 7 per-master-commit events) are legit
but mean different things — paper claims must pick a unit and report it
explicitly.

The 256 `inconclusive` cases are almost all `history_cap_reached`
(MAX_HISTORY_COMMITS = 10). Re-running with cap = 50 on only this subset
should recover an additional 20-40 confirmed backports.

---

#### F. Cross-tabulation — which rules get backported, which get ignored?

`analysis.06_zizmor_rule_cross_tab` cross-tabs the master-fixed rule
against the refined backport status and against gap presence. Script: **06**.

**Per-rule commit count (#commits in `clean_fixes/` whose `V_fixed_idents` includes the rule)**

| Rule | #commits | % of 364 |
|---|---:|---:|
| `unpinned-uses` | 253 | 69.5% |
| `excessive-permissions` | 131 | 36.0% |
| `artipacked` | 104 | 28.6% |
| `template-injection` | 38 | 10.4% |
| `archived-uses` | 34 | 9.3% |
| `cache-poisoning` | 14 | 3.8% |
| `dangerous-triggers` | 11 | 3.0% |
| `use-trusted-publishing` | 9 | 2.5% |
| `superfluous-actions` | 9 | 2.5% |
| others | ≤ 4 each | < 1.5% |

**Per-rule TRUE backport rate** (TRUE / (sum of all refined statuses for that rule))

| Rule | TRUE | total ‘already_fixed' branches | TRUE % |
|---|---:|---:|---:|
| `unpinned-uses` | 23 | 152 | **15.1%** |
| `artipacked` | 1 | 7 | 14.3% |
| `excessive-permissions` | 6 | 62 | 9.7% |
| `template-injection` | 0 | 159 | **0.0%** |
| `cache-poisoning` | 0 | 15 | 0.0% |
| (every other rule) | 0 | … | 0.0% |

**Key finding**: only 3 zizmor rules (`unpinned-uses`,
`excessive-permissions`, `artipacked`) ever get a deliberate backport in
this sample. **`template-injection` (script-injection / RCE class) has 0
TRUE backports despite 159 branches showing as "already_fixed" — those
are merge-sync false positives. release-branch script-injection is
effectively unmaintained.**

**Per-rule gap rate** (gap / (gap + already_fixed))

| Rule | #gap | #already_fixed | gap rate |
|---|---:|---:|---:|
| `artipacked` | 285 | 7 | **97.6%** |
| `excessive-permissions` | 316 | 62 | 83.6% |
| `unpinned-uses` | 675 | 152 | 81.6% |
| `obfuscation` | 4 | 1 | 80.0% |
| `archived-uses` | 39 | 24 | 61.9% |
| `cache-poisoning` | 15 | 15 | 50.0% |
| `unpinned-images` | 16 | 25 | 39.0% |
| `template-injection` | 29 | 159 | **15.4%** |

`artipacked` (no `persist-credentials: false` on checkout) is virtually
never set on release branches — 97.6% gap rate. `template-injection` low
gap rate reflects that most release branches never had the vulnerable
expression in the first place, not that maintainers actively fix them.

## Caveats

- **Step-index drift**: zizmor's finding routes use raw list indices. Inserting
  a step shifts subsequent indices, so a finding can appear as both fixed
  (at the old index) and introduced (at the new index). The clean-fix filter
  (`V_fixed != ∅` AND `V_introduced == ∅`) rejects such cases as a defensive
  measure, which trades some recall for precision.
- **Mirror commits**: ~4% of unique commit hashes appear in multiple repos
  (forks/mirrors). They contribute to multi-repo support of patterns but
  should be deduped when reporting maintainer-distinct counts.
- **Bot provenance**: a non-trivial fraction of clean-fix commits are
  generated by StepSecurity, OSSF Scorecard automation, etc. They are valid
  fixes but should be tagged separately from human-authored ones in any
  downstream evaluation.
