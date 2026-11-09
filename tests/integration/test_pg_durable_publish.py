"""Only revisions the synchronous standby holds reach ClickHouse.

These tests point `synchronous_standby_names` at a standby that is not attached, so
every commit waits, then restore it. The Postgres authority job runs its files one at a
time against a server with no background workers (LOOM_BACKGROUND_WORKERS=off), so no
other test commits meanwhile and this file's claims are the only publisher. They need
the CI layout (a primary with a synchronous standby, `dev/ci_start_postgres.sh`) and a
superuser role.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

_SEQUENTIAL = {"select_sequential_consistency": 1}
_ABSENT = "FIRST 1 (loom_absent_standby)"


@pytest.fixture
async def admin_conn(control_plane_url: str) -> AsyncIterator[Any]:
    """A superuser connection to the authority the server under test runs on."""
    import asyncpg

    from server.core import pg

    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    if os.environ.get("LOOM_PG_STANDBY") == "0":
        pytest.skip("Postgres started without a synchronous standby")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        if (await c.get("/v1/health")).json().get("storage_authority") != "postgres":
            pytest.skip("the server under test is not of the Postgres authority")
    assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
    # The test's own statements commit without the standby, which the tests detach.
    conn = await asyncpg.connect(dsn, server_settings={"synchronous_commit": "local"})
    await pg.init_connection(conn)
    try:
        if not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            pytest.skip("changing synchronous_standby_names needs a superuser")
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def short_waits(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """This process's pool, with a statement timeout short enough for a wait to end."""
    from server.core import pg
    from server.settings import settings

    await pg.close_pool()
    monkeypatch.setattr(settings, "pg_statement_timeout_ms", 1000)
    try:
        yield
    finally:
        await pg.close_pool()


@contextlib.asynccontextmanager
async def _standbys(conn: Any, names: str) -> AsyncIterator[None]:
    """`synchronous_standby_names` set to `names` for the block, then restored."""
    before = await conn.fetchval("SHOW synchronous_standby_names")
    await _set_standbys(conn, names)
    try:
        yield
    finally:
        await _set_standbys(conn, before)


async def _set_standbys(conn: Any, names: str) -> None:
    import asyncpg

    await conn.execute(f"ALTER SYSTEM SET synchronous_standby_names = '{names}'")
    await conn.execute("SELECT pg_reload_conf()")

    async def applied() -> bool:
        # A reload reaches each backend at its next command: a new session reads it.
        probe = await asyncpg.connect(os.environ["LOOM_PG_DSN"])
        try:
            return await probe.fetchval("SHOW synchronous_standby_names") == names
        finally:
            await probe.close()

    await eventually(applied, description=f"synchronous_standby_names = {names!r}")


async def test_a_durable_transaction_waits_for_the_standby(
    admin_conn: Any, short_waits: None
) -> None:
    from server.core import pg

    async with _standbys(admin_conn, _ABSENT):
        with pytest.raises(pg.PostgresUnavailableError) as raised:
            async with pg.durable_transaction() as conn:
                await conn.fetchval("SELECT 1")
        assert pg.may_have_committed(raised.value)
    async with pg.durable_transaction() as conn:  # the standby back: it commits
        assert await conn.fetchval("SELECT 1") == 1


@pytest.mark.parametrize("durability, refused", [("synchronous", True), ("local", False)])
async def test_no_standby_named_refuses_under_synchronous_only(
    admin_conn: Any,
    short_waits: None,
    monkeypatch: pytest.MonkeyPatch,
    durability: str,
    refused: bool,
) -> None:
    from server.core import pg
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_durability", durability)
    async with _standbys(admin_conn, ""):
        if refused:
            with pytest.raises(pg.PostgresUnavailableError):
                async with pg.durable_transaction():
                    pass
        else:
            async with pg.durable_transaction():
                pass


async def test_the_boot_check_writes_the_message_without_waiting(
    admin_conn: Any, short_waits: None
) -> None:
    from server.core import pg

    async with _standbys(admin_conn, _ABSENT):
        await asyncio.wait_for(pg.check_durable_message(), timeout=5)  # rolled back: no wait


async def test_a_connection_lost_before_the_commit_sends_nothing(
    admin_conn: Any, short_waits: None
) -> None:
    from server.core import pg

    with pytest.raises(pg.PostgresUnavailableError):
        async with pg.durable_transaction() as conn:
            pid = await conn.fetchval("SELECT pg_backend_pid()")
            await admin_conn.execute("SELECT pg_terminate_backend($1)", pid)


async def test_a_commit_on_the_primary_alone_reaches_clickhouse_once_the_standby_has_it(
    control_plane_url: str,
    mint_test_token: Any,
    admin_conn: Any,
    short_waits: None,
    ch_client: Any,
) -> None:
    """A write whose commit wait the server cut off is on the primary alone: no claim
    takes it while the standby lacks it, and a search publishes it once it holds it."""
    from server.core import pg
    from server.memory.store import pg_memory

    org, ns = unique_org("pgdur"), f"pg-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    body = {
        "org": org,
        "namespace": ns,
        "agent": "a",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": "a fact the standby has not seen",
    }

    async def due_now() -> None:
        await admin_conn.execute(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 second' "
            "WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )

    def in_clickhouse() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.memory FINAL WHERE org = {o:String} "
                "AND namespace = {ns:String}",
                settings=_SEQUENTIAL,
                parameters={"o": org, "ns": ns},
            ).result_rows[0][0]
        )

    async with _standbys(admin_conn, _ABSENT):
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
            answer = await c.post(
                "/v1/memory.set", json=body, headers={"Authorization": f"Bearer {token}"}
            )
        assert answer.status_code == 503, answer.text  # the commit's wait was cut off
        owed = await admin_conn.fetchval(
            "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
        assert owed == 1, "the commit is on the primary"
        await due_now()
        with pytest.raises(pg.PostgresUnavailableError):
            await pg_memory.claim_due(10, scope=(org, ns))
        assert in_clickhouse() == 0
    await due_now()  # the cut-off claim leased it
    # A search in the namespace publishes its backlog at the barrier, as the worker would.
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
        found = await c.post(
            "/v1/memory.search",
            json={"org": org, "namespace": ns, "agent": "a", "query": "the standby"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert found.status_code == 200, found.text
    assert in_clickhouse() == 1
