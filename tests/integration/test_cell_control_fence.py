"""Control requests remain fenced after their cell's lock backend dies."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from server.core import cell
from server.router import app, directory
from server.settings import settings
from tests.unit.cell_control_owner import owner as _owner

TOKEN = "lco_fence-test"


@pytest.fixture
async def control_stores(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import asyncpg

    dsn = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    admin = await asyncpg.connect(dsn)
    async with contextlib.AsyncExitStack() as cleanup:
        try:
            cleanup.push_async_callback(admin.close)
            name = f"loom_fence_{uuid.uuid4().hex[:12]}"
            await admin.execute(f'CREATE DATABASE "{name}"')
            cleanup.push_async_callback(admin.execute, f'DROP DATABASE "{name}" WITH (FORCE)')
            own = urlunsplit(urlsplit(dsn)._replace(path=f"/{name}"))
            monkeypatch.setattr(settings, "router_pg_dsn", own)
            monkeypatch.setattr(
                settings, "router_operator_sha256", hashlib.sha256(TOKEN.encode()).hexdigest()
            )
            monkeypatch.setattr(directory, "_pool", None)
            cleanup.push_async_callback(directory.close)
            conn = await asyncpg.connect(own)
            cleanup.push_async_callback(conn.close)
            await directory.migrate()
            await conn.execute(
                "INSERT INTO loom_router.cells (cell, is_first, host, deployment_id, credential_sha256, state) "
                "VALUES ('first', true, 'first.example.test', 'dep-first', $1, 'attaching')",
                "a" * 64,
            )
            transport = httpx.ASGITransport(app=app.build_app())
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                yield client, conn, own
        except BaseException:  # Owned cleanup cannot replace the first failure.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise


def expected(row: dict[str, Any], *, generation: bool = True) -> dict[str, Any]:
    out = {
        k: row[k]
        for k in (
            "cell",
            "is_first",
            "host",
            "deployment_id",
            "bucket",
            "bucket_prefix",
            "probe_url",
            "upstream_url",
            "current_fp",
            "next_fp",
        )
    }
    if generation:
        out["mutation_generation"] = row["mutation_generation"]
    return {"expected": out}


async def read(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/cells/v1/operator/cells/first")
    assert response.status_code == 200, response.text
    return response.json()


async def claim(client: httpx.AsyncClient, row: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/cells/v1/operator/cells/first/claim", json=expected(row, generation=False)
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_stale_http_after_backend_death_and_pair_aba(control_stores, monkeypatch) -> None:
    import asyncpg

    client, conn, own = control_stores
    a = await asyncpg.connect(own)
    b = await asyncpg.connect(own)
    release = asyncio.Event()
    entered = asyncio.Event()
    task = None
    try:
        await a.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
        snapshot = await read(client)
        old = await claim(client, snapshot)
        real = directory.set_limit

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real(*args, **kwargs)

        monkeypatch.setattr(directory, "set_limit", delayed)
        task = asyncio.create_task(
            client.post(
                "/cells/v1/operator/cells/first/limit", json={**expected(old), "closed": True}
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        await conn.execute("SELECT pg_terminate_backend($1)", a.get_server_pid())
        assert await b.fetchval("SELECT pg_try_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
        newer = await claim(client, await read(client))
        # A complete pair ABA cannot reauthorize A: only the current generation can write.
        await conn.execute(
            "UPDATE loom_router.cells SET current_fp = '{\"v\":1}', next_fp = '{\"v\":1}'"
        )
        await conn.execute("UPDATE loom_router.cells SET current_fp = NULL, next_fp = NULL")
        assert newer["mutation_generation"] != old["mutation_generation"]
        release.set()
        response = await task
        assert response.status_code == 409, response.text
        assert not await conn.fetchval("SELECT closed FROM loom_router.cells")
    finally:
        release.set()
        if task is not None:
            await task
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_replayed_claim_lost_ack_and_noop_wrong_generation(control_stores) -> None:
    client, conn, _ = control_stores
    await conn.execute("UPDATE loom_router.cells SET state = 'serving'")
    snapshot = await read(client)
    lost = await claim(client, snapshot)  # its caller lost this acknowledgement
    current = await claim(client, snapshot)  # duplicate HTTP execution must rotate again
    assert lost["mutation_generation"] != current["mutation_generation"]
    response = await client.post(
        "/cells/v1/operator/cells/first/limit", json={**expected(lost), "closed": False}
    )
    assert response.status_code == 409, response.text
    response = await client.post(
        "/cells/v1/operator/cells/first/limit", json={**expected(current), "closed": False}
    )
    assert response.status_code == 200, response.text
    assert (
        str(await conn.fetchval("SELECT mutation_generation FROM loom_router.cells"))
        == current["mutation_generation"]
    )


@pytest.fixture
async def local_control(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import asyncpg

    from server.core import pg
    from tests.integration._pg_database import own_database

    async with own_database() as dsn:
        monkeypatch.setattr(settings, "cell", "first")
        monkeypatch.setattr(settings, "pg_dsn", dsn)
        monkeypatch.setattr(settings, "document_s3_bucket", "")
        conn = await asyncpg.connect(dsn, server_settings={pg.TENANCY_MARKER: "on"})
        await directory._init(conn)
        await conn.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, 'dep-first')"
        )
        try:
            yield conn, dsn
        finally:
            await conn.close()
            await pg.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["prepare", "attach"])
async def test_dead_local_lock_backend_cannot_write_acceptance(local_control, writer) -> None:
    import asyncpg

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_prepare

    admin, dsn = local_control
    dead = await asyncpg.connect(dsn)
    await directory._init(dead)
    await dead.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
    rows = cell_prepare._CellRows("dep-first", None, "", None, None, 0)
    fp = cell.as_value({"deployment_id": "dep-first"})
    await admin.execute("SELECT pg_terminate_backend($1)", dead.get_server_pid())
    successor = await asyncpg.connect(dsn)
    try:
        assert await successor.fetchval("SELECT pg_try_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
        if writer == "prepare":
            with pytest.raises(
                (asyncpg.InterfaceError, asyncpg.PostgresError, asyncpg.InternalClientError)
            ):
                await cell_prepare._write_rows(
                    dead, rows, {"is_first": True, "mode": "recording"}, fp, None
                )
            assert not await admin.fetchval("SELECT EXISTS(SELECT 1 FROM loom.cell_self)")
        else:
            # A dead controller cannot switch to another session to complete its mode write.
            await admin.execute(
                "INSERT INTO loom.cell_first(is_first, cell) VALUES (true, 'first')"
            )
            await admin.execute("INSERT INTO loom.tenancy_mode(mode) VALUES ('recording')")
            await admin.execute("INSERT INTO loom.cell_self(current_fp) VALUES ($1)", fp)
            with pytest.raises(
                (asyncpg.InterfaceError, asyncpg.PostgresError, asyncpg.InternalClientError)
            ):
                await apg._mode_try(_owner(dead), 100, lambda _m: None, "first", (fp, None))
            assert await admin.fetchval("SELECT mode FROM loom.tenancy_mode") == "recording"
    finally:
        await successor.close()
        await dead.close()


@pytest.mark.asyncio
async def test_same_session_prepare_and_mode_commit(local_control) -> None:
    from server.cli import cell_attach_pg as apg
    from server.cli import cell_prepare

    conn, _ = local_control
    await apg.take_lock(conn, "test")
    fp = cell.as_value({"deployment_id": "dep-first"})
    rows = await cell_prepare._read_rows(conn)
    written = await cell_prepare._write_rows(
        conn, rows, {"is_first": True, "mode": "recording"}, fp, None
    )
    assert written == ["first", "mode", "own"]
    assert await apg.lock_held(conn)
    assert await apg._mode_try(_owner(conn), 100, lambda _m: None, "first", (fp, None))
    result = await apg.durable_read(_owner(conn))
    assert result.mode == "enforced" and result.first == (True, "first")
    assert await apg.lock_held(conn)
    # No-op prepare remains a same-backend synchronous durability boundary.
    rows = await cell_prepare._read_rows(conn)
    assert (
        await cell_prepare._write_rows(conn, rows, {"is_first": True, "mode": "enforced"}, fp, None)
        == []
    )
    assert (
        await _owner(conn).transaction(lambda: conn.fetchval("SELECT pg_backend_pid()"))
        == conn.get_server_pid()
    )


@pytest.mark.asyncio
async def test_recreated_row_fences_previous_generation(control_stores) -> None:
    client, conn, _ = control_stores
    previous = await claim(client, await read(client))
    response = await client.request(
        "DELETE", "/cells/v1/operator/cells/first", json=expected(previous)
    )
    assert response.status_code == 200, response.text
    await conn.execute(
        "INSERT INTO loom_router.cells(cell, is_first, host, state, deployment_id, credential_sha256) VALUES ('first', true, 'first.example.test', 'attaching', 'dep-first', $1)",
        "a" * 64,
    )
    recreated = await read(client)
    assert recreated["mutation_generation"] != previous["mutation_generation"]
    response = await client.post(
        "/cells/v1/operator/cells/first/limit", json={**expected(previous), "closed": True}
    )
    assert response.status_code == 409
    assert not await conn.fetchval("SELECT closed FROM loom_router.cells")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("current_fp", {"v": 1, "fields": {"x": False}}), ("host", "other.example.test")],
)
async def test_claim_checks_exact_pair_and_registration(control_stores, field, value) -> None:
    client, conn, _ = control_stores
    original = await read(client)
    if field == "current_fp":
        await conn.execute('UPDATE loom_router.cells SET current_fp = \'{"v":1,"fields":{"x":0}}\'')
    original[field] = value
    response = await client.post(
        "/cells/v1/operator/cells/first/claim", json=expected(original, generation=False)
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_missing_generation_requires_upgrade_after_auth(control_stores) -> None:
    client, conn, _ = control_stores
    for path in (
        "limit",
        "credential",
        "attach/begin",
        "attach/place",
        "attach/enforced",
        "attach/serving",
    ):
        response = await client.post(f"/cells/v1/operator/cells/first/{path}", json={})
        assert (response.status_code, response.json()["code"]) == (409, "CONTROL_UPGRADE_REQUIRED")
        response = await client.post(
            f"/cells/v1/operator/cells/first/{path}",
            content=b"broken",
            headers={"Authorization": "Bearer lco_wrong"},
        )
        assert response.status_code == 401
    assert await conn.fetchval("SELECT count(*) FROM loom_router.audit") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("aba", [False, True])
async def test_inflight_finalizer_cannot_release_after_new_owner_rewidens(
    control_stores,
    local_control,
    aba,
) -> None:
    """Use the production mutation guard with a test-only narrowing HTTP action.

    The update command is a later concern. This exercises its directory boundary without
    introducing an update route or implementation in this foundation.
    """
    import asyncpg
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from server.cli import cell_attach_pg as apg

    client, directory_admin, _ = control_stores
    local_admin, local_dsn = local_control
    current = {"v": 1, "fields": {"deployment_id": "dep-first", "bearer_issuers": ["old"]}}
    nxt = {"v": 1, "fields": {"deployment_id": "dep-first", "bearer_issuers": ["new"]}}
    await directory_admin.execute(
        "UPDATE loom_router.cells SET current_fp = $1::jsonb, next_fp = $2::jsonb",
        json.dumps(current),
        json.dumps(nxt),
    )
    await local_admin.execute("INSERT INTO loom.cell_first(is_first, cell) VALUES (true, 'first')")
    await local_admin.execute("INSERT INTO loom.tenancy_mode(mode) VALUES ('recording')")
    await local_admin.execute(
        "INSERT INTO loom.cell_self(current_fp, next_fp) VALUES ($1, $2)", nxt, None
    )
    a = await asyncpg.connect(local_dsn)
    b = await asyncpg.connect(local_dsn)
    await directory._init(b)
    started, resume = asyncio.Event(), asyncio.Event()
    task = None
    router = app.Router()

    async def finalizer(request):
        body = await router._operator_body(request)
        if isinstance(body, JSONResponse):
            return body
        compare = app._control(body)
        if isinstance(compare, JSONResponse):
            return compare
        started.set()
        await resume.wait()  # request already sent; caller can no longer retract it

        async def change():
            async def mutate(conn, found):
                await conn.execute(
                    "UPDATE loom_router.cells SET current_fp = next_fp, next_fp = NULL, issuers = ARRAY['new'] WHERE cell = 'first'"
                )
                return found

            return await directory._attach_change("first", compare, mutate)

        return await app._answered(change, app._attach_answer)

    client._transport.app.routes.insert(
        0, Route("/test-finalize/{cell}", finalizer, methods=["POST"])
    )
    try:
        await apg.take_lock(a, "old controller")
        old = await claim(client, await read(client))
        task = asyncio.create_task(client.post("/test-finalize/first", json=expected(old)))
        await asyncio.wait_for(started.wait(), 3)
        await local_admin.execute("SELECT pg_terminate_backend($1)", a.get_server_pid())
        await apg.take_lock(b, "new controller")
        newer = await claim(client, await read(client))

        # New owner claims BEFORE widening local acceptance, on the owned backend.
        async def widen():
            await b.execute("UPDATE loom.cell_self SET current_fp = $1, next_fp = $2", current, nxt)

        if aba:
            await directory_admin.execute(
                "UPDATE loom_router.cells SET current_fp = next_fp, next_fp = NULL"
            )
            await directory_admin.execute(
                "UPDATE loom_router.cells SET current_fp = $1::jsonb, next_fp = $2::jsonb",
                json.dumps(current),
                json.dumps(nxt),
            )
        await _owner(b).transaction(widen)
        assert newer["mutation_generation"] != old["mutation_generation"]
        resume.set()
        response = await task
        assert response.status_code == 409, response.text
        assert (await read(client))["next_fp"] == nxt
        assert await local_admin.fetchval("SELECT next_fp FROM loom.cell_self") == nxt
    finally:
        resume.set()
        if task is not None:
            await task
        await a.close()
        await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("placement_first", [False, True])
async def test_backout_closure_serializes_queued_placement(
    control_stores,
    local_control,
    placement_first,
) -> None:
    import asyncpg

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_control

    client, directory_admin, directory_dsn = control_stores
    local_admin, _ = local_control
    await directory_admin.execute("UPDATE loom_router.cells SET mode = 'enforced'")
    await local_admin.execute("INSERT INTO loom.cell_first(is_first, cell) VALUES (true, 'first')")
    await local_admin.execute("INSERT INTO loom.tenancy_mode(mode) VALUES ('recording')")
    await local_admin.execute("INSERT INTO loom.cell_self(current_fp) VALUES ('{\"v\":1}')")
    await apg.take_lock(local_admin, "backout test")
    original = await read(client)
    blocker = await asyncpg.connect(directory_dsn)
    placement = cleanup = None

    async def call(path, body):
        response = await client.post("/cells/v1/operator/cells/first" + path, json=body)
        if response.status_code != 200:
            raise cell_control._refused(response.json()["error"])
        return response.json()

    async def wait_for_lock(key):
        for _ in range(100):
            count = await directory_admin.fetchval(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid::bigint = $1 AND NOT granted",
                key,
            )
            if count:
                return
            await asyncio.sleep(0.01)
        pytest.fail("owned test request never reached its lock")

    tx = blocker.transaction()
    await tx.start()
    try:
        if placement_first:
            await blocker.fetchrow(
                "SELECT cell FROM loom_router.cells WHERE cell = 'first' FOR UPDATE"
            )
            placement = asyncio.create_task(
                directory.place("queued", "first", app.placement_refusal)
            )
            # The placement has its org lock and is queued at the cell row before claim.
            for _ in range(100):
                if await directory_admin.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid::bigint=$1 AND granted)",
                    directory._PLACE_LOCK,
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("placement did not acquire its org lock")
            cleanup = asyncio.create_task(cell_control.backout(_owner(local_admin), call, original))
            for _ in range(100):
                waiting = await directory_admin.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = 'loom-router' AND wait_event_type = 'Lock'"
                )
                if waiting >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("placement and claim did not both queue behind the cell row")
            await tx.commit()
            assert await placement == ("first", "dep-first")
            with pytest.raises(cell_control.Refused, match="holds placements"):
                await cleanup
            assert await local_admin.fetchval("SELECT EXISTS(SELECT 1 FROM loom.cell_self)")
            assert await local_admin.fetchval("SELECT mode FROM loom.tenancy_mode") == "recording"
        else:
            await blocker.execute(
                "SELECT pg_advisory_xact_lock($1, hashtext($2))", directory._PLACE_LOCK, "queued"
            )
            placement = asyncio.create_task(
                directory.place("queued", "first", app.placement_refusal)
            )
            await wait_for_lock(directory._PLACE_LOCK)
            await cell_control.backout(_owner(local_admin), call, original)
            assert not await local_admin.fetchval("SELECT EXISTS(SELECT 1 FROM loom.cell_self)")
            await tx.commit()
            with pytest.raises(directory.PlacementRefused, match="closed"):
                await placement
        assert await directory_admin.fetchval("SELECT closed FROM loom_router.cells")
        assert (await read(client))["placed_orgs"] == int(placement_first)
    finally:
        if blocker.is_in_transaction():
            await tx.rollback()
        for task in (placement, cleanup):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 — preserve the test failure while reaping owned tasks
                    pass
        await blocker.close()


@pytest.mark.asyncio
async def test_registration_rerun_keeps_claim_and_prior_column_lists_still_work(control_stores):
    client, conn, _ = control_stores
    held = await claim(client, await read(client))
    reg = directory.Registration(
        cell="first",
        is_first=True,
        host="first.example.test",
        deployment_id="dep-first",
        credential_sha256="a" * 64,
        bucket="",
        bucket_prefix="",
        probe_url="",
        tenancy_rows=0,
        state_rows=0,
        prefix_objects=0,
        processes_without_cell=0,
    )
    row, written = await directory.register(reg)
    assert not written and str(row.mutation_generation) == held["mutation_generation"]
    # The prior data plane uses explicit lists and does not interpret the added column.
    legacy = await conn.fetchrow(
        "SELECT cell, is_first, host, state, mode, closed, placements_max, placed, credential_sha256, credential_next_sha256, deployment_id, current_fp, next_fp FROM loom_router.cells"
    )
    assert legacy["cell"] == "first" and "mutation_generation" not in legacy
    await conn.execute(
        "INSERT INTO loom_router.cells(cell,host,state,credential_sha256,deployment_id) VALUES ('later','later.example.test','attaching',$1,'dep-later')",
        "b" * 64,
    )
    generations = await conn.fetch("SELECT mutation_generation FROM loom_router.cells")
    assert len({r["mutation_generation"] for r in generations}) == 2


@pytest.mark.asyncio
async def test_attach_lock_dies_between_confirmation_and_mode_write(local_control):
    """The ready callback is a real scheduling gap after the lock check."""
    import argparse

    import asyncpg

    from server.cli import cell_attach
    from server.cli import cell_attach_pg as apg

    admin, dsn = local_control
    fp = cell.as_value({"deployment_id": "dep-first"})
    await admin.execute("INSERT INTO loom.cell_first(is_first,cell) VALUES(true,'first')")
    await admin.execute("INSERT INTO loom.tenancy_mode(mode) VALUES('recording')")
    await admin.execute("INSERT INTO loom.cell_self(current_fp) VALUES($1)", fp)
    a, b = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    await apg.take_lock(a, "old attach")

    class Router:
        async def call(self, path, body):
            assert path == "/attach/enforced"
            return {"cell": "first"}

    run = cell_attach.Run(
        None, argparse.Namespace(cell="first", wait=0), Router(), lambda _text: None
    )
    run.lock_conn = a
    run.owner = _owner(a)
    run.mode = "recording"
    run.own_fp = (fp, None)
    run.row = {
        "cell": "first",
        "is_first": True,
        "host": "first.example.test",
        "deployment_id": "dep-first",
        "bucket": "",
        "bucket_prefix": "",
        "probe_url": "",
        "upstream_url": "",
        "current_fp": fp,
        "next_fp": None,
        "mutation_generation": str(uuid.UUID(int=1)),
    }

    async def ready():
        await admin.execute("SELECT pg_terminate_backend($1)", a.get_server_pid())
        await apg.take_lock(b, "successor")
        return []

    run.ready_for_commit = ready
    try:
        with pytest.raises(
            (asyncpg.InterfaceError, asyncpg.PostgresError, asyncpg.InternalClientError)
        ):
            await run.commit_mode()
        assert await apg.lock_held(b)
        assert await admin.fetchval("SELECT mode FROM loom.tenancy_mode") == "recording"
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_open", [False, True])
async def test_backout_close_cannot_be_reopened_before_same_session_cleanup(
    control_stores, local_control, fresh_open
):
    from server.cli import cell_attach_pg as apg
    from server.cli import cell_control

    client, directory_admin, _ = control_stores
    local, _ = local_control
    await directory_admin.execute(
        "UPDATE loom_router.cells SET mode = 'enforced', placements_max = 7"
    )
    await local.execute("INSERT INTO loom.cell_first(is_first, cell) VALUES (true, 'first')")
    await local.execute("INSERT INTO loom.tenancy_mode(mode) VALUES ('recording')")
    await local.execute("INSERT INTO loom.cell_self(current_fp) VALUES ('{\"v\":1}')")
    await apg.take_lock(local, "backout reopen test")
    pid = local.get_server_pid()
    before_close = opened = placement_error = None

    async def call(path, body):
        nonlocal before_close, opened, placement_error
        response = await client.post("/cells/v1/operator/cells/first" + path, json=body)
        assert response.status_code == 200, response.text
        answer = response.json()
        if path == "/claim":
            # The queued opener has the NEW generation, read before cleanup closes D.
            before_close = await read(client)
        if path == "/limit":
            assert answer["closed"] and answer["placed_orgs"] == 0
            assert await local.fetchval("SELECT EXISTS(SELECT 1 FROM loom.cell_self)")
            assert await apg.lock_held(local)
            snapshot = await read(client) if fresh_open else before_close
            opened = await client.post(
                "/cells/v1/operator/cells/first/limit",
                json={**expected(snapshot), "closed": False, "placements_max": 99},
            )
            # Actually attempt placement before the acknowledged close returns to cleanup.
            try:
                await directory.place("between-close-and-cleanup", "first", app.placement_refusal)
            except directory.PlacementRefused as exc:
                placement_error = str(exc)
        return answer

    await cell_control.backout(_owner(local), call, await read(client))
    assert opened.status_code == 409, opened.text
    assert "serving" in opened.json()["error"]
    assert placement_error and "closed" in placement_error
    after = await read(client)
    assert after["placed_orgs"] == 0
    assert tuple(
        await directory_admin.fetchrow("SELECT closed, placements_max FROM loom_router.cells")
    ) == (True, 7)
    assert after["mutation_generation"] == before_close["mutation_generation"]
    assert not await directory_admin.fetchval("SELECT EXISTS(SELECT 1 FROM loom_router.placements)")
    assert (
        await directory_admin.fetchval(
            "SELECT count(*) FROM loom_router.audit WHERE action = 'limit'"
        )
        == 1
    )
    assert local.get_server_pid() == pid and await apg.lock_held(local)
    for table in ("cell_first", "tenancy_mode", "cell_self", "tenancy"):
        assert not await local.fetchval(f"SELECT EXISTS(SELECT 1 FROM loom.{table})")


@pytest.mark.asyncio
async def test_attaching_open_refusal_preserves_guard_order_and_closed_attach_progress(
    control_stores,
):
    client, conn, _ = control_stores
    original = await read(client)
    current = await claim(client, original)

    async def limit(snapshot, **fields):
        return await client.post(
            "/cells/v1/operator/cells/first/limit", json={**expected(snapshot), **fields}
        )

    stale = await limit(original, closed=False, placements_max=99)
    assert stale.status_code == 409 and "generation" in stale.json()["error"]
    for field, value in (("host", "other.example.test"), ("current_fp", {"v": 2})):
        changed = await limit({**current, field: value}, closed=False, placements_max=99)
        assert (
            changed.status_code == 409 and "registration or fingerprints" in changed.json()["error"]
        )
    noop = await limit(current, closed=False, placements_max=99)
    assert noop.status_code == 409 and "serving" in noop.json()["error"]
    assert await conn.fetchval("SELECT placements_max FROM loom_router.cells") is None
    assert not await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM loom_router.audit WHERE action='limit')"
    )
    assert (await limit(current, closed=True)).status_code == 200
    assert (await limit(current, placements_max=0)).status_code == 200
    closed = await read(client)
    assert tuple(await conn.fetchrow("SELECT closed, placements_max FROM loom_router.cells")) == (
        True,
        0,
    )
    assert closed["mutation_generation"] == current["mutation_generation"]

    # An earlier mode request cannot escape the cleanup owner's claim.
    stale_mode = await client.post(
        "/cells/v1/operator/cells/first/attach/enforced",
        json={**expected(original), "deployment_id": "dep-first"},
    )
    assert stale_mode.status_code == 409 and "generation" in stale_mode.json()["error"]
    # Existing held-org attach batches intentionally bypass closed/quota.
    await conn.execute(
        "UPDATE loom_router.cells SET mode='enforced', attach_started_at=now(), current_fp='{\"v\":1}'"
    )
    row = await read(client)
    values = expected(row, generation=False)["expected"]
    control = directory.Control(values, uuid.UUID(row["mutation_generation"]))
    assert await directory.attach_place("first", "dep-first", ["held-org"], expected=control) == (
        1,
        1,
    )
    served = await directory.attach_serving("first", "dep-first", expected=control)
    assert served.row.closed and served.row.state == "serving"
    response = await limit(await read(client), closed=False)
    assert response.status_code == 200, response.text
    assert not response.json()["closed"]


@pytest.mark.asyncio
async def test_pending_attach_batch_cannot_escape_backout_claim(
    control_stores, local_control, monkeypatch
):
    import asyncpg

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_control

    client, directory_admin, _ = control_stores
    local, dsn = local_control
    await directory_admin.execute(
        "UPDATE loom_router.cells SET mode='enforced', attach_started_at=now(), current_fp='{\"v\":1}'"
    )
    await local.execute("INSERT INTO loom.cell_first(is_first,cell) VALUES(true,'first')")
    await local.execute("INSERT INTO loom.tenancy_mode(mode) VALUES('recording')")
    await local.execute("INSERT INTO loom.cell_self(current_fp) VALUES('{\"v\":1}')")
    old_backend = await asyncpg.connect(dsn)
    await apg.take_lock(old_backend, "old attach batch")
    old = await claim(client, await read(client))
    entered, resume = asyncio.Event(), asyncio.Event()
    real = directory.attach_place

    async def delayed(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(directory, "attach_place", delayed)
    pending = asyncio.create_task(
        client.post(
            "/cells/v1/operator/cells/first/attach/place",
            json={**expected(old), "deployment_id": "dep-first", "orgs": ["old-batch"]},
        )
    )

    async def call(path, body):
        response = await client.post("/cells/v1/operator/cells/first" + path, json=body)
        assert response.status_code == 200, response.text
        if path == "/limit":
            resume.set()
            stale = await pending
            assert stale.status_code == 409 and "generation" in stale.json()["error"]
        return response.json()

    try:
        await asyncio.wait_for(entered.wait(), 3)
        await local.execute("SELECT pg_terminate_backend($1)", old_backend.get_server_pid())
        await apg.take_lock(local, "backout successor")
        await cell_control.backout(_owner(local), call, await read(client))
        assert await apg.lock_held(local)
        assert not await local.fetchval("SELECT EXISTS(SELECT 1 FROM loom.cell_self)")
        assert not await directory_admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM loom_router.placements)"
        )
        assert await directory_admin.fetchval("SELECT closed FROM loom_router.cells")
    finally:
        resume.set()
        await pending
        await old_backend.close()
