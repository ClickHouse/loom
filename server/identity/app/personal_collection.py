"""Each end user's personal collection: where their saved items live.

A request names it `@me`; only the server maps that to a namespace, from the
user the gateway asserted, so no request field can put a row into another
user's collection. It is created on first use with one grant, admin to its
user, and takes no other (`grants.create` refuses one).
"""

from __future__ import annotations

import hashlib

from server.auth.grant_claims import account_change, collections_change
from server.auth.sharing import PERSONAL_PREFIX
from server.core import grant_cache
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.identity.api.models import NamespaceRegisterInput
from server.identity.app.namespace import insert_admin_grant, registry_row
from server.identity.store import collections as collections_repo

ALIAS = "@me"


def name_for(org: str, subject: str) -> str:
    return PERSONAL_PREFIX + hashlib.sha256(f"{org}\0{subject}".encode()).hexdigest()[:24]


async def personal_collection(org: str, subject: str) -> str:
    """The personal collection of `subject` in `org`, created if it is not yet
    (or no longer is: an erasure retires it)."""
    namespace = name_for(org, subject)

    async def _exists(org: str) -> bool:
        return await collections_repo.fetch(org, namespace) is not None

    # Cached with the org's grants: the erasure that retires it moves the watermark.
    if await grant_cache.org_value(f"personal:{namespace}", org, _exists):
        return namespace
    async with collections_change(org), account_change(org, namespace):
        if await _exists(org):
            return namespace
        ch = get_client()
        row = registry_row(
            NamespaceRegisterInput(
                org=org, namespace=namespace, owner_email=subject, description="Saved items"
            ),
            None,
            naive_utc_now_seconds(),
        )
        row["kind"] = "personal"
        await ch.insert("loom.namespaces", [row])
        await collections_repo.write(
            collections_repo.collection_row(org, namespace, personal=True, owner=subject), ch=ch
        )
        # The grant write moves the org's watermark, so the cached "not yet" goes too.
        await insert_admin_grant(ch, org, namespace, subject)
    return namespace
