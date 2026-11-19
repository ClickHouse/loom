"""Cell restore repairs real metadata and guards without creating directory routes."""

from __future__ import annotations

import contextlib

import pytest

from server.cli import cell_attach_pg as apg
from server.cli import cell_restore_pg as local
from server.core import cell, cell_triggers, pg
from server.settings import settings
from tests.integration._pg_database import own_database


@pytest.fixture
async def cell_db(monkeypatch):
    async with own_database() as dsn:
        monkeypatch.setattr(settings, "pg_dsn", dsn)
        monkeypatch.setattr(settings, "cell", "first")
        pg.expect_deployment(None)
        async with pg.dedicated(statement_timeout_ms=5000) as conn:
            await conn.execute(
                "INSERT INTO loom.authority(authority,epoch,deployment_id) VALUES('postgres',1,'dep-restore')"
            )
            await apg.take_lock(conn, "restore-test")
            yield conn
        pg.expect_deployment(None)


def desc(*, first=True, fingerprint=True, mode="enforced", state="serving"):
    return {
        "cell": "first",
        "deployment_id": "dep-restore",
        "is_first": first,
        "state": state,
        "mode": mode,
        "bucket": "",
        "bucket_prefix": "",
        "current_fp": cell.as_value({"deployment_id": "dep-restore"}) if fingerprint else None,
        "next_fp": None,
        "placed": 0,
        "sole_cell": True,
    }


RECORDED = {"authority": "postgres", "epoch": 1, "deployment_id": "dep-restore"}


@pytest.mark.parametrize("has_first", [False, True])
@pytest.mark.parametrize("has_mode", [False, True])
@pytest.mark.parametrize("has_own", [False, True])
@pytest.mark.parametrize("directory_fp", [False, True])
async def test_state_matrix_fills_missing_rows_and_preserves_unapproved_own(
    cell_db,
    has_first,
    has_mode,
    has_own,
    directory_fp,
):
    conn = cell_db
    if has_first:
        await conn.execute("INSERT INTO loom.cell_first(is_first,cell) VALUES(true,'first')")
    if has_mode:
        await conn.execute("INSERT INTO loom.tenancy_mode(mode) VALUES('recording')")
    old = {
        "current_fp": cell.as_value({"deployment_id": "dep-restore", "public_host": "old.test"}),
        "next_fp": None,
    }
    if has_own:
        await conn.execute("INSERT INTO loom.cell_self(current_fp) VALUES($1)", old["current_fp"])
    descriptor = desc(fingerprint=directory_fp, mode="recording", state="attaching")
    result = await local.state(conn, descriptor, RECORDED)
    assert result["first"] == {"is_first": True, "cell": "first"}
    assert result["mode"] == "recording"
    assert result["own"] == (
        {"current_fp": descriptor["current_fp"], "next_fp": None}
        if directory_fp
        else (old if has_own else None)
    )
    assert await local.state(conn, descriptor, RECORDED) == result


async def test_enforced_mode_never_lowers_and_obsolete_next_clears(cell_db):
    conn = cell_db
    descriptor = desc(mode="recording", state="attaching")
    await conn.execute("INSERT INTO loom.tenancy_mode(mode) VALUES('enforced')")
    await conn.execute(
        "INSERT INTO loom.cell_self(current_fp,next_fp) VALUES($1,$1)", descriptor["current_fp"]
    )
    result = await local.state(conn, descriptor, RECORDED)
    assert result["mode"] == "enforced" and result["own"]["next_fp"] is None


@pytest.mark.parametrize(
    "mode,first", [("recording", True), ("enforced", True), ("enforced", False)]
)
async def test_missing_own_partial_gets_every_guard_without_approval(cell_db, mode, first):
    from server.cli.cell_restore_triggers import repair

    conn = cell_db
    descriptor = desc(first=first, fingerprint=False, mode=mode, state="attaching")
    await local.state(conn, descriptor, RECORDED)
    assert await cell.read_state(conn) == "partial"
    with pytest.raises(RuntimeError, match="partial"):
        await cell_triggers.ensure_table(conn, "tenancy")
    await repair(conn, descriptor)
    assert await conn.fetchval("SELECT count(*) FROM loom.cell_self") == 0
    assert await cell.read_state(conn) == "partial"
    assert await cell_triggers.missing(conn) == []
    assert await cell_triggers.first_missing(conn) == []


