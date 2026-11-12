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


async def create_database(admin_dsn: str) -> tuple[str, str]:
    """A new, empty database on `admin_dsn`'s server: its name and its DSN."""
    import asyncpg

    name = f"loom_it_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.exceptions.InsufficientPrivilegeError:
        pytest.skip("this Postgres role cannot create a database")
    finally:
        await admin.close()
    return name, with_database(admin_dsn, name)


async def drop_database(admin_dsn: str, name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@contextlib.asynccontextmanager
async def own_database() -> AsyncIterator[str]:
    """The DSN of a new database holding every Postgres migration."""
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    name, target = await create_database(dsn)
    try:

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
        await drop_database(dsn, name)
