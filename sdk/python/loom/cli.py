"""`loom login | logout | whoami | call | token`: everything the dashboard does,
from a terminal or an agent's shell. `python -m loom …` runs it with only the SDK
installed; the server's `loom` command hands these five to it.

`loom call` reaches the whole API with the stored session (or LOOM_API_KEY):

  loom call grants.pending_for_me
  loom call grants.approve namespace=support subject=bob@example.com decision=approve
  loom call memory.search query="deploy window" top_k:=5 --namespace support
  loom call /v1/me/service_accounts
  loom call /v1/me/service_accounts.create --data '{"name": "ci", "namespaces": ["support"]}'

`loom token` makes, lists and revokes your own tokens (`loom token --help`).

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

COMMANDS = ("login", "logout", "whoami", "call", "token")


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


async def _token(args: argparse.Namespace) -> Any:
    """`loom token create | list | revoke` with the stored session."""
    from loom.agent import Agent

    agent = Agent("", "", "loom-cli", base_url=_server(args), profile=args.profile)
    try:
        if args.action == "list":
            return await agent.account.tokens()
        if args.action == "revoke":
            return await agent.account.revoke_token(args.name)
        return await agent.account.create_token(
            args.entries or "*", name=args.name, ttl_days=args.ttl_days
        )
    finally:
        await agent.close()


def _role_entry(value: str) -> dict[str, str]:
    ns, sep, role = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected NS=ROLE, got {value!r}")
    return {"namespace": ns, "role": role}


_TOKEN_HELP = """Your tokens for agents, scripts and CI, as the dashboard's Settings make
them. With no --namespace a token reads every namespace you belong to, now and
later; with some, it reads only those, in the order you name them (first =
searched first), at a role no higher than your sign-in can give. The answer
holds the token once, each MCP client's setup, and where calls that name no
namespace save (`saves_to`).

  loom token create                                  # everything you belong to
  loom token create --name ci --namespace-role support=writer --namespace docs
  loom token create --name nightly --namespace support --ttl-days 0   # never expires
  loom token list
  loom token revoke --name ci
"""


def _token_parser(sub: Any) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profile", help="credentials profile (default: LOOM_PROFILE or 'default')"
    )
    common.add_argument("--server", help="server URL (default: the one `loom login` signed in to)")
    p = sub.add_parser(
        "token",
        help="make, list and revoke your tokens for CI and agents",
        description=_TOKEN_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = p.add_subparsers(dest="action", required=True)
    create = actions.add_parser(
        "create",
        parents=[common],
        help="a token that reads everything you belong to, or only the namespaces named",
    )
    create.add_argument(
        "--name", help="its name, unique in the org while it lives (default: token-<day>-<hex>)"
    )
    # Both into one list, so their order on the command line is the read priority.
    create.add_argument(
        "--namespace-role",
        dest="entries",
        action="append",
        type=_role_entry,
        metavar="NS=ROLE",
        help="a namespace and the role there (repeatable)",
    )
    create.add_argument(
        "--namespace",
        dest="entries",
        action="append",
        metavar="NS",
        help="a namespace at the most your sign-in can give there (repeatable); '*' reads "
        "everything you belong to, the default",
    )
    create.add_argument(
        "--ttl-days", type=int, default=None, help="days it lives (default 90; 0: never expires)"
    )
    actions.add_parser("list", parents=[common], help="the tokens you made")
    revoke = actions.add_parser("revoke", parents=[common], help="end a token now")
    revoke.add_argument("--name", required=True)


def _list_ops() -> None:
    for name, spec in sorted(OPS.items()):
        print(f"{name:<40} {spec.role}")


def run(args: argparse.Namespace) -> int:
    """Run one parsed command: prints the answer, returns the exit code."""
    try:
        if args.cmd == "login":
            credentials.login(args.server, profile=args.profile, open_browser=not args.no_browser)
        elif args.cmd == "logout":
            confirmed = credentials.logout(args.profile)
            print("Signed out." if confirmed else "Signed out here; the server did not confirm.")
        elif args.cmd == "whoami":
            print(json.dumps(credentials.whoami(args.profile), indent=2))
        elif args.cmd == "token":
            print(json.dumps(asyncio.run(_token(args)), indent=2, default=str))
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
    except (LoomError, httpx.HTTPError, ValueError, OSError) as e:
        print(f"loom {args.cmd}: {e}", file=sys.stderr)
        for err in getattr(e, "detail", {}).get("errors") or []:
            where = ".".join(str(part) for part in err.get("loc", []))
            print(f"  {where}: {err.get('msg', err)}", file=sys.stderr)
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
    _token_parser(sub)
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
