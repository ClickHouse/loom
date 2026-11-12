"""`loom cell prepare` (server/cli/cell_prepare.py) against a real Postgres and ClickHouse,
with the router and the cell's probe listener served in process: the first cell
registers, its rows are written from the router's answer and read back, the nonces are
gone, and a rerun writes nothing; processes that disagree write no cell row (a missing
heartbeat or another `LOOM_CELL` registers nothing either). The cell's Postgres and the
directory are databases of the test's own (a `first` row is never deleted); the live
processes are narrowed to the test's own, since the integration server beats into the
same ClickHouse. Needs LOOM_PG_DSN, LOOM_ROUTER_PG_DSN and LOOM_CH_HOST."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from typing import Any

import httpx
import pytest

from server.cli import cell as cell_cli
from server.cli import cell_prepare
from server.core import cell, cell_probe, instances, pg
from server.core.ch import get_client
from server.core.leases import LOCK_TABLE
from server.core.schema import pg_migrate
from server.router import app as router_app
from server.router import directory
from server.settings import settings

OPERATOR = "lco_" + "1" * 128
CREDENTIAL_SHA = hashlib.sha256(b"lcr_eu-1").hexdigest()


async def _own_database(admin_dsn: str) -> tuple[str, str]:
    import asyncpg

    name = f"loom_t{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return name, admin_dsn.rsplit("/", 1)[0] + "/" + name


async def _drop(admin_dsn: str, name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture
async def stores(monkeypatch: pytest.MonkeyPatch):
    cell_dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    router_dsn = os.environ.get("LOOM_ROUTER_PG_DSN") or pytest.skip("LOOM_ROUTER_PG_DSN not set")
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    cell_db, own_cell = await _own_database(cell_dsn)
    router_db, own_router = await _own_database(router_dsn)
    deployment = f"dep-{uuid.uuid4().hex[:8]}"
    for name, value in {
        "cell": "eu-1",
        "storage_authority": "postgres",
        "pg_dsn": own_cell,
        "document_s3_bucket": "",
        "router_pg_dsn": own_router,
        "router_shared_host": "loom.example",
        "router_upstream_probe_url": "http://listener.internal:7790",
        "router_upstream_url": "http://upstream.internal:7777",
        "router_operator_sha256": hashlib.sha256(OPERATOR.encode()).hexdigest(),
        "router_operator_token": OPERATOR,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(directory, "_pool", None)
    ch = get_client()
    try:
        await pg_migrate.migrate()
        await pg.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, $1)",
            deployment,
        )
        await directory.migrate()
        await ch.connect()
        cell.set_deployment_id(deployment)
        live = [instances.instance_id()]
        monkeypatch.setattr(
            instances,
            "read_live",
            _live(live),
        )
        # The first cell's processes run without LOOM_CELL until attach.
        await ch.insert(instances.CELL_TABLE, [{**instances.cell_row(), "cell": ""}])
        yield ch, live
    finally:
        cell.set_deployment_id("")
        await ch.execute(
            f"ALTER TABLE {instances.CELL_TABLE} DELETE WHERE instance_id LIKE {{p:String}}",
            {"p": instances.instance_id() + "%"},
        )
        await ch.disconnect()
        await pg.close_pool()
        await directory.close()
        await _drop(cell_dsn, cell_db)
        await _drop(router_dsn, router_db)


def _live(ids: list[str]) -> Any:
    async def read_live(_ch: Any, *, all_deployments: bool = False) -> list[dict[str, Any]]:
        return [{"instance_id": i} for i in ids]

    return read_live


def _args(**over: Any) -> argparse.Namespace:
    fields = {
        "cell": "eu-1",
        "host": "LOOM.example",
        "credential_sha256": CREDENTIAL_SHA,
        "probe_url": "",
        "wait": 0,
        "router": "http://127.0.0.1:7900",
    }
    return argparse.Namespace(**{**fields, **over})


async def _prepare(ch: Any, args: argparse.Namespace, transport: Any) -> int:
    router = cell_cli.operator_router(args)
    assert isinstance(router, httpx.URL)
    return await cell_prepare.prepare(ch, args, router, transport)


class _CellPorts(httpx.AsyncBaseTransport):
    """The cell as the router reaches it: the upstream's main port (:7777) answers what
    `GET /v1/health/deployment` does there, any other port is the probe listener."""

    def __init__(self) -> None:
        self.listener = httpx.ASGITransport(app=cell_probe.listener_app())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.port == 7777:
            return httpx.Response(200, json=cell_probe.answer())
        return await self.listener.handle_async_request(request)


def _router_transport() -> httpx.ASGITransport:
    router = router_app.Router(probe_transport=_CellPorts())
    return httpx.ASGITransport(app=router_app.build_app(router))


async def _cell_rows() -> dict[str, Any]:
    first = await pg.fetchrow("SELECT is_first, cell FROM loom.cell_first")
    own = await pg.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
    return {
        "first": None if first is None else (first["is_first"], first["cell"]),
        "mode": await pg.fetchval("SELECT mode FROM loom.tenancy_mode"),
        "current": None if own is None else own["current_fp"],
        "next": None if own is None else own["next_fp"],
    }


async def _nonces_left(ch: Any) -> tuple[int, Any]:
    in_pg = await pg.fetchval("SELECT count(*) FROM loom.cell_probe")
    in_ch = await ch.query_one(
        f"SELECT count() AS n FROM {LOCK_TABLE} WHERE name = {{name:String}}",
        {"name": cell_probe.NONCE_NAME},
    )
    return int(in_pg), int(in_ch["n"])


@pytest.mark.asyncio
async def test_the_first_cell_registers_and_writes_its_rows_once(stores, capsys) -> None:
    ch, _live_ids = stores
    assert await _prepare(ch, _args(), _router_transport()) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "cell": "eu-1",
        "is_first": True,
        "state": "attaching",
        "mode": "recording",
        "registered": True,
        "rows_written": ["first", "mode", "own"],
    }
    reported = cell.as_value(cell.mine())
    assert await _cell_rows() == {
        "first": (True, "eu-1"),
        "mode": "recording",
        "current": reported,
        "next": None,
    }
    assert await _nonces_left(ch) == (0, 0)
    row = await directory.first_cell_named("eu-1")
    assert row is not None and (row.host, row.state, row.credential_sha256) == (
        "loom.example",
        "attaching",
        CREDENTIAL_SHA,
    )
    assert row.deployment_id == cell.deployment_id()
    # A rerun answers the same row and writes nothing.
    assert await _prepare(ch, _args(), _router_transport()) == 0
    again = json.loads(capsys.readouterr().out)
    assert (again["registered"], again["rows_written"]) == (False, [])
    assert await _cell_rows() == {
        "first": (True, "eu-1"),
        "mode": "recording",
        "current": reported,
        "next": None,
    }
    # Before attach the own row stands: a process on neither its current nor its next
    # (a configuration rolled out without `loom cell update`) is refused, nothing written.
    drifted = cell.as_value({**cell.mine(), "oidc_client_id": "drifted"})
    await ch.insert(
        instances.CELL_TABLE,
        [{**instances.cell_row(), "fingerprint": json.dumps(drifted, sort_keys=True)}],
    )
    with pytest.raises(cell_prepare.Refused, match="neither this cell's own row's current"):
        await _prepare(ch, _args(), _router_transport())
    assert (await _cell_rows())["current"] == reported
    await ch.insert(instances.CELL_TABLE, [instances.cell_row()])
    # Once attach wrote the cell's fingerprint to the directory, a rerun writes the own row
    # from it, overwriting one that differs.
    import asyncpg

    router_db = await asyncpg.connect(settings.router_pg_dsn)
    try:
        await router_db.execute(
            "UPDATE loom_router.cells SET current_fp = $2 WHERE cell = $1",
            "eu-1",
            json.dumps(reported),
        )
    finally:
        await router_db.close()
    stale = cell.as_value({**cell.mine(), "oidc_client_id": "stale"})
    await pg.execute("UPDATE loom.cell_self SET current_fp = $1", stale)
    assert await _prepare(ch, _args(), _router_transport()) == 0
    attached = json.loads(capsys.readouterr().out)
    assert attached["rows_written"] == ["own"]
    assert (await _cell_rows())["current"] == reported


@pytest.mark.asyncio
async def test_processes_that_disagree_write_no_row(stores) -> None:
    ch, live_ids = stores
    other = instances.instance_id() + "-other"
    live_ids.append(other)
    # A live process with no heartbeat row, or one with another LOOM_CELL, is refused
    # before anything is written.
    with pytest.raises(cell_prepare.Refused, match="no readable heartbeat row"):
        await _prepare(ch, _args(), _router_transport())
    theirs = {**instances.cell_row(), "instance_id": other}
    await ch.insert(instances.CELL_TABLE, [{**theirs, "cell": "eu-2"}])
    with pytest.raises(cell_prepare.Refused, match="another LOOM_CELL"):
        await _prepare(ch, _args(), _router_transport())
    assert await directory.first_cell() is None
    # Processes on different configurations: whether they may differ is the directory's
    # to say (after attach, a rollout runs `current` and `next`), so the cell registers and
    # no row is written until they agree; a rerun then finishes.
    fields = {**cell.mine(), "oidc_client_id": "another-client"}
    await ch.insert(
        instances.CELL_TABLE,
        [{**theirs, "fingerprint": json.dumps(cell.as_value(fields), sort_keys=True)}],
    )
    with pytest.raises(cell_prepare.Refused, match="different configurations"):
        await _prepare(ch, _args(), _router_transport())
    assert await _cell_rows() == {"first": None, "mode": None, "current": None, "next": None}
    assert await _nonces_left(ch) == (0, 0)
    assert await directory.first_cell() is not None
    live_ids.remove(other)
    assert await _prepare(ch, _args(), _router_transport()) == 0
    assert (await _cell_rows())["first"] == (True, "eu-1")


@pytest.mark.asyncio
async def test_stores_with_another_cells_state_are_refused_before_registering(stores) -> None:
    ch, _live_ids = stores
    # A mode row with no `first` row: written by no prepare of this directory.
    await pg.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
    with pytest.raises(cell_prepare.Refused, match="another cell's state"):
        await _prepare(ch, _args(), _router_transport())
    assert await directory.first_cell() is None
    assert await _cell_rows() == {"first": None, "mode": "enforced", "current": None, "next": None}
    assert await _nonces_left(ch) == (0, 0)


@pytest.mark.asyncio
async def test_a_later_cell_registers_after_the_first_as_enforced(
    stores, monkeypatch, capsys
) -> None:
    ch, live_ids = stores
    transport = _router_transport()
    assert await _prepare(ch, _args(), transport) == 0
    capsys.readouterr()
    # The later cell's own Postgres, deployment and LOOM_CELL; its processes beat with it.
    cell_dsn = os.environ["LOOM_PG_DSN"]
    later_db, later_dsn = await _own_database(cell_dsn)
    try:
        await pg.close_pool()
        monkeypatch.setattr(settings, "pg_dsn", later_dsn)
        monkeypatch.setattr(settings, "cell", "eu-2")
        await pg_migrate.migrate()
        later_id = f"dep-{uuid.uuid4().hex[:8]}"
        await pg.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, $1)",
            later_id,
        )
        cell.set_deployment_id(later_id)
        await ch.insert(instances.CELL_TABLE, [instances.cell_row()])
        sha = hashlib.sha256(b"lcr_eu-2").hexdigest()
        args = _args(
            cell="eu-2",
            host="eu-2.loom.example",
            credential_sha256=sha,
            probe_url="http://eu-2.internal:7790",
        )
        # A live process without its LOOM_CELL: a later cell runs with it from its first
        # boot, so the router registers nothing.
        unset = instances.instance_id() + "-unset"
        live_ids.append(unset)
        await ch.insert(
            instances.CELL_TABLE, [{**instances.cell_row(), "instance_id": unset, "cell": ""}]
        )
        with pytest.raises(cell_prepare.Refused, match="without its LOOM_CELL"):
            await _prepare(ch, args, transport)
        cells, _aliases = await directory.routing()
        assert [c["cell"] for c in cells] == ["eu-1"]
        assert await _cell_rows() == {"first": None, "mode": None, "current": None, "next": None}
        live_ids.remove(unset)
        assert await _prepare(ch, args, transport) == 0
        printed = json.loads(capsys.readouterr().out)
        assert (printed["is_first"], printed["mode"], printed["rows_written"]) == (
            False,
            "enforced",
            ["first", "mode", "own"],
        )
        assert await _cell_rows() == {
            "first": (False, "eu-2"),
            "mode": "enforced",
            "current": cell.as_value(cell.mine()),
            "next": None,
        }
        assert await _nonces_left(ch) == (0, 0)
    finally:
        await pg.close_pool()
        await _drop(cell_dsn, later_db)


@pytest.mark.asyncio
async def test_a_run_deletes_only_its_own_nonces(stores) -> None:
    """Another run that started meanwhile replaced them, and its probe still needs them."""
    ch, _live_ids = stores
    mine, theirs = cell_prepare._new_nonces(), cell_prepare._new_nonces()
    await cell_prepare._write_nonces(ch, mine)
    await cell_prepare._write_nonces(ch, theirs)
    await cell_prepare._delete_nonces(ch, mine)
    assert await cell_probe.nonces() == {**theirs, "bucket": None}
    await cell_prepare._delete_nonces(ch, theirs)
    assert await _nonces_left(ch) == (0, 0)
