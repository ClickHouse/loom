"""`loom sso adopt | link | unlink | password-link`: carry password accounts over to
single sign-on, and back off it (server/identity/app/account_admin.py)."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import dry_run_orgs, run_connected

_ADOPT_HELP = (
    "Let SSO sign-ins reach the password accounts whose email is at --domain: "
    "each person then signs in with the identity provider to their own account, "
    "its namespaces and data. Vouches that the domain's addresses are the "
    "provider's people. Dry run unless --apply."
)


def _duration(seconds: int) -> str:
    """`1 day`, `6 h` or `45 min`: the largest whole unit."""
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day" + ("s" if days != 1 else "")
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


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
    p_link = sso.add_parser(
        "link",
        help="Let one person's SSO sign-ins reach their password account registered under "
        "another address (a personal one, a +tag). The account keeps its subject, grants, "
        "namespaces and tokens; its contact email becomes --email.",
    )
    p_link.add_argument(
        "--email", required=True, help="the address they sign in with at the provider"
    )
    p_link.add_argument("--org", required=True)
    p_link.add_argument("--namespace", required=True, help="the account's namespace")
    add_apply_flags(p_link, writes="change its email and link it")
    p_link.set_defaults(func=_cmd_link)
    p_unlink = sso.add_parser(
        "unlink", help="Undo the links of one email: its grants stop admitting the accounts."
    )
    p_unlink.add_argument("--email", required=True)
    add_apply_flags(p_unlink, writes="remove the links")
    p_unlink.set_defaults(func=_cmd_unlink)
    p_password_link = sso.add_parser(
        "password-link",
        help="Print a one-time link (valid for LOOM_PASSWORD_LINK_TTL_SECONDS, a day by "
        "default) that sets the password of an email's SSO accounts, for after single "
        "sign-on is turned off.",
    )
    p_password_link.add_argument("--email", required=True)
    p_password_link.add_argument(
        "--org", default="", help="Needed when the email has accounts in several"
    )
    p_password_link.set_defaults(func=_cmd_password_link)


def _by() -> str:
    return f"operator:{getpass.getuser()}"


def _cmd_adopt(args: argparse.Namespace) -> int:
    from server.identity.app import account_admin

    async def _run(ch: Any) -> int:
        try:
            plan = await account_admin.adopt_plan(args.domain, args.org, args.skip)
        except ValueError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        print(f"{len(plan)} password account(s) at {args.domain} not linked to SSO:")
        if any(not a["skip"] for a in plan) and not args.keep_tokens:
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

    return run_connected(_run, orgs=dry_run_orgs(args))


def _carried_line(was: str, owner: str, names: list[str], elsewhere: bool, *, dry_run: bool) -> str:
    """The line naming the grants a link carries to `owner`, revoked."""
    if elsewhere:
        lead = f"{was} is another account's verified email: its grants stay theirs, and are "
    else:
        lead = f"revoked under {was}, so " if dry_run else ""
    return f"  {lead}revoked for {owner} too: {', '.join(names)}"


def _cmd_link(args: argparse.Namespace) -> int:
    from server.core.errors import LoomServerError
    from server.identity.app import account_admin

    async def _run(ch: Any) -> int:
        try:
            plan = await account_admin.link_plan(args.email, args.org, args.namespace)
            row = plan["row"]
            print(
                f"{args.org}/{args.namespace} (owner {row['owner_email']}, email "
                f"{plan['was'] or '-'}) would sign in as {args.email.strip().lower()}"
            )
            owner = row["owner_email"]
            if args.dry_run:
                if plan["revoked"]:
                    names = sorted({g["namespace"] for g in plan["revoked"]})
                    print(_carried_line(plan["was"], owner, names, plan["elsewhere"], dry_run=True))
                return 0
            # What the link read under the account and email claims, not the plan above.
            was, carried, elsewhere = await account_admin.link_account(
                args.email, args.org, args.namespace, by=_by()
            )
            if carried:
                print(_carried_line(was, owner, carried, elsewhere, dry_run=False))
        except (ValueError, LoomServerError) as e:  # refused (fix what it names) or busy (rerun)
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        print("linked: their next SSO sign-in lands on this account")
        return 0

    return run_connected(_run, orgs=dry_run_orgs(args))


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
            f"unlinked {len(accounts)} account(s): new SSO sign-ins no longer reach them, "
            f"and grants under {args.email} stop admitting them; "
            "end open sessions with `loom revoke-token --subject <subject>`"
        )
        return 0

    return run_connected(_run, org_free="unlinks an email's accounts in this cell")


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
            f"  one use, for {_duration(settings.password_link_ttl_seconds)}, while single "
            "sign-on is off; sets the password of " + ", ".join(namespaces)
        )
        return 0

    return run_connected(_run, orgs=(args.org,))
