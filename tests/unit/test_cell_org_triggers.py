"""The org triggers' session marker, the migration session, the refusal's error and the
migration rules that keep the triggers' ground true (server/core/cell_triggers.py,
server/core/pg.py, server/core/schema/pg_migrate.py; docs/architecture/cells.md)."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.core import cell_triggers, pg
from server.core.errors import TenancyRefusedError
from server.core.paths import PG_MIGRATIONS_DIR
from server.core.schema import pg_migrate
from server.settings import settings


@pytest.mark.parametrize("name,marked", [("eu-1", True), ("", False)])
def test_a_process_with_loom_cell_set_marks_its_sessions(
    monkeypatch: pytest.MonkeyPatch, name: str, marked: bool
) -> None:
    monkeypatch.setattr(settings, "cell", name)
    assert pg.marker_settings() == ({"loom.tenancy_checked": "on"} if marked else {})


@pytest.mark.parametrize("name", ["eu-1", ""])
def test_the_pool_and_a_dedicated_session_send_the_marker_only_with_loom_cell(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import asyncpg

    monkeypatch.setattr(settings, "cell", name)
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg, "_expected", None)
    sent: list[dict[str, str]] = []

    class _Conn:
        async def close(self, **_k: Any) -> None:
            return None

        def terminate(self) -> None:
            return None

        async def set_type_codec(self, *_a: Any, **_k: Any) -> None:
            return None

    async def _pool(*_a: Any, server_settings: dict[str, str], **_k: Any) -> Any:
        sent.append(server_settings)
        return object()

    async def _connect(*_a: Any, server_settings: dict[str, str], **_k: Any) -> Any:
        sent.append(server_settings)
        return _Conn()

    monkeypatch.setattr(asyncpg, "create_pool", _pool)
    monkeypatch.setattr(asyncpg, "connect", _connect)

    async def _run() -> None:
        monkeypatch.setattr(pg, "_pools", {})
        await pg._pool_for_loop()
        async with pg.dedicated():
            pass

    asyncio.run(_run())
    assert len(sent) == 2
    for server_settings in sent:
        assert (server_settings.get("loom.tenancy_checked") == "on") == bool(name)
        if not name:
            # A deployment that is not a cell sends exactly the parameters it sent before.
            assert "loom.tenancy_checked" not in server_settings


class _Session:
    """A migration session that records its statements."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: str, *_a: Any) -> None:
        self.statements.append(sql)

    async def fetch(self, _sql: str, *_a: Any) -> list[Any]:
        return []


@pytest.mark.parametrize("name", ["eu-1", ""])
@pytest.mark.parametrize("schema", ["loom", "loom_router"])
def test_the_migration_session_for_loom_is_marked_and_ensures_the_triggers(
    monkeypatch: pytest.MonkeyPatch, name: str, schema: str, tmp_path: Any
) -> None:
    monkeypatch.setattr(settings, "cell", name)
    session = _Session()
    ensured: list[Any] = []

    async def _ensure(conn: Any) -> None:
        ensured.append(conn)

    monkeypatch.setattr(cell_triggers, "ensure", _ensure)

    @contextlib.asynccontextmanager
    async def _connect() -> AsyncIterator[Any]:
        yield session

    asyncio.run(pg_migrate.migrate(tmp_path, connect=_connect, schema=schema))
    marked = "SET loom.tenancy_checked = 'on'" in session.statements
    # Whatever LOOM_CELL is: the migration's data steps rewrite rows already stored.
    assert marked == (schema == "loom")
    assert ensured == ([session] if schema == "loom" else [])
    if marked:  # right after the lock, before any file
        assert session.statements.index("SET loom.tenancy_checked = 'on'") == 1


def test_the_triggers_refusal_maps_to_a_503() -> None:
    class _Refused(Exception):
        sqlstate = "LT001"

    with pytest.raises(TenancyRefusedError) as caught:
        pg._raise_if_unavailable(_Refused("cell holds no tenancy row for org x"))
    assert caught.value.status_code == 503 and caught.value.code == "TENANCY_REFUSED"

    class _Other(Exception):
        sqlstate = "42P01"

    pg._raise_if_unavailable(_Other())  # not ours: the caller re-raises it as it is


def test_the_lock_key_the_troubleshooting_drop_step_prints_is_the_migration_lock() -> None:
    from pathlib import Path

    page = (
        Path(pg.__file__).resolve().parents[2] / "docs/operations/troubleshooting.md"
    ).read_text()
    assert page.count(str(pg_migrate._LOCK_KEY)) >= 2  # the state transaction and the drop step


