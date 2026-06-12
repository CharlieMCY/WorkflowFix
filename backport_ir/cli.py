"""CLI for the backport-IR pipeline.

    backport_ir selfcheck                      # offline smoke test (no data needed)
    backport_ir compile [--limit N]            # clean_fixes/ -> programs/*.wsp
    backport_ir apply PROGRAM.wsp TARGET.yml   # offline single-file apply
    backport_ir backport [--limit N] [--oracle]# replay onto gap branches (GitHub)
    backport_ir oracle PROGRAM BEFORE PATCHED  # zizmor acceptance verdict
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import (
    run_apply_local,
    run_backport,
    run_compile,
    run_oracle,
)


def cmd_selfcheck(args) -> int:
    from .selfcheck import main as selfcheck_main

    return selfcheck_main()


def cmd_compile(args) -> int:
    stats = run_compile(clean_fixes_dir=args.clean_fixes, out_dir=args.out, limit=args.limit)
    print(f"compiled IR programs -> {stats['out_dir']}")
    print(f"  programs:            {stats['n_programs']}")
    print(f"  total edits:         {stats['n_edits']}")
    print(f"  programs w/ review:  {stats['n_need_review']}")
    return 0


def cmd_apply(args) -> int:
    report = run_apply_local(args.program, args.target, out_dir=args.out)
    print(json.dumps(report["summary"], indent=2))
    print()
    for o in report["edits"]:
        line = f"  [{o['status']:>12}] {o['edit']}"
        if o.get("reason"):
            line += f"   ({o['reason']})"
        print(line)
    pc = report["postconditions"]
    print()
    print(f"post-conditions: {'OK' if pc['ok'] else 'FAILED'}")
    return 0


def cmd_backport(args) -> int:
    rows = run_backport(limit=args.limit, oracle=args.oracle)
    n = len(rows)
    patched = sum(1 for r in rows if r.get("status") == "patched")
    review = sum(1 for r in rows if r.get("summary", {}).get("needs_review"))
    print(f"backport attempts: {n}")
    print(f"  patched:         {patched}")
    print(f"  needs review:    {review}")
    if args.oracle:
        ok_zg = sum(1 for r in rows
                    if r.get("oracle", {}).get("zizmor_global", {}).get("success"))
        ok_zl = sum(1 for r in rows
                    if r.get("oracle", {}).get("zizmor_local", {}).get("success"))
        ok_a = sum(1 for r in rows
                   if r.get("oracle", {}).get("actionlint", {}).get("success"))
        ok_both = sum(1 for r in rows if r.get("oracle", {}).get("success"))
        print(f"  zizmor global:      {ok_zg}     (target rule reduced anywhere on release)")
        print(f"  zizmor local:       {ok_zl}     (target construct fixed at the master-targeted site)")
        print(f"  actionlint:         {ok_a}     (no new lint findings)")
        print(f"  zizmor_local AND actionlint: {ok_both}  (headline: paper-claim-correct)")
    return 0


def cmd_oracle(args) -> int:
    verdict = run_oracle(args.program, args.before, args.patched)
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    return 0 if verdict.get("success") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backport_ir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("selfcheck", help="run the self-contained offline smoke test")
    sp.set_defaults(func=cmd_selfcheck)

    sp = sub.add_parser("compile", help="compile clean_fixes/*/meta.json into IR programs")
    sp.add_argument("--clean-fixes", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_compile)

    sp = sub.add_parser("apply", help="apply one IR program to a local target (offline)")
    sp.add_argument("program", type=Path)
    sp.add_argument("target", type=Path)
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("backport",
                        help="replay IR onto release-branch gap files via GitHub")
    sp.add_argument("--gaps", type=Path, default=None)
    sp.add_argument("--clean-fixes", type=Path, default=None)
    sp.add_argument("--out", type=Path, default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--oracle", action="store_true",
                    help="also run the zizmor oracle (needs zizmor)")
    sp.set_defaults(func=cmd_backport)

    sp = sub.add_parser("oracle",
                        help="zizmor acceptance verdict for a patched file")
    sp.add_argument("program", type=Path)
    sp.add_argument("before", type=Path)
    sp.add_argument("patched", type=Path)
    sp.set_defaults(func=cmd_oracle)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
