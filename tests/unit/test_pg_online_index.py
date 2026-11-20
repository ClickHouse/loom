"""Only the declared concurrent btree form escapes a migration transaction."""

from __future__ import annotations

import pytest

from server.core.schema import pg_online_index

SQL = """-- loom:online-index loom_router.placements_by_cell_org ON loom_router.placements (cell, org)
CREATE INDEX CONCURRENTLY placements_by_cell_org ON loom_router.placements
USING btree (cell COLLATE "C", org COLLATE "C");
"""


def test_the_online_index_declaration_has_one_exact_target() -> None:
    parsed = pg_online_index.parse(SQL)
    assert parsed is not None
    assert parsed.schema == "loom_router"
    assert parsed.name == "placements_by_cell_org"
    assert parsed.table == "placements"
    assert parsed.columns == ("cell", "org")
    assert pg_online_index.parse("CREATE TABLE loom.t (id int);") is None


@pytest.mark.parametrize(
    "sql",
    [
        SQL + "SELECT 1;",
        SQL.replace("CREATE INDEX", "CREATE UNIQUE INDEX"),
        SQL.replace('org COLLATE "C"', 'org COLLATE "POSIX"'),
        SQL.replace("(cell, org)", "(org, cell)"),
        SQL.replace("ON loom_router.placements\n", "ON loom_router.other\n"),
        SQL.replace("USING btree", "USING hash"),
        SQL.replace('org COLLATE "C"', 'lower(org) COLLATE "C"'),
        SQL.replace(");", ") WHERE true;"),
        SQL.replace("-- loom:online-index", "-- loom:online-index-bad"),
        SQL.replace("placements_by_cell_org ON", "other_index ON", 1),
        SQL.replace("(cell, org)", "(cell, cell)"),
        SQL.replace("CONCURRENTLY", ""),
    ],
)
def test_other_online_sql_is_refused(sql: str) -> None:
    with pytest.raises(ValueError, match="online.index"):
        pg_online_index.parse(sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [True, False])
async def test_ledger_retries_commit_a_wal_fence_even_when_already_recorded(
    tmp_path, pending
) -> None:
    import contextlib

    from server.core import pg
    from server.core.schema import pg_migrate

    path = tmp_path / "001_index.sql"
    path.write_text(SQL)

    class Connection:
        def __init__(self):
            self.statements = []

        def transaction(self):
            return contextlib.nullcontext()

        async def execute(self, sql, *args):
            self.statements.append((sql, args))

    conn = Connection()
    await pg_migrate._record_online(conn, path, "loom_router", pending=pending)
    assert conn.statements[-1] == (pg._DURABLE_MESSAGE, ())
    assert len(conn.statements) == (2 if pending else 1)
    if pending:
        assert conn.statements[0][1] == (path.name, pg_migrate.checksum(path))


@pytest.mark.asyncio
async def test_migrator_preserves_a_borrowed_sessions_statement_budget(tmp_path) -> None:
    import contextlib

    from server.core.schema import pg_migrate

    class Connection:
        def __init__(self):
            self.statements = []

        async def execute(self, sql, *args):
            self.statements.append(sql)

        async def fetch(self, sql):
            return []

        async def fetchval(self, sql, *args):
            self.statements.append(sql)
            return True

    conn = Connection()

    @contextlib.asynccontextmanager
    async def connect():
        yield conn

    assert await pg_migrate.migrate(tmp_path, connect=connect, schema="loom_router") == []
    assert not any("statement_timeout" in sql for sql in conn.statements)
    assert "SET synchronous_commit = 'on'" in conn.statements
    assert conn.statements[-1] == "SELECT pg_advisory_unlock($1)"


@pytest.mark.asyncio
async def test_online_target_must_match_its_ledger_before_connecting(tmp_path) -> None:
    from server.core.schema import pg_migrate

    (tmp_path / "001_index.sql").write_text(SQL)

    def never_connect():
        pytest.fail("invalid target opened a connection")

    with pytest.raises(ValueError, match="ledger schema"):
        await pg_migrate.migrate(tmp_path, schema="loom", connect=never_connect)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["statement", "rollback", "commit"])
async def test_cancellation_closes_before_installed_asyncpg_transaction_unwind(
    tmp_path, phase
) -> None:
    import asyncio
    import contextlib
    from types import SimpleNamespace

    from asyncpg.transaction import Transaction

    from server.core.schema import pg_migrate

    (tmp_path / "001_one.sql").write_text("SELECT stalled_statement;")
    reached = asyncio.Event()
    terminated = asyncio.Event()
    closed = asyncio.Event()

    class Connection:
        _pool_release_ctr = 0
        _top_xact = None
        _protocol = SimpleNamespace(is_in_transaction=lambda: False)

        def is_closed(self):
            return closed.is_set() or terminated.is_set()

        def transaction(self):
            return Transaction(self, None, False, False)

        async def close(self, **kwargs):
            assert kwargs["timeout"] > 0
            assert not terminated.is_set()
            closed.set()

        def terminate(self):
            terminated.set()

        async def fetchval(self, sql, *args):
            return True

        async def fetch(self, sql):
            return []

        async def execute(self, sql, *args):
            if sql == "SELECT stalled_statement;" and phase == "rollback":
                raise ValueError("statement failed before rollback")
            if (
                (sql == "SELECT stalled_statement;" and phase == "statement")
                or (sql.startswith("ROLLBACK") and phase == "rollback")
                or (sql.startswith("COMMIT") and phase == "commit")
            ):
                reached.set()
                # Public close must run independently of transaction unwinding.
                while not closed.is_set():
                    try:
                        await closed.wait()
                    except asyncio.CancelledError:
                        continue

    conn = Connection()

    @contextlib.asynccontextmanager
    async def connect():
        yield conn

    migrating = asyncio.create_task(
        pg_migrate.migrate(tmp_path, connect=connect, schema="loom_router")
    )
    try:
        await asyncio.wait_for(reached.wait(), 1)
        migrating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(migrating, 1)
        assert closed.is_set() and not terminated.is_set()
    finally:
        closed.set()
        if not migrating.done():
            migrating.cancel()
        await asyncio.gather(migrating, return_exceptions=True)


def test_postgres_journals_keep_distinct_numbers_and_declared_online_forms() -> None:
    from server.core.paths import PG_MIGRATIONS_DIR, ROUTER_MIGRATIONS_DIR
    from server.core.schema import pg_migrate
    from server.core.schema.journal import migration_number

    for directory in (PG_MIGRATIONS_DIR, ROUTER_MIGRATIONS_DIR):
        files = pg_migrate.migration_files(directory)
        numbers = [migration_number(path.name) for path in files]
        assert None not in numbers and len(numbers) == len(set(numbers))
        for path in files:
            sql = path.read_text()
            parsed = pg_online_index.parse(sql)
            if "CONCURRENTLY" in sql:
                assert parsed is not None, f"{path.name} lacks an online-index declaration"