async def test_wrong_first_guard_definition_and_function_refuse(cell_db):
    from server.cli.cell_restore_triggers import repair

    conn = cell_db
    descriptor = desc()
    await local.state(conn, descriptor, RECORDED)
    await repair(conn, descriptor)
    await conn.execute("DROP TRIGGER cell_first_immutable ON loom.cell_first")
    await conn.execute(
        "CREATE TRIGGER cell_first_immutable BEFORE UPDATE ON loom.cell_first FOR EACH ROW EXECUTE FUNCTION loom.cell_first_immutable()"
    )
    await conn.execute("ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable")
    assert any(d.kind == "cell_first" for d in await cell_triggers.first_missing(conn))
    with pytest.raises(ValueError, match="first"):
        await repair(conn, descriptor)


@pytest.mark.parametrize("mode", ["recording", "enforced"])
async def test_first_attaching_sole_cell_keeps_not_yet_placed_orgs(cell_db, mode):
    conn = cell_db
    await conn.execute("INSERT INTO loom.tenancy(org) VALUES('a'),('b'),('z')")
    descriptor = desc(mode=mode, state="attaching")
    held = local.LocalTenancy(conn)
    await held.compare(["b"], descriptor, last=True)
    assert held.extra == 2
    assert await conn.fetchval("SELECT count(*) FROM loom.tenancy") == 3


@pytest.mark.parametrize(
    "change", [{"state": "serving"}, {"is_first": False}, {"sole_cell": False}]
)
async def test_serving_later_or_nonsole_cell_refuses_unknown_local_tenancy(cell_db, change):
    conn = cell_db
    await conn.execute("INSERT INTO loom.tenancy(org) VALUES('unknown')")
    descriptor = {**desc(state="attaching"), **change}
    with pytest.raises(ValueError, match="permanent placements"):
        await local.LocalTenancy(conn).compare([], descriptor, last=True)


async def test_noop_tenancy_batch_is_marked_and_durable(cell_db):
    conn = cell_db
    descriptor = desc()
    assert await local.tenancy(conn, descriptor, RECORDED, ["acme"]) == 1
    assert await local.tenancy(conn, descriptor, RECORDED, ["acme"]) == 0
    assert await conn.fetchval("SELECT org FROM loom.tenancy WHERE org='acme'") == "acme"


async def test_real_clickhouse_and_postgres_direct_nonce_round_trip(cell_db, monkeypatch):
    import os
    import uuid

    from server.cli.cell_restore_stores import identity, nonces
    from server.core import cell_probe, leases, pg_profile
    from server.core.ch import get_client

    if not os.environ.get("LOOM_CH_HOST"):
        pytest.skip("LOOM_CH_HOST not set")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    ch = get_client()
    await ch.connect()
    table = f"loom.restore_nonce_authority_{uuid.uuid4().hex}"

    class AuthorityTable:
        # Isolate the singleton authority without replacing shared deployment identity.
        async def query_one(self, sql, params=None):
            if sql == pg_profile._CH_RECORD:
                sql = sql.replace("loom.storage_authority", table)
            return await ch.query_one(sql, params)

        async def execute(self, sql, params=None):
            return await ch.execute(sql, params)

    stack = contextlib.AsyncExitStack()
    stack.push_async_callback(ch.disconnect)
    try:
        await ch.execute(f"CREATE TABLE {table} AS loom.storage_authority")
        stack.push_async_callback(ch.execute, f"DROP TABLE IF EXISTS {table}")
        paired = AuthorityTable()
        with pytest.raises(ValueError, match="records no Postgres authority"):
            await identity(paired, cell_db)
        for wrong in (
            {**RECORDED, "deployment_id": "other-deployment"},
            {**RECORDED, "epoch": 2},
        ):
            await ch.insert(table, [wrong])
            with pytest.raises(pg.AuthorityMismatch):
                await identity(paired, cell_db)
            await ch.execute(f"TRUNCATE TABLE {table}")
        assert await cell_db.fetchval("SELECT count(*) FROM loom.cell_probe") == 0
        await ch.insert(table, [RECORDED])
        assert await identity(paired, cell_db) == RECORDED
        await nonces(paired, cell_db)
        assert (
            await cell_db.fetchval(
                "SELECT count(*) FROM loom.cell_probe WHERE name=$1", cell_probe.NONCE_NAME
            )
            == 0
        )
        assert (
            await ch.query_one(
                f"SELECT holder FROM {leases.LOCK_TABLE} WHERE name={{name:String}}",
                {"name": cell_probe.NONCE_NAME},
            )
            is None
        )
    except BaseException:  # Owned cleanup preserves the nonce or identity failure.
        with contextlib.suppress(BaseException):
            await stack.aclose()
        raise
    else:
        await stack.aclose()


