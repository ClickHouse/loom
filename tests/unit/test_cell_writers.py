"""`server/cli/cell_writers.py`: reading grants (attach's own from `SHOW GRANTS FINAL`,
every user's from `system.grants` and `system.role_grants` per host), the writer predicate,
the objects, user directories and extra replicas it refuses, and attach's own grants. A
cluster of several hosts cannot run in CI, so its per-host reads are pinned here on a fake
ClickHouse. CH-free."""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.cli import cell_scan, cell_writers
from server.cli.cell_writers import Allowed, writer_reason
from server.core import cell_ch
from server.core.cell_ch import Topology
from server.insights.store import cell_catalog
from server.insights.store.cell_catalog import Grant, holds, parse_show_grants
from tests.unit.test_cell_scan import NAMES, ONE, TWO, FakeCH

DBS = ["loom", "loom_telemetry"]


def test_show_grants_lines_become_grants() -> None:
    grants = parse_show_grants(
        [
            "GRANT SELECT(id, org), INSERT ON loom.memory TO u",
            "GRANT SHOW, SELECT ON *.* TO u WITH GRANT OPTION",
            "GRANT TABLE ENGINE ON * TO u",
            "GRANT SELECT ON `odd.db`.* TO u",
            "GRANT REMOTE ON *.* TO u",
            "GRANT READ ON REMOTE TO u",
            "GRANT writers TO u",
            "REVOKE INSERT ON loom.secret FROM u",
        ]
    )
    assert grants == [
        Grant("SELECT", "loom", "memory"),
        Grant("INSERT", "loom", "memory"),
        Grant("SHOW", None, None, True),
        Grant("SELECT", None, None, True),
        Grant("TABLE ENGINE"),
        Grant("SELECT", "odd.db"),
        Grant("REMOTE"),
        Grant("REMOTE"),
        Grant("INSERT", "loom", "secret", revoked=True),
    ]


def test_a_privilege_is_held_through_any_grant_implying_it() -> None:
    assert holds([Grant("SOURCES")], "REMOTE")
    assert holds([Grant("ALL")], "SYSTEM SYNC REPLICA", "loom")
    assert holds([Grant("SHOW ACCESS")], "SHOW USERS")
    assert holds([Grant("SHOW ACCESS")], "SHOW ROLES")
    assert holds([Grant("ACCESS MANAGEMENT")], "SHOW ROLES")
    assert not holds([Grant("SHOW USERS")], "SHOW ROLES")
    assert holds([Grant("SYSTEM FLUSH")], "SYSTEM FLUSH ASYNC INSERT QUEUE")
    assert holds([Grant("SELECT", "loom")], "SELECT", "loom")
    assert holds([Grant("SELECT", "lo*")], "SELECT", "loom")
    # A grant on one table, or on another database, is not a grant on the database.
    assert not holds([Grant("SELECT", "loom", "memory")], "SELECT", "loom")
    assert not holds([Grant("SELECT", "other")], "SELECT", "loom")
    assert not holds([Grant("SELECT", "loom")], "SELECT")


def test_a_partial_revoke_narrowing_attachs_own_reads_refuses() -> None:
    lines = [
        "GRANT SELECT, SHOW, SYSTEM ON *.* TO attach",
        "GRANT SHOW USERS, SHOW ROLES, SHOW ROW POLICIES ON *.* TO attach",
        "REVOKE SHOW TABLES ON side.* FROM attach",
        "REVOKE SELECT ON loom.secret FROM attach",
        "REVOKE GRANT OPTION FOR SELECT ON other.* FROM attach",
    ]
    assert cell_catalog.own_problems(parse_show_grants(lines), ONE, DBS) == [
        "SHOW TABLES ON *.* (narrowed by REVOKE SHOW TABLES ON side.*)",
        "SELECT ON loom.* (narrowed by REVOKE SELECT ON loom.secret)",
    ]
    # On several hosts the revokes come as `system.grants` rows.
    rows = [
        {"user_name": "attach", "access_type": a, "database": None}
        for a in (
            "SELECT",
            "SHOW",
            "SHOW USERS",
            "SHOW ROLES",
            "SHOW ROW POLICIES",
            "SYSTEM",
            "REMOTE",
            "CLUSTER",
        )
    ]
    rows.append(
        {"user_name": "attach", "access_type": "SHOW", "database": "side", "is_partial_revoke": 1}
    )
    grants = cell_catalog.resolve_grants("attach", rows, [])
    assert cell_catalog.own_problems(grants, TWO, DBS) == [
        "SHOW TABLES ON *.* (narrowed by REVOKE SHOW ON side.*)",
        "SHOW COLUMNS ON *.* (narrowed by REVOKE SHOW ON side.*)",
    ]
    # A writer's revokes stay ignored: it is judged by what it was granted.
    assert writer_reason(parse_show_grants(["REVOKE INSERT ON loom.* FROM w"]), DBS) == ""


