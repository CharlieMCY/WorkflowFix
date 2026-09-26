# WorkflowBP — Replication Package

Research questions:

* **RQ1 (Gap).** Do security fixes on the default branch reach the release
  branches that downstream users build from?
* **RQ2 (Practice).** How often, and how quickly, are release branches fixed
  by deliberate backporting?
* **RQ3 (Challenges).** What dimensions of difficulty must an automated
  backporting tool overcome?
* **RQ4 (Comparison).** How does WorkflowBP compare with the approaches a
  maintainer could use today?
* **RQ5 (Cost).** What are the cost and latency of WorkflowBP compared with
  general-purpose LLMs?

Section 2 runs RQ1–RQ3, section 3 describes WorkflowBP, section 4 runs
RQ4–RQ5, and section 5 lists the LLM prompts and decoding parameters.

## 1. Setup

Python 3.12. zizmor and actionlint are called from `.venv/bin/`.

```bash
python3.12 -m venv .venv
# empirical study (RQ1–RQ3): zizmor 1.24.1
.venv/bin/pip install zizmor==1.24.1 actionlint-py==1.7.12.24 polars==1.40.1 PyYAML==6.0.3 \
    ruamel.yaml==0.19.1 requests==2.33.1 python-dotenv==1.2.2 tqdm==4.67.3
# WorkflowBP and its evaluation (RQ4–RQ5): zizmor 1.25.2
.venv/bin/pip install zizmor==1.25.2 actionlint-py==1.7.12.24 polars==1.41.2 PyYAML==6.0.3 \
    ruamel.yaml==0.19.1 requests==2.34.2 python-dotenv==1.2.2 tqdm==4.68.3

cp .env.example .env    # set GITHUB_TOKEN (or a pool GITHUB_TOKENS=t1,t2,...) and OPENROUTER_API_KEY
.venv/bin/python -m backport_ir selfcheck    # offline check of compiler, apply and oracles
```

