"""The read-side skew guard.

A statement naming a table that a PENDING migration creates is refused
before it reaches ClickHouse (`SchemaPendingError`, one warning per table per
process) and the request's ledger row says `schema_skipped=<table>`; DDL is
never refused; a fully migrated service sees every statement unchanged; the
pending set comes from `schema_status`'s cached ledger read.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core import ch_schema_guard
from server.core import request_work as rw
from server.core.errors import SchemaPendingError
from server.core.schema import status as ss

REAL_DIR = ss.MIGRATIONS_DIR
# A journal one file ahead of the service: the branch file is not in the ledger.
BRANCH = "002_branch.sql"
JOURNAL = {
    "000_ledger.sql": "CREATE TABLE IF NOT EXISTS loom._migrations (a Int8) ENGINE = Memory;\n",
    "001_base.sql": (
        "CREATE TABLE IF NOT EXISTS loom.agent_events (a Int8) ENGINE = Memory;\n"
        "CREATE TABLE IF NOT EXISTS loom.memory (a Int8) ENGINE = Memory;\n"
    ),
    BRANCH: "CREATE TABLE IF NOT EXISTS loom.memory_reinforcement (a Int8) ENGINE = Memory;\n",
    "003_links.sql": "CREATE TABLE IF NOT EXISTS loom.memory_chunk_links (a Int8) ENGINE = Memory;\n",
    "004_alter.sql": "ALTER TABLE loom.agent_events ADD COLUMN IF NOT EXISTS b Int8;\n",
}


def _applied() -> list[str]:
    """The ledger: every journal file but the branch one."""
    return [f for f in ss.repo_migrations() if f != BRANCH]


class _Result:
    column_names = ("filename",)
    summary: dict[str, Any] = {}

    @property
    def result_rows(self) -> list[tuple[str, ...]]:
        return [(f,) for f in _applied()]


class _FakeAsync:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.calls.append(("query", sql))
        return _Result()

    async def command(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.calls.append(("command", sql))
        return "ok"

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> Any:
        self.calls.append(("insert", table))
        return _Result()

    async def close(self) -> None:
        pass


def _client(fake: _FakeAsync) -> ch_mod.CHClient:
    c = ch_mod.CHClient()
    c._client = fake
    return c


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    journal = tmp_path / "migrations"
    journal.mkdir()
    for name, sql in JOURNAL.items():
        (journal / name).write_text(sql, encoding="utf-8")
    monkeypatch.setattr(ss, "MIGRATIONS_DIR", journal)
    ch_schema_guard.schema_guard_reset()
    ss.reset_cache()
    ch_mod.describe_cache_clear()
    yield
    ch_schema_guard.schema_guard_reset()
    ss.reset_cache()
    rw._current.set(None)


def _pending(monkeypatch: pytest.MonkeyPatch, files: list[str] | None) -> None:
    """Make `schema_status` report `files` as pending without a ledger."""

    async def _status(**_: Any) -> dict[str, Any]:
        return {"pending": files, "ledger_only": [], "applied": 1}

    monkeypatch.setattr(ss, "schema_status", _status)


# ------------------------------------------------------- the pending parser


def test_pending_created_tables_reads_the_journal_files() -> None:
    assert ss.pending_created_tables([BRANCH, "003_links.sql"]) == {
        "memory_reinforcement": BRANCH,
        "memory_chunk_links": "003_links.sql",
    }
    # An ALTER-only file creates nothing; an unknown file guards nothing; None
    # (an unreadable ledger) is empty.
    assert ss.pending_created_tables(["004_alter.sql"]) == {}
    assert ss.pending_created_tables(["999_not_here.sql"]) == {}
    assert ss.pending_created_tables(None) == {}


def test_the_real_journal_creates_every_table_of_the_schema() -> None:
    """The parser sees every CREATE shape the shipped journal uses."""
    from tests.unit.schema_ddl import schema_text

    created = {t for f in ss.repo_migrations(REAL_DIR) for t in ss.created_tables(f, REAL_DIR)}
    dumped = set(re.findall(r"^-- loom\.(\w+)$", schema_text(), re.M))
    assert dumped and dumped <= created, sorted(dumped - created)


def test_created_tables_sees_every_create_shape(tmp_path: Path) -> None:
    (tmp_path / "900_x.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.alpha (a Int8) ENGINE = Memory;\n"
        "CREATE MATERIALIZED VIEW loom.beta_mv TO loom.alpha AS SELECT 1;\n"
        "create table `loom`.`gamma` (a Int8) ENGINE = Memory;\n"
        "ALTER TABLE loom.delta ADD COLUMN x Int8;\n",
        encoding="utf-8",
    )
    assert ss.created_tables("900_x.sql", tmp_path) == ("alpha", "beta_mv", "gamma")


def test_statement_matching_is_by_whole_table_name() -> None:
    tables = {"memory_reinforcement": BRANCH}
    assert (
        ch_schema_guard.pending_table_named(
            "SELECT * FROM loom.memory_reinforcement FINAL WHERE 1", tables
        )
        == "memory_reinforcement"
    )
    assert (
        ch_schema_guard.pending_table_named("SELECT * FROM loom.memory_reinforcement_old", tables)
        is None
    )
    assert ch_schema_guard.pending_table_named("SELECT * FROM loom.memory", tables) is None
    assert ch_schema_guard.pending_table_named(
        "", tables, insert_table="loom.memory_reinforcement"
    ) == ("memory_reinforcement")
    assert ch_schema_guard.pending_table_named("", tables, insert_table="loom.memory") is None
    assert (
        ch_schema_guard.pending_table_named("SELECT * FROM loom.memory_reinforcement", {}) is None
    )


def test_a_pending_table_outside_loom_is_matched_by_its_database() -> None:
    tables = {"loom_telemetry.sli_minute": "043_request_sli.sql"}
    named = ch_schema_guard.pending_table_named
    assert named("SELECT sum(requests) FROM loom_telemetry.sli_minute", tables) == (
        "loom_telemetry.sli_minute"
    )
    assert named("SELECT * FROM loom.sli_minute", tables) is None
    assert named("", tables, insert_table="loom_telemetry.sli_minute") == (
        "loom_telemetry.sli_minute"
    )


# ------------------------------------------------------------- the refusal


async def test_a_read_of_a_pending_table_is_refused_before_the_driver(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pending(monkeypatch, [BRANCH])
    fake = _FakeAsync()
    c = _client(fake)
    bag = rw.begin()
    with caplog.at_level(logging.WARNING, logger="server.core.ch_schema_guard"):
        with pytest.raises(SchemaPendingError) as ei:
            await c.query_all(
                "SELECT memory_key FROM loom.memory_reinforcement FINAL WHERE org = 'o'"
            )
        with pytest.raises(SchemaPendingError):
            await c.query_one("SELECT count() FROM loom.memory_reinforcement")
    assert fake.calls == []  # never reached ClickHouse: 0 code-60 exceptions
    assert ei.value.code == "SCHEMA_PENDING" and ei.value.status_code == 503
    assert ei.value.extra == {
        "table": "memory_reinforcement",
        "migration": BRANCH,
    }
    assert bag.schema_skipped == ["memory_reinforcement"]  # once, not twice
    warnings = [r for r in caplog.records if "pending migration" in r.getMessage()]
    assert len(warnings) == 1  # one line per (table, process)


async def test_writes_are_refused_and_ddl_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _pending(monkeypatch, [BRANCH])
    fake = _FakeAsync()
    c = _client(fake)
    rw.begin()
    with pytest.raises(SchemaPendingError):
        await c.insert("loom.memory_reinforcement", [{"a": 1}])
    with pytest.raises(SchemaPendingError):
        await c.execute("INSERT INTO loom.memory_reinforcement (a) VALUES (1)")
    assert fake.calls == []
    # The migrator's own CREATE, and any DDL, passes: it is what ends the skew.
    await c.execute("CREATE TABLE IF NOT EXISTS loom.memory_reinforcement (a Int8) ENGINE = Memory")
    await c.execute("ALTER TABLE loom.memory_reinforcement ADD COLUMN b Int8")
    assert [k for k, _ in fake.calls] == ["command", "command"]


async def test_an_additive_leg_continues_and_the_ledger_names_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reinforcement fold catches every exception and keeps the rows'
    write-time base; under the guard it does the same, without a code-60
    round trip, and the request's row says which leg went without."""
    from server.memory.app.retrieval import search as ms
    from server.observability.event_args import request_work_columns

    _pending(monkeypatch, [BRANCH])
    fake = _FakeAsync()
    c = _client(fake)
    bag = rw.begin()

    class _Q:
        org, namespace = "o", "n"
        reinforcement_weight, reinforcement_half_life_days = 0.5, 30.0

    rows = [{"memory_key": "k1", "rank_score": 0.9, "reinforcement_count": 0}]
    folded = await ms._fold_reinforcement_into_rows(c, _Q(), rows)
    assert folded == 0 and rows[0]["rank_score"] == 0.9
    assert fake.calls == []
    assert request_work_columns(bag)["schema_skipped"] == "memory_reinforcement"