def test_attachs_own_user_needs_every_read_its_checks_make() -> None:
    full = [Grant("SELECT"), Grant("SHOW"), Grant("SHOW ACCESS"), Grant("SYSTEM")]
    assert cell_catalog.own_problems(full, ONE, DBS) == []
    assert cell_catalog.own_problems(full, TWO, DBS) == ["READ ON REMOTE", "CLUSTER ON *.*"]
    narrow = [Grant("SELECT", "loom", "memory"), Grant("SELECT", "system"), Grant("SHOW")]
    assert cell_catalog.own_problems(narrow, Topology("", 0), DBS) == [
        "SHOW USERS ON *.*",
        "SHOW ROLES ON *.*",
        "SHOW ROW POLICIES ON *.*",
        "SYSTEM FLUSH ASYNC INSERT QUEUE ON *.*",
        "SELECT ON loom.*",
        "SELECT ON loom_telemetry.*",
    ]


@pytest.mark.parametrize(
    ("grants", "reason"),
    [
        ([Grant("INSERT", "loom", "memory")], "holds INSERT on loom.memory"),
        ([Grant("ALTER UPDATE", "loom")], "holds ALTER UPDATE on loom.*"),
        ([Grant("CREATE VIEW")], "holds CREATE VIEW on *.*"),
        ([Grant("ALL")], "holds ALL on *.*"),
        ([Grant("ACCESS MANAGEMENT")], "holds ACCESS MANAGEMENT on *.*"),
        ([Grant("SELECT", "loom", None, True)], "holds SELECT WITH GRANT OPTION on loom.*"),
        ([Grant("INSERT", "lo*")], "holds INSERT on lo*.*"),
        ([Grant("SELECT"), Grant("SHOW")], ""),
        ([Grant("INSERT", "elsewhere"), Grant("SELECT", "elsewhere", None, True)], ""),
        # From any database: a Distributed table there forwards inserts without a grant on
        # the destination, and `remote()` writes as another user.
        ([Grant("CREATE TABLE", "elsewhere")], "holds CREATE TABLE on elsewhere.*"),
        ([Grant("CREATE", "elsewhere")], "holds CREATE on elsewhere.*"),
        ([Grant("CREATE VIEW", "elsewhere")], "holds CREATE VIEW on elsewhere.*"),
        ([Grant("ALL", "elsewhere")], "holds ALL on elsewhere.*"),
        ([Grant("REMOTE")], "holds REMOTE on *.*"),
        ([Grant("SOURCES")], "holds SOURCES on *.*"),
        ([Grant("WRITE")], "holds WRITE on *.*"),
        (parse_show_grants(["GRANT READ ON REMOTE TO u"]), "holds REMOTE on *.*"),
        # Acting as another user, or reaching this server again through a source.
        ([Grant("IMPERSONATE")], "holds IMPERSONATE on *.*"),
        ([Grant("SET DEFINER")], "holds SET DEFINER on *.*"),
        ([Grant("ALLOW SQL SECURITY NONE")], "holds ALLOW SQL SECURITY NONE on *.*"),
        ([Grant("URL")], "holds URL on *.*"),
        ([Grant("MYSQL")], "holds MYSQL on *.*"),
        ([Grant("FILE"), Grant("SQLITE"), Grant("CREATE FUNCTION")], ""),
        # A row policy made later hides rows from the next scan or check.
        ([Grant("CREATE ROW POLICY")], "holds CREATE ROW POLICY on *.*"),
        (
            [Grant("ALTER ROW POLICY", "system")],
            "holds ALTER ROW POLICY on system.*, which can hide rows from the scan",
        ),
        ([Grant("CREATE ROW POLICY", "elsewhere")], ""),
        # A temporary Distributed table forwards as a permanent one does.
        ([Grant("CREATE TEMPORARY TABLE")], "holds CREATE TEMPORARY TABLE on *.*"),
        (
            [Grant("CREATE ARBITRARY TEMPORARY TABLE")],
            "holds CREATE ARBITRARY TEMPORARY TABLE on *.*",
        ),
    ],
)
def test_the_writer_predicate(grants: list[Grant], reason: str) -> None:
    assert writer_reason(grants, DBS) == reason


def test_a_grants_row_as_newer_servers_write_it() -> None:
    rows = [
        {"user_name": "u", "access_type": "READ", "access_object": "REMOTE", "database": None},
        {"user_name": "u", "access_type": "WRITE", "access_object": "URL", "database": None},
        {"user_name": "u", "access_type": "INSERT", "database": "lo", "is_wildcard": 1},
    ]
    assert cell_catalog.resolve_grants("u", rows, []) == [
        Grant("REMOTE"),
        Grant("URL"),
        Grant("INSERT", "lo*"),
    ]


def test_attachs_own_sessions_have_only_its_default_roles() -> None:
    grant_rows = [
        {"user_name": None, "role_name": "kept", "access_type": "SHOW"},
        {"user_name": None, "role_name": "inner", "access_type": "SELECT"},
        {"user_name": None, "role_name": "spare", "access_type": "INSERT", "database": "loom"},
    ]
    # A role's grant of another reads `granted_role_is_default` 1, as the server writes it.
    role_rows = [
        {"user_name": "u", "role_name": None, "granted_role_name": "kept",
         "granted_role_is_default": 1},
        {"user_name": None, "role_name": "kept", "granted_role_name": "inner",
         "granted_role_is_default": 1},
        {"user_name": "u", "role_name": None, "granted_role_name": "spare",
         "granted_role_is_default": 0},
    ]  # fmt: skip
    every = cell_catalog.resolve_grants("u", grant_rows, role_rows)
    assert {g.access for g in every} == {"SHOW", "SELECT", "INSERT"}
    session = cell_catalog.resolve_grants("u", grant_rows, role_rows, default_only=True)
    assert {g.access for g in session} == {"SHOW", "SELECT"}


