"""A cell's org triggers and its `loom.tenancy` guard against a real Postgres
(server/core/cell_triggers.py, migrations 011 and 012): `ensure` creates them only on a
fully prepared cell in `enforced`, an unmarked session's row for an org the cell does not
hold, and its tenancy row, are refused there, a marked session's rows pass, and `missing`
names what the triggers lack. Each test runs in a database of its own (`_pg_database`):
`ensure` never drops a trigger."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from server.core import cell, cell_triggers, pg
from server.core.schema import pg_migrate
from tests.integration._pg_database import own_database

_OWN = {"v": 1, "fields": {}}
_INSERT = "INSERT INTO loom.scope_generations (org, scope, generation) VALUES ($1, $2, 1)"
_HOLD = "INSERT INTO loom.tenancy (org) VALUES ($1)"


@contextlib.asynccontextmanager
async def _cell(
    state: str, *, held: tuple[str, ...] = ("held-org",)
) -> AsyncIterator[tuple[str, Any]]:
    """A migrated database in `state`, with a marked connection holding `held` orgs."""
    import asyncpg

    async with own_database() as dsn:
        conn = await asyncpg.connect(dsn, server_settings={"loom.tenancy_checked": "on"})
        await pg.init_connection(conn)
        try:
            if state in ("enforced", "recording", "later-recording"):
                first = state != "later-recording"
                mode = "enforced" if state == "enforced" else "recording"
                await conn.execute(
                    "INSERT INTO loom.cell_first (is_first, cell) VALUES ($1, 'eu-1')", first
                )
                await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ($1)", mode)
                await conn.execute(
                    "INSERT INTO loom.cell_self (current_fp, next_fp) VALUES ($1::jsonb, NULL)",
                    _OWN,
                )
            elif state == "partial":
                await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
            for org in held:
                await conn.execute("INSERT INTO loom.tenancy (org) VALUES ($1)", org)
            yield dsn, conn
        finally:
            await conn.close()


async def _unmarked(dsn: str) -> Any:
    import asyncpg

    return await asyncpg.connect(dsn)


async def _cell_triggers(conn: Any) -> set[tuple[str, str, str]]:
    rows = await conn.fetch(
        "SELECT c.relname, t.tgname, t.tgenabled::text AS e FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal "
        "AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record')"
    )
    return {(r["relname"], r["tgname"], r["e"]) for r in rows}


async def _create_by_hand(conn: Any) -> None:
    """The triggers on scope_generations where `ensure` would not create them."""
    await conn.execute(
        cell_triggers._create("loom.scope_generations", cell_triggers.INSERT_TRIGGER)
    )
    await conn.execute(
        cell_triggers._create("loom.scope_generations", cell_triggers.UPDATE_TRIGGER)
    )


@pytest.mark.asyncio
async def test_a_deployment_never_prepared_gets_no_trigger_and_its_writes_pass() -> None:
    async with _cell("open", held=()) as (dsn, conn):
        await cell_triggers.ensure(conn)
        assert await _cell_triggers(conn) == set()
        plain = await _unmarked(dsn)
        try:
            await plain.execute(_INSERT, "any-org", "s")
        finally:
            await plain.close()


@pytest.mark.asyncio
async def test_ensure_guards_every_org_table_of_an_enforced_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _cell("enforced") as (_dsn, conn):
        await cell_triggers.ensure(conn)
        guarded = [
            r["relname"] for r in cell_triggers._guarded(await conn.fetch(cell_triggers.ORG_TABLES))
        ]
        assert "scope_generations" in guarded and "tenancy" not in guarded
        assert "idempotency" not in guarded and "idempotency_fences" not in guarded
        want = {(t, n, "O") for t in guarded for n in cell_triggers.ORG_TRIGGERS}
        want.add(("tenancy", cell_triggers.TENANCY_TRIGGER, "O"))
        assert await _cell_triggers(conn) == want
        assert [d for d in await cell_triggers.missing(conn)] == []
        # A second boot finds everything present: no statement runs.
        ran: list[list[str]] = []
        real = cell_triggers._run_ddl

        async def _count(c: Any, table: str, sqls: list[str]) -> None:
            ran.append(sqls)
            await real(c, table, sqls)

        monkeypatch.setattr(cell_triggers, "_run_ddl", _count)
        await cell_triggers.ensure(conn)
        assert ran == []


@pytest.mark.asyncio
async def test_an_unmarked_session_is_refused_an_unheld_org_on_an_enforced_cell() -> None:
    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        await conn.execute(_INSERT, "stray-org", "kept")  # an org already stored, marked
        plain = await _unmarked(dsn)
        try:
            refused = [
                (_INSERT, ("unheld-org", "s")),
                (
                    "INSERT INTO loom.scope_generations (org, scope) VALUES ($1, 'a'), ($2, 'b')",
                    ("held-org", "unheld-org"),
                ),
                (
                    "UPDATE loom.scope_generations SET org = $1 WHERE org = 'held-org'",
                    ("unheld-org",),
                ),
                (
                    "INSERT INTO loom.scope_generations (org, scope) VALUES ($1, 'new') "
                    "ON CONFLICT (org, scope) DO UPDATE SET generation = 2",
                    ("unheld-org",),
                ),
            ]
            await plain.execute(_INSERT, "held-org", "x")
            for sql, args in refused:
                with pytest.raises(asyncpg.PostgresError) as caught:
                    await plain.execute(sql, *args)
                assert caught.value.sqlstate == "LT001", sql
            passed = [
                (_INSERT, ("held-org", "y")),
                (_INSERT, ("", "sentinel")),
                (_INSERT, ("-", "sentinel")),
                (_INSERT, ("*", "sentinel")),
                ("UPDATE loom.scope_generations SET generation = 9 WHERE org = 'stray-org'", ()),
                (
                    "INSERT INTO loom.scope_generations (org, scope) VALUES ('stray-org', 'kept') "
                    "ON CONFLICT (org, scope) DO UPDATE SET generation = 3",
                    (),
                ),
            ]
            for sql, args in passed:
                await plain.execute(sql, *args)
            # Skipped where replication would apply the row (plain ENABLE triggers).
            await plain.execute("SET session_replication_role = replica")
            await plain.execute(_INSERT, "unheld-org", "replica")
        finally:
            await plain.close()


@pytest.mark.asyncio
async def test_a_marked_session_writes_every_org_and_keeps_its_marker_through_release() -> None:
    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        await conn.execute(_INSERT, "unheld-org", "marked")
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=1, server_settings={"loom.tenancy_checked": "on"}
        )
        try:
            async with pool.acquire() as c:
                await c.execute(_INSERT, "other-unheld", "a")
            async with pool.acquire() as c:  # the same connection, after RESET ALL
                assert (
                    await c.fetchval("SELECT current_setting('loom.tenancy_checked', true)") == "on"
                )
                await c.execute(_INSERT, "other-unheld", "b")
        finally:
            await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["recording", "partial"])
async def test_ensure_creates_no_org_trigger_in_recording_or_partial(state: str) -> None:
    import asyncpg

    async with _cell(state) as (dsn, conn):
        await cell_triggers.ensure(conn)
        assert await _cell_triggers(conn) == set()
        # The function itself follows the state rule: recording passes, partial refuses.
        await _create_by_hand(conn)
        plain = await _unmarked(dsn)
        try:
            if state == "recording":
                await plain.execute(_INSERT, "unheld-org", "s")
            else:
                with pytest.raises(asyncpg.PostgresError) as caught:
                    await plain.execute(_INSERT, "unheld-org", "s")
                assert caught.value.sqlstate == "LT001"
        finally:
            await plain.close()


@pytest.mark.asyncio
async def test_a_later_cell_whose_mode_row_reads_recording_counts_as_enforced() -> None:
    async with _cell("later-recording") as (_dsn, conn):
        assert await cell.read_state(conn) == "enforced"
        await cell_triggers.ensure(conn)
        assert ("scope_generations", "tenancy_insert", "O") in await _cell_triggers(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["enforced", "recording", "partial"])
async def test_ensure_repairs_triggers_left_in_another_state(state: str) -> None:
    async with _cell(state) as (_dsn, conn):
        if state == "enforced":
            await cell_triggers.ensure(conn)
        await conn.execute("ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable")
        await conn.execute("ALTER TABLE loom.cell_first ENABLE TRIGGER cell_first_no_truncate")
        if state == "enforced":
            await conn.execute("ALTER TABLE loom.scope_generations DISABLE TRIGGER tenancy_insert")
            await conn.execute(
                "ALTER TABLE loom.scope_generations ENABLE ALWAYS TRIGGER tenancy_update"
            )
            await conn.execute(
                "ALTER TABLE loom.memory_current ENABLE REPLICA TRIGGER tenancy_insert"
            )
            kinds = {d.kind for d in await cell_triggers.missing(conn)}
            assert {"enabled", "cell_first"} <= kinds
        await cell_triggers.ensure(conn)
        first = await conn.fetch(
            "SELECT tgenabled::text AS e FROM pg_trigger WHERE tgrelid = 'loom.cell_first'::regclass "
            "AND NOT tgisinternal"
        )
        assert [r["e"] for r in first] == ["A", "A"]
        if state == "enforced":
            assert all(e == "O" for _t, _n, e in await _cell_triggers(conn))


@pytest.mark.asyncio
async def test_ensure_moves_past_a_table_whose_lock_it_cannot_take(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        holder = await asyncpg.connect(dsn)
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute("LOCK TABLE loom.scope_generations IN ROW EXCLUSIVE MODE")
            # A literal in the holder's last statement stands for memory content.
            await holder.execute(
                "SELECT 'secret-memory-literal' FROM loom.scope_generations LIMIT 1"
            )
            with caplog.at_level("WARNING", logger="server.core.cell_triggers"):
                await cell_triggers.ensure(conn)  # logs the holder and goes on
            named = [
                r.getMessage() for r in caplog.records if "scope_generations" in r.getMessage()
            ]
            assert named and f"pid {holder.get_server_pid()}" in named[0]
            assert not any("secret-memory-literal" in r.getMessage() for r in caplog.records)
            got = await _cell_triggers(conn)
            assert not any(t == "scope_generations" for t, _n, _e in got)
            assert any(t == "memory_current" for t, _n, _e in got)
            await tx.rollback()
            await cell_triggers.ensure(conn)  # the next boot finishes the table
            assert ("scope_generations", "tenancy_insert", "O") in await _cell_triggers(conn)
        finally:
            await holder.close()


@pytest.mark.asyncio
async def test_missing_names_an_absent_trigger_and_a_wrong_function() -> None:
    async with _cell("enforced") as (_dsn, conn):
        kinds = [d.kind for d in await cell_triggers.missing(conn)]
        assert "absent" in kinds  # before the cell's first migrating boot
        await cell_triggers.ensure(conn)
        # An overload is another function: it neither raises a defect nor hides one.
        await conn.execute(
            "CREATE FUNCTION loom.tenancy_refuse_unheld(x text) RETURNS void "
            "LANGUAGE sql SECURITY DEFINER AS $$ SELECT $$"
        )
        assert await cell_triggers.missing(conn) == []
        # A body replaced by hand turns every trigger into a pass-through: named.
        await conn.execute(
            "CREATE OR REPLACE FUNCTION loom.tenancy_refuse_unheld() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
        )
        texts = {d.text for d in await cell_triggers.missing(conn)}
        assert "loom.tenancy_refuse_unheld() has a body or settings no migration writes" in texts
        await conn.execute((pg_migrate.PG_MIGRATIONS_DIR / "011_tenancy_triggers.sql").read_text())
        assert await cell_triggers.missing(conn) == []
        await conn.execute("ALTER FUNCTION loom.tenancy_refuse_unheld() SECURITY DEFINER")
        texts = [d.text for d in await cell_triggers.missing(conn)]
        assert any("SECURITY DEFINER" in t for t in texts)
        # A trigger of the right name, shape and function that guards nothing is named.
        await conn.execute("DROP TRIGGER tenancy_insert ON loom.scope_generations")
        await conn.execute(
            "CREATE TRIGGER tenancy_insert AFTER INSERT ON loom.scope_generations FOR EACH ROW "
            "WHEN (false) EXECUTE FUNCTION loom.tenancy_refuse_unheld()"
        )
        defects = await cell_triggers.missing(conn)
        named = "loom.scope_generations: tenancy_insert has another definition (its WHEN, columns or events)"
        assert ("definition", named) in {(d.kind, d.text) for d in defects}
        assert not any(d.kind == "definition" and "memory_current" in d.text for d in defects)
        # An inverted guard and a narrowed one are named too: the WHEN is compared whole.
        for when in (
            "current_setting('loom.tenancy_checked', true) IS NOT DISTINCT FROM 'on'",
            "current_setting('loom.tenancy_checked', true) IS DISTINCT FROM 'on' AND NEW.org = 'x'",
        ):
            await conn.execute("DROP TRIGGER tenancy_insert ON loom.scope_generations")
            await conn.execute(
                "CREATE TRIGGER tenancy_insert AFTER INSERT ON loom.scope_generations FOR EACH ROW "
                f"WHEN ({when}) EXECUTE FUNCTION loom.tenancy_refuse_unheld()"
            )
            assert named in {d.text for d in await cell_triggers.missing(conn)}, when
        # An update trigger narrowed to a column list never fires when org changes.
        await conn.execute("DROP TRIGGER tenancy_update ON loom.scope_generations")
        await conn.execute(
            "CREATE TRIGGER tenancy_update AFTER UPDATE OF generation ON loom.scope_generations "
            "FOR EACH ROW WHEN (current_setting('loom.tenancy_checked', true) IS DISTINCT FROM "
            "'on' AND OLD.org IS DISTINCT FROM NEW.org) EXECUTE FUNCTION loom.tenancy_refuse_unheld()"
        )
        assert named.replace("tenancy_insert", "tenancy_update") in {
            d.text for d in await cell_triggers.missing(conn)
        }


@pytest.mark.asyncio
async def test_a_migrating_boot_creates_the_triggers_and_its_files_pass_them(
    tmp_path: Path,
) -> None:
    """migrate itself, on an unmarked session of its own: it marks the session, its
    files write past the triggers, and it leaves both triggers at origin."""
    import asyncpg

    async with _cell("enforced") as (dsn, conn):

        @contextlib.asynccontextmanager
        async def _connect() -> AsyncIterator[Any]:
            session = await asyncpg.connect(dsn)
            try:
                yield session
            finally:
                await session.close()

        await pg_migrate.migrate(connect=_connect)
        guarded = {(t[1], t[2]) for t in await _cell_triggers(conn) if t[0] == "scope_generations"}
        assert guarded == {("tenancy_insert", "O"), ("tenancy_update", "O")}
        assert ("tenancy", "tenancy_record", "O") in await _cell_triggers(conn)
        for f in pg_migrate.migration_files(pg_migrate.PG_MIGRATIONS_DIR):
            shutil.copy(f, tmp_path / f.name)
        (tmp_path / "999_stray_org.sql").write_text(
            "INSERT INTO loom.scope_generations (org, scope, generation) "
            "VALUES ('unheld-org', 's', 1);\n"
        )
        assert await pg_migrate.migrate(tmp_path, connect=_connect) == ["999_stray_org.sql"]
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM loom.scope_generations WHERE org = 'unheld-org'"
            )
            == 1
        )
        assert await cell_triggers.missing(conn) == []


@pytest.mark.asyncio
async def test_a_partitioned_org_table_is_named_unsupported_and_its_partitions_untouched() -> None:
    async with _cell("enforced") as (_dsn, conn):
        await conn.execute("CREATE TABLE loom.parted (org text NOT NULL) PARTITION BY LIST (org)")
        await conn.execute(
            "CREATE TABLE loom.parted_a PARTITION OF loom.parted FOR VALUES IN ('a')"
        )
        await cell_triggers.ensure(conn)
        assert not {t for t in await _cell_triggers(conn) if t[0] in ("parted", "parted_a")}
        defects = {(d.kind, d.text) for d in await cell_triggers.missing(conn)}
        assert ("partitioned", "loom.parted is partitioned: unsupported") in defects
        assert not any("parted_a" in text for _kind, text in defects)


@pytest.mark.asyncio
async def test_the_refusal_reaches_callers_as_tenancy_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from server.core.errors import TenancyRefusedError
    from server.settings import settings

    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        saved = (settings.pg_dsn, settings.cell)
        await pg.close_pool()
        settings.pg_dsn, settings.cell = dsn, ""  # unmarked: LOOM_CELL unset
        try:
            with (
                caplog.at_level("WARNING", logger="server.core.pg"),
                pytest.raises(TenancyRefusedError) as caught,
            ):
                await pg.execute(_INSERT, "unheld-org", "pool")
            assert "unheld-org" not in str(caught.value)
            logged = " ".join(r.getMessage() for r in caplog.records)
            assert "loom.scope_generations" in logged and "unheld-org" in logged
            with pytest.raises(TenancyRefusedError):
                async with pg.transaction() as tx_conn:
                    await tx_conn.execute(_INSERT, "unheld-org", "transaction")
            with pytest.raises(TenancyRefusedError):
                async with pg.dedicated() as d:
                    await d.execute(_INSERT, "unheld-org", "dedicated")
            with pytest.raises(TenancyRefusedError):  # the guard, through the pool
                await pg.execute(_HOLD, "script-org")
        finally:
            await pg.close_pool()
            settings.pg_dsn, settings.cell = saved


@pytest.mark.asyncio
async def test_the_guarded_tables_are_erasures_org_tables() -> None:
    """Every org-keyed table erasure knows of is guarded, but the three exclusions: the
    expected set is read apart from the triggers' own catalog query."""
    async with _cell("open", held=()) as (_dsn, conn):
        from server.erasure.store.steps_pg import PG_CENSUS, PG_NO_MEMORY_CONTENT

        with_org = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'loom' AND column_name = 'org'"
            )
        }
        classified = {
            t.removeprefix("pg:").removeprefix("loom.") for t in (*PG_CENSUS, *PG_NO_MEMORY_CONTENT)
        }
        guarded = {
            r["relname"] for r in cell_triggers._guarded(await conn.fetch(cell_triggers.ORG_TABLES))
        }
        assert guarded == (classified & with_org) - cell_triggers.EXCLUDED
        # The memory and knowledge heads above all.
        assert {"memory_current", "memory_revisions", "knowledge_heads"} <= guarded


