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

### Headline numbers (10k sample)

These are reproducible by re-running the scripts above on the current `output/`:

| Metric | Value | Source |
|---|---|---|
| Clean-fix commits (`V_fixed != ∅` ∧ `V_introduced == ∅`) | 364 | 01 |
| Pattern buckets / structural sub-clusters | 43 / 346 | 02 |
| Out-of-sample match: level-1 hit | 95.6% (65/68) | 03 |
| Out-of-sample match: level-2 (structural) hit | 1.5% (1/68) | 03 |
| Audited release branches | 2 546 across 364 commits | 04 |
| Gap pairs (release branch still has the rule) | **835** | 04 |
| Commits with ≥1 gap | 101 (27.7%) | 04 |
| `already_fixed` branches passed to history scan | 472 | 05 |
| **TRUE backports** (`lag > 1 day`) | **27** | 05 |
| Same-day "fixes" (likely merge sync from master) | 118 | 05 |
| Independent prior fix on release | 6 | 05 |
| Inconclusive (mostly `history_cap_reached`) | 256 | 05 |

### Important caveat on the TRUE backport count

`05_history_lag_drill` exposes that 17 of the 27 TRUE backports belong to a
single master commit on `hyperledger/besu` (one fix replayed on 17 release
branches with ~51-day lag). So the dataset has only **about 7 distinct
master commits** with a confirmed real backport, distributed across:
`hyperledger/besu`, `kumahq/kuma`, `kubernetes/minikube`, `stac-utils/rustac`,
`assertj/assertj`, `apache/camel-quarkus`, `bitwarden/sdk*`, `matplotlib/matplotlib`.
The 27 vs 7 gap is purely from counting per-(commit, branch) pair rather than
per-master-commit; both ways of counting are valid but mean different things
and must be reported explicitly.

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
