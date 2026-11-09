"""Call sites that pass a database name as a value, or read DDL and statement text
back from ClickHouse, under a renamed schema (`LOOM_CH_DATABASE=acme-loom`)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from server.settings import Settings, settings


@pytest.fixture(autouse=True)
def renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_database", "acme-loom")
    monkeypatch.setattr(settings, "ch_telemetry_database", "acme_telemetry")


class _Capture:
    """An async client that records each statement and its parameters."""

    def __init__(self, answer: dict[str, Any] | None = None, rows: list[Any] | None = None):
        self.answer = answer if answer is not None else {"running": 0}
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((sql, dict(params or {})))
        return self.answer

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((sql, dict(params or {})))
        return self.rows


def test_a_refresh_target_is_found_under_any_database_name() -> None:
    from server.code.app.index import retire

    # Written for a SQL string literal; ClickHouse's regex sees it unescaped.
    pattern = re.compile(retire._REFRESH_TARGET.replace("\\\\", "\\"))
    head = "CREATE MATERIALIZED VIEW `acme-loom`.v REFRESH EVERY 1 HOUR TO "
    for target, name in (
        ("`acme-loom`.code_x", "code_x"),
        ("acme.code_y", "code_y"),
        ("loom.code_z", "code_z"),
    ):
        m = pattern.search(f"{head}{target} (`a` String) AS SELECT 1")
        assert m is not None and m.group(1) == name


def test_running_work_checks_match_the_qualified_statements() -> None:
    from server.insights.store import provider_prepared, provider_staging
    from server.insights.store.provider_staging import StagingNames

    names = StagingNames(uuid4(), "embedding_cache")
    ch = _Capture()
    asyncio.run(provider_prepared._assert_idle(names, ch))  # type: ignore[arg-type]
    assert ch.calls[0][1]["prefixes"] == [
        f"{op} `acme-loom`.{names.prepared} "
        for op in ("CREATE TABLE", "INSERT INTO", "ALTER TABLE")
    ]
    ch = _Capture()
    assert not asyncio.run(provider_staging.snapshot_query_running(names, ch=ch))  # type: ignore[arg-type]
    assert ch.calls[0][1]["prefix"] == f"CREATE TABLE `acme-loom`.{names.snapshot} "


def test_reclaim_reads_the_configured_database() -> None:
    from server.erasure.store import reclaim

    ch = _Capture(answer={"masked_parts": 0})
    assert asyncio.run(reclaim.masked_rows(ch, "loom.memory")) is None
    assert ch.calls[0][1] == {"db": "acme-loom", "t": "memory"}


def test_the_reader_holds_select_on_the_configured_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse_connect

    from server.cli import ch_setup

    def row(name: str, s: ch_setup.Setting) -> tuple[Any, ...]:
        low = None if s.low is None else str(s.low)
        high = None if s.high is None else str(s.high)
        return (name, s.value.strip("'"), low, high, int(s.const))

    class Reader:
        def command(self, sql: str) -> Any:
            return "loom_reader"

        def query(self, sql: str, **_: Any) -> SimpleNamespace:
            if sql == "SHOW GRANTS":
                return SimpleNamespace(
                    result_rows=[("GRANT SELECT ON `acme-loom`.* TO loom_reader",)]
                )
            return SimpleNamespace(result_rows=[row(n, s) for n, s in ch_setup._profile().items()])

        def close(self) -> None:
            pass

    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **_: Reader())
    assert ch_setup.reader_status("loom_reader") == (True, "")


def test_the_doctor_probes_the_reader_on_the_configured_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from server.cli import doctor

    sent: list[str] = []

    class Client:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def post(self, url: str, *, content: str, **_: Any) -> SimpleNamespace:
            sent.append(content)
            return SimpleNamespace(status_code=200, text="loom_reader\t1")

    monkeypatch.setattr(settings, "ch_reader_user", "loom_reader")
    monkeypatch.setattr(settings, "ch_host", "localhost")
    monkeypatch.setattr(httpx, "Client", Client)
    assert doctor._doctor_reader_probe(settings) == ("ok", "")
    assert "`acme-loom`.memory" in sent[0] and "loom.memory" not in sent[0]


def test_carried_writers_are_this_schemas_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import rmt_carry

    monkeypatch.setattr(settings, "ch_cluster", "")
    ch = _Capture(rows=[{"process": "p1"}])
    assert asyncio.run(rmt_carry._running_writers(ch)) == {"p1"}
    sql, params = ch.calls[0]
    assert "current_database = {db:String}" in sql
    assert params["db"] == "acme-loom"


@pytest.mark.parametrize(
    "env",
    [
        {"ch_database": "default", "ch_telemetry_database": "x_telemetry"},
        {"ch_database": "INFORMATION_SCHEMA", "ch_telemetry_database": "x_telemetry"},
        {"ch_telemetry_database": "system"},
    ],
)
def test_a_shared_database_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="must not be"):
        Settings(**env)


def test_a_renamed_schema_without_its_telemetry_says_how_to_undo_it() -> None:
    with pytest.raises(ValidationError, match="unset it"):
        Settings(ch_database="acme")


def test_the_schema_dump_reads_the_configured_databases_and_writes_the_aliases() -> None:
    from scripts.ops import dump_schema

    class Client:
        bound: list[str] = []

        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> SimpleNamespace:
            if sql.startswith("SHOW CREATE"):
                return SimpleNamespace(
                    result_rows=[("CREATE TABLE `acme-loom`.memory (`a` String) ENGINE = Log",)]
                )
            self.bound = list((parameters or {})["dbs"])
            return SimpleNamespace(
                result_rows=[
                    ("acme-loom", "memory", "MergeTree"),
                    ("acme_telemetry", "sli_minute", "MergeTree"),
                ]
            )

    client = Client()
    tables, plain_views, views = dump_schema.list_objects(client)
    assert (tables, plain_views, views) == (["loom.memory", "loom_telemetry.sli_minute"], [], [])
    assert client.bound == ["acme-loom", "acme_telemetry"]
    assert dump_schema.show_create(client, "loom.memory") == (
        "loom.memory",
        "CREATE TABLE loom.memory (`a` String) ENGINE = Log",
    )


@pytest.mark.parametrize("name", ["memory", "MEMORY", "conversation"])
def test_a_database_named_like_a_schema_table_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from server.core.schema import preflight

    monkeypatch.setattr(settings, "ch_database", name)
    with pytest.raises(preflight.PreflightError, match="a table or an alias"):
        preflight._refuse_ambiguous_names()


def test_the_configured_names_are_not_schema_qualifiers() -> None:
    from server.core.schema import preflight

    preflight._refuse_ambiguous_names()  # acme-loom / acme_telemetry
    qualifiers = preflight._qualifiers()
    assert {"memory", "conversation"} <= qualifiers  # names the migrations write before a dot
    assert not {"loom", "loom_telemetry"} & qualifiers


@pytest.mark.parametrize(
    "env",
    [{"ch_database": "0", "ch_telemetry_database": "t0"}, {"ch_telemetry_database": "1x"}],
)
def test_a_database_name_starts_with_a_letter(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**env)


def test_every_preflight_reaches_the_name_and_schema_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.schema import preflight
    from tests.unit.test_ch_preflight import FakeCH

    seen: list[str] = []
    monkeypatch.setattr(preflight, "_refuse_ambiguous_names", lambda: seen.append("names"))
    monkeypatch.setattr(preflight, "_refuse_moved_schema", lambda ch: seen.append("schema"))
    preflight.check(FakeCH(), need="server", migrates=False)
    assert seen == ["names", "schema"]
    seen.clear()
    preflight.check(FakeCH(), need="read")
    assert seen == ["names"]


def test_the_runners_lock_table_does_not_make_a_schema_foreign() -> None:
    from server.core.schema import preflight

    class FirstBootStopped:
        """`system.tables` after a first boot made the lock table and stopped."""

        tables = [("loom", "_migrations"), ("acme-loom", "_migration_lock")]

        def query(self, sql: str, parameters: dict[str, Any]) -> SimpleNamespace:
            own, schema, db = parameters["own"], parameters["schema"], parameters["db"]
            other = sum(1 for d, n in self.tables if d == schema and n == "_migrations")
            tables = sum(1 for d, n in self.tables if d == db and n not in own)
            ledger = sum(1 for d, n in self.tables if d == db and n == "_migrations")
            return SimpleNamespace(result_rows=[(other, tables, ledger)])

        def command(self, sql: str, parameters: dict[str, Any] | None = None) -> int:
            return 1  # the database exists

    preflight._refuse_moved_schema(FirstBootStopped())  # type: ignore[arg-type]
    FirstBootStopped.tables.append(("acme-loom", "orders"))
    with pytest.raises(preflight.PreflightError, match="holds 1 tables"):
        preflight._refuse_moved_schema(FirstBootStopped())  # type: ignore[arg-type]
