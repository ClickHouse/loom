"""Password accounts carried over to single sign-on.

Self-signup never verifies the email it is given, so an SSO sign-in does not
reach a password account on an email match alone: whoever typed the address
first would own the real person's sign-in. It does once the account is linked
here, by its owner proving the password once from an SSO sign-in, or by an
operator vouching for the domain (`loom sso adopt`). The account keeps its
subject, grants and namespaces; no new one is made.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.ch import get_client

TABLE = "loom.sso_links"

# Active password accounts naming an email and not linked to it.
_UNLINKED = f"""
    SELECT org, namespace, owner_email, contact_email, created_at
    FROM loom.namespaces FINAL
    WHERE active = 1 AND password_hash != '' AND {{where}}
      AND (contact_email, org, namespace) NOT IN (
          SELECT email, org, namespace FROM {TABLE} FINAL WHERE linked = 1
      )
    ORDER BY contact_email, org, namespace
"""


async def unlinked_accounts(email: str) -> list[dict[str, Any]]:
    """The password accounts naming `email` that SSO does not reach yet."""
    return await get_client().query_all(
        _UNLINKED.format(where="contact_email = {email:String}"),
        params={"email": email.strip().lower()},
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


async def link(email: str, accounts: list[tuple[str, str]], linked_by: str) -> None:
    """Let an SSO sign-in with `email` reach each (org, namespace)."""
    await _write(email, accounts, linked_by, linked=1)


async def linked(email: str) -> list[tuple[str, str]]:
    """The (org, namespace) accounts linked to `email`."""
    rows = await get_client().query_all(
        f"SELECT org, namespace FROM {TABLE} FINAL WHERE email = {{email:String}} AND linked = 1",
        params={"email": email.strip().lower()},
    )
    return [(r["org"], r["namespace"]) for r in rows]


async def unlink(email: str, accounts: list[tuple[str, str]], linked_by: str) -> None:
    """Stop SSO sign-ins with `email` reaching `accounts`."""
    await _write(email, accounts, linked_by, linked=0)


async def _write(email: str, accounts: list[tuple[str, str]], by: str, *, linked: int) -> None:
    if not accounts:
        return
    now, version = datetime.now(UTC), time.time_ns()
    await get_client().insert(
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