def test_a_role_named_as_a_user_is_not_that_user() -> None:
    grant_rows = [
        {"user_name": None, "role_name": "attach", "access_type": "INSERT", "database": "loom"},
        {"user_name": None, "role_name": "w", "access_type": "SHOW"},
    ]
    # Role `attach` holds role `w`; user `attach` holds neither.
    role_rows = [{"user_name": None, "role_name": "attach", "granted_role_name": "w"}]
    assert cell_catalog.resolve_grants("attach", grant_rows, role_rows) == []


def test_roles_are_followed_and_partial_revokes_ignored() -> None:
    grant_rows = [
        {"user_name": None, "role_name": "inner", "access_type": "INSERT", "database": "loom"},
        {"user_name": "u", "role_name": None, "access_type": "SELECT", "database": None},
        {
            "user_name": "u",
            "role_name": None,
            "access_type": "SELECT",
            "database": "loom",
            "table": "secret",
            "is_partial_revoke": 1,
        },
        {"user_name": "other", "role_name": None, "access_type": "ALL", "database": None},
    ]
    role_rows = [
        {"user_name": "u", "role_name": None, "granted_role_name": "outer"},
        {"user_name": None, "role_name": "outer", "granted_role_name": "inner"},
    ]
    assert cell_catalog.resolve_grants("u", grant_rows, role_rows) == [
        Grant("INSERT", "loom"),
        Grant("SELECT"),
        Grant("SELECT", "loom", "secret", revoked=True),
    ]


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("AS SELECT org FROM loom.memory, side.extra", "database side"),
        ("AS SELECT dictGet('side.orgs', 'org', id) AS org FROM loom.memory", "side.orgs"),
        ("AS SELECT dictGet('orgs', 'org', id) AS org FROM loom.memory", "dictionary or table"),
        ("AS SELECT joinGet('loom.j', 'org', id) AS org FROM loom.memory", ""),
        (
            "AS SELECT dictGet(concat('other.', 'd'), 'org', id) AS org FROM loom.memory",
            "named by an expression",
        ),
        ("AS SELECT m.org FROM loom.memory AS m", ""),
        # An alias named like a database never hides the table it stands for.
        (
            "AS SELECT x.org FROM loom.x AS x INNER JOIN evil.t AS evil ON evil.id = x.id",
            "database evil",
        ),
        ("AS SELECT x.org FROM loom.x AS x, evil.t AS evil", "database evil"),
        # A parameterized view's call, as written and as the server stores it.
        ("AS SELECT m.org FROM loom.memory AS side, side.pv(a = 1) AS m", "database side"),
        ("AS SELECT m.org FROM loom.memory AS side FINAL, side.pv(a = 1) AS m", "database side"),
        ("AS SELECT m.org FROM loom.memory AS side FINAL, side.t AS m", "database side"),
        ("AS SELECT m.org FROM loom.memory AS side SAMPLE 0.1, side.t AS m", "database side"),
        ("AS SELECT m.org FROM loom.memory AS side, `side.pv`(a = 1) AS m", "database side"),
        (
            "AS SELECT m.org FROM loom.memory AS side JOIN side.pv(a = 1) AS m ON m.id = side.id",
            "database side",
        ),
        (
            "AS SELECT m.org FROM loom.memory AS x JOIN `side.pv`(a = 1) AS m ON m.id = x.id",
            "database side",
        ),
        ("AS SELECT m.org FROM loom.memory AS x JOIN `loom.pv`(a = 1) AS m ON m.id = x.id", ""),
        ("AS SELECT org FROM loom.x AS evil, (SELECT 1 AS id) AS s", ""),
        ("AS SELECT m.org FROM loom.memory m JOIN (SELECT 1 AS id) AS s ON s.id = m.id", ""),
        ("AS WITH w AS (SELECT org FROM loom.memory) SELECT w.org FROM w", ""),
        ("AS SELECT x.org FROM loom.memory AS m", "database x"),
        ("AS SELECT org FROM loom.memory, remote('far:9000', loom.memory)", "function remote"),
        ("AS SELECT org FROM loom.memory, url('http://x', CSV, 'org String')", "function url"),
        ("AS SELECT org FROM loom.memory WHERE id IN (SELECT id FROM s3('x'))", "function s3"),
        ("AS SELECT org FROM loom.memory WHERE note = 'side.x'", ""),
        ("AS SELECT t.1 AS org FROM loom.memory", ""),
        ("AS SELECT org FROM s3('http://x', CSV)", "the table function s3"),
    ],
)
def test_any_outside_name_a_view_reads_counts(query: str, reason: str) -> None:
    rows = [
        {
            "database": "loom",
            "name": "v",
            "engine": "MaterializedView",
            "create_table_query": f"CREATE MATERIALIZED VIEW loom.v TO loom.t {query}",
        }
    ]
    functions = frozenset({"remote", "url", "s3"})
    found = cell_writers.object_problems(rows, DBS, functions).get("loom.v", "")
    assert reason in found and bool(found) == bool(reason), found


