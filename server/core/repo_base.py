"""Shared ClickHouse-client seam for the repository layer.

Every repository talks to ClickHouse through this minimal structural
protocol rather than the concrete `server.core.ch.CHClient`. That keeps each
repo decoupled from the client class and makes mocking trivial in unit
tests — pass any object with these four methods.

`_default_client()` is the production binding: the process-wide async
client from `server.core.ch`. Repos take `ch: _CHClient | None = None` and fall
back to it, so tests inject a fake and production gets the real one.
"""

from __future__ import annotations

from typing import Any, Protocol

from clickhouse_connect.driver.external import ExternalData

from server.core.ch_params import chunks


class _CHClient(Protocol):
    async def query_one(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
    ) -> dict[str, Any] | None: ...
    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
    ) -> list[dict[str, Any]]: ...
    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
        *,
        settings: dict[str, Any] | None = ...,
        external_data: ExternalData | None = ...,
    ) -> None: ...
    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = ...,
    ) -> None: ...


def _default_client() -> _CHClient:
    from server.core.ch import get_client

    return get_client()


async def query_all_sliced(
    ch: _CHClient, sql: str, params: dict[str, Any], key: str
) -> list[dict[str, Any]]:
    """`query_all` with the list `params[key]` bound a slice at a time: every slice's
    rows, in order. An empty list sends no statement and reads no row."""
    rows: list[dict[str, Any]] = []
    for part in chunks(params[key]):
        rows += await ch.query_all(sql, params | {key: part})
    return rows
