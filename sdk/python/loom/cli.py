"""`loom login | logout | whoami | call`: everything the dashboard does, from a
terminal or an agent's shell. `python -m loom …` runs it with only the SDK
installed; the server's `loom` command hands these four to it.

`loom call` reaches the whole API with the stored session (or LOOM_API_KEY):

  loom call grants.pending_for_me
  loom call grants.approve namespace=support subject=bob@example.com role=writer
  loom call memory.search query="deploy window" top_k=5 --namespace support
  loom call /v1/me/service_accounts
  loom call /v1/me/service_accounts.create --data '{"name": "ci", "namespaces": ["support"]}'

An op name is POSTed to /v1/<op> with org and namespace filled in from the
token (`--org`, `--namespace` override). A path starting with `/` is sent as
given: GET without a body, POST with one. `key=value` values are read as JSON
when they parse (`top_k=5`, `tags='["a"]'`), else as text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from loom import credentials
from loom.errors import LoomError
from loom.ops import OPS
from loom.token import decode_token_claims
from loom.transport import token_from_file

COMMANDS = ("login", "logout", "whoami", "call")


def _field(pair: str) -> tuple[str, Any]:
    key, sep, raw = pair.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected key=value, got {pair!r}")
    try:
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def _body(args: argparse.Namespace) -> dict[str, Any] | None:
    data = args.data
    if data == "-":
        data = sys.stdin.read()
    elif data and data.startswith("@"):
        with open(data[1:], encoding="utf-8") as f:
            data = f.read()
    body: dict[str, Any] = json.loads(data) if data else {}
    body.update(dict(args.fields))
    return body if (args.data or args.fields) else None


def _server(args: argparse.Namespace) -> str:
    """--server, else LOOM_BASE_URL, else the server this profile signed in to."""
    return (
        args.server
        or os.environ.get("LOOM_BASE_URL")
        or credentials.signed_in_server(args.profile)
        or credentials.DEFAULT_SERVER
    ).rstrip("/")


async def _call(args: argparse.Namespace) -> Any:
    from loom.agent import Agent

    server = _server(args)
    token = os.environ.get("LOOM_API_KEY") or (
        credentials.access_token(args.profile)
        if credentials.stored_login(server) is not None
        else token_from_file()
    )
    claims = decode_token_claims(token)
    org = args.org or (claims.org if claims else None) or "dev"
    namespace = args.namespace or (
        claims.namespaces[0] if claims and claims.namespaces else "local"
    )
    agent = Agent(org, namespace, "loom-cli", base_url=server)
    try:
        body = _body(args)
        if not args.target.startswith("/"):
            return await agent.call(args.target, body or {})
        method = args.method or ("POST" if body is not None else "GET")
        resp = await agent.transport.request(method, args.target, json=body)
        if "json" in resp.headers.get("content-type", ""):
            return resp.json()
        sys.stdout.buffer.write(resp.content)
        return None
    finally:
        await agent.close()


def _list_ops() -> None:
    for name, spec in sorted(OPS.items()):
        print(f"{name:<40} {spec.role}")


def run(args: argparse.Namespace) -> int:
    """Run one parsed command: prints the answer, returns the exit code."""
    if args.profile:
        os.environ["LOOM_PROFILE"] = args.profile  # the session `Agent` picks up
    try:
        if args.cmd == "login":
            info = credentials.login(
                args.server, profile=args.profile, open_browser=not args.no_browser
            )
            print(f"Signed in to {info['server']} as {info.get('subject')} ({info.get('org')}).")
        elif args.cmd == "logout":
            confirmed = credentials.logout(args.profile)
            print("Signed out." if confirmed else "Signed out here; the server did not confirm.")
        elif args.cmd == "whoami":
            print(json.dumps(credentials.whoami(args.profile), indent=2))
        elif args.list:
            _list_ops()
        else:
            if not args.target:
                print(
                    "loom call: name an op or a /path (`loom call --list` lists the ops)",
                    file=sys.stderr,
                )
                return 2
            result = asyncio.run(_call(args))
            if result is not None:
                print(json.dumps(result, indent=2, default=str))
    except LoomError as e:
        detail = f" ({e.code})" if getattr(e, "code", "") else ""
        print(f"loom {args.cmd}: {e}{detail}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, ValueError, OSError) as e:
        print(f"loom {args.cmd}: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None, prog: str = "python -m loom") -> int:
    parser = argparse.ArgumentParser(prog=prog, description="Use Loom from the command line.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("login", "sign in on any browser with a one-time code"),
        ("logout", "end the session on the server and forget it here"),
        ("whoami", "show who the stored session signs you in as"),
        ("call", "call any operation or API route with the stored session"),
    ):
        p = sub.add_parser(
            name,
            help=help_text,
            description=__doc__ if name == "call" else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("--profile", help="credentials profile (default: LOOM_PROFILE or 'default')")
        if name in ("login", "call"):
            p.add_argument("--server", help="server URL (default: LOOM_BASE_URL or localhost)")
        if name == "login":
            p.add_argument("--no-browser", action="store_true", help="only print the URL")
        if name == "call":
            p.add_argument("target", nargs="?", help="an op name (memory.search) or a /path")
            p.add_argument("fields", nargs="*", type=_field, help="key=value body fields")
            p.add_argument("--data", help="a JSON body, @file, or - for stdin")
            p.add_argument("--org", help="org for an op (default: the token's)")
            p.add_argument("--namespace", help="namespace for an op (default: the token's first)")
            p.add_argument("--method", choices=("GET", "POST"), help="for a /path")
            p.add_argument("--list", action="store_true", help="list every op and its role")
    return run(parser.parse_args(argv))
