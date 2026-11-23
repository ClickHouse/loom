"""Source controls use the matching installed SDK's protected request path."""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

_SOURCE_CLI_API = 2
_DEPENDENCY_MESSAGE = (
    "Source controls require the matching-release clickhouse-loom Python SDK "
    "alongside the server CLI. Install the SDK from the same release artifact, "
    "or use that SDK's python -m loom.source_cli entry point. "
    "The server-only wheel does not contain the SDK."
)
_ACTIONS = (
    "preview",
    "add",
    "update",
    "list",
    "inspect",
    "pause",
    "resume",
    "refresh",
    "read",
    "remove",
)
_GROUPS = {
    "connection": ("profiles", "connect", "list", "inspect", "discover", "browse", "disconnect"),
    "destination": ("list", "create", "grants", "grant", "preview"),
    "recording": ("add", "list", "sync-now"),
    "retained": ("list", "inspect", "remove"),
}


def register_knowledge_source_cli(ksub: Any) -> None:
    parser = ksub.add_parser(
        "source", help="MCP sources, connections, collection destinations and recordings."
    )
    actions = parser.add_subparsers(dest="source_command", required=True)
    for action in _ACTIONS:
        _command(actions, "", action)
    for group, names in _GROUPS.items():
        nested = actions.add_parser(group).add_subparsers(dest="source_action", required=True)
        for action in names:
            _command(nested, group, action)


def _command(sub: Any, group: str, action: str) -> None:
    parser = sub.add_parser(action, allow_abbrev=False)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--org", required=True)
    if group != "connection" and (group, action) != ("destination", "list"):
        parser.add_argument(
            "--namespace", help="Ordinary destination; must agree with the JSON field."
        )
    parser.add_argument(
        "--request-file",
        help="UTF-8 JSON API fields, at most 1 MiB; - reads stdin. See the source CLI guide.",
    )
    parser.add_argument(
        "--token-file", help="Owned mode-0600 token file or - for stdin; otherwise LOOM_API_KEY."
    )
    parser.add_argument(
        "--session-file",
        required=True,
        help="Existing application-owned private session pointer; "
        "never initialized by this command.",
    )
    parser.add_argument(
        "--intent",
        choices=("automatic", "explicit"),
        default="automatic",
        help="Default automatic guards every call; explicit is a direct user action.",
    )
    if (group, action) == ("connection", "connect"):
        parser.add_argument(
            "--bearer-file",
            help="Owned mode-0600 credential file or - for stdin. Never an argv secret.",
        )
    if (group, action) == ("recording", "add"):
        parser.add_argument(
            "--credential-file",
            required=True,
            help="Owned mode-0600 credential file or - for stdin. Never an argv secret.",
        )
    parser.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    try:
        entry = importlib.import_module("loom.source_cli")
    except ImportError:
        entry = None
    if entry is not None and getattr(entry, "SOURCE_CLI_API", None) == _SOURCE_CLI_API:
        from server.cli import _session_context

        if _session_context._explicit():
            args = argparse.Namespace(**{**vars(args), "intent": "explicit"})
        return int(entry.run_server_entry(args))
    print(_DEPENDENCY_MESSAGE, file=sys.stderr)
    return 2
