"""An org's own Postgres reads the SQL surface through pg_clickhouse, as
docs/guides/query-from-postgres.md sets it up: the views import as foreign
tables, a join with an application table returns the tenant's rows and no other
tenant's, a withheld value stays NULL, and a revoked SQL user reads nothing.
Skips without LOOM_PGCH_DSN (a Postgres with the extension, e.g.
dev/ci_start_pg_clickhouse.sh) or LOOM_CH_HOST."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from server.cli.sql_access import _login_check
from server.core.ch import get_client
from server.insights.app import sql_access
from server.insights.domain.sql_surface import VIEWS, Scope
from server.settings import settings
from tests.integration._resources import ch_client_from_env, unique_org

pytestmark = pytest.mark.asyncio

_MEMORY = [
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_type",
    "value",
    "version",
    "sensitivity",
]
# The session settings the guide sets on the reader role: the four a SQL user may change.
_SESSION = "join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0"


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


@pytest.fixture
async def pg() -> AsyncIterator[Any]:
    dsn = os.environ.get("LOOM_PGCH_DSN") or pytest.skip("LOOM_PGCH_DSN is not set")
    conn = await asyncpg.connect(dsn)
    yield conn
    await conn.close()


async def test_an_orgs_postgres_joins_its_tenants_memories(ch: Any, pg: Any) -> None:
    raw = ch_client_from_env()
    org, other = unique_org("pgch"), unique_org("pgchb")
    rows = [
        [org, "ns", "a", "alice", "-", "plan", "fact", "alice is on the team plan", 1, ""],
        [org, "ns", "a", "alice", "-", "pin", "fact", "badge pin 4471", 1, "high"],
        [org, "ns", "a", "bob", "-", "plan", "fact", "bob is on the free plan", 1, ""],
        [other, "ns", "a", "alice", "-", "plan", "fact", "another tenant's alice", 1, ""],
    ]
    raw.insert("loom.memory", rows, column_names=_MEMORY)
    tag = uuid.uuid4().hex[:8]
    user, server, schema = f"it_pgch_{tag}", f"loom_ch_{tag}", f"loom_{tag}"
    reader, reader_password = f"app_reader_{tag}", uuid.uuid4().hex
    granted = await sql_access.grant(user, Scope(org, ("ns",)), login_check=_login_check, ch=ch)
    assert granted.password
    try:
        await pg.execute("CREATE EXTENSION IF NOT EXISTS pg_clickhouse")
        if version := os.environ.get("LOOM_PGCH_VERSION"):
            # The extension carries its major and minor release; the image tag adds the patch.
            installed = await pg.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'pg_clickhouse'"
            )
            assert version.startswith(f"{installed}."), (version, installed)
        await pg.execute(
            f"CREATE SERVER {server} FOREIGN DATA WRAPPER clickhouse_fdw OPTIONS "
            f"(driver 'http', host '{settings.ch_host}', port '{settings.ch_port}', "
            f"dbname '{settings.ch_database}')"
        )
        # The guide's setup, as the owner: a reader role with its own mapping, the
        # session settings on the role, and grants on the imported schema alone.
        await pg.execute(f"CREATE ROLE {reader} LOGIN PASSWORD '{reader_password}'")
        for role in ("CURRENT_USER", reader):
            await pg.execute(
                f"CREATE USER MAPPING FOR {role} SERVER {server} "
                f"OPTIONS (user '{user}', password '{granted.password}')"
            )
        await pg.execute(f"ALTER ROLE {reader} SET pg_clickhouse.session_settings = '{_SESSION}'")
        await pg.execute(f"CREATE SCHEMA {schema}")
        await pg.execute(
            f"IMPORT FOREIGN SCHEMA {settings.ch_database} LIMIT TO ({', '.join(VIEWS)}) "
            f"FROM SERVER {server} INTO {schema}"
        )
        await pg.execute(f"CREATE TABLE {schema}.accounts (loom_user_id text, email text)")
        await pg.execute(
            f"INSERT INTO {schema}.accounts VALUES ('alice', 'alice@example.com'), "
            "('bob', 'bob@example.com')"
        )
        await pg.execute(f"GRANT USAGE ON SCHEMA {schema} TO {reader}")
        await pg.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader}")
        app = await asyncpg.connect(
            os.environ["LOOM_PGCH_DSN"], user=reader, password=reader_password
        )
        joined = await app.fetch(
            f"SELECT c.email, m.org, m.memory_key, m.value, m.value_withheld, "
            f"m.content_sha256 IS NULL AS hash_withheld "
            f"FROM {schema}.accounts AS c JOIN {schema}.sql_v1_memories AS m "
            f"ON m.user_id = c.loom_user_id ORDER BY c.email, m.memory_key"
        )
        assert [tuple(r) for r in joined] == [
            ("alice@example.com", org, "pin", None, "sensitive", True),
            ("alice@example.com", org, "plan", "alice is on the team plan", "", False),
            ("bob@example.com", org, "plan", "bob is on the free plan", "", False),
        ]
        usage = await app.fetchval(f"SELECT count(*) FROM {schema}.sql_v1_usage_daily")
        assert usage == 0
        # The role's setting reached the session the reader logged in with.
        assert await app.fetchval("SHOW pg_clickhouse.session_settings") == _SESSION

        # Any other setting is refused by the SQL user's profile, whatever Postgres sends.
        await app.execute("SET pg_clickhouse.session_settings = 'max_threads 2'")
        with pytest.raises(Exception, match="READONLY|readonly mode"):
            await app.fetchval(f"SELECT count(*) FROM {schema}.sql_v1_memories")
        await app.execute("RESET pg_clickhouse.session_settings")

        await sql_access.revoke(user, ch=ch)
        with pytest.raises(Exception, match="(?i)authentication|password"):
            await app.fetch(f"SELECT count(*) FROM {schema}.sql_v1_memories")
        await app.close()
    finally:
        await sql_access.revoke(user, ch=ch)
        await pg.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await pg.execute(f"DROP SERVER IF EXISTS {server} CASCADE")
        await pg.execute(f"DROP ROLE IF EXISTS {reader}")
        raw.close()
