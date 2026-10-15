"""`loom login | logout | whoami | call`: use a Loom server from the terminal
with the session `loom login` stores. They are the Python SDK's commands
(`loom.cli`), so the CLI and `loom.login()` share one implementation and one
credentials file; `main` hands their arguments straight to the SDK's parser.
Registered here by name only, so `loom --help` lists them."""

from __future__ import annotations

import sys
from typing import Any

SDK_COMMANDS = {
    "login": "Sign in to a Loom server on any browser with a one-time code.",
    "logout": "End the stored session on the server and forget it here.",
    "whoami": "Show who the stored session signs you in as.",
    "call": "Call any operation or API route with the stored session (`loom call --help`).",
}


def run_sdk_command(argv: list[str]) -> int:
    try:
        from loom.cli import main
    except ImportError:
        print(
            f"loom {argv[0]} needs the Python SDK: pip install clickhouse-loom "
            "(from a clone: pip install ./sdk/python)",
            file=sys.stderr,
        )
        return 1
    return main(argv, prog="loom")


def add_login_parsers(sub: Any) -> None:
    for name, help_text in SDK_COMMANDS.items():
        sub.add_parser(name, help=help_text, add_help=False)
