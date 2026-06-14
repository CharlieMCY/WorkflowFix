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

## Dataset tagging (`DATASET_TAG`)

Every output path used by every stage is routed through `common.dataset.output_dir()`,
which honours the `DATASET_TAG` environment variable:

- `DATASET_TAG=50k` → `output/50k/`
- `DATASET_TAG=10k` → `output/10k/`
- (unset)           → `output/` (legacy / scratch)

Analysis reports follow the same convention:
- `DATASET_TAG=50k` → `analysis_tools/reports/50k/`

A shared content-addressed cache (`cache/`) is **independent of the tag**, so
`50k` and `10k` runs share three things keyed by their content, not the
dataset:

| Cache layer | What it stores | Key |
|---|---|---|
| `cache/github/` | file bytes at a ref + blob SHA | `(repo, ref, path)` |
| `cache/commit/` | full commit JSON from GitHub | `(repo, sha)` (immutable, never expires) |
| `cache/llm/`    | LLM completion text + token counts | sha256(model ‖ system ‖ user) |

Branch-snapshot queries (branch list, "is SHA in branch HEAD's history?",
"commits touching path on branch") are **not** cached — their answer
depends on whichever HEAD a branch points at right now, so caching would
silently serve stale results into paper claims. Both `output/` and
`cache/` are gitignored.

Run all examples below with the tag set explicitly, e.g. `DATASET_TAG=50k …`.
Without a tag the pipeline writes to the legacy top-level `output/`, which is
empty in a clean checkout.

## Pipeline

Five stages, each writes to `output/<DATASET_TAG>/`:

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

End-to-end (sample → diffs → scan → clean-fixes → patterns):

```bash
DATASET_TAG=50k .venv/bin/python -m pattern_miner pipeline --n-commits 50000
```

Or stage by stage:

```bash
export DATASET_TAG=50k
.venv/bin/python -m pattern_miner sample --n-commits 50000
.venv/bin/python -m pattern_miner diffs
.venv/bin/python -m pattern_miner scan
.venv/bin/python -m pattern_miner clean-fixes
.venv/bin/python -m pattern_miner patterns
```

Cost on a workstation (Linux, 12 cores):

| Stage | 10k | 50k |
|---|---|---|
| sample | <30 s | ~1 min (streaming over 1.5 GB CSV) |
| diffs | ~3 min | ~12 min |
| scan | ~2 min | ~30 min (parallel zizmor; cached blobs skipped) |
| clean-fixes | <10 s | ~30 s |
| patterns | <5 s | ~10 s |

Sampling is deterministic in `(seed, commit_hash)`, so larger `--n-commits`
is a **strict superset** of smaller ones with the same seed — running 50k
after 10k reuses every already-scanned blob.

### Full pipeline (all stages, in order)

The complete chain to reproduce every number in the **Results walkthrough**
below. Set `DATASET_TAG` once at the top of the shell and all stages route
to `output/$DATASET_TAG/`:

```bash
export DATASET_TAG=50k

# 1. Local mining (no GitHub) — ~45 min for 50k
.venv/bin/python -m pattern_miner pipeline --n-commits 50000

# 2. (For analysis 03) Build an out-of-sample evaluation set
.venv/bin/python -m pattern_miner sample --n-commits 2000 --seed 99 \
    --out output/$DATASET_TAG/eval_sampled.parquet
.venv/bin/python -m pattern_miner diffs \
    --sample output/$DATASET_TAG/eval_sampled.parquet \
    --out output/$DATASET_TAG/eval_diffs.jsonl
.venv/bin/python -m pattern_miner scan --diffs output/$DATASET_TAG/eval_diffs.jsonl

# 3. Backport-gap audit + history classification (needs GITHUB_TOKEN, see below) — ~hours
.venv/bin/python -m backport_gaps find-gaps              # ~3 h on 50k
.venv/bin/python -m backport_gaps classify-history       # ~9 h on 50k

# 4. Reports
.venv/bin/python -m backport_gaps summary
.venv/bin/python -m backport_gaps history-summary

# 5. Standalone analysis scripts (see also "Analysis scripts" section)
for n in 01_clean_fix_filter_comparison \
         02_pattern_distribution \
         03_match_eval \
         04_gap_audit_drill \
         05_history_lag_drill \
         06_zizmor_rule_cross_tab; do
    .venv/bin/python -m analysis.$n
done
```

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

Every artifact below sits under `output/<DATASET_TAG>/` (e.g. `output/50k/`):

