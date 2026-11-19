"""`loom cell attach` (server/cli/cell_attach.py) against a real Postgres with a synchronous
standby, a real ClickHouse and the router served in process: a prepared first cell in
`recording` with orgs in both stores attaches to `serving` with every org placed and the
triggers created; a rerun on `serving` exits 0; a refusal after step 3 leaves `enforced` and
a rerun resumes from there; a second run is refused at the attach lock; the Postgres reads
attach makes refuse what they name (an unchecked writer's setting, a subscription, a role
that can make an org held, a transaction that may have read `recording`).

The cell's Postgres and the directory are databases of the test's own; the configured
ClickHouse databases are scratch ones; the cell's processes are heartbeat rows the test
writes. Needs LOOM_PG_DSN (a server with a synchronous standby), LOOM_ROUTER_PG_DSN and
LOOM_CH_HOST."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from server.cli import cell as cell_cli
from server.cli import cell_attach, cell_attach_pg, cell_prepare
from server.cli.cell_ch import Refused
from server.core import cell, cell_probe, cell_triggers, instances, pg
from server.core import ch as ch_core
from server.core.schema import pg_migrate
from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.integration._pg_database import create_database, drop_database
from tests.integration._resources import ch_client_from_env
from tests.unit.cell_control_owner import owner as _owner

OPERATOR = "lco_" + "2" * 128
CREDENTIAL_SHA = hashlib.sha256(b"lcr_first").hexdigest()
ROUTER = "http://127.0.0.1:7901"
HOST = "loom.example"


class _Network(httpx.AsyncBaseTransport):
    """What attach and the router reach: the router at ROUTER, the shared host's health
    route, the cell's upstream main port (:7777) and its probe listener (any other)."""

    def __init__(self) -> None:
        self.listener = httpx.ASGITransport(app=cell_probe.listener_app())
        self.router = httpx.ASGITransport(
            app=router_app.build_app(router_app.Router(probe_transport=self))
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1" and request.url.port == 7901:
            return await self.router.handle_async_request(request)
        if request.url.host == HOST or request.url.port == 7777:
            return httpx.Response(200, json=cell_probe.answer())
        return await self.listener.handle_async_request(request)


async def _admin(sql: str, *args: Any) -> Any:
    import asyncpg

    conn = await asyncpg.connect(os.environ["LOOM_PG_DSN"])
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def cell_clickhouse_databases() -> Iterator[tuple[str, str]]:
    """Migrate one owned pair; no authority write can replace the suite server's row."""
    from server.core.schema import migrate

    for name in ("LOOM_PG_DSN", "LOOM_ROUTER_PG_DSN", "LOOM_CH_HOST"):
        os.environ.get(name) or pytest.skip(f"{name} not set")
    suffix = uuid.uuid4().hex
    names = (f"loom_it_attach_{suffix}", f"loom_it_attacht_{suffix}")
    created: list[str] = []
    admin = ch_client_from_env(database="")
    error: BaseException | None = None
    try:
        for name in names:
            admin.command(f"CREATE DATABASE {name}")
            created.append(name)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settings, "ch_database", names[0])
            patch.setattr(settings, "ch_telemetry_database", names[1])
            normal_client = migrate.client
            bootstrap = normal_client()
            patch.setattr(
                migrate, "client", lambda **kwargs: normal_client(**kwargs) if kwargs else bootstrap
            )
            try:
                assert migrate.main([]) == 0
            finally:
                if sys.exception() is None:
                    bootstrap.close()
                else:
                    with contextlib.suppress(Exception):
                        bootstrap.close()
        admin.close()
        admin = None
        yield names
    except BaseException as caught:  # noqa: BLE001 — exact owned cleanup retains the first failure.
        error = caught
    finally:
        if admin is None:
            try:
                admin = ch_client_from_env(database="")
            except BaseException as caught:  # noqa: BLE001 — cleanup cannot replace the first failure.
                error = error or caught
        if admin is not None:
            for name in reversed(created):
                try:
                    admin.command(f"DROP DATABASE {name} SYNC")
                except BaseException as caught:  # noqa: BLE001 — attempt every exact owned cleanup.
                    error = error or caught
            try:
                admin.close()
            except BaseException as caught:  # noqa: BLE001 — close cannot replace the first failure.
                error = error or caught
        if error is not None:
            raise error


@pytest.fixture
def genuine_cell_processes() -> bool:
    """Synthetic cases own their rows; the live-replica case uses the real readers."""
    return False


