"""A user erasure's identity steps in the store: the groups that list the user
and their personal collection's directory row (the grants that name them are
revoked through `server.identity.app.grants.revoke_subject`).

The user is the principal `user:<user_id>` (server/auth/gateway.py). Each step
rewrites the rows it changes with a newer version, `INSERT ... SELECT *
REPLACE` from an inner FINAL read, so every column it does not change is
carried. The saga holds the claim an online writer of the same rows holds and
moves the org's grant watermark (server/erasure/app/saga_steps.py).
"""

from __future__ import annotations

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


async def groups_of_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> list[str]:
    """The live groups that list the user."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT group_id FROM loom.principal_groups FINAL"
        " WHERE org = {org:String} AND has(members, {p:String}) AND NOT deleted",
        params={"org": org, "p": "user:" + user_id},
    )
    return [str(r["group_id"]) for r in rows]


async def remove_from_group(
    org: str, group_id: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Take the user out of one group."""
    return await _rewrite(
        "loom.principal_groups",
        "org = {org:String} AND group_id = {g:String} AND has(members, {p:String}) AND NOT deleted",
        "arrayFilter(m -> m != {p:String}, members) AS members, "
        f"now64(3) AS updated_at, {_NEW_VERSION} AS version",
        {"org": org, "g": group_id, "p": "user:" + user_id},
        ch or _default_client(),
    )


async def retire_personal_collection_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Take the user's personal collection out of the directory, and the
    user's id off its row. Its documents were the user's own: the document
    steps erased them."""
    from server.core.principals import personal_name

    ch = ch or _default_client()
    return await _rewrite(
        "loom.namespaces",
        "org = {org:String} AND namespace = {ns:String} AND active",
        # `updated_at` is the directory's version: newer than the row's own.
        "false AS active, '' AS owner_email, '' AS description, "
        "greatest(updated_at + 1, toDateTime(now())) AS updated_at",
        {"org": org, "ns": personal_name(org, "user:" + user_id)},
        ch,
    )


async def forget_collection_owner_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Blank the user as the creator of the collections they made: in the tree
    and in the directory. The collections and other writers' documents stay."""
    ch = ch or _default_client()
    params = {"org": org, "p": "user:" + user_id}
    n = await _rewrite(
        "loom.collections",
        "org = {org:String} AND owner = {p:String}",
        f"'' AS owner, {_NEW_VERSION} AS version",
        params,
        ch,
    )
    await _rewrite(
        "loom.namespaces",
        "org = {org:String} AND kind = 'collection' AND owner_email = {p:String}",
        "'' AS owner_email, greatest(updated_at + 1, toDateTime(now())) AS updated_at",
        params,
        ch,
    )
    return n