```
output/
├── 50k/                          # one tag per sampled run
│   ├── sampled_commits.parquet   # input: commit-level sample with file lists
│   ├── diffs.jsonl               # intermediate: one row per (commit, workflow file)
│   ├── scans.jsonl               # intermediate: one row per unique blob hash
│   ├── patterns.jsonl            # FINAL: pattern catalog
│   ├── clean_fixes/              # FINAL: human-inspectable dump
│   │   ├── index.jsonl           # summary index, one row per commit
│   │   └── <repo_safe>__<sha10>/ # one directory per commit
│   │       ├── meta.json
│   │       ├── <flat-file>.before.yml
│   │       └── <flat-file>.after.yml
│   ├── backport_gaps/            # gap audit + history classification (§ below)
│   └── backport_ir/              # compiled .wsp programs + backport runs
└── 10k/                          # an older sample, untouched by 50k runs
    └── …
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
`output/$DATASET_TAG/clean_fixes/` (produced by `pattern_miner`) and, for each clean-fix
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
# Audit every clean-fix commit (uses output/$DATASET_TAG/clean_fixes/*/meta.json as input)
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

- For each historical commit of F on B (up to `MAX_HISTORY_COMMITS=50`),
  fetch the file and zizmor-scan it.
- Find the boundary commit where the master-fixed idents transition from
  present to absent. That commit's date is the backport date; `lag_days =
  backport_date - master_commit_date`.

Per-record concurrency: branches inside one master commit are processed by
a `ThreadPoolExecutor` (default 8 workers, configurable via `--workers`).
`requests.Session` is thread-safe for GETs and the urllib3 pool is sized
to match. This is what makes a 40-branch heavy record finish inside the
8-minute per-record budget.

Final post-hoc refinement by lag sign:

- `lag > +1 day`  → `true_backport`           (release applied master's fix later)
- `|lag| <= 1`    → `same_day_fix`            (likely a merge from master, not a deliberate backport)
- `lag < -1 day`  → `independent_prior_fix`   (release fixed independently before master)

Per-record time-budget (`PER_RECORD_TIMEOUT_S = 8 min`) and history cap
keep worst-case audit time bounded. Anything beyond becomes
`inconclusive` / `timed_out`.

```bash
.venv/bin/python -m backport_gaps classify-history          # default 8 workers
.venv/bin/python -m backport_gaps classify-history --workers 4
.venv/bin/python -m backport_gaps history-summary
```

### Output layout

```
output/<DATASET_TAG>/backport_gaps/
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

**Verification is layered, with a hard distinction between engine self-tests
and external oracles:**

- *Engine self-tests* (development / QA — NOT a paper-grade correctness signal):
  `check_postconditions` re-parses the patched YAML and asserts each landed
  edit's target state actually holds. Catches apply-engine bugs ("I said I
  wrote it but it isn't there") but it cannot tell you whether the patch
  fixes the vulnerability or keeps the workflow working — only that the
  engine kept its word. Useful for catching regressions when changing the
  apply engine; never reported as a success rate.