@pytest.fixture
async def cell_stores(
    monkeypatch: pytest.MonkeyPatch,
    cell_clickhouse_databases: tuple[str, str],
    genuine_cell_processes: bool,
) -> AsyncIterator[dict[str, Any]]:
    cell_dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    router_dsn = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    cell_db, own_cell = await create_database(cell_dsn)
    try:
        router_db, own_router = await create_database(router_dsn)
    except BaseException as first:  # Release only the database already created by this fixture.
        try:
            await drop_database(cell_dsn, cell_db)
        except BaseException as error:  # noqa: BLE001 — cleanup retains the creation failure.
            first.add_note(f"cell database cleanup also failed: {type(error).__name__}")
        raise
    deployment = str(uuid.uuid4())
    tag = f"it-{uuid.uuid4().hex[:8]}-"
    for name, value in {
        "cell": "first",
        "storage_authority": "postgres",
        "pg_dsn": own_cell,
        "document_s3_bucket": "",
        "router_pg_dsn": own_router,
        "router_shared_host": HOST,
        "router_upstream_probe_url": "http://listener.internal:7790",
        "router_upstream_url": "http://upstream.internal:7777",
        "router_operator_sha256": hashlib.sha256(OPERATOR.encode()).hexdigest(),
        "router_operator_token": OPERATOR,
        "ch_cluster": "",
        "ch_database": cell_clickhouse_databases[0],
        "ch_telemetry_database": cell_clickhouse_databases[1],
        "public_base_url": f"https://{HOST}",
        "oauth_issuer": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(directory, "_pool", None)
    # A step's poll interval and the waits stay short.
    monkeypatch.setattr(cell_attach_pg, "POLL_S", 0.2)
    ch = ch_core.CHClient()
    monkeypatch.setattr(ch_core, "_ch", ch)
    ids: list[str] = []
    real_live, real_recent = instances.read_live, instances.read_recent

    async def read_live(ch: Any, *, all_deployments: bool = False) -> list[dict[str, Any]]:
        rows = await real_live(ch, all_deployments=all_deployments)
        return [r for r in rows if r["instance_id"] in ids]

    async def read_recent(
        ch: Any, *, window_s: float, since: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await real_recent(ch, window_s=window_s, since=since)
        return [r for r in rows if r["instance_id"] in ids]

    # These assertions inspect only the heartbeat identities created by this fixture.
    if not genuine_cell_processes:
        monkeypatch.setattr(instances, "read_live", read_live)
        monkeypatch.setattr(instances, "read_recent", read_recent)
    try:
        await pg.close_pool()
        await ch.connect()
        await pg_migrate.migrate()
        await pg.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) "
            "VALUES ('postgres', 1, $1)",
            deployment,
        )
        await directory.migrate()
        await ch.insert(
            "loom.storage_authority",
            [
                {
                    "authority": "postgres",
                    "epoch": 1 if genuine_cell_processes else 10**12,
                    "deployment_id": deployment,
                }
            ],
        )
        cell.set_deployment_id(deployment)
        yield {"ch": ch, "ids": ids, "tag": tag, "deployment": deployment, "db": cell_db}
    finally:
        cell.set_deployment_id("")
        await _close_cell_stores(
            ch, deployment, ids, tag, (cell_dsn, cell_db), (router_dsn, router_db), sys.exception()
        )


async def _close_cell_stores(
    ch: Any,
    deployment: str,
    ids: list[str],
    tag: str,
    cell_database: tuple[str, str],
    router_database: tuple[str, str],
    first: BaseException | None,
) -> None:
    # Every operation owns an exact database, deployment, heartbeat id or org prefix.
    cleanup = [
        ch.execute(
            "ALTER TABLE loom.storage_authority DELETE WHERE deployment_id = {d:String} "
            "SETTINGS mutations_sync = 2",
            {"d": deployment},
        ),
        *(
            ch.execute(
                f"ALTER TABLE {table} DELETE WHERE has({{ids:Array(String)}}, instance_id)",
                {"ids": ids},
            )
            for table in (instances.TABLE, instances.CELL_TABLE)
        ),
        ch.execute("DELETE FROM loom.namespaces WHERE startsWith(org, {p:String})", {"p": tag}),
        ch.disconnect(),
        pg.close_pool(),
        directory.close(),
        drop_database(*cell_database),
        drop_database(*router_database),
    ]
    for operation in cleanup:
        try:
            await operation
        except BaseException as error:  # noqa: BLE001 — attempt every owned cleanup; keep first failure.
            if first is None:
                first = error
            else:
                first.add_note(f"store cleanup also failed: {type(error).__name__}")
    if first is not None:
        raise first


def _value(**keys: Any) -> str:
    reported = {
        **cell.as_value(cell.mine()),
        "serves": True,
        "marked": True,
        "router": True,
        "router_why": "",
        **keys,
    }
    return json.dumps(reported, sort_keys=True)


async def _beat(
    stores: dict[str, Any],
    instance_id: str,
    *,
    cell_name: str | None = "first",
    mode: str = "recording",
    stopped: bool = False,
    **keys: Any,
) -> None:
    """One process's heartbeat rows, as `instances.tick` writes them."""
    ch = stores["ch"]
    if instance_id not in stores["ids"]:
        stores["ids"].append(instance_id)
    await ch.insert(
        instances.TABLE, [{**instances.row(stopped=stopped), "instance_id": instance_id}]
    )
    if cell_name is not None:
        await ch.insert(
            instances.CELL_TABLE,
            [
                {
                    "instance_id": instance_id,
                    "cell": cell_name,
                    "tenancy_mode": mode,
                    "fingerprint": _value(**keys),
                }
            ],
        )


async def _prepare(stores: dict[str, Any], network: _Network) -> None:
    pid = f"it-{uuid.uuid4().hex[:8]}"
    await _beat(stores, pid, cell_name="first", mode="open")
    args = argparse.Namespace(
        cell="first",
        host=HOST,
        credential_sha256=CREDENTIAL_SHA,
        probe_url="",
        upstream_url=None,
        wait=0,
        router=ROUTER,
    )
    router = cell_cli.operator_router(args)
    assert isinstance(router, httpx.URL)
    assert await cell_prepare.prepare(stores["ch"], args, router, network) == 0
    stores["pid"] = pid
    await _beat(stores, pid, mode="recording")


