"""A user erasure's identity steps in the store: the groups that list the user,
the grant rows naming them, the collections they created and their personal
collection's directory row (their live grants are revoked first, through
`server.identity.app.grants.revoke_subject`).

The user is the principal `user:<user_id>` (server/auth/gateway.py). The group,
tree and directory steps rewrite the rows they change with a newer version,
`INSERT ... SELECT * REPLACE` from an inner FINAL read, so every column they do
not change is carried, and the saga holds the claim an online writer of the same
rows holds; it moves the groups watermark after a group change. The grant rows,
already revoked under each grant's claim, are deleted
(server/erasure/app/saga_steps.py).
"""

from __future__ import annotations

from server.core.repo_base import _CHClient, _default_client

# Newer than the row it replaces whatever the server's clock says: the writers
# version from the app's clock, which may run ahead.
_NEW_VERSION = "greatest(version + 1, toUInt64(toUnixTimestamp64Nano(now64(9))))"


async def _rewrite(
    table: str, where: str, replace: str, params: dict[str, str], ch: _CHClient
) -> int:
    """Rewrite the live rows of `table` matching `where` with `replace`, which
    makes them stop matching; returns how many there were. A rewrite whose
    rows still match afterwards did not land: the step fails, and the saga
    runs it again."""
    from server.core.ch import own_write_reads

    count = f"SELECT count() AS n FROM {table} FINAL WHERE {where}"
    with own_write_reads():
        n = int(((await ch.query_one(count, params=params)) or {}).get("n") or 0)
    if not n:
        return 0
    await ch.execute(
        f"INSERT INTO {table} SELECT * REPLACE ({replace}) "
        f"FROM (SELECT * FROM {table} FINAL WHERE {where})",
        params=params,
    )
    with own_write_reads():
        left = int(((await ch.query_one(count, params=params)) or {}).get("n") or 0)
    if left:
        raise RuntimeError(f"{table}: {left} of {n} rows still match after their rewrite")
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
        "false AS active, '' AS owner_email, '' AS description, '' AS title, "
        "greatest(updated_at + 1, toDateTime(now())) AS updated_at",
        {"org": org, "ns": personal_name(org, "user:" + user_id)},
        ch,
    )


async def collections_created_by(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> list[str]:
    """The collections whose tree row or directory row names the user as their
    creator (a create cut short has the first without the second)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT namespace FROM ("
        " SELECT namespace FROM loom.namespaces FINAL WHERE org = {org:String}"
        " AND kind = 'collection' AND owner_email = {p:String}"
        " UNION DISTINCT"
        " SELECT namespace FROM loom.collections FINAL WHERE org = {org:String}"
        " AND owner = {p:String}) ORDER BY namespace",
        params={"org": org, "p": "user:" + user_id},
    )
    return [str(r["namespace"]) for r in rows]


async def forget_collection_owner_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """Blank the user as the creator of the collections they made: in the tree
    and in the directory. The collections and other writers' documents stay.
    Returns the rows rewritten in both."""
    ch = ch or _default_client()
    params = {"org": org, "p": "user:" + user_id}
    tree = await _rewrite(
        "loom.collections",
        "org = {org:String} AND owner = {p:String}",
        f"'' AS owner, {_NEW_VERSION} AS version",
        params,
        ch,
    )
    directory = await _rewrite(
        "loom.namespaces",
        "org = {org:String} AND kind = 'collection' AND owner_email = {p:String}",
        "'' AS owner_email, greatest(updated_at + 1, toDateTime(now())) AS updated_at",
        params,
        ch,
    )
    return tree + directory


async def delete_grant_rows_of_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Every row naming the user (`user:<user_id>`) as a grant's subject, once
    those grants are revoked: a revoked row caps an older token's namespace
    claim, and no end-user token carries one, so it holds nothing but the
    user's id."""
    from server.erasure.store.mutations import count_then_mutate

    where = "org = {org:String} AND subject = {p:String}"
    return await count_then_mutate(
        "loom.namespace_grants",
        count_where=where,
        mutation="DELETE",
        mutation_where=where,
        params={"org": org, "p": "user:" + user_id},
        final=False,
        ch=ch,
    )
