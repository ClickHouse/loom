"""Placement retries survive promotion of a private physical standby.

Only the dedicated CI drill enables this file. Its pair is separate from the
integration server's stores because it pauses replication and stops the primary.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest

from server.cli import cell_place
from server.core import cell, pg, provider_process
from server.core.schema import pg_migrate
from server.router import directory
from server.settings import settings
from tests.integration._pg_database import create_database

PRIMARY = "loom-scale-place-drill-pg"
STANDBY = PRIMARY + "-standby"
ORG = "placement-durability"
DEPLOYMENT = "placement-drill"


async def _docker(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
        raise
    assert process.returncode == 0, stderr.decode()
    return stdout.decode().strip()


async def _sql(container: str, database: str, sql: str) -> str:
    return await _docker("exec", container, "psql", "-U", "loom", "-d", database, "-Atc", sql)


@dataclass
class Drill:
    observer: Any
    directory_dsn: str
    local_dsn: str
    tasks: list[asyncio.Task] = field(default_factory=list)
    paused: bool = False

    def task(self, work: Any) -> asyncio.Task:
        task = asyncio.create_task(work)
        self.tasks.append(task)
        return task

    async def pause(self) -> None:
        await _docker("pause", STANDBY)
        self.paused = True

    async def resume(self) -> None:
        await _docker("unpause", STANDBY)
        self.paused = False

    async def waiting(self, task: asyncio.Task, application: str) -> int:
        async with asyncio.timeout(5):
            while True:
                assert not task.done(), "placement acknowledged before the standby resumed"
                pid = await self.observer.fetchval(
                    "SELECT pid FROM pg_stat_activity WHERE application_name = $1 "
                    "AND wait_event = 'SyncRep'",
                    application,
                )
                if pid is not None:
                    assert not task.done()
                    return pid
                await asyncio.sleep(0.02)

    async def seed(self, dsn: str, sql: str, *, marked: bool = False) -> None:
        """Cancel a standby-blocked COMMIT and drain its acknowledgement before returning."""
        application = f"placement-seed-{len(self.tasks)}"
        conn = await asyncpg.connect(
            dsn,
            timeout=5,
            command_timeout=5,
            server_settings={
                "application_name": application,
                "synchronous_commit": "on",
                **(pg.marker_settings() if marked else {}),
            },
        )
        try:
            await conn.execute("BEGIN")
            await conn.execute(sql)
            commit = self.task(conn.execute("COMMIT"))
            await self.waiting(commit, application)
            commit.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(commit, 5)
            # Client cancellation precedes the server releasing the transaction.
            # Drain it before termination can cancel the driver's CancelRequest.
            await conn.close(timeout=5)
        finally:
            conn.terminate()


async def _cleanup_drill(state: Drill | None, observer: Any) -> None:
    errors: list[BaseException] = []
    if state is not None:
        try:
            if state.paused:
                await state.resume()
        except BaseException as exc:  # noqa: BLE001 — remaining owned cleanup must run
            errors.append(exc)
        for task in state.tasks:
            if not task.done():
                task.cancel()
        deadline = asyncio.get_running_loop().time() + 15
        for close in (
            lambda: asyncio.gather(*state.tasks, return_exceptions=True),
            directory.close,
            pg.close_pool,
        ):
            try:
                async with asyncio.timeout_at(deadline):
                    await close()
            except BaseException as exc:  # noqa: BLE001 — attempt each close within the shared bound
                errors.append(exc)
    try:
        observer.terminate()
    except BaseException as exc:  # noqa: BLE001 — preserve the first cleanup failure
        errors.append(exc)
    if errors:
        raise errors[0]


@pytest.fixture
async def drill(monkeypatch):
    if not os.environ.get("LOOM_CELL_PLACE_DRILL"):
        pytest.skip("LOOM_CELL_PLACE_DRILL not set: requires its disposable physical standby")
    assert os.environ["LOOM_CELL_PLACE_DRILL"] == "1"
    dsn = os.environ["LOOM_CELL_PLACE_DRILL_DSN"]
    observer = await asyncpg.connect(dsn, timeout=5, command_timeout=5)
    state = None
    try:
        # Refuse to disrupt a container unless the explicit DSN identifies this exact pair.
        identifier = str(
            await observer.fetchval("SELECT system_identifier FROM pg_control_system()")
        )
        for container in (PRIMARY, STANDBY):
            assert await _docker("inspect", "--format", "{{.Name}}", container) == "/" + container
            assert (
                await _sql(container, "loom", "SELECT system_identifier FROM pg_control_system()")
                == identifier
            )
        assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "t"
        assert (
            await observer.fetchval(
                "SELECT count(*) FROM pg_stat_replication WHERE application_name = 'loom_standby' "
                "AND sync_state = 'sync' AND state = 'streaming'"
            )
            == 1
        )
        _, directory_dsn = await create_database(dsn)
        _, local_dsn = await create_database(dsn)
        state = Drill(observer, directory_dsn, local_dsn)
        monkeypatch.setattr(directory, "_pool", None)
        monkeypatch.setattr(settings, "router_pg_dsn", directory_dsn)
        monkeypatch.setattr(settings, "pg_dsn", local_dsn)
        monkeypatch.setattr(settings, "storage_authority", "postgres")
        monkeypatch.setattr(settings, "cell", "first")
        monkeypatch.setattr(settings, "pg_statement_timeout_ms", 10_000)
        previous_expected = pg._expected
        previous_orgs = cell._code_orgs
        pg.expect_deployment(None)
        from server.code.domain.index import repos

        cell.set_code_orgs(repos.operator_orgs)
        try:
            async with asyncio.timeout(60):
                await _initialize(state)
            yield state
        finally:
            pg.expect_deployment(previous_expected)
            cell._code_orgs = previous_orgs
    except BaseException:  # Cleanup must not replace the setup failure.
        with contextlib.suppress(BaseException):
            await _cleanup_drill(state, observer)
        raise
    else:
        await _cleanup_drill(state, observer)


async def _initialize(drill: Drill) -> None:
    @contextlib.asynccontextmanager
    async def connect():
        conn = await asyncpg.connect(drill.local_dsn, command_timeout=10)
        try:
            yield conn
        finally:
            conn.terminate()

    await pg_migrate.migrate(connect=connect)
    await directory.migrate()
    fingerprint = cell.as_value(cell.fingerprint(DEPLOYMENT))
    async with connect() as conn:
        await conn.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, $1)",
            DEPLOYMENT,
        )
        await conn.execute("INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'first')")
        await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
        await conn.execute(
            "INSERT INTO loom.cell_self (current_fp) VALUES ($1::jsonb)", json.dumps(fingerprint)
        )
    async with directory._connection() as conn:
        await conn.execute(
            "INSERT INTO loom_router.cells (cell, is_first, host, state, mode, credential_sha256, deployment_id, current_fp) "
            "VALUES ('first', true, 'first.example.test', 'serving', 'enforced', $1, $2, $3)",
            "3" * 64,
            DEPLOYMENT,
            fingerprint,
        )


async def _directory_retry(drill: Drill) -> None:
    await drill.pause()
    await drill.seed(
        drill.directory_dsn,
        "INSERT INTO loom_router.placements (org, cell) VALUES ('placement-durability', 'first'); "
        "UPDATE loom_router.cells SET placed = placed + 1 WHERE cell = 'first'",
    )
    assert await directory.lookup(ORG) == ("first", "first.example.test")
    retry = drill.task(directory.place(ORG, "first", lambda _: "", actor="operator"))
    await drill.waiting(retry, "loom-router")
    await drill.resume()
    assert await asyncio.wait_for(retry, 5) == ("first", DEPLOYMENT)


async def _local_retry(drill: Drill, monkeypatch) -> None:
    async def verified_directory(router, name, org, deployment, fingerprint):
        # The directory retry above already acknowledged this winner's physical flush.
        assert (name, org, deployment) == ("first", ORG, DEPLOYMENT)
        assert await directory.lookup(org) == (name, "first.example.test")

    monkeypatch.setattr(cell_place, "_directory", verified_directory)
    await drill.pause()
    await drill.seed(
        drill.local_dsn,
        "INSERT INTO loom.tenancy (org) VALUES ('placement-durability')",
        marked=True,
    )
    assert (
        await _sql(
            PRIMARY,
            urlsplit(drill.local_dsn).path.removeprefix("/"),
            "SELECT org FROM loom.tenancy",
        )
        == ORG
    )
    args = argparse.Namespace(cell="first", org=ORG)
    application = provider_process.participant_tag(provider_process.process_identity())
    cancelled = drill.task(cell_place.place(args, httpx.URL("http://router.example.test")))
    pid = await drill.waiting(cancelled, application)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled, cell_place._bound_s() + 1)
    async with asyncio.timeout(5):
        while True:
            if not await drill.observer.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = $1)", pid
            ):
                break
            await asyncio.sleep(0.02)
    assert not await drill.observer.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = $1)", pid
    )
    retry = drill.task(cell_place.place(args, httpx.URL("http://router.example.test")))
    await drill.waiting(retry, application)
    await drill.resume()
    assert await asyncio.wait_for(retry, 5) == {
        "org": ORG,
        "cell": "first",
        "deployment_id": DEPLOYMENT,
    }


@pytest.mark.recovery
@pytest.mark.destructive
async def test_duplicate_placement_survives_physical_standby_promotion(drill, monkeypatch):
    async with asyncio.timeout(120):
        await _directory_retry(drill)
        await _local_retry(drill, monkeypatch)
        await directory.close()
        await pg.close_pool()
        drill.observer.terminate()
        await _docker("kill", "--signal", "KILL", PRIMARY)
        await _docker(
            "exec",
            "--user",
            "postgres",
            STANDBY,
            "bash",
            "-c",
            'pg_ctl -D "$PGDATA" promote -w -t 10',
        )
        assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "f"
        directory_db = urlsplit(drill.directory_dsn).path.removeprefix("/")
        local_db = urlsplit(drill.local_dsn).path.removeprefix("/")
        assert (
            await _sql(
                STANDBY,
                directory_db,
                "SELECT p.org, p.cell, c.deployment_id, c.placed FROM loom_router.placements p "
                "JOIN loom_router.cells c USING (cell)",
            )
            == f"{ORG}|first|{DEPLOYMENT}|1"
        )
        assert (
            await _sql(
                STANDBY,
                directory_db,
                "SELECT actor, action, detail->>'org', detail->>'cell' FROM loom_router.audit",
            )
            == f"operator|place|{ORG}|first"
        )
        assert await _sql(STANDBY, local_db, "SELECT org FROM loom.tenancy") == ORG