def _inside(engine: str, **row: str) -> dict[str, str]:
    return {"database": "loom", "name": "x", "engine": engine, **row}


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            _inside(
                "View", create_table_query="CREATE VIEW loom.x AS SELECT org FROM remote('f', a.b)"
            ),
            "a View reading the table function remote",
        ),
        (_inside("View", create_table_query="CREATE VIEW loom.x AS SELECT org FROM loom.m"), ""),
        (
            _inside("Merge", engine_full="Merge('side', '^t$')"),
            "a table of engine Merge reading database side",
        ),
        (_inside("Merge", engine_full="Merge(REGEXP('^lo'), '^t$')"), "reading database REGEXP"),
        (_inside("Merge", engine_full="Merge(currentDatabase(), '^t$')"), ""),
        (_inside("Merge", engine_full="Merge('loom', '^t$')"), ""),
        (
            _inside("Distributed", engine_full="Distributed('main', 'side', 't', rand())"),
            "a table of engine Distributed reading database side",
        ),
        # Another cluster's shards are servers outside this one.
        (
            _inside("Distributed", engine_full="Distributed('other', 'loom', 't', rand())"),
            "a table of engine Distributed reading cluster other, not main",
        ),
        (_inside("Distributed", engine_full="Distributed('main', 'loom', 't')"), ""),
        (_inside("Buffer", engine_full="Buffer('loom_telemetry', 't', 16, 1, 1, 1, 1, 1, 1)"), ""),
        (
            _inside("URL", engine_full="URL('http://x', CSV)"),
            "a table of engine URL, which reads outside",
        ),
        (_inside("S3", engine_full="S3('http://x')"), "a table of engine S3, which reads outside"),
        (_inside("File", engine_full="File(CSV)"), "a table of engine File, which reads outside"),
        (_inside("Memory", engine_full="Memory"), ""),
        (
            _inside(
                "WindowView",
                create_table_query="CREATE WINDOW VIEW loom.x TO loom.t AS SELECT org FROM side.s",
            ),
            "a window view reading database side",
        ),
    ],
)
def test_an_object_inside_reading_outside_counts(row: dict[str, str], reason: str) -> None:
    found = cell_writers.object_problems([row], DBS, frozenset({"remote"}), "main").get(
        "loom.x", ""
    )
    assert reason in found and bool(found) == bool(reason), found


def test_a_view_name_holding_as_does_not_hide_its_target() -> None:
    row = {
        "database": "side",
        "name": "a AS b",
        "engine": "MaterializedView",
        "create_table_query": "CREATE MATERIALIZED VIEW side.`a AS b` TO loom.memory "
        "(`org` String) AS SELECT org FROM side.s",
    }
    assert cell_writers.object_problems([row], DBS) == {
        "side.a AS b": "a materialized view writing a configured database"
    }


def test_a_window_view_outside_writing_inside_counts() -> None:
    row = {
        "database": "side",
        "name": "wv",
        "engine": "WindowView",
        "create_table_query": "CREATE WINDOW VIEW side.wv TO loom.memory AS SELECT org FROM side.s",
    }
    assert cell_writers.object_problems([row], DBS) == {
        "side.wv": "a window view writing a configured database"
    }
    assert "WindowView" in cell_writers._WRITING_VIEWS


def test_objects_writing_the_configured_databases_without_a_grant() -> None:
    rows = [
        {
            "database": "side",
            "name": "buf",
            "engine": "Buffer",
            "engine_full": "Buffer('loom', 'memory', 16, 10, 100, 10000, 1000000, 1e7, 1e8)",
        },
        {
            "database": "side",
            "name": "dist",
            "engine": "Distributed",
            "engine_full": "Distributed('main', 'loom_telemetry', 'sli_minute', rand())",
        },
        {
            "database": "side",
            "name": "merged",
            "engine": "Merge",
            "engine_full": "Merge(REGEXP('^loo'), '.*')",
        },
        {
            "database": "side",
            "name": "own_buf",
            "engine": "Buffer",
            "engine_full": "Buffer('side', 't', 16, 10, 100, 10000, 1000000, 1e7, 1e8)",
        },
        {
            "database": "side",
            "name": "mv",
            "engine": "MaterializedView",
            "create_table_query": "CREATE MATERIALIZED VIEW side.mv TO loom.memory (`org` String) "
            "AS SELECT org FROM side.src",
        },
        {
            "database": "side",
            "name": "src",
            "engine": "MergeTree",
            "dependencies_database": ["loom"],
            "dependencies_table": ["pull_mv"],
        },
        {
            "database": "loom",
            "name": "pull_mv",
            "engine": "MaterializedView",
            "create_table_query": "CREATE MATERIALIZED VIEW loom.pull_mv TO loom.memory "
            "AS SELECT org FROM side.src",
        },
        {
            "database": "loom",
            "name": "fn_mv",
            "engine": "MaterializedView",
            "create_table_query": "CREATE MATERIALIZED VIEW loom.fn_mv TO loom.memory "
            "AS SELECT org FROM url('http://x', CSV)",
        },
        {
            "database": "loom",
            "name": "ok_mv",
            "engine": "MaterializedView",
            "create_table_query": "CREATE MATERIALIZED VIEW loom.ok_mv TO loom.terms AS SELECT org "
            "FROM loom.chunks ARRAY JOIN arrayDistinct(tokens(text)) AS term",
        },
        {"database": "loom", "name": "feed", "engine": "Kafka", "engine_full": "Kafka(...)"},
    ]
    assert cell_writers.object_problems(rows, DBS, frozenset({"url"})) == {
        "side.buf": "a table of engine Buffer writing a configured database",
        "side.dist": "a table of engine Distributed writing a configured database",
        "side.merged": "a table of engine Merge writing a configured database",
        "side.mv": "a materialized view writing a configured database",
        "loom.pull_mv": "a materialized view reading database side",
        "loom.fn_mv": "a materialized view reading the table function url",
        "loom.feed": "a table of engine Kafka, which writes by itself",
    }


