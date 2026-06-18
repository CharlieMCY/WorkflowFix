"""CEGIS over the WSP DSL: the LLM synthesizes/repairs a semantic patch, the
trusted engine APPLIES it, and the oracles return counterexamples.

This is *not* "LLM rewrites the YAML file". The artifact the model produces is a
`.wsp` PROGRAM. The deterministic engine (`apply_program`) is the only thing that
ever touches the target file — so format preservation, idempotent anchoring, and
the pin/digest resolvers all still hold. The model's job is purely to make the
target-independent program *fit this drifted target*: convert the compiler's
`needs review` notes into concrete `insert_step` / `remove_step` ops, repair an
anchor whose job was renamed, choose where a step goes. The loop:

    candidate .wsp ─▶ from_wsp ─▶ apply_program(target) ─▶ oracles
         ▲                                                    │
         └──────── counterexamples (apply + oracle) ◀─────────┘   (≤ K rounds)

Counterexamples are concrete and symbolic: "anchor jobs.$J.steps did not resolve
(job renamed?)", "edit left for review: removes a whole list element", plus the
four acceptance-oracle violations. The model is free-form but every candidate is
machine-applied and machine-verified, so the CEGIS path keeps the symbolic path's
soundness — a candidate that doesn't apply-and-verify is never accepted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ._yaml import load_safe
from .compile import diff_texts, path_is_security_relevant
from .ir import IRProgram
from .neuro_backport import Case, run_oracles
from .verify import _touched_jobs
from .wsp import from_wsp, to_wsp

_FENCE_RE = re.compile(r"```(?:wsp|ya?ml|text)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


# --- intent (what the fix IS — from the main diff) --------------------------


def _security_master_diff(case: Case) -> dict[str, Any]:
    added, removed, changed = diff_texts(case.before_text, case.after_text)
    idents = case.idents

    def sec(d):
        return {p: v for p, v in d.items() if path_is_security_relevant(p, idents)}

    return {"added": sec(added), "removed": sec(removed), "changed": sec(changed)}


def _yaml_subtree(doc: Any, job: str) -> str:
    jobs = (doc or {}).get("jobs") or {} if isinstance(doc, dict) else {}
    body = jobs.get(job)
    if body is None:
        return "<absent>"
    try:
        import io

        from ruamel.yaml import YAML

        y = YAML()
        y.default_flow_style = False
        buf = io.StringIO()
        y.dump({job: body}, buf)
        return buf.getvalue().rstrip()
    except Exception:
        return repr(body)[:1500]


def build_intent(case: Case, program: IRProgram) -> str:
    sd = _security_master_diff(case)
    before_doc = load_safe(case.before_text) or {}
    after_doc = load_safe(case.after_text) or {}
    touched = sorted(_touched_jobs(program))

    lines = [f"TARGET SECURITY RULE(S): {', '.join(case.idents)}", ""]
    lines.append("SECURITY-RELEVANT CHANGE ON MAIN (the intent to reproduce):")
    for label, d in (("ADD", sd["added"]), ("REMOVE", sd["removed"]),
                     ("CHANGE", sd["changed"])):
        for p, v in sorted(d.items()):
            if label == "CHANGE" and isinstance(v, (list, tuple)) and len(v) == 2:
                lines.append(f"  {label}: {p}  ::  {v[0]!r} -> {v[1]!r}")
            else:
                lines.append(f"  {label}: {p}  ::  {v!r}")
    if not any(sd.values()):
        lines.append("  (the fix is structural — see the job before/after below)")
    lines.append("")
    if touched:
        lines.append("TOUCHED JOB(S) ON MAIN — before vs after (reproduce ONLY the "
                     "security change, adapted to the target; ignore unrelated diffs):")
        for j in touched:
            lines.append(f"--- job `{j}` BEFORE (main):")
            lines.append(_yaml_subtree(before_doc, j))
            lines.append(f"--- job `{j}` AFTER (main):")
            lines.append(_yaml_subtree(after_doc, j))
            lines.append("")
    return "\n".join(lines)


# --- counterexamples (symbolic feedback, post-apply / post-verify) -----------


def apply_counterexamples(program: IRProgram, apply_result) -> list[str]:
    """What the ENGINE could not do with the candidate program — the structural
    half of the counterexample (distinct from the oracle half)."""
    out: list[str] = []
    for edit, o in zip(program.edits, apply_result.edits):
        if o.status == "inapplicable":
            out.append(f"ANCHOR DID NOT RESOLVE on target: `{edit.describe()}` "
                       f"({o.reason}). The job/step may be renamed or absent — fix "
                       "the anchor (pin/recover) or the step identity.")
        elif o.status == "needs_review":
            out.append(f"EDIT NOT AUTO-APPLICABLE: `{edit.describe()}` ({o.reason}). "
                       "Express it concretely (e.g. insert_step/remove_step, or a "
                       "resolvable pin).")
    return out


def oracle_violations(oracles: dict[str, Any]) -> list[str]:
    v: list[str] = []
    zg = oracles.get("zizmor_global", {})
    if not zg.get("success"):
        if zg.get("status") == "scan_error":
            v.append(f"zizmor could not scan the applied output ({zg.get('where')}): "
                     f"{str(zg.get('error'))[:160]} — the program produced invalid YAML.")
        else:
            if zg.get("introduced_idents"):
                v.append("SECURITY REGRESSION: the patch INTRODUCED new findings "
                         f"{zg['introduced_idents']}. Remove the cause.")
            if not zg.get("resolved_idents"):
                miss = zg.get("missed_idents") or zg.get("relevant_targets") or []
                v.append(f"FIX NOT ACHIEVED: targeted finding(s) {miss} still present "
                         "after applying your program. Add/adjust edits so they clear.")
    al = oracles.get("actionlint", {})
    if not al.get("success") and al.get("status") != "scan_error":
        for f in (al.get("introduced") or [])[:6]:
            v.append(f"WORKFLOW BROKEN (actionlint): [{f.get('kind')}] {f.get('message')}")
    pm = oracles.get("permissions", {})
    if not pm.get("success"):
        for c in (pm.get("collateral_perm_changes") or [])[:6]:
            v.append(f"COLLATERAL: permissions changed on untouched job `{c['job']}` "
                     f"({c['before']!r}->{c['after']!r}). Anchor only the touched job(s).")
    mn = oracles.get("minimality", {})
    if not mn.get("success"):
        v.append("NON-MINIMAL: these non-security leaves changed; do not touch them: "
                 f"{(mn.get('non_security_changes') or [])[:15]}")
    return v


# --- wsp extraction ---------------------------------------------------------


def extract_wsp(text: str) -> str:
    m = _FENCE_RE.search(text)
    body = m.group(1) if m else text
    body = body.strip()
    # tolerate a model that drops the @@ head: a bare edit block is unparseable,
    # so only return text that actually contains a head.
    return body


# --- the CEGIS loop ---------------------------------------------------------

WSP_GRAMMAR = """\
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
"""

_SYSTEM = (
    "You are a GitHub Actions security-backport synthesizer. You write a WSP "
    "semantic patch (a program); a trusted engine applies it to the target and "
    "verifies it. You never edit the target file directly.\n\n" + WSP_GRAMMAR
)


@dataclass
class LLMResult:
    accepted: bool
    rounds: int
    wsp: str = ""
    patched_text: str = ""
    apply_summary: dict = field(default_factory=dict)
    oracles: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def llm_backport(
    case: Case,
    program: IRProgram,
    *,
    resolver=None,
    image_resolver=None,
    model: str | None = None,
    max_rounds: int = 3,
    max_tokens: int = 8192,
    log: Callable[[str], None] | None = None,
) -> LLMResult:
    """CEGIS: synthesize/repair a .wsp until apply+verify passes (or budget out).

    Pins (action SHAs, image digests) are handled at the GRAMMAR level: the model
    writes the `<sha: pin target_ref>` / `<sha256: pin digest>` markers and the
    engine resolves them against the target — so the model never needs (or sees) a
    literal SHA, and no pin-map has to be injected into the prompt."""
    from common.llm import complete, default_model
    from .apply import apply_program

    model = model or default_model()
    intent = build_intent(case, program)
    compiled = to_wsp(program)

    base_user = (
        f"{intent}\n\n"
        "The compiler auto-derived this TARGET-INDEPENDENT program from the main "
        "diff. KEEP the blocks under the head that already apply; the "
        "`# --- needs review ---` notes are edits it could NOT place on a drifted "
        "target (whole-step add/delete, renamed jobs) — express those concretely "
        "(insert_step/remove_step, fixed anchors, pin markers). Return a COMPLETE "
        "program that fully applies to THIS target and clears the finding:\n"
        "```wsp\n" + compiled.rstrip() + "\n```\n\n"
        f"TARGET FILE (release branch `{case.branch}`, path `{case.file_path}`):\n"
        "```yaml\n" + case.target_text.rstrip() + "\n```\n"
    )

    result = LLMResult(accepted=False, rounds=0)
    user = base_user
    for rnd in range(1, max_rounds + 1):
        result.rounds = rnd
        try:
            resp = complete(_SYSTEM, user, model=model, temperature=0.0,
                            max_tokens=max_tokens)
        except Exception as e:
            result.error = f"llm_error: {e}"
            return result
        result.input_tokens += int(resp.get("input_tokens", 0))
        result.output_tokens += int(resp.get("output_tokens", 0))
        wsp = extract_wsp(resp.get("text", ""))
        result.wsp = wsp

        viol: list[str]
        if "@@" not in wsp:
            viol = ["Your output was not a valid WSP program (missing the `@@ ... @@` "
                    "head). Output one complete ```wsp block."]
            oracles = {}
        else:
            try:
                prog2 = from_wsp(wsp)
            except Exception as e:
                prog2 = None
                viol = [f"Your .wsp failed to parse: {str(e)[:160]}. Fix the syntax."]
                oracles = {}
            if prog2 is not None:
                if not prog2.edits:
                    viol = ["Your .wsp parsed but contained NO edit lines. Add the "
                            "concrete edits (insert_step/remove_step/pins/leaf edits)."]
                    oracles = {}
                else:
                    res = apply_program(prog2, case.target_text, resolver=resolver,
                                        image_resolver=image_resolver)
                    result.patched_text = res.patched_text
                    result.apply_summary = res.summary()
                    oracles = run_oracles(program, case.target_text,
                                          res.patched_text, res)
                    result.oracles = oracles
                    ce_apply = apply_counterexamples(prog2, res)
                    ce_oracle = oracle_violations(oracles)
                    # The 4 oracles are the ground-truth verifier. A stray edit
                    # that didn't resolve but changed nothing must not fail an
                    # otherwise-verified patch — so accept on oracles; ce_apply is
                    # only counterexample feedback when we have NOT yet passed.
                    if oracles["accepted"]:
                        result.accepted = True
                        if log:
                            log(f"    round {rnd}: ACCEPTED")
                        result.history.append({"round": rnd, "accepted": True})
                        return result
                    viol = ce_apply + ce_oracle

        result.history.append({"round": rnd, "accepted": False, "violations": viol})
        if log:
            log(f"    round {rnd}: rejected — {len(viol)} counterexample(s)")
        if rnd == max_rounds:
            break
        user = (
            base_user
            + "\n\nYOUR PREVIOUS PROGRAM was applied by the engine and REJECTED:\n"
            + "```wsp\n" + wsp.rstrip() + "\n```\n\n"
            + "COUNTEREXAMPLES from the engine + verifier (fix every one; the engine "
            "applies your program literally, so make anchors resolve and edits "
            "clear the finding):\n"
            + "\n".join(f"- {x}" for x in viol)
            + "\n\nReturn the corrected COMPLETE .wsp in one ```wsp block."
        )
    return result
