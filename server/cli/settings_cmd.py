"""`loom settings list|set|reset`: every setting's value in force and where it
came from, and the deployment-wide overrides of live settings
(`server.core.settings_overrides`). Run with the deployment's env: `list`
shows that env with the stored overrides laid over it.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from server.cli._ch import run_connected


def _print_rows(rows: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        live = ", live" if r["live"] else ""
        print(f"{r['name']} = {json.dumps(r['value'])}  ({r['source']}{live})")


def _cmd_list(args: argparse.Namespace) -> int:
    from server.core import runtime_overrides, settings_overrides

    async def _go(ch: Any) -> int:
        settings_overrides.adopt(await runtime_overrides.load(ch=ch))
        rows = settings_overrides.rows(prefix=args.prefix, changed=args.changed)
        _print_rows(rows, args.json)
        return 0

    return run_connected(_go)


def _cmd_change(args: argparse.Namespace) -> int:
    from server.core import settings_overrides
    from server.core.errors import LoomServerError
    from server.observability import audit

    clearing = args.settings_cmd == "reset"

    async def _go(ch: Any) -> int:
        try:
            previous = settings_overrides.row(args.name)["value"]
            if clearing:
                await settings_overrides.clear(args.name, ch=ch)
            else:
                # The raw text, validated as the environment variable would be.
                await settings_overrides.put(args.name, args.value, ch=ch)
            row = settings_overrides.row(args.name)
        except LoomServerError as e:
            print(f"✗ {e.message}", file=sys.stderr)
            return 2
        await audit.emit(
            "setting.changed",
            org="-",
            namespace="-",
            actor=f"cli:{socket.gethostname()}",
            subject={
                "name": args.name,
                "value": row["value"],
                "previous": previous,
                "cleared": clearing,
            },
        )
        verb = "reset" if clearing else "set"
        print(f"✓ {verb}: {args.name} = {json.dumps(row['value'])} ({row['source']})")
        return 0

    return run_connected(_go)


def register_settings_cli(sub: Any) -> None:
    p = sub.add_parser(
        "settings", help="Every setting's value in force, and overrides of the live ones."
    )
    ssub = p.add_subparsers(dest="settings_cmd", required=True)
    ls = ssub.add_parser("list", help="Each setting: value, where it came from, whether live.")
    ls.add_argument("--prefix", default="", help="Only the settings whose name starts with this")
    ls.add_argument("--changed", action="store_true", help="Only the ones not at their default")
    ls.add_argument("--json", action="store_true", help="One JSON array")
    ls.set_defaults(func=_cmd_list)
    st = ssub.add_parser(
        "set", help="Override a live setting for the whole deployment; outlives a restart."
    )
    st.add_argument("name", help="The field name, e.g. memory_ask_recency_weight")
    st.add_argument("value", help="Written as its LOOM_<NAME> environment value would be")
    st.set_defaults(func=_cmd_change)
    rs = ssub.add_parser("reset", help="Remove the override: back to the env or the default.")
    rs.add_argument("name")
    rs.set_defaults(func=_cmd_change)
