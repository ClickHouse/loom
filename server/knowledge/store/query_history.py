"""Query-history registration rows; credentials stay sealed on this boundary."""

from __future__ import annotations

from typing import Any

from server.core.ch import sequential_reads
from server.core.repo_base import _CHClient, _default_client

TABLE = "loom.query_history_sources"
_COLUMNS = (
    "org, namespace, agent, user_id, session_id, name, source_id, host, port, secure, "
    "user, password_enc, cluster, databases, actor, source_class, created_at, "
    "tombstoned_at, version"
)


async def find(
    org: str, namespace: str, name: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    with sequential_reads():
        return await (ch or _default_client()).query_one(
            f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String} LIMIT 1",
            params={"org": org, "ns": namespace, "name": name},
        )


async def list_sources(
    org: str, namespace: str, limit: int, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    with sequential_reads():
        return await (ch or _default_client()).query_all(
            f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL "
            "ORDER BY name LIMIT {lim:UInt32}",
            params={"org": org, "ns": namespace, "lim": limit},
        )


async def active_after(after: str, limit: int) -> list[dict[str, Any]]:
    return await _default_client().query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE source_id > {after:UUID} "
        "AND tombstoned_at IS NULL ORDER BY source_id LIMIT {lim:UInt32}",
        params={"after": after, "lim": limit},
    )


async def insert(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    await (ch or _default_client()).insert(
        TABLE,
        [{column: row[column] for column in _COLUMNS.split(", ")}],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def next_source_version(org: str, namespace: str, name: str) -> int:
    from server.core.versions import next_version

    return await next_version(
        _default_client(), TABLE, {"org": org, "namespace": namespace, "name": name}
    )
