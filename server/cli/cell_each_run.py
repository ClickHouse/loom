"""Client-only command entry and conservative local resume admission."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NoReturn

from server.cli.cell_each_state import has_uncertainty, read_state, resume_read, run_lock


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, "Invalid cell administration options\n")


def _parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = _Parser(
        prog="loom cell each",
        description="Run a supported admin command in every cell.",
        add_help=add_help,
    )
    parser.add_argument("--router", required=True, help="Trusted public router origin")
    parser.add_argument(
        "--credentials", type=Path, required=True, help="Private automation key manifest"
    )
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--state", type=Path, help="Create an exclusive private outcome file")
    state.add_argument("--resume", type=Path, help="Resume an existing private outcome file")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Supported command after --")
    return parser


def add_each_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Share the exact client flags with normal CLI help and its generated tree."""
    parser = sub.add_parser(
        "each",
        help="Run a supported admin command in every cell",
        parents=[_parser(add_help=False)],
    )
    parser.set_defaults(func=_locked)


def _run(args: argparse.Namespace) -> int:
    from server.cli.cell_each import parse_command

    command = None
    saved = None
    try:
        if args.resume is not None:
            if args.command:
                raise ValueError("A resumed run supplies its original command")
            saved = read_state(args.resume)
            resume_read(args.resume, saved)
            if has_uncertainty(saved):
                print("UNSETTLED: an earlier action has no confirmed completion", file=sys.stderr)
                return 1
        else:
            tokens = args.command[1:] if args.command[:1] == ["--"] else args.command
            command = parse_command(tokens)
    except (OSError, ValueError):
        print("Invalid private cell administration input", file=sys.stderr)
        return 2
    from server.cli.cell_each_http import execute

    return execute(
        router=args.router,
        credentials=args.credentials,
        state_path=args.resume if args.resume is not None else args.state,
        command=command,
        state=saved,
    )


def _locked(args: argparse.Namespace) -> int:
    try:
        with run_lock(args.resume if args.resume is not None else args.state):
            return _run(args)
    except BlockingIOError:
        print("Cell administration run is already active", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Invalid private cell administration input", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Lock and inspect local history before any credential or HTTP facilities."""
    raw = sys.argv[1:] if argv is None else argv
    try:
        if (
            type(raw) is not list
            or len(raw) + 3 > 256
            or any(type(value) is not str or "\0" in value for value in raw)
            or sum(len(value.encode("utf-8")) + 1 for value in raw) + len(b"loom\0cell\0each\0")
            > 16384
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        print("Invalid cell administration options", file=sys.stderr)
        return 2
    return _locked(_parser().parse_args(raw))