def test_a_replica_keeper_lists_outside_the_cluster_is_extra() -> None:
    active = {"r0": 1, "r1": 1, "dr": 0}
    rows = [
        {
            "database": "loom",
            "table": "memory",
            "zookeeper_path": "/t/memory",
            "replica_name": "r0",
            "replica_is_active": active,
        },
        {
            "database": "loom",
            "table": "memory",
            "zookeeper_path": "/t/memory",
            "replica_name": "r1",
            "replica_is_active": active,
        },
        {
            "database": "loom",
            "table": "chunks",
            "zookeeper_path": "/t/chunks",
            "replica_name": "r0",
            "replica_is_active": {"r0": 1},
        },
    ]
    assert cell_catalog.extra_replicas(rows) == {"loom.memory": {"dr"}}


def _row(user: str, access: str, database: str | None = None) -> dict[str, Any]:
    return {"h": "x", "user_name": user, "role_name": None, "access_type": access,
            "database": database, "table": None, "grant_option": 0, "is_partial_revoke": 0}  # fmt: skip


def _single_host() -> FakeCH:
    own = [
        {"g": "GRANT SELECT, SHOW, SYSTEM ON *.* TO attach"},
        {"g": "GRANT SHOW USERS, SHOW ROLES, SHOW ROW POLICIES ON *.* TO attach"},
    ]
    return (
        FakeCH()
        .on("currentUser()", [{"u": "attach"}])
        .on("SHOW GRANTS FINAL", own)
        .on(
            "system.grants",
            [
                *(
                    _row("attach", a)
                    for a in (
                        "SELECT",
                        "SHOW",
                        "SHOW USERS",
                        "SHOW ROLES",
                        "SHOW ROW POLICIES",
                        "SYSTEM",
                    )
                ),
                _row("server", "INSERT", "loom"),
                _row("server", "SELECT", "loom"),
                _row("analyst", "SELECT", "loom"),
            ],
        )
        .on("system.role_grants", [])
        .on("system.users", [{"h": "x", "name": n} for n in ("attach", "server", "analyst")])
        .on("system.row_policies", [])
        .on("system.user_directories", [{"h": "x", "name": "users_xml", "type": "users_xml"}])
        .on(
            "system.table_functions",
            [{"h": "os-0", "name": "remote"}, {"h": "os-1", "name": "url"}],
        )
        .on("system.tables", [])
        .on("system.replicas", [])
    )


async def test_one_host_names_every_writer_outside_the_allow_list() -> None:
    refusals = await cell_writers.check_writers(_single_host(), ONE, {}, Allowed())
    assert [str(r) for r in refusals] == [
        "user server: holds INSERT on loom.*: name it with --ch-allow-user"
    ]
    allowed = Allowed(users=frozenset({"server"}))
    assert await cell_writers.check_writers(_single_host(), ONE, {}, allowed) == []


async def test_a_narrow_own_user_refuses_before_any_other_read() -> None:
    ch = (
        FakeCH()
        .on("currentUser()", [{"u": "attach"}])
        .on("SHOW GRANTS FINAL", [{"g": "GRANT SELECT ON loom.memory TO attach"}])
    )
    (refusal,) = await cell_writers.check_writers(ch, ONE, {}, Allowed())
    assert refusal.subject == "user attach" and "SELECT ON system.*" in refusal.reason
    assert not [sql for _, sql, _ in ch.calls if "system." in sql]


