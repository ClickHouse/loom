"""A Postgres database of a test's own, migrated from empty and dropped afterwards, for
tests that write the cell state rows: in the shared database they would gate the
integration server (server/core/cell.py)."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from server.core.schema import pg_migrate


def with_database(dsn: str, name: str) -> str:
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))


@contextlib.asynccontextmanager
async def own_database() -> AsyncIterator[str]:
    """The DSN of a new database holding every Postgres migration."""
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    name = f"loom_it_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(dsn)
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pytest.skip("this Postgres role cannot create a database")
        target = with_database(dsn, name)

        @contextlib.asynccontextmanager
        async def _connect() -> AsyncIterator[Any]:
            conn = await asyncpg.connect(target)
            try:
                yield conn
            finally:
                await conn.close()

        await pg_migrate.migrate(connect=_connect)
        yield target
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
