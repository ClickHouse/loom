"""The dry-run-by-default switch of the CLI's write commands.

A command that rewrites stored rows reports what it would write unless it is
given `--apply`; `--dry-run` stays accepted and names the default. Both set
`args.dry_run`, so a command body reads one attribute either way.
"""

from __future__ import annotations

import argparse

APPLY_HINT = "dry run: nothing was written; re-run with --apply to write"


def add_apply_flags(p: argparse.ArgumentParser, *, writes: str) -> None:
    """`--apply` (do `writes`) and `--dry-run` (the default), mutually exclusive."""
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--apply", dest="dry_run", action="store_false", help=f"{writes} (default: dry run)"
    )
    g.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report what --apply would write, write nothing (the default)",
    )
    p.set_defaults(dry_run=True, apply_gated=True)


def dry_run_hint(args: argparse.Namespace, rc: int) -> str | None:
    """The line a successful dry run of an --apply-gated command ends with."""
    if rc == 0 and getattr(args, "apply_gated", False) and getattr(args, "dry_run", False):
        return APPLY_HINT
    return None
