"""Password accounts carried over to single sign-on.

Self-signup never verifies the email it is given, so an SSO sign-in does not
reach a password account on an email match alone: whoever typed the address
first would own the real person's sign-in. It does once the account is linked
here, by its owner proving the password once from an SSO sign-in, or by an
operator vouching for the domain (`loom sso adopt`). The account keeps its
subject, grants and namespaces; no new one is made.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from server.auth import aliases
from server.core import grant_cache
from server.core.ch import get_client
from server.core.versions import next_version

TABLE = "loom.sso_links"

# Active password accounts naming an email and not linked to it.
_UNLINKED = f"""
    SELECT org, namespace, owner_email, contact_email, created_at, password_hash
    FROM loom.namespaces FINAL
    WHERE active = 1 AND password_hash != '' AND {{where}}
      AND (contact_email, org, namespace) NOT IN (
          SELECT email, org, namespace FROM {TABLE} FINAL WHERE linked = 1
      )
    ORDER BY contact_email, org, namespace
"""


async def unlinked_accounts(email: str) -> list[dict[str, Any]]:
    """The password accounts naming `email` that SSO does not reach yet."""
    email = email.strip().lower()
    if not email:  # accounts with no email are nobody's to link
        return []
    return await get_client().query_all(
        _UNLINKED.format(where="contact_email = {email:String}"),
        params={"email": email},
    )


async def unlinked_in_domain(domain: str, org: str = "") -> list[dict[str, Any]]:
    """`unlinked_accounts` for every email at `domain` (in `org`, if given)."""
    where = "endsWith(contact_email, {suffix:String})"
    if org:
        where += " AND org = {org:String}"
    return await get_client().query_all(
        _UNLINKED.format(where=where),
        params={"suffix": "@" + domain.strip().lower().lstrip("@"), "org": org},
    )


async def reached_by_sso(emails: list[str]) -> set[str]:
    """Which of `emails` an SSO sign-in already reaches: an SSO account, or a link."""
    if not emails:
        return set()
    rows = await get_client().query_all(
        f"""
        SELECT contact_email AS email FROM loom.namespaces FINAL
        WHERE active = 1 AND password_hash = '' AND contact_email IN {{emails:Array(String)}}
        UNION DISTINCT
        SELECT email FROM {TABLE} FINAL WHERE linked = 1 AND email IN {{emails:Array(String)}}
        """,
        params={"emails": emails},
    )
    return {r["email"] for r in rows}


async def link(email: str, accounts: list[tuple[str, str]], linked_by: str) -> None:
    """Let an SSO sign-in with `email` reach each (org, namespace)."""
    await _write(email, accounts, linked_by, linked=1)


async def link_live(
    email: str, accounts: list[tuple[str, str]], linked_by: str
) -> list[tuple[str, str]]:
    """`link` the accounts still active and naming `email`, each held as a
    release holds it: one released since it was found keeps no link for the
    next holder of its name. Returns those linked."""
    from server.auth.grant_claims import account_change

    async with AsyncExitStack() as held:
        for org, namespace in sorted(set(accounts)):
            await held.enter_async_context(account_change(org, namespace))
        rows = await get_client().query_all(
            "SELECT org, namespace FROM loom.namespaces FINAL WHERE active = 1 "
            "AND contact_email = {email:String} "
            "AND (org, namespace) IN {accounts:Array(Tuple(String, String))}",
            params={"email": email.strip().lower(), "accounts": accounts},
        )
        found = {(r["org"], r["namespace"]) for r in rows}
        live = [a for a in accounts if a in found]
        await link(email, live, linked_by)
    return live


async def linked(email: str) -> list[tuple[str, str]]:
    """The (org, namespace) accounts linked to `email`."""
    rows = await get_client().query_all(
        f"SELECT org, namespace FROM {TABLE} FINAL WHERE email = {{email:String}} AND linked = 1",
        params={"email": email.strip().lower()},
    )
    return [(r["org"], r["namespace"]) for r in rows]


async def forget_account(org: str, namespace: str, by: str, *, ch: Any = None) -> None:
    """Drop every link to (org, namespace): it was released, and a next holder
    of the name is not the person who linked it."""
    ch = ch or get_client()
    rows = await ch.query_all(
        f"SELECT email FROM {TABLE} FINAL WHERE org = {{org:String}} "
        "AND namespace = {ns:String} AND linked = 1",
        params={"org": org, "ns": namespace},
    )
    for r in rows:
        await _write(r["email"], [(org, namespace)], by, linked=0, ch=ch)


async def reaching(email: str) -> list[tuple[str, str]]:
    """The linked accounts an SSO sign-in with `email` would reach: active, and
    still naming the email (a deactivated or released one reaches nothing)."""
    rows = await get_client().query_all(
        f"""
        SELECT org, namespace FROM loom.namespaces FINAL
        WHERE contact_email = {{email:String}} AND active = 1
          AND (org, namespace) IN (
              SELECT org, namespace FROM {TABLE} FINAL WHERE email = {{email:String}} AND linked = 1
          )
        """,
        params={"email": email.strip().lower()},
    )
    return [(r["org"], r["namespace"]) for r in rows]


async def unlink(email: str, accounts: list[tuple[str, str]], linked_by: str) -> None:
    """Stop SSO sign-ins with `email` reaching `accounts`."""
    await _write(email, accounts, linked_by, linked=0)


async def _write(
    email: str, accounts: list[tuple[str, str]], by: str, *, linked: int, ch: Any = None
) -> None:
    if not accounts:
        return
    ch = ch or get_client()
    # Past the email's newest link row, whichever replica's clock wrote it: under
    # FINAL a later unlink with a lower version would be lost.
    version = await next_version(ch, TABLE, {"email": email.strip().lower()})
    now = datetime.now(UTC)
    # A link verifies the account's email, which ties it to the owner's grants
    # (`server.auth.aliases`): an unlink must not leave a cached tie admitting,
    # even when its reply is lost after the row landed.
    orgs = sorted({org for org, _ in accounts})
    async with AsyncExitStack() as announced:
        for org in orgs:
            await announced.enter_async_context(grant_cache.announced(org))
        try:
            await ch.insert(
                TABLE,
                [
                    {
                        "email": email.strip().lower(),
                        "org": org,
                        "namespace": namespace,
                        "linked": linked,
                        "linked_by": by,
                        "linked_at": now,
                        "version": version,
                    }
                    for org, namespace in accounts
                ],
            )
        finally:
            for org in orgs:
                aliases.invalidate_org(org)