- *External oracles* (the only judgments that don't know about backport_ir):
  three independent checks run on `(target-before, patched)`:
  - **`zizmor_local`** (headline): for every landed edit, the workflow-scope
    surrounding that edit (its enclosing step, or job, or root) must end up
    free of the rule master targeted, and must not introduce any new finding
    within that same scope. Honestly answers "did the construct master tried
    to fix on master also get fixed on the release-branch corresponding
    construct?" — without penalising the patch for findings at unrelated
    sites the master commit never addressed.
  - **`zizmor_global`** (loose upper bound, kept for contrast): symmetric to
    pattern_miner's clean-fix criterion — at least one targeted rule reduced
    anywhere on the release branch, nothing new introduced. Will mark a
    correctly-applied backport as failure whenever the release branch has
    independent instances of the same rule that master never touched, so
    it should be reported alongside `zizmor_local` to surface the gap, not
    in place of it.
  - **`actionlint`**: workflow still lints cleanly — no new actionlint
    findings introduced relative to `target-before`. Strongest static proxy
    for "the workflow still works at the GitHub-Actions-schema level"; we
    do not actually execute the patched workflow.
  A backport is treated as paper-claim-correct iff `zizmor_local` AND
  `actionlint` both pass. The combined verdict is what evaluation should
  report; the engine self-tests stay internal.

### Run

```bash
export DATASET_TAG=50k

# offline smoke test — needs no Gigawork data, no GitHub, no zizmor
.venv/bin/python -m backport_ir selfcheck

# compile clean-fix commits into .wsp programs
#   (output/$DATASET_TAG/backport_ir/programs/)
.venv/bin/python -m backport_ir compile [--limit N]

# apply one program to a LOCAL target workflow (offline)
.venv/bin/python -m backport_ir apply <program.wsp> <target.yml>

# replay onto every still-open release-branch gap (needs GITHUB_TOKEN)
# --oracle additionally runs all three external oracles per pair
.venv/bin/python -m backport_ir backport [--limit N] [--oracle]
```

`apply`/`backport` need `ruamel.yaml` (format-preserving round-trip, so a PR
carries only the intended diff). `--oracle` additionally needs `actionlint`
(installed as `actionlint-py` in `requirements.txt`). The oracle and
`backport` reuse `pattern_miner.scan` and `backport_gaps`' GitHub client
respectively. With `--oracle`, `cmd_backport` prints per-oracle counts:

```
zizmor global:      N    (target rule reduced anywhere on release; loose)
zizmor local:       N    (target construct fixed at the master-targeted site)
actionlint:         N    (no new lint findings)
zizmor_local AND actionlint: N  (headline: paper-claim-correct)
```

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

### Compile-time edge cases the engine handles

- *Scalar ↔ complex type changes* (e.g. `secrets: inherit` → `secrets: {…map…}`):
  detected and consolidated into a single parent-level `rewrite_value` with a
  complex `value` payload (rendered in WSP as JSON flow). Otherwise the diff
  decomposes into `ensure_present` on the new children plus `ensure_absent`
  on the parent — and the latter would silently delete the whole key.
- *Adding a whole new list element* (a step the source created that the
  target doesn't have): flagged at compile time with `needs_review` —
  the engine does not synthesise new list elements; without the flag,
  apply would silently report `inapplicable`.
- *Removing a whole list element* (a step the source deleted): flagged at
  compile time with `needs_review` — naïvely removing each key of the step
  individually would leave a husk step with no `uses`/`run` that
  `actionlint` will reject.

### Current limits (future work)

- A new *top-level* key (e.g. an added `permissions:` block) is appended at
  end-of-file — semantically correct, but not position-matched to the source.
- Inserting / deleting a whole list element is not synthesised by the engine
  (see above — flagged `needs_review`); a human completes such patches.
- Pin resolution needs a GitHub-backed resolver; offline, pins are
  `needs_review`.
- Multi-file coordination is per-file: a master commit that touches N
  workflow files compiles to N independent `.wsp` programs with no
  cross-file atomic-apply guarantee.

## Analysis scripts

[`analysis/`](analysis/) holds the standalone analysis scripts used to derive
the numbers we report. Each script is independent, reads from `output/`, and
prints to stdout — no figures, no API calls, no side effects. They are the
authoritative source for the numbers; the per-CLI `summary` subcommands are
deliberately compact and don't show all of these.

| Script | What it reports | Inputs |
|---|---|---|
| [`01_clean_fix_filter_comparison.py`](analysis/01_clean_fix_filter_comparison.py) | Commit counts under strict vs. three looser definitions of "clean fix" | `diffs.jsonl`, `scans.jsonl` |
| [`02_pattern_distribution.py`](analysis/02_pattern_distribution.py) | All level-1 buckets, `|V_fixed_idents|` size breakdown, sub-cluster uniqueness ratio | `patterns.jsonl` |
| [`03_match_eval.py`](analysis/03_match_eval.py) | Match outcome on an out-of-sample 2 000-commit pull (`seed=99`): full / level-1 / miss | `patterns.jsonl`, `eval_diffs.jsonl`, `scans.jsonl` |
| [`04_gap_audit_drill.py`](analysis/04_gap_audit_drill.py) | Per-commit gap distribution, long tail, per-ident gap counts, repo coverage, mirror-commit stats | `backport_gaps/gaps.jsonl` |
| [`05_history_lag_drill.py`](analysis/05_history_lag_drill.py) | Refined backport status (true / same-day / independent / inconclusive / never / timed-out), lag distribution, full TRUE-backport list, 1-3 month cluster drill | `backport_gaps/gaps_with_history.jsonl` |
| [`06_zizmor_rule_cross_tab.py`](analysis/06_zizmor_rule_cross_tab.py) | Per-rule commit counts, top rule co-occurrence, rule × backport-status table, rule × gap-presence table | `clean_fixes/*/meta.json`, `backport_gaps/gaps*.jsonl` |

### How to run

Each script is `python -m analysis.<NN_name>`. They expect
`output/$DATASET_TAG/` to contain the artifacts listed in the **Inputs**
column above. Run any single one:

```bash
export DATASET_TAG=50k
.venv/bin/python -m analysis.01_clean_fix_filter_comparison
.venv/bin/python -m analysis.02_pattern_distribution
.venv/bin/python -m analysis.04_gap_audit_drill
.venv/bin/python -m analysis.05_history_lag_drill
.venv/bin/python -m analysis.06_zizmor_rule_cross_tab
```

Analysis 03 additionally requires a fresh out-of-sample evaluation set
(it matches new clean fixes against the catalog produced by 02). Build
it once, then re-run 03 any time:

```bash
export DATASET_TAG=50k
# build the eval set (seed=99, disjoint from default seed=42)
.venv/bin/python -m pattern_miner sample --n-commits 2000 --seed 99 \
    --out output/$DATASET_TAG/eval_sampled.parquet
.venv/bin/python -m pattern_miner diffs \
    --sample output/$DATASET_TAG/eval_sampled.parquet \
    --out output/$DATASET_TAG/eval_diffs.jsonl
.venv/bin/python -m pattern_miner scan --diffs output/$DATASET_TAG/eval_diffs.jsonl

# run the match
.venv/bin/python -m analysis.03_match_eval
```

Run all six in sequence (loops works for every shell with `for`):

```bash
for n in 01_clean_fix_filter_comparison \
         02_pattern_distribution \
         03_match_eval \
         04_gap_audit_drill \
         05_history_lag_drill \
         06_zizmor_rule_cross_tab; do
    echo "============ analysis.$n ============"
    .venv/bin/python -m analysis.$n
done
```

### Per-CLI summaries (lighter weight)

Most of the same numbers are also available via the pipeline's own
`summary` subcommands (less detail, but no `analysis/` step needed):

```bash
.venv/bin/python -m pattern_miner patterns           # prints top-10 buckets
.venv/bin/python -m backport_gaps summary            # gap-audit overview
.venv/bin/python -m backport_gaps history-summary    # backport-status overview
```

## Evaluation harnesses (`analysis_tools/`) — §V

[`analysis_tools/`](analysis_tools/) holds the RQ5-7 evaluation harnesses
that turn the §III artifacts into the §V tables. They consume the same
`output/$DATASET_TAG/` tree the rest of the pipeline produces, write
reports under `analysis_tools/reports/$DATASET_TAG/`, and judge every
patch by the same external oracles (`zizmor_local` + `actionlint`)
backport_ir's `--oracle` mode uses.

**Prerequisites** (must exist for the same `DATASET_TAG`):
`clean_fixes/*/meta.json` (`pattern_miner pipeline`),
`backport_gaps/gaps.jsonl` (`backport_gaps find-gaps`),
`backport_gaps/gaps_with_history.jsonl` (`backport_gaps classify-history`),
`backport_ir/programs/*.wsp` (`backport_ir compile`),
and a valid `GITHUB_TOKEN` in `.env`.

**Resume safety**: every script row-appends results to its `*_rows.jsonl`
and skips already-completed `(repo, commit, branch, file)` on re-run, so
crashing or Ctrl-C never destroys prior work — just re-launch the same
command. GitHub fetches and LLM calls flow through the shared (tag-
independent) `cache/`, so an incremental run from `10k` → `50k` only
hits the network for genuinely new content.

### RQ5 — capability

> *On the 4 776 unpatched (fix, branch) gap pairs, how often does
> WORKFLOWBP produce a scanner-verified patch?*

```bash
export DATASET_TAG=50k
.venv/bin/python -m analysis_tools.rq5_capability --run
#   Re-aggregate without re-running:
.venv/bin/python -m analysis_tools.rq5_capability
#   Smoke test:
.venv/bin/python -m analysis_tools.rq5_capability --run --limit 20
```

Drives `backport_ir backport --oracle` over the full gap set and buckets
each pair by oracle verdict (`accepted`, `needs_review_only`,
`no_landed_edits`, `failed_zizmor_local`, `failed_actionlint`, …).

Outputs (under `reports/$DATASET_TAG/`):
- `rq5_outcome_buckets.md` — per-bucket summary
- `rq5_per_rule.md` — acceptance rate per zizmor rule
- `rq5_rows.jsonl` — one row per pair

### RQ6 — historical reproducibility

> *On the 242 confirmed true backports, does WORKFLOWBP's output match
> what the maintainer actually wrote?*

```bash
.venv/bin/python -m analysis_tools.rq6_reproducibility
#   Smoke test:
.venv/bin/python -m analysis_tools.rq6_reproducibility --limit 10
#   Re-aggregate without re-fetching:
.venv/bin/python -m analysis_tools.rq6_reproducibility --aggregate-only
```

For each true-backport pair fetches the release-branch file immediately
before (`target_before`) and at (`target_after`, the ground truth) the
maintainer's backport commit, then compiles the master fix into a WSP,
applies it to `target_before`, and classifies the result:

- **byte_equal** — byte-for-byte identical to the maintainer's patch
- **ast_equal** — identical after ruamel round-trip (whitespace/order normalised)
- **effect_equal** — both candidates pass `zizmor_local` + `actionlint`
- **divergent** — otherwise

Outputs (under `reports/$DATASET_TAG/`):
- `rq6_summary.md`, `rq6_rows.jsonl`
- `rq6/cases/<key>/{target_before,target_after_maintainer,our_patched}.yml`
  for hand inspection of any divergent case

### RQ7 — baseline comparison

> *How does WORKFLOWBP compare against verbatim copy-paste,
> Dependabot-style single-dependency updates, and an LLM baseline?*

```bash
# Three baselines (no LLM)
.venv/bin/python -m analysis_tools.rq7_comparison \
    --baselines workflowbp copy_paste dependabot
# Add the LLM baseline (needs ANTHROPIC_API_KEY in .env)
.venv/bin/python -m analysis_tools.rq7_comparison \
    --baselines workflowbp copy_paste dependabot llm
```

Baselines live in [`analysis_tools/baselines/`](analysis_tools/baselines/):

| Module | What it does |
|---|---|
| [`copy_paste`](analysis_tools/baselines/copy_paste.py) | Unified `(source_before -> source_after)` diff applied to `target_before` via a 3-way merge; fails when the pre-image can't be located on the drifted target. |
| [`dependabot_style`](analysis_tools/baselines/dependabot_style.py) | Extracts only `uses:` upgrades from the source diff and applies each as a single-dependency edit; ignores permissions/with/persist-credentials by construction. |
| [`llm`](analysis_tools/baselines/llm.py) | Calls Claude with `(source_before, source_after, target_before)` and asks for the patched release-branch YAML; every `actions/<owner>/<repo>@<40-hex>` in the response is checked against the live GitHub API and fabricated SHAs are reported separately. Responses cached in `cache/llm/` keyed by sha256 of the full prompt, so re-runs across dataset tags pay zero API tokens. |

Outputs (under `reports/$DATASET_TAG/`):
- `rq7_summary.md` — per-baseline accepted / failed table
- `rq7_llm_hallucination.md` — fabricated-vs-real SHA pin count (LLM only)
- `rq7_rows.jsonl` — one row per pair, with per-baseline buckets

### Acceptance criterion

All three RQs judge correctness through the same two external oracles
(`zizmor_local` + `actionlint`) used by `backport_ir backport --oracle`.
This is the symmetric form of the §III clean-fix criterion
`V_fixed ≠ ∅ ∧ V_introduced = ∅`, now applied to the release-branch
transition rather than master's. No script ever asks the engine itself
to grade its output — that would be circular.

### Reports layout

```
analysis_tools/reports/
└── 50k/
    ├── rq5_outcome_buckets.md   rq5_per_rule.md   rq5_rows.jsonl
    ├── rq6_summary.md           rq6_rows.jsonl
    │   └── rq6/cases/<key>/{target_before,target_after_maintainer,our_patched}.yml
    └── rq7_summary.md           rq7_llm_hallucination.md   rq7_rows.jsonl
```

The whole `analysis_tools/reports/` tree is gitignored — checked-in
reports would drift out of sync with re-runs. The `cache/` tree is also
gitignored but persists between runs to avoid re-paying for fetches and
LLM calls.

### Results walkthrough (50k sample)

Every number reported below is reproducible by running the corresponding
analysis script with `DATASET_TAG=50k`, i.e. against `output/50k/`. Each
entry below states:
**what the number means**, **what filter produced it**, **which input file
it comes from**, and **which script prints it**.

A previous 10k run produced earlier numbers (see git history); the 50k
re-run reported here both confirmed structural findings and **retracted
several quantitative claims** that were small-sample artifacts — those are
flagged inline below.

---

#### A. Foundational counts — from raw CSV to clean fixes

**`sampled commits = 50 000`**
Deterministic blake2b sample over `workflows.csv` (`pattern_miner sample
--n-commits 50000 --seed 42`). Filter: `git_change_type == 'M'`,
`valid_yaml == 'True'`, `valid_workflow == 'True'`. Because the sampling
is monotonic in the per-commit hash bucket, the 10k subset is a strict
prefix — re-running with a higher `n` only ADDS commits.

**`file-diffs = 75 158`**
For each sampled commit, every workflow file modified by it produces one
file-diff (so most commits contribute 1, some contribute several).
Source: `output/50k/diffs.jsonl`.

**`scanned blobs = 144 947`**
The union of `file_hash` and `previous_file_hash` across all file-diffs.
Each blob is fed to zizmor via stdin once. Source: `output/50k/scans.jsonl`.

**`commits with V_fixed ≠ ∅ = 7 629`**
A commit's `V_fixed` is the set of `(rule_ident, yaml_route)` pairs that
were in the before scan and gone in the after scan. Any non-empty `V_fixed`
counts here. Script: **`analysis.01_clean_fix_filter_comparison`**.

**`clean-fix commits (strict) = 1 804`**
`V_fixed ≠ ∅` AND `V_introduced == ∅`. The strict-empty `V_introduced`
filter rejects step-index drift artifacts (a step inserted in the middle
shifts subsequent indices, so the same logical finding appears once in
V_fixed and once in V_introduced — a false "moved"). Script: **01**.

**`loose-A = 6 417`, `loose-B = 5 304`, `loose-C = 7 629`**
Three progressively looser definitions, shown so the precision/recall
trade-off is visible. `loose_B` (every ident's count strictly non-increasing)
is the principled middle ground that recovers ~2.9× the strict count.
Pipeline currently uses **strict**. Script: **01**.

---

#### B. Pattern catalog — from clean fixes to a matchable library

**`80 level-1 buckets (V_fixed_idents)` / `1 675 level-2 sub-clusters`**
Two-level clustering over the 1 804 clean fixes (script: **02**):

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

Source: `output/50k/patterns.jsonl`.

**Structural uniqueness ratio = 0.93**
Defined as `n_subclusters / n_commits` over the whole catalog. Near 1
means nearly every commit has its own unique structural template — proves
structural templates are too repo-specific to be reused as-is for backport
rewrite. Script: **02**. (Ratio stable across 10k → 50k: 0.95 → 0.93.)

**|V_fixed_idents| distribution**

| size | #buckets | #commits | % commits |
|---:|---:|---:|---:|
| 1 | 17 | 1 156 | 64.1% |
| 2 | 22 |   274 | 15.2% |
| 3 | 14 |   270 | 15.0% |
| 4 | 17 |    89 |  4.9% |
| 5 |  9 |    14 |  0.8% |
| 7 |  1 |     1 |  0.1% |

Zipf — single-rule fixes dominate, but the three 3-rule buckets account
for 16% because StepSecurity bots reliably emit the `{artipacked,
excessive-permissions, unpinned-uses}` triple. Script: **02**.

---

#### C. Match generalization — does the catalog cover unseen commits?

Built an out-of-sample evaluation set: `sample --n-commits 2000 --seed 99`
→ `diffs` → `scan` → 68 clean-fix commits (`output/50k/eval_diffs.jsonl`).

| Outcome | Definition | Count | % |
|---|---|---:|---:|
| `full` | V_fixed_idents in catalog AND structural hash in some sub-cluster | 3 | 4.4% |
| `level-1` | V_fixed_idents in catalog but structural hash unseen | 63 | 92.6% |
| `miss` | V_fixed_idents not in catalog | 2 | 2.9% |

Script: **03**. **Level-1 hit ≈ 97 % means the semantic taxonomy is
near-saturated**; level-2 hit ≈ 4.4 % means structural templates rarely
transfer between repos — Stage 2 metavariable parameterization is
required for usable rewrite. (Catalog growth from 43 → 80 buckets
roughly doubled the full-match rate from 1.5% to 4.4%, but level-1
stayed pinned near-saturation.)

The 2 misses are rare 3-/5-rule combinations not present in training
(e.g. `{artipacked, unpinned-images, unpinned-uses}`).

---

#### D. Backport-gap audit — which release branches are still vulnerable?

For each of the 1 804 clean-fix commits on master, query GitHub for the
project's release-style branches and zizmor-scan the same workflow file
on each branch's HEAD. Branch classification (`backport_gaps find-gaps`):

- **gap**: branch has any ident from master's V_fixed_idents still present
- **already_fixed**: branch has none of those idents (file is "clean")
- **inapplicable**: file does not exist on the branch

Source: `output/50k/backport_gaps/gaps.jsonl`. Script: **04**.

**Branch-level counts (10 862 release branches across 1 789 status-ok commits)**

| Bucket | Count | % of branches |
|---|---:|---:|
| `inapplicable` (file absent) | 4 375 | 40.3% |
| `already_fixed` | 1 711 | 15.8% |
| **`gap` — still vulnerable** | **4 776** | **44.0%** |

So **44 % of audited (commit, release-branch) pairs are unpatched
backporting opportunities** — UP from 33% at 10k. Among only the
actionable subset (`gap + already_fixed = 6 487`), gap rate is **73.6 %**
(was 63.9% at 10k). **The 10k sample systematically under-estimated the
gap problem.**

**Commit-level counts**

- 548 / 1 789 commits (30.6%) have at least one gap branch.
- Per-commit gap distribution has an extreme long tail. Top 5:
  - **187** gaps — `superwall/Superwall-iOS` (`unpinned-uses`)
  - **169** gaps — `zkonduit/ezkl` (`archived-uses`)
  - **132** gaps — `tiledb-inc/tiledb-py` (`unpinned-uses`)
  - **129** gaps — `element-hq/synapse` (3-rule triple)
  - **119** gaps — `mystenlabs/sui` (4-rule fix)

At 10k the long-tail max was 80 (archesproject/arches); at 50k it's 187 —
the new sample uncovered drastically worse cases.

**Repo-level coverage (1 665 unique repos)**

| Category | #repos | % of audited repos |
|---|---:|---:|
| at least one gap | 510 | 30.6% |
| any prior backport (already_fixed) | 209 | 12.6% |
| both gap AND prior backport | 109 | 6.5% |
| any backport activity (either) | 610 | 36.6% |
| **neither (no actionable signal)** | **1 055** | **63.4%** |

The pattern from 10k holds at scale: about two-thirds of clean-fix
projects have no actionable backport signal — workflow file isn't on
their release branches, or no release-style branches exist. Backporting
as a problem applies to the remaining ~37%.

**Mirror commits**: 72 of 1 714 unique commit hashes appear in ≥2 repos
(4.2% mirror rate). Doesn't bias gap counts much but should be deduped
for any maintainer-distinct claim.

**Most-often-unpatched zizmor rules** (counted per (commit, gap-branch, ident)):

| Rule | #gap occurrences |
|---|---:|
| `unpinned-uses` | 3 753 |
| `excessive-permissions` | 1 746 |
| `artipacked` | 1 497 |
| `archived-uses` | 598 |
| `template-injection` | 327 |
| `secrets-inherit` | 87 |
| `unpinned-images` | 65 |
| `cache-poisoning` | 50 |
| `misfeature` | 38 |
| `bot-conditions` | 9 |
| `use-trusted-publishing` | 8 |
| `dangerous-triggers` | 7 |
| `superfluous-actions` | 7 |
| `unsound-contains` | 4 |
| `obfuscation` | 4 |
| `unsound-condition` | 3 |

Three rules absent from the 10k sample appear at 50k:
`secrets-inherit` (87 gaps), `dangerous-triggers` (pwn-request, 7), and
`bot-conditions` (9). Rare-rule visibility scales with sample size.

---

#### E. History classification — of `already_fixed`, which are TRUE backports?

`already_fixed` conflates "release branch backported master's fix" with
"release branch never had the issue". `backport_gaps classify-history`
walks each branch's file history (capped at `MAX_HISTORY_COMMITS = 50`)
and locates the boundary commit where the master-fixed idents
transitioned from present to absent. Then
`lag = backport_commit_date - master_commit_date`, and we refine the
status by lag sign:

| Refined status | Definition | Count | % of 1 711 |
|---|---|---:|---:|
| **`true_backport`** | confirmed backport AND `lag > +1 day` | **242** | 14.1% |
| `same_day_fix` | confirmed backport AND `|lag| ≤ 1 day` (likely merge sync) | 1 038 | 60.7% |
| `independent_prior_fix` | confirmed backport AND `lag < -1 day` (release fixed first) | 106 | 6.2% |
| `inconclusive` | history cap reached without resolution | 126 | 7.4% |
| `never_had_it` | scanned full history; finding never present | 81 | 4.7% |
| `timed_out` | per-record 16-min budget exhausted | 118 | 6.9% |

Script: **05**. Source: `output/50k/backport_gaps/gaps_with_history.jsonl`.

Within each master commit, the per-branch history walks run on a
`ThreadPoolExecutor` (default 8 workers). The per-record budget was
raised from 8 to 16 minutes after observing that ~50 heavy records
(40+ branches) needed >8 min under variable network conditions; this
recovered 47 additional TRUE backports. The 118 still-`timed_out`
branches come from a long tail of even heavier records — running with
yet a larger budget would likely yield more TRUE backports.

**TRUE backport lag distribution (n = 242)**

| Bucket | Count | % of TRUE |
|---|---:|---:|
| 1-7 days | 49 | 20.2% |
| **1-4 weeks** | **3** | **1.2%** |
| 1-3 months | 55 | 22.7% |
| 3-12 months | 62 | 25.6% |
| > 1 year | 73 | 30.2% |

Distribution percentiles: min 4d, p25 47d, median 181d, p75 543d, p90 1 127d, max 1 949d, mean 341d.

**The 1-4 week valley is the only finding that holds up at scale.**
The 10k run's stronger claim — "no backports happen within a month" —
turned out to be a small-sample artifact: 50k uncovers **49 backports
within 7 days** (mostly rapid-response style) plus the long tail. The
remaining valley between 1 and 4 weeks (only 3 cases, 1.2% of all TRUE
backports) is real: maintainers either patch immediately (within a week)
or schedule the work for ≥1 month later, with very little in between.

**Caveat: TRUE backport diversity scales with sample but stays concentrated**

The 242 `true_backport` pairs reduce to **52 distinct projects / 54
distinct master commits**. The top 4 projects still account for
108/242 = 45% of pairs:

| Project | #branches (= #true_backport pairs) | #master commits |
|---|---:|---:|
| `nymtech/nym` | 48 | 1 |
| `hyperledger/besu` | 26 | 1 |
| `apache/camel-quarkus` | 19 | 1 |
| `hashicorp/packer` | 15 | 1 |
| `spacetelescope/jdaviz` | 14 | 2 |
| `vectordotdev/vector` | 14 | 1 |
| `kairos-io/kairos` | 10 | 1 |
| `mlrun/mlrun` | 10 | 1 |
| `solana-labs/solana-program-library` | 6 | 1 |
| (tail) 43 more projects | ≤ 5 each | mostly 1 each |

Both numbers (242 per-branch pairs, 54 per-master-commit events) are
legit but mean different things — paper claims must pick a unit and
report it explicitly.

**Run history (for reproducibility)**

| Stage | sample | MAX_HISTORY | budget | concurrency | TRUE backports |
|---|---|---:|---:|---|---:|
| 10k-v1 | 10 000 | 10 | 8 min | sequential | 27 |
| 10k-v4 | 10 000 | 50 | 8 min | 8 workers (+ bug fix) | 61 |
| 50k-v4 | 50 000 | 50 | 8 min | 8 workers | 195 |
| **50k-v5** | **50 000** | **50** | **16 min** | **8 workers** | **242** |

The bump from 10k-v4 (61) to 50k-v4 (195) was 3.2× — sub-linear with the
5× sample increase. The 50k-v4 → 50k-v5 bump (+47) came purely from
the timeout raise.

---

#### F. Cross-tabulation — which rules get backported, which get ignored?

`analysis.06_zizmor_rule_cross_tab` cross-tabs the master-fixed rule
against the refined backport status and against gap presence. Script: **06**.

**Per-rule commit count (#commits in `clean_fixes/` whose `V_fixed_idents` includes the rule)**

| Rule | #commits | % of 1 804 |
|---|---:|---:|
| `unpinned-uses` | 1 202 | 66.6% |
| `excessive-permissions` | 652 | 36.1% |
| `artipacked` | 481 | 26.7% |
| `template-injection` | 195 | 10.8% |
| `archived-uses` | 141 | 7.8% |
| `use-trusted-publishing` | 55 | 3.0% |
| `cache-poisoning` | 47 | 2.6% |
| `dangerous-triggers` | 40 | 2.2% |
| `superfluous-actions` | 34 | 1.9% |
| `unsound-condition` | 30 | 1.7% |
| `unpinned-images` | 24 | 1.3% |
| `misfeature` | 15 | 0.8% |
| `secrets-inherit` | 12 | 0.7% |
| `obfuscation` | 6 | 0.3% |
| `bot-conditions` | 6 | 0.3% |
| `github-env` | 5 | 0.3% |
| `unsound-contains` | 2 | 0.1% |

**Per-rule TRUE backport rate** (TRUE / (sum of all refined statuses for that rule))

| Rule | TRUE | total `already_fixed` branches | TRUE % |
|---|---:|---:|---:|
| `misfeature` | 14 | 15 | **93.3%** |
| `use-trusted-publishing` | 6 | 9 | 66.7% |
| `unpinned-uses` | 128 | 366 | **35.0%** |
| `artipacked` | 28 | 116 | 24.1% |
| `template-injection` | 98 | 508 | **19.3%** |
| `dangerous-triggers` | 5 | 51 | 9.8% |
| `archived-uses` | 8 | 86 | 9.3% |
| `cache-poisoning` | 2 | 22 | 9.1% |
| `excessive-permissions` | 40 | 484 | 8.3% |
| `unpinned-images` | 2 | 136 | 1.5% |
| (rules with 0 TRUE backports) | 0 | various | 0.0% |

**Two 10k findings that 50k retracted:**

1. **`template-injection` 0% TRUE backport** (10k) → **19.3%** at 50k.
   The "RCE-class fixes are never deliberately backported" claim was a
   small-sample artifact (10k had 0/159). 50k shows 98 deliberate
   `template-injection` backports — still under-performing relative to
   `unpinned-uses` (35%), but not absent.
2. **`artipacked` 100% TRUE backport** (10k, 7/7) → **24.1%** (28/116)
   at 50k. The "100% clean signal" claim was tiny-sample noise.

**Findings that held up:**

- `unpinned-uses` remains the largest single contributor — 128/242 = 53%
  of all TRUE backports, and the highest backport rate among
  high-volume rules (35.0%).
- Most rules still don't get deliberate backports (`obfuscation`,
  `unsound-condition`, `superfluous-actions`, `bot-conditions`, etc.
  all stay at 0% TRUE), but the LIST of "0% rules" shrank because
  rare rules now have non-trivial denominators.

**Per-rule gap rate** (gap / (gap + already_fixed))

| Rule | #gap | #already_fixed | gap rate |
|---|---:|---:|---:|
| `secrets-inherit` | 87 | 1 | **98.9%** |
| `artipacked` | 1 497 | 116 | 92.8% |
| `unpinned-uses` | 3 753 | 366 | 91.1% |
| `archived-uses` | 598 | 86 | 87.4% |
| `obfuscation` | 4 | 1 | 80.0% |
| `excessive-permissions` | 1 746 | 484 | 78.3% |
| `misfeature` | 38 | 15 | 71.7% |
| `cache-poisoning` | 50 | 22 | 69.4% |
| `use-trusted-publishing` | 8 | 9 | 47.1% |
| `template-injection` | 327 | 508 | 39.2% |
| `unpinned-images` | 65 | 136 | 32.3% |
| `unsound-contains` | 4 | 11 | 26.7% |
| `bot-conditions` | 9 | 29 | 23.7% |
| `superfluous-actions` | 7 | 41 | 14.6% |
| `dangerous-triggers` | 7 | 51 | 12.1% |
| `unsound-condition` | 3 | 33 | 8.3% |
| `github-env` | 0 | 4 | 0.0% |

`secrets-inherit` (98.9% gap rate) and `artipacked` (92.8%) are the most
under-maintained on release branches. `template-injection`'s low gap
rate (39.2%) is misleading — most of its already-fixed branches are
merge-sync, not deliberate backports (TRUE rate 19.3%).

**Key findings**:

- Only 4 zizmor rule types ever yield a deliberate backport in this
  sample. The first two (`artipacked`, `use-trusted-publishing`) have
  very small denominators, but every one of the 7 `artipacked` already-
  fixed branches turned out to be a TRUE backport.
- `unpinned-uses` is the single largest contributor to TRUE backports
  (56/61). It also has the highest already-fixed denominator (152), so
  ~37% of branches that look fixed are real backports.
- **`template-injection` (script-injection / RCE class) has 0 TRUE
  backports despite 159 branches showing as "already_fixed" — every one
  is a merge-sync false positive.** Release-branch script-injection is
  effectively unmaintained.

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