# The migration rules the triggers rest on (.claude/rules/migrations.md).
_FILES = sorted(PG_MIGRATIONS_DIR.glob("*.sql"))


def _sql(path: Any) -> str:
    """A file's statements, comments dropped."""
    return re.sub(r"--[^\n]*", "", path.read_text())


# A statement that sets or resets the marker, across lines too (never past its `;`).
_SETS_MARKER = re.compile(
    r"\b(SET|RESET|set_config|ALTER\s+(ROLE|DATABASE))\b[^;]*loom\.tenancy_checked", re.I | re.S
)


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_no_migration_resets_or_sets_the_marker(path: Any) -> None:
    sql = _sql(path)
    assert not re.search(r"\bRESET\s+ALL\b", sql, re.I)
    assert not _SETS_MARKER.search(sql)


def test_the_marker_rule_catches_a_statement_split_across_lines() -> None:
    assert _SETS_MARKER.search("SELECT set_config(\n  'loom.tenancy_checked', 'on', false);")
    assert not _SETS_MARKER.search("SET lock_timeout = '5s'; SELECT 'loom.tenancy_checked';")


# An `org` column retyped, renamed or dropped; `COLUMN` is optional in each.
_CHANGES_ORG = (
    re.compile(r"\bALTER\s+(?:COLUMN\s+)?\"?org\"?\s+(?:SET\s+DATA\s+)?TYPE\b", re.I),
    re.compile(r"\bRENAME\s+(?:COLUMN\s+)?\"?org\"?\s+TO\b", re.I),
    re.compile(r"\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?\"?org\"?(?!\w)", re.I),
)
# An `org` column added to a table after it was created: its key is not checked here.
_ADDS_ORG = re.compile(r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\"?org\"?\s", re.I)


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_no_migration_retypes_renames_drops_or_adds_an_org_column(path: Any) -> None:
    sql = _sql(path)
    for rule in _CHANGES_ORG:
        assert not rule.search(sql), rule.pattern
    # An org column belongs in its CREATE TABLE, where the key test reads it.
    assert not _ADDS_ORG.search(sql)


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE loom.t ALTER org TYPE bigint;",
        "ALTER TABLE loom.t ALTER COLUMN org SET DATA TYPE text;",
        "ALTER TABLE loom.t RENAME org TO tenant;",
        "ALTER TABLE loom.t DROP org;",
        "ALTER TABLE loom.t DROP COLUMN IF EXISTS org;",
        "ALTER TABLE loom.t DROP COLUMN org CASCADE;",
        "ALTER TABLE loom.t DROP COLUMN org RESTRICT;",
    ],
)
def test_the_org_column_rules_catch_every_spelling(statement: str) -> None:
    assert any(rule.search(statement) for rule in _CHANGES_ORG)
    assert _ADDS_ORG.search("ALTER TABLE loom.t ADD COLUMN org text;")
    assert not any(rule.search("ALTER TABLE loom.t DROP organisation;") for rule in _CHANGES_ORG)


