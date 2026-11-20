"""The probe listener's store reads (server/core/cell_probe.py) against a real Postgres
and ClickHouse: a nonce written under the reserved name reads back, and none reads as
None. Needs LOOM_PG_DSN and LOOM_CH_HOST, a Postgres and a ClickHouse of their own."""

from __future__ import annotations

import os
import uuid

import pytest

from server.core import cell_probe, pg
from server.core.ch import get_client
from server.core.leases import LOCK_TABLE
from server.core.schema import pg_migrate
from server.settings import settings


@pytest.fixture
async def stores(monkeypatch: pytest.MonkeyPatch):
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    monkeypatch.setattr(settings, "pg_dsn", dsn)
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    await pg_migrate.migrate()
    await get_client().connect()
    yield
    async with pg.connection() as conn:
        await conn.execute("DELETE FROM loom.cell_probe WHERE name = $1", cell_probe.NONCE_NAME)
    await get_client().execute(
        f"DELETE FROM {LOCK_TABLE} WHERE name = {{name:String}}", {"name": cell_probe.NONCE_NAME}
    )
    await get_client().disconnect()
    await pg.close_pool()


@pytest.mark.asyncio
async def test_each_stores_nonce_reads_back_and_none_reads_as_none(stores: None) -> None:
    assert await cell_probe.nonces() == {"postgres": None, "clickhouse": None, "bucket": None}
    pg_nonce, ch_nonce = uuid.uuid4().hex, uuid.uuid4().hex
    async with pg.connection() as conn:
        await conn.execute(
            "INSERT INTO loom.cell_probe (name, nonce) VALUES ($1, $2)",
            cell_probe.NONCE_NAME,
            pg_nonce,
        )
    await get_client().execute(
        f"INSERT INTO {LOCK_TABLE} (name, holder, acquired_at, renewed_at, ttl_s, build_commit) "
        "VALUES ({name:String}, {nonce:String}, now64(3), now64(3), 60, '')",
        {"name": cell_probe.NONCE_NAME, "nonce": ch_nonce},
    )
    assert await cell_probe.nonces() == {
        "postgres": pg_nonce,
        "clickhouse": ch_nonce,
        "bucket": None,
    }
