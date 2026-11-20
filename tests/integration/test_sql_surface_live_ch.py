"""The SQL surface on a live ClickHouse: a SQL user that `loom sql-access`
made reads its own scope through the sql_v1_* views and nothing else (no
other tenant's rows, no base table, no scope table), never a `high` or
redacted value, a rejected or deleted memory or a deleted turn, and an
erased user's rows leave every view with the tables erasure deletes from.
The views are read in-process; the erasure runs on the server. Skips without
LOOM_CH_HOST."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from loom import Agent

from server.cli.sql_access import _login_check
from server.core.ch import get_client
from server.insights.app import sql_access
from server.insights.domain.sql_surface import VIEWS, Scope
from tests.integration._resources import ch_client_from_env, unique_org

pytestmark = pytest.mark.asyncio

_MEMORY = ["org", "namespace", "agent", "user_id", "session_id", "memory_key", "memory_type"]
_MEMORY += ["value", "version", "sensitivity", "status", "tombstoned_at", "redacted_at"]
_TURN = ["org", "namespace", "agent", "thread_id", "msg_id", "role", "content", "user_id"]
_TURN += ["tombstoned_at"]
_EVENT = ["org", "namespace", "agent", "user_id", "session_id", "operation", "request_id"]
_EVENT += ["latency_ms", "success"]
_T = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


def _seed(raw: Any, org: str, ns: str) -> None:
    def memory(key: str, value: str, version: int = 1, **kw: Any) -> list[Any]:
        row = {"sensitivity": "", "status": "active", "tombstoned_at": None, "redacted_at": None}
        row.update(kw)
        return [org, ns, "a", "alice", "-", key, "fact", value, version, *row.values()]

    raw.insert(
        "loom.memory",
        [
            memory("plain", f"{org} plain"),
            memory("secret", "sk-old", 1),
            memory("secret", "password=hunter22", 2, sensitivity="high"),
            memory("redacted", "[redacted]", redacted_at=_T),
            memory("rejected", "no", status="rejected"),
            memory("deleted", "gone", tombstoned_at=_T, status="tombstoned"),
        ],
        column_names=_MEMORY,
    )
    raw.insert(
        "loom.conversations",
        [
            [org, ns, "a", "t1", "m1", "user", f"{org} hello", "alice", None],
            [org, ns, "a", "t1", "m2", "user", "deleted turn", "alice", _T],
        ],
        column_names=_TURN,
    )
    raw.insert(
        "loom.agent_events",
        [[org, ns, "a", "alice", "-", "memory.set", str(uuid.uuid4()), 5, True]],
        column_names=_EVENT,
    )


def _as(user: str, password: str) -> Any:
    return ch_client_from_env(username=user, password=password)


def _rows(client: Any, sql: str) -> list[tuple[Any, ...]]:
    return sorted(tuple(r) for r in client.query(sql).result_rows)


async def test_a_sql_user_reads_its_own_scope_and_nothing_else(
    ch: Any, control_plane_url: str, mint_test_token: Any
) -> None:
    raw = ch_client_from_env()
    org_a, org_b = unique_org("sqla"), unique_org("sqlb")
    for org in (org_a, org_b):
        _seed(raw, org, "ns")
    _seed(raw, org_a, "other")
    tag = uuid.uuid4().hex[:8]
    user_a, user_b = f"it_sql_a_{tag}", f"it_sql_b_{tag}"
    user_n, user_all = f"it_sql_n_{tag}", f"it_sql_all_{tag}"
    try:
        a = await sql_access.grant(user_a, Scope(org_a, ("ns",)), login_check=_login_check, ch=ch)
        b = await sql_access.grant(user_b, Scope(org_b, ("*",)), login_check=_login_check, ch=ch)
        assert a.password and b.password
        reader_a, reader_b = _as(user_a, a.password), _as(user_b, b.password)

        memories = "SELECT org, namespace, memory_key, version, value, value_withheld "
        memories += "FROM loom.sql_v1_memories"
        assert _rows(reader_a, memories) == [
            (org_a, "ns", "plain", 1, f"{org_a} plain", ""),
            (org_a, "ns", "redacted", 1, None, "redacted"),
            (org_a, "ns", "secret", 2, None, "sensitive"),
        ]
        # A withheld value's hash is withheld too: a short secret's can be guessed.
        hashes = "SELECT memory_key, content_sha256 IS NULL FROM loom.sql_v1_memories"
        assert _rows(reader_a, hashes) == [("plain", 0), ("redacted", 1), ("secret", 1)]
        assert {r[0] for r in _rows(reader_b, memories)} == {org_b}

        # The version before the label held the same secret unlabelled.
        history = "SELECT memory_key, version, value, content_sha256 "
        history += "FROM loom.sql_v1_memory_history WHERE memory_key = 'secret'"
        assert _rows(reader_a, history) == [("secret", 1, None, None), ("secret", 2, None, None)]
        # A filter on the older version alone still sees the key's later label.
        first = history + " AND version = 1"
        assert _rows(reader_a, first) == [("secret", 1, None, None)]
        withheld = "SELECT memory_key, value, value_withheld, content_sha256 "
        withheld += "FROM loom.sql_v1_memory_history WHERE memory_key IN ('redacted', 'rejected')"
        assert _rows(reader_a, withheld) == [("redacted", None, "redacted", None)]
        deleted = "SELECT count() FROM loom.sql_v1_memory_history WHERE memory_key = 'deleted'"
        assert _rows(reader_a, deleted) == [(1,)]

        turns = "SELECT org, msg_id, content FROM loom.sql_v1_conversations"
        assert _rows(reader_a, turns) == [(org_a, "m1", f"{org_a} hello")]
        events = "SELECT org, namespace, operation FROM loom.sql_v1_agent_events"
        assert _rows(reader_a, events) == [(org_a, "ns", "memory.set")]
        usage = "SELECT org, namespace, operation, ops FROM loom.sql_v1_usage_daily"
        assert _rows(reader_a, usage) == [(org_a, "ns", "memory.set", 1)]

        refused_reads = ("loom.memory", "loom.sql_access", "loom.conversations")
        refused_reads += ("system.query_log", "system.mutations", "system.processes")
        for refused in refused_reads:
            with pytest.raises(Exception, match="ACCESS_DENIED|Not enough privileges"):
                reader_a.query(f"SELECT count() FROM {refused}")
        listed = "SELECT name FROM system.tables WHERE database = currentDatabase()"
        assert {r[0] for r in _rows(reader_a, listed)} == set(VIEWS)
        # A statement changes no setting that could cut a view's filter short.
        for setting in (
            "apply_deleted_mask = 0",
            "use_query_cache = 1",
            "use_query_condition_cache = 1",
            "max_rows_in_set = 1, set_overflow_mode = 'break'",
            "additional_table_filters = {'loom.memory_versions': 'version > 1'}",
            "optimize_move_to_prewhere_if_final = 1",
        ):
            statement = f"SELECT count() FROM loom.sql_v1_memory_history SETTINGS {setting}"
            with pytest.raises(Exception, match="READONLY|readonly mode"):
                reader_a.query(statement)
        # The settings clients send on their own are taken.
        harmless = "SETTINGS final = 1, join_use_nulls = 1, group_by_use_nulls = 1, "
        harmless += "transform_null_in = 1"
        assert _rows(reader_a, f"SELECT count() FROM loom.sql_v1_memories {harmless}") == [(3,)]
        # Loom's own user holds no scope row: the views read nothing for it.
        assert _rows(raw, f"SELECT count() FROM loom.sql_v1_memories WHERE org = '{org_a}'") == [
            (0,)
        ]

        # A caller's filter that raises on a value runs on the view's rows only: one
        # that fails on every other tenant's key never sees them. Tenant A's own keys
        # here are numbers, the other tenant's are not, and they share a part.
        numeric = unique_org("sqln")
        raw.insert(
            "loom.memory",
            [
                [numeric, "ns", "1", "2", "3", "42", "fact", "7", 1, "", "active", None, None],
                [
                    org_b,
                    "ns",
                    "a",
                    "zed",
                    "-",
                    "not-a-number",
                    "fact",
                    "secret",
                    1,
                    "",
                    "active",
                    None,
                    None,
                ],
            ],
            column_names=_MEMORY,
        )
        raw.insert(
            "loom.agent_events",
            [
                [numeric, "ns", "1", "2", "3", "4", "5", 6, True],
                [org_b, "ns", "a", "zed", "-", "memory.get", str(uuid.uuid4()), 1, True],
            ],
            column_names=_EVENT,
        )
        n = await sql_access.grant(user_n, Scope(numeric, ("ns",)), login_check=_login_check, ch=ch)
        assert n.password
        reader_n = _as(user_n, n.password)
        for view, column in (
            ("sql_v1_memories", "memory_key"),
            ("sql_v1_memories", "value"),
            ("sql_v1_memory_history", "memory_key"),
            ("sql_v1_agent_events", "operation"),
            ("sql_v1_usage_daily", "operation"),
        ):
            probe = f"SELECT count() FROM loom.{view} WHERE toUInt64({column}) > 0"
            # The filter ran on tenant N's own row, and on no other tenant's.
            assert _rows(reader_n, probe) == [(1,)], (view, column)
        await sql_access.revoke(user_n, ch=ch)

        # A namespace classified restricted after the grant leaves the views at once.
        raw.insert(
            "loom.namespaces",
            [[org_b, "ns", "restricted"]],
            column_names=["org", "namespace", "classification"],
        )
        assert _rows(reader_b, memories) == []
        with pytest.raises(sql_access.AccessRefused, match="restricted namespaces"):
            await sql_access.grant(user_b, Scope(org_b, ("ns",)), login_check=_login_check, ch=ch)

        # So does one the code registry restricts, under a whole-deployment grant too.
        everyone = await sql_access.grant(user_all, Scope("*", ()), login_check=_login_check, ch=ch)
        assert everyone.password
        reader_all = _as(user_all, everyone.password)
        orgs = f"SELECT DISTINCT org FROM loom.sql_v1_memories WHERE org IN ('{org_a}', '{org_b}')"
        assert {r[0] for r in _rows(reader_all, orgs)} == {org_a}  # org_b/ns is restricted
        raw.insert(
            "loom.code_repos",
            [[org_a, "other", "example/repo", "", "main", "restricted", 1]],
            column_names=[
                "org",
                "namespace",
                "repo",
                "git_remote",
                "git_branch",
                "classification",
                "version",
            ],
        )
        in_other = f"SELECT count() FROM loom.sql_v1_memories WHERE org = '{org_a}' AND namespace = 'other'"
        assert _rows(reader_all, in_other) == [(0,)]
        await sql_access.revoke(user_all, ch=ch)
        raw.command(
            "INSERT INTO loom.code_repos (org, namespace, repo, git_remote, git_branch, "
            "classification, version) VALUES "
            f"('{org_a}', 'other', 'example/repo', '', 'main', 'standard', 2)"
        )

        # A rescope takes the old namespace away (one server here, so at once).
        await sql_access.grant(user_a, Scope(org_a, ("other",)), login_check=_login_check, ch=ch)
        assert {r[1] for r in _rows(reader_a, memories)} == {"other"}

        token = mint_test_token(org=org_a, namespaces=["*"], roles=["org_admin", "admin"])
        async with Agent(
            org=org_a,
            namespace="ns",
            agent="platform-admin",
            base_url=control_plane_url,
            timeout_s=60.0,
            api_key=token,
        ) as admin:
            erased = await admin.erasure.request_user_id("alice", requester="ops@example.com")
        assert erased["status"] == "completed"
        for view in ("memories", "memory_history", "conversations"):
            left = f"SELECT count() FROM loom.sql_v1_{view} WHERE user_id = 'alice'"
            assert _rows(reader_a, left) == [(0,)], view

        await sql_access.revoke(user_a, ch=ch)
        with pytest.raises(Exception, match="AUTHENTICATION_FAILED|Authentication failed"):
            reader_a.query("SELECT 1")
    finally:
        for user in (user_a, user_b, user_n, user_all):
            await sql_access.revoke(user, ch=ch)
        raw.close()


async def test_a_ledger_read_prunes_to_its_scope_and_window(ch: Any) -> None:
    """The ledger views bound their own reads: the window and the scope sit in the
    view's WHERE, so neither another org's rows nor the grantee's rows from before
    the window are read, whatever the caller's statement says."""
    raw = ch_client_from_env()
    mine, other = unique_org("sqlwin"), unique_org("sqlwinb")
    seed = 100_000
    columns = (
        "org, namespace, agent, user_id, session_id, operation, request_id, latency_ms, success, ts"
    )
    old = "now64(3) - INTERVAL 62 DAY"

    def insert(org: str, user: str, ts: str, rows: int) -> None:
        raw.command(
            f"INSERT INTO loom.agent_events ({columns}) SELECT '{org}', 'ns', 'a', '{user}', '-', "
            f"'memory.get', toString(number), 1, true, {ts} FROM numbers({rows})"
        )

    insert(mine, "old-ledger", old, seed)  # before the window, its own month's part
    insert(other, "zed", "now64(3)", seed)  # another org, inside the window
    insert(mine, "recent", "now64(3)", 10)  # the grantee's own rows the window keeps
    user = f"it_sql_win_{uuid.uuid4().hex[:8]}"
    try:
        granted = await sql_access.grant(
            user, Scope(mine, ("ns",)), login_check=_login_check, ch=ch
        )
        assert granted.password
        reader = _as(user, granted.password)
        reads = {}
        for view, counted in (
            ("sql_v1_agent_events", "count()"),
            ("sql_v1_usage_daily", "sum(ops)"),
        ):
            query_id = str(uuid.uuid4())
            got = reader.query(
                f"SELECT {counted} FROM loom.{view}", settings={"query_id": query_id}
            ).result_rows
            assert got == [(10,)], view
            reads[view] = query_id
        raw.command("SYSTEM FLUSH LOGS")

        def read_rows(query_id: str) -> int:
            rows = raw.query(
                "SELECT read_rows FROM system.query_log "
                "WHERE query_id = {q:String} AND type = 'QueryFinish'",
                parameters={"q": query_id},
            ).result_rows
            assert len(rows) == 1, (query_id, rows)
            return int(rows[0][0])

        for view, query_id in reads.items():
            # Below one seed: neither the other org's rows nor the grantee's rows from
            # before the window were read.
            n = read_rows(query_id)
            assert 0 < n < seed, (view, n)
    finally:
        await sql_access.revoke(user, ch=ch)
        raw.close()


async def test_a_name_another_user_holds_is_refused(ch: Any) -> None:
    raw = ch_client_from_env()
    name = f"it_sql_foreign_{uuid.uuid4().hex[:8]}"
    raw.command(f"CREATE USER `{name}` IDENTIFIED WITH no_password")
    try:
        with pytest.raises(sql_access.AccessRefused, match="not a SQL user of the surface"):
            await sql_access.grant(name, Scope("o", ("n",)), login_check=_login_check, ch=ch)
        with pytest.raises(sql_access.AccessRefused, match="not a SQL user of the surface"):
            await sql_access.revoke(name, ch=ch)
    finally:
        raw.command(f"DROP USER IF EXISTS `{name}`")
        raw.close()
