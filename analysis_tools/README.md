# analysis_tools — §V evaluation harnesses (RQ5–RQ7)

Three runnable scripts that turn the existing `output/$DATASET_TAG/`
artifacts (clean fixes, gap pairs, history-classified branches) into the
§V tables. Each script writes a Markdown summary + a JSONL row dump to
`analysis_tools/reports/$DATASET_TAG/`.

All scripts honour `DATASET_TAG`; set it once at the top of the shell
(`export DATASET_TAG=50k`) and every input read and every output written
will be routed under that tag.

## Prerequisites

The §III pipeline must have finished first on the same `DATASET_TAG`;
specifically:

- `output/$DATASET_TAG/clean_fixes/*/meta.json` (from `pattern_miner pipeline`)
- `output/$DATASET_TAG/backport_gaps/gaps.jsonl` (from `backport_gaps find-gaps`)
- `output/$DATASET_TAG/backport_gaps/gaps_with_history.jsonl` (from
  `backport_gaps classify-history`)
- `output/$DATASET_TAG/backport_ir/programs/*.wsp` (from `backport_ir compile`)
- A valid `GITHUB_TOKEN` in `.env` (RQ5/RQ6/RQ7 all fetch
  release-branch files from GitHub).

GitHub fetches and LLM calls are served from the shared `cache/`
directory (not per-tag), so re-runs across dataset sizes never re-hit
the network for the same content.

## Resume safety

Every script appends results to `*_rows.jsonl` row by row and, on
re-launch, reads the existing JSONL to skip any
`(repo, commit, branch, file)` it has already processed. Crashing,
hitting Ctrl-C, or interrupting an overnight run never destroys prior
work — just re-run the same command.

## RQ5 — capability

> *On the 4,776 unpatched (fix, branch) pairs, how often does
> WORKFLOWBP produce a scanner-verified patch?*

```bash
# Drive the full backport pipeline (apply + oracles), then aggregate
.venv/bin/python -m analysis_tools.rq5_capability --run

# Re-aggregate without re-running (uses the existing backport_index.jsonl)
.venv/bin/python -m analysis_tools.rq5_capability

# Smoke test on a small sample
.venv/bin/python -m analysis_tools.rq5_capability --run --limit 20
```

Outputs (under `reports/$DATASET_TAG/`):

- `rq5_outcome_buckets.md` — per-bucket summary (accepted, needs-review-only, no-landed-edits, failed-zizmor-local, failed-actionlint, …)
- `rq5_per_rule.md` — per-zizmor-rule acceptance rate
- `rq5_rows.jsonl` — one row per pair, with its bucket

## RQ6 — historical reproducibility

> *On the 242 confirmed true backports, does WORKFLOWBP's output match
> what the maintainer actually wrote?*

```bash
.venv/bin/python -m analysis_tools.rq6_reproducibility

# Smoke test
.venv/bin/python -m analysis_tools.rq6_reproducibility --limit 10

# Re-aggregate without re-fetching
.venv/bin/python -m analysis_tools.rq6_reproducibility --aggregate-only
```

For each true-backport (commit, release-branch) pair the script fetches
the workflow file at the branch state immediately before the
maintainer's backport commit (`target_before`) and at the backport
commit itself (`target_after`, the ground truth). It compiles the
master fix into a WSP, applies it to `target_before`, and classifies
the result against `target_after`:

- **byte_equal** — byte-for-byte identical to the maintainer's patch
- **ast_equal** — identical after ruamel round-trip (whitespace/order normalised)
- **effect_equal** — both candidates accepted by zizmor_local + actionlint on the same `target_before`
- **divergent** — otherwise

Outputs (under `reports/$DATASET_TAG/`):

- `rq6_summary.md` — aggregated outcome table
- `rq6_rows.jsonl` — one row per (commit, branch, file)
- `rq6/cases/<key>/` — per-pair directory with
  `target_before.yml`, `target_after_maintainer.yml`, and
  `our_patched.yml` for hand inspection

## RQ7 — baseline comparison

> *How does WORKFLOWBP compare against verbatim copy-paste,
> Dependabot-style single-dependency updates, and an LLM baseline?*

```bash
# Three baselines (no LLM) on the full pair set
.venv/bin/python -m analysis_tools.rq7_comparison \
    --baselines workflowbp copy_paste dependabot

# Add the LLM baseline (requires ANTHROPIC_API_KEY env var)
.venv/bin/python -m analysis_tools.rq7_comparison \
    --baselines workflowbp copy_paste dependabot llm

# Smoke test
.venv/bin/python -m analysis_tools.rq7_comparison --limit 25
```

Baselines live in `analysis_tools/baselines/`:

| Module | What it does |
|---|---|
| `baselines.copy_paste` | Computes `(source_before -> source_after)` unified diff and applies each hunk to `target_before`; reports failure when the pre-image can't be located on the drifted target. |
| `baselines.dependabot_style` | Walks the source-side diff for `uses:` upgrades and applies each as a single-dependency edit on the target; ignores permissions/with/persist-credentials by construction. |
| `baselines.llm` | Calls the Anthropic API with `(source_before, source_after, target_before)` and asks for the patched release-branch file. Verifies every `actions/X@<40-hex>` in the output against the live GitHub API; counts fabricated SHAs separately. |

Outputs (under `reports/$DATASET_TAG/`):

- `rq7_summary.md` — per-baseline accepted / failed table
- `rq7_llm_hallucination.md` — fabricated-vs-real SHA pin count (LLM only)
- `rq7_rows.jsonl` — one row per pair, with per-baseline buckets

## Reproducing the §V tables

After all three scripts finish, the headline numbers for §V are:

- **RQ5**: the `accepted` count from `rq5_outcome_buckets.md`
- **RQ6**: byte + ast + effect from `rq6_summary.md`
- **RQ7**: side-by-side accepted percentages from `rq7_summary.md`
  plus the SHA hallucination rate from `rq7_llm_hallucination.md`

## Acceptance criterion

All three scripts judge correctness through the same two external
oracles (`zizmor_local` + `actionlint`) used by `backport_ir`'s
`run_backport --oracle`. This is the symmetric form of the clean-fix
criterion `V_fixed != ∅ ∧ V_introduced = ∅` from §III-A, applied to
the release-branch transition rather than the master-branch one (see
§IV-F closed-loop argument).
