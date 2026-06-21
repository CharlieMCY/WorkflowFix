"""End-to-end streaming driver: process every (repo, commit) in
workflows.csv through scan -> clean-fix check -> structural filter -> gap
audit, appending one summary row per commit to
output/$DATASET_TAG/streaming.jsonl.

Designed for full-dataset runs:
  - resume-safe (re-reading streaming.jsonl skips already-processed commits)
  - Ctrl-C safe (flushes per row; SIGINT lets in-flight commits finish and
    quits cleanly without losing data)
  - warm-cache friendly (reuses 50k/scans.jsonl, the shared cache/ tree,
    and optionally imports 50k/gaps.jsonl + 50k/clean_fixes/classification
    so audited commits don't repay the GitHub budget)

Output schema (one JSON object per line):
  repository, commit_hash, processed_at, source ("stream" | "imported_from_50k"),
  outcome ∈ {audited, host_removed, not_clean_fix, blob_missing,
             audit_failed, exception},
  v_fixed_idents (if a clean fix),
  gap_audit       (if outcome=="audited" — the full gap record),
  host_classification / file_classifications (if host_removed),
  reason / error  (if a failure outcome)
"""
from __future__ import annotations

import csv
import json
import signal
import sys
import threading
import time
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from common.cache import jsonl_append
from common.dataset import output_dir
from pattern_miner.extract_diff import diff_workflow_versions
from pattern_miner.fix_classify import classify
from pattern_miner.scan import diff_findings, scan_bytes

from .config import get_github_tokens
from .gaps import find_gap_for_commit
from .github import GitHubClient
from .history import classify_record as classify_history_record


OUT_FILENAME = "streaming.jsonl"
SCANS_FILENAME = "scans.jsonl"
DIFFS_FILENAME = "diffs.jsonl"
HISTORY_FILENAME = "gaps_with_history.jsonl"
PATTERNS_FILENAME = "patterns.jsonl"
# Background refresh interval for patterns.jsonl (seconds). 02 reads it.
PATTERNS_REFRESH_INTERVAL = 1800

# Per-record cap for history classification when called inline during
# streaming. The outer driver already runs `workers` commits in parallel,
# so we keep the inner per-record concurrency modest to avoid thread blow-up.
HISTORY_WORKERS_PER_RECORD = 4


# ---------- helpers --------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_blob(blobs_dir: Path, h: str) -> bytes | None:
    if not h:
        return None
    p = blobs_dir / h
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None


# ---------- per-commit pipeline -------------------------------------------


def _ensure_scanned(
    file_hash: str, blobs_dir: Path,
    scans: dict, scan_lock: threading.Lock, scans_path: Path,
) -> list[dict]:
    """Return the scan findings for this blob hash. On cache miss, run
    zizmor, store in memory, and append to scans.jsonl."""
    cached = scans.get(file_hash)
    if cached is not None:
        return cached
    text = _read_blob(blobs_dir, file_hash)
    if text is None:
        with scan_lock:
            scans.setdefault(file_hash, [])
        return []
    result = scan_bytes(text)
    findings = result.get("findings", []) if result.get("ok") else []
    with scan_lock:
        # Another worker may have inserted while we ran; second write is idempotent.
        if file_hash not in scans:
            scans[file_hash] = findings
            jsonl_append(scans_path, {
                "file_hash": file_hash, "ok": True, "findings": findings,
            })
    return findings


def _flatten_path(file_path: str) -> str:
    """`.github/workflows/build.yml` -> `.github__workflows__build` — same
    convention as pattern_miner.clean_fixes._flatten_path."""
    p = file_path.replace("/", "__")
    for ext in (".yml", ".yaml"):
        if p.endswith(ext):
            p = p[: -len(ext)]
    return p


def _write_diff_rows(rec: dict, before_findings, after_findings, blobs_dir,
                      diffs_path, write_locks) -> dict:
    """Compute the structural diff for one (commit, file). Append the row
    to diffs.jsonl in pattern_miner format. Returns the per-file detail."""
    repo = rec["_repo"]; sha = rec["_sha"]
    before_hash = rec.get("previous_file_hash", "")
    after_hash = rec.get("file_hash", "")
    file_path = rec["file_path"]

    diff = diff_workflow_versions(
        repository=repo, commit_hash=sha, file_path=file_path,
        file_hash=after_hash, previous_file_hash=before_hash,
        blobs_dir=blobs_dir,
    )

    fixed, introduced = diff_findings(before_findings, after_findings)

    row = {
        "repository": repo, "commit_hash": sha, "file_path": file_path,
        "file_hash": after_hash, "previous_file_hash": before_hash,
        "added": diff.added, "removed": diff.removed,
        "changed": {k: list(v) for k, v in diff.changed.items()},
        "parse_error": diff.parse_error,
        "V_fixed": fixed, "V_introduced": introduced,
    }
    with write_locks["diffs"]:
        jsonl_append(diffs_path, row)

    return {
        "file_path": file_path,
        "before_hash": before_hash, "after_hash": after_hash,
        "v_fixed": fixed, "v_introduced": introduced,
    }