def _cluster(
    process_user: str = "attach",
    lacking_on: str = "",
    grants: list[dict[str, Any]] | None = None,
    roles: list[dict[str, Any]] | None = None,
) -> FakeCH:
    """Two hosts; `lacking_on` a host whose attach grants leave SHOW out; `grants` and
    `roles` extra `system.grants` and `system.role_grants` rows."""
    own = [
        {"g": "GRANT SELECT, SHOW, SYSTEM, CLUSTER ON *.* TO attach"},
        {"g": "GRANT SHOW USERS, SHOW ROLES, SHOW ROW POLICIES, REMOTE ON *.* TO attach"},
    ]
    attach_grants = [
        {
            "h": h,
            "user_name": "attach",
            "role_name": None,
            "access_type": a,
            "database": None,
            "table": None,
            "grant_option": 0,
            "is_partial_revoke": 0,
        }
        for h in ("os-0", "os-1")
        for a in (
            "SELECT",
            "SHOW",
            "SHOW USERS",
            "SHOW ROLES",
            "SHOW ROW POLICIES",
            "SYSTEM",
            "REMOTE",
            "CLUSTER",
        )
        if not (h == lacking_on and a == "SHOW")
    ]
    local_writer = {
        "h": "os-1",
        "user_name": None,
        "role_name": "w",
        "access_type": "INSERT",
        "database": "loom",
        "table": None,
        "grant_option": 0,
        "is_partial_revoke": 0,
    }
    return (
        FakeCH()
        .on("currentUser()", [{"u": "attach"}])
        .on("SHOW GRANTS FINAL", own)
        .on(
            "system.processes",
            [{"h": "os-0", "user": "attach"}, {"h": "os-1", "user": process_user}],
        )
        .on(
            "system.users",
            [
                {"h": "os-0", "name": "attach"},
                {"h": "os-1", "name": "attach"},
                {"h": "os-1", "name": "local"},
            ],
        )
        .on("system.grants", [*attach_grants, local_writer, *(grants or [])])
        .on(
            "system.role_grants",
            [
                {"h": "os-1", "user_name": "local", "role_name": None, "granted_role_name": "w"},
                *(roles or []),
            ],
        )
        .on("system.row_policies", [])
        .on("system.user_directories", [{"h": "os-0", "name": "ldap", "type": "ldap"}])
        .on(
            "system.table_functions",
            [{"h": "os-0", "name": "remote"}, {"h": "os-1", "name": "url"}],
        )
        .on("system.tables", [])
        .on("system.replicas", [])
    )


async def test_a_cluster_reads_every_host_and_names_what_each_defines() -> None:
    ch = _cluster()
    # Attach's own user reads through clusterAllReplicas (REMOTE), which writes too.
    refusals = await cell_writers.check_writers(ch, TWO, NAMES, Allowed())
    assert [str(r) for r in refusals] == [
        "user attach: holds REMOTE on *.* on host ch-0: name it with --ch-allow-user",
        "user attach: holds REMOTE on *.* on host ch-1: name it with --ch-allow-user",
        "user local: holds INSERT on loom.* on host ch-1: name it with --ch-allow-user",
        "user directory ldap: its type, ldap, defines users this check cannot read on host "
        "ch-0: name it with --ch-allow-user-directory",
    ]
    reads = [s for k, s, _ in ch.calls if k == "query_all" and "hostName()" in s]
    assert reads and all(
        "clusterAllReplicas({c:String}, system." in s and s.endswith(cell_ch.CLUSTER_SQL)
        for s in reads
    )
    allowed = Allowed(users=frozenset({"attach", "local"}), user_directories=frozenset({"ldap"}))
    assert await cell_writers.check_writers(_cluster(), TWO, NAMES, allowed) == []


async def test_a_host_that_runs_the_read_as_another_user_refuses() -> None:
    (refusal,) = await cell_writers.check_writers(_cluster("default"), TWO, NAMES, Allowed())
    assert refusal.subject == "host ch-1" and "runs attach's reads as default" in refusal.reason


async def test_objects_and_extra_replicas_refuse_unless_named() -> None:
    def ch() -> FakeCH:
        fake = _single_host()
        fake.routes = [r for r in fake.routes if r[0] not in ("system.tables", "system.replicas")]
        fake.on(
            "system.tables",
            [
                {
                    "h": "x",
                    "database": "side",
                    "name": "buf",
                    "engine": "Buffer",
                    "engine_full": "Buffer(loom, memory, 16)",
                }
            ],
        )
        fake.on(
            "system.replicas",
            [
                {
                    "h": "x",
                    "database": "loom",
                    "table": "memory",
                    "zookeeper_path": "/m",
                    "replica_name": "r0",
                    "replica_is_active": {"r0": 1, "dr": 1},
                }
            ],
        )
        return fake

    allowed = Allowed(users=frozenset({"server"}))
    assert [r.subject for r in await cell_writers.check_writers(ch(), ONE, {}, allowed)] == [
        "object side.buf",
        "replica loom.memory:dr",
    ]
    allowed = Allowed(
        users=frozenset({"server"}),
        objects=frozenset({"side.buf"}),
        replicas=frozenset({"loom.memory:dr"}),
    )
    assert await cell_writers.check_writers(ch(), ONE, {}, allowed) == []


def test_an_outside_keeper_map_sharing_an_inside_ones_keys_counts() -> None:
    rows = [
        {"database": "loom", "name": "km", "engine": "KeeperMap",
         "engine_full": "KeeperMap('/loom/namespace_write_keys')"},
        {"database": "side", "name": "same", "engine": "KeeperMap",
         "engine_full": "KeeperMap('//loom/namespace_write_keys/', 100)"},
        {"database": "side", "name": "other", "engine": "KeeperMap",
         "engine_full": "KeeperMap('/side/keys')"},
    ]  # fmt: skip
    assert cell_writers.object_problems(rows, DBS) == {
        "side.same": "a KeeperMap table sharing loom.km's keys"
    }


