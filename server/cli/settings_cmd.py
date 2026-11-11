"""`loom settings list|set|reset|history`: every setting's value in force and
where it came from, the deployment-wide overrides of live settings
(`server.core.settings_overrides`) and the record of their changes. Run with the
deployment's env: `list` shows that env with the stored overrides laid over it.
"""

from __future__ import annotations

import argparse
import getpass
import json
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
    from server.core import settings_overrides

    async def _go(ch: Any) -> int:
        await settings_overrides.refresh(ch=ch)
        rows = settings_overrides.rows(prefix=args.prefix, changed=args.changed)
        _print_rows(rows, args.json)
        return 0

    return run_connected(_go, org_free="the cell's settings")


def _cmd_change(args: argparse.Namespace) -> int:
    from server.core.errors import LoomServerError
    from server.core.settings_overrides import NOT_GIVEN
    from server.insights.app import settings_changes

    clearing = args.settings_cmd == "reset"
    expected = NOT_GIVEN if args.expect is None else args.expect
    # Named before anything is stored: a change must not land without its audit row.
    actor = f"cli:{getpass.getuser()}"

    async def _go(ch: Any) -> int:
        try:
            # The raw text, validated as the environment variable would be.
            row = await settings_changes.change(
                args.name,
                None if clearing else args.value,
                clear=clearing,
                expected=expected,
                actor=actor,
                ch=ch,
            )
        except LoomServerError as e:
            print(f"✗ {e.message}", file=sys.stderr)
            return 2
        if row is None:
            print(f"✓ reset: {args.name} (not a setting of this build)")
        else:
            verb = "reset" if clearing else "set"
            print(f"✓ {verb}: {args.name} = {json.dumps(row['value'])} ({row['source']})")
        return 0

    return run_connected(_go, org_free="the cell's settings")


def _cmd_history(args: argparse.Namespace) -> int:
    from server.insights.app import settings_changes

    async def _go(ch: Any) -> int:
        changes = await settings_changes.history(args.name, args.limit, ch=ch)
        if args.json:
            print(json.dumps(changes, indent=2, default=str))
            return 0
        for c in changes:
            after = "reset" if c["cleared"] else "set"
            # Only an override is the deployment's value to set back; anything else but
            # None (a retired setting's reset, with no undo) was the changing process's
            # own, which a reset restores.
            was = "" if c["previous_source"] in ("override", None) else " (undo: reset)"
            print(
                f"{c['ts']}  {c['actor']}  {c['name']}: {json.dumps(c['previous'])}{was} → "
                f"{json.dumps(c['value'])} ({after})"
            )
        return 0

    return run_connected(_go, org_free="the cell's settings")


def register_settings_cli(sub: Any) -> None:
    p = sub.add_parser(
        "settings", help="Every setting's value in force, and overrides of the live ones."
    )
    ssub = p.add_subparsers(dest="settings_cmd", required=True)
    ls = ssub.add_parser("list", help="Each setting: value, where it came from, whether live.")
    ls.add_argument("--prefix", default="", help="Only the settings whose name starts with this")
    ls.add_argument(
        "--changed", action="store_true", help="Only the ones off their default or overridden"
    )
    ls.add_argument("--json", action="store_true", help="One JSON array")
    ls.set_defaults(func=_cmd_list)
    expect = "Change nothing unless this is the value in force (written as for set)"
    st = ssub.add_parser(
        "set", help="Override a live setting for the whole deployment; outlives a restart."
    )
    st.add_argument("name", help="The field name, e.g. memory_ask_recency_weight")
    st.add_argument("value", help="Written as its LOOM_<NAME> environment value would be")
    st.add_argument("--expect", help=expect)
    st.set_defaults(func=_cmd_change)
    rs = ssub.add_parser("reset", help="Remove the override: back to the env or the default.")
    rs.add_argument("name", help="The field name, e.g. memory_ask_recency_weight")
    rs.add_argument("--expect", help=expect)
    rs.set_defaults(func=_cmd_change)
    hs = ssub.add_parser(
        "history", help="Recorded changes, newest first: who, the value before and after."
    )
    hs.add_argument("name", nargs="?", default="", help="One setting; all when omitted")
    hs.add_argument("--limit", type=int, default=50)
    hs.add_argument("--json", action="store_true", help="One JSON array")
    hs.set_defaults(func=_cmd_history)