def _write_clean_fix(repo: str, sha: str, file_details: list[dict],
                     v_fixed_idents: list[str],
                     classification_kinds: list[dict] | None,
                     blobs_dir: Path, out_root: Path,
                     write_locks: dict) -> None:
    """Write clean_fixes/<dir>/{meta.json,*.before.yml,*.after.yml} +
    append to index.jsonl + classification.jsonl. `classification_kinds`
    may be None when the host-survival filter hasn't run yet (e.g.,
    audit failed earlier)."""
    cdir_name = f"{repo.replace('/', '__')}__{sha[:10]}"
    cdir = out_root / "clean_fixes" / cdir_name
    cdir.mkdir(parents=True, exist_ok=True)

    file_metas = []
    used_names: dict[str, int] = {}
    for fd in file_details:
        flat = _flatten_path(fd["file_path"])
        n = used_names.get(flat, 0)
        used_names[flat] = n + 1
        if n > 0:
            flat = f"{flat}__{n}"

        before_b = _read_blob(blobs_dir, fd["before_hash"]) or b""
        after_b = _read_blob(blobs_dir, fd["after_hash"]) or b""
        (cdir / f"{flat}.before.yml").write_bytes(before_b)
        (cdir / f"{flat}.after.yml").write_bytes(after_b)

        file_metas.append({
            "file_path": fd["file_path"],
            "before": f"{flat}.before.yml",
            "after": f"{flat}.after.yml",
            "scan_status": "ok",
            "V_fixed": fd["v_fixed"],
            "V_introduced": fd["v_introduced"],
        })

    meta = {
        "repository": repo,
        "commit_hash": sha,
        "github_url": f"https://github.com/{repo}/commit/{sha}",
        "V_fixed_count": sum(len(fm["V_fixed"]) for fm in file_metas),
        "V_fixed_idents": v_fixed_idents,
        "n_files_modified": len(file_metas),
        "files": file_metas,
    }
    (cdir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    cf_dir = out_root / "clean_fixes"
    with write_locks["clean_fixes_index"]:
        jsonl_append(cf_dir / "index.jsonl", {
            "dir": cdir_name,
            "repository": repo,
            "commit_hash": sha,
            "V_fixed_count": meta["V_fixed_count"],
            "V_fixed_idents": v_fixed_idents,
            "n_files_modified": meta["n_files_modified"],
        })

    if classification_kinds is not None:
        kinds = {k["kind"] for k in classification_kinds}
        agg = ("structural" if kinds == {"structural"}
               else "deletion" if "structural" not in kinds and "mixed" not in kinds
               else "mixed")
        with write_locks["classification"]:
            jsonl_append(cf_dir / "classification.jsonl", {
                "repository": repo,
                "commit_hash": sha,
                "kind": agg,
                "files": classification_kinds,
                "dir": cdir_name,
            })


def process_commit(
    repo: str, sha: str, file_recs: list[dict],
    *,
    scans: dict, scan_lock: threading.Lock, scans_path: Path,
    client: GitHubClient, blobs_dir: Path,
    out_root: Path, diffs_path: Path, gaps_path: Path,
    write_locks: dict,
) -> dict:
    base = {"repository": repo, "commit_hash": sha,
            "processed_at": _iso_now(), "source": "stream"}

    # 1) scan every needed blob (with cache) + write structural diffs
    file_details: list[dict] = []
    v_fixed_idents: set[str] = set()
    v_introduced_idents: set[str] = set()
    for rec in file_recs:
        before_findings = _ensure_scanned(
            rec.get("previous_file_hash", ""), blobs_dir,
            scans, scan_lock, scans_path,
        )
        after_findings = _ensure_scanned(
            rec.get("file_hash", ""), blobs_dir,
            scans, scan_lock, scans_path,
        )
        rec_full = {**rec, "_repo": repo, "_sha": sha}
        fd = _write_diff_rows(rec_full, before_findings, after_findings,
                                blobs_dir, diffs_path, write_locks)
        file_details.append(fd)
        for f in fd["v_fixed"]:
            v_fixed_idents.add(f["ident"])
        for f in fd["v_introduced"]:
            v_introduced_idents.add(f["ident"])

    # 2) clean-fix decision
    if not v_fixed_idents:
        return {**base, "outcome": "not_clean_fix", "reason": "no_v_fixed"}
    if v_introduced_idents:
        return {**base, "outcome": "not_clean_fix",
                "reason": "v_introduced_nonempty",
                "v_introduced_idents": sorted(v_introduced_idents),
                "v_fixed_idents": sorted(v_fixed_idents)}

    v_fixed_idents_sorted = sorted(v_fixed_idents)

    # 3) structural filter (per file) — classify EVERY clean fix
    file_kinds: list[dict] = []
    for fd in file_details:
        if not fd["v_fixed"]:
            file_kinds.append({"file_path": fd["file_path"], "kind": "structural"})
            continue
        before_b = _read_blob(blobs_dir, fd["before_hash"])
        after_b = _read_blob(blobs_dir, fd["after_hash"])
        if before_b is None or after_b is None:
            return {**base, "outcome": "blob_missing",
                    "file_path": fd["file_path"]}
        verdict = classify(
            before_b.decode("utf-8", "replace"),
            after_b.decode("utf-8", "replace"),
            fd["v_fixed"],
        )
        file_kinds.append({"file_path": fd["file_path"], "kind": verdict.kind})

    # Persist the clean fix (meta + before/after blobs + classification +
    # index) regardless of structural verdict — analyses depend on this.
    _write_clean_fix(repo, sha, file_details, v_fixed_idents_sorted,
                       file_kinds, blobs_dir, out_root, write_locks)

    if any(fk["kind"] != "structural" for fk in file_kinds):
        return {**base, "outcome": "host_removed",
                "v_fixed_idents": v_fixed_idents_sorted,
                "file_classifications": file_kinds}

    # 4) gap audit (only for structural clean fixes)
    meta = {
        "repository": repo, "commit_hash": sha,
        "V_fixed_idents": v_fixed_idents_sorted,
        "files": [
            {"file_path": fd["file_path"], "scan_status": "ok",
             "V_fixed": fd["v_fixed"], "V_introduced": fd["v_introduced"]}
            for fd in file_details
        ],
    }
    try:
        audit = find_gap_for_commit(client, meta)
    except Exception as e:
        return {**base, "outcome": "audit_failed",
                "v_fixed_idents": v_fixed_idents_sorted,
                "error": f"{type(e).__name__}: {e}"}

    with write_locks["gaps"]:
        jsonl_append(gaps_path, audit)

    # 5) history classification (only if audit succeeded with already_fixed
    # branches to walk). Writes augmented row to gaps_with_history.jsonl.
    history_path = out_root / "backport_gaps" / HISTORY_FILENAME
    try:
        augmented = classify_history_record(
            client, dict(audit),
            master_date_cache=write_locks["_master_date_cache"],
            max_workers=HISTORY_WORKERS_PER_RECORD,
        )
    except Exception as e:
        augmented = {**audit, "history_error": f"{type(e).__name__}: {e}"}
    with write_locks["history"]:
        jsonl_append(history_path, augmented)

    return {**base, "outcome": "audited",
            "v_fixed_idents": v_fixed_idents_sorted,
            "gap_audit_status": audit.get("status", "?")}


# ---------- driver ---------------------------------------------------------


def _build_resume_set(out_path: Path) -> set[tuple[str, str]]:
    if not out_path.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with out_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            try:
                r = json.loads(line)
                done.add((r["repository"], r["commit_hash"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def _import_from_50k(out_root: Path) -> set[tuple[str, str]]:
    """Bootstrap from 50k by copying its conventional artifacts into the
    full/ tree: streaming.jsonl meta-log, diffs.jsonl, scans.jsonl,
    clean_fixes/ (index + classification + per-commit dirs), and
    backport_gaps/gaps.jsonl. Analyses can run against output/full/
    immediately even before a single fresh commit has been processed."""
    fifty_k = output_dir(tag="50k")
    src_gaps = fifty_k / "backport_gaps" / "gaps.jsonl"
    src_class = fifty_k / "clean_fixes" / "classification.jsonl"
    src_cf = fifty_k / "clean_fixes"
    src_diffs = fifty_k / "diffs.jsonl"
    if not src_gaps.exists() or not src_class.exists():
        print(f"no 50k data to import (missing {src_gaps} or {src_class})")
        return set()

    out_path = out_root / OUT_FILENAME
    diffs_path = out_root / DIFFS_FILENAME
    gaps_dest = out_root / "backport_gaps"
    cf_dest = out_root / "clean_fixes"
    gaps_dest.mkdir(parents=True, exist_ok=True)
    cf_dest.mkdir(parents=True, exist_ok=True)

    # (i) Mirror clean_fixes/ tree (per-commit dirs + index + classification).
    import shutil
    n_cf = 0
    for cdir in src_cf.glob("*/"):
        dst = cf_dest / cdir.name
        if dst.exists():
            continue
        shutil.copytree(cdir, dst)
        n_cf += 1
    for fname in ("index.jsonl", "classification.jsonl"):
        src_f = src_cf / fname
        dst_f = cf_dest / fname
        if src_f.exists() and not dst_f.exists():
            shutil.copy2(src_f, dst_f)
    print(f"  clean_fixes/: copied {n_cf} per-commit dirs + index + classification")

    # (ii) Mirror diffs.jsonl + gaps.jsonl (append-only artifacts).
    if src_diffs.exists() and not diffs_path.exists():
        shutil.copy2(src_diffs, diffs_path)
        print(f"  diffs.jsonl copied ({diffs_path.stat().st_size:,} bytes)")
    gaps_path = gaps_dest / "gaps.jsonl"
    if src_gaps.exists() and not gaps_path.exists():
        shutil.copy2(src_gaps, gaps_path)
        print(f"  backport_gaps/gaps.jsonl copied ({gaps_path.stat().st_size:,} bytes)")

    # (iii) Append a one-line summary per 50k commit to streaming.jsonl.
    kind_by: dict[tuple[str, str], str] = {}
    for line in src_class.open():
        r = json.loads(line)
        kind_by[(r["repository"], r["commit_hash"])] = r["kind"]

    imported: set[tuple[str, str]] = set()
    cnt: Counter[str] = Counter()
    with out_path.open("a", encoding="utf-8") as fp:
        for line in src_gaps.open():
            r = json.loads(line)
            key = (r["repository"], r["commit_hash"])
            kind = kind_by.get(key, "unknown")
            stamp = {"repository": key[0], "commit_hash": key[1],
                     "processed_at": _iso_now(),
                     "source": "imported_from_50k",
                     "v_fixed_idents": r.get("V_fixed_idents", [])}
            if kind == "structural":
                outcome = "audited" if r.get("status") == "ok" else "audit_failed"
            elif kind in ("mixed", "deletion"):
                outcome = "host_removed"
                stamp["host_classification"] = kind
            else:
                outcome = "audited" if r.get("status") == "ok" else "audit_failed"
            if outcome == "audit_failed":
                stamp["error"] = r.get("status", "unknown")
            fp.write(json.dumps({**stamp, "outcome": outcome},
                                 ensure_ascii=False) + "\n")
            imported.add(key)
            cnt[outcome] += 1
        fp.flush()
    print(f"  streaming.jsonl: imported {len(imported):,} commits "
          + "(" + ", ".join(f"{k}={v}" for k, v in cnt.items()) + ")")
    return imported


def _refresh_patterns(out_dir: Path, patterns_path: Path) -> None:
    """Re-run the §III-B clustering on the current clean_fixes/ snapshot
    and atomically replace patterns.jsonl. Called periodically by the
    background refresh thread inside `run()`."""
    cf_dir = out_dir / "clean_fixes"
    index_path = cf_dir / "index.jsonl"
    diffs_p = out_dir / DIFFS_FILENAME
    scans_p = out_dir / SCANS_FILENAME
    if not (index_path.exists() and diffs_p.exists() and scans_p.exists()):
        return

    from pattern_miner.clean_fixes import (
        aggregate_commits, filter_clean_fixes, cluster_clean_fixes,
    )
    from pattern_miner.scan import load_scans

    scans = load_scans(scans_p)
    commits = aggregate_commits(diffs_p, scans)
    clean = filter_clean_fixes(commits)

    # Write to a tmp file then atomically replace, so a snapshot is never
    # partially-written from the reader's perspective.
    tmp = patterns_path.with_suffix(".jsonl.tmp")
    cluster_clean_fixes(clean, tmp)
    tmp.replace(patterns_path)


def _load_scan_cache(*paths: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as fp:
            for line in fp:
                try:
                    r = json.loads(line)
                    if r.get("ok"):
                        out[r["file_hash"]] = r.get("findings", [])
                except (json.JSONDecodeError, KeyError):
                    continue
    return out


def _group_csv(
    csv_path: Path, skip: set[tuple[str, str]],
) -> dict[tuple[str, str], list[dict]]:
    """Stream the CSV, keep only valid workflow modifications, group by
    (repo, commit). Skip commits already in `skip` so we don't blow memory
    on resumed runs."""
    by_commit: dict[tuple[str, str], list[dict]] = defaultdict(list)
    n_csv = n_kept = 0
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            n_csv += 1
            if n_csv % 500_000 == 0:
                print(f"  scanned {n_csv:,} CSV rows; "
                      f"queued {len(by_commit):,} commits "
                      f"({n_kept:,} file-rows kept)", flush=True)
            if (row.get("git_change_type") != "M"
                    or row.get("valid_yaml") != "True"
                    or row.get("valid_workflow") != "True"):
                continue
            key = (row["repository"], row["commit_hash"])
            if key in skip:
                continue
            by_commit[key].append({
                "file_path": row["file_path"],
                "file_hash": row["file_hash"],
                "previous_file_hash": row["previous_file_hash"],
            })
            n_kept += 1
    print(f"  total: {n_csv:,} CSV rows, {n_kept:,} file-rows kept, "
          f"{len(by_commit):,} commits queued")
    return dict(by_commit)


def run(
    workers: int = 8,
    csv_path: Path | None = None,
    blobs_dir: Path | None = None,
    bootstrap_50k: bool = True,
    progress_every: int = 50,
    limit: int | None = None,
) -> None:
    out_dir = output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / OUT_FILENAME
    scans_path = out_dir / SCANS_FILENAME
    diffs_path = out_dir / DIFFS_FILENAME
    gaps_path = out_dir / "backport_gaps" / "gaps.jsonl"
    gaps_path.parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "clean_fixes").mkdir(parents=True, exist_ok=True)
    csv_path = csv_path or Path("workflows.csv")
    blobs_dir = blobs_dir or Path("workflows")

    if not csv_path.exists():
        sys.exit(f"workflows.csv not found at {csv_path}")
    if not blobs_dir.is_dir():
        sys.exit(f"blobs dir not found at {blobs_dir}")

    done = _build_resume_set(out_path)
    if done:
        print(f"resume: {len(done):,} commits already in {out_path}")
    elif bootstrap_50k:
        done = _import_from_50k(out_dir)

    # The scan cache MUST mirror what's in output/<tag>/scans.jsonl: if we
    # preload from 50k but don't also write those entries here, downstream
    # analyses (e.g., 01) won't be able to look up findings for blobs the
    # streaming run "used from cache but never re-recorded". Either preload
    # AND mirror, or don't preload at all. We honour `bootstrap_50k` for
    # the decision so a clean run (`--no-import-50k`) has self-contained
    # output/<tag>/ artifacts.
    cache_sources = [scans_path]
    if bootstrap_50k:
        cache_sources.insert(0, output_dir(tag="50k") / "scans.jsonl")
    scans = _load_scan_cache(*cache_sources)
    print(f"scan cache: {len(scans):,} blobs preloaded")

    print(f"grouping CSV (skipping {len(done):,} already-done commits)...")
    commits = _group_csv(csv_path, done)
    items = list(commits.items())
    if limit is not None:
        items = items[:limit]
        print(f"--limit {limit}: processing first {len(items)} commits only")
    if not items:
        print("nothing to do — every commit is already processed.")
        return

    scan_lock = threading.Lock()
    streaming_lock = threading.Lock()
    write_locks = {
        "diffs": threading.Lock(),
        "gaps": threading.Lock(),
        "history": threading.Lock(),
        "clean_fixes_index": threading.Lock(),
        "classification": threading.Lock(),
        # master_date_cache used by classify_history_record across workers
        "_master_date_cache": {},
    }
    client = GitHubClient(get_github_tokens())

    # Mirror 50k's gaps_with_history.jsonl on first run so 05 can read the
    # combined dataset immediately. The bulk import already mirrored
    # backport_gaps/gaps.jsonl; this fills the history sibling if available.
    src_history = (output_dir(tag="50k") / "backport_gaps"
                    / HISTORY_FILENAME)
    dst_history = out_dir / "backport_gaps" / HISTORY_FILENAME
    if bootstrap_50k and src_history.exists() and not dst_history.exists():
        import shutil
        shutil.copy2(src_history, dst_history)
        print(f"backport_gaps/{HISTORY_FILENAME} copied "
              f"({dst_history.stat().st_size:,} bytes)")

    # Background thread: every PATTERNS_REFRESH_INTERVAL seconds, rebuild
    # patterns.jsonl from the current clean_fixes/index.jsonl + diffs.jsonl
    # + scans.jsonl. Cheap (~seconds even at full scale) and gives 02 a
    # fresh snapshot at any stopping point.
    patterns_path = out_dir / PATTERNS_FILENAME
    patterns_stop = threading.Event()

    def _patterns_refresh_loop():
        while not patterns_stop.is_set():
            try:
                _refresh_patterns(out_dir, patterns_path)
            except Exception as e:
                print(f"  [patterns-refresh] error: {e}", flush=True)
            patterns_stop.wait(PATTERNS_REFRESH_INTERVAL)

    patterns_thread = threading.Thread(
        target=_patterns_refresh_loop, daemon=True, name="patterns-refresh",
    )
    patterns_thread.start()

    shutdown = threading.Event()

    def _sigint(_signum, _frame):
        if shutdown.is_set():
            print("\n[SIGINT x2] hard stop")
            sys.exit(1)
        print("\n[SIGINT] finishing in-flight commits then stopping... "
              "(press Ctrl-C again for hard stop)", flush=True)
        shutdown.set()
    signal.signal(signal.SIGINT, _sigint)

    counter = Counter()
    start_t = time.time()

    def _worker(item):
        if shutdown.is_set():
            return
        (repo, sha), file_recs = item
        try:
            row = process_commit(
                repo, sha, file_recs,
                scans=scans, scan_lock=scan_lock,
                scans_path=scans_path, client=client,
                blobs_dir=blobs_dir,
                out_root=out_dir, diffs_path=diffs_path,
                gaps_path=gaps_path, write_locks=write_locks,
            )
        except Exception as e:
            row = {"repository": repo, "commit_hash": sha,
                   "processed_at": _iso_now(), "source": "stream",
                   "outcome": "exception",
                   "error": f"{type(e).__name__}: {e}"}
        with streaming_lock:
            jsonl_append(out_path, row)
            counter["_total"] += 1
            counter[row.get("outcome", "unknown")] += 1
            if counter["_total"] % progress_every == 0:
                elapsed = time.time() - start_t
                rate = counter["_total"] / elapsed if elapsed else 0
                remaining = len(items) - counter["_total"]
                eta_h = remaining / rate / 3600 if rate else float("inf")
                print(f"  {counter['_total']:,}/{len(items):,}  "
                      f"rate {rate*60:.0f}/min  ETA {eta_h:.1f}h  "
                      f"(audited={counter.get('audited',0)} "
                      f"host_removed={counter.get('host_removed',0)} "
                      f"not_clean_fix={counter.get('not_clean_fix',0)} "
                      f"audit_failed={counter.get('audit_failed',0)})",
                      flush=True)

    def _items_iter():
        for item in items:
            if shutdown.is_set():
                return
            yield item

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(_worker, _items_iter()):
            if shutdown.is_set():
                break

    # Stop the patterns refresher and do one final refresh so the on-disk
    # patterns.jsonl reflects the final state.
    patterns_stop.set()
    try:
        _refresh_patterns(out_dir, patterns_path)
    except Exception as e:
        print(f"final patterns refresh failed: {e}")

    elapsed = time.time() - start_t
    print(f"\nfinished. processed {counter['_total']:,} commits in {elapsed/3600:.2f}h")
    for k in sorted(k for k in counter if k != "_total"):
        print(f"  {k}: {counter[k]:,}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--csv", type=Path, default=Path("workflows.csv"))
    p.add_argument("--blobs", type=Path, default=Path("workflows"))
    p.add_argument("--no-import-50k", action="store_true",
                   help="skip the one-time import from output/50k/")
    p.add_argument("--progress-every", type=int, default=50)
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of fresh commits to process (smoke test)")
    args = p.parse_args(argv)
    run(workers=args.workers, csv_path=args.csv, blobs_dir=args.blobs,
        bootstrap_50k=not args.no_import_50k,
        progress_every=args.progress_every,
        limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
