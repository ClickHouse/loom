"""`LOOM_CH_DATABASE` and `LOOM_CH_TELEMETRY_DATABASE` decide where every statement
goes: the SQL names the `loom` and `loom_telemetry` aliases, `qualify` maps them where
a statement reaches the driver, and nothing in `server/` names a database by value."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from server.core import ch_names
from server.core.schema import preflight
from server.settings import Settings, settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_database", "acme-loom")
    monkeypatch.setattr(settings, "ch_telemetry_database", "acme_telemetry")


def test_the_defaults_leave_a_statement_as_written() -> None:
    sql = "SELECT * FROM loom.memory JOIN loom_telemetry.sli_minute USING (org)"
    assert ch_names.qualify(sql) is sql


@pytest.mark.usefixtures("renamed")
@pytest.mark.parametrize(
    ("sql", "want"),
    [
        ("SELECT * FROM loom.memory FINAL", "SELECT * FROM `acme-loom`.memory FINAL"),
        ("INSERT INTO `loom`.audit_log", "INSERT INTO `acme-loom`.audit_log"),
        ("FROM loom_telemetry.sli_minute", "FROM acme_telemetry.sli_minute"),
        ("GRANT SELECT ON loom.* TO r", "GRANT SELECT ON `acme-loom`.* TO r"),
        (
            "CREATE DATABASE IF NOT EXISTS loom_telemetry;",
            "CREATE DATABASE IF NOT EXISTS acme_telemetry;",
        ),
        ("create database loom", "create database `acme-loom`"),
        # KeeperMap keys live outside any database: each schema keeps its own path.
        (
            "ENGINE = KeeperMap('/loom/task_leases') PRIMARY KEY k",
            "ENGINE = KeeperMap('/acme-loom/task_leases') PRIMARY KEY k",
        ),
        ("SELECT '/loom/x' FROM loom.a", "SELECT '/loom/x' FROM `acme-loom`.a"),
        # Literals, comments, quoted identifiers and other names are left as written.
        ("SELECT 'loom.memory', \"loom\".x", "SELECT 'loom.memory', `acme-loom`.x"),
        (
            'SELECT `a loom.b`, "c loom.d" FROM loom.a',
            'SELECT `a loom.b`, "c loom.d" FROM `acme-loom`.a',
        ),
        ("SELECT 'it''s loom.x' FROM loom.a", "SELECT 'it''s loom.x' FROM `acme-loom`.a"),
        ("SELECT 1 -- loom.memory\nFROM loom.a", "SELECT 1 -- loom.memory\nFROM `acme-loom`.a"),
        ("SELECT /* loom.a */ 1", "SELECT /* loom.a */ 1"),
        ("FROM myloom.t, loom_x.t, x.loom.t", "FROM myloom.t, loom_x.t, x.loom.t"),
        ("DROP DATABASE loom_telemetry_old", "DROP DATABASE loom_telemetry_old"),
    ],
)
def test_qualify_maps_only_the_aliases(sql: str, want: str) -> None:
    assert ch_names.qualify(sql) == want


@pytest.mark.usefixtures("renamed")
def test_ddl_read_back_names_the_aliases_again() -> None:
    ddl = (
        "CREATE TABLE `acme-loom`.task_leases (k String) "
        "ENGINE = KeeperMap('/acme-loom/task_leases') PRIMARY KEY k COMMENT 'acme-loom.x'"
    )
    assert ch_names.unqualify(ddl) == (
        "CREATE TABLE loom.task_leases (k String) "
        "ENGINE = KeeperMap('/loom/task_leases') PRIMARY KEY k COMMENT 'acme-loom.x'"
    )
    assert ch_names.qualify(ch_names.unqualify(ddl)) == ddl
    view = "CREATE MATERIALIZED VIEW acme_telemetry.v REFRESH EVERY 1 MINUTE TO acme_telemetry.t"
    assert ch_names.unqualify(view) == (
        "CREATE MATERIALIZED VIEW loom_telemetry.v REFRESH EVERY 1 MINUTE TO loom_telemetry.t"
    )


def test_only_the_telemetry_database_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_telemetry_database", "acme_telemetry")
    sql = "INSERT INTO loom_telemetry.t SELECT * FROM loom.memory; KeeperMap('/loom/x')"
    assert ch_names.qualify(sql) == (
        "INSERT INTO acme_telemetry.t SELECT * FROM loom.memory; KeeperMap('/loom/x')"
    )


@pytest.mark.usefixtures("renamed")
def test_names_passed_as_values_map_both_ways() -> None:
    assert ch_names.physical("loom") == "acme-loom"
    assert ch_names.physical("loom_telemetry") == "acme_telemetry"
    assert ch_names.physical("system") == "system"
    assert ch_names.logical("acme-loom") == "loom"
    assert ch_names.logical("acme_telemetry") == "loom_telemetry"
    assert ch_names.quote("acme-loom") == "`acme-loom`"
    assert ch_names.quote("loom") == "loom"
    assert ch_names.split_table("loom.memory") == ("acme-loom", "memory")
    assert ch_names.split_table("memory") == ("acme-loom", "memory")
    assert ch_names.split_table("loom_telemetry.sli_minute") == ("acme_telemetry", "sli_minute")
    assert ch_names.split_table("system.parts") == ("system", "parts")


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.server_tz = None

    def query(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("query", args, kwargs))

    def command(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("command", args, kwargs))

    def insert(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("insert", args, kwargs))

    def create_insert_context(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_insert_context", args, kwargs))


@pytest.mark.usefixtures("renamed")
def test_the_client_wrapper_names_the_configured_databases() -> None:
    driver = _Driver()
    client = ch_names.NamedClient(driver)
    client.query("SELECT 1 FROM loom.memory", parameters={"a": "loom.memory"})
    client.command(cmd="DROP TABLE loom_telemetry.x")
    client.insert("loom.audit_log", [[1]])
    client.insert(table="audit_log", data=[[1]], database="loom")
    client.create_insert_context("loom.memory", ["a"])
    client.server_tz = "UTC"
    assert driver.calls == [
        ("query", ("SELECT 1 FROM `acme-loom`.memory",), {"parameters": {"a": "loom.memory"}}),
        ("command", (), {"cmd": "DROP TABLE acme_telemetry.x"}),
        ("insert", ("`acme-loom`.audit_log", [[1]]), {}),
        ("insert", (), {"table": "audit_log", "data": [[1]], "database": "acme-loom"}),
        ("create_insert_context", ("`acme-loom`.memory", ["a"]), {}),
    ]
    assert driver.server_tz == "UTC"


@pytest.mark.parametrize(
    "env",
    [
        {"ch_database": "x", "ch_telemetry_database": "x"},
        {"ch_database": "loom", "ch_telemetry_database": "loom"},
        {"ch_database": "loom_telemetry", "ch_telemetry_database": "t"},
        {"ch_database": "x", "ch_telemetry_database": "loom"},
        {"ch_database": "acme"},  # its telemetry would stay the first schema's
        {"ch_database": "a`b"},
        {"ch_database": "a'b"},
        {"ch_telemetry_database": "a/b"},
        {"ch_database": ""},
    ],
)
def test_a_database_that_would_map_twice_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**env)


class _GuardCH:
    """`system.tables` as the moved-schema guard reads it."""

    def __init__(self, *, other: int, tables: int = 0, ledger: int = 0, exists: bool) -> None:
        self.row = (other, tables, ledger)
        self.exists = exists
        self.params: list[dict[str, Any]] = []

    def query(self, sql: str, parameters: dict[str, Any]) -> Any:
        self.params.append(parameters)
        return type("R", (), {"result_rows": [self.row]})()

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> int:
        return int(self.exists)


@pytest.mark.parametrize(
    ("ch", "refused"),
    [
        (_GuardCH(other=1, exists=False), "which does not exist"),
        (_GuardCH(other=1, tables=3, exists=True), "holds 3 tables and no Loom ledger"),
        (_GuardCH(other=1, tables=0, exists=True), ""),  # created empty on purpose
        (_GuardCH(other=1, tables=40, ledger=1, exists=True), ""),  # its own schema
        (_GuardCH(other=0, exists=False), ""),  # no schema in loom to move away from
    ],
)
def test_a_new_database_beside_an_existing_schema_is_refused(
    monkeypatch: pytest.MonkeyPatch, ch: _GuardCH, refused: str
) -> None:
    monkeypatch.setattr(settings, "ch_database", "acme")
    if refused:
        with pytest.raises(preflight.PreflightError, match=refused):
            preflight._refuse_moved_schema(ch)  # type: ignore[arg-type]
    else:
        preflight._refuse_moved_schema(ch)  # type: ignore[arg-type]
    assert ch.params == [{"schema": "loom", "db": "acme", "own": ["_migration_lock"]}]


def test_the_default_database_never_reads_for_a_moved_schema() -> None:
    ch = _GuardCH(other=1, exists=False)
    preflight._refuse_moved_schema(ch)  # type: ignore[arg-type]
    assert ch.params == []


@pytest.mark.usefixtures("renamed")
def test_a_carried_write_goes_to_the_configured_database() -> None:
    from server.core import ch, rmt_carry

    statement = ch._fenced_statement("loom.memory", ["id"], ["String"], frozenset({"id"}), 0.0)
    assert statement.startswith("INSERT INTO `acme-loom`.memory (`id`)")
    assert "database = 'acme-loom' AND table = 'memory'" in statement
    assert "loom." not in statement.replace("`acme-loom`.", "")

    class Columns:
        keys: list[Any] = []

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.keys = params["keys"]
            return [{"database": "acme-loom", "table": "memory", "name": "id"}]

    reader = Columns()
    stored = asyncio.run(rmt_carry._stored(reader, ["loom.memory"]))
    assert reader.keys == [("acme-loom", "memory")]
    assert stored == {"loom.memory": {"id"}}


# A database named by value in a statement (`database = 'loom'`), which no setting
# would move: compare with currentDatabase() or bind `ch_names.database()`.
_LITERAL = re.compile(r"""database\s*(?:!?=|IN)\s*\(?\s*'loom(?:_telemetry)?'""", re.I)


def test_no_statement_names_a_loom_database_by_value() -> None:
    # scripts/ops runs on the server's clients; the gates run their own throwaway
    # ClickHouse at the default names.
    paths = [*(ROOT / "server").rglob("*.py"), *(ROOT / "scripts" / "ops").rglob("*.py")]
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted(paths)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _LITERAL.search(line)
    ]
    assert not hits, hits


# Every client connected to Loom's ClickHouse is built in one of these and wrapped.
_CLIENT_MODULES = {
    "server/core/ch.py",
    "server/core/schema/migrate.py",
    "server/core/schema/preflight.py",
    "server/observability/otel_exporters.py",
}
# Clients that never name Loom's tables: the reader-login probe, the SQL surface
# users' login probe, an external source.
_UNWRAPPED = {
    "server/cli/ch_setup.py",
    "server/cli/sql_access.py",
    "server/tracker/store/source/qa_intel.py",
}


def test_every_client_on_loom_clickhouse_is_wrapped() -> None:
    builders = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "server").rglob("*.py")
        if re.search(r"clickhouse_connect\.get_(?:async_)?client\(", path.read_text("utf-8"))
    }
    assert builders == _CLIENT_MODULES | _UNWRAPPED
    for module in _CLIENT_MODULES:
        assert "NamedClient(" in (ROOT / module).read_text("utf-8"), module