def _args(**over: Any) -> argparse.Namespace:
    fields: dict[str, Any] = {
        "cell": "first",
        "wait": 20,
        "scan_budget": 600,
        "dry_run": False,
        "ch_allow_user": [settings.ch_user or "default"],
        "ch_allow_user_directory": [],
        "ch_allow_object": [],
        "ch_allow_replica": [],
        "pg_allow_role": [],
        "router": ROUTER,
    }
    return argparse.Namespace(**{**fields, **over})


async def _attach(stores: dict[str, Any], network: _Network, **over: Any) -> int:
    args = _args(**over)
    url = cell_cli.operator_router(args)
    assert isinstance(url, httpx.URL)
    return await cell_attach.attach(stores["ch"], args, url, network)


async def _mode() -> str:
    return str(await pg.fetchval("SELECT mode FROM loom.tenancy_mode"))


async def _placed() -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(settings.router_pg_dsn)
    try:
        rows = await conn.fetch("SELECT org FROM loom_router.placements WHERE cell = 'first'")
    finally:
        await conn.close()
    return {r["org"] for r in rows}


async def _row() -> Any:
    return await directory.first_cell_named("first")


@pytest.mark.asyncio
async def test_a_recording_first_cell_attaches_to_serving(cell_stores, capsys) -> None:
    stores, network = cell_stores, _Network()
    tag = stores["tag"]
    await _prepare(stores, network)
    capsys.readouterr()
    # Orgs a recording cell holds: a tenancy row alone, a Postgres data row, a ClickHouse
    # row, and one name no request can carry (skipped and named).
    await pg.execute("INSERT INTO loom.tenancy (org) VALUES ($1)", tag + "signed-in")
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", tag + "pg")
    await stores["ch"].insert(
        "loom.namespaces",
        [
            {"org": tag + "ch", "namespace": "n", "owner_email": ""},
            {"org": tag + "bad\x00org", "namespace": "n", "owner_email": ""},
        ],
    )
    assert await _attach(stores, network) == 0
    report = json.loads(capsys.readouterr().out)
    mine = {tag + "signed-in", tag + "pg", tag + "ch"}
    assert report["state"] == "serving"
    assert await _mode() == "enforced"
    placed = await _placed()
    assert {o for o in placed if o.startswith(tag)} == mine
    held = {r["org"] for r in await pg.fetch("SELECT org FROM loom.tenancy")}
    assert held == placed
    assert any("clickhouse loom.namespaces" in k for k in report["skipped"]["malformed"])
    row = await _row()
    assert (row.state, row.mode, row.placed) == ("serving", "enforced", len(placed))
    async with pg.dedicated() as conn:
        assert await cell_triggers.missing(conn) == []
    # A rerun on `serving` is done.
    assert await _attach(stores, network) == 0
    assert json.loads(capsys.readouterr().out)["done"] is True


