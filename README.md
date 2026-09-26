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

Section 2 runs RQ1–RQ3, section 3 describes WorkflowBP, and section 4 runs
RQ4–RQ5.

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
* Prompts: `WSP_GRAMMAR`, `_SYSTEM` and `build_intent` in
  `backport_ir/llm_adapt.py`.

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

**Models and decoding.**
* Models are called through OpenRouter (`common/llm.py`) as
  `openai/gpt-5.4-mini` and `google/gemini-3.1-flash-lite`. Each WorkflowBP
  configuration and its pure-LLM baseline use the same model.
* Every request sets `temperature = 0` and `max_tokens = 8192`. `top_p` is
  not set, so the provider default applies.
* Pure-LLM prompt: `_SYSTEM` and `_prompt` in `neuro_eval/baseline_llm.py`.
  It gives the targeted rules, the source file before and after the fix,
  and the target file, and asks for the whole patched file.

---

`analysis_tools/`, `rq5_scan.py`, `rq6_check.py`, `scan_transplantability.py`,
`demo_backport.py` and `output/50k/` come from an earlier pilot run and are
not needed for the steps above.
