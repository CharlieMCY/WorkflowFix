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
/your_folder/cicd/
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
