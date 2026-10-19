"""Repository for loom.collections: each collection's place in its org's tree.

ReplacingMergeTree(version) on (org, namespace): every write is a whole row
from `collection_row`, so a move or a retirement carries every column.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client

COLUMNS = ("org", "namespace", "parent", "personal", "owner", "created_at", "deleted", "version")


def collection_row(
    org: str,
    namespace: str,
    *,
    parent: str = "",
    personal: bool = False,
    owner: str = "",
    created_at: datetime | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    """One whole loom.collections row."""
    return {
        "org": org,
        "namespace": namespace,
        "parent": parent,
        "personal": personal,
        "owner": owner,
        "created_at": created_at or datetime.now(UTC).replace(tzinfo=None),
        "deleted": deleted,
        "version": time.time_ns(),
    }


async def write(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(
        "loom.collections", [row], settings={"async_insert": 0, "wait_for_async_insert": 1}
    )


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
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The live rows whose parent is `namespace`."""
    from server.core.ch import own_write_reads

    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_all(
            f"""
            SELECT {", ".join(COLUMNS)}
            FROM loom.collections FINAL
            WHERE org = {{org:String}} AND parent = {{ns:String}} AND NOT deleted
            """,
            params={"org": org, "ns": namespace},
        )
