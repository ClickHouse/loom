"""`loom bot create | list | disable`: bots and CI jobs that get tokens (an
hour, or what the request asks up to the bot's maximum) by proving a key or
their CI platform's identity, never a long-lived secret
(server/auth/machines.py, server/identity/app/account_admin.py)."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

from server.cli._ch import run_connected


def register_bot_cli(sub: Any) -> None:
    from server.auth import machines

    p_bot = sub.add_parser(
        "bot", help="Bots and CI jobs that sign in with a key or their CI's identity."
    )
    bot = p_bot.add_subparsers(dest="subcommand", required=True)
    p_create = bot.add_parser(
        "create",
        help="Register a bot: its namespaces, and a key (--generate-key or --public-key) "
        "and/or a CI trust (--issuer, --audience, --claim). Its tokens are for sa:<name>; "
        "with --agent-claim, each agent the trust's tokens name gets sa:<name>:<agent> and "
        "its own namespace <name>-<agent>.",
    )
    p_create.add_argument(
        "--name", required=True, help="Its client_id: lower-case letters, digits, '-'"
    )
    p_create.add_argument("--org", required=True)
    p_create.add_argument(
        "--namespace-role",
        action="append",
        default=[],
        metavar="NS=ROLE",
        help="viewer|writer|admin on a namespace (repeatable; first = searched first); "
        "with --agent-claim, what every agent shares beside its own",
    )
    key = p_create.add_mutually_exclusive_group()
    key.add_argument(
        "--generate-key", metavar="PATH", help="Write a new P-256 private key here (0600)"
    )
    key.add_argument(
        "--public-key",
        metavar="PATH",
        help="The bot's own public key, a PEM file (P-256, Ed25519 or RSA)",
    )
    p_create.add_argument(
        "--issuer", default="", help="CI trust: the platform's OIDC issuer (https)"
    )
    p_create.add_argument(
        "--audience", default="", help="CI trust: the audience its token must name"
    )
    p_create.add_argument(
        "--claim",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="CI trust: a claim the token must carry; VALUE ending in * matches a prefix "
        "(repeatable; at least one, e.g. sub=repo:org/repo:ref:refs/heads/main)",
    )
    p_create.add_argument(
        "--jwks-uri",
        default="",
        help="CI trust: where the issuer publishes its keys (https), when its OIDC "
        "discovery is not reachable from Loom",
    )
    p_create.add_argument(
        "--max-token-age",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="CI trust: the longest a workload token may live (exp less iat) to be taken",
    )
    p_create.add_argument(
        "--max-access-token-ttl",
        type=int,
        default=machines.TOKEN_TTL,
        metavar="SECONDS",
        help="The longest token the bot is issued when its request asks (expires_in, 60 to "
        "86400); unasked, a token lives an hour, or this when it is shorter",
    )
    p_create.add_argument(
        "--max-agents",
        type=int,
        default=1000,
        help="With --agent-claim: how many agents the trust may make",
    )
    p_create.add_argument(
        "--agent-claim",
        default="",
        metavar="CLAIM",
        help="CI trust: the claim naming the agent (e.g. agent_id); each agent gets its own "
        "subject and namespace, made on its first sign-in",
    )
    p_create.add_argument(
        "--platform-admin",
        action="store_true",
        help="Its tokens carry the platform_admin role, which spans every org; it takes no "
        "--namespace-role and no --agent-claim",
    )
    p_create.add_argument("--replace", action="store_true", help="Replace a bot of the same name")
    p_create.set_defaults(func=_cmd_create)
    p_list = bot.add_parser("list", help="Every bot, active or disabled.")
    p_list.set_defaults(func=_cmd_list)
    p_disable = bot.add_parser("disable", help="Refuse its proofs and revoke the tokens it holds.")
    p_disable.add_argument("--name", required=True)
    p_disable.set_defaults(func=_cmd_disable)


def _fail(message: str) -> int:
    print(f"  ✗ {message}", file=sys.stderr)
    return 2


def _cmd_create(args: argparse.Namespace) -> int:
    from server.auth import machines
    from server.auth.grant_claims import NotClaimed
    from server.core.errors import GrantBusyError
    from server.identity.app import account_admin

    try:
        private, public = machines.generate_key() if args.generate_key else (b"", b"")
        if args.public_key:
            public = Path(args.public_key).read_bytes()
        bot = account_admin.bot_from(
            name=args.name,
            org=args.org,
            namespace_roles=args.namespace_role,
            public_key_pem=public.decode(),
            issuer=args.issuer,
            audience=args.audience,
            claims=args.claim,
            agent_claim=args.agent_claim,
            jwks_uri=args.jwks_uri,
            max_token_s=args.max_token_age,
            max_agents=args.max_agents,
            max_access_token_s=args.max_access_token_ttl,
            platform_admin=args.platform_admin,
        )
    except (OSError, ValueError) as e:
        return _fail(str(e))

    async def _run(ch: Any) -> int:
        if args.generate_key:
            # Never over an existing file, and gone again if the bot is refused.
            try:
                fd = os.open(args.generate_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError as e:
                return _fail(str(e))
            with os.fdopen(fd, "wb") as f:
                f.write(private)
        try:
            await account_admin.create_bot(bot, by=f"cli:{getpass.getuser()}", replace=args.replace)
        except (ValueError, GrantBusyError, NotClaimed) as e:  # nothing was saved
            if args.generate_key:
                os.remove(args.generate_key)
            return _fail(str(e))
        print(f"bot {args.name}: tokens for {machines.subject(args.name)} in {args.org}")
        if bot.get("platform_admin", False):
            print("  ! platform_admin: its tokens act in every org")
        if bot["public_keys"]:
            key_file = args.generate_key or "<its private key>"
            print(f"  key:  LOOM_CLIENT_ID={args.name} LOOM_CLIENT_KEY_FILE={key_file}")
        if bot["issuer"]:
            print(f"  CI:   LOOM_CLIENT_ID={args.name} LOOM_WORKLOAD_AUDIENCE={args.audience}")
        return 0

    return run_connected(_run, orgs=(args.org,))


def _cmd_list(args: argparse.Namespace) -> int:
    from server.auth import machines

    async def _run(ch: Any) -> int:
        for r in await machines.listed():
            proofs = ["key"] * bool(r["has_key"]) + [f"CI {r['issuer']}"] * bool(r["issuer"])
            proofs += [f"agents by {r['agent_claim']}"] * bool(r["agent_claim"])
            ttl = int(r["max_access_token_s"])
            limit = f"  max access token {ttl} s" * (ttl != machines.TOKEN_TTL)
            state = "active" if r["active"] else "disabled"
            roles = ", ".join(f"{ns}={role}" for ns, role in r["ns_roles"])
            roles = "platform_admin (every org)" if r.get("platform_admin") else roles
            shown = " + ".join(proofs) + limit
            print(f"  {r['name']:<24} {state:<8} {r['org']:<16} {roles:<40} {shown}")
        return 0

    return run_connected(_run, org_free="lists this cell's bots")


def _cmd_disable(args: argparse.Namespace) -> int:
    from server.identity.app import account_admin

    async def _run(ch: Any) -> int:
        if not await account_admin.disable_bot(args.name, by=f"cli:{getpass.getuser()}"):
            return _fail(f"no bot {args.name!r}")
        print(f"bot {args.name} disabled; its tokens are revoked")
        return 0

    return run_connected(_run, org_free="a bot of this cell, by name")
