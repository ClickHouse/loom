"""`loom sso adopt | unlink | password-link`: carry password accounts over to
single sign-on, and back off it (server/identity/app/account_admin.py)."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected

_ADOPT_HELP = (
    "Let SSO sign-ins reach the password accounts whose email is at --domain: "
    "each person then signs in with the identity provider to their own account, "
    "its namespaces and data. Vouches that the domain's addresses are the "
    "provider's people. Dry run unless --apply."
)


def register_sso_cli(sub: Any) -> None:
    p_sso = sub.add_parser("sso", help="Carry password accounts over to single sign-on.")
    sso = p_sso.add_subparsers(dest="subcommand", required=True)
    p_adopt = sso.add_parser("adopt", help=_ADOPT_HELP)
    p_adopt.add_argument("--domain", required=True, help="Email domain, e.g. example.com")
    p_adopt.add_argument("--org", default="", help="Only accounts in this org")
    p_adopt.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Leave this email's accounts out, e.g. a bot's (repeatable)",
    )
    p_adopt.add_argument(
        "--keep-tokens",
        action="store_true",
        help="Keep the tokens the accounts already hold (by default they are revoked: "
        "whoever registered an address may hold some)",
    )
    add_apply_flags(p_adopt, writes="link the accounts")
    p_adopt.set_defaults(func=_cmd_adopt)
    p_unlink = sso.add_parser("unlink", help="Undo the links of one email.")
    p_unlink.add_argument("--email", required=True)
    add_apply_flags(p_unlink, writes="remove the links")
    p_unlink.set_defaults(func=_cmd_unlink)
    p_link = sso.add_parser(
        "password-link",
        help="Print a one-time link (a day) that sets the password of an email's SSO "
        "accounts, for after single sign-on is turned off.",
    )
    p_link.add_argument("--email", required=True)
    p_link.add_argument("--org", default="", help="Needed when the email has accounts in several")
    p_link.set_defaults(func=_cmd_password_link)


def _by() -> str:
    return f"operator:{getpass.getuser()}"


def _cmd_adopt(args: argparse.Namespace) -> int:
    from server.identity.app import account_admin

    async def _run(ch: Any) -> int:
        plan = await account_admin.adopt_plan(args.domain, args.org, args.skip)
        print(f"{len(plan)} password account(s) at {args.domain} not linked to SSO:")
        if plan and not args.keep_tokens:
            print(
                "  --apply revokes every token each adopted subject holds, the service-account "
                "and MCP tokens people made from their sessions too (--keep-tokens keeps them)"
            )
        for a in plan:
            print(
                f"  {a['email']:<36} {a['org']}/{a['namespace']:<28} owner {a['owner']:<24} "
                f"created {a['created_at']}" + (f"  SKIP: {a['skip']}" if a["skip"] else "")
            )
        if args.dry_run:
            return 0
        try:
            linked = await account_admin.adopt(plan, by=_by(), keep_tokens=args.keep_tokens)
        except ValueError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        print(f"linked {linked} account(s)")
        return 0

    return run_connected(_run)


def _cmd_unlink(args: argparse.Namespace) -> int:
    from server.auth import sso_links
    from server.identity.app import account_admin

    async def _run(ch: Any) -> int:
        accounts = await sso_links.linked(args.email)
        print(f"{len(accounts)} account(s) linked to {args.email}:")
        for org, namespace in accounts:
            print(f"  {org}/{namespace}")
        if not accounts or args.dry_run:
            return 0
        await account_admin.unlink(args.email, by=_by())
        print(
            f"unlinked {len(accounts)} account(s): new SSO sign-ins no longer reach them; "
            "end open sessions with `loom revoke-token --subject <subject>`"
        )
        return 0

    return run_connected(_run)


def _cmd_password_link(args: argparse.Namespace) -> int:
    from server.identity.app import account_admin
    from server.settings import settings

    async def _run(ch: Any) -> int:
        try:
            code, namespaces = await account_admin.password_link(args.email, args.org, by=_by())
        except ValueError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        base = (settings.oauth_issuer or settings.public_base_url).rstrip("/")
        if not base:
            print(
                "  ! LOOM_OAUTH_ISSUER and LOOM_PUBLIC_BASE_URL are unset: "
                "put this server's address in front of the link",
                file=sys.stderr,
            )
        print(f"{base}/oauth/set-password?code={code}")
        print(
            "  one use, for a day, while single sign-on is off; sets the password of "
            + ", ".join(namespaces)
        )
        return 0

    return run_connected(_run)
