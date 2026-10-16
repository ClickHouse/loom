"""`loom sso adopt` and `loom sso unlink`: carry password accounts over to
single sign-on (server/auth/sso_links.py)."""

from __future__ import annotations

import argparse
import getpass
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


def _cmd_adopt(args: argparse.Namespace) -> int:
    from server.auth import sso_links

    async def _run(ch: Any) -> int:
        rows = await sso_links.unlinked_in_domain(args.domain, args.org)
        by_email: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_email.setdefault(r["contact_email"], []).append(r)
        on_sso = await sso_links.reached_by_sso(list(by_email))
        print(f"{len(rows)} password account(s) at {args.domain} not linked to SSO:")
        adoptable = []
        for email, accounts in by_email.items():
            skip = _skip_reason(accounts, email in on_sso)
            for r in accounts:
                print(
                    f"  {email:<36} {r['org']}/{r['namespace']:<28} owner {r['owner_email']:<24} "
                    f"created {str(r['created_at'])[:10]}" + (f"  SKIP: {skip}" if skip else "")
                )
            if not skip:
                adoptable.append((email, accounts))
        if not adoptable or args.dry_run:
            return 0
        await _link_all(adoptable, keep_tokens=args.keep_tokens)
        print(f"linked {sum(len(a) for _, a in adoptable)} account(s)")
        return 0

    return run_connected(_run)


def _skip_reason(accounts: list[dict[str, Any]], on_sso: bool) -> str:
    """Why an email's accounts are not adopted; '' to adopt them. Each of these
    would let an SSO sign-in land on someone else's subject, or sign in to none."""
    if on_sso:
        return "already signs in with SSO; link by its password instead"
    if len({a["org"] for a in accounts}) > 1:
        return "accounts in several orgs"
    if len({a["owner_email"] for a in accounts}) > 1:
        return "accounts of several owners"
    return ""


async def _link_all(
    adoptable: list[tuple[str, list[dict[str, Any]]]], *, keep_tokens: bool
) -> None:
    from datetime import UTC, datetime

    from server.auth import revocation, sso_links
    from server.observability import audit

    by, now = f"operator:{getpass.getuser()}", datetime.now(UTC)
    for email, accounts in adoptable:
        org, owner = accounts[0]["org"], accounts[0]["owner_email"]
        await sso_links.link(email, [(org, a["namespace"]) for a in accounts], by)
        if not keep_tokens:
            # Whoever registered the address may hold tokens from before; the
            # person signs in again through the provider.
            await revocation.revoke(owner, issued_before=now, reason="sso adopt", revoked_by=by)
        await audit.emit(
            "auth.sso_linked",
            org=org,
            namespace="-",
            actor=by,
            subject={
                "email": email,
                "by": by,
                "subject": owner,
                "namespaces": [a["namespace"] for a in accounts],
                "tokens_revoked": not keep_tokens,
            },
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
    from server.identity.app import password_link
    from server.settings import settings

    async def _run(ch: Any) -> int:
        accounts = await password_link.sso_accounts(args.email, args.org)
        orgs = {a["org"] for a in accounts}
        if not accounts:
            print(f"  ✗ {args.email} has no SSO account (one without a password)")
            return 1
        if len(orgs) > 1:
            print(f"  ✗ {args.email} has accounts in {sorted(orgs)}; pass --org")
            return 2
        code = await password_link.make_link(args.email, accounts)
        base = (settings.oauth_issuer or settings.public_base_url).rstrip("/")
        print(f"{base}/oauth/set-password?code={code}")
        print(
            f"  one use, for a day, while single sign-on is off; sets the password of "
            f"{', '.join(a['namespace'] for a in accounts)}"
        )
        return 0

    return run_connected(_run)