@pytest.mark.asyncio
async def test_a_refusal_after_step_3_leaves_enforced_and_a_rerun_resumes(
    cell_stores, monkeypatch, capsys
) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    org = stores["tag"] + "a"
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", org)
    # A process with LOOM_CELL unset appears once the mode is committed: refused at once.
    real = cell_attach.Run.durable

    async def durable_then_unset(self: Any) -> int:
        xmax = await real(self)
        await _beat(stores, f"it-unset-{uuid.uuid4().hex[:6]}", cell_name="")
        return xmax

    monkeypatch.setattr(cell_attach.Run, "durable", durable_then_unset)
    assert await _attach(stores, network) == 1
    assert "runs with LOOM_CELL unset" in capsys.readouterr().err
    assert await _mode() == "enforced"
    row = await _row()
    assert (row.state, row.mode) == ("attaching", "enforced")
    # Placed live once enforced; the rerun resumes from the durable read.
    monkeypatch.setattr(cell_attach.Run, "durable", real)
    for i in list(stores["ids"]):
        if i.startswith("it-unset-"):
            await _beat(stores, i, cell_name="first", mode="enforced")
    await _beat(stores, stores["pid"], mode="enforced")
    assert await _attach(stores, network) == 0
    assert org in await _placed()
    assert (await _row()).state == "serving"


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["trigger", "catalog"])
async def test_a_defect_during_the_rescan_keeps_the_cell_attaching(
    cell_stores, monkeypatch, capsys, defect: str
) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    role = f"it_rescan_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    real_scan = cell_attach.Run.scan

    async def scan_then_defect(self: Any, *args: Any) -> Any:
        found = await real_scan(self, *args)
        if self.mode == "enforced":
            if defect == "trigger":
                await pg.execute(
                    f"ALTER TABLE loom.scope_generations DISABLE TRIGGER {cell_triggers.INSERT_TRIGGER}"
                )
            else:
                await pg.execute(f"GRANT INSERT (org) ON loom.tenancy TO {role}")
        return found

    monkeypatch.setattr(cell_attach.Run, "scan", scan_then_defect)
    try:
        assert await _attach(stores, network) == 1
        refused = capsys.readouterr().err
        assert ("postgres triggers" if defect == "trigger" else f"postgres role {role}") in refused
        assert await _mode() == "enforced"
        assert (await _row()).state == "attaching"
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_a_second_run_is_refused_at_the_lock(cell_stores, capsys) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    async with pg.dedicated() as holder:
        await cell_attach_pg.take_lock(holder, "the test")
        assert await _attach(stores, network) == 1
        assert "attach lock: held by pid" in capsys.readouterr().err
    assert await _mode() == "recording"


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(cell_stores, capsys) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    capsys.readouterr()
    org = stores["tag"] + "dry"
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", org)
    assert await _attach(stores, network, dry_run=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["per_source"]["postgres loom.scope_generations"] == {
        "count": 1,
        "sample": [org],
    }
    assert await _mode() == "recording"
    assert await _placed() == set()
    assert await pg.fetchval("SELECT count(*) FROM loom.tenancy") == 0
    async with pg.dedicated() as conn:
        assert any(d.kind == "absent" for d in await cell_triggers.missing(conn))


@pytest.mark.asyncio
async def test_an_org_past_the_limit_refuses_before_any_tenancy_row(cell_stores, capsys) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    long = "o" * (cell.ORG_MAX_BYTES + 1)
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", long)
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ('org-ok', '*')")
    assert await _attach(stores, network) == 1
    assert f"longer than {cell.ORG_MAX_BYTES} bytes" in capsys.readouterr().err
    assert await _mode() == "recording"
    assert await pg.fetchval("SELECT count(*) FROM loom.tenancy") == 0
    assert (await _row()).placed == 0


@pytest.mark.asyncio
async def test_settings_and_subscriptions_that_pass_unchecked_writers_refuse(
    cell_stores, capsys
) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    db = stores["db"]
    role = f"it_role_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        # Stored as first spelled: a fresh session that never set the placeholder.
        await _admin(f'ALTER ROLE {role} IN DATABASE "{db}" SET "Loom.Tenancy_Checked" = \'on\'')
        async with pg.dedicated() as conn:
            setting = await conn.fetchval(
                "SELECT setconfig::text FROM pg_db_role_setting s JOIN pg_roles r "
                "ON r.oid = s.setrole WHERE r.rolname = $1",
                role,
            )
            assert "Loom.Tenancy_Checked" in setting
            refusals = await cell_attach_pg.unmarked_refusals(conn, False)
        assert [r.subject for r in refusals] == [f"postgres setting for {role} in {db}"]
        # Another database's entry does not refuse.
        await _admin(f'ALTER ROLE {role} IN DATABASE "{db}" RESET ALL')
        await _admin(
            f"ALTER ROLE {role} IN DATABASE postgres SET session_replication_role = 'replica'"
        )
        async with pg.dedicated() as conn:
            assert await cell_attach_pg.unmarked_refusals(conn, False) == []
        # A role that can insert a tenancy row, a column grant alone, refuses attach.
        await pg.execute(f"GRANT USAGE ON SCHEMA loom TO {role}")
        await pg.execute(f"GRANT INSERT (org) ON loom.tenancy TO {role}")
        assert await _attach(stores, network) == 1
        err = capsys.readouterr().err
        assert f"postgres role {role}: may insert into loom.tenancy" in err
        assert await _mode() == "recording"
        assert await _attach(stores, network, pg_allow_role=[role], dry_run=True) == 0
    finally:
        await pg.execute(f"REVOKE ALL ON loom.tenancy FROM {role}")
        await pg.execute(f"REVOKE ALL ON SCHEMA loom FROM {role}")
        await _admin(f"ALTER ROLE {role} IN DATABASE postgres RESET ALL")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_a_disabled_subscription_refuses(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    name = f"it_sub_{uuid.uuid4().hex[:6]}"
    await pg.execute(
        f"CREATE SUBSCRIPTION {name} CONNECTION 'host=127.0.0.1 dbname=none' "
        "PUBLICATION none WITH (connect = false, enabled = false)"
    )
    try:
        async with pg.dedicated() as conn:
            refusals = await cell_attach_pg.unmarked_refusals(conn, False)
        assert [str(r) for r in refusals] == [
            f"postgres subscription {name}: disabled: its apply worker fires no origin "
            "trigger; drop it"
        ]
    finally:
        await pg.execute(f"ALTER SUBSCRIPTION {name} SET (slot_name = NONE)")
        await pg.execute(f"DROP SUBSCRIPTION {name}")


@pytest.mark.asyncio
async def test_step_4_waits_for_a_transaction_that_may_have_read_recording(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    async with pg.dedicated() as writer, pg.dedicated() as poller:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ('w', '*')")
        # X is read after a later transaction completed, as after the durable read.
        await poller.execute("BEGIN; SELECT pg_current_xact_id(); COMMIT")
        xmax = await cell_attach_pg.snapshot_xmax(poller)
        with pytest.raises(Refused, match="may have read `recording`"):
            await cell_attach_pg.wait_transactions(poller, xmax, cell_attach_pg.Wait(0.5))
        await tx.rollback()
        # A session idle outside a transaction holds nothing: the wait passes.
        await cell_attach_pg.wait_transactions(poller, xmax, cell_attach_pg.Wait(0.5))


@pytest.mark.asyncio
async def test_the_session_checks_and_the_encoding(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    async with pg.dedicated() as conn:
        assert await cell_attach_pg.direct_refusals(conn, "attach") == []
        assert await cell_attach_pg.encoding_refusals(conn) == []
    async with pg.dedicated(marked=False) as conn:
        refusals = await cell_attach_pg.direct_refusals(conn, "attach")
        assert [r.reason.split(":")[0] for r in refusals] == [
            f"attach's session loses {pg.TENANCY_MARKER}"
        ]
    name = f"it_latin_{uuid.uuid4().hex[:6]}"
    await _admin(
        f"CREATE DATABASE {name} ENCODING 'LATIN1' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0"
    )
    try:
        import asyncpg

        from tests.integration._pg_database import with_database

        conn = await asyncpg.connect(with_database(os.environ["LOOM_PG_DSN"], name))
        try:
            (refusal,) = await cell_attach_pg.encoding_refusals(conn)
            assert "LATIN1, not UTF8" in refusal.reason
        finally:
            await conn.close()
    finally:
        await _admin(f"DROP DATABASE {name}")


@pytest.mark.asyncio
async def test_the_mode_commit_waits_out_a_for_share_holder(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    logged: list[str] = []

    async def ok() -> list[str]:
        return []

    async with pg.dedicated() as holder, pg.dedicated() as conn:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT mode FROM loom.tenancy_mode FOR SHARE")

        async def release() -> None:
            await asyncio.sleep(3)
            await tx.rollback()

        task = asyncio.create_task(release())
        try:
            await cell_attach_pg.take_lock(conn, "mode test")
            own = await conn.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
            await cell_attach_pg.commit_enforced(
                _owner(conn),
                ok,
                ok,
                cell_attach_pg.Wait(20),
                logged.append,
                lambda _m: None,
                expected_cell="first",
                expected_fp=(own["current_fp"], own["next_fp"]),
            )
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await _mode() == "enforced"
    assert logged == ["the mode's lock is busy; retrying"]


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["set", "admin"])
async def test_a_login_can_lower_mode_through_a_noninherited_group(
    cell_stores, membership: str
) -> None:
    await _prepare(cell_stores, _Network())
    group = f"it_group_{uuid.uuid4().hex[:6]}"
    login = f"it_login_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {group} NOLOGIN")
    try:
        await _admin(f"CREATE ROLE {login} LOGIN")
        async with pg.dedicated() as conn:
            await conn.execute(f"GRANT USAGE ON SCHEMA loom TO {group}")
            await conn.execute(f"GRANT UPDATE (mode) ON loom.tenancy_mode TO {group}")
            flags = (
                "ADMIN FALSE, INHERIT FALSE, SET TRUE"
                if membership == "set"
                else "ADMIN TRUE, INHERIT FALSE, SET FALSE"
            )
            await conn.execute(f"GRANT {group} TO {login} WITH {flags}")
            assert not await conn.fetchval(
                "SELECT has_column_privilege($1, 'loom.tenancy_mode', 'mode', 'UPDATE')", login
            )
            holders = await cell_attach_pg.privilege_holders(conn, exempt_self=True)
            await conn.execute(f"SET SESSION AUTHORIZATION {login}")
            if membership == "admin":
                await conn.execute(f"GRANT {group} TO {login} WITH SET TRUE")
            await conn.execute(f"SET ROLE {group}")
            assert (
                await conn.execute("UPDATE loom.tenancy_mode SET mode = 'enforced'") == "UPDATE 1"
            )
            assert (
                await conn.execute("UPDATE loom.tenancy_mode SET mode = 'recording'") == "UPDATE 1"
            )
            await conn.execute("RESET SESSION AUTHORIZATION")
        assert "may update loom.tenancy_mode (mode)" in holders.others.get(login, [])
        assert login not in holders.own
        assert any(
            r.subject == f"postgres role {login}"
            for r in cell_attach_pg.privilege_refusals(holders, [])
        )
        assert not any(
            r.subject == f"postgres role {login}"
            for r in cell_attach_pg.privilege_refusals(holders, [login])
        )
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {login}, {group}")
        await _admin(f"DROP ROLE IF EXISTS {login}")
        await _admin(f"DROP ROLE {group}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grant,why",
    [
        ("GRANT INSERT (org) ON loom.tenancy TO {r}", "may insert into loom.tenancy"),
        ("GRANT UPDATE (org) ON loom.tenancy TO {r}", "may update loom.tenancy (org)"),
        ("GRANT UPDATE (mode) ON loom.tenancy_mode TO {r}", "may update loom.tenancy_mode (mode)"),
        ("GRANT INSERT (mode) ON loom.tenancy_mode TO {r}", "may insert into loom.tenancy_mode"),
        (
            "GRANT INSERT, TRUNCATE ON loom.tenancy_mode TO {r}",
            "may insert into loom.tenancy_mode",
        ),
        (
            "GRANT TRIGGER ON loom.scope_generations TO {r}",
            "holds TRIGGER on loom.scope_generations",
        ),
        ("GRANT CREATE ON SCHEMA public TO {r}", "may create in schema public"),
        ("GRANT CREATE ON SCHEMA loom TO {r}", "may create in schema loom"),
        (
            "GRANT SET ON PARAMETER session_replication_role TO {r}",
            "may set session_replication_role",
        ),
        (
            "SET loom.tenancy_checked = 'off'; "
            "GRANT ALTER SYSTEM ON PARAMETER loom.tenancy_checked TO {r}",
            "may ALTER SYSTEM SET loom.tenancy_checked",
        ),
        ("GRANT pg_write_server_files TO {r}", "is a member of pg_write_server_files"),
        # A member of the owner role, ADMIN only: it can grant itself the rest.
        ("GRANT {owner} TO {r} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE", "owns loom.tenancy"),
    ],
)
async def test_each_right_that_makes_an_org_held_is_named(
    cell_stores, grant: str, why: str
) -> None:
    await _prepare(cell_stores, _Network())
    role = f"it_r_{uuid.uuid4().hex[:6]}"
    owner = await pg.fetchval("SELECT current_user")
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        async with pg.dedicated() as conn:
            await conn.execute("GRANT USAGE ON SCHEMA loom TO " + role)
            for sql in grant.format(r=role, owner=owner).split("; "):
                await conn.execute(sql)
            holders = await cell_attach_pg.privilege_holders(conn, exempt_self=True)
        assert owner in holders.own
        assert any(why in w for w in holders.others.get(role, [])), holders.others
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_a_function_owner_counts_though_it_owns_no_table(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    role = f"it_f_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        async with pg.dedicated() as conn:
            await conn.execute(f"ALTER FUNCTION loom.tenancy_refuse_unheld() OWNER TO {role}")
            holders = await cell_attach_pg.privilege_holders(conn, exempt_self=True)
            await conn.execute("ALTER FUNCTION loom.tenancy_refuse_unheld() OWNER TO CURRENT_USER")
        assert holders.others[role] == ["owns loom.tenancy_refuse_unheld()"]
    finally:
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_a_repeatable_read_snapshot_from_before_x_holds_step_4(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    async with pg.dedicated() as reader, pg.dedicated() as poller:
        tx = reader.transaction(isolation="repeatable_read")
        await tx.start()
        await reader.fetchval("SELECT mode FROM loom.tenancy_mode")  # its snapshot, no xid
        await poller.execute("BEGIN; SELECT pg_current_xact_id(); COMMIT")
        xmax = await cell_attach_pg.snapshot_xmax(poller)
        with pytest.raises(Refused, match="may have read `recording`"):
            await cell_attach_pg.wait_transactions(poller, xmax, cell_attach_pg.Wait(0.5))
        await tx.rollback()


@pytest.mark.asyncio
async def test_prepare_is_refused_while_attach_holds_the_lock(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    args = argparse.Namespace(
        cell="first",
        host=HOST,
        credential_sha256=CREDENTIAL_SHA,
        probe_url="",
        upstream_url=None,
        wait=0,
        router=ROUTER,
    )
    router = cell_cli.operator_router(args)
    assert isinstance(router, httpx.URL)
    async with pg.dedicated() as holder:
        await cell_attach_pg.take_lock(holder, "attach")
        with pytest.raises(cell_prepare.Refused, match="attach lock: held by pid"):
            await cell_prepare.prepare(cell_stores["ch"], args, router, _Network())


@pytest.mark.asyncio
async def test_doctor_names_the_same_holders_settings_and_subscriptions(cell_stores) -> None:
    from server.cli import doctor_cell

    await _prepare(cell_stores, _Network())
    role = f"it_d_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        await pg.execute(f"GRANT USAGE ON SCHEMA loom TO {role}")
        await pg.execute(f"GRANT INSERT ON loom.tenancy TO {role}")
        await _admin(f"ALTER ROLE {role} SET session_replication_role = 'replica'")
        lines = {n: (g, d) for n, g, d in await doctor_cell._writer_reads(())}
        assert lines["cell_tenancy_privilege"][0] == "WARN"
        assert f"postgres role {role}" in lines["cell_tenancy_privilege"][1]
        assert lines["cell_unchecked_writers"][0] == "WARN"
        allowed = {n: g for n, g, _d in await doctor_cell._writer_reads((role,))}
        assert allowed["cell_tenancy_privilege"] == "PASS"
    finally:
        await pg.execute(f"REVOKE ALL ON loom.tenancy FROM {role}")
        await pg.execute(f"REVOKE ALL ON SCHEMA loom FROM {role}")
        await _admin(f"ALTER ROLE {role} RESET ALL")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_doctor_names_stray_orgs_smallest_tables_first(cell_stores) -> None:
    from server.cli import doctor_cell

    stores = cell_stores
    await _prepare(stores, _Network())
    tag = stores["tag"]
    await pg.execute("INSERT INTO loom.tenancy (org) VALUES ($1)", tag + "held")
    await pg.execute(
        "INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", tag + "held"
    )
    await pg.execute(
        "INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", tag + "stray"
    )
    lines = {n: (g, d) for n, g, d in await doctor_cell._stray_reads(stores["ch"], 300)}
    grade, detail = lines["cell_stray_orgs"]
    assert grade == "WARN" and tag + "stray" in detail and tag + "held" not in detail


@pytest.mark.asyncio
async def test_doctor_reads_smallest_first_and_leaves_the_largest_unchecked(
    cell_stores, monkeypatch
) -> None:
    from server.cli import cell_scan, doctor_cell

    stores = cell_stores
    await _prepare(stores, _Network())
    tag = stores["tag"]
    await pg.execute("CREATE TABLE loom.it_small (org text PRIMARY KEY)")
    await pg.execute("CREATE TABLE loom.it_large (org text PRIMARY KEY)")
    await pg.execute("INSERT INTO loom.it_small VALUES ($1)", tag + "small")
    await pg.execute(
        "INSERT INTO loom.it_large SELECT $1 || g FROM generate_series(1, 20000) g", tag + "l"
    )
    await pg.execute("ANALYZE loom.it_small, loom.it_large")
    # This ordering check owns two Postgres tables; shared ClickHouse logs keep growing.
    monkeypatch.setattr(
        cell_triggers,
        "ORG_TABLES",
        "SELECT relname FROM pg_class WHERE oid IN "
        "('loom.it_small'::regclass, 'loom.it_large'::regclass)",
    )

    async def clickhouse_inventory(*_args: Any) -> tuple[list[Any], list[Any]]:
        return [], []

    monkeypatch.setattr(cell_scan, "list_clickhouse", clickhouse_inventory)
    clock = [0.0]
    real_budget = cell_scan.Budget
    monkeypatch.setattr(
        cell_scan, "Budget", lambda seconds: real_budget(seconds, clock=lambda: clock[0])
    )
    # The budget runs out on the largest table, as it does on a real cell when that table's
    # read is what is too slow: every smaller one was read first.
    real, read = cell_scan.pg_orgs, []

    async def pg_orgs(table: str, budget: Any) -> list[str]:
        read.append(table)
        if table == "loom.it_large":
            clock[0] += budget.left() + 0.1
            budget.require(table)
        return await real(table, budget)

    monkeypatch.setattr(cell_scan, "pg_orgs", pg_orgs)
    lines = {n: (g, d) for n, g, d in await doctor_cell._stray_reads(stores["ch"], 3)}
    assert read == ["loom.it_small", "loom.it_large"]
    assert tag + "small" in lines["cell_stray_orgs"][1]
    grade, detail = lines["cell_stray_orgs_unchecked"]
    assert grade == "WARN" and detail.endswith(": loom.it_large")


@pytest.mark.asyncio
async def test_signaling_another_role_requires_an_effective_signal_grant(cell_stores) -> None:
    import asyncpg

    caller = f"it_signal_{uuid.uuid4().hex[:6]}"
    target = f"it_target_{uuid.uuid4().hex[:6]}"
    password = uuid.uuid4().hex
    await _admin(f"CREATE ROLE {caller} LOGIN")
    try:
        await _admin(f"CREATE ROLE {target} LOGIN PASSWORD '{password}'")
        victim = await asyncpg.connect(settings.pg_dsn, user=target, password=password)
        try:
            pid = await victim.fetchval("SELECT pg_backend_pid()")
            async with pg.dedicated() as admin, pg.dedicated() as signal:
                await admin.execute(f"GRANT pg_signal_backend TO {caller} WITH INHERIT FALSE")
                await signal.execute(f"SET SESSION AUTHORIZATION {caller}")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await signal.fetchval("SELECT pg_terminate_backend($1)", pid)
                await admin.execute(f"GRANT pg_signal_backend TO {caller} WITH INHERIT TRUE")
                assert await signal.fetchval("SELECT pg_terminate_backend($1)", pid)
        finally:
            await victim.close()
    finally:
        await _admin(f"DROP ROLE {caller}")
        await _admin(f"DROP ROLE IF EXISTS {target}")


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["pg_read_all_stats"])
async def test_attach_needs_its_predefined_capabilities_without_set_role(
    cell_stores, monkeypatch, capsys, capability: str
) -> None:
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    role = f"it_caps_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    real_dedicated = pg.dedicated
    real_connect = pg._connect_dedicated

    async def as_attacher(**kwargs: Any) -> Any:
        conn = await real_connect(**kwargs)
        await conn.execute(f"SET SESSION AUTHORIZATION {role}")
        return conn

    try:
        async with real_dedicated() as conn:
            owner = await conn.fetchval("SELECT current_user")
            await conn.execute(f"GRANT {owner} TO {role} WITH INHERIT TRUE, SET FALSE")
            await conn.execute(f"GRANT pg_read_all_stats TO {role}")
            await conn.execute(f"GRANT {capability} TO {role} WITH INHERIT FALSE")
            assert await conn.fetchval("SELECT pg_has_role($1, $2, 'MEMBER')", role, capability)
            assert not await conn.fetchval("SELECT pg_has_role($1, $2, 'USAGE')", role, capability)
        before = await _row()
        with monkeypatch.context() as own:
            own.setattr(pg, "_connect_dedicated", as_attacher)
            assert await _attach(stores, network) == 1
        assert (
            f"attach's role lacks effective privileges of {capability}" in capsys.readouterr().err
        )
        after = await _row()
        assert (after.state, after.mode, after.placed) == (before.state, before.mode, before.placed)
        assert await _mode() == "recording"
        assert await pg.fetchval("SELECT count(*) FROM loom.tenancy") == 0
        async with real_dedicated() as conn:
            await conn.execute(f"GRANT {capability} TO {role} WITH INHERIT TRUE")
        with monkeypatch.context() as own:
            own.setattr(pg, "_connect_dedicated", as_attacher)
            assert await _attach(stores, network) == 0, capsys.readouterr().err
        assert (await _row()).state == "serving"
    finally:
        async with real_dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_attach_requires_every_probe_privilege(cell_stores) -> None:
    role = f"it_probe_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE")
    grants = [
        ("SELECT",),
        *(tuple(p for p in privileges if p != missing) for missing in privileges),
        privileges,
    ]
    observed: dict[tuple[str, ...], list[str]] = {}
    try:
        async with pg.dedicated() as conn:
            await conn.execute(f"GRANT USAGE ON SCHEMA loom TO {role}")
            for held in grants:
                await conn.execute(f"REVOKE ALL ON loom.cell_probe FROM {role}")
                await conn.execute(f"GRANT {', '.join(held)} ON loom.cell_probe TO {role}")
                await conn.execute(f"SET ROLE {role}")
                refusals = await cell_attach_pg.own_role_refusals(conn, [])
                await conn.execute("RESET ROLE")
                observed[held] = [r.reason for r in refusals if "loom.cell_probe" in r.reason]
        assert observed == {
            held: (
                []
                if held == privileges
                else ["attach's role lacks SELECT, INSERT, UPDATE and DELETE on loom.cell_probe"]
            )
            for held in grants
        }
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_a_role_with_the_grants_but_not_the_owner_role_is_refused_before_any_write(
    cell_stores,
) -> None:
    from server.core import cell_triggers

    await _prepare(cell_stores, _Network())
    role = f"it_o_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        async with pg.dedicated() as conn:
            await conn.execute(f"GRANT USAGE ON SCHEMA loom TO {role}")
            await conn.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA loom TO {role}"
            )
            await conn.execute(f"SET ROLE {role}")
            rows = list(await conn.fetch(cell_triggers.ORG_TABLES))
            refusals = await cell_attach_pg.own_role_refusals(conn, rows)
            lacking = [r.reason for r in refusals if "owner role" in r.reason]
            assert (
                "attach's role lacks effective privileges of the owner role of loom.tenancy"
                in lacking
            )
            assert any("loom.scope_generations" in r for r in lacking)
            # The trigger pass tells the cause apart: denied, not a busy lock.
            outcome = await cell_triggers.ensure_table(
                conn, "scope_generations", recording_first=True
            )
            assert outcome.answer == "denied"
            await conn.execute("RESET ROLE")
            assert await _mode() == "recording"
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_doctor_names_a_table_it_cannot_read_and_reads_the_rest(cell_stores) -> None:
    from server.cli import doctor_cell

    stores = cell_stores
    await _prepare(stores, _Network())
    tag = stores["tag"]
    # An org table with no org-led index: its skip scan refuses, the read goes on.
    await pg.execute("CREATE TABLE loom.it_no_org_index (id int PRIMARY KEY, org text)")
    await pg.execute("INSERT INTO loom.it_no_org_index VALUES (1, $1)", tag + "unindexed")
    await pg.execute(
        "INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", tag + "stray"
    )
    lines = {n: (g, d) for n, g, d in await doctor_cell._stray_reads(stores["ch"], 300)}
    assert tag + "stray" in lines["cell_stray_orgs"][1]
    grade, detail = lines["cell_stray_orgs_unread"]
    assert grade == "WARN" and "postgres loom.it_no_org_index" in detail
    assert "cell_stray_orgs_unchecked" not in lines


@pytest.mark.asyncio
async def test_attach_exempts_the_role_it_runs_as_and_doctor_does_not(cell_stores) -> None:
    await _prepare(cell_stores, _Network())
    role = f"it_ci_{uuid.uuid4().hex[:6]}"
    await _admin(f"CREATE ROLE {role} LOGIN")
    try:
        async with pg.dedicated() as conn:
            await conn.execute(f"GRANT USAGE ON SCHEMA loom TO {role}")
            await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA loom TO {role}")
            await conn.execute(f"GRANT INSERT ON loom.tenancy TO {role}")
            await conn.execute(f"SET ROLE {role}")
            as_attach = await cell_attach_pg.privilege_holders(conn, exempt_self=True)
            as_doctor = await cell_attach_pg.privilege_holders(conn, exempt_self=False)
            await conn.execute("RESET ROLE")
        # Attach runs as the operator's role; a CI role doctor runs as is warned about.
        assert role in as_attach.own and role not in as_attach.others
        assert role not in as_doctor.own
        assert "may insert into loom.tenancy" in as_doctor.others[role]
    finally:
        async with pg.dedicated() as conn:
            await conn.execute(f"DROP OWNED BY {role}")
        await _admin(f"DROP OWNED BY {role}")
        await _admin(f"DROP ROLE {role}")


@pytest.mark.asyncio
async def test_wait_0_attaches_an_idle_cell(cell_stores, capsys) -> None:
    """--wait 0 is one try of each wait, no retry: an idle cell still reaches serving."""
    stores, network = cell_stores, _Network()
    await _prepare(stores, network)
    org = stores["tag"] + "w0"
    await pg.execute("INSERT INTO loom.scope_generations (org, scope) VALUES ($1, '*')", org)
    capsys.readouterr()
    assert await _attach(stores, network, wait=0) == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out)["state"] == "serving"
    assert org in await _placed()
