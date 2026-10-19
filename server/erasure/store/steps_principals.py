"""A user erasure's identity steps in the store: the groups that list the user
and their personal collection's directory row (the grants that name them are
revoked through `server.identity.app.grants.revoke_subject`).

The user is the principal `user:<user_id>` (server/auth/gateway.py). Each step
rewrites the rows it changes with a newer version, `INSERT ... SELECT *
REPLACE` from an inner FINAL read, so every column it does not change is
carried; a grant or a group change moves the org's grant watermark so every
process drops what it cached.
"""

from __future__ import annotations

from server.core import grant_cache
from server.core.repo_base import _CHClient, _default_client

# Newer than the row it replaces whatever the server's clock says: the writers
# version from the app's clock, which may run ahead.
_NEW_VERSION = "greatest(version + 1, toUInt64(toUnixTimestamp64Nano(now64(9))))"


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
    """Take the user's personal collection out of the directory. Its documents
    were the user's own: the document steps erased them."""
    from server.core.principals import personal_name

    ch = ch or _default_client()
    return await _rewrite(
        "loom.namespaces",
        "org = {org:String} AND namespace = {ns:String} AND active",
        "false AS active, toDateTime(now()) AS updated_at",
        {"org": org, "ns": personal_name(org, "user:" + user_id)},
        ch,
    )
