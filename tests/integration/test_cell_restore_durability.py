"""Restore metadata retries survive promotion of an explicitly owned physical pair."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest

from server.cli import cell_attach_pg as apg
from server.cli import cell_restore, cell_restore_pg
from server.core import cell, pg
from server.core.paths import PG_MIGRATIONS_DIR
from server.core.schema import pg_migrate
from server.router import app, directory
from server.settings import settings
from tests.integration._pg_database import create_database
from tests.integration.test_cell_place_durability import _docker, _sql

PRIMARY = "loom-scale-restore-drill-pg"
STANDBY = PRIMARY + "-standby"
DEPLOYMENT = "restore-drill"
ORG = "restore-permanent"
OPERATOR = "lco_" + "4" * 128


@dataclass
class Drill:
    observer: Any
    directory_dsn: str
    local_dsn: str
    tasks: list[asyncio.Task] = field(default_factory=list)
    paused: bool = False

    def task(self, work: Awaitable[Any]) -> asyncio.Task:
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
        async with asyncio.timeout(8):
            while True:
                assert not task.done(), "restore acknowledged before standby flush"
                pid = await self.observer.fetchval(
                    "SELECT pid FROM pg_stat_activity WHERE application_name = $1 "
                    "AND wait_event = 'SyncRep'",
                    application,
                )
                if pid is not None:
                    assert not task.done()
                    return pid
                await asyncio.sleep(0.02)

    async def released(self, pid: int) -> None:
        async with asyncio.timeout(5):
            while True:
                if not await self.observer.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid = $1)", pid
                ):
                    return
                await asyncio.sleep(0.02)

    async def seed(self, dsn: str, work: Callable[[Any], Awaitable[Any]]) -> None:
        """Leave a primary-visible commit whose standby acknowledgement was cancelled."""
        application = "restore-seed"
        conn = await asyncpg.connect(
            dsn,
            timeout=5,
            command_timeout=5,
            server_settings={
                **pg.marker_settings(),
                "application_name": application,
                "synchronous_commit": "on",
            },
        )
        try:
            await conn.execute("BEGIN")
            await work(conn)
            commit = self.task(conn.execute("COMMIT"))
            pid = await self.waiting(commit, application)
            commit.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(commit, 3)
        finally:
            await pg.close_owned(conn, end=time.monotonic() + 1)
        await self.released(pid)

    async def cancel_and_retry(
        self,
        work: Callable[[], Awaitable[Any]],
        application: str,
        *,
        visible: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        first = self.task(work())
        pid = await self.waiting(first, application)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        await self.released(pid)
        if visible is not None:
            await visible()
        retry = self.task(work())
        await self.waiting(retry, application)
        await self.resume()
        return await asyncio.wait_for(retry, 15)


async def _ownership(observer: Any) -> None:
    identifier = str(await observer.fetchval("SELECT system_identifier FROM pg_control_system()"))
    for container in (PRIMARY, STANDBY):
        assert await _docker("inspect", "--format", "{{.Name}}", container) == "/" + container
        assert (
            await _sql(container, "loom", "SELECT system_identifier FROM pg_control_system()")
            == identifier
        )
    assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "t"
    assert (
        await observer.fetchval(
            "SELECT count(*) FROM pg_stat_replication WHERE application_name='loom_standby' "
            "AND sync_state='sync' AND state='streaming'"
        )
        == 1
    )


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
    async with connect() as conn:
        await conn.execute(
            "INSERT INTO loom.authority(authority,epoch,deployment_id) VALUES('postgres',1,$1)",
            DEPLOYMENT,
        )
    fingerprint = cell.as_value(cell.fingerprint(DEPLOYMENT))
    async with directory._connection() as conn:
        await conn.execute(
            "INSERT INTO loom_router.cells(cell,is_first,host,state,mode,credential_sha256,"
            "deployment_id,current_fp) VALUES('first',true,'restore.example.test','serving',"
            "'enforced',$1,$2,$3)",
            "b" * 64,
            DEPLOYMENT,
            fingerprint,
        )


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
        deadline = asyncio.get_running_loop().time() + 10
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
    if not os.environ.get("LOOM_CELL_RESTORE_DRILL"):
        pytest.skip("LOOM_CELL_RESTORE_DRILL not set: needs its disposable physical standby")
    assert os.environ["LOOM_CELL_RESTORE_DRILL"] == "1"
    dsn = os.environ["LOOM_CELL_RESTORE_DRILL_DSN"]
    observer = await asyncpg.connect(dsn, timeout=5, command_timeout=5)
    state = None
    try:
        await _ownership(observer)
        _, directory_dsn = await create_database(dsn)
        _, local_dsn = await create_database(dsn)
        state = Drill(observer, directory_dsn, local_dsn)
        monkeypatch.setattr(directory, "_pool", None)
        monkeypatch.setattr(settings, "router_pg_dsn", directory_dsn)
        monkeypatch.setattr(settings, "pg_dsn", local_dsn)
        monkeypatch.setattr(settings, "storage_authority", "postgres")
        monkeypatch.setattr(settings, "cell", "first")
        monkeypatch.setattr(settings, "public_base_url", "https://restore.example.test")
        monkeypatch.setattr(settings, "oauth_issuer", "")
        monkeypatch.setattr(settings, "document_s3_bucket", "")
        monkeypatch.setattr(
            settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
        )
        monkeypatch.setattr(pg, "_expected", None)
        monkeypatch.setattr(cell, "_code_orgs", None)
        async with asyncio.timeout(60):
            await _initialize(state)
        yield state
    except BaseException:  # Owned cleanup must preserve setup or test failure.
        with contextlib.suppress(BaseException):
            await _cleanup_drill(state, observer)
        raise
    else:
        await _cleanup_drill(state, observer)


async def _directory_page() -> dict[str, Any]:
    transport = httpx.ASGITransport(app=app.build_app(app.Router()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/cells/v1/operator/cells/first/restore-read",
            json={"deployment_id": DEPLOYMENT, "after": None, "expected_sha256": None},
            headers={"Authorization": "Bearer " + OPERATOR},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def _local_work(
    descriptor: dict[str, Any],
    application: str,
    work: Callable[[Any, dict[str, Any], dict[str, Any]], Awaitable[Any]],
) -> Any:
    async with pg.dedicated(statement_timeout_ms=5000) as conn:
        await conn.fetchval("SELECT set_config('application_name',$1,false)", application)
        await apg.take_lock(conn, "restore-drill")
        recorded = await cell_restore_pg.identity(conn, descriptor)
        budget = cell_restore.Budget(20)
        budget.conn = conn
        return await budget.call(work(conn, descriptor, recorded))


async def _migrate(conn: Any, _descriptor: Any, _recorded: Any) -> None:
    await cell_restore._migrate(conn)


async def _promote_and_verify(
    drill: Drill,
    descriptor: dict[str, Any],
    pending: Path,
) -> None:
    drill.observer.terminate()
    await _docker("kill", "--signal", "KILL", PRIMARY)
    await _docker(
        "exec", "--user", "postgres", STANDBY, "bash", "-c", 'pg_ctl -D "$PGDATA" promote -w -t 10'
    )
    assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "f"
    directory_name = urlsplit(drill.directory_dsn).path.removeprefix("/")
    local_name = urlsplit(drill.local_dsn).path.removeprefix("/")
    assert (
        await _sql(
            STANDBY,
            directory_name,
            "SELECT cell FROM loom_router.placements WHERE org='restore-permanent'",
        )
        == "first"
    )
    assert await _sql(STANDBY, directory_name, "SELECT placed FROM loom_router.cells") == "1"
    assert (
        int(
            await _sql(
                STANDBY,
                directory_name,
                "SELECT count(*) FROM loom_router.audit WHERE action='restore_read'",
            )
        )
        > 0
    )
    restored = json.loads(
        await _sql(
            STANDBY,
            local_name,
            "SELECT jsonb_build_object('first',(SELECT row_to_json(f) FROM "
            "(SELECT is_first,cell FROM loom.cell_first) f),"
            "'mode',(SELECT mode FROM loom.tenancy_mode),"
            "'own',(SELECT row_to_json(o) FROM (SELECT current_fp,next_fp FROM loom.cell_self) o),"
            "'org',(SELECT org FROM loom.tenancy WHERE org='restore-permanent'))",
        )
    )
    assert restored == {
        "first": {"is_first": True, "cell": "first"},
        "mode": "enforced",
        "org": ORG,
        "own": {"current_fp": descriptor["current_fp"], "next_fp": None},
    }
    ledger = json.loads(
        await _sql(
            STANDBY,
            local_name,
            "SELECT jsonb_object_agg(filename,checksum) FROM loom.pg_migrations",
        )
    )
    assert ledger == {path.name: pg_migrate.checksum(path) for path in pg_migrate.migration_files()}
    assert ledger[pending.name] == pg_migrate.checksum(pending)
    assert await _sql(STANDBY, local_name, "SELECT value FROM loom.restore_pending_probe") == "42"


@pytest.mark.asyncio
@pytest.mark.recovery
@pytest.mark.destructive
async def test_restore_fences_survive_physical_standby_promotion(
    drill: Drill,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def place(conn):
        await conn.execute("INSERT INTO loom_router.placements(org,cell) VALUES($1,'first')", ORG)
        await conn.execute("UPDATE loom_router.cells SET placed=1 WHERE cell='first'")

    await drill.pause()
    await drill.seed(drill.directory_dsn, place)
    page = await drill.cancel_and_retry(_directory_page, "loom-router")
    assert page["orgs"] == [ORG] and page["done"] is True
    descriptor = page["descriptor"]

    async def insert(conn):
        await conn.execute("INSERT INTO loom.tenancy(org) VALUES($1)", ORG)

    await drill.pause()
    await drill.seed(drill.local_dsn, insert)

    async def tenancy(conn, snapshot, recorded):
        return await cell_restore_pg.tenancy(conn, snapshot, recorded, [ORG])

    assert (
        await drill.cancel_and_retry(
            lambda: _local_work(descriptor, "restore-tenancy", tenancy), "restore-tenancy"
        )
        == 0
    )
    await drill.pause()
    state = await drill.cancel_and_retry(
        lambda: _local_work(descriptor, "restore-state", cell_restore_pg.state), "restore-state"
    )
    assert state["mode"] == "enforced" and state["own"]["current_fp"] == descriptor["current_fp"]
    await drill.pause()
    await drill.cancel_and_retry(
        lambda: _local_work(descriptor, "restore-schema", _migrate), "restore-schema"
    )
    # The acknowledged shipped schema is a genuine prefix of this test-only journal.
    pending = tmp_path / "999_restore_pending_probe.sql"
    pending.write_text(
        "CREATE TABLE loom.restore_pending_probe(value integer PRIMARY KEY); "
        "INSERT INTO loom.restore_pending_probe VALUES(42);"
    )
    real_files = pg_migrate.migration_files
    files = [*real_files(), pending]
    monkeypatch.setattr(
        pg_migrate,
        "migration_files",
        lambda mig_dir=PG_MIGRATIONS_DIR: (
            files if mig_dir == PG_MIGRATIONS_DIR else real_files(mig_dir)
        ),
    )
    async with pg.dedicated() as conn:
        applied = {
            r["filename"]: r["checksum"]
            for r in await conn.fetch("SELECT filename,checksum FROM loom.pg_migrations")
        }
        assert pg_migrate.plan(applied, files) == [pending]
        assert await conn.fetchval("SELECT to_regclass('loom.restore_pending_probe')") is None

    async def visible() -> None:
        async with pg.dedicated() as conn:
            assert await conn.fetchval("SELECT value FROM loom.restore_pending_probe") == 42
            assert await conn.fetchval(
                "SELECT checksum FROM loom.pg_migrations WHERE filename=$1", pending.name
            ) == pg_migrate.checksum(pending)

    await drill.pause()
    await drill.cancel_and_retry(
        lambda: _local_work(descriptor, "restore-pending-schema", _migrate),
        "restore-pending-schema",
        visible=visible,
    )
    await _promote_and_verify(drill, descriptor, pending)
