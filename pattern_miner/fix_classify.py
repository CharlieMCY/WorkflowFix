"""Classify each clean fix as structural / mixed / deletion.

A clean-fix commit C satisfies V_fixed != empty AND V_introduced = empty
(§III-B), so every ZIZMOR finding F in V_fixed was present BEFORE C and
absent AFTER C. But "absent after" admits two mechanisms:

  structural   the YAML construct that hosted the finding still exists
               after the commit; what changed is the value/shape inside
               it (e.g., a `permissions` block was added on the same job,
               or a `uses:` line was repinned on the same step).
               => the maintainer FIXED the vulnerability primitive.

  deletion     the YAML construct that hosted the finding is gone in
               after — the master commit removed the whole step / job /
               service that carried it.
               => the maintainer REMOVED the vulnerable code rather
               than fixing it. No static backporter can reproduce this
               on a divergent release branch (re-synthesising the
               deletion would require structural decisions a rewriter
               can't make safely; see §IV-B).

A commit is structural iff every finding in its V_fixed is structural.
If at least one finding is deletion, the commit is mixed (some hosts
survived, some did not) or pure deletion (no host survived). Both
mixed and deletion are out of scope for an automated backporter and
are filtered out of the working set used in §III-C and §III-D.

Identity model. The host of a finding F at route R is determined by R's
deepest meaningful enclosing construct:

   jobs.X.steps[i].*                -> step at index i in job X
   jobs.X.services.<name>.*         -> service `name` in job X
   jobs.X.container.*               -> the container of job X
   jobs.X[.uses|.name|...]          -> job X
   <root-level path> / "" / "on"    -> the workflow root (always present)

For step hosts we look up the actual step in BEFORE at the cited index
and extract its content identity (`uses=`, `run=`, `id=`, `name=`);
the host "survives" iff a step with the same identity appears in AFTER
(in any job, since master may have renamed the job at the same time).
For job hosts we accept job rename: the host survives iff some job in
AFTER has a step-identity set with non-trivial overlap with BEFORE's
job X (>=1 shared step identity). Service hosts survive iff the same
service-name key exists under the same (or renamed) job in AFTER.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from .extract_diff import _step_identity


_STEP_RE = re.compile(r"^jobs\.(?P<job>[^.\[]+)\.steps\[(?P<idx>\d+)\]")
_SERVICE_RE = re.compile(r"^jobs\.(?P<job>[^.\[]+)\.services\.(?P<svc>[^.\[]+)")
_JOB_RE = re.compile(r"^jobs\.(?P<job>[^.\[]+)")


@dataclass
class HostVerdict:
    """Per-finding classification."""
    route: str
    host_kind: str        # "step" | "service" | "job" | "root"
    host_id: str          # human-readable host identity (e.g., "jobs.build.steps[uses=actions/checkout]")
    survived: bool        # True iff the host still exists in AFTER


@dataclass
class FixVerdict:
    """Per-commit classification."""
    kind: str             # "structural" | "mixed" | "deletion"
    per_finding: list[HostVerdict]


# --- YAML lookup helpers --------------------------------------------------


def _safe_load(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _jobs(doc: Any) -> dict:
    if not isinstance(doc, dict):
        return {}
    j = doc.get("jobs")
    return j if isinstance(j, dict) else {}


def _step_id_set(job: Any) -> set[str]:
    """Set of step identities (content keys) for a job's steps list."""
    if not isinstance(job, dict):
        return set()
    steps = job.get("steps")
    if not isinstance(steps, list):
        return set()
    return {_step_identity(s) for s in steps if isinstance(s, dict)}


def _step_at(job: Any, idx: int) -> dict | None:
    if not isinstance(job, dict):
        return None
    steps = job.get("steps")
    if not isinstance(steps, list) or idx >= len(steps):
        return None
    s = steps[idx]
    return s if isinstance(s, dict) else None


# --- per-finding survival -------------------------------------------------


def _job_survives(job_name: str, before_doc: Any, after_doc: Any) -> tuple[bool, str | None]:
    """A job survives if (a) same name exists in after, or (b) some
    after-job has a step-identity overlap >= 1 with the before-job.
    Returns (survived, surviving_after_job_name_or_None)."""
    after_jobs = _jobs(after_doc)
    if job_name in after_jobs:
        return True, job_name
    # Try rename detection
    before_jobs = _jobs(before_doc)
    before_job = before_jobs.get(job_name)
    if before_job is None:
        return False, None
    before_ids = _step_id_set(before_job) - {"anon"}
    if not before_ids:
        # No step identities to anchor on — can't detect rename
        return False, None
    for after_name, after_job in after_jobs.items():
        after_ids = _step_id_set(after_job) - {"anon"}
        if before_ids & after_ids:
            return True, after_name
    return False, None


