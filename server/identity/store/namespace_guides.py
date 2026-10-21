"""Repository for loom.namespace_guides: one row per revision of a namespace's
guide, the highest revision current (schemas/migrations/072_namespace_guides.sql).
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

_COLUMNS = "revision, content, summary, note, updated_by, updated_at"


async def insert_row(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Write one revision, committed before it returns (the next read must see it)."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.namespace_guides",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def latest(org: str, namespace: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    """The namespace's highest revision, cleared or not; None when it never had one."""
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        f"SELECT {_COLUMNS} FROM loom.namespace_guides "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "ORDER BY revision DESC LIMIT 1",
        params={"org": org, "ns": namespace},
    )
    return row


async def at_revision(
    org: str, namespace: str, revision: int, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        f"SELECT {_COLUMNS} FROM loom.namespace_guides "
        "WHERE org = {org:String} AND namespace = {ns:String} AND revision = {rev:UInt32} "
        "LIMIT 1",
        params={"org": org, "ns": namespace, "rev": revision},
    )
    return row


async def history(
    org: str, namespace: str, limit: int, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The newest `limit` revisions, newest first, without their content."""
    ch = ch or _default_client()
    rows: list[dict[str, Any]] = await ch.query_all(
        "SELECT revision, summary, note, updated_by, updated_at, length(content) AS chars "
        "FROM loom.namespace_guides "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "ORDER BY revision DESC LIMIT 1 BY revision LIMIT {lim:UInt32}",
        params={"org": org, "ns": namespace, "lim": limit},
    )
    return rows


# The current revision of each guide of the org, from the sort key alone: the
# history's text is read only at these keys.
_HEADS = (
    "(namespace, revision) IN (SELECT namespace, max(revision) FROM loom.namespace_guides "
    "WHERE org = {org:String} GROUP BY namespace)"
)

# The namespaces of the org whose current guide summary contains {q:String}: the
# namespace directory's search. The summary is the one part of a guide the org
# sees; matching the text would let a caller who may not read it rebuild it a
# character at a time.
SUMMARY_MATCH_SQL = (
    "SELECT namespace FROM loom.namespace_guides WHERE org = {org:String} AND "
    + _HEADS
    + " AND positionCaseInsensitive(summary, {q:String}) > 0"
)


async def summaries(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> dict[str, dict[str, Any]]:
    """The current revision and summary of each of `namespaces` that has had a
    guide; a cleared one has an empty summary."""
    if not namespaces:
        return {}
    ch = ch or _default_client()
    rows: list[dict[str, Any]] = await ch.query_all(
        "SELECT namespace, revision, summary FROM loom.namespace_guides "
        "WHERE org = {org:String} AND namespace IN {ns:Array(String)} "
        "ORDER BY namespace, revision DESC LIMIT 1 BY namespace",
        params={"org": org, "ns": namespaces},
    )
    return {str(r["namespace"]): r for r in rows}