def _page_drop_step() -> tuple[str, str, str]:
    """The troubleshooting page's drop step as printed: what runs before the generating
    query, the query (psql's `\\gexec` runs each row it returns), and what follows."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs/operations/troubleshooting.md").read_text()
    section = text[text.index("### A process with LOOM_CELL unset is gated") :]
    block = re.findall(r"```sql\n(.*?)```", section, re.S)[2]
    lines = [
        ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("\\set")
    ]
    query_start = next(i for i, ln in enumerate(lines) if ln.startswith("SELECT format("))
    query_end = next(i for i, ln in enumerate(lines) if ln.endswith("\\gexec"))
    before = "\n".join(lines[:query_start])
    query = " ".join(lines[query_start : query_end + 1]).removesuffix("\\gexec")
    after = "\n".join(lines[query_end + 1 :])
    return before, query, after


@pytest.mark.asyncio
async def test_the_pages_drop_step_removes_the_cell_triggers_for_good() -> None:
    import asyncpg

    before, query, after = _page_drop_step()
    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        assert await _cell_triggers(conn)
        # The state transaction first (the gate's own test runs it), then the drop step.
        for table in ("cell_first", "tenancy_mode", "cell_self", "tenancy"):
            if table == "cell_first":
                await conn.execute(
                    "ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable"
                )
            await conn.execute(f"DELETE FROM loom.{table}")
        await conn.execute("ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable")
        holder = await asyncpg.connect(dsn)
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", pg_migrate._LOCK_KEY)
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await conn.execute(before)
            await conn.execute("ROLLBACK")
            await holder.execute("SELECT pg_advisory_unlock($1)", pg_migrate._LOCK_KEY)
        finally:
            await holder.close()
        await conn.execute(before)
        for row in await conn.fetch(query):
            await conn.execute(row[0])
        await conn.execute(after)
        assert await _cell_triggers(conn) == set()
        await cell_triggers.ensure(conn)  # the next migrating boot: `open`, none again
        assert await _cell_triggers(conn) == set()


@pytest.mark.asyncio
async def test_doctors_sessions_read_the_marker_as_the_pool_sends_it() -> None:
    from server.cli import doctor_cell
    from server.settings import settings

    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        saved = (settings.pg_dsn, settings.cell, pg._expected)
        settings.pg_dsn, settings.cell, pg._expected = dsn, "eu-1", None
        try:
            state, marked, defects = await doctor_cell._marked_session()
            assert (state, marked) == ("enforced", True) and defects == []
            assert await doctor_cell._unmarked_session() is False
            # A database setting marks every session: doctor names it.
            database = await conn.fetchval("SELECT current_database()")
            await conn.execute(f'ALTER DATABASE "{database}" SET loom.tenancy_checked = on')
            assert await doctor_cell._unmarked_session() is True
            # So does one that stops origin triggers firing for every session.
            await conn.execute(f'ALTER DATABASE "{database}" RESET loom.tenancy_checked')
            assert await doctor_cell._unmarked_session() is False
            await conn.execute(f'ALTER DATABASE "{database}" SET session_replication_role = local')
            assert await doctor_cell._unmarked_session() is False  # local fires them
            await conn.execute(
                f'ALTER DATABASE "{database}" SET session_replication_role = replica'
            )
            assert await doctor_cell._unmarked_session() is True
        finally:
            settings.pg_dsn, settings.cell, pg._expected = saved


@pytest.mark.asyncio
async def test_a_blocked_boot_names_the_request_queued_ahead_of_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reader holds the table, an ALTER waits behind it, and ensure waits behind the
    ALTER: the log names the queued request, which points at the reader."""
    import asyncio

    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        reader, alter = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
        try:
            tx = reader.transaction()
            await tx.start()
            await reader.execute("SELECT 1 FROM loom.scope_generations LIMIT 1")
            queued = asyncio.ensure_future(
                alter.execute("ALTER TABLE loom.scope_generations ADD COLUMN zz_probe int")
            )
            for _ in range(100):
                waiting = await conn.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE pid = $1 AND NOT granted",
                    alter.get_server_pid(),
                )
                if waiting:
                    break
                await asyncio.sleep(0.05)
            with caplog.at_level("WARNING", logger="server.core.cell_triggers"):
                await cell_triggers.ensure(conn)
            named = [
                r.getMessage() for r in caplog.records if "scope_generations" in r.getMessage()
            ]
            assert named and f"pid {alter.get_server_pid()} (waiting" in named[0]
            await tx.rollback()
            await queued
        finally:
            await reader.close()
            await alter.close()


