"""The update coordinator commits real local and directory state on isolated stores."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from argparse import Namespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.cli import cell_control, cell_update_participants, cell_update_pg, cell_update_probe
from server.cli.cell_update_run import Run
from server.core import cell, deployment_config, pg
from server.router import app
from server.settings import settings
from tests.integration.test_cell_control_fence import control_stores as control_stores
from tests.integration.test_cell_control_fence import local_control as local_control
from tests.unit.cell_control_owner import owner
from tests.unit.test_cell_update_run import value

REAL_POST = cell_update_probe.post
REAL_STORES_CHECK = app.Router._stores_check


async def _assert_widening_rolled_back(local_dsn, directory_conn, backend_pid, before):
    import asyncpg

    observer = await asyncpg.connect(local_dsn)
    try:
        await pg.init_connection(observer)
        local = await cell_update_pg.read(observer, "first")
        assert local == before
        assert await observer.fetchval("SELECT count(*) FROM loom.tenancy") == 0
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", backend_pid
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", backend_pid
        )
    finally:
        await observer.close()
    recorded = await directory_conn.fetchrow("SELECT current_fp,next_fp FROM loom_router.cells")
    assert json.loads(recorded["current_fp"]) == value()
    assert json.loads(recorded["next_fp"]) == value("new", orgs=("permanent",))
    assert (
        await directory_conn.fetchval(
            "SELECT cell FROM loom_router.placements WHERE org=$1", "permanent"
        )
        == "first"
    )


@pytest.fixture
async def update_stores(control_stores, local_control, monkeypatch, tmp_path):
    client, directory_conn, _ = control_stores
    local_conn, local_dsn = local_control
    token = "lco_" + "f" * 128
    monkeypatch.setattr(settings, "router_operator_token", token)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )
    current = value()
    await directory_conn.execute(
        "UPDATE loom_router.cells SET state='serving',mode='enforced', current_fp=$1::jsonb, "
        "keys_fp=$2, issuers=ARRAY['old']",
        json.dumps(current),
        deployment_config.fingerprints(settings)["keys_fp"],
    )
    await local_conn.execute("INSERT INTO loom.cell_first(is_first,cell) VALUES(true,'first')")
    await local_conn.execute("INSERT INTO loom.tenancy_mode(mode) VALUES('enforced')")
    await local_conn.execute("INSERT INTO loom.cell_self(current_fp) VALUES($1::jsonb)", current)
    await local_conn.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
    path = tmp_path / "target.yaml"
    path.write_text("version: 1\nsettings: {}\n")
    events = []
    real_write = cell_update_pg.write

    async def write(owned, expected, row, target, pending, orgs):
        events.append("local-write")
        # A successor is valid only in a fresh owner after the original backend died.
        assert owned.conn is local_conn or local_conn.is_closed()
        # The observed directory reservation is already durable before local widening.
        recorded = await directory_conn.fetchrow("SELECT current_fp,next_fp FROM loom_router.cells")
        assert (json.loads(recorded["current_fp"]) if recorded["current_fp"] else None) == row[
            "current_fp"
        ]
        assert (json.loads(recorded["next_fp"]) if recorded["next_fp"] else None) == row["next_fp"]
        return await real_write(owned, expected, row, target, pending, orgs)

    async def post(run, route, body):
        events.append("directory-" + body["action"])
        return await run.call(
            route,
            {
                **body,
                **cell_control.preconditions(run.row),
                "nonces": {"postgres": "pg", "clickhouse": "ch", "bucket": None},
            },
        )

    async def stores_checked(*_):
        return None

    from server.router.app import Router

    monkeypatch.setattr(Router, "_stores_check", stores_checked)
    monkeypatch.setattr(cell_update_probe, "post", post)
    monkeypatch.setattr(cell_update_pg, "write", write)

    def make(action="config", target=None):
        from server.cli import cell_update_config

        monkeypatch.setattr(
            cell_update_config,
            "target_fingerprint",
            lambda *_: target or value("new", orgs=("permanent",)),
        )
        args = Namespace(
            cell="first",
            config=path if action == "config" else None,
            finish=action == "finish",
            revert=action == "revert",
            probe_url=None,
            wait=0,
        )
        return Run(None, args, httpx.URL("http://router"), client._transport)

    yield make, local_conn, directory_conn, owner(local_conn), events, local_dsn


@pytest.mark.asyncio
async def test_acceptance_error_rolls_back_tenancy_and_preserves_directory_retry(update_stores):
    import asyncpg

    make, local_conn, directory_conn, owned, _, local_dsn = update_stores
    backend_pid = local_conn.get_server_pid()
    before = await cell_update_pg.read(local_conn, "first")
    await local_conn.execute(
        "CREATE FUNCTION reject_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'acceptance refused by test'; END $$; "
        "CREATE TRIGGER reject_acceptance BEFORE UPDATE ON loom.cell_self "
        "FOR EACH ROW EXECUTE FUNCTION reject_acceptance()"
    )
    with pytest.raises(asyncpg.RaiseError, match="acceptance refused by test"):
        await make().start(owned)
    assert owned.closed and local_conn.is_closed()
    await _assert_widening_rolled_back(local_dsn, directory_conn, backend_pid, before)


@pytest.mark.asyncio
async def test_cancelled_acceptance_releases_backend_and_rolls_back_tenancy(update_stores):
    import asyncpg

    make, local_conn, directory_conn, owned, _, local_dsn = update_stores
    backend_pid = local_conn.get_server_pid()
    before = await cell_update_pg.read(local_conn, "first")
    async with contextlib.AsyncExitStack() as cleanup:
        try:
            blocker = await asyncpg.connect(local_dsn)
            cleanup.push_async_callback(blocker.close)
            transaction = blocker.transaction()
            await transaction.start()
            cleanup.push_async_callback(transaction.rollback)
            observer = await asyncpg.connect(local_dsn)
            cleanup.push_async_callback(observer.close)
            await blocker.fetchrow("SELECT id FROM loom.cell_self FOR UPDATE")
            task = asyncio.create_task(make().start(owned))
            cleanup.push_async_callback(asyncio.gather, task, return_exceptions=True)
            cleanup.callback(task.cancel)
            async with asyncio.timeout(4):
                while not await observer.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 "
                    "AND wait_event_type='Lock' AND query LIKE 'UPDATE loom.cell_self%')",
                    backend_pid,
                ):
                    assert not task.done()
                    await asyncio.sleep(0.005)
            task.cancel("acceptance-stop")
            with pytest.raises(asyncio.CancelledError, match="acceptance-stop"):
                await asyncio.wait_for(task, timeout=4)
            assert owned.closed and local_conn.is_closed()
        except BaseException:  # Every acquired resource must close before failure escapes.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise
    await _assert_widening_rolled_back(local_dsn, directory_conn, backend_pid, before)


@pytest.mark.asyncio
async def test_coordinator_config_and_retry_commit_both_real_stores_once(update_stores):
    make, local_conn, directory_conn, owned, events, _ = update_stores
    await make().start(owned)
    await make().start(owned)
    expected = value("new", orgs=("permanent",))
    local = await cell_update_pg.read(local_conn, "first")
    assert local.current == value() and local.pending == expected
    assert [dict(row) for row in await local_conn.fetch("SELECT org FROM loom.tenancy")] == [
        {"org": "permanent"}
    ]
    assert await directory_conn.fetchval("SELECT placed FROM loom_router.cells") == 1
    assert await directory_conn.fetchval("SELECT count(*) FROM loom_router.placements") == 1
    assert events == ["directory-config", "local-write", "directory-config", "local-write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["finish", "revert"])
async def test_actual_cutoff_precedes_turnover_and_directory_release(
    update_stores, monkeypatch, action
):
    make, local_conn, directory_conn, owned, events, _ = update_stores
    await make().start(owned)
    # Lost local repair is reproduced by deleting only this scratch tenancy row.
    await local_conn.execute("DELETE FROM loom.tenancy WHERE org='permanent'")
    target = value("new", orgs=("permanent",)) if action == "finish" else value()

    async def barrier(run, wanted):
        assert wanted == target
        local = await cell_update_pg.read(local_conn, "first")
        assert local.current == target and local.pending is None
        assert await local_conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM loom.tenancy WHERE org='permanent')"
        )
        assert await directory_conn.fetchval("SELECT next_fp IS NOT NULL FROM loom_router.cells")
        events.append("turnover")

    monkeypatch.setattr(cell_update_participants, "settle", barrier)
    await make(action).start(owned)
    stored = await directory_conn.fetchrow(
        "SELECT current_fp,next_fp,placed FROM loom_router.cells"
    )
    assert json.loads(stored["current_fp"]) == target and stored["next_fp"] is None
    assert stored["placed"] == 1
    assert events[-3:] == ["local-write", "turnover", "directory-" + action]


@pytest.mark.asyncio
async def test_directory_commit_then_local_failure_is_repaired_without_duplicate_placement(
    update_stores, monkeypatch
):
    make, local_conn, directory_conn, owned, _, _ = update_stores
    real = cell_update_pg.write
    monkeypatch.setattr(
        cell_update_pg, "write", AsyncMock(side_effect=OSError("local unavailable"))
    )
    with pytest.raises(OSError, match="local unavailable"):
        await make().start(owned)
    assert await directory_conn.fetchval("SELECT next_fp IS NOT NULL FROM loom_router.cells")
    assert (await cell_update_pg.read(local_conn, "first")).pending is None
    monkeypatch.setattr(cell_update_pg, "write", real)
    await make().start(owned)
    assert (await cell_update_pg.read(local_conn, "first")).pending == value(
        "new", orgs=("permanent",)
    )
    assert await directory_conn.fetchval("SELECT placed FROM loom_router.cells") == 1


@pytest.mark.asyncio
async def test_turnover_timeout_leaves_real_directory_dual_and_local_single_for_retry(
    update_stores, monkeypatch
):
    make, local_conn, directory_conn, owned, _, _ = update_stores
    await make().start(owned)
    monkeypatch.setattr(
        cell_update_participants, "settle", AsyncMock(side_effect=TimeoutError("old process"))
    )
    with pytest.raises(TimeoutError, match="old process"):
        await make("finish").start(owned)
    assert (await cell_update_pg.read(local_conn, "first")).pending is None
    assert await directory_conn.fetchval("SELECT next_fp IS NOT NULL FROM loom_router.cells")
    monkeypatch.setattr(cell_update_participants, "settle", AsyncMock())
    await make("finish").start(owned)
    assert await directory_conn.fetchval("SELECT next_fp IS NULL FROM loom_router.cells")


@pytest.mark.asyncio
async def test_prebegin_real_local_pair_only_never_reserves_or_places(update_stores):
    make, local_conn, directory_conn, owned, events, _ = update_stores
    await directory_conn.execute(
        "UPDATE loom_router.cells SET state='attaching',current_fp=NULL,next_fp=NULL,issuers='{}'"
    )
    await make().start(owned)
    assert (await cell_update_pg.read(local_conn, "first")).pending == value(
        "new", orgs=("permanent",)
    )
    assert await directory_conn.fetchval(
        "SELECT current_fp IS NULL AND next_fp IS NULL FROM loom_router.cells"
    )
    assert await directory_conn.fetchval("SELECT count(*) FROM loom_router.placements") == 0
    assert await local_conn.fetchval("SELECT count(*) FROM loom.tenancy") == 0
    assert events == ["local-write"]


@pytest.mark.asyncio
async def test_changed_root_keys_refuse_before_real_local_cutoff(update_stores, monkeypatch):
    make, local_conn, directory_conn, owned, events, _ = update_stores
    await make().start(owned)
    before = await cell_update_pg.read(local_conn, "first")
    await directory_conn.execute("UPDATE loom_router.cells SET keys_fp='changed'")
    barrier = AsyncMock(side_effect=AssertionError("must not reach barrier"))
    monkeypatch.setattr(cell_update_participants, "settle", barrier)
    with pytest.raises(cell_control.Refused, match="root-secret fingerprint changed"):
        await make("finish").start(owned)
    assert await cell_update_pg.read(local_conn, "first") == before
    assert events.count("local-write") == 1
    barrier.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("aba", [False, True])
async def test_delayed_actual_finish_cannot_release_after_backend_death_and_fresh_rewiden(
    update_stores, monkeypatch, aba
):
    import asyncio

    import asyncpg

    from server.router import directory

    make, local_conn, directory_conn, owned, _, dsn = update_stores
    await make().start(owned)
    monkeypatch.setattr(cell_update_participants, "settle", AsyncMock())
    entered, resume = asyncio.Event(), asyncio.Event()
    real_change = directory.update_configuration
    delayed_once = False

    async def delayed(*args, **kwargs):
        nonlocal delayed_once
        if args[2] == "finish" and not delayed_once:
            delayed_once = True
            entered.set()
            await resume.wait()
        return await real_change(*args, **kwargs)

    monkeypatch.setattr(directory, "update_configuration", delayed)
    async with contextlib.AsyncExitStack() as cleanup:
        try:
            pending = asyncio.create_task(make("finish").start(owned))
            cleanup.push_async_callback(asyncio.gather, pending, return_exceptions=True)
            cleanup.callback(pending.cancel)
            cleanup.callback(resume.set)
            admin = await asyncpg.connect(dsn)
            cleanup.push_async_callback(admin.close)
            successor = await asyncpg.connect(dsn)
            cleanup.push_async_callback(successor.close)
            await directory._init(successor)
            await asyncio.wait_for(entered.wait(), 5)
            old_generation = str(
                await directory_conn.fetchval("SELECT mutation_generation FROM loom_router.cells")
            )
            await admin.execute("SELECT pg_terminate_backend($1)", local_conn.get_server_pid())
            assert await successor.fetchval("SELECT pg_try_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
            fresh_owner = owner(successor)
            await make().start(fresh_owner)
            if aba:
                await make("revert").start(fresh_owner)
                await make().start(fresh_owner)
            assert old_generation != str(
                await directory_conn.fetchval("SELECT mutation_generation FROM loom_router.cells")
            )
            assert (await cell_update_pg.read(successor, "first")).pending is not None
            resume.set()
            with pytest.raises(cell_control.Refused, match="generation|attach lock|lock session"):
                await pending
            stored = await directory_conn.fetchrow(
                "SELECT current_fp,next_fp,placed FROM loom_router.cells"
            )
            assert json.loads(stored["current_fp"]) == value()
            assert json.loads(stored["next_fp"]) == value("new", orgs=("permanent",))
            assert stored["placed"] == 1
            assert (await cell_update_pg.read(successor, "first")).pending is not None
        except BaseException:  # Owned cleanup must preserve the original test failure.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise


@pytest.mark.asyncio
async def test_local_cas_refuses_changed_state_before_tenancy_repair(update_stores):
    make, local_conn, _, owned, _, _ = update_stores
    await make().start(owned)
    initial = await cell_update_pg.read(local_conn, "first")
    run = make("finish")
    run.row = await run.call("")
    await local_conn.execute("UPDATE loom.cell_self SET next_fp=NULL")
    with pytest.raises(cell_control.Refused, match="acceptance changed"):
        await cell_update_pg.write(owned, initial, run.row, initial.current, None, ["not-held"])
    # The owner discards the failed transaction; the command cannot reuse the backend.
    assert owned.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["valid", "wrong", "busy"])
async def test_update_uses_fresh_real_pg_ch_nonces_and_cleans_only_its_values(
    update_stores, monkeypatch, proof
):
    import os
    from uuid import uuid4

    from server.cli import cell_prepare
    from server.core import cell_probe, leases
    from server.core.ch import CHClient, query_tag

    if not os.environ.get("LOOM_CH_HOST"):
        pytest.skip("Set LOOM_CH_HOST to the owned disposable ClickHouse")
    make, local_conn, directory_conn, owned, _, _ = update_stores
    database = "update_nonce_" + uuid4().hex
    table = database + ".locks"
    client = CHClient()
    await client.connect()
    with query_tag("test.cell_update.nonces"):
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.push_async_callback(client.disconnect)
            try:
                await client.execute("CREATE DATABASE {db:Identifier}", {"db": database})
                cleanup.push_async_callback(
                    client.execute, "DROP DATABASE {db:Identifier} SYNC", {"db": database}
                )
                await client.execute(
                    "CREATE TABLE {db:Identifier}.locks (name String, holder String, "
                    "acquired_at DateTime64(3), renewed_at DateTime64(3), ttl_s UInt32, "
                    "build_commit String) ENGINE=KeeperMap({path:String}) PRIMARY KEY name",
                    {"db": database, "path": "/loom/test/" + database},
                )
                monkeypatch.setattr(
                    leases, "LOCK_RELEASE", leases.LOCK_RELEASE.replace(leases.LOCK_TABLE, table)
                )
                monkeypatch.setattr(leases, "LOCK_TABLE", table)
                monkeypatch.setattr("server.core.ch.get_client", lambda: client)
                monkeypatch.setattr(cell_update_probe, "post", REAL_POST)
                monkeypatch.setattr(app.Router, "_stores_check", REAL_STORES_CHECK)
                monkeypatch.setattr(cell_prepare, "_sleep", AsyncMock())
                await directory_conn.execute(
                    "UPDATE loom_router.cells SET probe_url='http://probe',upstream_url='http://main'"
                )
                requests = []

                async def network(request):
                    if request.url.host == "main":
                        return httpx.Response(200, json={"deployment_id": "dep-first"})
                    requests.append(request)
                    actual = await cell_probe.nonces()
                    assert actual["postgres"] and actual["clickhouse"]
                    if proof == "busy" and len(requests) == 1:
                        return httpx.Response(429, headers={"Retry-After": "1"})
                    return httpx.Response(
                        200,
                        json={
                            "deployment_id": "dep-first",
                            "bucket": {"name": "", "prefix": ""},
                            "nonces": actual | ({"postgres": "old"} if proof == "wrong" else {}),
                        },
                    )

                router = app.Router(probe_transport=httpx.MockTransport(network))
                run = make()
                run.transport = httpx.ASGITransport(app=app.build_app(router))
                run.ch, run.args.wait = client, 3
                if proof == "wrong":
                    with pytest.raises(cell_prepare.Refused, match="does not read the stores"):
                        await run.start(owned)
                    assert await directory_conn.fetchval(
                        "SELECT next_fp IS NULL FROM loom_router.cells"
                    )
                    assert (await cell_update_pg.read(local_conn, "first")).pending is None
                else:
                    await run.start(owned)
                    assert (await cell_update_pg.read(local_conn, "first")).pending is not None
                assert len(requests) == (2 if proof == "busy" else 1)
                assert await cell_probe.nonces() == {
                    "postgres": None,
                    "clickhouse": None,
                    "bucket": None,
                }
            except BaseException:  # Owned cleanup must not replace setup or body failure.
                with contextlib.suppress(BaseException):
                    await cleanup.aclose()
                raise
