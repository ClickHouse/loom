"""Repository for loom.namespaces, the namespace directory.

ReplacingMergeTree(updated_at) keyed on (org, namespace): a writer that re-inserts a row
must carry every column forward, so the rewrite paths read the whole row here first.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def fetch_row(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The directory row of (org, namespace), every column, active or soft-deleted; None when
    the name was never registered."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT * FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    return dict(row) if row else None


async def is_active(org: str, namespace: str, *, ch: _CHClient | None = None) -> bool:
    """Is (org, namespace) a registered, active namespace? A soft-deleted one is not."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT 1 AS one FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND active
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    return row is not None


async def descriptions(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """The directory description of each of `namespaces` that has one."""
    if not namespaces:
        return {}
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT namespace, description FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace IN {ns:Array(String)} AND description != ''
        LIMIT {n:UInt32}
        """,
        params={"org": org, "ns": namespaces, "n": len(namespaces)},
    )
    return {str(r["namespace"]): str(r["description"]) for r in rows}