@pytest.mark.asyncio
async def test_ensure_takes_a_lock_freed_between_its_tries() -> None:
    """A table busy for the first try and free by the next is guarded at this boot."""
    import asyncio

    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        holder = await asyncpg.connect(dsn)
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute("LOCK TABLE loom.scope_generations IN ROW EXCLUSIVE MODE")

            async def _free_soon() -> None:
                await asyncio.sleep(cell_triggers.LOCK_RETRY_S / 2)
                await tx.rollback()

            freeing = asyncio.ensure_future(_free_soon())
            await cell_triggers.ensure(conn)
            await freeing
            assert ("scope_generations", "tenancy_insert", "O") in await _cell_triggers(conn)
        finally:
            await holder.close()


@pytest.mark.asyncio
async def test_an_unmarked_session_is_refused_a_tenancy_row_on_an_enforced_cell() -> None:
    import asyncpg

    async with _cell("enforced") as (dsn, conn):
        await cell_triggers.ensure(conn)
        plain = await _unmarked(dsn)
        try:
            for sql, args in (
                (_HOLD, ("script-org",)),
                ("UPDATE loom.tenancy SET org = $1 WHERE org = 'held-org'", ("renamed-org",)),
            ):
                with pytest.raises(asyncpg.PostgresError) as caught:
                    await plain.execute(sql, *args)
                assert caught.value.sqlstate == "LT001", sql
            # An update that keeps the org, an upsert that hits a row, and a delete pass.
            await plain.execute("UPDATE loom.tenancy SET org = org WHERE org = 'held-org'")
            await plain.execute(
                "INSERT INTO loom.tenancy (org) VALUES ('held-org') "
                "ON CONFLICT (org) DO UPDATE SET org = EXCLUDED.org"
            )
            await conn.execute(_HOLD, "gone-org")
            await plain.execute("DELETE FROM loom.tenancy WHERE org = 'gone-org'")
        finally:
            await plain.close()
        assert await conn.fetchval("SELECT array_agg(org) FROM loom.tenancy") == ["held-org"]
        await conn.execute(_HOLD, "checked-org")  # a marked session writes it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["recording", "later-recording", "partial", "recording-without-own", "open"]
)
async def test_the_guard_passes_only_a_first_cell_still_recording(state: str) -> None:
    import asyncpg

    async with _cell(state.removesuffix("-without-own"), held=()) as (dsn, conn):
        if state == "recording-without-own":
            await conn.execute("DELETE FROM loom.cell_self")  # partial: no own row
        await cell_triggers.ensure(conn)
        await conn.execute("DROP TRIGGER IF EXISTS tenancy_record ON loom.tenancy")
        await conn.execute(cell_triggers._create("loom.tenancy", cell_triggers.TENANCY_TRIGGER))
        plain = await _unmarked(dsn)
        try:
            if state == "recording":
                await plain.execute(_HOLD, "recorded-org")
                await plain.execute("UPDATE loom.tenancy SET org = 'moved-org'")
            else:  # `open` counts the row: a tenancy row alone reads `partial`
                with pytest.raises(asyncpg.PostgresError) as caught:
                    await plain.execute(_HOLD, "script-org")
                assert caught.value.sqlstate == "LT001"
        finally:
            await plain.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["recording", "enforced"])
