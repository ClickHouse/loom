"""Real event CHECK metadata, old insert drain and grant refusal on a private database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core import ch as ch_module
from server.core.ch import CHClient, as_analyst, query_tag, uncaptured
from server.core.sql_literal import sql_string
from server.insights.domain import analysis as boundary
from server.insights.domain.observation_policy import observer_policies_allow
from server.insights.store import analysis as analysis_store
from server.insights.store import event_cutover

pytestmark = pytest.mark.usefixtures("ch_target_required")


@asynccontextmanager
async def _observer_principal(world, monkeypatch):
    """A private native login whose policies cannot narrow other test principals."""
    username, password = "cutover_observer_" + uuid4().hex, uuid4().hex
    client = CHClient()
    configuration = ch_module.connection_kwargs()
    try:
        with uncaptured():
            await world.client.execute(
                f"CREATE USER {username} IDENTIFIED WITH sha256_password BY "
                + sql_string(password),
                settings={"log_queries_cut_to_length": 1, "log_formatted_queries": 0},
            )
        for access in (
            f"SELECT ON {world.db}.*",
            "SELECT ON system.*",
            "SHOW ROW POLICIES ON *.*",
        ):
            await world.client.execute(f"GRANT {access} TO {username}")
        await world.client.execute(f"GRANT READ ON REMOTE TO {username}")
        with monkeypatch.context() as patch:
            patch.setattr(
                ch_module,
                "connection_kwargs",
                lambda: {
                    **configuration,
                    "username": username,
                    "password": password,
                },
            )
            try:
                await client.connect()
                assert await client.query_one("SELECT currentUser() AS user") == {"user": username}
                writers = await event_cutover._writers(client)
                async with client.policy_observers() as observers:
                    assert {observer.server_uuid for observer in observers} == writers.servers
                    for observer in observers:
                        assert isinstance(await observer.policies(), list)
                yield client, username
            finally:
                await client.disconnect()
    finally:
        await world.client.execute(f"DROP USER IF EXISTS {username}")


@asynccontextmanager
async def _hidden_population(world, username, target, predicate):
    """Keep other principals unrestricted while narrowing only the owned observer."""
    suffix = uuid4().hex
    allow, hide = "cutover_allow_" + suffix, "cutover_hide_" + suffix
    try:
        await world.client.execute(
            f"CREATE ROW POLICY {allow} ON {target} AS PERMISSIVE FOR SELECT USING 1 TO ALL"
        )
        await world.client.execute(
            f"CREATE ROW POLICY {hide} ON {target} AS RESTRICTIVE "
            f"FOR SELECT USING {predicate} TO {username}"
        )
        yield
    finally:
        for name in (hide, allow):
            await world.client.execute(f"DROP ROW POLICY IF EXISTS {name} ON {target}")


@pytest_asyncio.fixture
async def world(monkeypatch) -> AsyncIterator[SimpleNamespace]:
    client = CHClient()
    db, identity = "event_cutover_" + uuid4().hex, str(uuid4())
    attempted = False
    try:
        await client.connect()
        attempted = True
        await client.execute(f"CREATE DATABASE {db} UUID '{identity}' ENGINE = Atomic")
        for name in ("agent_events", "agent_events_private"):
            await client.execute(f"CREATE TABLE {db}.{name} AS loom.{name}")
        monkeypatch.setattr(event_cutover, "database", lambda: db)
        yield SimpleNamespace(client=client, db=db)
    finally:
        try:
            if attempted:
                row = await client.query_one(
                    "SELECT toString(uuid) AS uuid FROM system.databases WHERE name = {db:String}",
                    params={"db": db},
                )
                if row:
                    assert row["uuid"] == identity, "Private cutover database identity changed"
                    await client.execute(f"DROP DATABASE {db} SYNC")
        finally:
            await client.disconnect()


async def test_native_check_is_enforced_and_clean_storage_is_confirmed(world) -> None:
    await event_cutover.confirm_ordinary_events(ch=world.client)
    with pytest.raises(Exception, match="CONSTRAINT|Constraint"):
        await world.client.execute(
            f"INSERT INTO {world.db}.agent_events (mirror_marked) VALUES (1)",
            settings={"async_insert": 0},
        )
    row = await world.client.query_one(f"SELECT count() AS n FROM {world.db}.agent_events")
    assert row and row["n"] == 0


async def test_native_reprovisioning_accepts_installed_generated_event_policies(world) -> None:
    # The running deployment owns this login; only policies on our private tables change.
    assert await world.client.query_one(
        "SELECT count() AS n FROM system.users WHERE name = {user:String}",
        {"user": boundary.ANALYST_USER},
    ) == {"n": 1}
    prefix = await analysis_store.setting_prefix(ch=world.client)
    assert prefix is not None
    tables = ("agent_events", "agent_events_private")
    statements = [
        boundary.policy_sql(f"`{world.db}`", name, table, using, recipient)
        for table in tables
        for name, using, recipient in (
            (boundary.OTHERS_POLICY_NAME, "1", f"ALL EXCEPT `{boundary.ANALYST_USER}`"),
            (boundary.POLICY_NAME, "0", f"`{boundary.ANALYST_USER}`"),
        )
    ]
    username = "cutover_grantee_" + uuid4().hex
    try:
        await world.client.execute(f"CREATE USER {username} IDENTIFIED WITH no_password")
        statements.append(f"GRANT SELECT ON {world.db}.agent_events TO {username}")
        await analysis_store.write_entities(
            statements, names=boundary.SettingNames(prefix), ch=world.client
        )
        async with world.client.policy_observers() as channels:
            catalog = tuple(await channels[0].policies())
            installed = tuple(statement for statement in catalog if world.db in statement)
            assert len(installed) == 4
            assert observer_policies_allow(installed, world.db)
        # The second grant proof observes the database's actual SHOW CREATE spelling.
        await analysis_store.write_entities(
            statements, names=boundary.SettingNames(prefix), ch=world.client
        )
        await event_cutover.confirm_ordinary_events(ch=world.client)
    finally:
        for table in tables:
            for name in (boundary.POLICY_NAME, boundary.OTHERS_POLICY_NAME):
                await world.client.execute(
                    f"DROP ROW POLICY IF EXISTS `{name}` ON {world.db}.{table}"
                )
        await world.client.execute(f"DROP USER IF EXISTS {username}")


@pytest.mark.parametrize("kind", ["missing", "wrong", "assume"])
async def test_native_missing_wrong_or_assumed_constraint_is_not_a_fence(world, kind) -> None:
    await world.client.execute(
        f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
    )
    if kind != "missing":
        clause = "CHECK 1" if kind == "wrong" else f"ASSUME {event_cutover.ORDINARY}"
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events ADD CONSTRAINT ordinary_events_only {clause}"
        )
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world.client)


async def test_native_private_contamination_is_preserved_and_refused(world) -> None:
    await world.client.execute(
        f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
    )
    await world.client.execute(
        f"INSERT INTO {world.db}.agent_events (mirror_marked) VALUES (1)",
        settings={"async_insert": 0},
    )
    await world.client.execute(
        f"ALTER TABLE {world.db}.agent_events ADD CONSTRAINT ordinary_events_only "
        f"CHECK {event_cutover.ORDINARY}"
    )
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world.client)
    row = await world.client.query_one(f"SELECT count() AS n FROM {world.db}.agent_events")
    assert row and row["n"] == 1


async def test_native_filtered_contamination_and_self_hidden_policy_catalog_still_refuse(
    world, monkeypatch
):
    async with _observer_principal(world, monkeypatch) as (observer, username):
        await event_cutover.confirm_ordinary_events(ch=observer)
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
        )
        await world.client.execute(
            f"INSERT INTO {world.db}.agent_events (mirror_marked) VALUES (1)",
            settings={"async_insert": 0},
        )
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events ADD CONSTRAINT ordinary_events_only "
            f"CHECK {event_cutover.ORDINARY}"
        )
        async with _hidden_population(
            world, username, f"{world.db}.agent_events", "mirror_marked = 0"
        ):
            async with _hidden_population(world, username, "system.row_policies", "0"):
                assert await observer.query_one(
                    f"SELECT count() AS n FROM {world.db}.agent_events"
                ) == {"n": 0}
                assert await observer.query_one("SELECT count() AS n FROM system.row_policies") == {
                    "n": 0
                }
                with pytest.raises(event_cutover.EventCutoverUnavailable):
                    await event_cutover.confirm_ordinary_events(ch=observer)
                assert await world.client.query_one(
                    f"SELECT count() AS n FROM {world.db}.agent_events"
                ) == {"n": 1}


async def _running(client: CHClient, marker: str) -> dict | None:
    return await client.query_one(
        "SELECT query_id, read_rows FROM system.processes "
        "WHERE query_kind = 'Insert' AND Settings['log_comment'] = {tag:String}",
        params={"tag": marker},
    )


@pytest.mark.parametrize("hide_old_insert", [False, True])
async def test_native_insert_with_old_metadata_drains_before_the_private_census(
    world, monkeypatch, hide_old_insert
) -> None:
    producer = CHClient()
    observers_stack = AsyncExitStack()
    task = None
    marker = "cutover_old_insert_" + uuid4().hex
    producer_user, producer_password = "cutover_producer_" + uuid4().hex, uuid4().hex
    user_created = False
    try:
        if hide_old_insert:
            observer, username = await observers_stack.enter_async_context(
                _observer_principal(world, monkeypatch)
            )
            await event_cutover.confirm_ordinary_events(ch=observer)
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
        )
        user_created = True
        with uncaptured():
            await world.client.execute(
                f"CREATE USER {producer_user} IDENTIFIED WITH sha256_password BY "
                + sql_string(producer_password),
                settings={"log_queries_cut_to_length": 1, "log_formatted_queries": 0},
            )
        await world.client.execute(f"GRANT INSERT ON {world.db}.agent_events TO {producer_user}")
        await producer.connect()

        async def insert() -> None:
            with query_tag(marker), as_analyst(producer_user, producer_password, {}):
                assert await producer.query_one("SELECT currentUser() AS user") == {
                    "user": producer_user
                }
                await producer.raw_bytes(
                    f"INSERT INTO {world.db}.agent_events (mirror_marked) "
                    "SELECT toUInt8(1) FROM numbers(100) WHERE sleepEachRow(0.05) = 0 "
                    "SETTINGS async_insert = 0, max_block_size = 10, max_threads = 1, "
                    "max_execution_time = 8, timeout_overflow_mode = 'throw'"
                )

        task = asyncio.create_task(insert())
        async with asyncio.timeout(5):
            while not (row := await _running(world.client, marker)) or not row["read_rows"]:
                assert not task.done(), "The fixture missed the prepared INSERT"
                await asyncio.sleep(0.01)
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events ADD CONSTRAINT ordinary_events_only "
            f"CHECK {event_cutover.ORDINARY}"
        )
        assert not task.done(), "The fixture must observe an INSERT crossing the CHECK"
        query_id = row["query_id"]
        observed = {"old": False, "census": False}

        def recording(read):
            async def query(sql, params=None):
                if "AS private" in sql:
                    assert observed["old"]
                    assert await _running(world.client, marker) is None
                    observed["census"] = True
                rows = await read(sql, params)
                if "FROM system.processes" in sql:
                    observed["old"] |= any(
                        (producer_user, query_id) == tuple(item)
                        for entry in rows
                        for item in entry["inserts"]
                    )
                return rows

            return query

        @asynccontextmanager
        async def observers():
            async with world.client.policy_observers() as channels:
                yield tuple(
                    SimpleNamespace(
                        server_uuid=channel.server_uuid,
                        user=channel.user,
                        roles=channel.roles,
                        policies=channel.policies,
                        query_all=recording(channel.query_all),
                    )
                    for channel in channels
                )

        if hide_old_insert:
            async with _hidden_population(world, username, "system.processes", "0"):
                assert await _running(world.client, marker) is not None
                assert await observer.query_one(
                    "SELECT count() AS n FROM system.processes WHERE query_kind = 'Insert' "
                    "AND query_id = {qid:String}",
                    {"qid": query_id},
                ) == {"n": 0}
                with pytest.raises(event_cutover.EventCutoverUnavailable):
                    await event_cutover.confirm_ordinary_events(ch=observer)
            assert observed == {"old": False, "census": False}
        else:
            with pytest.raises(event_cutover.EventCutoverUnavailable):
                await event_cutover.confirm_ordinary_events(
                    ch=SimpleNamespace(
                        query_all=recording(world.client.query_all),
                        policy_observers=observers,
                    )
                )
            assert observed == {"old": True, "census": True}
        await task
        assert await _running(world.client, marker) is None
        row = await world.client.query_one(f"SELECT count() AS n FROM {world.db}.agent_events")
        assert row and row["n"] == 100
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await producer.disconnect()
        await observers_stack.aclose()
        if user_created:
            await world.client.execute(f"DROP USER IF EXISTS {producer_user}")


async def _queued(world, query_id: str) -> None:
    rows = await world.client.query_all(
        "SELECT `entries.query_id` AS ids, `entries.bytes` AS sizes "
        "FROM system.asynchronous_inserts WHERE database = {db:String} "
        "AND table = 'agent_events' AND has(`entries.query_id`, {qid:String})",
        params={"db": world.db, "qid": query_id},
    )
    assert len(rows) == 1, "The exact detached request must still be queued"
    assert rows[0]["ids"].count(query_id) == 1
    assert rows[0]["sizes"][rows[0]["ids"].index(query_id)] > 0


async def _rejected_flush(world, query_id: str) -> None:
    # The delayed log write, rather than queue disappearance, proves the exact terminal outcome.
    async with asyncio.timeout(20):
        while True:
            row = await world.client.query_one(
                "SELECT status, exception, flush_query_id FROM system.asynchronous_insert_log "
                "WHERE database = {db:String} AND table = 'agent_events' "
                "AND query_id = {qid:String} ORDER BY event_time_microseconds DESC LIMIT 1",
                params={"db": world.db, "qid": query_id},
            )
            if row:
                assert row["status"] == "FlushError", row
                assert "ordinary_events_only" in row["exception"], row
                assert row["flush_query_id"], row
                return
            await asyncio.sleep(0.05)


async def _submit_detached(world, query_id: str) -> None:
    producer = CHClient()
    try:
        await producer.connect()
        observer = await world.client.query_one("SELECT toString(serverUUID()) AS id")
        assert await producer.query_one("SELECT toString(serverUUID()) AS id") == observer
        await producer.execute(
            f"INSERT INTO {world.db}.agent_events (mirror_marked) VALUES (1)",
            settings={
                "async_insert": 1,
                "wait_for_async_insert": 0,
                "async_insert_use_adaptive_busy_timeout": 0,
                "async_insert_busy_timeout_max_ms": 60000,
                "query_id": query_id,
            },
        )
    finally:
        await producer.disconnect()


async def test_native_disconnected_queued_insert_uses_the_installed_check(world) -> None:
    query_id = "cutover_queued_" + uuid4().hex
    await world.client.execute(
        f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
    )
    try:
        await _submit_detached(world, query_id)
        await _queued(world, query_id)
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events ADD CONSTRAINT ordinary_events_only "
            f"CHECK {event_cutover.ORDINARY}"
        )
        await _queued(world, query_id)
        # Scoped flush waits its collected batches; their exceptions are logged separately.
        await world.client.execute(f"SYSTEM FLUSH ASYNC INSERT QUEUE {world.db}.agent_events")
        await _rejected_flush(world, query_id)
        await event_cutover.confirm_ordinary_events(ch=world.client)
        with pytest.raises(Exception, match="ordinary_events_only"):
            await world.client.execute(
                f"INSERT INTO {world.db}.agent_events (mirror_marked) VALUES (1)",
                settings={"async_insert": 0},
            )
        row = await world.client.query_one(f"SELECT count() AS n FROM {world.db}.agent_events")
        assert row and row["n"] == 0
    finally:
        # A failed observation must not leave this database's buffered request behind.
        await world.client.execute(f"SYSTEM FLUSH ASYNC INSERT QUEUE {world.db}.agent_events")


@pytest.mark.parametrize("valid", [False, True])
async def test_native_unchecked_print_refuses_with_or_without_a_valid_fence(
    world, valid, monkeypatch, capsys
) -> None:
    import argparse

    from server.cli import ch_setup

    if not valid:
        await world.client.execute(
            f"ALTER TABLE {world.db}.agent_events DROP CONSTRAINT ordinary_events_only"
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Unchecked files must not execute grants or administrative requests")

    monkeypatch.setattr(ch_setup, "run_connected", forbidden)
    args = argparse.Namespace(print_analyst_sql=True, apply_analyst_sql=False, print_sql=False)
    assert ch_setup._cmd_ch_setup(args) == 2
    out = capsys.readouterr()
    assert not out.out and "--apply-analyst-sql" in out.err