async def test_a_fully_migrated_service_sees_every_statement_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pending(monkeypatch, [])
    fake = _FakeAsync()
    c = _client(fake)
    rw.begin()
    stmts = [
        "SELECT memory_key FROM loom.memory_reinforcement FINAL",
        "SELECT * FROM loom.memory WHERE 1",
    ]
    for sql in stmts:
        await c.query_all(sql)
    await c.insert("loom.memory_reinforcement", [{"a": 1}])
    issued = [sql for kind, sql in fake.calls if kind == "query" and not sql.startswith("DESCRIBE")]
    assert issued == stmts  # byte-identical text, same order
    assert rw.current() is not None and rw.current().schema_skipped == []


async def test_the_guard_reads_the_ledger_through_schema_status_once_per_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    class _Ledger:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            calls["n"] += 1
            assert "loom._migrations" in sql
            return [{"filename": f} for f in _applied()]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Ledger())
    tables = await ch_schema_guard.pending_tables(now=1000.0)
    assert tables == {"memory_reinforcement": BRANCH}
    assert await ch_schema_guard.pending_tables(now=1030.0) == tables and calls["n"] == 1
    await ch_schema_guard.pending_tables(now=1061.0)
    assert calls["n"] == 1  # schema_status's own 60 s cache answered
    ss.reset_cache()
    await ch_schema_guard.pending_tables(now=1122.0)
    assert calls["n"] == 2