async def test_a_dropped_guard_function_is_named_in_any_state_and_fails_an_enforced_boot(
    state: str,
) -> None:
    """As a dropped org function: `missing` names it in any state, `ensure` on an `enforced`
    cell cannot create the guard and fails the boot, and rerunning migration 012 lets the
    next boot create it."""
    import asyncpg

    async with _cell(state) as (_dsn, conn):
        await conn.execute(f"DROP FUNCTION {cell_triggers.TENANCY_FUNCTION}()")
        if state == "enforced":
            with pytest.raises(asyncpg.exceptions.UndefinedFunctionError):
                await cell_triggers.ensure(conn)
        defects = {(d.kind, d.text) for d in await cell_triggers.missing(conn)}
        assert ("function", "loom.tenancy_refuse_unmarked() is absent") in defects
        if state == "enforced":  # the failed boot created no guard
            assert ("absent", "loom.tenancy: no tenancy_record trigger") in defects
        await conn.execute((pg_migrate.PG_MIGRATIONS_DIR / "012_tenancy_guard.sql").read_text())
        await cell_triggers.ensure(conn)
        if state == "enforced":
            assert ("tenancy", "tenancy_record", "O") in await _cell_triggers(conn)
            assert await cell_triggers.missing(conn) == []


@pytest.mark.asyncio
async def test_missing_names_a_guard_disabled_narrowed_or_calling_another_function() -> None:
    async with _cell("enforced") as (_dsn, conn):
        await cell_triggers.ensure(conn)
        await conn.execute("ALTER TABLE loom.tenancy DISABLE TRIGGER tenancy_record")
        assert ("enabled", "loom.tenancy: tenancy_record is 'D', not 'O'") in {
            (d.kind, d.text) for d in await cell_triggers.missing(conn)
        }
        await cell_triggers.ensure(conn)  # a boot enables it again
        assert await cell_triggers.missing(conn) == []
        for event, function, kind in (
            ("INSERT", cell_triggers.TENANCY_FUNCTION, "shape"),  # no UPDATE OF org
            ("INSERT OR UPDATE OF org", cell_triggers.FUNCTION, "function"),
        ):
            await conn.execute("DROP TRIGGER tenancy_record ON loom.tenancy")
            await conn.execute(
                f"CREATE TRIGGER tenancy_record AFTER {event} ON loom.tenancy FOR EACH ROW "
                "WHEN (current_setting('loom.tenancy_checked', true) IS DISTINCT FROM 'on') "
                f"EXECUTE FUNCTION {function}()"
            )
            kinds = {d.kind for d in await cell_triggers.missing(conn)}
            assert {kind, "definition"} <= kinds, event
        await conn.execute(
            f"CREATE OR REPLACE FUNCTION {cell_triggers.TENANCY_FUNCTION}() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
        )
        texts = {d.text for d in await cell_triggers.missing(conn)}
        assert "loom.tenancy_refuse_unmarked() has a body or settings no migration writes" in texts


