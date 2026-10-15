"""`loom login | logout | whoami | call`: everything the dashboard does, from a
terminal or an agent's shell. `python -m loom …` runs it with only the SDK
installed; the server's `loom` command hands these four to it.

`loom call` reaches the whole API with the stored session (or LOOM_API_KEY):

  loom call grants.pending_for_me
  loom call grants.approve namespace=support subject=bob@example.com role=writer
  loom call memory.search query="deploy window" top_k:=5 --namespace support
  loom call /v1/me/service_accounts
  loom call /v1/me/service_accounts.create --data '{"name": "ci", "namespaces": ["support"]}'

An op name is POSTed to /v1/<op> with org and namespace filled in from the
token (`--org`, `--namespace` override). A path starting with `/` is sent as
given: GET without a body, POST with one. `key=value` sends text as it is;
`key:=json` sends a JSON value (`top_k:=5`, `tags:='["a"]'`, `pinned:=true`).
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

COMMANDS = ("login", "logout", "whoami", "call")


def _field(pair: str) -> tuple[str, Any]:
    key, sep, raw = pair.partition("=")
    if not sep or not key or key == ":":
        raise argparse.ArgumentTypeError(f"expected key=value or key:=json, got {pair!r}")
    if not key.endswith(":"):
        return key, raw
    try:
        return key[:-1], json.loads(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{key[:-1]}: not JSON: {raw!r}") from e


def _body(args: argparse.Namespace) -> dict[str, Any] | None:
    data = args.data
    if data == "-":
        data = sys.stdin.read()
    elif data and data.startswith("@"):
        with open(data[1:], encoding="utf-8") as f:
            data = f.read()
    body = json.loads(data) if data else {}
    if not isinstance(body, dict):
        raise ValueError("--data must be a JSON object")
    body.update(dict(args.fields))
    return body if (args.data or args.fields) else None


def _server(args: argparse.Namespace) -> str | None:
    """--server, else LOOM_BASE_URL, else the server `loom login` signed in
    to. None with --profile: that profile's session names its own server."""
    if args.profile:
        return str(args.server) if args.server else None
    return (
        args.server
        or os.environ.get("LOOM_BASE_URL")
        or credentials.signed_in_server()
        or credentials.DEFAULT_SERVER
    ).rstrip("/")


async def _call(args: argparse.Namespace) -> Any:
    from loom.agent import Agent

    body = args.body
    agent = Agent(
        args.org or "",
        args.namespace or "",
        "loom-cli",
        base_url=_server(args),
        profile=args.profile,
    )
    try:
        if not args.target.startswith("/"):
            # The identity an op needs, from the token unless given.
            claims = agent.transport.token_claims()
            agent.org = args.org or (claims.org if claims else None) or "dev"
            agent.namespace = args.namespace or (
                claims.namespaces[0] if claims and claims.namespaces else "local"
            )
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
            if not args.target or not (args.target.startswith("/") or args.target in OPS):
                what = (
                    f"unknown operation {args.target!r}" if args.target else "name an op or a /path"
                )
                print(f"loom call: {what} (`loom call --list` lists the ops)", file=sys.stderr)
                return 2
            result = asyncio.run(_call(args))
            if result is not None:
                print(json.dumps(result, indent=2, default=str))
    except LoomError as e:
        print(f"loom {args.cmd}: {e}", file=sys.stderr)
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
            p.add_argument(
                "fields", nargs="*", type=_field, help="key=text or key:=json body fields"
            )
            p.add_argument("--data", help="a JSON body, @file, or - for stdin")
            p.add_argument("--org", help="org for an op (default: the token's)")
            p.add_argument("--namespace", help="namespace for an op (default: the token's first)")
            p.add_argument("--method", choices=("GET", "POST"), help="for a /path")
            p.add_argument("--list", action="store_true", help="list every op and its role")
    # Fields may follow options (`--namespace x query=…`), which argparse
    # leaves over; anything else left over is a mistake.
    args, extra = parser.parse_known_args(argv)
    if extra and (args.cmd != "call" or any(x.startswith("-") for x in extra)):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if extra:
        try:
            args.fields = [*args.fields, *map(_field, extra)]
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))
    if args.cmd == "call":
        try:
            args.body = _body(args)
        except (ValueError, OSError) as e:
            parser.error(f"--data: {e}")
    return run(args)
