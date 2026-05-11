"""Run zizmor (or any compatible scanner) on workflow blobs and cache findings.

The scanner is the ground-truth oracle for "this commit fixed N vulnerabilities":
  V_fixed(commit) = (findings_in_before) - (findings_in_after)

We scan each unique blob hash exactly once (blobs are content-addressed in
Gigawork; the same hash never holds different content), then look up before/
after findings per file-diff at analysis time.

Output: scans.jsonl, one record per blob:
    {
      "file_hash": "<64-hex>",
      "ok": true,
      "findings": [
        {"ident": "artipacked", "severity": "Medium", "route": "jobs.X.steps[0]"},
        ...
      ]
    }
or on scan failure:
    {"file_hash": "<64-hex>", "ok": false, "error": "..."}

A finding's identity is `(ident, route)` — same rule firing at same YAML path
in before and after means it was NOT fixed. Disappearing means it was.
"""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from tqdm import tqdm

from .config import BLOBS_DIR, OUTPUT_DIR

# zizmor entry point lives in the same venv as this module.
ZIZMOR = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "zizmor"

# zizmor exit codes:
#   0  -> no findings, clean
#   1  -> hard error (e.g. invalid YAML)
#   >1 -> findings present (encodes max severity / count)
_OK_EXIT_CODES = {0}  # plus any non-1 codes when JSON parses
_ERROR_EXIT_CODE = 1


# --- finding identity --------------------------------------------------------


def _route_to_str(route_segments: list[dict]) -> str:
    """{Key: 'jobs'} {Key: 'X'} {Index: 0} -> 'jobs.X[0]'"""
    parts: list[str] = []
    for seg in route_segments:
        if "Key" in seg:
            parts.append(seg["Key"])
        elif "Index" in seg:
            parts.append(f"[{seg['Index']}]")
    return ".".join(parts).replace(".[", "[")


def _normalize_finding(f: dict) -> dict:
    """Reduce zizmor's verbose JSON to the fields we need for set diffing."""
    locs = f.get("locations") or []
    if locs:
        route = locs[0].get("symbolic", {}).get("route", {}).get("route", [])
        route_str = _route_to_str(route)
    else:
        route_str = ""
    det = f.get("determinations", {}) or {}
    return {
        "ident": f.get("ident", ""),
        "severity": det.get("severity", ""),
        "confidence": det.get("confidence", ""),
        "route": route_str,
    }


def finding_id(f: dict) -> tuple[str, str]:
    """Stable identity: (rule, yaml_path). Used for V_before vs V_after diff."""
    return (f["ident"], f["route"])


# --- single-blob scan --------------------------------------------------------


def scan_one(file_hash: str, blobs_dir: Path = BLOBS_DIR, timeout: int = 30) -> dict:
    """Run zizmor on one blob via stdin. Returns the record to write to scans.jsonl."""
    blob_path = blobs_dir / file_hash
    try:
        text = blob_path.read_bytes()
    except FileNotFoundError:
        return {"file_hash": file_hash, "ok": False, "error": "blob missing"}

    try:
        proc = subprocess.run(
            [
                str(ZIZMOR),
                "--format", "json",
                "--no-online-audits",
                "--no-progress",
                "-q",
                "-",
            ],
            input=text,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"file_hash": file_hash, "ok": False, "error": "timeout"}

    # exit 1 = hard error; everything else either has [] or [findings...]
    if proc.returncode == _ERROR_EXIT_CODE:
        return {"file_hash": file_hash, "ok": False,
                "error": (proc.stderr or b"").decode("utf-8", "replace")[:200]}

    try:
        raw = json.loads(proc.stdout or b"[]")
    except json.JSONDecodeError as e:
        return {"file_hash": file_hash, "ok": False, "error": f"json: {e}"}

    return {
        "file_hash": file_hash,
        "ok": True,
        "findings": [_normalize_finding(f) for f in raw],
    }


# --- batch scan --------------------------------------------------------------


def _scan_one_safe(file_hash: str) -> dict:
    """Wrapper for ProcessPoolExecutor that never raises."""
    try:
        return scan_one(file_hash)
    except Exception as e:  # pragma: no cover - defensive
        return {"file_hash": file_hash, "ok": False, "error": f"{type(e).__name__}: {e}"}


def scan_blobs(
    hashes: Iterable[str],
    out_path: Path | None = None,
    n_workers: int | None = None,
    skip_existing: bool = True,
) -> Path:
    """Scan all unique blob hashes in parallel, append-only to scans.jsonl.

    If `skip_existing` is True (default), hashes already present in the output
    file are not re-scanned. Lets us resume an interrupted run.
    """
    out_path = out_path or (OUTPUT_DIR / "scans.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_workers = n_workers or max(1, (os.cpu_count() or 4))

    todo = set(hashes)
    if skip_existing and out_path.exists():
        with out_path.open("r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    rec = json.loads(line)
                    todo.discard(rec["file_hash"])
                except (json.JSONDecodeError, KeyError):
                    continue

    if not todo:
        return out_path

    todo_list = sorted(todo)
    # Append mode so an interrupted run can be resumed safely.
    with out_path.open("a", encoding="utf-8") as fp, \
         ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_scan_one_safe, h): h for h in todo_list}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="zizmor"):
            rec = fut.result()
            fp.write(json.dumps(rec))
            fp.write("\n")
            fp.flush()

    return out_path


# --- analysis helpers --------------------------------------------------------


def load_scans(path: Path | None = None) -> dict[str, list[dict]]:
    """Return {file_hash -> list of normalized findings} for successful scans only."""
    path = path or (OUTPUT_DIR / "scans.jsonl")
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            rec = json.loads(line)
            if rec.get("ok") and "findings" in rec:
                out[rec["file_hash"]] = rec["findings"]
    return out


def diff_findings(
    before: list[dict],
    after: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Return (V_fixed, V_introduced).

    V_fixed     = findings present in `before` but absent in `after` (commit fixed them)
    V_introduced= findings in `after` but absent in `before` (commit added them)
    """
    before_idx = {finding_id(f): f for f in before}
    after_idx = {finding_id(f): f for f in after}
    fixed = [before_idx[k] for k in before_idx.keys() - after_idx.keys()]
    introduced = [after_idx[k] for k in after_idx.keys() - before_idx.keys()]
    return fixed, introduced