**Data.** Input: the GitHub Actions workflow-history dataset (Gigawork,
MSR'24): `workflows.csv` and the `workflows/` blob directory. All outputs are
written to `output/full/` (`DATASET_TAG=full`).

**Release-branch audit data (`data/`).** The results of the release-branch
audit are included, so the RQ1–RQ2 reports in Section 2 can be run without
GitHub access:

```bash
mkdir -p output/full/backport_gaps
gunzip -c data/gaps.jsonl.gz              > output/full/backport_gaps/gaps.jsonl
gunzip -c data/gaps_with_history.jsonl.gz > output/full/backport_gaps/gaps_with_history.jsonl
```

Each file has one JSON line per default-branch security fix:

| Field | Meaning |
|---|---|
| `repository`, `commit_hash`, `default_branch` | The project and the fixing commit on its default branch |
| `V_fixed_idents` | zizmor rules the fix removed |
| `target_files` | Workflow files the fix changed |
| `gap_branches` / `already_fixed_branches` / `inapplicable_branches` | Release branches where the weakness is still present / already absent / the file does not exist |
| `…branches[].branch_head_sha` | Commit SHA of the branch HEAD at audit time (the point-in-time snapshot that was scanned) |
| `…branches[].files[]` | Per file: `file_path`, `status` (`ok`, `absent`, `scan_failed`) and, for gap branches, `V_present_idents` |

`gaps_with_history.jsonl` adds the history walk of every already-fixed branch:

| Field | Meaning |
|---|---|
| `already_fixed_branches[].backport_status` | `confirmed_backport` (the issue was present on the branch and later removed), `never_had_it`, `inconclusive`, or `timed_out` |
| `already_fixed_branches[].lag_days` | Days between the default-branch fix and its removal on the branch: > 1 is a plausible backport, within ±1 same-day, < −1 an independent prior fix |
| `…history_classifications[].backport_commit_sha`, `backport_commit_date` | The branch commit where the issue disappeared |
| `master_commit_date`, `record_timed_out`, `record_duration_s` | Date of the default-branch fix; whether the history budget was exceeded |

## 2. Empirical study (RQ1–RQ3)

```bash
export DATASET_TAG=full
# scan every commit before/after, apply the security-fix filter, audit release branches, classify history
.venv/bin/python -m backport_gaps stream-all --csv <dataset>/workflows.csv --blobs <dataset>/workflows --workers 8
.venv/bin/python -m backport_gaps.audit_remaining    # audit fixes not audited by stream-all
.venv/bin/python -m backport_gaps.retry_failed       # re-audit records that hit transient GitHub errors

# reports (read output/full/backport_gaps/ only)
.venv/bin/python -m backport_gaps summary --gaps output/full/backport_gaps/gaps.jsonl                     # RQ1: gap / already-fixed / inapplicable branches
.venv/bin/python -m analysis.04_gap_audit_drill                                                         # RQ1, RQ3: affected projects, gap branches per fix and project
.venv/bin/python -m backport_gaps history-summary --gaps output/full/backport_gaps/gaps_with_history.jsonl  # RQ2: history classes and backport lag
```

Results reported in the paper:

| RQ1 | Count | Share |
|---|---:|---:|
| Workflow-changing commits | 1,903,191 | |
| Security fixes | 69,865 | |
| Projects | 24,238 | |
| Release branches audited | 474,490 | 100% |
| — still vulnerable (gap) | 224,392 | 47.3% |
| — already fixed | 78,907 | 16.6% |
| — inapplicable (file absent) | 171,191 | 36.1% |
| Gap among branches that carry the workflow | 224,392 / 303,299 | 74.0% |
| Projects with at least one gap branch | 7,172 / 24,238 | 29.6% |

| RQ1 gap rate per rule | Rate | | Rate |
|---|---:|---|---:|
| artipacked | 93.6% | unsound-contains | 53.8% |
| secrets-inherit | 92.9% | use-trusted-publishing | 53.5% |
| unpinned-uses | 91.6% | bot-conditions | 45.1% |
| superfluous-actions | 84.5% | github-env | 36.9% |
| excessive-permissions | 81.7% | dangerous-triggers | 33.0% |
| archived-uses | 79.2% | obfuscation | 13.5% |
| misfeature | 73.2% | insecure-commands | 11.8% |
| template-injection | 66.4% | unsound-condition | 10.3% |
| unpinned-images | 62.1% | unredacted-secrets | 0.0% |
| overprovisioned-secrets | 59.6% | | |

| RQ2: already-fixed branches (78,907) | Branches | Share |
|---|---:|---:|
| Plausible backport (lag > 1 day) | 19,476 | 24.7% |
| Same-day (likely auto-merge) | 41,408 | 52.5% |
| Independent prior fix (lag < −1 day) | 5,670 | 7.2% |
| Never had the issue | 4,020 | 5.1% |
| Inconclusive (history too deep) | 4,582 | 5.8% |
| Timed out (budget exceeded) | 3,751 | 4.8% |

| RQ2: backport lag | Value |
|---|---:|
| Median | 243 days |
| Within one week | 1,296 |
| One to three months | 1,576 |
| Three to twelve months | 9,065 |
| More than one year | 6,580 |
| Maximum | 2,326 days |

| RQ3 | Value |
|---|---:|
| Mean gap branches per project with a gap | 31.3 |
| Largest number of branches missing one fix | 2,971 |
| Fixes missing from more than 50 branches | 752 |
| Fixes coupling two or more rules | 23,965 / 69,865 (34.3%) |
| {artipacked, excessive-permissions, unpinned-uses} | 9,380 (13.4%) |
| Unpatched branches where the fix location no longer exists | 60.5% |

* zizmor scans each file on its own through stdin
  (`--format json --no-online-audits`, `pattern_miner/scan.py`). It reads
  no repository configuration and makes no network calls, so a verdict
  depends only on the file content and the zizmor version.
* A release branch is audited by fetching the workflow at the branch's
  current HEAD and re-scanning it (`backport_gaps/gaps.py`).
* The history walk (RQ2) checks at most 50 commits per file within
  16 minutes per fix (`MAX_HISTORY_COMMITS` and `PER_RECORD_TIMEOUT_S` in
  `backport_gaps/history.py`). Records over this budget are labelled
  `timed_out`.

## 3. WorkflowBP

| Step | Module | Entry point |
|---|---|---|
| Compile fix → WSP | `backport_ir/compile.py` | `compile_program` |
| WSP text format | `backport_ir/wsp.py` | `to_wsp`, `from_wsp` |
| Apply to a branch | `backport_ir/match.py`, `backport_ir/apply.py` | `apply_program` |
| Four oracles | `backport_ir/verify.py` | `zizmor_oracle_local`, `actionlint_oracle`, `permissions_oracle`, `minimality_oracle` |
| LLM fallback (CEGIS) | `backport_ir/llm_adapt.py` | `llm_backport` |

Offline example (compile a fix, then apply it to a release-branch file whose job was renamed):

```bash
.venv/bin/python -m backport_ir compile --clean-fixes backport_ir/examples/clean_fixes --out /tmp/wsp_demo
.venv/bin/python -m backport_ir apply /tmp/wsp_demo/*.wsp backport_ir/examples/target-release-branch.yml --out /tmp/wsp_demo/patched
```

**WSP grammar** (as parsed by `backport_ir/wsp.py`):

```ebnf
program      ::= '@@' header '@@' block+
header       ::= '# source:' repo '@' sha path  'fixes' rule (',' rule)*  metavar_decl*
metavar_decl ::= 'metavariable job' '$' var ['pin' '"' job_key '"']
                 ['recover' 'uses=' action (',' 'uses=' action)*] ['bind' ('one' | 'each')]
block        ::= anchor (edit)+
anchor       ::= '.' | seg ('.' seg)*                  (* '.' = document root *)
seg          ::= key | '$' var | key '[' ('uses' | 'id' | 'name') '=' value ']'
edit         ::= '+' key [':' value]                    (* ENSURE_PRESENT *)
               | '-' key                                (* ENSURE_ABSENT *)
               | '-' key ':' old  '+' key ':' new       (* REWRITE: same key, same block *)
               | '+ step' ('before'|'after'|'start'|'end') ['[' field '=' value ']'] '=' json_step
               | '- step' '[' field '=' value ']'       (* whole-step edits: LLM fallback only *)
value        ::= scalar | json_flow | action '@<sha: pin target_ref>' | image '@<sha256: pin digest>'
```

**Rule-to-construct map** (`_IDENT_CONSTRUCTS`, `backport_ir/compile.py`):
excessive-permissions, use-trusted-publishing → `permissions`;
artipacked → `persist-credentials`; unpinned-uses, archived-uses → `uses`;
unpinned-images → `image`; secrets-inherit → `secrets`;
template-injection → `env`, `run`; github-env → `run`;
dangerous-triggers → `on`; bot-conditions → `if`.

**`<sha: pin target_ref>`.** `unpinned-uses` flags a mutable ref such as
`@v3`, not a missing one: GitHub requires every remote `uses:` to carry an
`@ref`. At apply time the engine reads the ref of the target's own step,
resolves it with `GET /repos/{owner}/{repo}/commits/{ref}`, and writes
`owner/repo@<sha>  # <ref>`, so the branch keeps its action version. A ref
that does not resolve becomes a review item, never a guess.

**Oracles.** A backport is accepted only when all four pass:
(1) no targeted finding and no new finding in any edited scope;
(2) actionlint reports no new finding;
(3) no untouched job's `permissions` block changes;
(4) every changed leaf belongs to a targeted rule's construct.

**LLM fallback.** Runs only on cases the symbolic engine does not accept.
There are at most K = 3 rounds, each a fresh `[system, user]` request.
* Round 1 gives the targeted rules, the security-relevant part of the
  default-branch diff with the touched jobs, the compiled WSP, and the
  target file.
* Each later round adds the previous WSP and its counterexamples: anchors
  that did not resolve, edits that could not be placed, and oracle
  violations.
* The model writes only the WSP; the engine applies it and the same oracles
  decide.
* Prompts and decoding parameters: Section 5.

**GitHub API.** Tokens from `GITHUB_TOKENS` / `GITHUB_TOKEN` are rotated per
request.
* A rate-limited token (HTTP 403) is parked until its reset time and the
  request moves to another token.
* Network errors and HTTP 5xx are retried up to 3 times with back-off.
* Timeouts are 10 s (connect) and 30 s (read).
* Responses are cached under `cache/` (`backport_gaps/github.py`,
  `common/gh_tokens.py`).

## 4. Evaluation (RQ4–RQ5)

```bash
# case population: symbolic pass over all unpatched cases, labelling each with its restructuring class
.venv/bin/python -m neuro_eval.full_backport --gaps output/full/backport_gaps/gaps.jsonl --out-dir output/full/backport_run --no-llm
# 12,000-case sample, proportional to the restructuring classes (seed 11)
.venv/bin/python -m neuro_eval.make_llm_sample --sym output/full/backport_run/symbolic_rows.jsonl --size 12000 --seed 11 --out output/full/llm_sample.jsonl

# WorkflowBP (symbolic engine + LLM fallback) and the pure-LLM baseline, one run per model
LLM_BACKEND=openrouter LLM_MODEL=openai/gpt-5.4-mini .venv/bin/python -m neuro_eval.llm_experiments \
    --sample output/full/llm_sample.jsonl --out-dir output/full/llm_experiments --rounds 3
LLM_BACKEND=openrouter LLM_MODEL=google/gemini-3.1-flash-lite .venv/bin/python -m neuro_eval.llm_experiments \
    --sample output/full/llm_sample.jsonl --out-dir output/full/llm_experiments_gemini --rounds 3
# copy-paste and Dependabot-style baselines
.venv/bin/python -m neuro_eval.baselines_cp_dep --gaps output/full/backport_gaps/gaps.jsonl --out-dir output/full/baselines
# zizmor --fix baseline (reads the target files saved by the GPT run)
.venv/bin/python -m neuro_eval.baseline_zizmorfix --sample output/full/llm_sample.jsonl --out-dir output/full/baseline_zizmorfix

# RQ4: compare all methods on the sample (reads output/full/ only)
.venv/bin/python -m neuro_eval.compare_methods                                                     # GPT
.venv/bin/python -m neuro_eval.compare_methods --llm output/full/llm_experiments_gemini/rows.jsonl   # Gemini
```

* `compare_methods` columns: `combined` = WorkflowBP, `symbolic` = symbolic
  engine only, `pureLLM_valid` = pure-LLM baseline (patches whose action pins
  resolve to real commits), `copy_paste`, `dependabot`, `zizmor_fix` =
  `zizmor --fix=all` judged by the same four oracles as WorkflowBP.

Results reported in the paper (12,000 sampled cases out of 312,401):

| RQ4 method | `compare_methods` column | Passed | Rate |
|---|---|---:|---:|
| WorkflowBP (Gemini-3.1-flash-lite) | `combined` | 8,148 | 67.9% |
| WorkflowBP (GPT-5.4-mini) | `combined` | 7,174 | 59.8% |
| Pure-LLM (Gemini) | `pureLLM_valid` | 5,940 | 49.5% |
| Pure-LLM (GPT) | `pureLLM_valid` | 4,044 | 33.7% |
| Symbolic engine only | `symbolic` | 3,291 | 27.4% |
| Copy-paste | `copy_paste` | 2,158 | 18.0% |
| Dependabot-style update | `dependabot` | 1,565 | 13.0% |

RQ5: mean tokens per case and fallback rounds:

```bash
for d in output/full/llm_experiments output/full/llm_experiments_gemini; do .venv/bin/python -c "
import json, sys
ok = [r for r in map(json.loads, open(sys.argv[1])) if r.get('status') == 'ok']
fb = [r for r in ok if r.get('fallback_run')]
m = lambda xs, k: sum(r.get(k, 0) for r in xs) / len(xs)
print(sys.argv[1], f'fallback: {len(fb)} cases in={int(m(fb,\"fb_in\"))} out={int(m(fb,\"fb_out\"))} rounds={m(fb,\"fb_rounds\"):.2f}',
      f'| pure-LLM: {len(ok)} cases in={int(m(ok,\"bl_in\"))} out={int(m(ok,\"bl_out\"))}')" "$d/rows.jsonl"; done
```

Results reported in the paper:

| RQ5 path | Cases | Input tokens | Output tokens | Mean rounds |
|---|---:|---:|---:|---:|
| Fallback WSP (GPT) | 8,709 | 9,066 | 446 | 2.01 |
| Fallback WSP (Gemini) | 8,709 | 9,032 | 501 | 1.82 |
| Pure-LLM rewrite (GPT) | 12,000 | 3,954 | 1,158 | |
| Pure-LLM rewrite (Gemini) | 12,000 | 4,551 | 1,343 | |

## 5. LLM prompts and decoding parameters

### Decoding parameters

| Parameter | Value |
|---|---|
| Endpoint | OpenRouter `/chat/completions` (`LLM_BACKEND=openrouter`, `common/llm.py`) |
| Models | `openai/gpt-5.4-mini`, `google/gemini-3.1-flash-lite` (each WorkflowBP configuration and its pure-LLM baseline use the same model) |
| Messages | one system and one user message per request; no conversation history |
| `temperature` | 0 |
| `max_tokens` | 8,192 |
| `top_p`, `seed`, other sampling parameters | not sent (provider defaults) |
| WSP-synthesis rounds | at most 3 (`--rounds`); stops at the first program that passes the oracles |
| Baseline rewrite rounds | `--bl-rounds` (default 3); stops at the first accepted file |
| Target file size | at most 16,000 characters (`--max-chars`) |
| Output parsing | WSP synthesis: first fenced `wsp` (or `yaml`/`text`) code block; baseline: largest fenced code block that parses as YAML |
| Request timeout, retries | 180 s; up to 7 attempts, back-off on HTTP 429/5xx and network errors |
| Response cache | temperature-0 responses cached under `cache/llm/`, keyed by model, system prompt and user prompt |

### WSP-synthesis prompt (LLM fallback, `backport_ir/llm_adapt.py`)

<details><summary>System prompt</summary>

````text
You are a GitHub Actions security-backport synthesizer. You write a WSP semantic patch (a program); a trusted engine applies it to the target and verifies it. You never edit the target file directly.

WSP semantic-patch syntax (this is what you OUTPUT — a program, not a file). A
trusted engine parses and APPLIES it to the target, so anchors must resolve
EXACTLY against the target's real job keys and step identities.

  @@
  # source: <repo>@<sha> <path>
  fixes <rule>, <rule>
  metavariable job $J pin "<exact-job-key-on-target>" bind one
  @@

  <anchor path>
  <edit lines>

ANCHORS are semantic paths. `$J` is the job metavariable from the head; it binds
the job whose key you `pin`. Steps are matched by IDENTITY, never index:
  jobs.$J.permissions
  jobs.$J.steps[uses=actions/checkout]          <- selector is the action NAME
  jobs.$J.steps[uses=actions/checkout].with        ONLY. NEVER include @version:
  jobs.$J.services.db                              [uses=actions/checkout]  ✓
                                                   [uses=actions/checkout@v4] ✗
Also valid step selectors: [id=build], [name=Checkout].

EDIT LINES under an anchor:
  + key: value          ensure_present (create/set)
  - key                 ensure_absent (delete)
  - key: old / + key: new   rewrite (a - and a + on the SAME key, same block)
  + step before|after [uses=X] = {<json step>}   insert a whole step
  - step [id=Y]                                  remove a whole step

THE EXACT IDIOMS (copy these shapes):

# unpinned-uses / archived-uses — pin an EXISTING action to a commit SHA. The
# engine fills the SHA from the marker; do NOT invent one. Selector has NO @ver:
jobs.$J.steps[uses=actions/checkout]
- uses: actions/checkout@v4
+ uses: actions/checkout@<sha: pin target_ref>

# artipacked — disable credential persistence on a checkout step:
jobs.$J.steps[uses=actions/checkout].with
+ persist-credentials: false

# excessive-permissions — add/scope a permissions block on the touched job:
jobs.$J.permissions
+ contents: read

# unpinned-images — pin an image to its digest (engine fills it):
jobs.$J.services.db
- image: postgres:15
+ image: postgres@<sha256: pin digest>

# replace an archived action with a maintained one — remove + insert, do NOT
# rewrite field-by-field:
jobs.$J.steps
- step [uses=actions/old-archived]
+ step after [id=checkout] = {"uses": "maintained/replacement@v2", "with": {...}}

RULES:
- The engine applies your program literally. If an anchor does not resolve, that
  edit silently does nothing and the finding stays — so use the target's ACTUAL
  job key (in `pin`) and ACTUAL action names (in selectors, name only, no @ver).
- Whole-step add/delete MUST use insert_step/remove_step — you cannot build or
  delete a step from `+ key`/`- key` leaf lines.
- For ALL pins write the markers EXACTLY (`<sha: pin target_ref>` for actions,
  `<sha256: pin digest>` for images); the engine fills the real hash from the
  target. Never write a literal SHA — you cannot know the correct one.
- Change ONLY security constructs of the named rule(s); anchor ONLY touched jobs.
- Output ONE complete .wsp in a single ```wsp code block. Nothing else.
````
</details>

<details><summary>User prompt, round 1 (<code>build_intent</code> and <code>llm_backport</code>)</summary>

````text
TARGET SECURITY RULE(S): {rules}

SECURITY-RELEVANT CHANGE ON MAIN (the intent to reproduce):
  ADD: {path}  ::  {value}
  REMOVE: {path}  ::  {value}
  CHANGE: {path}  ::  {old} -> {new}
  (the fix is structural — see the job before/after below)      <- only if no leaf above

TOUCHED JOB(S) ON MAIN — before vs after (reproduce ONLY the security change, adapted to the target; ignore unrelated diffs):
--- job `{job}` BEFORE (main):
{job YAML before the fix}
--- job `{job}` AFTER (main):
{job YAML after the fix}

The compiler auto-derived this TARGET-INDEPENDENT program from the main diff. KEEP the blocks under the head that already apply; the `# --- needs review ---` notes are edits it could NOT place on a drifted target (whole-step add/delete, renamed jobs) — express those concretely (insert_step/remove_step, fixed anchors, pin markers). Return a COMPLETE program that fully applies to THIS target and clears the finding:
```wsp
{compiled WSP}
```

TARGET FILE (release branch `{branch}`, path `{path}`):
```yaml
{target file}
```
````
</details>

<details><summary>User prompt, rounds 2–3</summary>

````text
{round-1 user prompt}

YOUR PREVIOUS PROGRAM was applied by the engine and REJECTED:
```wsp
{previous WSP}
```

COUNTEREXAMPLES from the engine + verifier (fix every one; the engine applies your program literally, so make anchors resolve and edits clear the finding):
- {counterexample}
- ...

Return the corrected COMPLETE .wsp in one ```wsp block.
````

Counterexample kinds: output without a `@@` head, WSP parse error, WSP without
edit lines, `ANCHOR DID NOT RESOLVE`, `EDIT NOT AUTO-APPLICABLE`, zizmor scan
error, `SECURITY REGRESSION` (new findings), `FIX NOT ACHIEVED`,
`WORKFLOW BROKEN (actionlint)`, `COLLATERAL` permission change on an untouched
job, `NON-MINIMAL` change.
</details>

### Baseline-rewrite prompt (pure-LLM, `neuro_eval/baseline_llm.py`)

<details><summary>System prompt</summary>

````text
You backport a security fix for a GitHub Actions workflow. You are given the fix as a before/after pair on the source branch and the current file on a divergent target branch. Output the FULLY PATCHED target file: apply the same security fix, adapted to the target's job names, action versions, and structure. Change only what the security fix requires; leave everything else unchanged. Output ONLY the patched YAML in a single ```yaml code block, with no explanation.
````
</details>

<details><summary>User prompt, round 1 (<code>_prompt</code>)</summary>

````text
Security rules to fix: {rules}

=== SOURCE BEFORE FIX ===
{source file before the fix}
=== SOURCE AFTER FIX ===
{source file after the fix}
=== TARGET FILE TO PATCH ===
{target file}
````
</details>

<details><summary>User prompt, later rounds (<code>rewrite</code>)</summary>

````text
{round-1 user prompt}

YOUR PREVIOUS OUTPUT was REJECTED:
```yaml
{previous patched file}
```

FEEDBACK from the verifier (fix every one):
- {violation}
- ...

Return the corrected COMPLETE patched file in one ```yaml block.
````

Feedback kinds: unparseable YAML, zizmor scan error, `SECURITY REGRESSION`,
`FIX NOT ACHIEVED`, `WORKFLOW BROKEN (actionlint)`, `FABRICATED PIN` (an
action SHA that does not exist).
</details>

---

`analysis_tools/`, `rq5_scan.py`, `rq6_check.py`, `scan_transplantability.py`,
`demo_backport.py` and `output/50k/` come from an earlier pilot run and are
not needed for the steps above.
