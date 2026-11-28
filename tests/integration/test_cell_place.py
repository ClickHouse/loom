"""Operator placement on two cells' databases and a real directory, without memory rows."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import time

import httpx
import pytest

from server.cli import cell_place
from server.core import cell, pg
from server.router import app, directory
from server.settings import settings
from tests.integration._pg_database import create_database, drop_database, own_database

OPERATOR = "lco_" + "3" * 128
ROUTER = httpx.URL("http://127.0.0.1:7901")


@pytest.fixture
async def stores(monkeypatch):
    admin = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    name, dsn = await create_database(admin)
    stack = contextlib.AsyncExitStack()
    try:
        stack.push_async_callback(drop_database, admin, name)
        first = await stack.enter_async_context(own_database())
        second = await stack.enter_async_context(own_database())
        monkeypatch.setattr(directory, "_pool", None)
        stack.push_async_callback(directory.close)
        monkeypatch.setattr(settings, "router_pg_dsn", dsn)
        monkeypatch.setattr(settings, "storage_authority", "postgres")
        monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
        monkeypatch.setattr(
            settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
        )
        await directory.migrate()
        stack.push_async_callback(pg.close_pool)
        stack.callback(pg.expect_deployment, None)
        pg.expect_deployment(None)
        router_app = app.build_app(app.Router())
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_client(
                **{**kw, "transport": kw.get("transport") or httpx.ASGITransport(app=router_app)}
            ),
        )
        targets = {"first": first, "second": second}
        for cell_name, cell_dsn in targets.items():
            await _initialize(cell_dsn, cell_name)
        yield targets
    except BaseException:  # Cleanup must preserve the setup failure.
        with contextlib.suppress(BaseException):
            await stack.aclose()
        raise
    else:
        await stack.aclose()


async def _initialize(dsn, name):
    import asyncpg

    fields = cell.fingerprint(f"dep-{name}")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, $1)",
            f"dep-{name}",
        )
        await conn.execute(
            "INSERT INTO loom.cell_first (is_first, cell) VALUES ($1, $2)", name == "first", name
        )
        await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
        await conn.execute(
            "INSERT INTO loom.cell_self (current_fp) VALUES ($1::jsonb)",
            json.dumps(cell.as_value(fields)),
        )
    finally:
        await conn.close()
    async with directory._connection() as conn:
        await conn.execute(
            "INSERT INTO loom_router.cells (cell, is_first, host, state, mode, credential_sha256, deployment_id, current_fp) "
            "VALUES ($1, $2, $3, 'serving', 'enforced', $4, $5, $6)",
            name,
            name == "first",
            f"{name}.example.test",
            hashlib.sha256(name.encode()).hexdigest(),
            f"dep-{name}",
            cell.as_value(fields),
        )


async def _local(stores, monkeypatch, name, org="acme"):
    monkeypatch.setattr(settings, "cell", name)
    monkeypatch.setattr(settings, "pg_dsn", stores[name])
    return await cell_place.place(argparse.Namespace(cell=name, org=org), ROUTER)


async def _tenancy(dsn):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        return [r["org"] for r in await conn.fetch("SELECT org FROM loom.tenancy ORDER BY org")]
    finally:
        await conn.close()


async def test_operator_place_retries_and_refuses_a_different_cell(
    stores, monkeypatch, record_property
):
    started = time.perf_counter()
    first = await _local(stores, monkeypatch, "first")
    record_property("local_first_place_ms", (time.perf_counter() - started) * 1000)
    assert first == {"org": "acme", "cell": "first", "deployment_id": "dep-first"}
    started = time.perf_counter()
    assert await _local(stores, monkeypatch, "first") == first
    record_property("local_duplicate_place_ms", (time.perf_counter() - started) * 1000)
    assert await _tenancy(stores["first"]) == ["acme"]
    with pytest.raises(cell_place.PlacementRefused, match="belongs to cell 'first'"):
        await _local(stores, monkeypatch, "second")
    assert await _tenancy(stores["second"]) == []
    async with directory._connection() as conn:
        assert await conn.fetchval("SELECT placed FROM loom_router.cells WHERE cell = 'first'") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM loom_router.audit WHERE action = 'place'")
            == 3
        )


async def test_two_cells_racing_one_org_have_one_winner_and_count_once(stores):
    async def place(name):
        fields = cell.as_value(cell.fingerprint(f"dep-{name}"))
        async with httpx.AsyncClient() as client:
            return await client.post(
                str(ROUTER.copy_with(path=f"/cells/v1/operator/cells/{name}/place")),
                json={"org": "raced", "deployment_id": f"dep-{name}", "fingerprint": fields},
                headers={"Authorization": f"Bearer {OPERATOR}"},
            )

    a, b = await asyncio.gather(place("first"), place("second"))
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    async with directory._connection() as conn:
        assert await conn.fetchval("SELECT sum(placed) FROM loom_router.cells") == 1
        assert await conn.fetchval("SELECT count(*) FROM loom_router.placements") == 1
    assert await _tenancy(stores["first"]) == await _tenancy(stores["second"]) == []


@pytest.mark.parametrize("limit", ["closed = true", "placements_max = 0"])
async def test_new_closed_or_full_cell_writes_neither_store(stores, monkeypatch, limit):
    async with directory._connection() as conn:
        await conn.execute(f"UPDATE loom_router.cells SET {limit} WHERE cell = 'first'")
    with pytest.raises(cell_place.PlacementRefused, match="router refused"):
        await _local(stores, monkeypatch, "first")
    assert await _tenancy(stores["first"]) == []
    assert await directory.lookup("acme") is None


async def test_failed_local_insert_resumes_and_existing_winner_ignores_closed_quota(
    stores, monkeypatch
):
    import asyncpg

    conn = await asyncpg.connect(stores["first"])
    try:
        await conn.execute(
            "CREATE FUNCTION loom.fail_place() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'local insertion refused'; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER fail_place BEFORE INSERT ON loom.tenancy FOR EACH ROW EXECUTE FUNCTION loom.fail_place()"
        )
        with pytest.raises(asyncpg.RaiseError, match="local insertion refused"):
            await _local(stores, monkeypatch, "first")
        assert await _tenancy(stores["first"]) == []
        assert (await directory.lookup("acme"))[0] == "first"
        await conn.execute("DROP TRIGGER fail_place ON loom.tenancy")
        async with directory._connection() as dc:
            await dc.execute(
                "UPDATE loom_router.cells SET closed = true, placements_max = 0 WHERE cell = 'first'"
            )
        await _local(stores, monkeypatch, "first")
        assert await _tenancy(stores["first"]) == ["acme"]
    finally:
        await conn.close()


async def test_first_recording_refuses_and_enforced_attaching_succeeds(stores, monkeypatch):
    import asyncpg

    conn = await asyncpg.connect(stores["first"])
    try:
        await conn.execute("UPDATE loom.tenancy_mode SET mode = 'recording'")
        with pytest.raises(cell_place.PlacementRefused, match="needs enforced"):
            await _local(stores, monkeypatch, "first")
        assert await directory.lookup("acme") is None
        await conn.execute("UPDATE loom.tenancy_mode SET mode = 'enforced'")
        async with directory._connection() as dc:
            await dc.execute(
                "UPDATE loom_router.cells SET state = 'attaching' WHERE cell = 'first'"
            )
        await _local(stores, monkeypatch, "first")
        assert await _tenancy(stores["first"]) == ["acme"]
    finally:
        await conn.close()


@pytest.mark.parametrize("changed", ["mode", "fingerprint"])
async def test_changed_local_state_after_directory_call_refuses_insert(
    stores, monkeypatch, changed
):
    import asyncpg

    real = cell_place._directory

    async def change_after_call(*args):
        await real(*args)
        conn = await asyncpg.connect(stores["first"])
        try:
            if changed == "mode":
                await conn.execute("UPDATE loom.tenancy_mode SET mode = 'recording'")
            else:
                fields = cell.fingerprint("dep-first")
                fields["oidc_client_id"] = "changed"
                await conn.execute(
                    "UPDATE loom.cell_self SET current_fp = $1::jsonb",
                    json.dumps(cell.as_value(fields)),
                )
        finally:
            await conn.close()

    monkeypatch.setattr(cell_place, "_directory", change_after_call)
    with pytest.raises(cell_place.PlacementRefused):
        await _local(stores, monkeypatch, "first")
    assert (await directory.lookup("acme"))[0] == "first"
    assert await _tenancy(stores["first"]) == []