@pytest.mark.asyncio
async def test_ensure_table_creates_a_recording_first_cells_triggers_only_when_asked() -> None:
    """Attach creates a first cell's triggers in `recording`, one table at a time; a boot's
    `ensure` never does, and the function passes every row there."""
    async with _cell("recording") as (dsn, conn):
        with pytest.raises(RuntimeError, match="recording"):
            await cell_triggers.ensure_table(conn, "scope_generations")
        assert await cell_triggers.ensure_table(conn, "scope_generations", recording_first=True)
        assert await cell_triggers.ensure_table(conn, "tenancy", recording_first=True)
        assert await _cell_triggers(conn) == {
            ("scope_generations", "tenancy_insert", "O"),
            ("scope_generations", "tenancy_update", "O"),
            ("tenancy", "tenancy_record", "O"),
        }
        # Again: nothing to do.
        assert await cell_triggers.ensure_table(conn, "scope_generations", recording_first=True)
        with pytest.raises(ValueError, match="no cell triggers"):
            await cell_triggers.ensure_table(conn, "idempotency", recording_first=True)
        plain = await _unmarked(dsn)
        try:
            await plain.execute(_INSERT, "unheld-org", "s")  # recording passes every row
            await plain.execute(_HOLD, "new-org")  # a first cell still recording records
        finally:
            await plain.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("loom_cell", ["eu-1", ""])
