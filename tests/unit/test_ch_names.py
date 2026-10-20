"""`LOOM_CH_DATABASE` and `LOOM_CH_TELEMETRY_DATABASE` decide where every statement
goes: the SQL names the `loom` and `loom_telemetry` aliases, `qualify` maps them where
a statement reaches the driver, and nothing in `server/` names a database by value."""

from __future__ import annotations

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
        ("SELECT 'loom.memory', \"loom\".x", "SELECT 'loom.memory', \"loom\".x"),
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


@pytest.mark.usefixtures("renamed")
def test_the_client_wrapper_names_the_configured_databases() -> None:
    driver = _Driver()
    client = ch_names.NamedClient(driver)
    client.query("SELECT 1 FROM loom.memory", parameters={"a": "loom.memory"})
    client.command(cmd="DROP TABLE loom_telemetry.x")
    client.insert("loom.audit_log", [[1]])
    client.insert(table="audit_log", data=[[1]], database="loom")
    client.server_tz = "UTC"
    assert driver.calls == [
        ("query", ("SELECT 1 FROM `acme-loom`.memory",), {"parameters": {"a": "loom.memory"}}),
        ("command", (), {"cmd": "DROP TABLE acme_telemetry.x"}),
        ("insert", ("`acme-loom`.audit_log", [[1]]), {}),
        ("insert", (), {"table": "audit_log", "data": [[1]], "database": "acme-loom"}),
    ]
    assert driver.server_tz == "UTC"


@pytest.mark.parametrize(
    "env",
    [
        {"ch_database": "x", "ch_telemetry_database": "x"},
        {"ch_database": "loom_telemetry", "ch_telemetry_database": "t"},
        {"ch_database": "x", "ch_telemetry_database": "loom"},
        {"ch_database": "a`b"},
        {"ch_database": "a'b"},
        {"ch_telemetry_database": "a/b"},
        {"ch_database": ""},
    ],
)
def test_a_database_that_would_map_twice_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**env)


class _LedgerCH:
    def __init__(self, ledger: int) -> None:
        self.ledger = ledger
        self.params: list[dict[str, Any]] = []

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> int:
        self.params.append(dict(parameters or {}))
        return self.ledger


def test_a_new_database_beside_an_existing_schema_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ch_database", "acme")
    ch = _LedgerCH(ledger=1)
    with pytest.raises(preflight.PreflightError, match="LOOM_CH_DATABASE=loom"):
        preflight._refuse_moved_schema(ch)  # type: ignore[arg-type]
    assert ch.params == [{"db": "loom"}]
    preflight._refuse_moved_schema(_LedgerCH(ledger=0))  # type: ignore[arg-type]


def test_the_default_database_never_reads_for_a_moved_schema() -> None:
    ch = _LedgerCH(ledger=1)
    preflight._refuse_moved_schema(ch)  # type: ignore[arg-type]
    assert ch.params == []


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
# Clients that never name Loom's tables: the reader-login probe, an external source.
_UNWRAPPED = {"server/cli/ch_setup.py", "server/tracker/store/source/qa_intel.py"}


def test_every_client_on_loom_clickhouse_is_wrapped() -> None:
    builders = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "server").rglob("*.py")
        if re.search(r"clickhouse_connect\.get_(?:async_)?client\(", path.read_text("utf-8"))
    }
    assert builders == _CLIENT_MODULES | _UNWRAPPED
    for module in _CLIENT_MODULES:
        assert "NamedClient(" in (ROOT / module).read_text("utf-8"), module
