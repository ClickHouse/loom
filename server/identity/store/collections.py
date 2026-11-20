"""Repository for loom.collections: each collection's place in its org's tree.

ReplacingMergeTree(version) on (org, namespace): every write is a whole row
from `collection_row`, so a move or a retirement carries every column.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client

COLUMNS = ("org", "namespace", "parent", "owner", "created_at", "deleted", "version")


def collection_row(
    org: str,
    namespace: str,
    *,
    parent: str = "",
    owner: str = "",
    created_at: datetime | None = None,
    deleted: bool = False,
    after: int = 0,
) -> dict[str, Any]:
    """One whole loom.collections row, versioned above `after` (the version of
    the row it replaces) whatever this host's clock says."""
    return {
        "org": org,
        "namespace": namespace,
        "parent": parent,
        "owner": owner,
        "created_at": created_at or datetime.now(UTC).replace(tzinfo=None),
        "deleted": deleted,
        "version": max(time.time_ns(), after + 1),
    }


async def write(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.collections", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )


async def stored_version(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    """The version of the collection's newest row, a deleted one included; 0 for none."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        row = await ch.query_one(
            "SELECT max(version) AS v FROM loom.collections"
            " WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": namespace},
        )
    return int((row or {}).get("v") or 0)


async def fetch(org: str, namespace: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """The collection's live row, or None."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_one(
            f"""
            SELECT {", ".join(COLUMNS)}
            FROM loom.collections FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND NOT deleted
            """,
            params={"org": org, "ns": namespace},
        )


async def children(
    org: str, namespace: str, *, owner: str = "", ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The live rows whose parent is `namespace`; with `owner`, only the ones it
    created."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    mine = " AND owner = {owner:String}"
    with own_write_reads():
        return await ch.query_all(
            f"""
            SELECT {", ".join(COLUMNS)}
            FROM loom.collections FINAL
            WHERE org = {{org:String}} AND parent = {{ns:String}} AND NOT deleted
            {mine if owner else ""}
            """,
            params={"org": org, "ns": namespace, "owner": owner},
        )