def _tables(sql: str) -> list[tuple[str, str]]:
    """(name, body) of each CREATE TABLE in a file."""
    out = []
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)\s*\(", sql, re.I):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        out.append((m.group(1), sql[m.end() : i - 1]))
    return out


def test_an_org_keyed_table_leads_its_primary_key_with_org() -> None:
    checked = 0
    for path in _FILES:
        for name, body in _tables(_sql(path)):
            if not re.search(r"^\s*org\s", body, re.I | re.M):
                continue
            checked += 1
            inline = re.search(r"^\s*org\s[^,\n]*PRIMARY KEY", body, re.I | re.M)
            key = re.search(r"PRIMARY KEY\s*\(\s*(\w+)", body, re.I)
            assert inline or (key and key.group(1) == "org"), f"{path.name}: {name}"
    assert checked >= 10


# loom doctor's cell checks (server/cli/doctor_cell.py).
def _defect(kind: str) -> Any:
    return cell_triggers.Defect(kind, f"a {kind} defect")


def test_doctor_names_a_partial_state_and_an_unmarked_session() -> None:
    from server.cli import doctor_cell

    grades = dict((n, (g, d)) for n, g, d in doctor_cell._grades("partial", False, True, []))
    assert grades["cell_state"][0] == "WARN" and "troubleshooting" in grades["cell_state"][1]
    assert grades["cell_marker"][0] == "WARN"
    assert grades["cell_marker_everywhere"][0] == "WARN"


@pytest.mark.parametrize("state,named", [("enforced", True), ("recording", False), ("open", False)])
def test_doctor_names_an_absent_org_trigger_only_on_an_enforced_cell(
    state: str, named: bool
) -> None:
    from server.cli import doctor_cell

    grades = {n: g for n, g, _d in doctor_cell._grades(state, True, False, [_defect("absent")])}
    assert grades["cell_triggers"] == ("WARN" if named else "PASS")
    assert grades["cell_marker"] == "PASS" and "cell_marker_everywhere" not in grades


def test_doctor_names_every_other_defect_in_any_state() -> None:
    from server.cli import doctor_cell

    rows = doctor_cell._grades("recording", True, False, [_defect("cell_first")])
    assert ("cell_triggers", "WARN", "a cell_first defect") in rows


def test_doctor_refuses_a_cell_with_no_pg_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor_cell

    monkeypatch.setattr(settings, "pg_dsn", "")
    rows = asyncio.run(doctor_cell._checks(object()))
    assert rows[0][1] == "FAIL" and "LOOM_PG_DSN" in rows[0][2]


@pytest.mark.parametrize(
    "raised,grade,text",
    [
        ("mismatch", "FAIL", "deployment check refused"),
        ("access", "FAIL", "deployment check refused"),
        ("outage", "WARN", "not read"),
    ],
)
def test_doctor_fails_only_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch, raised: str, grade: str, text: str
) -> None:
    from server.cli import doctor_cell
    from server.core import pg_profile
    from server.core.pg import AuthorityMismatch

    class _Denied(Exception):
        code = 497

    async def _recorded(_ch: Any) -> dict[str, Any]:
        if raised == "mismatch":
            raise AuthorityMismatch("the deployment records the ClickHouse authority")
        if raised == "access":
            raise _Denied("Code: 497. ACCESS_DENIED")
        raise OSError("connection refused")

    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg_profile, "recorded_authority", _recorded)
    rows = asyncio.run(doctor_cell._checks(object()))
    assert len(rows) == 1 and rows[0][1] == grade and text in rows[0][2]


def test_doctor_passes_before_the_first_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor_cell
    from server.core import pg_profile

    async def _none(_ch: Any) -> None:
        return None

    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg_profile, "recorded_authority", _none)
    rows = asyncio.run(doctor_cell._checks(object()))
    assert rows == [("cell_state", "PASS", "cell checks not run: before the first boot")]


def test_a_dedicated_blocks_trigger_refusal_is_the_same_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    class _Conn:
        async def close(self, **_k: Any) -> None:
            return None

        def terminate(self) -> None:
            return None

        async def set_type_codec(self, *_a: Any, **_k: Any) -> None:
            return None

    async def _connect(*_a: Any, **_k: Any) -> Any:
        return _Conn()

    class _Refused(Exception):
        sqlstate = "LT001"

    class _Deadlock(Exception):
        sqlstate = "40P01"

    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg, "_expected", None)
    monkeypatch.setattr(asyncpg, "connect", _connect)

    async def _raise(e: Exception) -> None:
        async with pg.dedicated():
            raise e

    with pytest.raises(TenancyRefusedError):
        asyncio.run(_raise(_Refused("cell holds no tenancy row for org x")))
    # Any other error of the block passes through as it is (an erasure's retry reads it).
    with pytest.raises(_Deadlock):
        asyncio.run(_raise(_Deadlock()))


def _recorded_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg_profile

    async def _recorded(_ch: Any) -> dict[str, Any]:
        return {"deployment_id": "dep-1", "epoch": 1}

    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg_profile, "recorded_authority", _recorded)
    monkeypatch.setattr(pg, "_expected", None)


def test_doctor_refuses_a_postgres_of_another_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's own deployment check refuses it: the exit fails, nothing is read."""
    from server.cli import doctor_cell
    from server.core.pg import AuthorityMismatch

    _recorded_ok(monkeypatch)

    async def _other() -> Any:
        raise AuthorityMismatch("LOOM_PG_DSN names another deployment's database")

    monkeypatch.setattr(doctor_cell, "_marked_session", _other)
    rows = asyncio.run(doctor_cell._checks(object()))
    assert rows == [
        (
            "cell_deployment",
            "FAIL",
            "deployment check refused: LOOM_PG_DSN names another deployment's database",
        )
    ]