async def test_restore_budget_closes_real_backend_stalled_in_transaction_rollback(
    cell_db, monkeypatch
):
    import asyncio
    import time

    import asyncpg

    from server.cli import cell_restore

    conn = cell_db
    pid = conn.get_server_pid()
    execute = asyncpg.Connection.execute
    rolled_back = asyncio.Event()

    async def stalled_rollback(self, query, *args, **kwargs):
        if self is conn and query.startswith("ROLLBACK"):
            rolled_back.set()
            await execute(self, "SELECT pg_sleep(10)")
        return await execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", stalled_rollback)

    async def work():
        async with conn.transaction():
            raise ValueError("failure before rollback")

    budget = cell_restore.Budget(0.2)
    budget.conn = conn
    observer = await asyncpg.connect(settings.pg_dsn)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="--wait"):
            await budget.call(work())
        assert rolled_back.is_set() and time.monotonic() - started < 0.5
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
        )
    finally:
        await observer.close(timeout=1)


async def test_trigger_ddl_failure_leaves_public_close_to_restore_owner(cell_db):
    import asyncio

    import asyncpg

    from server.cli import cell_restore

    conn = cell_db
    pid = conn.get_server_pid()
    budget = cell_restore.Budget(1)
    budget.conn = conn
    observer = await asyncpg.connect(settings.pg_dsn)

    async def work():
        async with asyncio.timeout(0.05):
            await cell_triggers._run_ddl(conn, "tenancy", ["SELECT pg_sleep(10)"], attempts=1)

    try:
        with pytest.raises(TimeoutError):
            await budget.call(work())
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
        )
    finally:
        await observer.close(timeout=1)


async def test_failed_trigger_repair_keeps_original_error_until_owner_close(cell_db, monkeypatch):
    import asyncpg

    from server.cli import cell_restore, cell_restore_triggers

    conn = cell_db
    pid = conn.get_server_pid()
    budget = cell_restore.Budget(1)
    budget.conn = conn
    observer = await asyncpg.connect(settings.pg_dsn)
    original = None

    async def failing_tables(conn, descriptor):
        nonlocal original
        assert await observer.fetchval(
            "SELECT count(*) >= 2 FROM pg_locks WHERE pid=$1 AND locktype='advisory'", pid
        )
        try:
            await cell_triggers._run_ddl(conn, "tenancy", ["SELECT 1 / 0"], attempts=1)
        except asyncpg.DivisionByZeroError as error:
            original = error
            raise

    monkeypatch.setattr(cell_restore_triggers, "_tables", failing_tables)
    try:
        with pytest.raises(asyncpg.DivisionByZeroError) as caught:
            await budget.call(cell_restore_triggers.repair(conn, desc()))
        assert caught.value is original
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
        )
    finally:
        await observer.close(timeout=1)


