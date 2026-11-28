"""The restore fixture adopts its private database before any pooled work."""

from __future__ import annotations

import asyncio
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from server.core import pg
from server.settings import settings
from tests.integration import test_cell_restore_reconcile as reconcile


@pytest.mark.parametrize("warm", [True, False])
async def test_restore_fixture_retires_prior_pool_before_switching_database(monkeypatch, warm):
    first = "postgresql://fixture@127.0.0.1/restore_first"
    previous = "postgresql://fixture@127.0.0.1/prior_database"
    events = []

    class Pool:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False

        async def close(self):
            self.closed = True
            events.append(("close", self.dsn, settings.pg_dsn))

    old = Pool(previous)
    pools = weakref.WeakKeyDictionary()
    if warm:
        pools[asyncio.get_running_loop()] = old
    monkeypatch.setattr(pg, "_pools", pools)
    monkeypatch.setattr(pg, "_opening", weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_closing", weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_expected", None)
    monkeypatch.setattr(settings, "pg_dsn", previous)

    async def create_pool(dsn, **kwargs):
        events.append(("create", dsn))
        return Pool(dsn)

    async def connect(dsn, **kwargs):
        events.append(("connect", dsn))
        return SimpleNamespace(
            dsn=dsn,
            close=AsyncMock(),
            fetchval=AsyncMock(return_value=dsn.rsplit("/", 1)[1]),
        )

    async def fetchval(sql):
        assert sql == "SELECT current_database()"
        return (await pg._pool_for_loop()).dsn.rsplit("/", 1)[1]

    monkeypatch.setattr(pg, "fetchval", fetchval)
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(pg, "_check_deployment", AsyncMock())
    monkeypatch.setattr(pg, "init_connection", AsyncMock())
    client = SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(), execute=AsyncMock(), insert=AsyncMock()
    )
    monkeypatch.setattr(reconcile.ch_module, "get_client", lambda: client)
    fixture = reconcile.ch.__wrapped__({"first": first}, monkeypatch)
    try:
        await anext(fixture)
        pooled = await pg._pool_for_loop()
        async with pg.dedicated() as dedicated:
            observed = (pooled.dsn, dedicated.dsn)
    finally:
        await fixture.aclose()
    assert observed == (first, first)
    assert not pools
    assert client.disconnect.await_count == 1
    if warm:
        assert old.closed
        assert events[0] == ("close", previous, previous)
    else:
        assert not old.closed
        assert events[:2] == [("connect", first), ("create", first)]