def test_doctor_names_a_postgres_that_never_answers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor_cell

    _recorded_ok(monkeypatch)

    async def _hangs() -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(doctor_cell, "_SESSION_S", 0.05)
    monkeypatch.setattr(doctor_cell, "_marked_session", _hangs)
    rows = asyncio.run(doctor_cell._checks(object()))
    assert len(rows) == 1 and rows[0][:2] == ("cell_state", "WARN") and "not read" in rows[0][2]


def test_doctor_bounds_a_clickhouse_that_stalls(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor_cell
    from server.core import pg_profile

    async def _stalls(_ch: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(settings, "pg_dsn", "postgresql://x/y")
    monkeypatch.setattr(pg_profile, "recorded_authority", _stalls)
    monkeypatch.setattr(doctor_cell, "_SESSION_S", 0.05)
    rows = asyncio.run(doctor_cell._checks(object()))
    assert len(rows) == 1 and rows[0][:2] == ("cell_state", "WARN") and "not read" in rows[0][2]


def test_a_failed_state_read_fails_the_migrating_boot(tmp_path: Any) -> None:
    """ensure skips a table it cannot change, but never a state it cannot read."""

    class _Unreadable(_Session):
        async def fetchrow(self, _sql: str, *_a: Any) -> Any:
            raise OSError("the state rows are not readable")

    session = _Unreadable()

    @contextlib.asynccontextmanager
    async def _connect() -> AsyncIterator[Any]:
        yield session

    with pytest.raises(OSError, match="not readable"):
        asyncio.run(pg_migrate.migrate(tmp_path, connect=_connect, schema="loom"))


def test_missing_names_a_table_this_role_cannot_alter() -> None:
    class _Catalog:
        async def fetch(self, sql: str, *_a: Any) -> list[dict[str, Any]]:
            if "pg_attribute" in sql:
                return [{"relname": "memory_current", "relkind": "r", "owned": False}]
            return []

        async def fetchrow(self, _sql: str, *_a: Any) -> None:
            return None  # no function: nothing to compare the triggers with

    defects = asyncio.run(cell_triggers.missing(_Catalog()))
    assert ("owner", "loom.memory_current: this role cannot alter it") in {
        (d.kind, d.text) for d in defects
    }


def test_missing_keeps_its_other_defects_when_it_cannot_compare_definitions() -> None:
    class _NoTemp:
        def transaction(self) -> Any:
            class _Tx:
                async def start(self) -> None:
                    return None

                async def rollback(self) -> None:
                    return None

            return _Tx()

        async def execute(self, sql: str, *_a: Any) -> None:
            if "TEMP TABLE" in sql:
                raise PermissionError("permission denied to create temporary tables")

        async def fetch(self, sql: str, *_a: Any) -> list[dict[str, Any]]:
            if "pg_attribute" in sql:
                return [{"relname": "memory_current", "relkind": "r", "owned": True}]
            return []

        async def fetchrow(self, _sql: str, *_a: Any) -> dict[str, Any]:
            body, config = cell_triggers.expected_functions()[-1]
            return {"volatile": "v", "definer": False, "body": body, "config": config}

    kinds = {d.kind: d.text for d in asyncio.run(cell_triggers.missing(_NoTemp()))}
    assert kinds["definition"] == "trigger definitions not compared: PermissionError"
    assert "absent" in kinds and "cell_first" in kinds
    assert "function" not in kinds


def test_the_expected_functions_are_read_only_from_the_files_that_define_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from server.core.schema import pg_migrate

    shipped = pg_migrate.migration_files()
    helper = tmp_path / "900_helper.sql"
    helper.write_text(
        "CREATE FUNCTION loom.other() RETURNS void LANGUAGE sql\n"
        "    SET search_path = pg_catalog\nAS $$ SELECT $$;\n"
        "COMMENT ON FUNCTION loom.tenancy_refuse_unheld() IS 'org triggers';\n"
    )
    altered = tmp_path / "901_alter.sql"
    altered.write_text("ALTER FUNCTION loom.tenancy_refuse_unheld() OWNER TO loom;\n")
    monkeypatch.setattr(pg_migrate, "migration_files", lambda: [*shipped, helper, altered])
    found = cell_triggers.expected_functions()
    assert len(found) == 1 and "cell holds no tenancy row" in found[0][0]


def test_a_bound_that_ran_out_is_named_on_a_not_read_line() -> None:
    from server.cli.doctor_cell import _reason

    assert _reason(TimeoutError()) == "TimeoutError: no answer within the bound"
    assert _reason(OSError("refused")) == "OSError: refused"


def test_the_expected_function_is_read_from_its_migration() -> None:
    body, config = cell_triggers.expected_functions()[-1]
    assert "RAISE EXCEPTION 'cell holds no tenancy row for org %'" in body
    assert not body.lstrip().startswith("AS") and "$$" not in body
    assert config == ["search_path=pg_catalog, loom, pg_temp"]


def test_the_refusal_reaches_a_caller_as_a_server_fault_not_retried() -> None:
    """The envelope both transports send: 503, configuration, not retryable."""
    from server.core.errors import classified

    e = TenancyRefusedError("cell holds no tenancy row for org x")
    detail = classified(e.to_detail(), e.status_code)
    assert e.status_code == 503
    assert detail["code"] == "TENANCY_REFUSED"
    assert detail["category"] == "configuration" and detail["retryable"] is False


class _Cell:
    """An enforced cell's catalog with one guarded table and no trigger yet; `execute`
    raises `error` on a CREATE TRIGGER."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def transaction(self) -> Any:
        @contextlib.asynccontextmanager
        async def _tx() -> AsyncIterator[None]:
            yield

        return _tx()

    async def fetchrow(self, _sql: str, *_a: Any) -> dict[str, Any]:
        return {
            "is_first": False,
            "cell": "eu-2",
            "mode": "enforced",
            "has_own": True,
            "has_tenancy": True,
        }

    async def fetch(self, sql: str, *_a: Any) -> list[dict[str, Any]]:
        if "pg_attribute" in sql:
            return [{"relname": "memory_current", "relkind": "r", "owned": False}]
        return []

    async def execute(self, sql: str, *_a: Any) -> None:
        if sql.startswith("CREATE TRIGGER"):
            raise self.error


def test_ensure_serves_past_a_table_the_role_cannot_alter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncpg

    denied = asyncpg.exceptions.InsufficientPrivilegeError("must be owner of table memory_current")
    with caplog.at_level("WARNING", logger="server.core.cell_triggers"):
        asyncio.run(cell_triggers.ensure(_Cell(denied)))
    assert any("not applied on loom.memory_current" in r.getMessage() for r in caplog.records)


def test_ensure_fails_the_boot_on_any_other_ddl_error() -> None:
    """A DDL that never applies must not leave every table unguarded behind log lines."""
    import asyncpg

    broken = asyncpg.exceptions.PostgresSyntaxError("syntax error at or near WHEN")
    with pytest.raises(asyncpg.exceptions.PostgresSyntaxError):
        asyncio.run(cell_triggers.ensure(_Cell(broken)))


def test_the_refusal_answers_no_org_and_logs_the_text(caplog: pytest.LogCaptureFixture) -> None:
    class _Refused(Exception):
        sqlstate = "LT001"
        table_name = "memory_current"
        message = "cell holds no tenancy row for org other-org"

    with (
        caplog.at_level("WARNING", logger="server.core.pg"),
        pytest.raises(TenancyRefusedError) as caught,
    ):
        pg._raise_if_unavailable(_Refused("cell holds no tenancy row for org other-org"))
    assert "other-org" not in str(caught.value) and "loom doctor" in str(caught.value)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "other-org" in logged and "loom.memory_current" in logged


@pytest.mark.parametrize("name,runs", [("eu-1", True), ("", False)])
def test_doctor_runs_the_cell_checks_only_with_loom_cell(
    monkeypatch: pytest.MonkeyPatch, name: str, runs: bool
) -> None:
    from server.cli import doctor, doctor_cell

    monkeypatch.setattr(settings, "cell", name)
    monkeypatch.setattr(settings, "ch_host", "127.0.0.1")
    monkeypatch.setattr(doctor, "_doctor_ch_reachable", lambda *_a: (True, "ok"))
    monkeypatch.setattr(
        doctor, "_doctor_preflight_check", lambda: ("clickhouse_preflight", "PASS", "")
    )
    monkeypatch.setattr(doctor, "_doctor_ch_read_checks", lambda _s: [])
    monkeypatch.setattr(doctor, "_doctor_llm_provider_check", lambda: ("llm_provider", "PASS", ""))
    monkeypatch.setattr(doctor, "_doctor_scim_token_check", lambda _s: ("scim_token", "PASS", ""))
    called: list[bool] = []
    monkeypatch.setattr(
        doctor_cell, "cell_checks", lambda: called.append(True) or [("cell_state", "PASS", "")]
    )
    names = [n for n, _g, _d in doctor._run_live_checks()]
    assert called == ([True] if runs else []) and ("cell_state" in names) == runs
