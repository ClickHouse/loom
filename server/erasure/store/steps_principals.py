"""A user erasure's identity steps: the grants that name the user, the groups
that list them, and their personal collection.

The user is the principal `user:<user_id>` (server/auth/gateway.py). Each step
rewrites the rows it changes with a newer version, `INSERT ... SELECT *
REPLACE` from an inner FINAL read, so every column it does not change is
carried, and it moves the org's grant watermark so every process drops what it
cached.
"""

from __future__ import annotations

from server.core import grant_cache
from server.core.repo_base import _CHClient, _default_client

_NEW_VERSION = "toUInt64(toUnixTimestamp64Nano(now64(9)))"


async def _rewrite(
    table: str, where: str, replace: str, params: dict[str, str], ch: _CHClient
) -> int:
    """Rewrite the live rows of `table` matching `where` with `replace`;
    returns how many there were."""
    row = await ch.query_one(f"SELECT count() AS n FROM {table} FINAL WHERE {where}", params=params)
    n = int((row or {}).get("n") or 0)
    if n:
        await ch.execute(
            f"INSERT INTO {table} SELECT * REPLACE ({replace}) "
            f"FROM (SELECT * FROM {table} FINAL WHERE {where})",
            params=params,
        )
    return n


async def revoke_grants_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Revoke every live grant naming the user."""
    ch = ch or _default_client()
    n = await _rewrite(
        "loom.namespace_grants",
        "org = {org:String} AND lower(subject) = lower({p:String}) AND revoked_at IS NULL",
        f"toDateTime(now()) AS revoked_at, {_NEW_VERSION} AS version",
        {"org": org, "p": "user:" + user_id},
        ch,
    )
    if n:
        await grant_cache.changed(org)
    return n


async def remove_from_groups_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Take the user out of every group that lists them."""
    ch = ch or _default_client()
    n = await _rewrite(
        "loom.principal_groups",
        "org = {org:String} AND has(members, {p:String}) AND NOT deleted",
        "arrayFilter(m -> m != {p:String}, members) AS members, "
        f"now64(3) AS updated_at, {_NEW_VERSION} AS version",
        {"org": org, "p": "user:" + user_id},
        ch,
    )
    if n:
        await grant_cache.changed(org)
    return n


async def retire_personal_collection_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Take the user's personal collection out of the tree and out of the
    directory. Its documents were the user's: the document steps erased them."""
    ch = ch or _default_client()
    params = {"org": org, "p": "user:" + user_id}
    n = await _rewrite(
        "loom.collections",
        "org = {org:String} AND owner = {p:String} AND personal AND NOT deleted",
        f"true AS deleted, {_NEW_VERSION} AS version",
        params,
        ch,
    )
    await _rewrite(
        "loom.namespaces",
        "org = {org:String} AND kind = 'personal' AND owner_email = {p:String} AND active",
        "false AS active, toDateTime(now()) AS updated_at",
        params,
        ch,
    )
    if n:
        await grant_cache.changed(org)
    return n
