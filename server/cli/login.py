"""`loom login`, `loom logout`, `loom whoami`: sign in to a Loom server from the
terminal. The flow itself lives in the Python SDK (`loom.credentials`), so the
CLI and `loom.login()` in a notebook share one implementation and one
credentials file."""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _cmd_login(args: argparse.Namespace) -> int:
    try:
        from loom.credentials import run
    except ImportError:
        print(
            f"loom {args.cmd} needs the Python SDK: pip install clickhouse-loom "
            "(from a clone: pip install ./sdk/python)",
            file=sys.stderr,
        )
        return 1
    return run(
        args.cmd,
        profile=args.profile,
        server=getattr(args, "server", None),
        open_browser=not getattr(args, "no_browser", False),
    )


def add_login_parsers(sub: Any) -> None:
    for name, help_text in (
        ("login", "Sign in to a Loom server on any browser with a one-time code."),
        ("logout", "End the stored session on the server and forget it here."),
        ("whoami", "Show who the stored session signs you in as."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--profile", help="Credentials profile (default: LOOM_PROFILE or 'default')."
        )
        if name == "login":
            p.add_argument("--server", help="Server URL (default: LOOM_BASE_URL or localhost).")
            p.add_argument("--no-browser", action="store_true", help="Only print the URL.")
        p.set_defaults(func=_cmd_login)
