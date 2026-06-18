"""The executable backport-patch IR (in-memory model).

A compiled patch is an `IRProgram`: a list of anchored, idempotent `Edit`s
derived automatically from ONE master clean-fix commit's (before -> after)
diff. The IR is what gets replayed onto a drifted release branch.

Design (settled up front):

  * Edits are *idempotent ensures*, not imperative diff hunks, so replaying them
    onto a drifted / partially-fixed target converges instead of corrupting it.

  * Anchors locate an edit by YAML *semantic identity* — the job is a
    metavariable, steps are matched by `uses=`/`id=`/`name=` — NOT by line or
    list index. That is what absorbs the structural drift between master and a
    release branch.

  * `Pin` payloads express version alignment: pin to the *target's* current ref,
    never blindly copy master's resolved SHA.

Three ops, one per diff bucket:

    ensure_present   key must exist with value     (compiled from diff.added)
    ensure_absent    key must not exist            (compiled from diff.removed)
    rewrite_value    key's value must become X      (compiled from diff.changed)

Serialization lives in `wsp.py`: a program is stored and shown as a
Coccinelle/SmPL-style Workflow Semantic Patch, which is the single on-disk
format. The IR classes here are plain dataclasses with no JSON of their own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- ops --------------------------------------------------------------------

ENSURE_PRESENT = "ensure_present"
ENSURE_ABSENT = "ensure_absent"
REWRITE_VALUE = "rewrite_value"
# v3 typed list-element ops — make whole-step add/delete (the `restructure`
# class) expressible in the DSL so the apply engine can place them on the
# target. The target-dependent parameters (where to splice, which step to
# delete) are concrete fields here; a synthesizer (LLM) or compiler fills them.
INSERT_STEP = "insert_step"
REMOVE_STEP = "remove_step"
OPS = frozenset({ENSURE_PRESENT, ENSURE_ABSENT, REWRITE_VALUE,
                 INSERT_STEP, REMOVE_STEP})


# --- anchor segments --------------------------------------------------------


@dataclass(frozen=True)
class Seg:
    """One step of an anchor path.

    kind == 'key'     a literal mapping key            (jobs, steps, with, ...)
    kind == 'keyvar'  a metavariable mapping key       ($JOB — any job name)
    kind == 'list'    a list element matched by identity ([uses=actions/checkout])
    """

    kind: str
    name: str = ""        # kind=='key'
    var: str = ""         # kind=='keyvar'  (the SmPL job metavariable, e.g. $J)
    list_kind: str = ""   # kind=='list': uses|id|name|run|str|scalar|anon
                          #               (the paper's `field` in [field=context])
    value: str = ""       # kind=='list': identity value
                          #               (the paper's `context` in [field=context])
    # --- v2 keyvar binding discipline (only meaningful when kind=='keyvar') ---
    key_pin: str = ""
    """The literal job key the master fix touched — the PRIMARY identity. The
    metavariable binds this job first (it is always known at compile time)."""
    card: str = ""
    """Binding cardinality, SmPL-style: 'one' binds exactly one job (fail-closed
    to review if ambiguous — never fans out); 'each' fans to every match (the
    legacy unconstrained $JOB). Empty == legacy 'each'."""
    fingerprint: tuple = ()
    """A tuple of (field, value) step identities — e.g. (("uses","actions/checkout"),)
    that uniquely picks the job on master. Used ONLY as drift recovery when the
    pinned job key is ABSENT on the target (the release branch renamed the job).
    Empty == no recovery == fail-closed (inapplicable). A job is NEVER created."""

    @staticmethod
    def key(name: str) -> "Seg":
        return Seg("key", name=name)

    @staticmethod
    def keyvar(var: str) -> "Seg":
        return Seg("keyvar", var=var)

    @staticmethod
    def jobvar(var: str, key_pin: str = "", card: str = "one",
               fingerprint: tuple = ()) -> "Seg":
        """A job metavariable bound by literal pin (primary) + fingerprint
        (rename recovery), with an explicit binding cardinality."""
        return Seg("keyvar", var=var, key_pin=key_pin, card=card,
                   fingerprint=fingerprint)

    @staticmethod
    def listid(list_kind: str, value: str) -> "Seg":
        return Seg("list", list_kind=list_kind, value=value)

    def __str__(self) -> str:
        if self.kind == "key":
            return self.name
        if self.kind == "keyvar":
            return f"${self.var}"
        return f"[{self.list_kind}={self.value}]"


@dataclass
class Anchor:
    """A path to a *parent container* (the edit's `key` lives directly under it)."""

    segs: list[Seg] = field(default_factory=list)

    def __str__(self) -> str:
        out = ""
        for s in self.segs:
            if s.kind == "list":
                out += str(s)
            else:
                out += ("." + str(s)) if out else str(s)
        return out or "<root>"


# --- pin payload (version-aligned action ref) -------------------------------


@dataclass
class Pin:
    """A rewrite_value payload meaning: pin `action` to a commit SHA, choosing
    the SHA of the TARGET's *current ref* (not the source commit's SHA), so the
    backport pins in place with zero version change. Resolution happens at apply
    time via an injected ref->SHA resolver; an unresolved pin becomes a review
    item, never a guess.
    """

    action: str               # e.g. 'actions/checkout' (action) or 'ghcr.io/x/y' (image)
    align: str = "target_ref"
    kind: str = "action"      # 'action' -> ref->commit SHA; 'image' -> tag->@sha256 digest


# --- a single edit ----------------------------------------------------------


@dataclass
class Edit:
    """One idempotent edit: ensure `key` under `anchor` reaches a target state."""

    op: str
    anchor: Anchor
    key: str
    value: Any = None              # ensure_present / rewrite_value literal; insert_step: the step mapping
    pin: Pin | None = None         # rewrite_value via version-aligned pin
    expected_old: str | None = None  # rewrite_value: old value sketch (sanity check)
    review: str = ""               # non-empty => not auto-applicable; human must check
    # --- insert_step / remove_step placement (target-dependent) ---
    where: str = ""                # 'before' | 'after' | 'start' | 'end'
    ref_field: str = ""            # identity field of the step to anchor at / remove ('uses'|'id'|'name')
    ref_value: str = ""            # identity value

    def _ref(self) -> str:
        return f"[{self.ref_field}={self.ref_value}]" if self.ref_field else ""

    def describe(self) -> str:
        """One-line human-readable form, SmPL-ish, for reports."""
        loc = f"{self.anchor}.{self.key}" if str(self.anchor) != "<root>" else self.key
        if self.op == INSERT_STEP:
            pos = f" {self.where} {self._ref()}".rstrip()
            return f"+ {self.anchor}.step{pos} = {self.value!r}"
        if self.op == REMOVE_STEP:
            return f"- {self.anchor}.step {self._ref()}".rstrip()
        if self.op == ENSURE_PRESENT:
            return f"+ {loc} = {self.value!r}"
        if self.op == ENSURE_ABSENT:
            return f"- {loc}"
        if self.pin is not None:
            tgt = "@sha256 digest" if self.pin.kind == "image" else "target_ref SHA"
            return f"~ {loc} : pin({self.pin.action} -> {tgt})"
        return f"~ {loc} : -> {self.value!r}"


# --- a compiled program (one master commit) ---------------------------------


@dataclass
class IRProgram:
    """All edits compiled from one master clean-fix commit's diff of one file."""

    repository: str
    commit_hash: str
    source_file: str               # workflow path on master
    target_idents: list[str]       # zizmor idents this commit fixed (program-level)
    edits: list[Edit] = field(default_factory=list)
    github_url: str = ""

    def is_fully_automatable(self) -> bool:
        """True iff no edit needs human review (e.g. an unsupported list edit)."""
        return all(not e.review for e in self.edits)
