"""Real erasure replay after restoring a cell to before permanent placement."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import pytest

from server.cli import cell_restore
from server.cli import restore as reconciliation
from server.core import ch as ch_module
from server.core import pg
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import handlers
from server.erasure.store import ledger
from server.router import directory
from server.settings import settings
from tests.integration import test_cell_place as placement
from tests.integration._resources import unique_org
from tests.integration.test_restore_reconcile import _PG_TABLES

TABLES = ("tenancy", *_PG_TABLES)


class ScratchClickHouse:
    """Real I/O with singleton identity, receipts and audit isolated in exact tables."""

    def __init__(self, real, aliases):
        self.real, self.aliases = real, aliases

    def sql(self, value):
        for source, target in self.aliases.items():
            value = value.replace(source, target)
        return value

    async def query_one(self, sql, params=None):
        return await self.real.query_one(self.sql(sql), params)

    async def query_all(self, sql, params=None):
        return await self.real.query_all(self.sql(sql), params)

    async def execute(self, sql, params=None, **kwargs):
        return await self.real.execute(self.sql(sql), params, **kwargs)

    async def insert(self, table, rows, **kwargs):
        return await self.real.insert(self.aliases.get(table, table), rows, **kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
async def stores(monkeypatch):
    async for value in placement.stores.__wrapped__(monkeypatch):
        yield value


@pytest.fixture
async def ch(stores, monkeypatch):
    # Dedicated and pooled work must both adopt this fixture's private database.
    await pg.close_pool()
    real = ch_module.get_client()
    await real.connect()
    tag = uuid.uuid4().hex
    aliases = {
        f"loom.{name}": f"loom.restore_reconcile_{tag}_{name}"
        for name in ("storage_authority", "erasure_requests", "audit_log", "instances")
    }
    stack = contextlib.AsyncExitStack()
    stack.push_async_callback(real.disconnect)
    try:
        for source, target in aliases.items():
            if source == "loom.instances":
                # A KeeperMap clone would share the running server's heartbeat keys.
                await real.execute(
                    f"CREATE TABLE {target} ENGINE = Memory AS SELECT * FROM {source} LIMIT 0"
                )
            else:
                await real.execute(f"CREATE TABLE {target} AS {source}")
            stack.push_async_callback(real.execute, f"DROP TABLE IF EXISTS {target}")
        await real.insert(
            aliases["loom.storage_authority"],
            [{"authority": "postgres", "epoch": 1, "deployment_id": "dep-first"}],
        )
        paired = ScratchClickHouse(real, aliases)
        monkeypatch.setattr(ch_module, "_ch", paired)
        monkeypatch.setattr(settings, "cell", "first")
        monkeypatch.setattr(settings, "pg_dsn", stores["first"])
        monkeypatch.setattr(settings, "document_s3_bucket", "")
        stack.push_async_callback(pg.close_pool)
        async with pg.dedicated() as conn:
            expected = unquote(urlsplit(stores["first"]).path.removeprefix("/"))
            assert await pg.fetchval("SELECT current_database()") == expected
            assert await conn.fetchval("SELECT current_database()") == expected
        yield paired
    except BaseException:  # Cleanup must preserve the erasure or repair failure.
        with contextlib.suppress(BaseException):
            await stack.aclose()
        raise
    else:
        await stack.aclose()


async def snapshot(org, tag):
    async with pg.dedicated() as conn:
        for table in TABLES:
            await conn.execute(
                f"CREATE TABLE public.restore_{tag}_{table} AS "
                f"SELECT * FROM loom.{table} WHERE org=$1",
                org,
            )
    return datetime.now(UTC)


async def restore_snapshot(org, tag):
    async with pg.dedicated() as conn, conn.transaction():
        for table in reversed(TABLES):
            await conn.execute(f"DELETE FROM loom.{table} WHERE org=$1", org)
        for table in TABLES:
            columns = ",".join(
                row["column_name"]
                for row in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='loom' "
                    "AND table_name=$1 AND is_generated='NEVER' ORDER BY ordinal_position",
                    table,
                )
            )
            await conn.execute(
                f"INSERT INTO loom.{table} ({columns}) "
                f"SELECT {columns} FROM public.restore_{tag}_{table}"
            )
            await conn.execute(f"DROP TABLE public.restore_{tag}_{table}")


async def write_and_erase(org, ns):
    value = "content erased after the backup"
    async with pg.dedicated() as conn:
        await conn.execute(
            "INSERT INTO loom.memory_current "
            "(org,namespace,agent,user_id,session_id,memory_key,memory_id,version,seq,content_sha256,row) "
            "VALUES($1,$2,'fixture','user','session','key',$3,1,1,$4,$5)",
            org,
            ns,
            uuid.uuid4(),
            hashlib.sha256(value.encode()).hexdigest(),
            {"tags": [], "value": value, "actor": "restore-fixture", "source_class": "imported"},
        )
    output = await handlers.handle_erasure_request(
        ErasureRequestInput(
            org=org,
            scope_type="namespace_list",
            scope_value=json.dumps({"namespaces": [ns], "purge": True, "external": False}),
            requester="restore-fixture",
            purge=True,
            external=False,
        )
    )
    assert output.status == "completed"
    assert await pg.fetchval("SELECT count(*) FROM loom.memory_current WHERE org=$1", org) == 0
    return str(output.erasure_id)


@pytest.mark.parametrize("restored", ["postgres", "both"])
async def test_erased_permanent_org_reconciles_after_metadata_restore(
    stores, ch, monkeypatch, restored
):
    org, ns, tag = unique_org("cell-restore"), "fixture", uuid.uuid4().hex[:12]
    point = await snapshot(org, tag)
    assert await placement._local(stores, monkeypatch, "first", org) == {
        "org": org,
        "cell": "first",
        "deployment_id": "dep-first",
    }
    erasure_id = await write_and_erase(org, ns)
    receipts = await ledger.completed_since(point)
    assert [str(row["id"]) for row in receipts] == [erasure_id]
    assert await pg.fetchval(
        "SELECT completed_at IS NOT NULL FROM loom.erasure_ledger WHERE org=$1 AND id=$2",
        org,
        uuid.UUID(erasure_id),
    )
    erased_fence = await pg.fetchval(
        "SELECT fenced_at FROM loom.idempotency_fences WHERE org=$1", org
    )
    assert erased_fence is not None
    await restore_snapshot(org, tag)
    assert await pg.fetchval("SELECT count(*) FROM loom.tenancy WHERE org=$1", org) == 0
    assert await pg.fetchval("SELECT count(*) FROM loom.erasure_ledger WHERE org=$1", org) == 0
    if restored == "both":
        await ch.execute("TRUNCATE TABLE loom.erasure_requests")
        assert await ledger.completed_since(point) == []
    repaired = await cell_restore.Restore(
        argparse.Namespace(cell="first", wait=30), placement.ROUTER, cell_restore.Budget(30)
    ).run(ch)
    assert repaired["inserted_tenancy"] == 1 and repaired["snapshot_orgs"] == 1
    result = await reconciliation.reconcile(restored, point, apply=True, live=False, ch=ch)
    assert result == (0 if restored == "postgres" else 2)
    assert await pg.fetchval("SELECT count(*) FROM loom.memory_current WHERE org=$1", org) == 0
    assert await pg.fetchval("SELECT count(*) FROM loom.tenancy WHERE org=$1", org) == 1
    if restored == "postgres":
        entry = await pg.fetchrow(
            "SELECT completed_at,pg_steps FROM loom.erasure_ledger WHERE org=$1 AND id=$2",
            org,
            uuid.UUID(erasure_id),
        )
        assert entry["completed_at"] is not None
        assert {"pg_memory.delete", "pg_knowledge.delete"} <= set(entry["pg_steps"])
        assert (
            await pg.fetchval("SELECT fenced_at FROM loom.idempotency_fences WHERE org=$1", org)
            > erased_fence
        )
    else:
        assert await pg.fetchval("SELECT count(*) FROM loom.erasure_ledger WHERE org=$1", org) == 0
        assert (
            await pg.fetchval("SELECT fenced_at FROM loom.idempotency_fences WHERE org=$1", org)
            == erased_fence
        )
    assert (await directory.lookup(org))[0] == "first"
    async with directory._connection() as conn:
        assert await conn.fetchval("SELECT placed FROM loom_router.cells WHERE cell='first'") == 1