def test_an_outside_table_that_connects_out_counts() -> None:
    rows = [
        {"database": "side", "name": "u", "engine": "URL", "engine_full": "URL('http://x', CSV)"},
        {"database": "side", "name": "p", "engine": "PostgreSQL", "engine_full": "PostgreSQL(...)"},
        {"database": "side", "name": "m", "engine": "MergeTree", "engine_full": "MergeTree"},
    ]
    assert cell_writers.object_problems(rows, DBS) == {
        "side.u": "a table of engine URL, which connects out with credentials of its own",
        "side.p": "a table of engine PostgreSQL, which connects out with credentials of its own",
    }


async def test_the_listing_checks_every_host_runs_it_as_attachs_user() -> None:
    listed = await cell_scan.list_tables(_cluster("default"), None, TWO, NAMES)
    assert [r.subject for r in listed.refusals] == ["host ch-1"]
    assert listed.clickhouse == []
    listed = await cell_scan.list_tables(_cluster(lacking_on="os-1"), None, TWO, NAMES)
    assert [str(r) for r in listed.refusals] == [
        "user attach: lacks SHOW TABLES ON *.*, SHOW COLUMNS ON *.* on host ch-1"
    ]


def _own_through_a_spare_role(host: str) -> list[dict[str, Any]]:
    """Attach holding SHOW only through a role that is not a default one, on `host`."""
    return [
        {"h": host, "user_name": None, "role_name": "spare", "access_type": "SHOW",
         "database": None, "table": None, "grant_option": 0, "is_partial_revoke": 0},
    ]  # fmt: skip


async def test_a_need_held_only_through_a_non_default_role_refuses_on_one_host() -> None:
    ch = _single_host()
    ch.routes = [r for r in ch.routes if r[0] not in ("system.grants", "system.role_grants")]
    rows = [
        _row("attach", a)
        for a in ("SELECT", "SHOW USERS", "SHOW ROLES", "SHOW ROW POLICIES", "SYSTEM")
    ] + _own_through_a_spare_role("x")
    ch.on("system.grants", rows)
    ch.on(
        "system.role_grants",
        [{"h": "x", "user_name": "attach", "role_name": None, "granted_role_name": "spare",
          "granted_role_is_default": 0}],
    )  # fmt: skip
    refusals = await cell_writers.check_writers(ch, ONE, {}, Allowed())
    assert [str(r) for r in refusals] == [
        "user attach: lacks SHOW TABLES ON *.*, SHOW COLUMNS ON *.*"
    ]


async def test_a_need_held_only_through_a_non_default_role_refuses_on_one_of_several() -> None:
    spare = {"h": "os-1", "user_name": "attach", "role_name": None,
             "granted_role_name": "spare", "granted_role_is_default": 0}  # fmt: skip
    ch = _cluster(lacking_on="os-1", grants=_own_through_a_spare_role("os-1"), roles=[spare])
    listed = await cell_scan.list_tables(ch, None, TWO, NAMES)
    assert [str(r) for r in listed.refusals] == [
        "user attach: lacks SHOW TABLES ON *.*, SHOW COLUMNS ON *.* on host ch-1"
    ]


def _policy(name: str, table: str = "memory", **row: Any) -> dict[str, Any]:
    return {"short_name": name, "database": "loom", "table": table, "select_filter": "1",
            "is_restrictive": 0, "apply_to_all": 0, "apply_to_list": [],
            "apply_to_except": [], **row}  # fmt: skip


def test_loom_s_own_row_policy_pair_lets_attach_read_every_row() -> None:
    from server.insights.domain.analysis import OTHERS_POLICY_NAME, POLICY_NAME

    rows = [
        _policy(POLICY_NAME, select_filter="org = 'a'", apply_to_list=["loom_analyst"]),
        _policy(OTHERS_POLICY_NAME, apply_to_all=1, apply_to_except=["loom_analyst"]),
    ]
    assert cell_catalog.row_policy_problems(rows, "attach") == []