async def test_the_cell_refresh_reads_the_marker_its_pool_sends(loom_cell: str) -> None:
    """The pool sends the marker as a startup value with LOOM_CELL set: the refresh reads it
    after RESET ALL; without LOOM_CELL it reads false, never null."""
    from server.settings import settings

    async with _cell("recording") as (dsn, _conn):
        saved = (settings.pg_dsn, settings.cell, pg._expected, cell._view)
        settings.pg_dsn, settings.cell, pg._expected = dsn, loom_cell, None
        try:
            assert await cell.refresh() is True
            assert cell.marked() is bool(loom_cell)
            assert cell.reported_mode() == "recording"
        finally:
            await pg.close_pool()
            settings.pg_dsn, settings.cell, pg._expected, cell._view = saved


@pytest.mark.asyncio
async def test_a_failed_refresh_returns_its_connection_idle_with_no_loop_error() -> None:
    """A read that fails inside the refresh's transaction is rolled back before the pool
    takes the connection back, so the release logs nothing through the loop's handler."""
    import asyncio

    from server.settings import settings

    async with _cell("recording") as (dsn, _conn):
        saved = (settings.pg_dsn, settings.cell, pg._expected)
        settings.pg_dsn, settings.cell, pg._expected = dsn, "eu-1", None
        loop = asyncio.get_running_loop()
        errors: list[dict[str, Any]] = []
        handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            with pytest.raises(pg.PostgresRefusedError):
                await pg.fetchrow_reset("SELECT 1 / 0")
            await asyncio.sleep(0.1)  # a release callback would have run by now
            assert errors == []
            # The pool hands the connection out again, outside any transaction.
            async with pg.connection() as conn:
                assert not conn.is_in_transaction()
                assert await conn.fetchval("SELECT 1") == 1
        finally:
            loop.set_exception_handler(handler)
            await pg.close_pool()
            settings.pg_dsn, settings.cell, pg._expected = saved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marked,leaked,expected",
    [(True, False, True), (False, True, False), (False, False, False)],
    ids=["startup-marker", "leaked-set", "no-marker"],
)
async def test_the_refresh_reads_marked_from_the_session_it_runs_on(
    monkeypatch: pytest.MonkeyPatch, marked: bool, leaked: bool, expected: bool
) -> None:
    """`cell.refresh` on one real session: a startup marker reads true; a session `SET` of
    it, as another client may leave on a pooled server connection, is dropped by the RESET
    ALL in the read's transaction and reads false, as no marker does."""
    from server.settings import settings

    async with _cell("recording") as (dsn, _conn):
        monkeypatch.setattr(settings, "pg_dsn", dsn)
        monkeypatch.setattr(settings, "cell", "eu-1")  # the startup marker goes with it
        monkeypatch.setattr(pg, "_expected", None)
        monkeypatch.setattr(cell, "_view", cell._View())
        async with pg.dedicated(marked=marked) as session:
            if leaked:
                await session.execute("SET loom.tenancy_checked = 'on'")
                assert await session.fetchval("SHOW loom.tenancy_checked") == "on"

            @contextlib.asynccontextmanager
            async def _connection() -> AsyncIterator[Any]:
                yield session

            monkeypatch.setattr(pg, "connection", _connection)
            assert await cell.refresh() is True
            assert cell.marked() is expected
            assert cell.reported_mode() == "recording"
