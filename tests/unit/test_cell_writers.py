"""`server/cli/cell_writers.py`: reading grants (`SHOW GRANTS … FINAL` on one host,
`system.grants` and `system.role_grants` per host on several), the writer predicate, the
objects, user directories and extra replicas it refuses, and attach's own grants. A cluster
of several hosts cannot run in CI, so its per-host reads are pinned here on a fake
ClickHouse. CH-free."""

from __future__ import annotations

import pytest

from server.cli import cell_writers
from server.cli.cell_scan import Topology
from server.cli.cell_writers import Allowed, Grant, holds, parse_show_grants, writer_reason
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
    ]


def test_a_privilege_is_held_through_any_grant_implying_it() -> None:
    assert holds([Grant("SOURCES")], "REMOTE")
    assert holds([Grant("ALL")], "SYSTEM SYNC REPLICA", "loom")
    assert holds([Grant("SHOW ACCESS")], "SHOW USERS")
    assert holds([Grant("SYSTEM FLUSH")], "SYSTEM FLUSH ASYNC INSERT QUEUE")
    assert holds([Grant("SELECT", "loom")], "SELECT", "loom")
    assert holds([Grant("SELECT", "lo*")], "SELECT", "loom")
    # A grant on one table, or on another database, is not a grant on the database.
    assert not holds([Grant("SELECT", "loom", "memory")], "SELECT", "loom")
    assert not holds([Grant("SELECT", "other")], "SELECT", "loom")
    assert not holds([Grant("SELECT", "loom")], "SELECT")


def test_attachs_own_user_needs_every_read_its_checks_make() -> None:
    full = [Grant("SELECT"), Grant("SHOW"), Grant("SHOW USERS"), Grant("SYSTEM")]
    assert cell_writers.own_problems(full, ONE, DBS) == []
    assert cell_writers.own_problems(full, TWO, DBS) == ["READ ON REMOTE", "CLUSTER ON *.*"]
    narrow = [Grant("SELECT", "loom", "memory"), Grant("SELECT", "system"), Grant("SHOW")]
    assert cell_writers.own_problems(narrow, Topology("", frozenset(), 0), DBS) == [
        "SHOW USERS ON *.*",
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
    ],
)
def test_the_writer_predicate(grants: list[Grant], reason: str) -> None:
    assert writer_reason(grants, DBS) == reason


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
    assert cell_writers.resolve_grants("u", grant_rows, role_rows) == [
        Grant("INSERT", "loom"),
        Grant("SELECT"),
    ]


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
    assert cell_writers.object_problems(rows, DBS) == {
        "side.buf": "a Buffer table writing a configured database",
        "side.dist": "a Distributed table writing a configured database",
        "side.merged": "a Merge table writing a configured database",
        "side.mv": "a materialized view writing a configured database",
        "loom.pull_mv": "a materialized view reading database side",
        "loom.fn_mv": "a materialized view reading the table function url",
        "loom.feed": "a Kafka table, which writes by itself",
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
    assert cell_writers.extra_replicas(rows) == {"loom.memory": {"dr"}}


def _single_host() -> FakeCH:
    own = [
        {"g": "GRANT SELECT, SHOW, SYSTEM ON *.* TO attach"},
        {"g": "GRANT SHOW USERS ON *.* TO attach"},
    ]
    return (
        FakeCH()
        .on("currentUser()", [{"u": "attach"}])
        .on("SHOW GRANTS FINAL", own)
        .on("SHOW GRANTS FOR `attach` FINAL", own)
        .on("SHOW GRANTS FOR `server` FINAL", [{"g": "GRANT INSERT, SELECT ON loom.* TO server"}])
        .on("SHOW GRANTS FOR `analyst` FINAL", [{"g": "GRANT SELECT ON loom.* TO analyst"}])
        .on("system.users", [{"h": "x", "name": n} for n in ("attach", "server", "analyst")])
        .on("system.user_directories", [{"h": "x", "name": "users_xml", "type": "users_xml"}])
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
    assert len(ch.calls) == 2


def _cluster(process_user: str = "attach") -> FakeCH:
    own = [
        {"g": "GRANT SELECT, SHOW, SYSTEM, CLUSTER ON *.* TO attach"},
        {"g": "GRANT SHOW USERS, REMOTE ON *.* TO attach"},
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
        for a in ("SELECT", "SHOW", "SHOW USERS", "SYSTEM", "REMOTE", "CLUSTER")
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
        .on("system.grants", [*attach_grants, local_writer])
        .on(
            "system.role_grants",
            [{"h": "os-1", "user_name": "local", "role_name": None, "granted_role_name": "w"}],
        )
        .on("system.user_directories", [{"h": "os-0", "name": "ldap", "type": "ldap"}])
        .on("system.tables", [])
        .on("system.replicas", [])
    )


async def test_a_cluster_reads_every_host_and_names_what_each_defines() -> None:
    ch = _cluster()
    refusals = await cell_writers.check_writers(ch, TWO, NAMES, Allowed())
    assert [str(r) for r in refusals] == [
        "user local: holds INSERT on loom.* on host ch-1: name it with --ch-allow-user",
        "user directory ldap: its type, ldap, defines users this check cannot read on host "
        "ch-0: name it with --ch-allow-user-directory",
    ]
    reads = [s for k, s, _ in ch.calls if k == "query_all" and "hostName()" in s]
    assert reads and all(
        "clusterAllReplicas({c:String}, system." in s
        and s.endswith("SETTINGS skip_unavailable_shards = 0, prefer_localhost_replica = 1")
        for s in reads
    )
    allowed = Allowed(users=frozenset({"local"}), user_directories=frozenset({"ldap"}))
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