@pytest.mark.parametrize("opening", ["_check_deployment", "init_connection"])
@pytest.mark.parametrize("ending", ["deadline", "cancel", "second_cancel"])
async def test_restore_owns_raw_backend_before_initialization(
    cell_db, monkeypatch, opening, ending
):
    import argparse
    import asyncio
    import time

    import asyncpg
    import httpx

    from server.cli import cell_restore

    observer = await asyncpg.connect(settings.pg_dsn)
    owned = {}
    started, closing = asyncio.Event(), asyncio.Event()
    close = pg.close_owned
    closes = 0

    async def public_close(conn, *, end):
        nonlocal closes
        closes += 1
        closing.set()
        await asyncio.sleep(0.02)
        await close(conn, end=end)

    async def blocked(conn):
        owned["pid"] = conn.get_server_pid()
        await conn.execute("SELECT pg_advisory_lock(846233)")
        started.set()
        await conn.execute("SELECT pg_sleep(10)")

    monkeypatch.setattr(pg, opening, blocked)
    monkeypatch.setattr(pg, "close_owned", public_close)
    seconds = 0.3 if ending == "deadline" else 1.0
    budget = cell_restore.Budget(seconds)
    task = asyncio.create_task(
        cell_restore.Restore(
            argparse.Namespace(cell="first", wait=seconds),
            httpx.URL("http://127.0.0.1:7901"),
            budget,
        ).run(object())
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with asyncio.timeout(0.2):
            for _ in range(200):
                if await observer.fetchval(
                    "SELECT wait_event='PgSleep' FROM pg_stat_activity WHERE pid=$1", owned["pid"]
                ):
                    break
                await asyncio.sleep(0.001)
            else:
                pytest.fail("raw initialization never reached its actual sleeping query")
        before = time.monotonic()
        if ending != "deadline":
            task.cancel("first initialization cancellation")
        if ending == "second_cancel":
            await asyncio.wait_for(closing.wait(), 0.3)
            task.cancel("second initialization cancellation")
        if ending == "deadline":
            with pytest.raises(TimeoutError, match="--wait"):
                await task
        else:
            with pytest.raises(asyncio.CancelledError, match="first initialization cancellation"):
                await task
        assert time.monotonic() - before < 0.6 and time.monotonic() <= budget.end + 0.1
        assert closes == 1
        assert owned["pid"] != cell_db.get_server_pid()
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", owned["pid"]
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", owned["pid"]
        )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if "pid" in owned:
            await observer.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid=$1", owned["pid"]
            )
        await observer.close(timeout=1)


async def test_late_raw_connection_stays_with_closed_budget_owner(cell_db, monkeypatch):
    import argparse
    import asyncio
    import time

    import asyncpg
    import httpx

    from server.cli import cell_restore

    connect, close = asyncpg.connect, pg.close_owned
    owned, initialized, closed = {}, [], []

    async def late(*args, **kwargs):
        conn = await connect(*args, **kwargs)
        owned["pid"] = conn.get_server_pid()
        await conn.execute("SELECT pg_advisory_lock(846234)")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return conn

    async def initialize(conn):
        initialized.append(conn)

    async def public_close(conn, *, end):
        closed.append(conn.get_server_pid())
        await close(conn, end=end)

    observer = await connect(settings.pg_dsn)
    monkeypatch.setattr(asyncpg, "connect", late)
    monkeypatch.setattr(pg, "_check_deployment", initialize)
    monkeypatch.setattr(pg, "close_owned", public_close)
    budget = cell_restore.Budget(0.4)
    try:
        with pytest.raises(TimeoutError, match="--wait"):
            await cell_restore.Restore(
                argparse.Namespace(cell="first", wait=0.4),
                httpx.URL("http://127.0.0.1:7901"),
                budget,
            ).run(object())
        assert time.monotonic() <= budget.end + 0.1
        assert closed == [owned["pid"]] and initialized == []
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", owned["pid"]
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", owned["pid"]
        )
    finally:
        if "pid" in owned:
            await observer.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid=$1", owned["pid"]
            )
        await observer.close(timeout=1)


@pytest.mark.parametrize("opening", ["_check_deployment", "init_connection"])
async def test_raw_initialization_preserves_original_error_after_public_close(
    cell_db, monkeypatch, opening
):
    import argparse

    import asyncpg
    import httpx

    from server.cli import cell_restore

    failure = ValueError("initialization programmer error")
    owned = {}

    async def fail(conn):
        owned["pid"] = conn.get_server_pid()
        await conn.execute("SELECT pg_advisory_lock(846235)")
        raise failure

    monkeypatch.setattr(pg, opening, fail)
    observer = await asyncpg.connect(settings.pg_dsn)
    try:
        with pytest.raises(ValueError) as caught:
            await cell_restore.Restore(
                argparse.Namespace(cell="first", wait=1),
                httpx.URL("http://127.0.0.1:7901"),
                cell_restore.Budget(1),
            ).run(object())
        assert caught.value is failure
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", owned["pid"]
        )
        assert not await observer.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", owned["pid"]
        )
    finally:
        await observer.close(timeout=1)