def test_any_row_policy_on_a_system_table_refuses() -> None:
    rows = [
        _policy("own", database="system", table="grants", apply_to_all=1),
        _policy("loom_analyst_others", database="system", table="tables", apply_to_all=1),
    ]
    assert cell_catalog.row_policy_problems(rows, "attach") == [
        ("row policy on system.grants: own", "a row policy hides rows from the checks"),
        (
            "row policy on system.tables: loom_analyst_others",
            "a row policy hides rows from the checks",
        ),
    ]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        # Any other policy, even one for another user: once one exists, a user no policy
        # covers reads no row.
        ([_policy("theirs", apply_to_list=["bi"])], "a row policy hides rows"),
        (
            [_policy("loom_analyst_others", apply_to_all=1, apply_to_except=["attach"])],
            "no permissive policy `USING 1` covers attach",
        ),
        (
            [_policy("loom_analyst_others", apply_to_all=1, is_restrictive=1)],
            "no permissive policy `USING 1` covers attach",
        ),
        (
            [_policy("loom_analyst_others", apply_to_all=1, select_filter="org != ''")],
            "no permissive policy `USING 1` covers attach",
        ),
        (
            [
                _policy("loom_analyst_scope", apply_to_list=["attach"]),
                _policy("loom_analyst_others", apply_to_all=1, apply_to_except=["loom_analyst"]),
            ],
            "names other users or roles than Loom writes",
        ),
        # A role attach's user holds, in either list, covers or excludes it unnamed.
        (
            [
                _policy("loom_analyst_scope", apply_to_list=["loom_analyst"]),
                _policy(
                    "loom_analyst_others", apply_to_all=1, apply_to_except=["loom_analyst", "r"]
                ),
            ],
            "names other users or roles than Loom writes",
        ),
        (
            [
                _policy("loom_analyst_scope", apply_to_list=["loom_analyst", "r"]),
                _policy("loom_analyst_others", apply_to_all=1, apply_to_except=["loom_analyst"]),
            ],
            "names other users or roles than Loom writes",
        ),
    ],
)
def test_a_row_policy_hiding_rows_from_attach_refuses(
    rows: list[dict[str, Any]], reason: str
) -> None:
    found = cell_catalog.row_policy_problems(rows, "attach")
    assert any(reason in why for _, why in found), found


async def test_the_row_policies_read_covers_the_system_tables() -> None:
    ch = _cluster()
    await cell_catalog.own_refusals(ch, TWO, NAMES)
    (sql,) = [s for s in ch.sql("query_all") if "system.row_policies" in s]
    assert "database IN {dbs:Array(String)} OR database = 'system'" in sql


async def test_a_row_policy_on_a_configured_table_refuses_the_listing() -> None:
    ch = _cluster()
    ch.routes = [r for r in ch.routes if r[0] != "system.row_policies"]
    ch.routes.insert(0, ("system.row_policies", [{"h": "os-1", **_policy("theirs")}]))
    listed = await cell_scan.list_tables(ch, None, TWO, NAMES)
    assert [str(r) for r in listed.refusals] == [
        "row policy on loom.memory: theirs: a row policy hides rows from the scan on host ch-1",
        "row policy on loom.memory: no permissive policy `USING 1` covers attach: it reads no "
        "row on host ch-1",
    ]


async def test_a_refused_process_list_read_is_named() -> None:
    ch = _cluster()
    ch.routes.insert(
        0, ("system.processes", _AccessDenied("Code: 497. DB::Exception: Not enough privileges"))
    )
    listed = await cell_scan.list_tables(ch, None, TWO, NAMES)
    assert [r.subject for r in listed.refusals] == ["user attach"]
    assert "cannot read what the checks read (Code: 497" in listed.refusals[0].reason


class _AccessDenied(Exception):
    """A server refusal as the driver words one."""


def _looms_policies() -> list[dict[str, Any]]:
    """The row policies Loom's analyst setup writes, as `system.row_policies` lists them."""
    from server.insights.domain import analysis
    from server.observability.store.event_reads import MASKED_EVENT_COLUMNS

    caps = analysis.Caps(
        max_execution_s=10,
        max_rows_to_read=1000,
        max_memory_bytes=10_000,
        max_result_rows=50,
        max_result_bytes=100_000,
        max_concurrent=4,
        max_threads=2,
    )
    sql = analysis.entity_sql(
        "`loom`",
        analysis.SettingNames("SQL_"),
        "pw",
        b"\0" * 32,
        caps,
        event_columns=MASKED_EVENT_COLUMNS,
    )
    shape = re.compile(
        r"CREATE ROW POLICY OR REPLACE `(\w+)` ON `loom`\.(\w+) AS (\w+) FOR SELECT "
        r"USING (.*) TO (ALL EXCEPT )?`(\w+)`$"
    )
    rows = []
    for stmt in sql:
        if m := shape.match(stmt):
            name, table, kind, using, except_, user = m.groups()
            rows.append(
                _policy(
                    name,
                    table,
                    select_filter=using,
                    is_restrictive=int(kind == "restrictive"),
                    apply_to_all=int(bool(except_)),
                    apply_to_list=[] if except_ else [user],
                    apply_to_except=[user] if except_ else [],
                )
            )
    assert len(rows) == sum(s.startswith("CREATE ROW POLICY") for s in sql)
    return rows


def test_the_policies_loom_s_analyst_setup_writes_pass() -> None:
    rows = _looms_policies()
    assert {r["table"] for r in rows} >= {"memory", "analysis_canary"}
    assert cell_catalog.row_policy_problems(rows, "attach") == []
    # Without every other user's policy, the canary would read no row for attach.
    alone = [
        r
        for r in rows
        if not (r["table"] == "analysis_canary" and r["short_name"] == "loom_analyst_others")
    ]
    assert cell_catalog.row_policy_problems(alone, "attach") == [
        (
            "row policy on loom.analysis_canary",
            "no permissive policy `USING 1` covers attach: it reads no row",
        )
    ]
