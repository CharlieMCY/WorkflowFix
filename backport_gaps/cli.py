"""CLI for the backport-gap auditor.

Subcommands:
    find-gaps   audit every clean-fix commit's release branches; write gaps.jsonl
    summary     print aggregate stats from a gaps.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gaps import run, summarize
from .history import run as run_history
from .history import summarize as summarize_history


def cmd_find_gaps(args):
    out = run(out_path=args.out, limit=args.limit)
    print(f"gaps written -> {out}")


def cmd_summary(args):
    summarize(in_path=args.gaps)


def cmd_classify_history(args):
    out = run_history(
        in_path=args.gaps,
        out_path=args.out,
        limit=args.limit,
        max_workers=args.workers,
    )
    print(f"history-classified gaps -> {out}")


def cmd_history_summary(args):
    summarize_history(in_path=args.gaps)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backport_gaps")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("find-gaps",
                        help="audit clean-fix commits' release branches via GitHub API")
    sp.add_argument("--out", type=Path, default=None,
                    help="output JSONL (default: output/backport_gaps/gaps.jsonl)")
    sp.add_argument("--limit", type=int, default=None,
                    help="only process the first N clean-fix commits (smoke test)")
    sp.set_defaults(func=cmd_find_gaps)

    sp = sub.add_parser("summary",
                        help="aggregate stats from a gaps.jsonl")
    sp.add_argument("--gaps", type=Path, default=None,
                    help="gaps.jsonl path (default: output/backport_gaps/gaps.jsonl)")
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("classify-history",
                        help="walk each already_fixed branch's file history to "
                             "confirm true backports and compute lag")
    sp.add_argument("--gaps", type=Path, default=None,
                    help="input gaps.jsonl (default: output/backport_gaps/gaps.jsonl)")
    sp.add_argument("--out", type=Path, default=None,
                    help="output JSONL (default: output/backport_gaps/gaps_with_history.jsonl)")
    sp.add_argument("--limit", type=int, default=None,
                    help="only process the first N gaps.jsonl rows (smoke test)")
    sp.add_argument("--workers", type=int, default=8,
                    help="per-record branch concurrency (default 8)")
    sp.set_defaults(func=cmd_classify_history)

    sp = sub.add_parser("history-summary",
                        help="aggregate stats incl. lag distribution from gaps_with_history.jsonl")
    sp.add_argument("--gaps", type=Path, default=None,
                    help="path (default: output/backport_gaps/gaps_with_history.jsonl)")
    sp.set_defaults(func=cmd_history_summary)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