async def test_an_unreadable_ledger_or_the_switch_off_guards_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Down:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("Code: 497. Not enough privileges")

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Down())
    assert await ch_schema_guard.pending_tables(now=5.0) == {}


async def test_the_ledger_read_itself_is_never_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`schema_status` reads `loom._migrations` through this client while the
    guard refreshes; the in-flight flag lets that statement through."""
    fake = _FakeAsync()
    c = _client(fake)
    monkeypatch.setattr(ch_mod, "get_client", lambda: c)
    tables = await ch_schema_guard.pending_tables(now=50.0)
    assert any(sql.startswith("SELECT filename FROM loom._migrations") for _, sql in fake.calls)
    assert tables == {"memory_reinforcement": BRANCH}


async def test_an_anomalous_ledger_idles_the_guard_instead_of_refusing_everything(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ledger that reports nothing applied, or the whole journal pending
    (the base file creates `loom.agent_events`), is not a branch ahead
    of the service; refusing every statement for a TTL would be worse than
    the skew. One warning, guard idle."""
    many = [f"{i:04d}_x.sql" for i in range(ch_schema_guard._SCHEMA_GUARD_MAX_PENDING + 1)]
    _pending(monkeypatch, many)  # applied 1, pending past the cap
    with caplog.at_level(logging.WARNING, logger="server.core.ch_schema_guard"):
        assert await ch_schema_guard.pending_tables(now=10.0) == {}
    assert any("schema guard idle" in r.getMessage() for r in caplog.records)
    ch_schema_guard.schema_guard_reset()

    async def _none_applied(**_: Any) -> dict[str, Any]:
        return {"pending": [BRANCH], "ledger_only": [], "applied": 0}

    monkeypatch.setattr(ss, "schema_status", _none_applied)
    assert await ch_schema_guard.pending_tables(now=20.0) == {}
    fake = _FakeAsync()
    c = _client(fake)
    monkeypatch.setattr(ch_mod, "PROJECTED_TABLES", frozenset())  # no DESCRIBE first
    await c.insert("loom.agent_events", [{"a": 1}])
    assert [k for k, _ in fake.calls if k == "insert"] == ["insert"]


def test_reset_cache_also_forgets_the_guard_set() -> None:
    ch_schema_guard._schema_guard["tables"] = {"memory_reinforcement": BRANCH}
    ch_schema_guard._schema_guard["at"] = 5.0
    ss.reset_cache()
    assert (
        ch_schema_guard._schema_guard["tables"] == {}
        and ch_schema_guard._schema_guard["at"] is None
    )


def test_the_sdk_and_the_api_doc_know_the_code() -> None:
    from loom.errors import ERROR_CODES, BackendUnavailable, SchemaPending

    assert ERROR_CODES["SCHEMA_PENDING"] is SchemaPending
    assert issubclass(SchemaPending, BackendUnavailable)
    doc = Path("docs/reference/http-api.md").read_text(encoding="utf-8")
    assert "`SCHEMA_PENDING`" in doc
