"""`loom sso adopt` and `loom sso unlink`: carry password accounts over to
single sign-on (server/auth/sso_links.py)."""

from __future__ import annotations

import argparse
import getpass
from collections import Counter
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


def _cmd_adopt(args: argparse.Namespace) -> int:
    from server.auth import sso_links

    async def _run(ch: Any) -> int:
        rows = await sso_links.unlinked_in_domain(args.domain, args.org)
        orgs = Counter((r["contact_email"], r["org"]) for r in rows)
        spread = Counter(email for email, _ in orgs)
        print(f"{len(rows)} password account(s) at {args.domain} not linked to SSO:")
        for r in rows:
            # An email in several orgs resolves to none of them (sign-in fails closed).
            flag = "  several orgs: will not sign in" if spread[r["contact_email"]] > 1 else ""
            print(
                f"  {r['contact_email']:<40} {r['org']}/{r['namespace']:<32} "
                f"created {str(r['created_at'])[:10]}{flag}"
            )
        if not rows or args.dry_run:
            return 0
        await _link_all(rows)
        print(f"linked {len(rows)} account(s)")
        return 0

    return run_connected(_run)


async def _link_all(rows: list[dict[str, Any]]) -> None:
    from server.auth import sso_links
    from server.observability import audit

    by = f"operator:{getpass.getuser()}"
    accounts: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        accounts.setdefault(r["contact_email"], []).append((r["org"], r["namespace"]))
    for email, linked in accounts.items():
        await sso_links.link(email, linked, by)
        await audit.emit(
            "auth.sso_linked",
            org=linked[0][0],
            namespace="-",
            actor=by,
            subject={"email": email, "by": by, "namespaces": [ns for _, ns in linked]},
        )


def _cmd_unlink(args: argparse.Namespace) -> int:
    from server.auth import sso_links
    from server.observability import audit

    async def _run(ch: Any) -> int:
        by = f"operator:{getpass.getuser()}"
        accounts = await sso_links.linked(args.email)
        print(f"{len(accounts)} account(s) linked to {args.email}:")
        for org, namespace in accounts:
            print(f"  {org}/{namespace}")
        if not accounts or args.dry_run:
            return 0
        await sso_links.unlink(args.email, accounts, by)
        for org, namespace in accounts:
            await audit.emit(
                "auth.sso_unlinked",
                org=org,
                namespace=namespace,
                actor=by,
                subject={"email": args.email.strip().lower()},
            )
        print(
            f"unlinked {len(accounts)} account(s): new SSO sign-ins no longer reach them; "
            "end open sessions with `loom revoke-token --subject <subject>`"
        )
        return 0

    return run_connected(_run)


def _cmd_password_link(args: argparse.Namespace) -> int:
    from server.auth import password_setup
    from server.settings import settings

    async def _run(ch: Any) -> int:
        accounts = await password_setup.sso_accounts(args.email, args.org)
        orgs = {a["org"] for a in accounts}
        if not accounts:
            print(f"  ✗ {args.email} has no SSO account (one without a password)")
            return 1
        if len(orgs) > 1:
            print(f"  ✗ {args.email} has accounts in {sorted(orgs)}; pass --org")
            return 2
        code = await password_setup.make_link(args.email, accounts)
        base = (settings.oauth_issuer or settings.public_base_url).rstrip("/")
        print(f"{base}/oauth/set-password?code={code}")
        print(
            f"  one use, for a day, while single sign-on is off; sets the password of "
            f"{', '.join(a['namespace'] for a in accounts)}"
        )
        return 0

    return run_connected(_run)