def _classify_finding(route: str, before_doc: Any, after_doc: Any) -> HostVerdict:
    # Step host
    m = _STEP_RE.match(route)
    if m:
        job_name = m.group("job")
        idx = int(m.group("idx"))
        before_job = _jobs(before_doc).get(job_name)
        step = _step_at(before_job, idx)
        if step is None:
            # Can't even find the host in before — treat as deletion
            return HostVerdict(route, "step", f"jobs.{job_name}.steps[{idx}]", False)
        identity = _step_identity(step)
        host_id = f"jobs.{job_name}.steps[{identity}]"
        if identity == "anon":
            # Anchor too weak; conservatively fall back to job-level survival
            survived, _ = _job_survives(job_name, before_doc, after_doc)
            return HostVerdict(route, "step", host_id, survived)
        # Look for a step with the same identity in ANY after-job
        for after_job in _jobs(after_doc).values():
            if isinstance(after_job, dict):
                for s in after_job.get("steps") or []:
                    if isinstance(s, dict) and _step_identity(s) == identity:
                        return HostVerdict(route, "step", host_id, True)
        return HostVerdict(route, "step", host_id, False)

    # Service host
    m = _SERVICE_RE.match(route)
    if m:
        job_name, svc = m.group("job"), m.group("svc")
        host_id = f"jobs.{job_name}.services.{svc}"
        job_survived, after_job_name = _job_survives(job_name, before_doc, after_doc)
        if not job_survived:
            return HostVerdict(route, "service", host_id, False)
        after_job = _jobs(after_doc).get(after_job_name) or {}
        services = after_job.get("services") if isinstance(after_job, dict) else None
        survived = isinstance(services, dict) and svc in services
        return HostVerdict(route, "service", host_id, survived)

    # Job host (route is jobs.X or jobs.X.<scalar field> with no further structure)
    m = _JOB_RE.match(route)
    if m:
        job_name = m.group("job")
        survived, _ = _job_survives(job_name, before_doc, after_doc)
        return HostVerdict(route, "job", f"jobs.{job_name}", survived)

    # Root-level finding (permissions, on, "")
    return HostVerdict(route, "root", route or "<root>", True)


# --- per-commit aggregate -------------------------------------------------


def classify(before_text: str, after_text: str,
             fixed_findings: list[dict]) -> FixVerdict:
    """Classify one (before, after, V_fixed) triple.

    `fixed_findings` is the list of dicts from clean_fixes/.../meta.json
    under files[*].V_fixed, each carrying `route` and `ident`.
    """
    before = _safe_load(before_text)
    after = _safe_load(after_text)
    verdicts = [_classify_finding(f.get("route", ""), before, after)
                for f in fixed_findings]
    if not verdicts:
        return FixVerdict(kind="structural", per_finding=[])
    survived = sum(1 for v in verdicts if v.survived)
    if survived == len(verdicts):
        kind = "structural"
    elif survived == 0:
        kind = "deletion"
    else:
        kind = "mixed"
    return FixVerdict(kind=kind, per_finding=verdicts)


def classify_commit_meta(meta: dict, file_text_reader) -> dict:
    """Aggregate per-file classifications into a per-commit verdict.

    `meta` is a parsed clean_fixes/<dir>/meta.json. `file_text_reader`
    is a callable that takes (commit_dir, "before"|"after", file_record)
    and returns the YAML text.

    A commit is structural iff all of its files are structural; if any
    file is mixed -> mixed; else (no structural files) -> deletion.
    """
    per_file = []
    for f in meta.get("files", []):
        before_text = file_text_reader(meta, "before", f)
        after_text = file_text_reader(meta, "after", f)
        v = classify(before_text, after_text, f.get("V_fixed", []))
        per_file.append({
            "file_path": f.get("file_path", ""),
            "kind": v.kind,
            "n_findings": len(v.per_finding),
            "n_survived": sum(1 for h in v.per_finding if h.survived),
            "per_finding": [
                {"route": h.route, "host_kind": h.host_kind,
                 "host_id": h.host_id, "survived": h.survived}
                for h in v.per_finding
            ],
        })
    kinds = {p["kind"] for p in per_file}
    if not kinds or kinds == {"structural"}:
        agg = "structural"
    elif "structural" not in kinds and "mixed" not in kinds:
        agg = "deletion"
    else:
        agg = "mixed"
    return {
        "repository": meta["repository"],
        "commit_hash": meta["commit_hash"],
        "kind": agg,
        "files": per_file,
    }
